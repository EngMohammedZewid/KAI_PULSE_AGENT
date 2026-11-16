from __future__ import annotations
import csv, io, json, urllib.parse, datetime
from ibm_watsonx_orchestrate.agent_builder.tools import tool

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


def _to_float(s) -> float | None:
    """Parse a number that may be quoted with commas, e.g. '9,477.29'."""
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _to_bool(v) -> bool:
    if isinstance(v, bool): return v
    if isinstance(v, str): return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _parse_date(s: str) -> datetime.date | None:
    """Try common date formats."""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def _period_key(dt: datetime.date, period: str) -> str:
    if period == "year":
        return str(dt.year)
    if period == "quarter":
        q = (dt.month - 1) // 3 + 1
        return f"{dt.year}-Q{q}"
    return dt.strftime("%Y-%m")


@tool
def render_chart_from_csv(
    x_column: str,
    y_columns: str,
    csv_content: str = "",
    file_path: str = "",
    title: str = "Chart",
    area: str = "false",
    aggregate: str = "sum",
    group_by: str = "month",
    chart_type: str = "line",
) -> str:
    """
    Draw a chart from CSV data. Accepts either pasted CSV text OR a file path.
    Provide one of csv_content or file_path — csv_content takes priority.

    The first row must be a header row with column names.
    Date columns are automatically grouped by period and values are aggregated.

    Args:
        x_column:    Header of the column to use as X-axis (dates or categories).
                     Example: "Order Date"
        y_columns:   Comma-separated column headers to plot as data series.
                     Example: "Revenue,Cost"
        csv_content: Paste the full CSV text here (header row + data rows).
                     Use this when you cannot provide a file path.
                     Example: "Month,Revenue,Cost\nJan,5000,3000\nFeb,6000,3500"
        file_path:   Full path to a CSV file accessible from the server.
                     Leave blank if using csv_content.
        title:       Chart title.
        area:        Set to "true" to fill under lines. Default "false".
        aggregate:   How to combine values per group: "sum" or "avg". Default "sum".
        group_by:    Time grouping: "month", "quarter", "year", or "none".
                     Default "month".
        chart_type:  "line" or "bar". Default "line".

    Returns:
        Markdown image string that renders as an inline chart in the chat.
    """
    is_area = _to_bool(area)
    do_group = group_by.strip().lower() != "none"
    period   = group_by.strip().lower()
    use_sum  = aggregate.strip().lower() != "avg"

    # ── Load CSV from content string or file ───────────────────────────────────
    csv_text = (csv_content or "").strip()
    if csv_text:
        # Normalise escaped newlines that LLMs sometimes produce
        csv_text = csv_text.replace("\\n", "\n").replace("\\t", "\t")
        reader = csv.DictReader(io.StringIO(csv_text))
        headers = reader.fieldnames or []
        rows = list(reader)
    elif file_path.strip():
        try:
            with open(file_path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                headers = reader.fieldnames or []
                rows = list(reader)
        except FileNotFoundError:
            return (
                f"File not found: '{file_path}'.\n"
                "Note: the tool runs on IBM Cloud and cannot access local Windows paths. "
                "Please paste the CSV content directly using the csv_content parameter instead."
            )
        except Exception as e:
            raise ValueError(f"Cannot read CSV file: {e}")
    else:
        return (
            "Please provide either csv_content (paste CSV text) "
            "or file_path (server-accessible path)."
        )

    if x_column not in headers:
        raise ValueError(f"Column '{x_column}' not found. Available: {list(headers)}")

    y_col_list = [c.strip() for c in y_columns.split(",") if c.strip()]
    missing = [c for c in y_col_list if c not in headers]
    if missing:
        raise ValueError(f"Column(s) not found: {missing}. Available: {list(headers)}")

    # ── Aggregate ──────────────────────────────────────────────────────────────
    if do_group:
        buckets: dict[str, dict[str, list]] = {}
        for row in rows:
            raw_x = row[x_column].strip()
            dt = _parse_date(raw_x)
            key = _period_key(dt, period) if dt else raw_x
            if key not in buckets:
                buckets[key] = {c: [] for c in y_col_list}
            for col in y_col_list:
                v = _to_float(row[col])
                if v is not None:
                    buckets[key][col].append(v)

        x_labels = sorted(buckets.keys())
        y_data: dict[str, list] = {c: [] for c in y_col_list}
        for key in x_labels:
            for col in y_col_list:
                vals = buckets[key][col]
                if not vals:
                    y_data[col].append(None)
                elif use_sum:
                    y_data[col].append(round(sum(vals), 2))
                else:
                    y_data[col].append(round(sum(vals) / len(vals), 2))
    else:
        x_labels = []
        y_data = {c: [] for c in y_col_list}
        for row in rows:
            x_labels.append(row[x_column].strip())
            for col in y_col_list:
                y_data[col].append(_to_float(row[col]))

    # ── Build chart config ─────────────────────────────────────────────────────
    ct = chart_type.strip().lower()
    datasets = []
    for i, col in enumerate(y_col_list):
        color = _COLORS[i % len(_COLORS)]
        ds: dict = {
            "label": col,
            "data": y_data[col],
            "backgroundColor": color + "33" if (ct == "line" and is_area) else color,
            "borderColor": color,
            "borderWidth": 2 if ct == "line" else 1,
            "spanGaps": True,
        }
        if ct == "line":
            ds.update({
                "fill": is_area,
                "tension": 0.3,
                "pointRadius": 3,
                "pointBackgroundColor": color,
            })
        datasets.append(ds)

    config = {
        "type": ct,
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
