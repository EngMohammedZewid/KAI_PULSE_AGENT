from __future__ import annotations

import csv
import datetime
import io
import json
import os
import re
import urllib.parse
import urllib.request

from ibm_watsonx_orchestrate.agent_builder.tools import tool

# ---------------------------------------------------------------------------
# IBM Cloud Object Storage helpers (IAM token auth, stdlib only — no ibm_boto3)
# ---------------------------------------------------------------------------
# Required env vars (set in watsonx Orchestrate .env or environment):
#   COS_API_KEY   — IBM Cloud API key with COS Reader/Writer access
#   COS_BUCKET    — Target bucket name
#   COS_ENDPOINT  — e.g. s3.us-south.cloud-object-storage.appdomain.cloud
# ---------------------------------------------------------------------------

_COLORS = [
    "#2E6DB4", "#C8A84B", "#E05C2E", "#3DAA6D",
    "#9B59B6", "#1ABC9C", "#E74C3C", "#F39C12",
]
_QUICKCHART = "https://quickchart.io/chart"
_IAM_URL = "https://iam.cloud.ibm.com/identity/token"


# ── Auth helpers ─────────────────────────────────────────────────────────────

def _get_iam_token(api_key: str) -> str:
    """Exchange an IBM Cloud API key for a short-lived IAM bearer token."""
    data = urllib.parse.urlencode({
        "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
        "apikey": api_key,
    }).encode()
    req = urllib.request.Request(
        _IAM_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["access_token"]


def _cos_request(method: str, path: str, token: str, endpoint: str) -> bytes:
    """Make an authenticated request to COS and return the raw response body."""
    url = f"https://{endpoint.rstrip('/')}/{path.lstrip('/')}"
    req = urllib.request.Request(
        url,
        method=method,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _list_objects(bucket: str, endpoint: str, token: str, prefix: str = "") -> list[tuple[str, str]]:
    """Return [(key, size_bytes), ...] for objects in the bucket."""
    params = urllib.parse.urlencode({"prefix": prefix, "max-keys": "200"})
    raw = _cos_request("GET", f"{bucket}?{params}", token, endpoint)
    xml = raw.decode("utf-8", errors="replace")
    keys = re.findall(r"<Key>([^<]+)</Key>", xml)
    sizes = re.findall(r"<Size>([^<]+)</Size>", xml)
    if len(sizes) < len(keys):
        sizes += ["?"] * (len(keys) - len(sizes))
    return list(zip(keys, sizes))


def _fetch_csv(bucket: str, key: str, endpoint: str, token: str) -> str:
    """Download a COS object and return its text content."""
    raw = _cos_request("GET", f"{bucket}/{urllib.parse.quote(key, safe='/')}", token, endpoint)
    return raw.decode("utf-8-sig", errors="replace")


def _cos_env() -> tuple[str, str, str]:
    """Return (api_key, bucket, endpoint) from environment or raise.
    Orchestrate injects connection credentials as WXO_CONNECTION_{app_id}_{KEY}.
    """
    pfx = "WXO_CONNECTION_cos_credentials_"
    api_key  = os.environ.get(f"{pfx}COS_API_KEY", "").strip()
    bucket   = os.environ.get(f"{pfx}COS_BUCKET", "").strip()
    endpoint = os.environ.get(f"{pfx}COS_ENDPOINT", "").strip()
    if not api_key:
        raise RuntimeError(f"{pfx}COS_API_KEY environment variable is not set.")
    if not bucket:
        raise RuntimeError(f"{pfx}COS_BUCKET environment variable is not set.")
    if not endpoint:
        raise RuntimeError(f"{pfx}COS_ENDPOINT environment variable is not set.")
    return api_key, bucket, endpoint


# ── Chart helpers (same as chart_csv_tool.py) ────────────────────────────────

def _chart_url(config: dict, width: int = 700, height: int = 420) -> str:
    import copy
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
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _parse_date(s: str) -> datetime.date | None:
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


def _build_chart(
    csv_text: str,
    x_column: str,
    y_columns: str,
    title: str,
    area: str,
    aggregate: str,
    group_by: str,
    chart_type: str,
) -> str:
    """Shared chart-building logic used by all COS chart tools."""
    is_area = _to_bool(area)
    do_group = group_by.strip().lower() != "none"
    period = group_by.strip().lower()
    use_sum = aggregate.strip().lower() != "avg"

    reader = csv.DictReader(io.StringIO(csv_text))
    headers = list(reader.fieldnames or [])
    rows = list(reader)

    if x_column not in headers:
        raise ValueError(f"Column '{x_column}' not found. Available: {headers}")

    y_col_list = [c.strip() for c in y_columns.split(",") if c.strip()]
    missing = [c for c in y_col_list if c not in headers]
    if missing:
        raise ValueError(f"Column(s) not found: {missing}. Available: {headers}")

    # Aggregate
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

    # Build Chart.js config
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
            ds.update({"fill": is_area, "tension": 0.3, "pointRadius": 3, "pointBackgroundColor": color})
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


# ── Public tools ─────────────────────────────────────────────────────────────

@tool
def list_cos_files(
    prefix: str = "",
) -> str:
    """
    List files stored in the IBM Cloud Object Storage bucket.
    Use this to discover available file names before calling render_chart_from_cos
    or inspect_cos_file.

    Args:
        prefix: Optional filter prefix. For example "sales/" to list only files
                in a folder named 'sales'. Leave blank to list all files.

    Returns:
        A numbered list of file names and their sizes.
    """
    try:
        api_key, bucket, endpoint = _cos_env()
        token = _get_iam_token(api_key)
        objects = _list_objects(bucket, endpoint, token, prefix)
    except Exception as e:
        return f"Error connecting to COS: {e}"

    if not objects:
        msg = f"No files found in bucket '{bucket}'"
        if prefix:
            msg += f" with prefix '{prefix}'"
        return msg + "."

    lines = [f"Files in bucket '{bucket}'", "=" * 40]
    for i, (key, size) in enumerate(objects, 1):
        try:
            kb = f"{int(size) / 1024:.1f} KB"
        except (ValueError, TypeError):
            kb = size
        lines.append(f"  {i:3}. {key}  ({kb})")
    lines.append("")
    lines.append(f"Total: {len(objects)} file(s)")
    lines.append("")
    lines.append("Tip: Use the exact file name as file_key in render_chart_from_cos or inspect_cos_file.")
    return "\n".join(lines)


@tool
def inspect_cos_file(
    file_key: str,
) -> str:
    """
    Download a CSV file from IBM Cloud Object Storage and inspect its structure:
    column names, row count, and a 3-row sample.

    Use this before render_chart_from_cos to confirm exact column names.

    Args:
        file_key: The exact file name (key) in the bucket.
                  Use list_cos_files to find available files.
                  Example: "sales_data.csv"

    Returns:
        Column names, row count, and sample rows from the file.
    """
    try:
        api_key, bucket, endpoint = _cos_env()
        token = _get_iam_token(api_key)
        csv_text = _fetch_csv(bucket, file_key, endpoint, token)
    except Exception as e:
        return f"Error fetching '{file_key}' from COS: {e}"

    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)

    if not rows:
        return f"File '{file_key}' appears to be empty."

    header = rows[0]
    sample = rows[1:4]
    total_data_rows = max(0, len(rows) - 1)

    lines = [
        f"File: {file_key}",
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
    lines.append("Tip: Use these exact column names in render_chart_from_cos.")
    return "\n".join(lines)


@tool
def render_chart_from_cos(
    file_key: str,
    x_column: str,
    y_columns: str,
    title: str = "Chart",
    area: str = "false",
    aggregate: str = "sum",
    group_by: str = "month",
    chart_type: str = "line",
) -> str:
    """
    Download a CSV file from IBM Cloud Object Storage and draw a chart from it.

    Use list_cos_files to find file names, and inspect_cos_file to confirm
    exact column names before calling this tool.

    Args:
        file_key:   The exact file name (key) in the COS bucket.
                    Example: "sales_data.csv"
        x_column:   Column name to use as the X-axis (dates or categories).
                    Example: "Order Date"
        y_columns:  Comma-separated column names to plot as data series.
                    Example: "Revenue,Cost"
        title:      Chart title displayed above the chart.
        area:       "true" to fill area under lines (area chart). Default "false".
        aggregate:  How to combine values per group: "sum" or "avg". Default "sum".
        group_by:   Time grouping: "month", "quarter", "year", or "none".
                    Default "month".
        chart_type: "line" or "bar". Default "line".

    Returns:
        Markdown image that renders as an inline chart, plus a download link.
    """
    try:
        api_key, bucket, endpoint = _cos_env()
        token = _get_iam_token(api_key)
        csv_text = _fetch_csv(bucket, file_key, endpoint, token)
    except Exception as e:
        return f"Error fetching '{file_key}' from COS: {e}"

    try:
        return _build_chart(
            csv_text=csv_text,
            x_column=x_column,
            y_columns=y_columns,
            title=title,
            area=area,
            aggregate=aggregate,
            group_by=group_by,
            chart_type=chart_type,
        )
    except ValueError as e:
        return f"Chart error: {e}"
