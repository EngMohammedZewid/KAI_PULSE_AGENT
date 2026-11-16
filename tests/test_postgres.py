"""Postgres integration: users/threads repo and the LangGraph checkpointer.

Skipped unless KAI_TEST_DATABASE_URL points at a disposable database, e.g.

    docker run -d --name kai-pg-test -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16-alpine
    KAI_TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres python -m unittest tests.test_postgres
"""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid

from langchain_core.messages import HumanMessage

from kai.files import LocalFileStore
from tests.test_graph import ScriptedModel, route, say
from tests.test_tools import STATEMENT_CSV

DB_URL = os.environ.get("KAI_TEST_DATABASE_URL", "")


@unittest.skipUnless(DB_URL, "set KAI_TEST_DATABASE_URL to run Postgres tests")
class PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from kai.server.main import open_postgres

        cls.pool, cls.saver, cls.repo = open_postgres(DB_URL)

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()

    def test_repo_round_trip_and_ownership(self):
        alice, token = self.repo.create_user("Alice " + uuid.uuid4().hex[:6])
        bob, _ = self.repo.create_user("Bob " + uuid.uuid4().hex[:6])
        self.assertEqual(self.repo.user_for_token(token).id, alice.id)
        self.assertIsNone(self.repo.user_for_token(token + "x"))

        t = self.repo.create_thread(alice.id)
        self.repo.touch_thread(t.id, "First title")
        self.repo.touch_thread(t.id, "Ignored second title")
        self.assertEqual(self.repo.get_thread(alice.id, t.id).title, "First title")
        self.assertIsNone(self.repo.get_thread(bob.id, t.id))
        self.assertEqual([x.id for x in self.repo.list_threads(alice.id)], [t.id])

    def test_conversation_survives_a_new_graph(self):
        """State saved by one graph instance is read back by a fresh one (like a restart)."""
        from kai.graph import build_graph

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(tmp)
            cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "file_store": store}}
            ref = store.put("jan.csv", STATEMENT_CSV.encode())

            first = build_graph(ScriptedModel(), checkpointer=self.saver)
            first.invoke({"messages": [HumanMessage("add")], "uploads": [ref]}, cfg)

            model = ScriptedModel(script=[route("general"), say("Still here.")])
            second = build_graph(model, checkpointer=self.saver)
            state = second.invoke({"messages": [HumanMessage("are you there?")]}, cfg)
            self.assertEqual(state["statements"], [ref["id"]])
            self.assertEqual(state["files"][0]["name"], "jan.csv")
            self.assertEqual(state["messages"][-1].content, "Still here.")
            self.assertEqual(len(state["messages"]), 4)


if __name__ == "__main__":
    unittest.main()
