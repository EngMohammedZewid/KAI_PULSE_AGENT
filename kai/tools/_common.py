"""Helpers shared by the state-aware tools."""
from __future__ import annotations

import re
from typing import Any, Optional

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from tools._chart_spec import capture, to_spec

_IMG_SRC = re.compile(r'<img src="([^"]+)"')
_MD_IMG = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def chart_url(rendered: str) -> Optional[str]:
    """Pull the image URL out of a chart tool's HTML or Markdown output."""
    m = _IMG_SRC.search(rendered) or _MD_IMG.search(rendered)
    return m.group(1) if m else None


def chart_ref(title: str, url: str, source: str, recorded: list, unit: str = "") -> dict:
    """A ChartRef for `url`, with the interactive spec when its config was recorded."""
    ref = {"title": title, "url": url, "source": source}
    for found_url, config in recorded:
        if found_url == url:
            spec = to_spec(config, unit=unit)
            if spec:
                if not spec["title"]:
                    spec["title"] = title[:120]
                ref["spec"] = spec
            break
    return ref


def reply(tool_call_id: str, content: str, artifact: Any = None, **update: Any) -> Command:
    """A Command that updates state and answers the tool call in one step.
    `artifact` rides on the ToolMessage for the UI and is never shown to the model."""
    message = ToolMessage(content, tool_call_id=tool_call_id, artifact=artifact)
    return Command(update={**update, "messages": [message]})


def record_charts(request, execute):
    """ToolNode wrapper: charts drawn by a plain chart tool are stored in state with
    their interactive spec, like financial_chart and chart_from_file already do."""
    with capture() as recorded:
        result = execute(request)
    if not recorded or not isinstance(result, ToolMessage):
        return result
    content = str(result.content)
    args = request.tool_call.get("args") or {}
    name = request.tool_call.get("name") or "chart"
    charts = []
    for url, config in recorded:
        if url in content:
            title = str(args.get("title") or name.replace("_", " ")).strip()
            charts.append(chart_ref(title, url, f"tool:{name}", [(url, config)]))
    if not charts:
        return result
    return Command(update={"charts": charts, "messages": [result]})


def unknown_file(file_id: str, state: dict) -> str:
    names = ", ".join(f"{f['id']} ({f['name']})" for f in state.get("files") or []) or "none"
    return f"Error: no uploaded file has id '{file_id}'. Files in this conversation: {names}."
