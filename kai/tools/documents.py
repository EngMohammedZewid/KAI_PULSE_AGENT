"""Report tool: the fixed part of the report pipeline.

The model writes the content; this tool does the rest the same way every time:
asks "PDF or Word?" (a LangGraph interrupt) unless the user already said,
applies the conversation language, appends stored charts that the content
does not embed yet, and builds and uploads the file.
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from langchain_core.tools import InjectedToolCallId
from langgraph.prebuilt import InjectedState
from langgraph.types import interrupt

from kai.tools._common import reply
from tools._tooling import tool
from tools.pdf_tool import create_pdf_report, create_word_report

FORMAT_QUESTION = {
    "English": "Would you prefer **PDF** or **Word (.docx)**?",
    "Arabic": "هل تفضّل التقرير بصيغة **PDF** أم **Word (.docx)**؟",
}


def parse_format(answer: object) -> Optional[str]:
    text = str(answer or "").strip().lower()
    if any(k in text for k in ("docx", "word", "وورد", "ورد")):
        return "docx"
    if any(k in text for k in ("pdf", "بي دي اف", "بى دى اف")):
        return "pdf"
    return None


CHARTS_HEADING = {"English": "Charts", "Arabic": "الرسوم البيانية"}


def with_charts(content: str, charts: list[dict], language: str = "English") -> str:
    missing = [c for c in charts if c["url"] not in content]
    if not missing:
        return content
    embeds = "\n\n".join(f"![{c['title']}]({c['url']})" for c in missing)
    heading = CHARTS_HEADING.get(language, CHARTS_HEADING["English"])
    return f"{content.rstrip()}\n\n## {heading}\n\n{embeds}\n"


def _build(fmt: str, **kwargs) -> str:
    builder = create_word_report if fmt == "docx" else create_pdf_report
    return builder.func(**kwargs)


@tool
def create_report(
    title: str,
    content: str,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    format: Optional[Literal["pdf", "docx"]] = None,
    include_charts: bool = True,
) -> object:
    """Build a PDF or Word report from content you wrote, and return its download link.

    Args:
        title: Report title.
        content: The full report in Markdown-like text: ## headings, - bullets, ![title](url) images.
        format: "pdf" or "docx". Only pass it if the user named the format; otherwise the user is asked.
        include_charts: Append charts drawn earlier in the conversation that the content does not already embed.
    """
    language = state.get("language", "English")
    fmt = format
    if fmt is None:
        answer = interrupt({"type": "report_format",
                            "question": FORMAT_QUESTION.get(language, FORMAT_QUESTION["English"])})
        fmt = parse_format(answer)
        if fmt is None:
            return (f"The user answered '{answer}', which is not PDF or Word. "
                    "Ask them which format they want, then call create_report with format.")

    body = with_charts(content, state.get("charts") or [], language) if include_charts else content
    result = _build(fmt, content=body, title=title, topic=title, language=language)
    return reply(tool_call_id, result, report_format=fmt)
