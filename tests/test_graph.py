"""Phase 3 checks: whole conversation turns through the compiled graph.

A scripted chat model returns pre-set messages (a routing choice, a tool call,
a final answer), so each test pins down what the LLM "says" and checks that
the fixed nodes, routing, tools, interrupts and state do the right thing.
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from typing import Any
from unittest import mock

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import Field

from kai.files import LocalFileStore
from kai.graph import build_graph
from kai.nodes import is_add_only
from kai.tools.documents import parse_format, with_charts
from tests.test_tools import STATEMENT_CSV


class ScriptedModel(BaseChatModel):
    """Returns the scripted messages in order and records every prompt it gets."""

    script: list[AIMessage] = Field(default_factory=list)
    seen: list[list[Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        if not self.script:
            raise AssertionError("model called more often than scripted")
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])

    def bind_tools(self, tools, **kwargs):
        return self


def route(agent: str) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": "Route", "args": {"agent": agent},
                                      "id": "route_" + uuid.uuid4().hex[:6]}])


def call(name: str, args: dict | None = None) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args or {},
                                      "id": "call_" + uuid.uuid4().hex[:6]}])


def say(text: str) -> AIMessage:
    return AIMessage(text)


def halves() -> tuple[bytes, bytes]:
    lines = STATEMENT_CSV.strip().split("\n")
    return ("\n".join(lines[:7]).encode(), "\n".join([lines[0]] + lines[7:]).encode())


class GraphTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LocalFileStore(self.tmp.name)
        self.model = ScriptedModel()
        self.graph = build_graph(self.model, checkpointer=InMemorySaver())
        self.cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "file_store": self.store}}

    def turn(self, text: str, *, files: dict[str, bytes] | None = None, script=()):
        self.model.script.extend(script)
        uploads = [self.store.put(name, data) for name, data in (files or {}).items()]
        state = self.graph.invoke({"messages": [HumanMessage(text)], "uploads": uploads}, self.cfg)
        self.assertEqual(self.model.script, [], "not every scripted model reply was used")
        return state

    def system_prompt(self, call_index: int = -1) -> str:
        return self.model.seen[call_index][0].content


class StatementFlowTests(GraphTestCase):
    def test_add_statements_without_llm(self):
        jan_feb, mar = halves()
        s = self.turn("add this statement", files={"jan_feb.csv": jan_feb})
        self.assertIn("Statement 1 received", s["messages"][-1].content)
        s = self.turn("Add", files={"mar.csv": mar})
        self.assertIn("Statement 2 received", s["messages"][-1].content)
        self.assertIn("mar.csv", s["messages"][-1].content)
        self.assertEqual(len(s["statements"]), 2)
        self.assertEqual(self.model.seen, [], "Scenario B must not call the LLM")

    def test_analyze_all_then_follow_up(self):
        jan_feb, mar = halves()
        self.turn("add", files={"jan_feb.csv": jan_feb})
        s = self.turn("add", files={"mar.csv": mar})
        ids = s["statements"]

        s = self.turn("analyze all", script=[
            route("finance"), call("analyze_statements"), say("Average income is EGP 20,667.")])
        self.assertEqual(s["route"], "finance")
        self.assertEqual(s["analysis"]["file_ids"], ids)
        self.assertAlmostEqual(s["analysis"]["metrics"]["avg_monthly_income"], 20666.67, places=2)
        self.assertEqual(s["messages"][-1].content, "Average income is EGP 20,667.")
        prompt = self.system_prompt(1)  # call 0 is the router, 1 is the finance agent
        self.assertIn("You handle bank statements", prompt)
        for fid in ids:
            self.assertIn(fid, prompt)

        s = self.turn("how much mortgage can I afford?", script=[
            route("finance"), call("simulate_mortgage"), say("Between EGP 3,753 and 5,003.")])
        tool_reply = [m for m in s["messages"] if isinstance(m, ToolMessage)][-1]
        self.assertIn("EGP 3,753", tool_reply.content)
        self.assertIn("Stored analysis of", self.system_prompt(-2))

    def test_arabic_add_reply(self):
        s = self.turn("أضف هذا الكشف", files={"statement.csv": STATEMENT_CSV.encode()})
        self.assertEqual(s["language"], "Arabic")
        self.assertIn("تم استلام الكشف رقم 1", s["messages"][-1].content)

    def test_upload_with_analysis_request_goes_to_finance(self):
        s = self.turn("analyze this statement", files={"s.csv": STATEMENT_CSV.encode()},
                      script=[route("finance"), say("Which currency?")])
        fid = s["files"][0]["id"]
        self.assertEqual(s["turn_uploads"], [fid])
        self.assertIn(f"{fid}: s.csv (csv)  [uploaded with the latest message]",
                      self.system_prompt(-1))
        s = self.turn("EGP", script=[route("finance"), say("ok")])
        self.assertEqual(s["turn_uploads"], [], "turn_uploads resets each turn")


class ReportTests(GraphTestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch("kai.tools.documents._build", return_value="Report ready: https://example.com/r.pdf")
        self.build = p.start()
        self.addCleanup(p.stop)
        self.graph.update_state(self.cfg, {"charts": [
            {"title": "Savings", "url": "https://quickchart.io/chart/render/s1", "source": "financial:savings"}]})

    def test_asks_format_then_builds(self):
        self.model.script.extend([
            route("documents"),
            call("create_report", {"title": "Q1 Finances", "content": "## Overview\n- Fine"}),
            say("Here is your report: https://example.com/r.pdf")])
        s = self.graph.invoke({"messages": [HumanMessage("write a report")]}, self.cfg)
        interrupts = s.get("__interrupt__") or []
        self.assertEqual(len(interrupts), 1)
        self.assertIn("PDF", interrupts[0].value["question"])
        self.build.assert_not_called()

        s = self.graph.invoke(Command(resume="PDF please"), self.cfg)
        self.assertEqual(self.model.script, [])
        fmt, kwargs = self.build.call_args.args[0], self.build.call_args.kwargs
        self.assertEqual(fmt, "pdf")
        self.assertEqual(kwargs["language"], "English")
        self.assertIn("![Savings](https://quickchart.io/chart/render/s1)", kwargs["content"])
        self.assertEqual(s["report_format"], "pdf")
        self.assertEqual(s["messages"][-1].content, "Here is your report: https://example.com/r.pdf")

    def test_named_format_skips_question(self):
        s = self.turn("make it a Word document", script=[
            route("documents"),
            call("create_report", {"title": "T", "content": "## A\n![Savings](https://quickchart.io/chart/render/s1)",
                                   "format": "docx"}),
            say("done")])
        self.assertFalse(s.get("__interrupt__"))
        self.assertEqual(self.build.call_args.args[0], "docx")
        self.assertEqual(self.build.call_args.kwargs["content"].count("render/s1"), 1,
                         "a chart already in the content is not appended twice")


class RoutingTests(GraphTestCase):
    def test_general(self):
        s = self.turn("hello", script=[route("general"), say("Hi! I can analyze statements...")])
        self.assertEqual(s["route"], "general")
        self.assertIn("explain what KAI Pulse can do", self.system_prompt(-1))

    def test_unparseable_router_reply_falls_back_to_general(self):
        s = self.turn("hmm", script=[say("not a tool call"), say("How can I help?")])
        self.assertEqual(s["route"], "general")
        self.assertEqual(s["messages"][-1].content, "How can I help?")

    def test_each_specialist_gets_only_its_tools(self):
        from kai.graph import SPECIALIST_TOOLS

        names = {k: {t.name for t in v} for k, v in SPECIALIST_TOOLS.items()}
        self.assertIn("analyze_statements", names["finance"])
        self.assertNotIn("send_file_via_sendgrid", names["finance"])
        self.assertIn("create_report", names["documents"])
        self.assertTrue(all(n.startswith("cognos_") for n in names["cognos"]))


class HelperTests(unittest.TestCase):
    def test_add_detection(self):
        self.assertTrue(is_add_only("add this statement"))
        self.assertTrue(is_add_only("أضف"))
        self.assertFalse(is_add_only("add it and analyze"))
        self.assertFalse(is_add_only("what is my address?"))

    def test_parse_format(self):
        self.assertEqual(parse_format("PDF please"), "pdf")
        self.assertEqual(parse_format("Word"), "docx")
        self.assertEqual(parse_format("وورد"), "docx")
        self.assertIsNone(parse_format("whatever"))

    def test_with_charts(self):
        c = [{"title": "A", "url": "u1"}, {"title": "B", "url": "u2"}]
        out = with_charts("text with u1", c)
        self.assertNotIn("![A](u1)", out)
        self.assertIn("![B](u2)", out)


if __name__ == "__main__":
    unittest.main()
