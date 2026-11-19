"""Phase 6 checks: pilot feedback, admin commands and the side-by-side replay."""
from __future__ import annotations

import argparse
import tempfile
import unittest

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from evals.compare import conversation, missing_numbers, numbers, run, turns
from kai.files import LocalFileStore
from kai.graph import build_graph
from kai.models import DevEchoModel
from kai.server.admin import run as admin_run
from kai.server.app import create_app
from kai.server.repo import MemoryRepo


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = MemoryRepo()
        graph = build_graph(DevEchoModel(), checkpointer=InMemorySaver())
        self.client = TestClient(create_app(graph, self.repo, LocalFileStore(tmp.name)))
        self.alice, token = self.repo.create_user("Alice")
        self.h = {"Authorization": f"Bearer {token}"}
        _, bob_token = self.repo.create_user("Bob")
        self.bob = {"Authorization": f"Bearer {bob_token}"}
        self.tid = self.client.post("/api/threads", json={}, headers=self.h).json()["id"]

    def say(self, text: str) -> dict:
        r = self.client.post(f"/api/threads/{self.tid}/messages", data={"text": text}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def rate(self, index: int, rating: str, comment: str = "", headers=None):
        return self.client.post(f"/api/threads/{self.tid}/feedback", headers=headers or self.h,
                                json={"message_index": index, "rating": rating, "comment": comment})

    def test_replies_carry_their_index(self):
        first = self.say("one")["messages"][0]
        second = self.say("two")["messages"][0]
        self.assertEqual((first["index"], second["index"]), (1, 3))
        history = self.client.get(f"/api/threads/{self.tid}", headers=self.h).json()["messages"]
        self.assertEqual([m["index"] for m in history], [0, 1, 2, 3])

    def test_rate_reply_and_change_mind(self):
        index = self.say("hello")["messages"][0]["index"]
        self.assertEqual(self.rate(index, "down", "wrong numbers").status_code, 204)
        self.assertEqual(self.repo.feedback_summary()["down_comments"], ["wrong numbers"])
        self.assertEqual(self.rate(index, "up").status_code, 204)  # replaces the earlier rating
        summary = self.repo.feedback_summary()
        self.assertEqual((summary["up"], summary["down"]), (1, 0))
        view = self.client.get(f"/api/threads/{self.tid}", headers=self.h).json()
        self.assertEqual(view["feedback"], {str(index): 1})

    def test_only_assistant_replies_in_own_threads(self):
        self.say("hello")
        self.assertEqual(self.rate(0, "up").status_code, 422, "index 0 is the user's message")
        self.assertEqual(self.rate(9, "up").status_code, 422)
        self.assertEqual(self.rate(1, "up", headers=self.bob).status_code, 404)
        self.assertEqual(self.rate(1, "meh").status_code, 422)

    def test_disabled_user_is_locked_out(self):
        self.assertEqual(self.client.get("/api/me", headers=self.h).status_code, 200)
        self.repo.disable_user(self.alice.id)
        self.assertEqual(self.client.get("/api/me", headers=self.h).status_code, 401)


class AdminTests(unittest.TestCase):
    def test_commands(self):
        repo, out = MemoryRepo(), []
        admin_run(repo, argparse.Namespace(command="create-user", name="Dana"), out.append)
        self.assertTrue(out[1].startswith("API token (shown once): kai_"))
        user = repo.list_users()[0]
        out.clear()
        admin_run(repo, argparse.Namespace(command="disable-user", user_id=user.id), out.append)
        admin_run(repo, argparse.Namespace(command="list-users"), out.append)
        self.assertIn("disabled", out[1])
        self.assertEqual(out[-1], "1 users, 0 active")
        self.assertEqual(admin_run(repo, argparse.Namespace(command="disable-user", user_id="nope"),
                                   out.append), 1)


EXPORT = [
    {"thread_id": "t-1111aaaa", "messages": [
        {"role": "user", "created_at": "2026-05-01T10:00:00Z", "content": [{"response_type": "text", "text": "hi"}]},
        {"role": "assistant", "created_at": "2026-05-01T10:00:02Z", "content": [{"response_type": "text", "text": "Hello!"}]},
        {"role": "user", "created_at": "2026-05-01T10:01:00Z", "content": "what is my average income?"},
        {"role": "assistant", "created_at": "2026-05-01T10:01:05Z",
         "content": [{"text": "Your average monthly income is EGP 20,666.67."}, {"text": "DTI is 14.5%."}]},
        {"role": "tool", "created_at": "2026-05-01T10:01:03Z", "content": "ignored"},
    ]},
    {"thread_id": "t-2222bbbb", "messages": [
        {"role": "user", "created_at": "2026-05-02T09:00:00Z", "content": {"text": "thanks"}},
    ]},
]


class CompareTests(unittest.TestCase):
    def test_reads_export_shapes(self):
        conv = conversation(EXPORT[0])
        self.assertEqual([r for r, _ in conv], ["user", "assistant", "user", "assistant"])
        pairs = turns(conv)
        self.assertEqual(pairs[1][0], "what is my average income?")
        self.assertIn("20,666.67", pairs[1][1])
        self.assertIn("14.5%", pairs[1][1])

    def test_number_checks(self):
        self.assertEqual(numbers("EGP 20,666.67 in 2026, 3 months, 450.5"), {20666.67, 450.5})
        self.assertEqual(missing_numbers("income 20,666.67", "about EGP 20,667 a month"), [])
        self.assertEqual(missing_numbers("income 20,666.67 and debt 3,000", "income 20,667"), [3000.0])

    def test_replay_report_with_dev_model(self):
        report, summary = run(EXPORT, DevEchoModel, "dev")
        self.assertEqual(summary["threads"], 2)
        self.assertEqual(summary["turns"], 3)
        self.assertEqual(summary["missing_numbers"], 1, "the echo model drops the old income figure")
        self.assertIn("## Thread t-1111a", report)
        self.assertIn("missing numbers: 20,666.67", report)
        self.assertIn("(dev model, no LLM) You said: what is my average income?", report)


if __name__ == "__main__":
    unittest.main()
