"""
fetch_session_history_payload.py
--------------------------------
Refreshes payload data from watsonx Orchestrate and exports full session
history from stored trace payloads up to the latest user/assistant exchange.

Usage:
  python tools/fetch_session_history_payload.py --agent-name "Incident manager"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import logging

# Route all log output to stdout so PowerShell doesn't treat it as an error.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)

from dotenv import load_dotenv
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(__file__))
from payload_extractor_tool import _get_engine  # noqa: E402
from pipeline_extractor import run_extraction  # noqa: E402


def _to_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, bytes):
        return json.loads(value.decode("utf-8"))
    raise ValueError("Unsupported raw_payload type")


def _extract_turn(raw_payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """
    Walk every span and find the WatsonxChatModel span that holds the full
    conversation history.  Return the LAST user message and the assistant
    completion so callers always see the most-recent exchange.
    """
    best_user: Optional[str] = None
    best_assistant: Optional[str] = None

    trace_data = raw_payload.get("traceData", {})
    for resource_span in trace_data.get("resourceSpans", []):
        for scope_span in resource_span.get("scopeSpans", []):
            for span in scope_span.get("spans", []):
                attrs = {
                    attr.get("key"): attr.get("value", {}).get("stringValue")
                    for attr in span.get("attributes", [])
                }

                # Collect all prompt roles/contents
                prompt_roles: Dict[int, str] = {}
                prompt_contents: Dict[int, str] = {}
                for key, value in attrs.items():
                    m = re.match(r"gen_ai\.prompt\.(\d+)\.(role|content)$", key)
                    if m:
                        idx = int(m.group(1))
                        field = m.group(2)
                        if field == "role":
                            prompt_roles[idx] = value or ""
                        else:
                            prompt_contents[idx] = value or ""

                # Find the highest-index user message in this span
                user_indices = sorted(
                    [i for i, r in prompt_roles.items() if r == "user"],
                    reverse=True,
                )
                if user_indices:
                    candidate = prompt_contents.get(user_indices[0])
                    # Prefer the span with the most messages (richest context)
                    if candidate and (
                        best_user is None or len(prompt_roles) > len(prompt_roles)
                    ):
                        best_user = candidate

                # Assistant completion
                completion = attrs.get("gen_ai.completion.0.content")
                if completion and best_assistant is None:
                    best_assistant = completion

    return best_user, best_assistant


def _read_history(agent_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
    engine = _get_engine()

    base_sql = """
        SELECT captured_at, trace_id, agent_id, raw_payload
        FROM agent_payloads
        WHERE direction = 'trace'
    """
    params: Dict[str, Any] = {"limit": limit}

    if agent_id:
        base_sql += " AND agent_id = :agent_id"
        params["agent_id"] = agent_id

    base_sql += " ORDER BY captured_at DESC LIMIT :limit"

    history: List[Dict[str, Any]] = []
    seen_trace_ids = set()

    with engine.connect() as conn:
        rows = conn.execute(text(base_sql), params).mappings().all()

    for row in rows:
        trace_id = row.get("trace_id")
        if not trace_id or trace_id in seen_trace_ids:
            continue
        seen_trace_ids.add(trace_id)

        raw_payload = _to_json(row.get("raw_payload"))
        user, assistant = _extract_turn(raw_payload)

        history.append(
            {
                "captured_at": row.get("captured_at"),
                "trace_id": trace_id,
                "agent_id": row.get("agent_id"),
                "user_input": user,
                "assistant_response": assistant,
                "raw_payload": raw_payload,
            }
        )

    history.sort(key=lambda item: str(item.get("captured_at") or ""))
    return history


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Export full session history payload up to the latest user/assistant turn."
    )
    parser.add_argument("--agent-id", default=os.getenv("WXO_AGENT_ID"))
    parser.add_argument("--agent-name", default=os.getenv("WXO_AGENT_NAME"))
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Max trace rows to read from DB after refresh (before dedup by trace_id).",
    )
    parser.add_argument(
        "--out",
        default="session_history_payload.json",
        help="Output JSON file path.",
    )
    args = parser.parse_args()

    run_extraction(args.agent_id, args.agent_name)
    history = _read_history(args.agent_id, args.limit)

    last_turn = history[-1] if history else None

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": args.agent_id,
        "agent_name": args.agent_name,
        "total_turns": len(history),
        "last_turn": last_turn,
        "history": history,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Export complete: {args.out}")
    print(f"Total turns: {len(history)}")
    if last_turn:
        print(f"Last trace_id: {last_turn.get('trace_id')}")
        print(f"Last user input: {last_turn.get('user_input')}")
        print(f"Last assistant response: {last_turn.get('assistant_response')}")


if __name__ == "__main__":
    main()
