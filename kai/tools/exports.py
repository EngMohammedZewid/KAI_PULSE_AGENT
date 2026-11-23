"""Table export for the graph: the same Excel/CSV files as tools/export_table_tool.py,
saved for download from the chat instead of uploaded to S3 with a public link."""
from __future__ import annotations

import logging
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId

from kai.tools.outputs import file_name, output_reply, save_output
from tools._tooling import tool
from tools.export_table_tool import _normalise_data, _to_csv_bytes, _to_xlsx_bytes

log = logging.getLogger(__name__)


@tool
def export_table_to_file(
    data: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
    file_format: str = "xlsx",
    filename: str = "",
    sheet_name: str = "Data",
) -> object:
    """Export tabular data to an Excel (XLSX) or CSV file and return its download link.

    Args:
        data: The table: a JSON array of objects, a CSV string with a header row, or a Markdown pipe table.
        file_format: "xlsx" (default) or "csv".
        filename: Optional file name without extension. Defaults to "export".
        sheet_name: Sheet name for XLSX files (default "Data"). Ignored for CSV.
    """
    rows = _normalise_data(data)
    if not rows:
        return ("Could not read the table. Pass a JSON array of objects, a CSV string "
                "with a header row, or a Markdown pipe table.")
    fmt = (file_format or "").lower().strip().lstrip(".")
    fmt = fmt if fmt in ("csv", "xlsx") else "xlsx"
    try:
        body = _to_csv_bytes(rows) if fmt == "csv" else _to_xlsx_bytes(rows, sheet_name=sheet_name or "Data")
    except Exception:
        log.exception("Building the %s export failed", fmt)
        return f"Building the {fmt.upper()} file failed. Tell the user and offer to try again."
    ref, link = save_output(config, file_name(filename, fmt, fallback="export"), body)
    columns = len(rows[0]) if rows else 0
    return output_reply(tool_call_id, ref, link, f"The {fmt.upper()} file ({len(rows)} rows × {columns} columns)")
