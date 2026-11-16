"""
payload_extractor_tool.py
--------------------------
A watsonx Orchestrate ADK tool that intercepts the agent's own
conversation turn and writes the full request / response payload to an
external database.

Supported DB targets (controlled by the DB_TYPE env var):
  - postgresql  (default)
  - mysql
  - sqlite      (for local dev / testing)
  - mongodb

Dependencies (add to requirements.txt):
  ibm-watsonx-orchestrate
  sqlalchemy>=2.0
  psycopg2-binary        # PostgreSQL
  pymysql                # MySQL  (optional)
  pymongo                # MongoDB (optional)
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
from typing import Any, Dict, Optional

from ibm_watsonx_orchestrate.agent_builder.tools import tool


# ---------------------------------------------------------------------------
# AWS S3 helpers (stdlib-only, SigV4)
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
    sig = hmac.new(_signing_key(secret_key, date_stamp, region), sts.encode(), hashlib.sha256).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={sig}"
    )
    req = urllib.request.Request(
        f"https://{host}{canonical_uri}",
        method="PUT", data=body,
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
    signed_headers = "host"
    cr = "\n".join(["GET", canonical_uri, canonical_qs, canonical_headers, signed_headers, "UNSIGNED-PAYLOAD"])
    sts = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        hashlib.sha256(cr.encode()).hexdigest(),
    ])
    sig = hmac.new(_signing_key(secret_key, date_stamp, region), sts.encode(), hashlib.sha256).hexdigest()
    return f"https://{host}{canonical_uri}?{canonical_qs}&X-Amz-Signature={sig}"


def _s3_env() -> tuple:
    def g(k):
        return os.environ.get(f"WXO_CONNECTION_aws_credentials_{k}") or os.environ.get(k, "")
    ak = g("AWS_ACCESS_KEY_ID")
    sk = g("AWS_SECRET_ACCESS_KEY")
    bucket = g("AWS_S3_BUCKET")
    region = g("AWS_S3_REGION") or "us-east-1"
    missing = [k for k, v in {"AWS_ACCESS_KEY_ID": ak, "AWS_SECRET_ACCESS_KEY": sk, "AWS_S3_BUCKET": bucket}.items() if not v]
    if missing:
        raise RuntimeError(f"Missing S3 credentials: {', '.join(missing)}")
    return ak, sk, bucket, region


def _upload_payload_to_s3(
    turn_id: str,
    agent_id: str,
    thread_id: str,
    session_id: str,
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
    req_record_id: str,
    resp_record_id: str,
) -> tuple:
    """
    Upload the full conversation turn as a JSON file to S3.
    Returns (s3_key, presigned_url).
    """
    captured_at = datetime.utcnow()
    date_str = captured_at.strftime("%Y%m%d")
    folder = f"payloads/{date_str}/{agent_id or 'unknown'}"
    s3_key = f"{folder}/{turn_id}.json"

    payload = {
        "turn_id": turn_id,
        "captured_at": captured_at.isoformat() + "Z",
        "agent_id": agent_id,
        "thread_id": thread_id,
        "session_id": session_id,
        "request_record_id": req_record_id,
        "response_record_id": resp_record_id,
        "request": request_payload,
        "response": response_payload,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    ak, sk, bucket, region = _s3_env()
    _s3_put_bytes(s3_key, body, "application/json", ak, sk, bucket, region)
    url = _s3_presign_get(s3_key, ak, sk, bucket, region, expires=86400)
    return s3_key, url


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_engine():
    """Return a SQLAlchemy engine based on DB_TYPE env var."""
    from sqlalchemy import create_engine

    db_type = os.getenv("DB_TYPE", "postgresql").lower()

    if db_type == "postgresql":
        url = (
            f"postgresql+psycopg2://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
            f"@{os.environ['DB_HOST']}:{os.environ.get('DB_PORT', '5432')}"
            f"/{os.environ['DB_NAME']}"
        )
    elif db_type == "mysql":
        url = (
            f"mysql+pymysql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
            f"@{os.environ['DB_HOST']}:{os.environ.get('DB_PORT', '3306')}"
            f"/{os.environ['DB_NAME']}"
        )
    elif db_type == "sqlite":
        path = os.getenv("DB_SQLITE_PATH", "agent_payloads.db")
        url = f"sqlite:///{path}"
    else:
        raise ValueError(f"Unsupported DB_TYPE: {db_type}")

    return create_engine(url, pool_pre_ping=True)


def _ensure_table(engine) -> None:
    """Create the agent_payloads table if it does not already exist."""
    from sqlalchemy import text

    ddl = """
    CREATE TABLE IF NOT EXISTS agent_payloads (
        id              VARCHAR(36)  PRIMARY KEY,
        agent_id        VARCHAR(255),
        thread_id       VARCHAR(36),
        run_id          VARCHAR(36),
        session_id      VARCHAR(255),
        user_id         VARCHAR(255),
        tenant_id       VARCHAR(255),
        captured_at     TIMESTAMP    NOT NULL,
        direction       VARCHAR(16)  NOT NULL,  -- 'request' | 'response'
        role            VARCHAR(32),
        content_type    VARCHAR(64),
        content_text    TEXT,
        tool_name       VARCHAR(255),
        tool_call_id    VARCHAR(255),
        tool_calls      TEXT,                   -- JSON array
        raw_payload     TEXT         NOT NULL,  -- full JSON
        trace_id        VARCHAR(255),
        status          VARCHAR(64),
        token_input     INTEGER,
        token_output    INTEGER
    )
    """
    with engine.connect() as conn:
        conn.execute(text(ddl))
        conn.commit()


def _insert_payload(engine, record: Dict[str, Any]) -> None:
    """Insert one payload record into agent_payloads."""
    from sqlalchemy import text

    sql = text("""
        INSERT INTO agent_payloads (
            id, agent_id, thread_id, run_id, session_id,
            user_id, tenant_id, captured_at, direction,
            role, content_type, content_text, tool_name,
            tool_call_id, tool_calls, raw_payload,
            trace_id, status, token_input, token_output
        ) VALUES (
            :id, :agent_id, :thread_id, :run_id, :session_id,
            :user_id, :tenant_id, :captured_at, :direction,
            :role, :content_type, :content_text, :tool_name,
            :tool_call_id, :tool_calls, :raw_payload,
            :trace_id, :status, :token_input, :token_output
        )
    """)
    with engine.connect() as conn:
        conn.execute(sql, record)
        conn.commit()


def _insert_mongodb(record: Dict[str, Any]) -> None:
    """Insert into MongoDB (when DB_TYPE=mongodb)."""
    from pymongo import MongoClient

    client = MongoClient(os.environ["MONGODB_URI"])
    db = client[os.getenv("MONGODB_DB", "wxo_payloads")]
    collection = db["agent_payloads"]
    collection.insert_one(record)
    client.close()


# ---------------------------------------------------------------------------
# Core save function
# ---------------------------------------------------------------------------

def save_payload_to_db(
    raw: Dict[str, Any],
    direction: str,
    agent_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    status: Optional[str] = None,
    token_input: Optional[int] = None,
    token_output: Optional[int] = None,
) -> str:
    """
    Persist one agent payload record to the configured external database.

    Parameters
    ----------
    raw        : full payload dict (will be JSON-serialised and stored as-is)
    direction  : 'request' or 'response'
    ...        : optional metadata fields

    Returns
    -------
    The UUID of the inserted record.
    """
    # ---- flatten key fields from the raw payload -------------------------
    role = raw.get("role") or (
        (raw.get("choices") or [{}])[0].get("message", {}).get("role")
        if "choices" in raw
        else None
    )

    # content: handle both string and list-of-blocks formats
    content_block = raw.get("content") or (
        (raw.get("choices") or [{}])[0].get("message", {}).get("content")
        if "choices" in raw
        else None
    )
    if isinstance(content_block, list):
        content_text = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content_block
        )
        content_type = content_block[0].get("type") if content_block else None
    else:
        content_text = str(content_block) if content_block is not None else None
        content_type = "text"

    # tool fields
    add_props = raw.get("additional_properties") or {}
    tool_name = add_props.get("tool_name")
    tool_call_id = add_props.get("tool_call_id")
    tool_calls_raw = add_props.get("tool_calls")
    tool_calls = json.dumps(tool_calls_raw) if tool_calls_raw else None

    # usage / token counts
    usage = raw.get("usage") or {}
    token_input = token_input or usage.get("prompt_tokens") or usage.get("input_tokens")
    token_output = token_output or usage.get("completion_tokens") or usage.get("output_tokens")

    record = {
        "id": str(uuid.uuid4()),
        "agent_id": agent_id or raw.get("agent_id"),
        "thread_id": thread_id or raw.get("thread_id"),
        "run_id": run_id or raw.get("run_id"),
        "session_id": session_id,
        "user_id": user_id,
        "tenant_id": tenant_id or raw.get("tenant_id"),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "direction": direction,
        "role": role,
        "content_type": content_type,
        "content_text": content_text,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "tool_calls": tool_calls,
        "raw_payload": json.dumps(raw, ensure_ascii=False),
        "trace_id": trace_id or raw.get("trace_id"),
        "status": status,
        "token_input": token_input,
        "token_output": token_output,
    }

    db_type = os.getenv("DB_TYPE", "postgresql").lower()
    if db_type == "mongodb":
        _insert_mongodb(record)
    else:
        engine = _get_engine()
        _ensure_table(engine)
        _insert_payload(engine, record)

    return record["id"]


# ---------------------------------------------------------------------------
# ADK Tool definition
# ---------------------------------------------------------------------------

@tool
def capture_agent_payload(
    user_message: str,
    agent_response: str,
    agent_id: str = "",
    thread_id: str = "",
    session_id: str = "",
) -> str:
    """
    Capture and persist the current agent conversation turn to an external DB
    and upload the full payload as a JSON file to AWS S3 for Orchestrate access.

    Call this tool at the end of any agent turn to record the full
    request/response cycle for auditing, analytics, or fine-tuning purposes.

    Args:
        user_message: The raw user message sent to the agent.
        agent_response: The agent's final reply text.
        agent_id: The watsonx Orchestrate agent UUID (optional).
        thread_id: The conversation thread UUID (optional).
        session_id: An application-level session identifier (optional).

    Returns:
        A confirmation string with DB record IDs and the S3 download link.
    """
    resolved_agent_id = agent_id or os.getenv("WXO_AGENT_ID", "")
    turn_id = str(uuid.uuid4())

    request_payload = {
        "role": "user",
        "content": user_message,
        "agent_id": resolved_agent_id,
        "thread_id": thread_id,
        "session_id": session_id,
    }

    response_payload = {
        "role": "assistant",
        "content": agent_response,
        "agent_id": resolved_agent_id,
        "thread_id": thread_id,
        "session_id": session_id,
    }

    req_id = save_payload_to_db(
        raw=request_payload,
        direction="request",
        agent_id=resolved_agent_id,
        thread_id=thread_id,
        session_id=session_id,
    )
    resp_id = save_payload_to_db(
        raw=response_payload,
        direction="response",
        agent_id=resolved_agent_id,
        thread_id=thread_id,
        session_id=session_id,
    )

    # Upload full turn JSON to S3
    s3_result = ""
    try:
        s3_key, download_url = _upload_payload_to_s3(
            turn_id=turn_id,
            agent_id=resolved_agent_id,
            thread_id=thread_id,
            session_id=session_id,
            request_payload=request_payload,
            response_payload=response_payload,
            req_record_id=req_id,
            resp_record_id=resp_id,
        )
        s3_result = (
            f"\nS3 file    : {s3_key}"
            f"\n[⬇ Download payload JSON]({download_url})"
            f"\n_S3 link expires in 24 hours._"
        )
    except Exception as exc:
        s3_result = f"\nS3 upload failed: {exc}"

    return (
        f"Payload captured successfully."
        f"\nturn_id={turn_id}"
        f"\nrequest_record_id={req_id}"
        f"\nresponse_record_id={resp_id}"
        + s3_result
    )
