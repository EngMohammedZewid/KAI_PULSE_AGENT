"""
file_upload_s3_tool.py
-----------------------
Tool that uploads a file shared in the chat to AWS S3 and returns a
24-hour presigned download link.

Supports:
  - Plain text  (CSV, JSON, TXT, Markdown, XML, YAML …)
  - Base64-encoded binary (XLSX, PDF, images, etc.)

The agent is instructed to call this tool automatically whenever a user
shares / uploads any file in the conversation.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request

from ibm_watsonx_orchestrate.agent_builder.tools import tool


# ---------------------------------------------------------------------------
# AWS SigV4 helpers  (stdlib-only, same pattern as chart_s3_tool.py)
# ---------------------------------------------------------------------------

def _hmac_sha256(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    k = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k = _hmac_sha256(k, region)
    k = _hmac_sha256(k, "s3")
    k = _hmac_sha256(k, "aws4_request")
    return k


def _s3_env() -> tuple[str, str, str, str]:
    def g(key: str, default: str = "") -> str:
        return (
            os.environ.get(f"WXO_CONNECTION_aws_credentials_{key}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_aws-credentials_{key}", "").strip()
            or os.environ.get(key, "").strip()
            or default
        )
    access_key = g("AWS_ACCESS_KEY_ID",     "")
    secret_key = g("AWS_SECRET_ACCESS_KEY", "")
    bucket     = g("AWS_S3_BUCKET",         "aws-buc-for-orchestrate")
    region     = g("AWS_S3_REGION",         "us-east-2")
    return access_key, secret_key, bucket, region


def _s3_put(
    file_key: str,
    body: bytes,
    content_type: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str,
) -> None:
    host = f"{bucket}.s3.{region}.amazonaws.com"
    t = datetime.datetime.utcnow()
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")

    canonical_uri = "/" + urllib.parse.quote(file_key.lstrip("/"), safe="/")
    payload_hash = hashlib.sha256(body).hexdigest()

    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "content-type;host;x-amz-content-sha256;x-amz-date"

    cr = "\n".join([
        "PUT", canonical_uri, "",
        canonical_headers, signed_headers, payload_hash,
    ])
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    sts = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        hashlib.sha256(cr.encode("utf-8")).hexdigest(),
    ])
    sig_key = _signing_key(secret_key, date_stamp, region)
    signature = hmac.new(sig_key, sts.encode("utf-8"), hashlib.sha256).hexdigest()

    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    url = f"https://{host}{canonical_uri}"
    req = urllib.request.Request(
        url, method="PUT", data=body,
        headers={
            "Content-Type": content_type,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": auth,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read()


def _presigned_get(
    file_key: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str,
    expires: int = 86400,
) -> str:
    host = f"{bucket}.s3.{region}.amazonaws.com"
    t = datetime.datetime.utcnow()
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    credential = f"{access_key}/{credential_scope}"

    canonical_uri = "/" + urllib.parse.quote(file_key.lstrip("/"), safe="/")
    qs_params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": credential,
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_qs = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(qs_params.items())
    )
    cr = "\n".join([
        "GET", canonical_uri, canonical_qs,
        f"host:{host}\n", "host", "UNSIGNED-PAYLOAD",
    ])
    sts = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        hashlib.sha256(cr.encode("utf-8")).hexdigest(),
    ])
    sig_key = _signing_key(secret_key, date_stamp, region)
    signature = hmac.new(sig_key, sts.encode("utf-8"), hashlib.sha256).hexdigest()

    return f"https://{host}{canonical_uri}?{canonical_qs}&X-Amz-Signature={signature}"


def _detect_content_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "csv":  "text/csv",
        "json": "application/json",
        "txt":  "text/plain",
        "md":   "text/markdown",
        "yaml": "application/yaml",
        "yml":  "application/yaml",
        "xml":  "application/xml",
        "html": "text/html",
        "pdf":  "application/pdf",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xls":  "application/vnd.ms-excel",
        "png":  "image/png",
        "jpg":  "image/jpeg",
        "jpeg": "image/jpeg",
        "gif":  "image/gif",
    }.get(ext, "application/octet-stream")


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@tool
def upload_chat_file_to_s3(
    filename: str,
    file_content: str,
    s3_folder: str = "uploads",
    is_base64: bool = False,
) -> str:
    """
    Upload a file shared in the chat to the AWS S3 bucket and return a
    24-hour presigned download link.

    Call this tool AUTOMATICALLY whenever the user shares or uploads any
    file in the conversation — do not wait for explicit instructions.

    Supports plain text files (CSV, JSON, TXT, Markdown, YAML, XML) and
    base64-encoded binary files (PDF, XLSX, PNG, JPG, etc.).

    Args:
        filename:     Original filename including extension, e.g. "sales.csv".
                      Used to determine the S3 key and content type.
        file_content: The file contents as a UTF-8 string for text files, or
                      as a base64-encoded string for binary files.
        s3_folder:    S3 prefix / virtual folder to store the file in.
                      Defaults to "uploads". Use "" for root.
        is_base64:    Set True if file_content is base64-encoded (binary files
                      such as PDF, XLSX, PNG). Set False (default) for plain text.

    Returns:
        A confirmation message with the S3 key and a 24-hour download link.
    """
    filename = (filename or "").strip()
    if not filename:
        return "filename is required."

    content_raw = (file_content or "").strip()
    if not content_raw:
        return "file_content is empty — please provide the file content."

    # Build the S3 key
    folder = (s3_folder or "").strip().strip("/")
    s3_key = f"{folder}/{filename}" if folder else filename

    # Decode content to bytes
    if is_base64:
        try:
            body = base64.b64decode(content_raw)
        except Exception as e:
            return f"Failed to decode base64 content: {e}"
    else:
        # Normalise escaped newlines that LLMs sometimes insert
        text = content_raw.replace("\\n", "\n").replace("\\t", "\t")
        body = text.encode("utf-8")

    content_type = _detect_content_type(filename)

    # Upload
    try:
        access_key, secret_key, bucket, region = _s3_env()
        _s3_put(s3_key, body, content_type, access_key, secret_key, bucket, region)
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        return f"Upload failed — S3 HTTP {e.code}: {err}"
    except Exception as e:
        return f"Upload failed: {e}"

    # Presigned download link (24 hours)
    try:
        link = _presigned_get(s3_key, access_key, secret_key, bucket, region)
    except Exception:
        link = f"https://{bucket}.s3.{region}.amazonaws.com/{urllib.parse.quote(s3_key, safe='/')}"

    size_kb = len(body) / 1024
    return (
        f"File uploaded to S3 successfully.\n"
        f"  Bucket   : {bucket} ({region})\n"
        f"  S3 key   : {s3_key}\n"
        f"  Size     : {size_kb:.1f} KB\n"
        f"  Type     : {content_type}\n\n"
        f"Download link (valid 24 hours):\n{link}"
    )
