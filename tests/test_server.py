"""Phase 4 checks: the HTTP API around the graph.

Uses FastAPI's TestClient with in-memory users/threads, an in-memory
checkpointer, a local FileStore and the scripted model from test_graph.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from kai.files import LocalFileStore
from kai.graph import build_graph
from kai.models import DevEchoModel
from kai.server.app import Settings, create_app
from kai.server.repo import MemoryRepo, hash_token
from tests.test_graph import ScriptedModel, call, route, say
from tests.test_tools import STATEMENT_CSV


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.model = ScriptedModel()
        self.repo = MemoryRepo()
        graph = build_graph(self.model, checkpointer=InMemorySaver())
        app = create_app(graph, self.repo, LocalFileStore(tmp.name), Settings(max_upload_mb=1))
        self.client = TestClient(app)
        self.alice, alice_token = self.repo.create_user("Alice")
        self.bob, bob_token = self.repo.create_user("Bob")
        self.as_alice = {"Authorization": f"Bearer {alice_token}"}
        self.as_bob = {"Authorization": f"Bearer {bob_token}"}

    def new_thread(self, headers=None) -> str:
        r = self.client.post("/api/threads", json={}, headers=headers or self.as_alice)
        self.assertEqual(r.status_code, 201)
        return r.json()["id"]

    def post(self, thread_id, text="", files=None, headers=None, script=()):
        self.model.script.extend(script)
        r = self.client.post(f"/api/threads/{thread_id}/messages", data={"text": text},
                             files=[("files", (n, d)) for n, d in (files or {}).items()],
                             headers=headers or self.as_alice)
        return r


class AuthTests(ServerTestCase):
    def test_requires_valid_token(self):
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        bad = {"Authorization": "Bearer kai_not_a_real_token"}
        self.assertEqual(self.client.get("/api/me", headers=bad).status_code, 401)
        self.assertEqual(self.client.get("/api/me", headers=self.as_alice).json()["name"], "Alice")

    def test_tokens_are_stored_hashed(self):
        token = self.as_alice["Authorization"].split()[1]
        self.assertNotIn(token, self.repo._tokens)
        self.assertIn(hash_token(token), self.repo._tokens)

    def test_threads_are_private(self):
        tid = self.new_thread()
        self.assertEqual(self.client.get(f"/api/threads/{tid}", headers=self.as_bob).status_code, 404)
        r = self.post(tid, "hello", headers=self.as_bob)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.client.get("/api/threads", headers=self.as_bob).json(), [])
        self.assertEqual(len(self.client.get("/api/threads", headers=self.as_alice).json()), 1)

    def test_health_and_page_need_no_token(self):
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True})
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("KAI Pulse", page.text)
        self.assertEqual(page.headers["cache-control"], "no-cache")


class ConversationTests(ServerTestCase):
    def test_upload_add_then_history(self):
        tid = self.new_thread()
        r = self.post(tid, "add this statement", files={"jan.csv": STATEMENT_CSV.encode()})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("Statement 1 received", body["messages"][0]["content"])
        self.assertIsNone(body["interrupt"])

        history = self.client.get(f"/api/threads/{tid}", headers=self.as_alice).json()
        self.assertEqual([m["role"] for m in history["messages"]], ["user", "assistant"])
        self.assertEqual(history["messages"][0]["attachments"], ["jan.csv"])
        self.assertEqual(history["thread"]["title"], "add this statement")

    def test_report_question_answered_by_button_or_typing(self):
        p = mock.patch("kai.tools.documents._render", return_value=b"%PDF-1.4 test")
        build = p.start()
        self.addCleanup(p.stop)
        tid = self.new_thread()

        r = self.post(tid, "write a report on our chat", script=[
            route("documents"), call("create_report", {"title": "Chat", "content": "## Overview"}),
            say("Your report: https://example.com/r.pdf")])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["interrupt"]["type"], "report_format")
        self.assertEqual(r.json()["messages"], [])
        pending = self.client.get(f"/api/threads/{tid}", headers=self.as_alice).json()
        self.assertEqual(pending["interrupt"]["type"], "report_format")

        r = self.post(tid, "Word please")  # typed answer resumes the pending question
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(build.call_args.args[0], "docx")
        self.assertEqual(r.json()["messages"][-1]["content"], "Your report: https://example.com/r.pdf")
        self.assertIsNone(r.json()["interrupt"])

        r = self.client.post(f"/api/threads/{tid}/resume", json={"answer": "pdf"}, headers=self.as_alice)
        self.assertEqual(r.status_code, 409, "nothing is pending any more")

    def test_resume_endpoint(self):
        p = mock.patch("kai.tools.documents._render", return_value=b"%PDF-1.4 test")
        build = p.start()
        self.addCleanup(p.stop)
        tid = self.new_thread()
        self.post(tid, "report please", script=[
            route("documents"), call("create_report", {"title": "T", "content": "## A"}), say("done")])
        r = self.client.post(f"/api/threads/{tid}/resume", json={"answer": "pdf"}, headers=self.as_alice)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(build.call_args.args[0], "pdf")

    def test_model_error_returns_500_without_details(self):
        tid = self.new_thread()
        r = self.post(tid, "hello")  # nothing scripted -> the model raises
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("scripted", r.text)


class SlowEcho(DevEchoModel):
    """Echo model that takes a moment, so concurrent requests really overlap."""

    def _generate(self, *args, **kwargs):
        time.sleep(0.05)
        return super()._generate(*args, **kwargs)


class ConcurrencyTests(unittest.TestCase):
    """Two users talking at the same time never see each other's messages."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = MemoryRepo()
        graph = build_graph(SlowEcho(), checkpointer=InMemorySaver())
        self.client = TestClient(create_app(graph, self.repo, LocalFileStore(tmp.name)))

    def test_two_users_at_once(self):
        users = {}
        for name in ("Alice", "Bob"):
            _, token = self.repo.create_user(name)
            headers = {"Authorization": f"Bearer {token}"}
            tid = self.client.post("/api/threads", json={}, headers=headers).json()["id"]
            users[name] = (headers, tid)

        def chat(name: str, i: int) -> tuple[str, str]:
            headers, tid = users[name]
            text = f"{name} message {i}"
            while True:  # the same user's own messages queue behind each other
                r = self.client.post(f"/api/threads/{tid}/messages", data={"text": text}, headers=headers)
                if r.status_code != 409:
                    break
                time.sleep(0.01)
            self.assertEqual(r.status_code, 200, r.text)
            return text, r.json()["messages"][-1]["content"]

        jobs = [(n, i) for i in range(5) for n in users]
        with ThreadPoolExecutor(max_workers=6) as pool:
            for text, reply in pool.map(lambda job: chat(*job), jobs):
                self.assertTrue(reply.endswith(f"You said: {text}"), reply)

        for name, (headers, tid) in users.items():
            history = self.client.get(f"/api/threads/{tid}", headers=headers).json()["messages"]
            texts = [m["content"] for m in history]
            other = "Bob" if name == "Alice" else "Alice"
            self.assertEqual(len(history), 10)
            self.assertFalse(any(other in t for t in texts), f"{name} sees {other}'s messages")

    def test_same_thread_runs_one_at_a_time(self):
        _, token = self.repo.create_user("Carol")
        headers = {"Authorization": f"Bearer {token}"}
        tid = self.client.post("/api/threads", json={}, headers=headers).json()["id"]
        url = f"/api/threads/{tid}/messages"
        with ThreadPoolExecutor(max_workers=4) as pool:
            codes = sorted(pool.map(lambda i: self.client.post(url, data={"text": f"m{i}"}, headers=headers).status_code,
                                    range(4)))
        self.assertIn(200, codes)
        self.assertTrue(set(codes) <= {200, 409}, codes)
        history = self.client.get(f"/api/threads/{tid}", headers=headers).json()["messages"]
        self.assertEqual(len(history), 2 * codes.count(200), "each accepted message has one reply")


class UploadLimitTests(ServerTestCase):
    def test_rejects_unknown_type(self):
        r = self.post(self.new_thread(), "x", files={"run.exe": b"MZ"})
        self.assertEqual(r.status_code, 415)

    def test_rejects_large_file(self):
        r = self.post(self.new_thread(), "x", files={"big.csv": b"a" * (1024 * 1024 + 1)})
        self.assertEqual(r.status_code, 413)

    def test_rejects_empty_message(self):
        self.assertEqual(self.post(self.new_thread()).status_code, 422)


if __name__ == "__main__":
    unittest.main()
