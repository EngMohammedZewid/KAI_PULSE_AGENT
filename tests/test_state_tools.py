"""Phase 2 checks: state-aware tools run inside a real LangGraph ToolNode.

Each test builds START -> ToolNode -> END over KaiState with an in-memory
checkpointer, so tool Commands go through the real reducers and state carries
across calls on the same thread. QuickChart calls are patched out.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from kai.files import LocalFileStore, read_text
from kai.state import KaiState, add_charts, add_unique, merge_files
from kai.tools import STATE_TOOLS
from tests.test_tools import STATEMENT_CSV

REPO = Path(__file__).resolve().parents[1]
FAKE_CHART = "https://quickchart.io/chart/render/fake-{n}"


def _halves() -> tuple[bytes, bytes]:
    lines = STATEMENT_CSV.strip().split("\n")
    header, rows = lines[0], lines[1:]
    jan_feb = "\n".join([header] + rows[:6]).encode()
    mar = "\n".join([header] + rows[6:]).encode()
    return jan_feb, mar


class ToolGraphTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalFileStore(self.tmp.name)
        builder = StateGraph(KaiState)
        builder.add_node("tools", ToolNode(STATE_TOOLS))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        self.graph = builder.compile(checkpointer=InMemorySaver())
        self.cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "file_store": self.store}}
        n = iter(range(1000))
        patcher_targets = ["tools.bank_statement_tool._chart_url",
                           "tools.chart_csv_tool._chart_url",
                           "tools.chart_excel_ts_tool._chart_url"]
        for target in patcher_targets:
            p = mock.patch(target, side_effect=lambda *a, **k: FAKE_CHART.format(n=next(n)))
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, name: str, data: bytes) -> str:
        ref = self.store.put(name, data)
        self.graph.update_state(self.cfg, {"files": [ref]})
        return ref["id"]

    def call(self, name: str, args: dict | None = None) -> tuple[str, dict]:
        msg = AIMessage(content="", tool_calls=[{"name": name, "args": args or {},
                                                "id": "call_" + uuid.uuid4().hex[:8]}])
        state = self.graph.invoke({"messages": [msg]}, self.cfg)
        last = state["messages"][-1]
        self.assertIsInstance(last, ToolMessage)
        return str(last.content), state


class SchemaTests(unittest.TestCase):
    def test_model_never_passes_file_contents_or_analysis(self):
        forbidden = {"csv_content", "csv_files", "statement_text", "analysis_json",
                     "file_content", "state", "config", "tool_call_id"}
        for t in STATE_TOOLS:
            visible = set(t.tool_call_schema.model_json_schema().get("properties", {}))
            self.assertFalse(visible & forbidden, f"{t.name} exposes {visible & forbidden}")


class ReducerTests(unittest.TestCase):
    def test_merge_files_replaces_by_id(self):
        a = {"id": "f_1", "name": "a.csv"}
        a2 = {"id": "f_1", "name": "a-v2.csv"}
        b = {"id": "f_2", "name": "b.csv"}
        self.assertEqual(merge_files([a, b], [a2]), [a2, b])

    def test_add_unique_and_charts(self):
        self.assertEqual(add_unique(["x"], ["x", "y"]), ["x", "y"])
        c = {"title": "t", "url": "u", "source": "s"}
        self.assertEqual(add_charts([c], [dict(c)]), [c])


class FinanceFlowTests(ToolGraphTestCase):
    def test_add_then_analyze_all(self):
        jan_feb, mar = _halves()
        f1 = self.upload("jan_feb.csv", jan_feb)
        f2 = self.upload("mar.csv", mar)

        out, state = self.call("queue_statement", {"file_id": f1})
        self.assertIn("Statement 1 received", out)
        out, state = self.call("queue_statement", {"file_id": f2})
        self.assertIn("Statement 2 received", out)
        self.assertEqual(state["statements"], [f1, f2])
        self.assertIsNone(state.get("analysis"))

        out, state = self.call("analyze_statements")  # Scenario C: no ids, uses the queue
        metrics = state["analysis"]["metrics"]
        self.assertEqual(state["analysis"]["file_ids"], [f1, f2])
        self.assertEqual(metrics["months"], ["2026-01", "2026-02", "2026-03"])
        self.assertAlmostEqual(metrics["avg_monthly_income"], 20666.67, places=2)
        self.assertIn("avg_monthly_income", out)
        self.assertLess(len(out), 3000, "tool reply should be a brief, not the whole JSON")

        out, state2 = self.call("analyze_statements", {"file_ids": [f2, f1]})
        self.assertTrue(out.startswith("Already analyzed"))
        self.assertEqual(state2["analysis"], state["analysis"])

    def test_mortgage_and_charts_use_stored_analysis(self):
        fid = self.upload("statement.csv", STATEMENT_CSV.encode())
        out, _ = self.call("simulate_mortgage")
        self.assertIn("no analysis yet", out)

        self.call("analyze_statements", {"file_ids": [fid]})
        out, _ = self.call("simulate_mortgage")
        self.assertIn("EGP 3,753", out)
        self.assertIn("EGP 5,003", out)

        out, state = self.call("financial_chart", {"chart_type": "savings"})
        self.assertIn("fake-", out)
        out, state = self.call("financial_chart", {"chart_type": "categories", "title": "Spend"})
        self.assertEqual([c["source"] for c in state["charts"]],
                         ["financial:savings", "financial:categories"])
        self.assertEqual(state["charts"][1]["title"], "Spend")

    def test_errors_leave_state_unchanged(self):
        csv_id = self.upload("a.csv", STATEMENT_CSV.encode())
        txt_id = self.upload("notes.txt", b"hello")
        out, state = self.call("queue_statement", {"file_id": "f_missing"})
        self.assertIn("no uploaded file has id 'f_missing'", out)
        self.assertIn(csv_id, out)
        out, state = self.call("queue_statement", {"file_id": txt_id})
        self.assertIn("not a CSV or PDF", out)
        out, state = self.call("analyze_statements")
        self.assertIn("no statements to analyze", out)
        self.assertFalse(state.get("statements"))
        self.assertIsNone(state.get("analysis"))


class FileToolTests(ToolGraphTestCase):
    def test_list_describe_and_chart_csv(self):
        fid = self.upload("statement.csv", STATEMENT_CSV.encode())
        out, _ = self.call("list_files")
        self.assertIn(f"{fid}: statement.csv (csv", out)

        out, _ = self.call("describe_file", {"file_id": fid})
        for col in ("Date", "Description", "Credit"):
            self.assertIn(col, out)

        out, state = self.call("chart_from_file", {
            "file_id": fid, "x_column": "Date", "y_columns": "Credit,Debit",
            "title": "Cash in and out", "chart_type": "bar"})
        self.assertIn("fake-", out)
        self.assertEqual(state["charts"][-1]["source"], f"file:{fid}")

    def test_describe_excel(self):
        xlsx = REPO / "excel_chart_recommendation_test_sales_data.xlsx"
        fid = self.upload(xlsx.name, xlsx.read_bytes())
        out, _ = self.call("describe_file", {"file_id": fid})
        self.assertIn("Sheets: Sales_Data", out)
        for col in ("Order Date", "Revenue", "Quantity"):
            self.assertIn(col, out)

    def test_pdf_text_is_extracted(self):
        from reportlab.pdfgen import canvas

        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(72, 720, "2026-01-01 Salary 20000")
        c.save()
        ref = self.store.put("statement.pdf", buf.getvalue())
        self.assertEqual(ref["kind"], "pdf")
        self.assertIn("Salary 20000", read_text(self.store, ref))

    def test_store_rejects_keys_outside_root(self):
        ref = self.store.put("a.csv", b"x")
        ref["key"] = "../../escape.csv"
        with self.assertRaises(ValueError):
            self.store.read_bytes(ref)


if __name__ == "__main__":
    unittest.main()
