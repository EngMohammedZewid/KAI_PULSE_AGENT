"""Runs eval cases through the real graph and checks what happened.

Every external side effect is replaced by a recording fake in both modes, so
an eval never uploads, emails or sends data to a third-party chart service:
QuickChart, S3 (reports, exports, uploads), SendGrid, Cognos, and the PDF/Word
builder. In live mode only the chat model is real (plus PDF statement
extraction when the case allows it).

A case is a list of turns. A turn sends a user message (optionally with
fixture files) or answers a pending question (`resume`), then checks:

    route           specialist the router picked
    tools           tools that must be called this turn (any order)
    not_tools       tools that must not be called
    llm_calls       exact number of chat-model calls
    interrupt       type of question that must be pending afterwards
    reply_any       the final reply contains at least one of these (case-insensitive)
    reply_each      a list of such groups; every group must match
    reply_arabic    the final reply is written in Arabic
    state           dotted paths in graph state -> expected values (floats to 0.01)
    report          {"format": "pdf", "contains": [...]} for the built report
    email           {"to": "..."} for the sent email
    export          {"format": "csv"} for an exported table

`script` lists what the scripted model says, in order: {"route": agent},
{"call": {tool: args}}, {"say": text}. "$file:<name>" in args becomes that
uploaded file's id.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from kai.files import LocalFileStore
from kai.graph import build_graph
from kai.nodes import _ARABIC

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


# ---------------------------------------------------------------------------
# Fakes for external services
# ---------------------------------------------------------------------------

@dataclass
class SideEffects:
    reports: list[dict] = field(default_factory=list)
    emails: list[dict] = field(default_factory=list)
    exports: list[dict] = field(default_factory=list)
    charts: int = 0


def _fake_services(stack: ExitStack, effects: SideEffects, mock_extraction: Optional[str]) -> None:
    def chart_url(*_a, **_k):
        effects.charts += 1
        return f"https://quickchart.io/chart/render/eval-{effects.charts}"

    for module in ("bank_statement_tool", "chart_csv_tool", "chart_excel_ts_tool",
                   "chart_analytics_tools", "chart_s3_tool", "chart_cos_tool"):
        stack.enter_context(mock.patch(f"tools.{module}._chart_url", side_effect=chart_url))

    def build_report(fmt, **kwargs):
        effects.reports.append({"format": fmt, **kwargs})
        return f"Report ready: https://example.com/reports/eval-{len(effects.reports)}.{fmt}"

    stack.enter_context(mock.patch("kai.tools.documents._build", side_effect=build_report))

    fake_s3 = ("AKIAEVALFAKE", "eval-secret", "eval-bucket", "us-east-2")

    def s3_put(key, body, content_type, *_creds):
        effects.exports.append({"key": key, "content_type": content_type, "size": len(body)})

    stack.enter_context(mock.patch("tools.export_table_tool._s3_env", return_value=fake_s3))
    stack.enter_context(mock.patch("tools.export_table_tool._s3_put", side_effect=s3_put))
    stack.enter_context(mock.patch("tools.export_table_tool._s3_presign",
                                   side_effect=lambda key, *a, **k: f"https://example.com/{key}"))

    class FakeSendGrid:
        def __init__(self, *_a, **_k):
            pass

        def send(self, message):
            data = message.get() if hasattr(message, "get") else {}
            to = [p["email"] for pers in data.get("personalizations", []) for p in pers.get("to", [])]
            effects.emails.append({"to": to, "subject": data.get("subject", "")})
            return types_ns(status_code=202, body=b"", headers={})

    stack.enter_context(mock.patch("tools.sendgrid_email_tool.SendGridAPIClient", FakeSendGrid))
    stack.enter_context(mock.patch("tools.sendgrid_email_tool._sg_env",
                                   return_value=("SG.eval", "kai@example.com", "KAI Pulse")))
    stack.enter_context(mock.patch("tools.sendgrid_email_tool._download_file",
                                   return_value=(b"%PDF-1.4 eval", "application/pdf")))

    stack.enter_context(mock.patch("tools.cognos_analytics_tools._base_url",
                                   return_value="https://cognos.example.com"))
    stack.enter_context(mock.patch("tools.cognos_analytics_tools._api_get", return_value={
        "content": [{"id": "i_sales_dash", "type": "exploration", "name": "Sales Dashboard"},
                    {"id": "i_hr_report", "type": "report", "name": "HR Headcount"}]}))

    if mock_extraction:
        rows = (FIXTURES / mock_extraction).read_text(encoding="utf-8")
        stack.enter_context(mock.patch("tools.bank_statement_tool._llm_call", return_value=rows))


def types_ns(**kw):
    return type("Obj", (), kw)()


# ---------------------------------------------------------------------------
# Scripted model and call counting
# ---------------------------------------------------------------------------

class CallCounter(BaseCallbackHandler):
    def __init__(self):
        self.count = 0

    def on_chat_model_start(self, *args, **kwargs):
        self.count += 1


def _resolve(value: Any, files: dict[str, str]) -> Any:
    if isinstance(value, str) and value.startswith("$file:"):
        return files[value.split(":", 1)[1]]
    if isinstance(value, list):
        return [_resolve(v, files) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, files) for k, v in value.items()}
    return value


def scripted_messages(script: list[dict], files: dict[str, str]) -> list[AIMessage]:
    out = []
    for step in script:
        if "route" in step:
            out.append(AIMessage("", tool_calls=[{"name": "Route", "args": {"agent": step["route"]},
                                                  "id": "r_" + uuid.uuid4().hex[:6]}]))
        elif "call" in step:
            (name, args), = step["call"].items()
            out.append(AIMessage("", tool_calls=[{"name": name, "args": _resolve(args or {}, files),
                                                  "id": "c_" + uuid.uuid4().hex[:6]}]))
        else:
            out.append(AIMessage(step["say"]))
    return out


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _get(state: dict, path: str) -> Any:
    value: Any = state
    for part in path.split("."):
        if isinstance(value, list):
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _same(actual: Any, expected: Any) -> bool:
    if isinstance(expected, float) and isinstance(actual, (int, float)):
        return abs(actual - expected) <= 0.01
    return actual == expected


@dataclass
class TurnResult:
    label: str
    failures: list[str]
    tools: list[str]
    route: str
    llm_calls: int
    seconds: float


@dataclass
class CaseResult:
    id: str
    passed: bool
    turns: list[TurnResult]
    skipped: str = ""


def check_turn(expect: dict, *, state: dict, tools: list[str], reply: str, llm_calls: int,
               interrupt: Optional[str], effects: SideEffects, before: SideEffects) -> list[str]:
    fails = []
    if "route" in expect and state.get("route") != expect["route"]:
        fails.append(f"route: expected {expect['route']}, got {state.get('route') or '(none)'}")
    for t in expect.get("tools", []):
        if t not in tools:
            fails.append(f"tool {t} not called (called: {', '.join(tools) or 'none'})")
    for t in expect.get("not_tools", []):
        if t in tools:
            fails.append(f"tool {t} should not be called")
    if "llm_calls" in expect and llm_calls != expect["llm_calls"]:
        fails.append(f"llm_calls: expected {expect['llm_calls']}, got {llm_calls}")
    if "interrupt" in expect and interrupt != expect["interrupt"]:
        fails.append(f"interrupt: expected {expect['interrupt']}, got {interrupt}")
    if "reply_any" in expect and not any(s.lower() in reply.lower() for s in expect["reply_any"]):
        fails.append(f"reply has none of {expect['reply_any']}: {reply[:160]!r}")
    for group in expect.get("reply_each", []):
        if not any(s.lower() in reply.lower() for s in group):
            fails.append(f"reply has none of {group}: {reply[:160]!r}")
    if expect.get("reply_arabic") and not _ARABIC.search(reply):
        fails.append(f"reply is not in Arabic: {reply[:120]!r}")
    for path, value in (expect.get("state") or {}).items():
        actual = _get(state, path)
        if not _same(actual, value):
            fails.append(f"state {path}: expected {value!r}, got {actual!r}")
    if "report" in expect:
        new = effects.reports[len(before.reports):]
        if not new:
            fails.append("no report was built")
        else:
            r = new[-1]
            if "format" in expect["report"] and r["format"] != expect["report"]["format"]:
                fails.append(f"report format: expected {expect['report']['format']}, got {r['format']}")
            for s in expect["report"].get("contains", []):
                if s not in r.get("content", ""):
                    fails.append(f"report content lacks {s!r}")
            if "language" in expect["report"] and r.get("language") != expect["report"]["language"]:
                fails.append(f"report language: expected {expect['report']['language']}, got {r.get('language')}")
    if "email" in expect:
        new = effects.emails[len(before.emails):]
        if not new or expect["email"]["to"] not in new[-1]["to"]:
            fails.append(f"no email sent to {expect['email']['to']}")
    if "export" in expect:
        new = effects.exports[len(before.exports):]
        ext = "." + expect["export"]["format"]
        if not any(e["key"].endswith(ext) for e in new):
            fails.append(f"no {ext} export uploaded")
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _snapshot(effects: SideEffects) -> SideEffects:
    return SideEffects(list(effects.reports), list(effects.emails), list(effects.exports), effects.charts)


def run_case(case: dict, *, live: bool, model_factory=None) -> CaseResult:
    missing = [k for k in (case.get("requires_env") or []) if live and not os.environ.get(k)]
    if missing:
        return CaseResult(case["id"], True, [], skipped=f"needs {', '.join(missing)}")

    from tests.test_graph import ScriptedModel  # scripted mode only, but cheap to import

    effects = SideEffects()
    with ExitStack() as stack, tempfile.TemporaryDirectory() as tmp:
        _fake_services(stack, effects, None if live and case.get("live_extraction") else case.get("mock_extraction"))
        store = LocalFileStore(tmp)
        model = model_factory() if live else ScriptedModel()
        graph = build_graph(model, checkpointer=InMemorySaver())
        files: dict[str, str] = {}
        cfg_base = {"configurable": {"thread_id": uuid.uuid4().hex, "file_store": store}}
        results: list[TurnResult] = []

        for i, turn in enumerate(case["turns"], 1):
            counter = CallCounter()
            cfg = {**cfg_base, "callbacks": [counter], "recursion_limit": 25}
            uploads = []
            for name in turn.get("files", []):
                ref = store.put(Path(name).name, (FIXTURES / name).read_bytes())
                files[Path(name).name] = ref["id"]
                uploads.append(ref)
            if not live:
                model.script.extend(scripted_messages(turn.get("script", []), files))

            before_msgs = len(graph.get_state(cfg_base).values.get("messages", []))
            before = _snapshot(effects)
            started = time.monotonic()
            label = f"turn {i}: " + (f"resume {turn['resume']!r}" if "resume" in turn else repr(turn.get("user", ""))[:60])
            try:
                if "resume" in turn:
                    graph.invoke(Command(resume=turn["resume"]), cfg)
                else:
                    graph.invoke({"messages": [HumanMessage(turn["user"])], "uploads": uploads}, cfg)
                error = None
            except Exception as e:  # a crash is a failed turn, not a crashed run
                error = f"{type(e).__name__}: {e}"
            seconds = time.monotonic() - started

            snapshot = graph.get_state(cfg_base, subgraphs=True)
            state = snapshot.values
            messages = state.get("messages", [])
            # A specialist paused on a question keeps its new messages in its own
            # (sub-graph) state until it finishes, so read them from there.
            for task in snapshot.tasks or ():
                sub = getattr(task, "state", None)
                if task.interrupts and sub is not None and hasattr(sub, "values"):
                    messages = sub.values.get("messages", messages)
            new = messages[before_msgs:]
            tools = [tc["name"] for m in new if isinstance(m, AIMessage) for tc in (m.tool_calls or [])
                     if tc["name"] != "Route"]
            replies = [m for m in new if isinstance(m, AIMessage) and isinstance(m.content, str) and m.content.strip()]
            reply = replies[-1].content if replies else ""
            interrupt = None
            for task in snapshot.tasks or ():
                for it in task.interrupts or ():
                    interrupt = (it.value or {}).get("type") if isinstance(it.value, dict) else "question"

            fails = [error] if error else check_turn(
                turn.get("expect", {}), state=state, tools=tools, reply=reply,
                llm_calls=counter.count, interrupt=interrupt, effects=effects, before=before)
            if not live and model.script and not error:
                fails.append(f"{len(model.script)} scripted model replies were not used")
                model.script.clear()
            results.append(TurnResult(label, fails, tools, state.get("route", ""), counter.count, seconds))

    return CaseResult(case["id"], all(not t.failures for t in results), results)


def report_json(results: list[CaseResult], mode: str) -> dict:
    return {
        "mode": mode,
        "passed": sum(r.passed and not r.skipped for r in results),
        "failed": sum(not r.passed for r in results),
        "skipped": sum(bool(r.skipped) for r in results),
        "cases": [{"id": r.id, "passed": r.passed, "skipped": r.skipped,
                   "turns": [t.__dict__ for t in r.turns]} for r in results],
    }


def load_cases(path: Optional[Path] = None) -> list[dict]:
    import yaml

    path = path or Path(__file__).parent / "cases.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)
