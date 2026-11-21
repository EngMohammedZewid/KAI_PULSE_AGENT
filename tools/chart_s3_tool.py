from __future__ import annotations

import csv
import datetime
import hashlib
import hmac
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from tools._settings import require_bucket
from tools._tooling import tool
from tools._chart_spec import records_chart

# ---------------------------------------------------------------------------
# AWS S3 tools — uses stdlib SigV4 signing, no boto3 required
# Required env vars (injected via Orchestrate connection):
#   AWS_ACCESS_KEY_ID
#   AWS_SECRET_ACCESS_KEY
#   AWS_S3_BUCKET
#   AWS_S3_REGION
# ---------------------------------------------------------------------------

_COLORS = [
    "#2E6DB4", "#C8A84B", "#E05C2E", "#3DAA6D",
    "#9B59B6", "#1ABC9C", "#E74C3C", "#F39C12",
]
_QUICKCHART = "https://quickchart.io/chart"


# ── AWS SigV4 helpers ────────────────────────────────────────────────────────

def _hmac_sha256(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    k = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k = _hmac_sha256(k, region)
    k = _hmac_sha256(k, "s3")
    k = _hmac_sha256(k, "aws4_request")
    return k


def _s3_request(
    method: str,
    path: str,
    query: dict,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str,
    body: bytes = b"",
    extra_headers: dict | None = None,
) -> bytes:
    """Make a SigV4-signed request to S3 and return raw response bytes."""
    host = f"{bucket}.s3.{region}.amazonaws.com"
    t = datetime.datetime.utcnow()
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")

    canonical_uri = "/" + path.lstrip("/")
    canonical_qs = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(query.items())
    )

    # x-amz-content-sha256 is required by S3 — must be in canonical headers AND in the request
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"

    canonical_request = "\n".join([
        method,
        canonical_uri,
        canonical_qs,
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    sig_key = _signing_key(secret_key, date_stamp, region)
    signature = hmac.new(sig_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    sep = "?" if canonical_qs else ""
    url = f"https://{host}{canonical_uri}{sep}{canonical_qs}"
    headers = {
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Authorization": authorization,
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(
        url,
        method=method,
        data=body if body else None,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _s3_env() -> tuple[str, str, str, str]:
    """Return (access_key, secret_key, bucket, region).
    Lookup order:
      1. WXO_CONNECTION_aws_credentials_{KEY}  (Orchestrate app connection injection)
      2. WXO_CONNECTION_aws-credentials_{KEY}  (hyphen variant)
      3. Plain AWS_{KEY} env var
      4. Hardcoded defaults
    """
    def g(key: str, default: str = "") -> str:
        return (
            os.environ.get(f"WXO_CONNECTION_aws_credentials_{key}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_aws-credentials_{key}", "").strip()
            or os.environ.get(key, "").strip()
            or default
        )
    access_key = g("AWS_ACCESS_KEY_ID",     "")
    secret_key = g("AWS_SECRET_ACCESS_KEY", "")
    bucket     = require_bucket(g("AWS_S3_BUCKET"))
    region     = g("AWS_S3_REGION",         "us-east-2")
    return access_key, secret_key, bucket, region


def _list_s3_objects(access_key, secret_key, bucket, region, prefix="") -> list[tuple[str, str]]:
    query = {"list-type": "2", "max-keys": "200"}
    if prefix:
        query["prefix"] = prefix
    try:
        raw = _s3_request("GET", "/", query, access_key, secret_key, bucket, region)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"S3 HTTP {e.code} listing bucket: {body}") from e
    xml = raw.decode("utf-8", errors="replace")
    keys = re.findall(r"<Key>([^<]+)</Key>", xml)
    sizes = re.findall(r"<Size>([^<]+)</Size>", xml)
    if len(sizes) < len(keys):
        sizes += ["?"] * (len(keys) - len(sizes))
    return list(zip(keys, sizes))


def _fetch_s3_csv(access_key, secret_key, bucket, region, key) -> str:
    try:
        raw = _s3_request(
            "GET",
            urllib.parse.quote(key, safe="/"),
            {},
            access_key, secret_key, bucket, region,
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"S3 HTTP {e.code} fetching '{key}': {body}") from e
    return raw.decode("utf-8-sig", errors="replace")


# ── Chart helpers ─────────────────────────────────────────────────────────────

@records_chart
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


def _build_chart(csv_text, x_column, y_columns, title, area, aggregate, group_by, chart_type) -> str:
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


# ── Public tools ──────────────────────────────────────────────────────────────

@tool
def upload_file_to_s3(
    file_key: str,
    content: str,
) -> str:
    """
    Upload text content (CSV, JSON, TXT, etc.) as a file to the AWS S3 bucket.

    Use this to save data directly from the chat into S3 so it can later be
    used with render_chart_from_s3 or inspect_s3_file.

    How to use:
      1. Paste your CSV/text content into the 'content' parameter.
      2. Provide a filename as 'file_key' (e.g. "sales_data.csv").
      3. The file will be created (or overwritten) in the S3 bucket.

    Args:
        file_key: The filename to store in S3, e.g. "sales_data.csv".
                  You can include a folder path: "reports/sales_2025.csv".
        content:  The full text content to upload (CSV rows, JSON, etc.).
                  Tip: paste your CSV data here including the header row.

    Returns:
        Confirmation message with the file location, or an error description.
    """
    text = (content or "").strip()
    if not text:
        return "content is empty — please provide the file content to upload."
    if not file_key.strip():
        return "file_key is empty — please provide a filename (e.g. 'sales_data.csv')."

    # Normalise escaped newlines that LLMs sometimes produce
    text = text.replace("\\n", "\n").replace("\\t", "\t")
    body = text.encode("utf-8")

    # Detect content type
    lower_key = file_key.lower()
    if lower_key.endswith(".csv"):
        content_type = "text/csv"
    elif lower_key.endswith(".json"):
        content_type = "application/json"
    else:
        content_type = "text/plain"

    try:
        access_key, secret_key, bucket, region = _s3_env()
        _s3_request(
            "PUT",
            urllib.parse.quote(file_key.strip(), safe="/"),
            {},
            access_key, secret_key, bucket, region,
            body=body,
            extra_headers={"Content-Type": content_type},
        )
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        return f"Upload failed — S3 HTTP {e.code}: {err_body}"
    except Exception as e:
        return f"Upload failed: {e}"

    size_kb = len(body) / 1024
    return (
        f"File uploaded successfully.\n"
        f"  Bucket : {bucket} ({region})\n"
        f"  Key    : {file_key}\n"
        f"  Size   : {size_kb:.1f} KB\n\n"
        f"You can now use inspect_s3_file or render_chart_from_s3 with file_key=\"{file_key}\"."
    )


@tool
def list_s3_files(
    prefix: str = "",
) -> str:
    """
    List files stored in the AWS S3 bucket configured for this agent.
    Use this to discover available file names before calling render_chart_from_s3
    or inspect_s3_file.

    Args:
        prefix: Optional filter prefix. For example "sales/" to list only files
                in a folder named 'sales'. Leave blank to list all files.

    Returns:
        A numbered list of file names and their sizes.
    """
    try:
        access_key, secret_key, bucket, region = _s3_env()
        objects = _list_s3_objects(access_key, secret_key, bucket, region, prefix)
    except Exception as e:
        return f"Error connecting to S3: {e}"

    if not objects:
        msg = f"No files found in bucket '{bucket}'"
        if prefix:
            msg += f" with prefix '{prefix}'"
        return msg + "."

    lines = [f"Files in S3 bucket '{bucket}' ({region})", "=" * 50]
    for i, (key, size) in enumerate(objects, 1):
        try:
            kb = f"{int(size) / 1024:.1f} KB"
        except (ValueError, TypeError):
            kb = size
        lines.append(f"  {i:3}. {key}  ({kb})")
    lines.append("")
    lines.append(f"Total: {len(objects)} file(s)")
    lines.append("")
    lines.append("Tip: Use the exact file name as file_key in render_chart_from_s3 or inspect_s3_file.")
    return "\n".join(lines)


@tool
def inspect_s3_file(
    file_key: str,
) -> str:
    """
    Download a CSV file from the AWS S3 bucket and inspect its structure:
    column names, row count, and a 3-row sample.

    Use this before render_chart_from_s3 to confirm exact column names.

    Args:
        file_key: The exact file name (key) in the S3 bucket.
                  Use list_s3_files to find available files.
                  Example: "sales_data.csv"

    Returns:
        Column names, row count, and sample rows from the file.
    """
    try:
        access_key, secret_key, bucket, region = _s3_env()
        csv_text = _fetch_s3_csv(access_key, secret_key, bucket, region, file_key)
    except Exception as e:
        return f"Error fetching '{file_key}' from S3: {e}"

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
    lines.append("Tip: Use these exact column names in render_chart_from_s3.")
    return "\n".join(lines)


@tool
def render_chart_from_s3(
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
    Download a CSV file from AWS S3 and draw a chart from it.

    Use list_s3_files to find file names, and inspect_s3_file to confirm
    exact column names before calling this tool.

    Args:
        file_key:   The exact file name (key) in the S3 bucket.
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
        access_key, secret_key, bucket, region = _s3_env()
        csv_text = _fetch_s3_csv(access_key, secret_key, bucket, region, file_key)
    except Exception as e:
        return f"Error fetching '{file_key}' from S3: {e}"

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
