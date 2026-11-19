from __future__ import annotations

import csv
import io
import json

from tools._tooling import tool

# ---------------------------------------------------------------------------
# NOTE: This tool runs on IBM Cloud servers, not on the user local machine.
# It cannot access local Windows paths (e.g. d:\...).
# All data must be passed as text directly in the parameters.
# ---------------------------------------------------------------------------


@tool
def inspect_csv_content(
    csv_content: str,
) -> str:
    """
    Inspect pasted CSV text: returns column names, row count, and a 3-row sample.
    Use this before calling render_chart_from_csv so the agent knows the exact column names.

    How to use:
      1. Open your CSV file in Notepad or Excel -> Save As -> CSV.
      2. Copy the entire file contents.
      3. Paste the full CSV text into the csv_content parameter.

    Args:
        csv_content: The full text of the CSV file (header row + data rows).
                     Example: "Order Date,Revenue,Cost\\n2025-01-01,5000,3000"

    Returns:
        Column names, total row count, and a 3-row data sample.
    """
    text = (csv_content or "").strip()
    if not text:
        return "csv_content is empty. Please paste the CSV text into this parameter."

    text = text.replace("\\n", "\n").replace("\\t", "\t")

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)

    if not rows:
        return "No rows found in the CSV content."

    header = rows[0]
    sample = rows[1:4]
    total_data_rows = max(0, len(rows) - 1)

    lines = [
        f"Total data rows: {total_data_rows}",
        "",
        f"Columns ({len(header)}):",
    ]
    for i, col in enumerate(header, 1):
        lines.append(f"  {i:2}. {col}")

    lines.append("")
    lines.append("Sample (first 3 data rows):")
    for row in sample:
        lines.append("  " + " | ".join(str(v) for v in row))

    lines.append("")
    lines.append(
        "Tip: Copy these exact column names into render_chart_from_csv "
        "with csv_content set to the same pasted text."
    )

    return "\n".join(lines)


@tool
def read_csv_summary(
    csv_content: str,
    columns: str = "",
    max_rows: int = 20,
) -> str:
    """
    Read and display rows from pasted CSV text.
    Useful for previewing data before charting.

    Args:
        csv_content: The full CSV text (header + data rows).
        columns:     Optional comma-separated list of column names to show.
                     Leave blank to show all columns.
        max_rows:    Maximum number of data rows to display (default 20, max 200).

    Returns:
        A formatted table of the CSV data.
    """
    text = (csv_content or "").strip()
    if not text:
        return "csv_content is empty."

    text = text.replace("\\n", "\n").replace("\\t", "\t")
    max_rows = max(1, min(max_rows, 200))

    reader = csv.DictReader(io.StringIO(text))
    all_headers = list(reader.fieldnames or [])

    if columns.strip():
        selected = [c.strip() for c in columns.split(",") if c.strip()]
        missing = [c for c in selected if c not in all_headers]
        if missing:
            return f"Column(s) not found: {missing}. Available: {all_headers}"
    else:
        selected = all_headers

    rows = list(reader)
    preview = rows[:max_rows]

    col_widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in preview)) for c in selected}
    sep = "  ".join("-" * col_widths[c] for c in selected)
    header_row = "  ".join(c.ljust(col_widths[c]) for c in selected)

    lines = [
        f"Showing {len(preview)} of {len(rows)} rows | Columns: {', '.join(selected)}",
        "",
        header_row,
        sep,
    ]
    for row in preview:
        lines.append("  ".join(str(row.get(c, "")).ljust(col_widths[c]) for c in selected))

    if len(rows) > max_rows:
        lines.append(f"... [{len(rows) - max_rows} more rows not shown]")

    return "\n".join(lines)


@tool
def summarize_csv_statistics(
    csv_content: str,
    numeric_columns: str = "",
) -> str:
    """
    Compute basic statistics (min, max, sum, average, count) for numeric columns
    in pasted CSV text.

    Args:
        csv_content:     The full CSV text (header + data rows).
        numeric_columns: Comma-separated column names to analyse.
                         Leave blank to auto-detect all numeric columns.

    Returns:
        A statistics summary for each numeric column.
    """
    text = (csv_content or "").strip()
    if not text:
        return "csv_content is empty."

    text = text.replace("\\n", "\n").replace("\\t", "\t")

    reader = csv.DictReader(io.StringIO(text))
    all_headers = list(reader.fieldnames or [])
    rows = list(reader)

    if not rows:
        return "No data rows found."

    if numeric_columns.strip():
        target_cols = [c.strip() for c in numeric_columns.split(",") if c.strip()]
        missing = [c for c in target_cols if c not in all_headers]
        if missing:
            return f"Column(s) not found: {missing}. Available: {all_headers}"
    else:
        target_cols = []
        for col in all_headers:
            try:
                float(str(rows[0].get(col, "")).replace(",", "").strip())
                target_cols.append(col)
            except (ValueError, TypeError):
                pass

    if not target_cols:
        return "No numeric columns found. Please specify numeric_columns explicitly."

    lines = [f"Statistics for {len(rows)} rows:", ""]
    for col in target_cols:
        values = []
        for row in rows:
            raw = str(row.get(col, "")).replace(",", "").strip()
            try:
                values.append(float(raw))
            except (ValueError, TypeError):
                pass
        if not values:
            lines.append(f"{col}: no numeric values found")
            continue
        total = sum(values)
        lines.append(f"{col}:")
        lines.append(f"    Count : {len(values)}")
        lines.append(f"    Sum   : {total:,.2f}")
        lines.append(f"    Avg   : {total / len(values):,.2f}")
        lines.append(f"    Min   : {min(values):,.2f}")
        lines.append(f"    Max   : {max(values):,.2f}")
        lines.append("")

    return "\n".join(lines).strip()
