"""Tools that work on uploaded files by id instead of pasted file contents."""
from __future__ import annotations

from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId
from langgraph.prebuilt import InjectedState

from kai.files import read_text, store_from_config
from kai.state import find_file
from kai.tools._common import chart_ref, chart_url, reply, unknown_file
from tools._chart_spec import capture
from tools._tooling import tool
from tools.chart_csv_tool import render_chart_from_csv
from tools.chart_excel_ts_tool import inspect_excel_content, render_chart_from_excel
from tools.file_browser_tool import inspect_csv_content

_PREVIEW_CHARS = 1500


@tool
def list_files(state: Annotated[dict, InjectedState]) -> str:
    """List the files uploaded in this conversation with their ids and types."""
    files = state.get("files") or []
    if not files:
        return "No files have been uploaded in this conversation."
    return "\n".join(f"- {f['id']}: {f['name']} ({f['kind']}, {f['size']:,} bytes)"
                     for f in files)


@tool
def describe_file(
    file_id: str,
    state: Annotated[dict, InjectedState],
    config: RunnableConfig,
) -> str:
    """Show an uploaded file's structure: columns and sample rows, or a text preview.

    Call this before charting a CSV or Excel file to learn the exact column names.

    Args:
        file_id: Id of the uploaded file.
    """
    ref = find_file(state, file_id)
    if ref is None:
        return unknown_file(file_id, state)
    store = store_from_config(config)
    if ref["kind"] == "csv":
        return inspect_csv_content.func(csv_content=read_text(store, ref))
    if ref["kind"] == "excel":
        return inspect_excel_content.func(file_path=store.locator(ref))
    if ref["kind"] in ("pdf", "text"):
        text = read_text(store, ref)
        more = f"\n\n[... {len(text) - _PREVIEW_CHARS:,} more characters]" if len(text) > _PREVIEW_CHARS else ""
        return f"{ref['name']} ({len(text):,} characters):\n\n{text[:_PREVIEW_CHARS]}{more}"
    return f"{ref['name']} is a {ref['kind']} file ({ref['size']:,} bytes); it has no text to show."


@tool
def chart_from_file(
    file_id: str,
    x_column: str,
    y_columns: str,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
    title: str = "Chart",
    chart_type: Literal["line", "bar"] = "line",
    group_by: Literal["month", "quarter", "year", "none"] = "month",
    aggregate: Literal["sum", "avg"] = "sum",
    area: bool = False,
    sheet_name: str = "",
) -> object:
    """Draw a chart from an uploaded CSV or Excel file and keep it for reports.

    Args:
        file_id: Id of the uploaded CSV or Excel file.
        x_column: Column for the X axis (dates or categories).
        y_columns: Comma-separated columns to plot.
        title: Chart title.
        chart_type: "line" or "bar". Excel files always draw a line chart.
        group_by: Group dates by "month", "quarter", "year", or "none".
        aggregate: Combine values in a group with "sum" or "avg".
        area: Fill the area under each line.
        sheet_name: Excel sheet to read. Empty means the first sheet.
    """
    ref = find_file(state, file_id)
    if ref is None:
        return unknown_file(file_id, state)
    store = store_from_config(config)
    flag = "true" if area else "false"
    if ref["kind"] not in ("csv", "excel"):
        return f"Error: {ref['name']} is not a CSV or Excel file."
    with capture() as recorded:
        if ref["kind"] == "csv":
            rendered = render_chart_from_csv.func(
                x_column=x_column, y_columns=y_columns, csv_content=read_text(store, ref),
                title=title, area=flag, aggregate=aggregate, group_by=group_by,
                chart_type=chart_type,
            )
        else:
            rendered = render_chart_from_excel.func(
                file_path=store.locator(ref), x_column=x_column, y_columns=y_columns,
                sheet_name=sheet_name, title=title, area=flag, aggregate=aggregate,
                group_by=group_by,
            )
    url = chart_url(rendered)
    if not url:
        return rendered  # the wrapped tool's error message
    return reply(tool_call_id, rendered,
                 charts=[chart_ref(title, url, f"file:{file_id}", recorded)])


DATA_FILE_TOOLS = [list_files, describe_file, chart_from_file]
