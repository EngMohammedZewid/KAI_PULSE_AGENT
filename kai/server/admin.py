"""Admin commands for the pilot (need DATABASE_URL):

    python -m kai.server.admin create-user "Full Name"   # prints the API token once
    python -m kai.server.admin list-users
    python -m kai.server.admin disable-user USER_ID      # revokes that user's token
    python -m kai.server.admin feedback-report           # thumbs up/down so far

Only token hashes are stored, so a lost token cannot be recovered: disable the
user and create a new one.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from kai.server.repo import Repo


def run(repo: Repo, args: argparse.Namespace, out=print) -> int:
    if args.command == "create-user":
        user, token = repo.create_user(args.name)
        out(f"Created user {user.name} ({user.id}).")
        out(f"API token (shown once): {token}")
    elif args.command == "list-users":
        users = repo.list_users()
        for u in users:
            out(f"{u.id}  {'disabled' if u.disabled else 'active  '}  {u.name}")
        out(f"{len(users)} users, {sum(not u.disabled for u in users)} active")
    elif args.command == "disable-user":
        if not repo.disable_user(args.user_id):
            out(f"No user with id {args.user_id}.")
            return 1
        out(f"Disabled {args.user_id}; their token no longer works.")
    elif args.command == "feedback-report":
        out(json.dumps(repo.feedback_summary(), ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kai.server.admin")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("create-user", help="create a user and print their API token").add_argument("name")
    sub.add_parser("list-users", help="list users and whether they are active")
    sub.add_parser("disable-user", help="revoke a user's token").add_argument("user_id")
    sub.add_parser("feedback-report", help="thumbs up/down counts and recent comments")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("Set DATABASE_URL first.", file=sys.stderr)
        return 2
    from kai.server.main import open_postgres

    pool, _, repo = open_postgres(database_url)
    try:
        return run(repo, args)
    finally:
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
