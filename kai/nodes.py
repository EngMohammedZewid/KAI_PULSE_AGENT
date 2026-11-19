"""Fixed graph nodes: run on every turn, without the LLM.

detect_language  - Arabic script in the user's latest message -> Arabic.
ingest           - records this turn's uploads, and handles "add" (Scenario B):
                   statements are queued and acknowledged with no LLM call.
"""
from __future__ import annotations

import re
from typing import Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
# "add", "add this", "add statement"; Arabic أضف / اضف / إضافة
_ADD = re.compile(r"(?<![a-z])add(?![a-z])|أضف|اضف|إضافة|اضافة", re.IGNORECASE)
_ANALYZE = re.compile(r"analy|حلل|تحليل", re.IGNORECASE)
_STATEMENT_KINDS = ("csv", "pdf")


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return " ".join(part.get("text", "") for part in content
                    if isinstance(part, dict)) if isinstance(content, list) else ""


def latest_user_text(state: dict) -> str:
    for message in reversed(state.get("messages") or []):
        if isinstance(message, HumanMessage):
            return _text(message)
    return ""


def detect_language(state: dict) -> dict:
    text = latest_user_text(state)
    if not text.strip():
        return {}
    return {"language": "Arabic" if _ARABIC.search(text) else "English"}


def is_add_only(text: str) -> bool:
    """'add this statement' -> True; 'add and analyze' -> False."""
    return bool(_ADD.search(text)) and not _ANALYZE.search(text)


def _received_reply(language: str, count: int, name: str) -> str:
    if language == "Arabic":
        return (f"تم استلام الكشف رقم {count} ✓ — {name}.\n"
                "ارفع الملف التالي، أو اكتب **analyze all** عند الانتهاء.")
    return (f"Statement {count} received ✓ — {name}.\n"
            "Upload the next file, or say **analyze all** when you're done.")


def ingest(state: dict) -> dict:
    uploads = list(state.get("uploads") or [])
    update: dict = {"uploads": [], "turn_uploads": [f["id"] for f in uploads], "route": ""}
    if uploads:
        update["files"] = uploads

    statements = [f for f in uploads if f["kind"] in _STATEMENT_KINDS]
    if statements and is_add_only(latest_user_text(state)):
        queued = list(state.get("statements") or [])
        new_ids = [f["id"] for f in statements if f["id"] not in queued]
        language = state.get("language", "English")
        lines = [_received_reply(language, len(queued) + i + 1, f["name"])
                 for i, f in enumerate(statements) if f["id"] in new_ids]
        update["statements"] = new_ids
        update["messages"] = [AIMessage("\n\n".join(lines) or "Already queued.", name="kai")]
        update["route"] = "done"
    return update


def after_ingest(state: dict) -> str:
    return "done" if state.get("route") == "done" else "router"


def route_target(state: dict) -> Optional[str]:
    return state.get("route") or "general"
