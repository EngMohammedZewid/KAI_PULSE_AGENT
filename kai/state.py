"""Graph state for KAI Pulse.

On Orchestrate, everything the agent "remembered" lived in the chat history:
the model copied whole CSVs and the analysis JSON into tool arguments. Here
that data lives in typed state fields, and tools read it through
``InjectedState``. The model only ever passes short file ids.

Each list field has a reducer so that tool updates merge instead of
overwriting (LangGraph applies them when a tool returns a ``Command``).
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from langgraph.graph import MessagesState
from typing_extensions import NotRequired, TypedDict

FileKind = Literal["csv", "excel", "pdf", "text", "image", "other"]
Language = Literal["English", "Arabic"]
ReportFormat = Literal["pdf", "docx"]


class FileRef(TypedDict):
    """A file the user uploaded. The bytes live in the FileStore, not in state."""

    id: str              # short id the model uses to refer to the file
    name: str            # original filename
    mime: str
    kind: FileKind
    size: int            # bytes
    key: str             # FileStore key (an S3 key, or a path under the local root)


class ChartRef(TypedDict):
    """A chart rendered during the conversation, reused when building reports."""

    title: str
    url: str
    source: str          # e.g. "financial:savings" or "file:f_ab12cd34"


class Analysis(TypedDict):
    """Result of the bank statement analysis plus the files it covered."""

    file_ids: list[str]
    metrics: dict[str, Any]    # the JSON returned by analyze_bank_statement


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------

def merge_files(left: Optional[list[FileRef]], right: Optional[list[FileRef]]) -> list[FileRef]:
    """Add new files; a file with an existing id replaces the old entry in place."""
    merged: dict[str, FileRef] = {f["id"]: f for f in (left or [])}
    for f in right or []:
        merged[f["id"]] = f
    return list(merged.values())


def add_unique(left: Optional[list[str]], right: Optional[list[str]]) -> list[str]:
    """Append ids in order, skipping ones already present."""
    out = list(left or [])
    for item in right or []:
        if item not in out:
            out.append(item)
    return out


def add_charts(left: Optional[list[ChartRef]], right: Optional[list[ChartRef]]) -> list[ChartRef]:
    """Append charts, skipping a URL that is already recorded."""
    out = list(left or [])
    seen = {c["url"] for c in out}
    for c in right or []:
        if c["url"] not in seen:
            out.append(c)
            seen.add(c["url"])
    return out


class KaiState(MessagesState):
    """Everything the KAI Pulse graph keeps per conversation thread."""

    language: NotRequired[Language]
    files: NotRequired[Annotated[list[FileRef], merge_files]]
    # Bank statements queued with "add" (Scenario B), analyzed together later.
    statements: NotRequired[Annotated[list[str], add_unique]]
    analysis: NotRequired[Optional[Analysis]]
    charts: NotRequired[Annotated[list[ChartRef], add_charts]]
    report_format: NotRequired[Optional[ReportFormat]]

    # Per-turn fields (overwritten every turn by the ingest node)
    uploads: NotRequired[list[FileRef]]      # input: files attached to this message
    turn_uploads: NotRequired[list[str]]     # ids of the files attached to this message
    route: NotRequired[str]                  # specialist chosen by the router


def find_file(state: dict, file_id: str) -> Optional[FileRef]:
    for f in state.get("files") or []:
        if f["id"] == file_id:
            return f
    return None
