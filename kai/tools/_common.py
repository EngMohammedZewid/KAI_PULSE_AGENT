"""Helpers shared by the state-aware tools."""
from __future__ import annotations

import re
from typing import Any, Optional

from langchain_core.messages import ToolMessage
from langgraph.types import Command

_IMG_SRC = re.compile(r'<img src="([^"]+)"')
_MD_IMG = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def chart_url(rendered: str) -> Optional[str]:
    """Pull the image URL out of a chart tool's HTML or Markdown output."""
    m = _IMG_SRC.search(rendered) or _MD_IMG.search(rendered)
    return m.group(1) if m else None


def reply(tool_call_id: str, content: str, **update: Any) -> Command:
    """A Command that updates state and answers the tool call in one step."""
    return Command(update={**update,
                           "messages": [ToolMessage(content, tool_call_id=tool_call_id)]})


def unknown_file(file_id: str, state: dict) -> str:
    names = ", ".join(f"{f['id']} ({f['name']})" for f in state.get("files") or []) or "none"
    return f"Error: no uploaded file has id '{file_id}'. Files in this conversation: {names}."
