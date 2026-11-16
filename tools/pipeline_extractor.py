"""
pipeline_extractor.py
---------------------
Stand-alone script that:

  1. Authenticates to watsonx Orchestrate using the REST API.
  2. Polls ALL assistant runs for a given agent.
  3. For each run, fetches its events (messages, tool calls, steps).
  4. Optionally pulls Trace/Span data via the Observability SDK.
  5. Saves every record to an external database via save_payload_to_db().

Run as a cron job, a Cloud Functions action, or an event-driven consumer.

Usage
-----
    python pipeline_extractor.py [--once] [--agent-id <uuid>] [--agent-name "Incident manager"] [--interval 60]

Environment variables (set in .env or shell):
    WXO_API_ENDPOINT   e.g. https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/<instance-id>
  WXO_API_KEY        IBM Cloud IAM API key  -OR-
  WXO_USERNAME / WXO_PASSWORD  (for Developer Edition)
  WXO_AGENT_ID       target agent UUID (can also pass on CLI)
    WXO_AGENT_NAME     target agent display/name (optional, can also pass on CLI)

    Backward-compatible aliases (already used in some env files):
    WO_INSTANCE        alias of WXO_API_ENDPOINT
    WO_API_KEY         alias of WXO_API_KEY

  DB_TYPE            postgresql | mysql | sqlite | mongodb  (default: postgresql)
  DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME      (SQL targets)
  MONGODB_URI / MONGODB_DB                                  (MongoDB target)
  DB_SQLITE_PATH                                            (SQLite target)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

import requests

# Import the persistence helper from the ADK tool module
sys.path.insert(0, os.path.dirname(__file__))
from payload_extractor_tool import save_payload_to_db  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class WxOClient:
    """Thin REST client for the watsonx Orchestrate Server API."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def list_runs(self, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        """GET /v1/orchestrate/runs"""
        r = self._session.get(
            f"{self.base_url}/v1/orchestrate/runs",
            params={"limit": limit, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def get_run(self, run_id: str) -> Dict[str, Any]:
        """GET /v1/orchestrate/runs/{run_id}"""
        r = self._session.get(
            f"{self.base_url}/v1/orchestrate/runs/{run_id}",
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def get_run_events(self, run_id: str) -> List[Dict[str, Any]]:
        """GET /v1/orchestrate/runs/{run_id}/events"""
        r = self._session.get(
            f"{self.base_url}/v1/orchestrate/runs/{run_id}/events",
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Message Threads
    # ------------------------------------------------------------------

    def list_threads(self) -> List[Dict[str, Any]]:
        """GET /v1/orchestrate/threads"""
        r = self._session.get(f"{self.base_url}/v1/orchestrate/threads", timeout=30)
        r.raise_for_status()
        return r.json()

    def list_messages(self, thread_id: str) -> List[Dict[str, Any]]:
        """GET /v1/orchestrate/threads/{thread_id}/messages"""
        r = self._session.get(
            f"{self.base_url}/v1/orchestrate/threads/{thread_id}/messages",
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    def list_agents(self) -> List[Dict[str, Any]]:
        """GET list registered agents using v2 endpoint, with v1 fallback."""
        # Preferred endpoint used by ADK clients
        v1_url = f"{self.base_url}/v1/orchestrate/agents"
        r = self._session.get(v1_url, params={"include_hidden": "true", "include": "global"}, timeout=30)
        if r.status_code == 200:
            payload = r.json()
            if isinstance(payload, dict):
                return payload.get("data", [])
            if isinstance(payload, list):
                return payload

        # Fallback to v2 shape for some deployments
        v2_url = f"{self.base_url}/v2/orchestrate/agents"
        r = self._session.get(v2_url, params={"limit": 100, "include_hidden": "true"}, timeout=30)
        r.raise_for_status()
        payload = r.json()
        if isinstance(payload, dict):
            return payload.get("data", [])
        if isinstance(payload, list):
            return payload
        return []


# ---------------------------------------------------------------------------
# IAM token helper
# ---------------------------------------------------------------------------

def get_iam_token(api_key: str) -> str:
    """Exchange an IBM Cloud API key for a short-lived IAM bearer token."""
    resp = requests.post(
        "https://iam.cloud.ibm.com/identity/token",
        data={
            "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
            "apikey": api_key,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_wxo_access_token(base_url: str, api_key: str) -> str:
    """
    Get a watsonx Orchestrate access token using ADK's auth flow.
    Falls back to direct IAM token exchange if ADK auth is unavailable.
    """
    try:
        from ibm_watsonx_orchestrate import Client, Credentials

        credentials = Credentials(url=base_url, api_key=api_key)
        client = Client(credentials)
        if getattr(client, "token", None):
            return client.token
    except Exception:
        pass

    return get_iam_token(api_key)


def get_dev_edition_token(base_url: str, username: str, password: str) -> str:
    """Authenticate to the Developer Edition local server."""
    resp = requests.post(
        f"{base_url}/api/v1/auth/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def get_env(*keys: str) -> Optional[str]:
    """Return the first non-empty environment variable among keys."""
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    return None


# ---------------------------------------------------------------------------
# Extraction logic
# ---------------------------------------------------------------------------

def iter_all_runs(client: WxOClient) -> Iterator[Dict[str, Any]]:
    """Yield every run record, handling pagination automatically."""
    offset = 0
    limit = 100
    while True:
        page = client.list_runs(limit=limit, offset=offset)
        runs = page.get("data", [])
        if not runs:
            break
        yield from runs
        offset += len(runs)
        if offset >= page.get("total", 0):
            break


def resolve_agent_id(client: WxOClient, agent_name: Optional[str], agent_id: Optional[str]) -> Optional[str]:
    """Resolve the target agent UUID using either explicit ID or agent name."""
    if agent_id:
        return agent_id
    if not agent_name:
        return None

    agents = client.list_agents()
    normalized = agent_name.strip().lower()

    exact_matches: List[Dict[str, Any]] = []
    partial_matches: List[Dict[str, Any]] = []
    for agent in agents:
        candidate_names = [
            str(agent.get("name") or ""),
            str(agent.get("display_name") or ""),
            str(agent.get("displayName") or ""),
        ]
        for candidate in candidate_names:
            lowered = candidate.strip().lower()
            if not lowered:
                continue
            if lowered == normalized:
                exact_matches.append(agent)
                break
            if normalized in lowered:
                partial_matches.append(agent)
                break

    if len(exact_matches) == 1:
        resolved = exact_matches[0].get("id")
        log.info("Resolved agent '%s' to id=%s", agent_name, resolved)
        return resolved

    if len(exact_matches) > 1:
        ids = [str(a.get("id")) for a in exact_matches]
        raise ValueError(
            f"Agent name '{agent_name}' matched multiple agents exactly: {ids}. Use --agent-id instead."
        )

    if len(partial_matches) == 1:
        resolved = partial_matches[0].get("id")
        log.info("Resolved agent '%s' to id=%s (partial name match)", agent_name, resolved)
        return resolved

    if len(partial_matches) > 1:
        ids = [str(a.get("id")) for a in partial_matches]
        raise ValueError(
            f"Agent name '{agent_name}' matched multiple agents partially: {ids}. Use --agent-id instead."
        )

    raise ValueError(f"Agent named '{agent_name}' not found in this instance.")


def process_run(client: WxOClient, run: Dict[str, Any], agent_id: Optional[str]) -> int:
    """
    Extract and persist a single run and all of its events.
    Returns the number of records saved.
    """
    run_id = run["id"]
    thread_id = run.get("thread_id")
    tenant_id = run.get("tenant_id")
    trace_id = run.get("trace_id")
    status = run.get("status")

    usage = run.get("usage") or {}
    token_input = usage.get("prompt_tokens") or usage.get("input_tokens")
    token_output = usage.get("completion_tokens") or usage.get("output_tokens")

    saved = 0

    # --- Persist the run itself ----------------------------------------
    save_payload_to_db(
        raw=run,
        direction="run",
        agent_id=agent_id,
        thread_id=thread_id,
        run_id=run_id,
        tenant_id=tenant_id,
        trace_id=trace_id,
        status=status,
        token_input=token_input,
        token_output=token_output,
    )
    saved += 1
    log.info("  ✓ Run %s saved (status=%s)", run_id, status)

    # --- Fetch and persist run events -----------------------------------
    try:
        events: List[Dict[str, Any]] = client.get_run_events(run_id)
    except requests.HTTPError as exc:
        log.warning("  Could not fetch events for run %s: %s", run_id, exc)
        events = []

    for event in events:
        event_type = event.get("event", "unknown")
        data = event.get("data", {})
        direction = "event"

        # Map well-known event types to cleaner direction labels
        if event_type in ("message.created", "message.completed"):
            direction = "response" if data.get("role") == "assistant" else "request"
        elif event_type in ("run.step.started", "run.step.completed"):
            direction = "step"
        elif event_type == "run.started":
            direction = "request"
        elif event_type in ("run.completed", "run.failed"):
            direction = "response"

        data["_event_type"] = event_type
        data["_event_id"] = event.get("id")

        save_payload_to_db(
            raw=data,
            direction=direction,
            agent_id=agent_id,
            thread_id=thread_id,
            run_id=run_id,
            tenant_id=tenant_id,
            trace_id=trace_id,
            status=event_type,
        )
        saved += 1

    log.info("  ✓ %d event(s) saved for run %s", len(events), run_id)
    return saved


def process_threads(client: WxOClient, agent_id: Optional[str]) -> int:
    """
    Extract all message threads + their individual messages.
    Returns the number of records saved.
    """
    saved = 0
    threads = client.list_threads()
    log.info("Found %d thread(s)", len(threads))

    for thread in threads:
        thread_id = thread.get("id") or thread.get("thread_id")
        if not thread_id:
            continue

        messages = client.list_messages(thread_id)
        for msg in messages:
            role = msg.get("role", "unknown")
            direction = "request" if role == "user" else "response"

            save_payload_to_db(
                raw=msg,
                direction=direction,
                agent_id=agent_id,
                thread_id=thread_id,
                tenant_id=msg.get("tenant_id"),
            )
            saved += 1

        log.info("  Thread %s: %d message(s) saved", thread_id, len(messages))

    return saved


def process_traces(agent_id: Optional[str]) -> int:
    """
    Use the ADK TracesController (observability SDK) to pull span data.
    Requires watsonx Orchestrate credentials configured in the ADK environment.
    Returns the number of trace records saved.
    """
    saved = 0
    try:
        from datetime import timedelta

        from ibm_watsonx_orchestrate.cli.commands.observability.traces.traces_controller import (
            TracesController,
        )
        from ibm_watsonx_orchestrate.client.observability.traces import (
            TraceFilters,
            TraceSort,
        )

        controller = TracesController()
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=24)

        filters = TraceFilters(
            start_time=start_time.isoformat().replace("+00:00", "Z"),
            end_time=end_time.isoformat().replace("+00:00", "Z"),
            agent_ids=[agent_id] if agent_id else None,
        )
        sort = TraceSort(field="start_time", direction="desc")

        try:
            search_resp = controller.search_traces(
                filters=filters,
                sort=sort,
                include_root_spans=True,
            )
        except TypeError:
            # Backward compatibility for ADK versions without include_root_spans.
            search_resp = controller.search_traces(
                filters=filters,
                sort=sort,
            )

        for summary in (search_resp.traceSummaries or []):
            trace_id = summary.traceId
            spans_response, json_str = controller.export_trace_to_json(
                trace_id, pretty=False
            )

            raw_trace: Dict[str, Any] = json.loads(json_str)
            raw_trace["_trace_summary"] = {
                "traceId": trace_id,
                "durationMs": getattr(summary, "durationMs", None),
                "spanCount": getattr(summary, "spanCount", None),
                "agentNames": getattr(summary, "agentNames", []),
            }

            save_payload_to_db(
                raw=raw_trace,
                direction="trace",
                agent_id=agent_id,
                trace_id=trace_id,
            )
            saved += 1

        log.info("Saved %d trace(s) from observability platform", saved)

    except Exception as exc:
        log.warning("Trace extraction skipped: %s", exc)

    return saved


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_client() -> WxOClient:
    base_url = get_env("WXO_API_ENDPOINT", "WO_INSTANCE")
    if not base_url:
        raise EnvironmentError("Set WXO_API_ENDPOINT (or WO_INSTANCE) in the environment.")

    if api_key := get_env("WXO_API_KEY", "WO_API_KEY"):
        log.info("Authenticating via watsonx Orchestrate credentials …")
        token = get_wxo_access_token(base_url, api_key)
    elif (uname := get_env("WXO_USERNAME")) and (pwd := get_env("WXO_PASSWORD")):
        log.info("Authenticating via Developer Edition login …")
        token = get_dev_edition_token(base_url, uname, pwd)
    else:
        raise EnvironmentError(
            "Set WXO_API_KEY (or WO_API_KEY)  or  WXO_USERNAME + WXO_PASSWORD in the environment."
        )

    return WxOClient(base_url, token)


def run_extraction(agent_id: Optional[str], agent_name: Optional[str]) -> None:
    client = build_client()
    resolved_agent_id = resolve_agent_id(client, agent_name, agent_id)

    if resolved_agent_id:
        log.info("Targeting agent id=%s", resolved_agent_id)
    else:
        log.info("No agent filter configured. Extracting all agents.")

    log.info("=== Extracting assistant runs ===")
    total_saved = 0
    for run in iter_all_runs(client):
        # Filter by agent if specified
        if resolved_agent_id and run.get("agent_id") != resolved_agent_id:
            continue
        try:
            total_saved += process_run(client, run, resolved_agent_id)
        except Exception as exc:
            log.error("Failed to process run %s: %s", run.get("id"), exc)

    log.info("=== Extracting message threads ===")
    total_saved += process_threads(client, resolved_agent_id)

    log.info("=== Extracting observability traces ===")
    total_saved += process_traces(resolved_agent_id)

    log.info("=== Done. Total records saved: %d ===", total_saved)


def main() -> None:
    parser = argparse.ArgumentParser(description="watsonx Orchestrate payload extractor")
    parser.add_argument("--agent-id", default=get_env("WXO_AGENT_ID"), help="Agent UUID to filter")
    parser.add_argument(
        "--agent-name",
        default=get_env("WXO_AGENT_NAME"),
        help="Agent name/display name to resolve automatically",
    )
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds")
    args = parser.parse_args()

    if args.once:
        run_extraction(args.agent_id, args.agent_name)
    else:
        log.info("Starting polling loop (interval=%ds). Ctrl-C to stop.", args.interval)
        while True:
            try:
                run_extraction(args.agent_id, args.agent_name)
            except Exception as exc:
                log.error("Extraction cycle failed: %s", exc)
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
