from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from ibm_watsonx_orchestrate.agent_builder.tools import tool
from ibm_watsonx_orchestrate.client.client import Client
from ibm_watsonx_orchestrate.client.credentials import Credentials
from ibm_watsonx_orchestrate_clients.agents.agent_client import AgentClient
from ibm_watsonx_orchestrate_clients.threads.threads_client import ThreadsClient
from sqlalchemy import text


# ---------------------------------------------------------------------------
# Inlined DB helpers (no sibling-module import — each tool runs in isolation)
# ---------------------------------------------------------------------------

def _get_engine():
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
        direction       VARCHAR(16)  NOT NULL,
        role            VARCHAR(32),
        content_type    VARCHAR(64),
        content_text    TEXT,
        tool_name       VARCHAR(255),
        tool_call_id    VARCHAR(255),
        tool_calls      TEXT,
        raw_payload     TEXT         NOT NULL,
        trace_id        VARCHAR(255),
        status          VARCHAR(64),
        token_input     INTEGER,
        token_output    INTEGER
    )
    """
    with engine.connect() as conn:
        conn.execute(text(ddl))
        conn.commit()


def _save_message_to_db(
    raw: Dict[str, Any],
    agent_id: str,
    thread_id: str,
    session_id: str,
    run_id: Optional[str],
    role: str,
    content_text: str,
    message_id: str,
    status: str = "synced",
) -> None:
    db_type = os.getenv("DB_TYPE", "postgresql").lower()
    record: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "agent_id": agent_id,
        "thread_id": thread_id,
        "run_id": run_id,
        "session_id": session_id,
        "user_id": None,
        "tenant_id": None,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "direction": "thread_message",
        "role": role,
        "content_type": "text",
        "content_text": content_text,
        "tool_name": "sync_agent_session_messages_to_db",
        "tool_call_id": message_id,
        "tool_calls": None,
        "raw_payload": json.dumps(raw, ensure_ascii=False),
        "trace_id": None,
        "status": status,
        "token_input": None,
        "token_output": None,
    }
    if db_type == "mongodb":
        from pymongo import MongoClient
        client = MongoClient(os.environ["MONGODB_URI"])
        db_conn = client[os.getenv("MONGODB_DB", "wxo_payloads")]
        db_conn["agent_payloads"].insert_one(record)
        client.close()
    else:
        engine = _get_engine()
        _ensure_table(engine)
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
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()
    if content is None:
        return ""
    return str(content)


def _build_clients(base_url: str, api_key: str) -> Tuple[ThreadsClient, AgentClient]:
    creds = Credentials(url=base_url, api_key=api_key)
    client = Client(creds)
    threads_client = ThreadsClient(base_url=base_url, api_key=client.token)
    agent_client = AgentClient(base_url=base_url, api_key=client.token)
    return threads_client, agent_client


def _resolve_target_agent(
    agent_client: AgentClient,
    target_agent_id: str,
    target_agent_name: str,
) -> Dict[str, str]:
    if target_agent_id:
        agents = _normalize_rows(agent_client.get())
        for agent in agents:
            if str(agent.get("id") or "") == target_agent_id:
                return {
                    "id": target_agent_id,
                    "name": str(agent.get("name") or ""),
                    "display_name": str(agent.get("display_name") or agent.get("displayName") or ""),
                }
        return {"id": target_agent_id, "name": "", "display_name": ""}

    if not target_agent_name:
        raise ValueError("Provide target_agent_name or target_agent_id.")

    wanted = target_agent_name.strip().lower()
    agents = _normalize_rows(agent_client.get())
    exact: List[Dict[str, Any]] = []
    partial: List[Dict[str, Any]] = []

    for agent in agents:
        for key in ("name", "display_name", "displayName"):
            value = str(agent.get(key) or "").strip().lower()
            if not value:
                continue
            if value == wanted:
                exact.append(agent)
                break
            if wanted in value:
                partial.append(agent)
                break

    if len(exact) == 1:
        agent = exact[0]
    elif len(partial) == 1:
        agent = partial[0]
    elif len(exact) > 1 or len(partial) > 1:
        raise ValueError("Target agent name matched multiple agents. Provide target_agent_id instead.")
    else:
        raise ValueError(f"Target agent '{target_agent_name}' not found.")

    return {
        "id": str(agent.get("id") or ""),
        "name": str(agent.get("name") or ""),
        "display_name": str(agent.get("display_name") or agent.get("displayName") or ""),
    }


def _conversation_key(agent_id: str, thread_id: str) -> str:
    return f"{agent_id}:{thread_id}"


def _get_existing_message_ids(agent_id: str) -> Set[str]:
    engine = _get_engine()
    sql = text(
        """
        SELECT tool_call_id
        FROM agent_payloads
        WHERE direction = 'thread_message'
          AND agent_id = :agent_id
          AND tool_call_id IS NOT NULL
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"agent_id": agent_id}).fetchall()
    return {str(row[0]) for row in rows if row[0]}


@tool
def sync_agent_session_messages_to_db(
    target_agent_name: str = "",
    target_agent_id: str = "",
    include_existing_history: bool = True,
) -> str:
    """
    Fetch user/assistant messages for a target agent and save them in DB.

    The tool stores each message in agent_payloads with:
    - direction = thread_message
    - agent_id = target agent id
    - thread_id = original thread id
    - session_id = "<agent_id>:<thread_id>" as the conversation key
    - tool_call_id = source message id (used to avoid duplicate inserts)

    Args:
        target_agent_name: Name/display name of the agent whose sessions should be synced.
        target_agent_id: UUID of the target agent. If set, this takes precedence.
        include_existing_history: When true, sync all available thread history for the target agent.

    Returns:
        JSON string with sync summary and conversation grouping.
    """
    load_dotenv(dotenv_path=Path(".env"))

    base_url = _get_env("WXO_API_ENDPOINT", "WO_INSTANCE")
    api_key = _get_env("WXO_API_KEY", "WO_API_KEY")
    if not base_url or not api_key:
        raise EnvironmentError("Missing credentials. Set WXO_API_ENDPOINT/WO_INSTANCE and WXO_API_KEY/WO_API_KEY.")

    threads_client, agent_client = _build_clients(base_url=base_url, api_key=api_key)
    target = _resolve_target_agent(
        agent_client=agent_client,
        target_agent_id=target_agent_id.strip(),
        target_agent_name=target_agent_name.strip(),
    )

    target_id = target["id"]
    if not target_id:
        raise ValueError("Unable to resolve target agent id.")

    existing_message_ids = _get_existing_message_ids(target_id)
    threads = _normalize_rows(threads_client.get_all_threads(agent_id=target_id))

    conversations: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "thread_id": "",
            "conversation_key": "",
            "user_messages": 0,
            "assistant_messages": 0,
            "first_timestamp": None,
            "last_timestamp": None,
        }
    )
    inserted = 0
    skipped = 0

    for thread in threads:
        thread_id = str(thread.get("id") or thread.get("thread_id") or "")
        if not thread_id:
            continue

        conversation_key = _conversation_key(target_id, thread_id)
        messages = _normalize_rows(threads_client.get_thread_messages(thread_id))
        if not include_existing_history and messages:
            messages = [messages[-1]]

        for message in messages:
            message_id = str(message.get("id") or "")
            role = str(message.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            if not message_id:
                continue
            if message_id in existing_message_ids:
                skipped += 1
                continue

            timestamp = (
                message.get("updated_at")
                or message.get("created_at")
                or message.get("created_on")
            )

            raw_payload = {
                "message_id": message_id,
                "thread_id": thread_id,
                "conversation_key": conversation_key,
                "agent_id": target_id,
                "agent_name": target.get("name") or target.get("display_name"),
                "role": role,
                "content": message.get("content"),
                "content_text": _extract_text(message.get("content")),
                "timestamp": timestamp,
                "additional_properties": {
                    "tool_name": "sync_agent_session_messages_to_db",
                    "tool_call_id": message_id,
                    "conversation_key": conversation_key,
                    "source": "threads_api",
                },
            }

            _save_message_to_db(
                raw=raw_payload,
                agent_id=target_id,
                thread_id=thread_id,
                session_id=conversation_key,
                run_id=str(message.get("run_id") or "") or None,
                role=role,
                content_text=_extract_text(message.get("content")),
                message_id=message_id,
                status="synced",
            )
            inserted += 1

            summary = conversations[conversation_key]
            summary["thread_id"] = thread_id
            summary["conversation_key"] = conversation_key
            if role == "user":
                summary["user_messages"] += 1
            else:
                summary["assistant_messages"] += 1

            if timestamp:
                if summary["first_timestamp"] is None or str(timestamp) < str(summary["first_timestamp"]):
                    summary["first_timestamp"] = timestamp
                if summary["last_timestamp"] is None or str(timestamp) > str(summary["last_timestamp"]):
                    summary["last_timestamp"] = timestamp

            existing_message_ids.add(message_id)

    result = {
        "status": "ok",
        "target_agent": {
            "id": target_id,
            "name": target.get("name"),
            "display_name": target.get("display_name"),
        },
        "threads_seen": len(threads),
        "messages_inserted": inserted,
        "messages_skipped_existing": skipped,
        "conversations": sorted(conversations.values(), key=lambda c: c.get("conversation_key") or ""),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)