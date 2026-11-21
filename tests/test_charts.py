"""Interactive charts: plain-data specs, recording them in state, and serving them to the page."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from kai.files import LocalFileStore
from kai.graph import build_graph
from kai.models import DevEchoModel
from kai.server.app import create_app, serialize
from kai.server.repo import MemoryRepo
from kai.state import KaiState
from kai.tools import STATE_TOOLS
from kai.tools._common import record_charts
from tests.test_tools import STATEMENT_CSV
from tools._chart_spec import capture, records_chart, to_spec
from tools.chart_analytics_tools import render_bar_chart

BAR = {
    "type": "bar",
    "data": {"labels": ["Jan", "Feb", "Mar"], "datasets": [
        {"label": "Income", "data": [20000, 20000, 22000], "backgroundColor": "#123456"},
        {"label": "Spending", "data": ["1950", 920.5, None]},
    ]},
    "options": {"plugins": {"title": {"display": True, "text": "Cash flow"}},
                "scales": {"x": {"stacked": True}, "y": {"stacked": True}}},
}


class SpecTests(unittest.TestCase):
    def test_bar_spec_is_plain_data(self):
        spec = to_spec(BAR, unit="EGP")
        self.assertEqual(spec, {
            "kind": "bar", "title": "Cash flow", "labels": ["Jan", "Feb", "Mar"],
            "datasets": [{"label": "Income", "data": [20000.0, 20000.0, 22000.0]},
                         {"label": "Spending", "data": [1950.0, 920.5, None]}],
            "stacked": True, "unit": "EGP"})
        self.assertNotIn("backgroundColor", json.dumps(spec), "styling stays on the page")

    def test_kinds_and_flags(self):
        horizontal = to_spec({"type": "horizontalBar", "data": BAR["data"]})
        self.assertTrue(horizontal["horizontal"])
        self.assertTrue(to_spec({**BAR, "options": {"indexAxis": "y"}})["horizontal"])
        pie = to_spec({"type": "doughnut", "data": {"labels": ["A", "B"],
                                                     "datasets": [{"data": [1, 3]}]}})
        self.assertEqual(pie["kind"], "doughnut")
        area = to_spec({"type": "line", "data": {"labels": ["a"],
                                                  "datasets": [{"data": [1], "fill": True}]}})
        self.assertTrue(area["datasets"][0]["area"])

    def test_unusable_configs_fall_back_to_the_image(self):
        self.assertIsNone(to_spec({"type": "radar", "data": BAR["data"]}))
        self.assertIsNone(to_spec({"type": "bar", "data": {"labels": ["a"], "datasets": [
            {"data": ["function(){alert(1)}", "nope"]}]}}))
        self.assertIsNone(to_spec("not a dict"))
        self.assertIsNone(to_spec({"type": "bar", "data": {"labels": "a,b", "datasets": []}}))

    def test_text_is_capped_and_numbers_are_finite(self):
        spec = to_spec({"type": "line", "data": {"labels": ["x" * 500, ["Q1", "2026"]],
                                                  "datasets": [{"label": "L", "data": [float("inf"), {"y": 5}]}]}})
        self.assertEqual(len(spec["labels"][0]), 120)
        self.assertEqual(spec["labels"][1], "Q1 2026")
        self.assertEqual(spec["datasets"][0]["data"], [None, 5.0])

    def test_recording_only_inside_capture(self):
        helper = records_chart(lambda config, width=1: "https://quickchart.io/chart/render/x")
        helper(BAR)  # outside capture: nothing kept anywhere
        with capture() as found:
            helper(BAR)
        self.assertEqual(found, [("https://quickchart.io/chart/render/x", BAR)])


def _offline():
    """QuickChart is unreachable in tests: tools fall back to the long ?c= URL."""
    p = mock.patch("urllib.request.urlopen", side_effect=OSError("offline"))
    p.start()
    return p


class GraphRecordingTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_offline().stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = LocalFileStore(tmp.name)
        builder = StateGraph(KaiState)
        builder.add_node("tools", ToolNode([*STATE_TOOLS, render_bar_chart], wrap_tool_call=record_charts))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        self.graph = builder.compile(checkpointer=InMemorySaver())
        self.cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "file_store": self.store}}

    def call(self, name, args=None):
        msg = AIMessage(content="", tool_calls=[{"name": name, "args": args or {},
                                                "id": "call_" + uuid.uuid4().hex[:8]}])
        return self.graph.invoke({"messages": [msg]}, self.cfg)

    def test_financial_chart_and_kpis(self):
        ref = self.store.put("jan.csv", STATEMENT_CSV.encode())
        self.graph.update_state(self.cfg, {"files": [ref]})
        state = self.call("analyze_statements", {"file_ids": [ref["id"]]})
        kpis = state["messages"][-1].artifact["kpis"]
        self.assertEqual(kpis["currency"], "EGP")
        self.assertAlmostEqual(kpis["income"], 20666.67, places=2)
        self.assertEqual(kpis["dti"], 14.5)

        state = self.call("financial_chart", {"chart_type": "income_vs_spending"})
        chart = state["charts"][-1]
        self.assertTrue(chart["url"].startswith("https://quickchart.io/chart?"))
        self.assertEqual(chart["spec"]["unit"], "EGP")
        self.assertEqual(len(chart["spec"]["labels"]), 3)
        self.assertGreaterEqual(len(chart["spec"]["datasets"]), 2)

    def test_plain_chart_tool_is_recorded_by_the_wrapper(self):
        state = self.call("render_bar_chart", {
            "categories_json": '["Q1","Q2"]', "title": "Sales",
            "series_json": '[{"name":"North","values":[5,7]},{"name":"South","values":[3,4]}]'})
        chart = state["charts"][-1]
        self.assertEqual(chart["source"], "tool:render_bar_chart")
        self.assertEqual(chart["spec"]["labels"], ["Q1", "Q2"])
        self.assertEqual([d["label"] for d in chart["spec"]["datasets"]], ["North", "South"])
        self.assertIn(chart["url"], str(state["messages"][-1].content))


class SerializeTests(unittest.TestCase):
    def test_reply_carries_its_charts_and_turn_kpis(self):
        url = "https://quickchart.io/chart/render/abc"
        spec = to_spec(BAR)
        messages = [
            HumanMessage("analyze"),
            AIMessage("", tool_calls=[{"name": "analyze_statements", "args": {}, "id": "c1"}]),
            ToolMessage("brief", tool_call_id="c1", artifact={"kpis": {"income": 1}}),
            AIMessage(f'Here you go <img src="{url}" alt="x" />'),
            HumanMessage("thanks"),
            AIMessage("You're welcome."),
        ]
        out = serialize(messages, [{"title": "t", "url": url, "source": "s", "spec": spec},
                                   {"title": "old", "url": "https://quickchart.io/other", "source": "s"}])
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "user", "assistant"])
        self.assertEqual(out[1]["charts"], [{"url": url, "spec": spec}])
        self.assertEqual(out[1]["kpis"], {"income": 1})
        self.assertNotIn("kpis", out[3])
        self.assertNotIn("charts", out[3])


class PageAssetTests(unittest.TestCase):
    def test_chart_scripts_are_served_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(build_graph(DevEchoModel(), checkpointer=InMemorySaver()),
                                           MemoryRepo(), LocalFileStore(tmp)))
            page = client.get("/").text
            self.assertIn('src="/static/vendor/chart.umd.js"', page)
            self.assertIn('src="/static/charts.js"', page)
            lib = client.get("/static/vendor/chart.umd.js")
            self.assertEqual(lib.status_code, 200)
            self.assertIn("Chart.js v4.4.1", lib.text[:200])
            self.assertIn("KaiCharts", client.get("/static/charts.js").text)


if __name__ == "__main__":
    unittest.main()
