"""Admin commands (need DATABASE_URL):

    python -m kai.server.admin create-user "Full Name"

Prints the new user's API token once. Only its hash is stored, so a lost
token cannot be recovered: create a new user instead.
"""
from __future__ import annotations

import argparse
import os
import sys

from kai.server.main import open_postgres


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kai.server.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user", help="create a user and print their API token")
    create.add_argument("name")
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("Set DATABASE_URL first.", file=sys.stderr)
        return 2
    pool, _, repo = open_postgres(database_url)
    try:
        if args.command == "create-user":
            user, token = repo.create_user(args.name)
            print(f"Created user {user.name} ({user.id}).")
            print(f"API token (shown once): {token}")
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
