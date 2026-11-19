"""export_table_tool.py
---------------------
Tool that exports tabular data (from conversation or user-provided) to
CSV or XLSX, uploads to AWS S3, and returns a presigned download link.

The agent can pass data as:
  - A list of dicts   → [{col: val, ...}, ...]
  - A CSV text string → "col1,col2\nval1,val2\n..."
  - A Markdown table  → "| col1 | col2 |\n|------|------|\n| val1 | val2 |"
"""

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
from typing import Any, Dict, List, Optional, Union

from tools._tooling import tool

# ---------------------------------------------------------------------------
# Credential helpers (same pattern as pdf_tool.py)
# ---------------------------------------------------------------------------

def _s3_env() -> tuple:
    def g(k: str, d: str = "") -> str:
        return (
            os.environ.get(f"WXO_CONNECTION_aws_credentials_{k}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_aws-credentials_{k}", "").strip()
            or os.environ.get(k, "").strip()
            or d
        )
    return (
        g("AWS_ACCESS_KEY_ID",     ""),
        g("AWS_SECRET_ACCESS_KEY", ""),
        g("AWS_S3_BUCKET",         "aws-buc-for-orchestrate"),
        g("AWS_S3_REGION",         "us-east-2"),
    )


def _signing_key(secret: str, date_str: str, region: str) -> bytes:
    def hmac_sha256(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()
    return hmac_sha256(
        hmac_sha256(
            hmac_sha256(
                hmac_sha256(f"AWS4{secret}".encode(), date_str),
                region,
            ),
            "s3",
        ),
        "aws4_request",
    )


def _s3_put(s3_key: str, body: bytes, content_type: str,
            ak: str, sk: str, bucket: str, region: str) -> None:
    host = f"{bucket}.s3.{region}.amazonaws.com"
    t = datetime.datetime.utcnow()
    ad = t.strftime("%Y%m%dT%H%M%SZ")
    ds = t.strftime("%Y%m%d")
    uri = "/" + urllib.parse.quote(s3_key.lstrip("/"), safe="/")
    ph = hashlib.sha256(body).hexdigest()
    ch = f"content-type:{content_type}\nhost:{host}\nx-amz-content-sha256:{ph}\nx-amz-date:{ad}\n"
    sh = "content-type;host;x-amz-content-sha256;x-amz-date"
    cr = "\n".join(["PUT", uri, "", ch, sh, ph])
    cs = f"{ds}/{region}/s3/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", ad, cs, hashlib.sha256(cr.encode()).hexdigest()])
    sig = hmac.new(_signing_key(sk, ds, region), sts.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={ak}/{cs}, SignedHeaders={sh}, Signature={sig}"
    req = urllib.request.Request(
        f"https://{host}{uri}", method="PUT", data=body,
        headers={"Content-Type": content_type, "x-amz-date": ad,
                 "x-amz-content-sha256": ph, "Authorization": auth},
    )
    try:
        with urllib.request.urlopen(req, timeout=120):
            pass
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"S3 PUT {e.code}: {e.read().decode('utf-8', errors='replace')}") from e


def _s3_presign(s3_key: str, ak: str, sk: str, bucket: str, region: str,
                expires: int = 86400) -> str:
    host = f"{bucket}.s3.{region}.amazonaws.com"
    t = datetime.datetime.utcnow()
    ad = t.strftime("%Y%m%dT%H%M%SZ")
    ds = t.strftime("%Y%m%d")
    uri = "/" + urllib.parse.quote(s3_key.lstrip("/"), safe="/")
    cs = f"{ds}/{region}/s3/aws4_request"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{ak}/{cs}",
        "X-Amz-Date": ad,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    cqs = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(params.items())
    )
    cr = "\n".join(["GET", uri, cqs, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    sts = "\n".join(["AWS4-HMAC-SHA256", ad, cs, hashlib.sha256(cr.encode()).hexdigest()])
    sig = hmac.new(_signing_key(sk, ds, region), sts.encode(), hashlib.sha256).hexdigest()
    return f"https://{host}{uri}?{cqs}&X-Amz-Signature={sig}"


# ---------------------------------------------------------------------------
# Data parsing helpers
# ---------------------------------------------------------------------------

_MD_TABLE_ROW = re.compile(r"^\|(.+)\|$")


def _parse_markdown_table(text: str) -> List[Dict[str, str]]:
    """Parse a Markdown pipe-table into a list of dicts."""
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    rows = [l for l in lines if _MD_TABLE_ROW.match(l)]
    if len(rows) < 2:
        return []
    headers = [h.strip() for h in rows[0].strip("|").split("|")]
    data_rows = []
    for row in rows[1:]:
        # skip separator lines like |---|---|
        if re.match(r"^\|[\s\-\|:]+\|$", row):
            continue
        cells = [c.strip() for c in row.strip("|").split("|")]
        # pad or trim to match header length
        while len(cells) < len(headers):
            cells.append("")
        data_rows.append(dict(zip(headers, cells[:len(headers)])))
    return data_rows


def _parse_csv_text(text: str) -> List[Dict[str, str]]:
    """Parse a CSV string into a list of dicts."""
    reader = csv.DictReader(io.StringIO(text.strip()))
    return [dict(row) for row in reader]


def _normalise_data(raw: Any) -> List[Dict[str, Any]]:
    """
    Accept multiple input types and return a uniform list of dicts.
      - list of dicts → return as-is
      - JSON string   → parse, then try list/dict.data
      - CSV string    → parse with csv.DictReader
      - Markdown table string → parse pipe table
    """
    if isinstance(raw, list):
        return raw  # type: ignore[return-value]

    if isinstance(raw, dict):
        # maybe {"data": [...]}
        if isinstance(raw.get("data"), list):
            return raw["data"]
        # single-row dict
        return [raw]

    if not isinstance(raw, str):
        return []

    text = raw.strip()

    # Try JSON first
    try:
        parsed = json.loads(text)
        return _normalise_data(parsed)
    except (json.JSONDecodeError, ValueError):
        pass

    # Markdown table
    if "|" in text and _MD_TABLE_ROW.match(text.splitlines()[0].strip()):
        rows = _parse_markdown_table(text)
        if rows:
            return rows

    # CSV fallback
    rows = _parse_csv_text(text)
    return rows


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------

def _to_csv_bytes(rows: List[Dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")  # BOM for Excel compatibility


def _to_xlsx_bytes(rows: List[Dict[str, Any]], sheet_name: str = "Sheet1") -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]  # Excel sheet name max 31 chars

    if not rows:
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    headers = list(rows[0].keys())

    # Header row styling
    header_fill = PatternFill("solid", fgColor="1A4D88")
    header_font = Font(bold=True, color="FFFFFF", size=11)

    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # Data rows — alternate row shading
    stripe_fill = PatternFill("solid", fgColor="EDF2F7")
    for row_idx, record in enumerate(rows, start=2):
        fill = stripe_fill if row_idx % 2 == 0 else None
        for col_idx, header in enumerate(headers, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=record.get(header, ""))
            if fill:
                cell.fill = fill
            cell.alignment = Alignment(vertical="center")

    # Auto-fit column widths (approximate)
    for col_idx, header in enumerate(headers, start=1):
        max_len = len(str(header))
        for record in rows:
            val = str(record.get(header, ""))
            if len(val) > max_len:
                max_len = len(val)
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = min(max_len + 4, 60)

    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Main tool
# ---------------------------------------------------------------------------

@tool
def export_table_to_file(
    data: str,
    file_format: str = "xlsx",
    filename: str = "",
    sheet_name: str = "Data",
    s3_folder: str = "exports",
) -> str:
    """Export tabular data to an Excel (XLSX) or CSV file and return a download link.

    Args:
        data: The table data to export. Accepted formats:
              - A JSON array of objects: '[{"Name":"Alice","Age":30}, ...]'
              - A CSV string: 'Name,Age\\nAlice,30\\n...'
              - A Markdown pipe table: '| Name | Age |\\n|------|-----|\\n| Alice | 30 |'
        file_format: Output format — "xlsx" (default) or "csv".
        filename: Optional base filename (without extension). A timestamp is
                  appended automatically. Defaults to "export".
        sheet_name: Sheet name for XLSX files (default: "Data"). Ignored for CSV.
        s3_folder: S3 folder/prefix for the uploaded file (default: "exports").

    Returns:
        A presigned S3 download URL valid for 24 hours, or an error message.
    """
    # --- Parse data ---
    rows = _normalise_data(data)
    if not rows:
        return (
            "Could not parse the provided data. Please pass one of:\n"
            "  • A JSON array of objects\n"
            "  • A CSV string with a header row\n"
            "  • A Markdown pipe table"
        )

    fmt = file_format.lower().strip().lstrip(".")
    if fmt not in ("csv", "xlsx"):
        fmt = "xlsx"

    # --- Serialise ---
    try:
        if fmt == "csv":
            file_bytes = _to_csv_bytes(rows)
            content_type = "text/csv"
        else:
            file_bytes = _to_xlsx_bytes(rows, sheet_name=sheet_name)
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    except Exception as e:
        return f"Failed to generate {fmt.upper()} file: {e}"

    # --- S3 upload ---
    ak, sk, bucket, region = _s3_env()
    base = (filename.strip() or "export")
    # sanitise filename
    base = re.sub(r"[^\w\-]", "_", base)
    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    s3_key = f"{s3_folder.rstrip('/')}/{base}_{ts}.{fmt}"

    try:
        _s3_put(s3_key, file_bytes, content_type, ak, sk, bucket, region)
    except Exception as e:
        return f"S3 upload failed: {e}"

    # --- Presigned URL ---
    try:
        url = _s3_presign(s3_key, ak, sk, bucket, region, expires=86400)
    except Exception as e:
        return f"Failed to generate download link: {e}"

    row_count = len(rows)
    col_count = len(rows[0]) if rows else 0
    ext = fmt.upper()
    return (
        f"Your {ext} file is ready — {row_count} rows × {col_count} columns.\n\n"
        f"[Download {base}.{fmt}]({url})\n\n"
        f"*(Link expires in 24 hours)*"
    )
