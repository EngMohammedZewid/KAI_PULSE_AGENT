"""Side-by-side replay: old Orchestrate answers vs the new LangGraph agent.

    python -m evals.compare --export evals/reports/orchestrate_threads.json --live
    python -m evals.compare --export ... --dev        # no LLM, checks the plumbing

Each exported thread's user messages are sent, in order, to a fresh thread of
the new graph. External services are faked as in the evals (nothing is
uploaded or emailed); a "PDF or Word?" question is answered "pdf". The
Markdown report pairs every user message with the old and the new answer and
flags turns where numbers in the old answer are missing from the new one, or
the new agent failed.

Limits: files uploaded in Orchestrate are not in the export and are not
replayed, so turns that depended on a file will differ. The report holds real
conversations: it is written under evals/reports/ (git-ignored).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from evals.harness import SideEffects, _fake_services
from kai.files import LocalFileStore
from kai.graph import build_graph

_NUMBER = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])\d+(?:\.\d+)?")


# ---------------------------------------------------------------------------
# Reading an Orchestrate export
# ---------------------------------------------------------------------------

def _texts(content: Any) -> list[str]:
    """All text inside an Orchestrate message's content, whatever its shape."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [t for part in content for t in _texts(part)]
    if isinstance(content, dict):
        found = [content["text"]] if isinstance(content.get("text"), str) else []
        for key in ("content", "parts", "generic"):
            if key in content:
                found += _texts(content[key])
        return found
    return []


def conversation(thread: dict) -> list[tuple[str, str]]:
    """[(role, text)] for user/assistant messages, oldest first."""
    msgs = sorted(thread.get("messages") or [],
                  key=lambda m: str(m.get("created_at") or m.get("created_on") or ""))
    out = []
    for m in msgs:
        role = str(m.get("role") or "").lower()
        if role in ("user", "assistant"):
            text = "\n".join(t for t in _texts(m.get("content")) if t.strip()).strip()
            if text:
                out.append((role, text))
    return out


def turns(conv: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Pair each user message with the old assistant answer(s) that followed it."""
    pairs: list[tuple[str, list[str]]] = []
    for role, text in conv:
        if role == "user":
            pairs.append((text, []))
        elif pairs:
            pairs[-1][1].append(text)
    return [(u, "\n\n".join(a)) for u, a in pairs]


# ---------------------------------------------------------------------------
# Comparing answers
# ---------------------------------------------------------------------------

def numbers(text: str) -> set[float]:
    """Numbers of 3+ significant digits (amounts, totals), with separators removed."""
    out = set()
    for raw in _NUMBER.findall(text or ""):
        value = float(raw.replace(",", ""))
        if abs(value) >= 100 and not 1900 <= value <= 2100:  # skip small counts and years
            out.add(round(value, 2))
    return out


def missing_numbers(old: str, new: str) -> list[float]:
    """Old-answer numbers absent from the new answer (also matching when rounded to whole units)."""
    new_nums = numbers(new)
    new_rounded = {round(n) for n in new_nums}
    return sorted(n for n in numbers(old) if n not in new_nums and round(n) not in new_rounded)


@dataclass
class TurnComparison:
    user: str
    old: str
    new: str
    missing: list[float] = field(default_factory=list)
    error: str = ""
    note: str = ""

    @property
    def flagged(self) -> bool:
        return bool(self.missing or self.error or not self.new.strip())


def replay(conv_turns: list[tuple[str, str]], model, effects: SideEffects) -> list[TurnComparison]:
    graph = build_graph(model, checkpointer=InMemorySaver())
    with tempfile.TemporaryDirectory() as tmp:
        cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "file_store": LocalFileStore(tmp)},
               "recursion_limit": 25}
        results = []
        for user, old in conv_turns:
            before = len(graph.get_state(cfg).values.get("messages", []))
            note, error = "", ""
            try:
                state = graph.invoke({"messages": [HumanMessage(user)]}, cfg)
                if state.get("__interrupt__"):
                    note = "answered the PDF/Word question with 'pdf'"
                    state = graph.invoke(Command(resume="pdf"), cfg)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            msgs = graph.get_state(cfg).values.get("messages", [])[before:]
            replies = [m.content for m in msgs if isinstance(m, AIMessage)
                       and isinstance(m.content, str) and m.content.strip()]
            new = replies[-1] if replies else ""
            results.append(TurnComparison(user, old, new, missing_numbers(old, new), error, note))
        return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _cell(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    text = text if len(text) <= limit else text[:limit] + " …"
    return text.replace("|", "\\|").replace("\n", "<br>") or "—"


def render(results: dict[str, list[TurnComparison]], mode: str) -> str:
    all_turns = [t for ts in results.values() for t in ts]
    flagged = [t for t in all_turns if t.flagged]
    lines = [
        f"# Orchestrate vs LangGraph replay ({mode})",
        "",
        f"{len(results)} threads, {len(all_turns)} turns, {len(flagged)} flagged "
        f"({sum(bool(t.missing) for t in all_turns)} with missing numbers, "
        f"{sum(bool(t.error) for t in all_turns)} errors, "
        f"{sum(not t.new.strip() and not t.error for t in all_turns)} empty answers).",
        "",
        "Files uploaded in Orchestrate are not replayed; turns that relied on one will differ.",
    ]
    for tid, ts in results.items():
        lines += ["", f"## Thread {tid[:8]}", "", "| # | User | Orchestrate | LangGraph | Check |",
                  "| --- | --- | --- | --- | --- |"]
        for i, t in enumerate(ts, 1):
            check = []
            if t.error:
                check.append(f"error: {t.error[:120]}")
            if t.missing:
                check.append("missing numbers: " + ", ".join(f"{n:,.2f}".rstrip("0").rstrip(".") for n in t.missing))
            if not t.new.strip() and not t.error:
                check.append("empty answer")
            if t.note:
                check.append(t.note)
            lines.append(f"| {i} | {_cell(t.user, 300)} | {_cell(t.old)} | {_cell(t.new)} | "
                         f"{_cell('; '.join(check) or 'ok', 300)} |")
    return "\n".join(lines) + "\n"


def run(export: list[dict], model_factory, mode: str, limit: Optional[int] = None) -> tuple[str, dict]:
    effects = SideEffects()
    results: dict[str, list[TurnComparison]] = {}
    with ExitStack() as stack:
        _fake_services(stack, effects, None)
        for thread in export[:limit] if limit else export:
            conv_turns = turns(conversation(thread))
            if conv_turns:
                results[str(thread.get("thread_id") or uuid.uuid4().hex)] = replay(conv_turns, model_factory(), effects)
    all_turns = [t for ts in results.values() for t in ts]
    summary = {"threads": len(results), "turns": len(all_turns),
               "flagged": sum(t.flagged for t in all_turns),
               "missing_numbers": sum(bool(t.missing) for t in all_turns),
               "errors": sum(bool(t.error) for t in all_turns)}
    return render(results, mode), summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evals.compare")
    p.add_argument("--export", type=Path, required=True, help="JSON from evals.orchestrate_export")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="use the configured chat model")
    mode.add_argument("--dev", action="store_true", help="no LLM (echo model), plumbing check")
    p.add_argument("--limit", type=int, help="replay only the first N threads")
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)

    from kai.models import DevEchoModel, get_chat_model

    factory = get_chat_model if args.live else DevEchoModel
    export = json.loads(args.export.read_text(encoding="utf-8"))
    report, summary = run(export, factory, "live" if args.live else "dev", args.limit)
    out = args.out or Path("evals/reports") / f"compare-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(json.dumps(summary), f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
