"""
sync_sessions.py
----------------
Standalone script to fetch user/assistant messages from any watsonx Orchestrate
agent and persist them to the configured database (SQLite, PostgreSQL, MySQL, MongoDB).

Each conversation is identified by a stable key: <agent_id>:<thread_id>
stored in the session_id column so you can filter conversations per agent.

Usage:
  python tools/sync_sessions.py --agent-name "Questions and Requests manager"
  python tools/sync_sessions.py --agent-name "CBE Exchange Agent"
  python tools/sync_sessions.py --agent-id <uuid>
  python tools/sync_sessions.py --agent-name "Incident manager" --no-history

Environment variables (set in .env):
  WXO_API_ENDPOINT / WO_INSTANCE   watsonx Orchestrate instance URL
  WXO_API_KEY / WO_API_KEY         IBM Cloud API key
  DB_TYPE                          sqlite | postgresql | mysql | mongodb
  DB_SQLITE_PATH                   path to SQLite file (default: agent_payloads.db)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from dotenv import load_dotenv
from ibm_watsonx_orchestrate.client.client import Client
from ibm_watsonx_orchestrate.client.credentials import Credentials
from ibm_watsonx_orchestrate_clients.agents.agent_client import AgentClient
from ibm_watsonx_orchestrate_clients.threads.threads_client import ThreadsClient
from sqlalchemy import create_engine, text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
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
                parts.append(item.get("text") or item.get("content") or json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()
    if content is None:
        return ""
    return str(content)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _get_engine():
    db_type = os.getenv("DB_TYPE", "sqlite").lower()
    if db_type == "postgresql":
        url = (
            f"postgresql+psycopg2://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
            f"@{os.environ['DB_HOST']}:{os.environ.get('DB_PORT', '5432')}/{os.environ['DB_NAME']}"
        )
    elif db_type == "mysql":
        url = (
            f"mysql+pymysql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
            f"@{os.environ['DB_HOST']}:{os.environ.get('DB_PORT', '3306')}/{os.environ['DB_NAME']}"
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


def _get_existing_message_ids(engine, agent_id: str) -> Set[str]:
    sql = text("""
        SELECT tool_call_id FROM agent_payloads
        WHERE direction = 'thread_message'
          AND agent_id = :agent_id
          AND tool_call_id IS NOT NULL
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"agent_id": agent_id}).fetchall()
    return {str(row[0]) for row in rows if row[0]}


def _insert_message(engine, record: Dict[str, Any]) -> None:
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


# ---------------------------------------------------------------------------
# Orchestrate API
# ---------------------------------------------------------------------------

def _build_clients(base_url: str, api_key: str):
    creds = Credentials(url=base_url, api_key=api_key)
    client = Client(creds)
    threads_client = ThreadsClient(base_url=base_url, api_key=client.token)
    agent_client = AgentClient(base_url=base_url, api_key=client.token)
    return threads_client, agent_client


def _resolve_agent(agent_client: AgentClient, agent_id: Optional[str], agent_name: Optional[str]) -> Dict[str, str]:
    if agent_id:
        agents = _normalize_rows(agent_client.get())
        for a in agents:
            if str(a.get("id") or "") == agent_id:
                return {
                    "id": agent_id,
                    "name": str(a.get("name") or ""),
                    "display_name": str(a.get("display_name") or a.get("displayName") or ""),
                }
        return {"id": agent_id, "name": "", "display_name": ""}

    if not agent_name:
        raise ValueError("Provide --agent-id or --agent-name.")

    wanted = agent_name.strip().lower()
    agents = _normalize_rows(agent_client.get())
    exact, partial = [], []
    for a in agents:
        for key in ("name", "display_name", "displayName"):
            val = str(a.get(key) or "").strip().lower()
            if not val:
                continue
            if val == wanted:
                exact.append(a)
                break
            if wanted in val:
                partial.append(a)
                break

    pool = exact if exact else partial
    if len(pool) == 1:
        a = pool[0]
        return {
            "id": str(a.get("id") or ""),
            "name": str(a.get("name") or ""),
            "display_name": str(a.get("display_name") or a.get("displayName") or ""),
        }
    if len(pool) > 1:
        raise ValueError(f"Name '{agent_name}' matched multiple agents. Use --agent-id.")
    raise ValueError(f"Agent '{agent_name}' not found.")


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def sync(
    threads_client: ThreadsClient,
    target: Dict[str, str],
    engine,
    include_history: bool,
) -> Dict[str, Any]:
    target_id = target["id"]
    existing_ids = _get_existing_message_ids(engine, target_id)
    threads = _normalize_rows(threads_client.get_all_threads(agent_id=target_id))
    log.info("Found %d threads for agent '%s'", len(threads), target.get("display_name") or target.get("name"))

    conversations: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "thread_id": "",
        "conversation_key": "",
        "agent_name": target.get("display_name") or target.get("name"),
        "user_messages": 0,
        "assistant_messages": 0,
        "first_timestamp": None,
        "last_timestamp": None,
    })
    inserted = 0
    skipped = 0

    for thread in threads:
        thread_id = str(thread.get("id") or thread.get("thread_id") or "")
        if not thread_id:
            continue

        conversation_key = f"{target_id}:{thread_id}"
        messages = _normalize_rows(threads_client.get_thread_messages(thread_id))

        if not include_history and messages:
            messages = messages[-1:]  # only the most recent message per thread

        for msg in messages:
            msg_id = str(msg.get("id") or "")
            role = str(msg.get("role") or "")
            if role not in {"user", "assistant"} or not msg_id:
                continue
            if msg_id in existing_ids:
                skipped += 1
                continue

            ts = msg.get("updated_at") or msg.get("created_at") or msg.get("created_on")
            content_text = _extract_text(msg.get("content"))

            raw = {
                "message_id": msg_id,
                "thread_id": thread_id,
                "conversation_key": conversation_key,
                "agent_id": target_id,
                "agent_name": target.get("display_name") or target.get("name"),
                "role": role,
                "content": msg.get("content"),
                "content_text": content_text,
                "timestamp": ts,
            }

            record = {
                "id": str(uuid.uuid4()),
                "agent_id": target_id,
                "thread_id": thread_id,
                "run_id": str(msg.get("run_id") or "") or None,
                "session_id": conversation_key,
                "user_id": None,
                "tenant_id": None,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "direction": "thread_message",
                "role": role,
                "content_type": "text",
                "content_text": content_text,
                "tool_name": "sync_sessions",
                "tool_call_id": msg_id,
                "tool_calls": None,
                "raw_payload": json.dumps(raw, ensure_ascii=False),
                "trace_id": None,
                "status": "synced",
                "token_input": None,
                "token_output": None,
            }
            _insert_message(engine, record)
            inserted += 1

            summary = conversations[conversation_key]
            summary["thread_id"] = thread_id
            summary["conversation_key"] = conversation_key
            summary["user_messages" if role == "user" else "assistant_messages"] += 1
            if ts:
                if not summary["first_timestamp"] or str(ts) < str(summary["first_timestamp"]):
                    summary["first_timestamp"] = ts
                if not summary["last_timestamp"] or str(ts) > str(summary["last_timestamp"]):
                    summary["last_timestamp"] = ts

            existing_ids.add(msg_id)

        if inserted > 0 and inserted % 50 == 0:
            log.info("  ... %d messages inserted so far", inserted)

    return {
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv(dotenv_path=Path(".env"))

    parser = argparse.ArgumentParser(description="Sync agent session messages to DB")
    parser.add_argument("--agent-id", default=_get_env("WXO_AGENT_ID"))
    parser.add_argument("--agent-name", default=_get_env("WXO_AGENT_NAME"))
    parser.add_argument("--no-history", action="store_true",
                        help="Only sync the last message per thread instead of full history")
    args = parser.parse_args()

    if not args.agent_id and not args.agent_name:
        parser.error("Provide --agent-id or --agent-name")

    base_url = _get_env("WXO_API_ENDPOINT", "WO_INSTANCE")
    api_key = _get_env("WXO_API_KEY", "WO_API_KEY")
    if not base_url or not api_key:
        sys.exit("ERROR: Set WXO_API_ENDPOINT (or WO_INSTANCE) and WXO_API_KEY (or WO_API_KEY) in .env")

    threads_client, agent_client = _build_clients(base_url=base_url, api_key=api_key)
    target = _resolve_agent(agent_client, args.agent_id, args.agent_name)
    log.info("Target agent: %s  (id=%s)", target.get("display_name") or target.get("name"), target["id"])

    engine = _get_engine()
    _ensure_table(engine)

    result = sync(
        threads_client=threads_client,
        target=target,
        engine=engine,
        include_history=not args.no_history,
    )

    print("\n" + "=" * 60)
    print(f"Agent:             {result['target_agent']['display_name'] or result['target_agent']['name']}")
    print(f"Threads seen:      {result['threads_seen']}")
    print(f"Messages inserted: {result['messages_inserted']}")
    print(f"Messages skipped:  {result['messages_skipped_existing']}  (already in DB)")
    print(f"Conversations:     {len(result['conversations'])}")
    print("=" * 60)
    for conv in result["conversations"]:
        u = conv["user_messages"]
        a = conv["assistant_messages"]
        key = conv["conversation_key"]
        print(f"  {key}  |  user={u}  assistant={a}  |  {conv['first_timestamp']} → {conv['last_timestamp']}")
    print()


if __name__ == "__main__":
    main()
