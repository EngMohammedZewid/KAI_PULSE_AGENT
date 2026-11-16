from __future__ import annotations
import io, json, urllib.parse, urllib.request, datetime
from collections import defaultdict
import openpyxl
from tools._tooling import tool

_COLORS = ["#2E6DB4","#C8A84B","#E05C2E","#3DAA6D","#9B59B6","#1ABC9C","#E74C3C","#F39C12"]
_QUICKCHART = "https://quickchart.io/chart"


def _chart_url(config: dict, width: int = 700, height: int = 420) -> str:
    import copy, urllib.request
    cfg = copy.deepcopy(config)
    opts = cfg.setdefault("options", {})
    pl = opts.setdefault("plugins", {})
    pl.setdefault("title", {}).setdefault("font", {}).setdefault("size", 11)
    pl.setdefault("legend", {}).setdefault("labels", {}).setdefault("font", {}).setdefault("size", 10)
    for sc in opts.get("scales", {}).values():
        sc.setdefault("ticks", {}).setdefault("font", {}).setdefault("size", 9)
        if "title" in sc:
            sc["title"].setdefault("font", {}).setdefault("size", 9)
    body = json.dumps({"chart": cfg, "width": width, "height": height, "backgroundColor": "white"}).encode("utf-8")
    req = urllib.request.Request(
        "https://quickchart.io/chart/create",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())["url"]
    except Exception:
        params = urllib.parse.urlencode({
            "c": json.dumps(cfg, separators=(",", ":")),
            "w": width, "h": height, "bkg": "white",
        })
        return f"{_QUICKCHART}?{params}"


def _bool(v):
    if isinstance(v, bool): return v
    if isinstance(v, str): return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _cell_value(cell):
    """Return a clean value from an openpyxl cell."""
    v = cell.value
    if v is None:
        return None
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v
    return v


def _period_key(dt, period: str) -> str:
    """Convert a date to a grouping key string."""
    if isinstance(dt, datetime.datetime):
        dt = dt.date()
    if period == "year":
        return str(dt.year)
    if period == "quarter":
        q = (dt.month - 1) // 3 + 1
        return f"{dt.year}-Q{q}"
    # default: month
    return dt.strftime("%Y-%m")


def _load_workbook(file_path: str) -> openpyxl.Workbook:
    """Open an Excel workbook from a local path OR an http(s) URL."""
    if file_path.startswith("http://") or file_path.startswith("https://"):
        req = urllib.request.Request(file_path, headers={"User-Agent": "watsonx-orchestrate-agent/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        return openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    return openpyxl.load_workbook(file_path, read_only=True, data_only=True)


@tool
def inspect_excel_content(
    file_path: str,
    sheet_name: str = "",
    max_sample_rows: int = 5,
) -> str:
    """
    Inspect an Excel (.xlsx) file: return sheet names, column headers,
    row count, and a sample of data rows.

    Accepts a local file path OR an https:// URL (e.g. an S3 presigned link).
    Use this BEFORE render_chart_from_excel to discover the exact column names.

    Args:
        file_path:       Full local path to the .xlsx file, OR an https:// URL
                         (presigned S3 download link from upload_chat_file_to_s3).
        sheet_name:      Sheet to inspect. Leave empty to use the first sheet.
        max_sample_rows: Number of sample data rows to show (default 5, max 20).

    Returns:
        Sheet names, column headers, row count, and sample rows.
    """
    try:
        wb = _load_workbook(file_path)
    except FileNotFoundError:
        return f"File not found: {file_path}"
    except Exception as e:
        return f"Cannot open Excel file: {e}"

    sheet_names = wb.sheetnames
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
    rows = list(ws.iter_rows())
    wb.close()

    if not rows:
        return "The sheet is empty."

    headers = [str(_cell_value(c) or "").strip() for c in rows[0]]
    data_rows = rows[1:]
    total = len(data_rows)
    sample_n = max(1, min(max_sample_rows, 20))
    sample = data_rows[:sample_n]

    lines = [
        f"Sheets: {', '.join(sheet_names)}",
        f"Active sheet: {ws.title}",
        f"Total data rows: {total}",
        "",
        f"Columns ({len(headers)}):",
    ]
    for i, h in enumerate(headers, 1):
        lines.append(f"  {i:2}. {h}")
    lines.append("")
    lines.append(f"Sample ({sample_n} rows):")
    lines.append("  " + " | ".join(headers))
    for row in sample:
        vals = [str(_cell_value(c) or "") for c in row]
        lines.append("  " + " | ".join(vals))
    lines.append("")
    lines.append(
        "Tip: Use these exact column names in render_chart_from_excel, "
        "passing this file_path and the same sheet_name."
    )
    return "\n".join(lines)


@tool
def render_chart_from_excel(
    file_path: str,
    x_column: str,
    y_columns: str,
    sheet_name: str = "",
    title: str = "Chart from Excel",
    area: str = "false",
    aggregate: str = "sum",
    group_by: str = "month",
) -> str:
    """
    Read an Excel (.xlsx) file and draw a time-series line chart from its data.

    The first row must be a header row. Date columns are automatically grouped
    by the chosen period (month/quarter/year) and y-values are aggregated.

    Args:
        file_path:   Full path to the Excel file. Example: C:/data/sales.xlsx
        x_column:    Header of the column to use as X-axis (should contain dates).
                     Example: "Order Date"
        y_columns:   Comma-separated column headers to plot as data series.
                     Example: "Revenue,Cost"
        sheet_name:  Name of the sheet to read. Leave empty to use the first sheet.
        title:       Chart title.
        area:        Set to "true" to fill the area under each line. Default "false".
        aggregate:   How to combine values in each group: "sum" or "avg". Default "sum".
        group_by:    Time period to group date values: "month", "quarter", or "year".
                     Use "none" to plot every row as-is (no aggregation). Default "month".

    Returns:
        Markdown image string that renders as an inline chart in the chat.

    Example:
        render_chart_from_excel(
            file_path="https://bucket.s3.amazonaws.com/uploads/sales.xlsx?...",
            x_column="Order Date",
            y_columns="Revenue,Cost",
            title="Monthly Sales",
            group_by="month",
            aggregate="sum"
        )
    """
    is_area = _bool(area)

    # ── Open workbook (supports local path or https:// URL) ───────────────────
    try:
        wb = _load_workbook(file_path)
    except FileNotFoundError:
        raise ValueError(f"File not found: {file_path}")
    except Exception as e:
        raise ValueError(f"Cannot open Excel file: {e}")

    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active

    rows = list(ws.iter_rows())
    wb.close()

    if not rows:
        raise ValueError("The sheet is empty.")

    headers = [str(_cell_value(c) or "").strip() for c in rows[0]]

    if x_column not in headers:
        raise ValueError(f"Column '{x_column}' not found. Available: {headers}")

    y_col_list = [c.strip() for c in y_columns.split(",") if c.strip()]
    missing = [c for c in y_col_list if c not in headers]
    if missing:
        raise ValueError(f"Column(s) not found: {missing}. Available: {headers}")

    x_idx  = headers.index(x_column)
    y_idxs = [headers.index(c) for c in y_col_list]

    # ── Aggregate data ─────────────────────────────────────────────────────────
    do_aggregate = group_by.strip().lower() != "none"

    if do_aggregate:
        # ordered dict: period_key → {col: [values]}
        buckets: dict[str, dict[str, list]] = {}
        for row in rows[1:]:
            x_val = _cell_value(row[x_idx])
            if x_val is None:
                continue
            # Accept date/datetime or string date
            if isinstance(x_val, (datetime.date, datetime.datetime)):
                key = _period_key(x_val, group_by.strip().lower())
            else:
                key = str(x_val)
            if key not in buckets:
                buckets[key] = {c: [] for c in y_col_list}
            for col_name, col_idx in zip(y_col_list, y_idxs):
                raw = _cell_value(row[col_idx])
                try:
                    buckets[key][col_name].append(float(raw))
                except (TypeError, ValueError):
                    pass

        x_labels = sorted(buckets.keys())
        use_sum = aggregate.strip().lower() != "avg"
        y_data: dict[str, list] = {c: [] for c in y_col_list}
        for key in x_labels:
            for col_name in y_col_list:
                vals = buckets[key][col_name]
                if not vals:
                    y_data[col_name].append(None)
                elif use_sum:
                    y_data[col_name].append(round(sum(vals), 2))
                else:
                    y_data[col_name].append(round(sum(vals) / len(vals), 2))
    else:
        x_labels = []
        y_data = {c: [] for c in y_col_list}
        for row in rows[1:]:
            x_val = _cell_value(row[x_idx])
            if x_val is None:
                continue
            if isinstance(x_val, (datetime.date, datetime.datetime)):
                x_labels.append(x_val.strftime("%Y-%m-%d"))
            else:
                x_labels.append(str(x_val))
            for col_name, col_idx in zip(y_col_list, y_idxs):
                raw = _cell_value(row[col_idx])
                try:
                    y_data[col_name].append(float(raw) if raw is not None else None)
                except (TypeError, ValueError):
                    y_data[col_name].append(None)

    # ── Build QuickChart config ────────────────────────────────────────────────
    datasets = []
    for i, col_name in enumerate(y_col_list):
        color = _COLORS[i % len(_COLORS)]
        datasets.append({
            "label": col_name,
            "data": y_data[col_name],
            "borderColor": color,
            "backgroundColor": color + "33" if is_area else "transparent",
            "fill": is_area,
            "tension": 0.3,
            "pointRadius": 3,
            "pointBackgroundColor": color,
            "spanGaps": True,
        })

    config = {
        "type": "line",
        "data": {"labels": x_labels, "datasets": datasets},
        "options": {
            "plugins": {
                "title": {"display": True, "text": title},
                "legend": {"position": "bottom"},
            },
            "scales": {
                "x": {"title": {"display": True, "text": x_column}},
                "y": {"title": {"display": True, "text": aggregate.upper()}},
            },
        },
    }

    url = _chart_url(config)
    return f'<img src="{url}" alt="{title}" style="max-width:60%;display:block" />\n\n<a href="{url}">&#11015;&#xFE0F; Download chart</a>\n\n'
