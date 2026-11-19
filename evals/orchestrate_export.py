"""Read-only export of the Orchestrate agent's conversations, for side-by-side replay.

    python -m evals.orchestrate_export [--out evals/reports/orchestrate_threads.json]

Needs the Orchestrate SDK (ibm-watsonx-orchestrate, see legacy_orchestrate/
requirements.txt) and WXO_API_ENDPOINT, WXO_API_KEY and WXO_AGENT_ID in the
environment. The export holds real conversations: it is written under
evals/reports/ (git-ignored) and must never be committed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _rows(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("data", "threads", "messages", "items", "results"):
            if isinstance(value.get(key), list):
                return value[key]
    return []


def export(base_url: str, api_key: str, agent_id: str) -> list[dict]:
    from ibm_watsonx_orchestrate.client.client import Client
    from ibm_watsonx_orchestrate.client.credentials import Credentials
    from ibm_watsonx_orchestrate_clients.threads.threads_client import ThreadsClient

    client = Client(Credentials(url=base_url, api_key=api_key))
    threads_client = ThreadsClient(base_url=base_url, api_key=client.token)
    out = []
    for t in _rows(threads_client.get_all_threads(agent_id=agent_id)):
        tid = str(t.get("id") or t.get("thread_id") or "")
        try:
            messages = _rows(threads_client.get_thread_messages(tid))
        except Exception as e:  # one unreadable thread should not stop the export
            print(f"thread {tid[:8]}: {type(e).__name__}", file=sys.stderr)
            continue
        out.append({"thread_id": tid, "created_at": t.get("created_at"), "messages": messages})
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evals.orchestrate_export")
    p.add_argument("--out", type=Path, default=Path("evals/reports/orchestrate_threads.json"))
    args = p.parse_args(argv)
    env = {k: os.environ.get(k, "").strip() for k in ("WXO_API_ENDPOINT", "WXO_API_KEY", "WXO_AGENT_ID")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        print(f"Set {', '.join(missing)} first.", file=sys.stderr)
        return 2
    threads = export(env["WXO_API_ENDPOINT"], env["WXO_API_KEY"], env["WXO_AGENT_ID"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(threads, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"{len(threads)} threads, {sum(len(t['messages']) for t in threads)} messages -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
