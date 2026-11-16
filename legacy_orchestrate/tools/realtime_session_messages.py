"""
Real-time chat session message listener for watsonx Orchestrate.

This script continuously polls thread messages for a target agent,
prints newly seen messages to stdout, and optionally writes them to
an append-only JSONL file.

Examples:
  python tools/realtime_session_messages.py --agent-id <uuid>
  python tools/realtime_session_messages.py --agent-name "Incident manager" --interval 2
  python tools/realtime_session_messages.py --agent-id <uuid> --roles all --out messages.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from dotenv import load_dotenv
from ibm_watsonx_orchestrate.client.client import Client
from ibm_watsonx_orchestrate.client.credentials import Credentials
from ibm_watsonx_orchestrate_clients.agents.agent_client import AgentClient
from ibm_watsonx_orchestrate_clients.common.base_client import ClientAPIException
from ibm_watsonx_orchestrate_clients.threads.threads_client import ThreadsClient


def get_env(*keys: str) -> Optional[str]:
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real-time listener for Orchestrate thread messages")
    parser.add_argument("--agent-id", default=get_env("WXO_AGENT_ID"), help="Agent UUID")
    parser.add_argument("--agent-name", default=get_env("WXO_AGENT_NAME"), help="Agent name/display name")
    parser.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds")
    parser.add_argument("--roles", choices=["user", "assistant", "all"], default="user", help="Which roles to stream")
    parser.add_argument("--out", default="session_messages.jsonl", help="Append-only JSONL output file")
    parser.add_argument(
        "--history",
        action="store_true",
        help="Emit existing history on startup before switching to real-time mode",
    )
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit")
    return parser.parse_args()


def extract_text(content: Any) -> str:
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


def normalize_rows(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    if isinstance(data, list):
        return data
    return []


def build_clients(base_url: str, api_key: str) -> tuple[Client, ThreadsClient, AgentClient]:
    creds = Credentials(url=base_url, api_key=api_key)
    client = Client(creds)
    threads_client = ThreadsClient(base_url=base_url, api_key=client.token)
    agent_client = AgentClient(base_url=base_url, api_key=client.token)
    return client, threads_client, agent_client


def is_auth_error(exc: Exception) -> bool:
    if isinstance(exc, ClientAPIException):
        if getattr(exc, "status_code", None) == 401:
            return True
    message = str(exc).lower()
    return "status_code=401" in message or "401" in message and "expired" in message


def resolve_agent_id(agent_client: AgentClient, agent_id: Optional[str], agent_name: Optional[str]) -> str:
    if agent_id:
        return agent_id
    if not agent_name:
        raise ValueError("Provide --agent-id or --agent-name (or set WXO_AGENT_ID/WXO_AGENT_NAME).")

    agents = normalize_rows(agent_client.get())
    wanted = agent_name.strip().lower()

    exact: List[Dict[str, Any]] = []
    partial: List[Dict[str, Any]] = []
    for agent in agents:
        for key in ("display_name", "displayName", "name"):
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
        return str(exact[0]["id"])
    if len(exact) > 1:
        raise ValueError(f"Agent name '{agent_name}' matched multiple agents. Use --agent-id.")
    if len(partial) == 1:
        return str(partial[0]["id"])
    if len(partial) > 1:
        raise ValueError(f"Agent name '{agent_name}' matched multiple agents. Use --agent-id.")
    raise ValueError(f"Agent named '{agent_name}' was not found.")


def collect_messages(
    threads_client: ThreadsClient,
    agent_id: str,
    seen_ids: Set[str],
    roles: str,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    threads = normalize_rows(threads_client.get_all_threads(agent_id=agent_id))

    for thread in threads:
        thread_id = thread.get("id") or thread.get("thread_id")
        if not thread_id:
            continue
        messages = normalize_rows(threads_client.get_thread_messages(thread_id))

        for msg in messages:
            msg_id = str(msg.get("id") or "")
            role = str(msg.get("role") or "")
            if not msg_id or msg_id in seen_ids:
                continue

            seen_ids.add(msg_id)

            if roles != "all" and role != roles:
                continue

            ts = msg.get("updated_at") or msg.get("created_on") or msg.get("created_at")
            events.append(
                {
                    "message_id": msg_id,
                    "thread_id": str(thread_id),
                    "role": role,
                    "timestamp": ts,
                    "text": extract_text(msg.get("content")),
                }
            )

    events.sort(key=lambda x: x.get("timestamp") or "")
    return events


def emit_events(events: Iterable[Dict[str, Any]], out_file: Path) -> None:
    if not events:
        return

    out_file.parent.mkdir(parents=True, exist_ok=True)
    with out_file.open("a", encoding="utf-8") as f:
        for event in events:
            line = json.dumps(event, ensure_ascii=False)
            f.write(line + "\n")
            print(line)


def main() -> None:
    load_dotenv(dotenv_path=Path(".env"))
    args = parse_args()

    base_url = get_env("WXO_API_ENDPOINT", "WO_INSTANCE")
    api_key = get_env("WXO_API_KEY", "WO_API_KEY")
    if not base_url or not api_key:
        raise EnvironmentError("Missing credentials. Set WXO_API_ENDPOINT/WO_INSTANCE and WXO_API_KEY/WO_API_KEY.")

    _, threads_client, agent_client = build_clients(base_url=base_url, api_key=api_key)
    resolved_agent_id = resolve_agent_id(agent_client, args.agent_id, args.agent_name)

    print(json.dumps({
        "event": "listener_started",
        "agent_id": resolved_agent_id,
        "roles": args.roles,
        "interval_sec": args.interval,
        "history": bool(args.history),
        "time": datetime.utcnow().isoformat() + "Z",
    }))

    seen_ids: Set[str] = set()
    out_path = Path(args.out)

    if not args.history:
        # Prime seen set so only newly arriving messages are emitted.
        try:
            _ = collect_messages(threads_client, resolved_agent_id, seen_ids, roles="all")
        except Exception as exc:
            if is_auth_error(exc):
                print(json.dumps({
                    "event": "auth_refresh",
                    "reason": "token_expired_on_startup",
                    "time": datetime.utcnow().isoformat() + "Z",
                }))
                _, threads_client, agent_client = build_clients(base_url=base_url, api_key=api_key)
                resolved_agent_id = resolve_agent_id(agent_client, args.agent_id, args.agent_name)
                _ = collect_messages(threads_client, resolved_agent_id, seen_ids, roles="all")
            else:
                raise

    try:
        while True:
            try:
                events = collect_messages(threads_client, resolved_agent_id, seen_ids, roles=args.roles)
            except Exception as exc:
                if not is_auth_error(exc):
                    raise

                print(json.dumps({
                    "event": "auth_refresh",
                    "reason": "token_expired",
                    "time": datetime.utcnow().isoformat() + "Z",
                }))

                _, threads_client, agent_client = build_clients(base_url=base_url, api_key=api_key)
                resolved_agent_id = resolve_agent_id(agent_client, args.agent_id, args.agent_name)
                events = collect_messages(threads_client, resolved_agent_id, seen_ids, roles=args.roles)

            emit_events(events, out_path)

            if args.once:
                break

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(json.dumps({
            "event": "listener_stopped",
            "reason": "keyboard_interrupt",
            "time": datetime.utcnow().isoformat() + "Z",
        }))


if __name__ == "__main__":
    main()
