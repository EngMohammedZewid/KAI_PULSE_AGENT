"""Users, API tokens and thread ownership.

Tokens are random 32-byte strings shown once at creation; only their SHA-256
hash is stored. Each conversation thread belongs to one user, and every API
call checks that ownership before touching the graph.

Two backends: MemoryRepo (tests, local dev) and PostgresRepo (deployment,
sharing the same database as the LangGraph checkpointer).
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol


def new_token() -> str:
    return "kai_" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class User:
    id: str
    name: str


@dataclass
class Thread:
    id: str
    user_id: str
    title: str
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


class Repo(Protocol):
    def create_user(self, name: str) -> tuple[User, str]: ...
    def user_for_token(self, token: str) -> Optional[User]: ...
    def create_thread(self, user_id: str, title: str = "") -> Thread: ...
    def get_thread(self, user_id: str, thread_id: str) -> Optional[Thread]: ...
    def list_threads(self, user_id: str) -> list[Thread]: ...
    def touch_thread(self, thread_id: str, title: Optional[str] = None) -> None: ...


class MemoryRepo:
    def __init__(self):
        self._lock = threading.Lock()
        self._users: dict[str, User] = {}
        self._tokens: dict[str, str] = {}      # token hash -> user id
        self._threads: dict[str, Thread] = {}

    def create_user(self, name: str) -> tuple[User, str]:
        user, token = User(id=uuid.uuid4().hex, name=name), new_token()
        with self._lock:
            self._users[user.id] = user
            self._tokens[hash_token(token)] = user.id
        return user, token

    def user_for_token(self, token: str) -> Optional[User]:
        user_id = self._tokens.get(hash_token(token))
        return self._users.get(user_id) if user_id else None

    def create_thread(self, user_id: str, title: str = "") -> Thread:
        thread = Thread(id=uuid.uuid4().hex, user_id=user_id, title=title)
        with self._lock:
            self._threads[thread.id] = thread
        return thread

    def get_thread(self, user_id: str, thread_id: str) -> Optional[Thread]:
        thread = self._threads.get(thread_id)
        return thread if thread and thread.user_id == user_id else None

    def list_threads(self, user_id: str) -> list[Thread]:
        mine = [t for t in self._threads.values() if t.user_id == user_id]
        return sorted(mine, key=lambda t: t.updated_at, reverse=True)

    def touch_thread(self, thread_id: str, title: Optional[str] = None) -> None:
        with self._lock:
            thread = self._threads.get(thread_id)
            if thread:
                thread.updated_at = _now()
                if title and not thread.title:
                    thread.title = title


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kai_users (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    disabled    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kai_threads (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES kai_users(id),
    title       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS kai_threads_user_updated ON kai_threads (user_id, updated_at DESC);
"""


class PostgresRepo:
    """Uses a psycopg ConnectionPool opened with row_factory=dict_row."""

    def __init__(self, pool):
        self.pool = pool

    def setup(self) -> None:
        # One statement per execute: the pool prepares statements
        # (prepare_threshold=0), and a prepared statement can't hold several.
        with self.pool.connection() as conn:
            for statement in filter(None, (s.strip() for s in _SCHEMA.split(";"))):
                conn.execute(statement)

    def create_user(self, name: str) -> tuple[User, str]:
        user, token = User(id=uuid.uuid4().hex, name=name), new_token()
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO kai_users (id, name, token_hash) VALUES (%s, %s, %s)",
                         (user.id, name, hash_token(token)))
        return user, token

    def user_for_token(self, token: str) -> Optional[User]:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT id, name FROM kai_users WHERE token_hash = %s AND NOT disabled",
                (hash_token(token),)).fetchone()
        return User(id=row["id"], name=row["name"]) if row else None

    def create_thread(self, user_id: str, title: str = "") -> Thread:
        thread = Thread(id=uuid.uuid4().hex, user_id=user_id, title=title)
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO kai_threads (id, user_id, title) VALUES (%s, %s, %s)",
                         (thread.id, user_id, title))
        return thread

    @staticmethod
    def _thread(row) -> Thread:
        return Thread(id=row["id"], user_id=row["user_id"], title=row["title"],
                      created_at=row["created_at"], updated_at=row["updated_at"])

    def get_thread(self, user_id: str, thread_id: str) -> Optional[Thread]:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM kai_threads WHERE id = %s AND user_id = %s",
                               (thread_id, user_id)).fetchone()
        return self._thread(row) if row else None

    def list_threads(self, user_id: str) -> list[Thread]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM kai_threads WHERE user_id = %s "
                                "ORDER BY updated_at DESC LIMIT 200", (user_id,)).fetchall()
        return [self._thread(r) for r in rows]

    def touch_thread(self, thread_id: str, title: Optional[str] = None) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE kai_threads SET updated_at = now(), "
                "title = CASE WHEN title = '' AND %s <> '' THEN %s ELSE title END WHERE id = %s",
                (title or "", title or "", thread_id))
