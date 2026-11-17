"""FastAPI service around the KAI Pulse graph.

Every /api call needs `Authorization: Bearer <token>`. A thread is visible
only to the user who created it; another user's thread id answers 404.

    GET  /api/me
    GET  /api/threads                       the caller's threads, newest first
    POST /api/threads                       {"title"?} -> new thread
    GET  /api/threads/{id}                  history + pending question, if any
    POST /api/threads/{id}/messages         multipart: text, files[] -> new replies
    POST /api/threads/{id}/resume           {"answer"} -> answers a pending question
    POST /api/threads/{id}/feedback         {"message_index", "rating": up|down, "comment"?}
    GET  /                                  the chat page
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

from kai.files import FileStore
from kai.server.repo import Repo, Thread, User

log = logging.getLogger("kai.server")
STATIC = Path(__file__).parent / "static"


@dataclass
class Settings:
    max_upload_mb: int = 20
    max_files_per_message: int = 10
    allowed_extensions: frozenset[str] = field(default_factory=lambda: frozenset({
        ".csv", ".xlsx", ".xls", ".pdf", ".txt", ".md", ".json", ".xml", ".yaml", ".yml",
        ".png", ".jpg", ".jpeg", ".gif"}))
    recursion_limit: int = 25


class NewThread(BaseModel):
    title: str = ""


class Resume(BaseModel):
    answer: str


class Feedback(BaseModel):
    message_index: int
    rating: Literal["up", "down"]
    comment: str = ""


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def serialize(messages: list) -> list[dict]:
    """User and assistant messages for the UI; tool traffic is left out.
    `index` is the message's position in this list, stable as the thread grows."""
    out = []
    for m in messages:
        if isinstance(m, HumanMessage):
            out.append({"role": "user", "content": _text(m.content),
                        "attachments": m.additional_kwargs.get("attachments", [])})
        elif isinstance(m, AIMessage) and _text(m.content).strip():
            out.append({"role": "assistant", "content": _text(m.content)})
    for i, m in enumerate(out):
        m["index"] = i
    return out


def pending_question(snapshot) -> Optional[dict]:
    interrupts = list(getattr(snapshot, "interrupts", ()) or ())
    if not interrupts:
        for task in snapshot.tasks or ():
            interrupts.extend(task.interrupts or ())
    if not interrupts:
        return None
    value = interrupts[0].value
    return value if isinstance(value, dict) else {"question": str(value)}


def thread_json(t: Thread) -> dict:
    return {"id": t.id, "title": t.title or "New conversation",
            "updated_at": t.updated_at.isoformat()}


def create_app(graph, repo: Repo, store: FileStore, settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="KAI Pulse", docs_url="/api/docs", openapi_url="/api/openapi.json")
    busy: dict[str, threading.Lock] = {}
    busy_guard = threading.Lock()

    def current_user(request: Request) -> User:
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        user = repo.user_for_token(token.strip()) if scheme.lower() == "bearer" and token else None
        if user is None:
            raise HTTPException(401, "Missing or invalid API token.",
                                headers={"WWW-Authenticate": "Bearer"})
        return user

    def owned_thread(thread_id: str, user: User = Depends(current_user)) -> Thread:
        thread = repo.get_thread(user.id, thread_id)
        if thread is None:
            raise HTTPException(404, "Thread not found.")
        return thread

    def run_config(thread: Thread) -> dict:
        return {
            "configurable": {"thread_id": thread.id, "file_store": store},
            "metadata": {"kai_user_id": thread.user_id, "kai_thread_id": thread.id},
            "tags": ["kai-pulse"],
            "run_name": "kai-pulse-turn",
            "recursion_limit": settings.recursion_limit,
        }

    def run_turn(thread: Thread, graph_input: Any, title: Optional[str] = None) -> dict:
        with busy_guard:
            lock = busy.setdefault(thread.id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "This conversation is still answering the previous message.")
        try:
            cfg = run_config(thread)
            before = len(graph.get_state(cfg).values.get("messages", []))
            try:
                graph.invoke(graph_input, cfg)
            except Exception:
                log.exception("Graph run failed for thread %s", thread.id)
                raise HTTPException(500, "The assistant hit an error. Please try again.")
            snapshot = graph.get_state(cfg)
            repo.touch_thread(thread.id, title)
            messages = snapshot.values.get("messages", [])
            first_new = len(serialize(messages[:before]))
            new = [m for m in serialize(messages)[first_new:] if m["role"] == "assistant"]
            return {"messages": new, "interrupt": pending_question(snapshot)}
        finally:
            lock.release()

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        # no-cache: browsers revalidate, so a new page version shows up without a hard refresh.
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/me")
    def me(user: User = Depends(current_user)) -> dict:
        return {"id": user.id, "name": user.name}

    @app.get("/api/threads")
    def list_threads(user: User = Depends(current_user)) -> list[dict]:
        return [thread_json(t) for t in repo.list_threads(user.id)]

    @app.post("/api/threads", status_code=201)
    def create_thread(body: NewThread, user: User = Depends(current_user)) -> dict:
        return thread_json(repo.create_thread(user.id, body.title.strip()[:120]))

    @app.get("/api/threads/{thread_id}")
    def get_thread(thread: Thread = Depends(owned_thread)) -> dict:
        snapshot = graph.get_state(run_config(thread))
        return {"thread": thread_json(thread),
                "messages": serialize(snapshot.values.get("messages", [])),
                "interrupt": pending_question(snapshot),
                "feedback": {str(i): r for i, r in repo.get_feedback(thread.id).items()}}

    @app.post("/api/threads/{thread_id}/feedback", status_code=204)
    def feedback(body: Feedback, thread: Thread = Depends(owned_thread)) -> None:
        messages = serialize(graph.get_state(run_config(thread)).values.get("messages", []))
        if not (0 <= body.message_index < len(messages)) or \
                messages[body.message_index]["role"] != "assistant":
            raise HTTPException(422, "message_index must point at an assistant reply.")
        repo.set_feedback(thread.user_id, thread.id, body.message_index,
                          1 if body.rating == "up" else -1, body.comment.strip()[:1000])

    @app.post("/api/threads/{thread_id}/messages")
    def post_message(
        thread: Thread = Depends(owned_thread),
        text: str = Form(""),
        files: list[UploadFile] = File(default=[]),
    ) -> dict:
        text = text.strip()
        if not text and not files:
            raise HTTPException(422, "Send a message, a file, or both.")
        if len(files) > settings.max_files_per_message:
            raise HTTPException(413, f"At most {settings.max_files_per_message} files per message.")

        # A typed reply while the assistant is waiting for an answer (e.g. "PDF") resumes it.
        if not files and pending_question(graph.get_state(run_config(thread))):
            return run_turn(thread, Command(resume=text))

        limit = settings.max_upload_mb * 1024 * 1024
        uploads = []
        for f in files:
            name = Path(f.filename or "upload").name
            if Path(name).suffix.lower() not in settings.allowed_extensions:
                raise HTTPException(415, f"{name}: file type not supported.")
            data = f.file.read(limit + 1)
            if len(data) > limit:
                raise HTTPException(413, f"{name} is larger than {settings.max_upload_mb} MB.")
            uploads.append(store.put(name, data))

        message = HumanMessage(text or f"(uploaded {len(uploads)} file(s))",
                               additional_kwargs={"attachments": [u["name"] for u in uploads]})
        return run_turn(thread, {"messages": [message], "uploads": uploads},
                        title=(text or uploads[0]["name"])[:60])

    @app.post("/api/threads/{thread_id}/resume")
    def resume(body: Resume, thread: Thread = Depends(owned_thread)) -> dict:
        if not pending_question(graph.get_state(run_config(thread))):
            raise HTTPException(409, "Nothing is waiting for an answer in this conversation.")
        return run_turn(thread, Command(resume=body.answer))

    return app
