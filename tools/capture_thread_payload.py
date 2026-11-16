"""
capture_thread_payload.py
--------------------------
A watsonx Orchestrate ADK tool that captures the FULL conversation history
of the current thread (all user/assistant messages from message 1 onwards)
and saves it as a structured JSON file to AWS S3.

Unlike capture_agent_payload (which records only the current single turn),
this tool fetches every message in the thread via the WXO Threads API and
writes a single, self-contained JSON snapshot of the whole conversation.

S3 credentials are read from the WXO Connection named 'aws-credentials':
  WXO_CONNECTION_aws_credentials_AWS_ACCESS_KEY_ID
  WXO_CONNECTION_aws_credentials_AWS_SECRET_ACCESS_KEY
  WXO_CONNECTION_aws_credentials_AWS_S3_BUCKET
  WXO_CONNECTION_aws_credentials_AWS_S3_REGION  (default: us-east-1)

WXO API credentials:
  WXO_API_ENDPOINT / WO_INSTANCE   watsonx Orchestrate instance URL
  WXO_API_KEY / WO_API_KEY         IBM Cloud API key
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from ibm_watsonx_orchestrate.agent_builder.tools import tool


# ---------------------------------------------------------------------------
# AWS S3 helpers (stdlib-only, SigV4) — same pattern as pdf_report_tool.py
# ---------------------------------------------------------------------------

def _hmac_sha256(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    k = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k = _hmac_sha256(k, region)
    k = _hmac_sha256(k, "s3")
    k = _hmac_sha256(k, "aws4_request")
    return k


def _s3_put_bytes(
    file_key: str,
    body: bytes,
    content_type: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str,
) -> None:
    host = f"{bucket}.s3.{region}.amazonaws.com"
    t = datetime.utcnow()
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
    cr = "\n".join(["PUT", canonical_uri, "", canonical_headers, signed_headers, payload_hash])
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    sts = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        hashlib.sha256(cr.encode()).hexdigest(),
    ])
    sig = hmac.new(
        _signing_key(secret_key, date_stamp, region), sts.encode(), hashlib.sha256
    ).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={sig}"
    )
    req = urllib.request.Request(
        f"https://{host}{canonical_uri}",
        method="PUT",
        data=body,
        headers={
            "Content-Type": content_type,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": auth,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120):
            pass
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"S3 PUT failed ({e.code}): {e.read().decode('utf-8', errors='replace')}"
        ) from e


def _s3_presign_get(
    file_key: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str,
    expires: int = 86400,
) -> str:
    host = f"{bucket}.s3.{region}.amazonaws.com"
    t = datetime.utcnow()
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")
    canonical_uri = "/" + urllib.parse.quote(file_key.lstrip("/"), safe="/")
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    credential = f"{access_key}/{credential_scope}"
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
    canonical_headers = f"host:{host}\n"
    cr = "\n".join([
        "GET", canonical_uri, canonical_qs, canonical_headers, "host", "UNSIGNED-PAYLOAD"
    ])
    sts = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        hashlib.sha256(cr.encode()).hexdigest(),
    ])
    sig = hmac.new(
        _signing_key(secret_key, date_stamp, region), sts.encode(), hashlib.sha256
    ).hexdigest()
    return f"https://{host}{canonical_uri}?{canonical_qs}&X-Amz-Signature={sig}"


def _s3_env() -> tuple:
    def g(key: str, default: str = "") -> str:
        return (
            os.environ.get(f"WXO_CONNECTION_aws_credentials_{key}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_aws-credentials_{key}", "").strip()
            or os.environ.get(key, "").strip()
            or default
        )
    ak     = g("AWS_ACCESS_KEY_ID",     "")
    sk     = g("AWS_SECRET_ACCESS_KEY", "")
    bucket = g("AWS_S3_BUCKET",         "aws-buc-for-orchestrate")
    region = g("AWS_S3_REGION",         "us-east-2")
    return ak, sk, bucket, region


# ---------------------------------------------------------------------------
# WXO Threads API helpers
# ---------------------------------------------------------------------------

def _get_env(*keys: str) -> Optional[str]:
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    return None


def _normalize_rows(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    if isinstance(data, list):
        return data
    return []


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(
                    item.get("text") or item.get("content") or json.dumps(item, ensure_ascii=False)
                )
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()
    if content is None:
        return ""
    return str(content)


def _fetch_thread_messages(thread_id: str, base_url: str, api_key: str) -> List[Dict[str, Any]]:
    """
    Fetch all user/assistant messages for a given thread from the WXO Threads API.
    Returns a list of message dicts sorted by timestamp ascending.
    """
    from ibm_watsonx_orchestrate.client.client import Client
    from ibm_watsonx_orchestrate.client.credentials import Credentials
    from ibm_watsonx_orchestrate_clients.threads.threads_client import ThreadsClient

    creds = Credentials(url=base_url, api_key=api_key)
    client = Client(creds)
    threads_client = ThreadsClient(base_url=base_url, api_key=client.token)

    raw_messages = _normalize_rows(threads_client.get_thread_messages(thread_id))

    messages: List[Dict[str, Any]] = []
    for msg in raw_messages:
        role = str(msg.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        timestamp = (
            msg.get("created_at")
            or msg.get("updated_at")
            or msg.get("created_on")
        )
        messages.append({
            "id": str(msg.get("id") or ""),
            "role": role,
            "content": _extract_text(msg.get("content")),
            "timestamp": str(timestamp) if timestamp else None,
            "run_id": str(msg.get("run_id") or "") or None,
        })

    # Sort by timestamp ascending (earliest first)
    messages.sort(key=lambda m: m.get("timestamp") or "")
    return messages


# ---------------------------------------------------------------------------
# ADK Tool
# ---------------------------------------------------------------------------

@tool
def capture_thread_payload(
    thread_id: str,
    agent_id: str = "",
    session_label: str = "",
) -> str:
    """
    Capture the full conversation history of the current thread and save it as
    a JSON file to S3.

    This tool fetches EVERY user and assistant message in the thread from the
    beginning of the conversation, not just the current turn. Use it at any
    point during or after a conversation to create a complete payload snapshot.

    The JSON file is uploaded to S3 under:
      thread-payloads/{YYYYMMDD}/{thread_id}.json

    Args:
        thread_id:     The current thread ID (conversation ID). Required.
        agent_id:      The watsonx Orchestrate agent UUID. Falls back to the
                       WXO_AGENT_ID environment variable if not provided.
        session_label: Optional human-readable label embedded in the JSON for
                       easy identification (e.g. "sales_demo", "support_case_123").

    Returns:
        A message with the S3 key, message count, and a 24-hour download link.
    """
    load_dotenv(dotenv_path=Path(".env"))

    thread_id = (thread_id or "").strip()
    if not thread_id:
        return "thread_id is required."

    resolved_agent_id = (agent_id or "").strip() or os.getenv("WXO_AGENT_ID", "")

    # --- Fetch messages from WXO Threads API ---
    base_url = (
        _get_env("WXO_API_ENDPOINT", "WO_INSTANCE")
        or "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/16cd2449-1e4f-4fc9-a4d8-9d69c3864a61"
    )
    api_key = (
        _get_env("WXO_API_KEY", "WO_API_KEY")
    )
    if not base_url or not api_key:
        return (
            "Missing WXO credentials. "
            "Set WXO_API_ENDPOINT and WXO_API_KEY (or WO_INSTANCE / WO_API_KEY)."
        )

    try:
        messages = _fetch_thread_messages(thread_id, base_url, api_key)
    except Exception as exc:
        return f"Failed to fetch thread messages: {exc}"

    if not messages:
        return f"No user/assistant messages found in thread {thread_id}."

    # --- Build payload document ---
    captured_at = datetime.now(timezone.utc)
    payload = {
        "thread_id": thread_id,
        "agent_id": resolved_agent_id,
        "session_label": session_label or "",
        "captured_at": captured_at.isoformat(),
        "message_count": len(messages),
        "messages": messages,
    }

    # --- Upload to S3 ---
    date_str = captured_at.strftime("%Y%m%d")
    s3_key = f"thread-payloads/{date_str}/{thread_id}.json"

    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    try:
        ak, sk, bucket, region = _s3_env()
        _s3_put_bytes(s3_key, body, "application/json", ak, sk, bucket, region)
        download_url = _s3_presign_get(s3_key, ak, sk, bucket, region, expires=86400)
    except RuntimeError as exc:
        return (
            f"Thread payload built ({len(messages)} messages) but S3 upload failed: {exc}"
        )

    size_kb = len(body) / 1024
    return (
        f"Thread payload captured and uploaded to S3.\n\n"
        f"  Thread   : {thread_id}\n"
        f"  Agent    : {resolved_agent_id or 'n/a'}\n"
        f"  Messages : {len(messages)}\n"
        f"  S3 key   : {s3_key}\n"
        f"  Size     : {size_kb:.1f} KB\n\n"
        f"[⬇ Download conversation JSON]({download_url})\n\n"
        f"_Link expires in 24 hours._"
    )
