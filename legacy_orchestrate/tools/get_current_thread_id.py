"""
get_current_thread_id.py
-------------------------
A watsonx Orchestrate ADK tool that automatically resolves the current
(most recently active) thread ID for this agent without the user needing
to provide it manually.

How it works:
  1. Reads WXO_AGENT_ID from the environment (injected by WXO at runtime).
  2. Calls the WXO Threads API to list all threads for this agent.
  3. Returns the thread with the most recent activity (latest updated_at /
     created_at timestamp) — i.e. the conversation currently in progress.

Typical usage:
  User: "get the current thread id"
  Agent calls: get_current_thread_id()
  → returns the thread ID, which can then be passed to capture_thread_payload
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from ibm_watsonx_orchestrate.agent_builder.tools import tool


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


def _parse_ts(value: Any) -> str:
    """Normalise a timestamp to an ISO string for sorting; empty string if absent."""
    if not value:
        return ""
    return str(value)


# ---------------------------------------------------------------------------
# ADK Tool
# ---------------------------------------------------------------------------

@tool
def get_current_thread_id(agent_id: str = "") -> str:
    """
    Automatically find and return the current (most recently active) thread ID
    for this agent.

    The tool queries the watsonx Orchestrate Threads API and returns the thread
    that was updated most recently — which is the ongoing conversation.

    Use the returned thread_id with capture_thread_payload to save the full
    conversation to S3.

    Args:
        agent_id: The watsonx Orchestrate agent UUID. Defaults to the
                  WXO_AGENT_ID environment variable (set automatically at runtime).

    Returns:
        A JSON string with the current thread_id, agent_id, last activity
        timestamp, message count, and a list of the most recent threads found.
    """
    load_dotenv(dotenv_path=Path(".env"))

    resolved_agent_id = (
        (agent_id or "").strip()
        or _get_env("WXO_AGENT_ID") or ""
        or "17d0ba4c-7591-43ab-b85b-c5c272222cf0"
    )

    base_url = (
        _get_env("WXO_API_ENDPOINT", "WO_INSTANCE")
        or "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/16cd2449-1e4f-4fc9-a4d8-9d69c3864a61"
    )
    api_key = (
        _get_env("WXO_API_KEY", "WO_API_KEY")
    )

    try:
        from ibm_watsonx_orchestrate.client.client import Client
        from ibm_watsonx_orchestrate.client.credentials import Credentials
        from ibm_watsonx_orchestrate_clients.threads.threads_client import ThreadsClient

        creds = Credentials(url=base_url, api_key=api_key)
        client = Client(creds)
        threads_client = ThreadsClient(base_url=base_url, api_key=client.token)

        raw_threads = _normalize_rows(threads_client.get_all_threads(agent_id=resolved_agent_id))
    except Exception as exc:
        return f"Failed to fetch threads: {exc}"

    if not raw_threads:
        return json.dumps({
            "status": "no_threads",
            "agent_id": resolved_agent_id,
            "message": "No threads found for this agent.",
        }, indent=2)

    # Sort threads by most recent activity (updated_at → created_at → id fallback)
    def _sort_key(t: Dict[str, Any]) -> str:
        return (
            _parse_ts(t.get("updated_at"))
            or _parse_ts(t.get("created_at"))
            or _parse_ts(t.get("created_on"))
            or str(t.get("id") or "")
        )

    sorted_threads = sorted(raw_threads, key=_sort_key, reverse=True)
    current = sorted_threads[0]

    current_thread_id = str(current.get("id") or current.get("thread_id") or "")
    last_activity = (
        current.get("updated_at")
        or current.get("created_at")
        or current.get("created_on")
    )

    # Build a short summary of the top 5 most recent threads for context
    recent_list = []
    for t in sorted_threads[:5]:
        tid = str(t.get("id") or t.get("thread_id") or "")
        ts  = t.get("updated_at") or t.get("created_at") or t.get("created_on")
        recent_list.append({
            "thread_id": tid,
            "last_activity": str(ts) if ts else None,
            "is_current": tid == current_thread_id,
        })

    result = {
        "status": "ok",
        "current_thread_id": current_thread_id,
        "agent_id": resolved_agent_id,
        "last_activity": str(last_activity) if last_activity else None,
        "total_threads_found": len(raw_threads),
        "recent_threads": recent_list,
        "hint": (
            f"Pass current_thread_id='{current_thread_id}' to capture_thread_payload "
            "to save the full conversation to S3."
        ),
    }
    return json.dumps(result, indent=2)
