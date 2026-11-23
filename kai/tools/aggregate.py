"""aggregate_file: group an uploaded CSV/Excel file and return the totals as a table.

The model can't add up a whole file itself (it only sees sample rows), so any
"monthly totals" it shows or exports must come from here. The result is a
Markdown table that export_table_to_file turns into Excel with real numbers.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Annotated, Any, Iterable, Literal, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import InjectedState

from kai.files import read_text, store_from_config
from kai.state import find_file
from kai.tools._common import unknown_file
from tools._tooling import tool

MAX_GROUPS = 500
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _to_date(value: Any) -> Optional[dt.date]:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip().translate(_ARABIC_DIGITS)
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _to_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().translate(_ARABIC_DIGITS).replace(",", "").replace("٬", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _bucket(d: dt.date, period: str) -> str:
    if period == "year":
        return str(d.year)
    if period == "quarter":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    if period == "week":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if period == "day":
        return d.isoformat()
    return f"{d.year}-{d.month:02d}"


def aggregate_rows(rows: Iterable[dict], group_column: str, value_columns: list[str],
                   period: str = "month", how: str = "sum",
                   start: Optional[dt.date] = None, end: Optional[dt.date] = None) -> tuple[list[tuple], int]:
    """Group rows -> [(key, [value per column]), ...] sorted by key, plus rows skipped.

    period "none" groups by the column's raw value (e.g. a channel name);
    otherwise the column is parsed as a date and bucketed.
    """
    groups: dict[str, list[list[float]]] = {}
    skipped = 0
    for row in rows:
        raw = row.get(group_column)
        if period == "none":
            key = str(raw).strip() if raw not in (None, "") else None
        else:
            d = _to_date(raw)
            if d is None or (start and d < start) or (end and d > end):
                key = None
            else:
                key = _bucket(d, period)
        if key is None:
            skipped += 1
            continue
        cells = groups.setdefault(key, [[] for _ in value_columns])
        for i, col in enumerate(value_columns):
            n = _to_number(row.get(col))
            if n is not None:
                cells[i].append(n)

    def agg(values: list[float]) -> Optional[float]:
        if how == "count":
            return float(len(values))
        if not values:
            return None
        return {"sum": sum, "avg": lambda v: sum(v) / len(v), "min": min, "max": max}[how](values)

    return [(k, [agg(v) for v in groups[k]]) for k in sorted(groups)], skipped


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return ""
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"


def _rows_from(ref: dict, store, sheet_name: str) -> tuple[list[dict], list[str]]:
    if ref["kind"] == "csv":
        reader = csv.DictReader(io.StringIO(read_text(store, ref).lstrip("﻿")))
        rows = [{(k or "").strip(): v for k, v in r.items()} for r in reader]
        return rows, [(c or "").strip() for c in (reader.fieldnames or [])]
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(store.read_bytes(ref)), read_only=True, data_only=True)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(it, [])]
    rows = [dict(zip(header, r)) for r in it]
    wb.close()
    return rows, header


@tool
def aggregate_file(
    file_id: str,
    group_column: str,
    value_columns: str,
    state: Annotated[dict, InjectedState],
    config: RunnableConfig,
    period: Literal["day", "week", "month", "quarter", "year", "none"] = "month",
    aggregate: Literal["sum", "avg", "min", "max", "count"] = "sum",
    start_date: str = "",
    end_date: str = "",
    sheet_name: str = "",
) -> str:
    """Group an uploaded CSV or Excel file and return the totals as a table (computed over every row).

    Use this for any monthly/weekly/yearly totals or averages you show or export: pass its table
    to export_table_to_file for Excel/CSV. Call describe_file first to get exact column names.

    Args:
        file_id: Id of the uploaded CSV or Excel file.
        group_column: Date column to bucket by period, or with period "none" any column to group by (e.g. a channel).
        value_columns: Comma-separated numeric columns to aggregate.
        period: "day", "week", "month", "quarter", "year", or "none" to group by the column's values.
        aggregate: "sum", "avg", "min", "max" or "count".
        start_date: Optional first date to include, YYYY-MM-DD.
        end_date: Optional last date to include, YYYY-MM-DD.
        sheet_name: Excel sheet to read. Empty means the first sheet.
    """
    ref = find_file(state, file_id)
    if ref is None:
        return unknown_file(file_id, state)
    if ref["kind"] not in ("csv", "excel"):
        return f"Error: {ref['name']} is not a CSV or Excel file."
    start = _to_date(start_date) if start_date else None
    end = _to_date(end_date) if end_date else None
    if (start_date and not start) or (end_date and not end):
        return "Error: start_date and end_date must look like YYYY-MM-DD."

    rows, header = _rows_from(ref, store_from_config(config), sheet_name)
    cols = [c.strip() for c in value_columns.split(",") if c.strip()]
    missing = [c for c in [group_column, *cols] if c not in header]
    if missing or not cols:
        return (f"Error: unknown column(s) {', '.join(missing) or '(none given)'}. "
                f"Columns in {ref['name']}: {', '.join(header)}.")

    result, skipped = aggregate_rows(rows, group_column, cols, period, aggregate, start, end)
    if not result:
        return f"No rows matched in {ref['name']} (skipped {skipped} rows without a usable {group_column})."
    if len(result) > MAX_GROUPS:
        return f"Error: {len(result)} groups is too many; use a longer period or a date range."

    label = group_column if period == "none" else period.capitalize()
    lines = [f"| {label} | " + " | ".join(f"{c} ({aggregate})" for c in cols) + " |",
             "| --- | " + " | ".join("---" for _ in cols) + " |"]
    lines += [f"| {k} | " + " | ".join(_fmt(v) for v in vals) + " |" for k, vals in result]
    if aggregate in ("sum", "count"):
        totals = [sum(v[i] or 0 for _, v in result) for i in range(len(cols))]
        lines.append("| Total | " + " | ".join(_fmt(t) for t in totals) + " |")
    note = f" {skipped} rows outside the range or without a usable value were skipped." if skipped else ""
    return (f"{aggregate.capitalize()} of {', '.join(cols)} by {label.lower()} from {ref['name']} "
            f"({len(rows):,} rows read).{note}\n\n" + "\n".join(lines) +
            "\n\nTo export this table, pass it unchanged as `data` to export_table_to_file.")
