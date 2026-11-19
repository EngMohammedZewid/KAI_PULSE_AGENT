"""Build the app from environment variables.

    uvicorn --factory kai.server.main:build_app --host 0.0.0.0 --port 8000

DATABASE_URL set   -> Postgres for conversation state (LangGraph checkpointer)
                      and for users/threads.
DATABASE_URL unset -> in-memory state, for local development only; a dev user
                      is created and its token printed at startup.

Tracing: set LANGSMITH_TRACING=true and LANGSMITH_API_KEY (and optionally
LANGSMITH_PROJECT); LangChain picks these up automatically. Each run carries
the user id and thread id as metadata.
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from kai.files import default_store
from kai.graph import build_graph
from kai.models import get_chat_model
from kai.server.app import Settings, create_app
from kai.server.repo import MemoryRepo, PostgresRepo

log = logging.getLogger("kai.server")


def open_postgres(database_url: str):
    """Pool + checkpointer + repo sharing one Postgres database."""
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        database_url,
        min_size=1,
        max_size=int(os.environ.get("KAI_DB_POOL_SIZE", "10")),
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=True,
    )
    saver = PostgresSaver(pool)
    saver.setup()
    repo = PostgresRepo(pool)
    repo.setup()
    return pool, saver, repo


def build_app() -> FastAPI:
    logging.basicConfig(level=os.environ.get("KAI_LOG_LEVEL", "INFO"))
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        _, checkpointer, repo = open_postgres(database_url)
    else:
        from langgraph.checkpoint.memory import InMemorySaver

        checkpointer, repo = InMemorySaver(), MemoryRepo()
        _, token = repo.create_user("Local developer")
        log.warning("DATABASE_URL is not set: conversations live in memory only. "
                    "Dev API token: %s", token)

    graph = build_graph(get_chat_model(), checkpointer=checkpointer)
    settings = Settings(max_upload_mb=int(os.environ.get("KAI_MAX_UPLOAD_MB", "20")))
    return create_app(graph, repo, default_store(), settings)
