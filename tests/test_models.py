"""Chat model selection from the environment (no network calls)."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from kai.models import DevEchoModel, get_chat_model

QWEN_URL = "https://ws-example.ap-southeast-1.maas.aliyuncs.com/apps/anthropic"


class ModelSelectionTests(unittest.TestCase):
    def env(self, **values):
        return mock.patch.dict(os.environ, values, clear=True)

    def test_dev_and_unknown(self):
        with self.env(KAI_MODEL_PROVIDER="dev"):
            self.assertIsInstance(get_chat_model(), DevEchoModel)
        with self.env(KAI_MODEL_PROVIDER="nope"), self.assertRaises(RuntimeError):
            get_chat_model()

    def test_anthropic_compatible_qwen(self):
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError:
            self.skipTest("langchain-anthropic not installed")
        with self.env(KAI_MODEL_PROVIDER="anthropic", KAI_MODEL_ID="qwen4-max",
                      KAI_MODEL_BASE_URL=QWEN_URL, DASHSCOPE_API_KEY="test-key"):
            model = get_chat_model()
        self.assertIsInstance(model, ChatAnthropic)
        self.assertEqual(model.model, "qwen4-max")
        self.assertEqual(model.anthropic_api_url, QWEN_URL)
        self.assertEqual(model.anthropic_api_key.get_secret_value(), "test-key")
        self.assertEqual(model.temperature, 0)
        self.assertEqual(model.thinking, {"type": "disabled"})

    def test_thinking_blocks_are_dropped(self):
        from kai.models import plain_content

        empty_thinking = {"type": "thinking", "thinking": "", "signature": ""}
        self.assertEqual(plain_content([empty_thinking, {"type": "text", "text": "٢ + ٢ = ٤"}]), "٢ + ٢ = ٤")
        tool_use = {"type": "tool_use", "id": "t1", "name": "x", "input": {}}
        self.assertEqual(plain_content([empty_thinking, tool_use]), [tool_use])
        self.assertEqual(plain_content("plain"), "plain")

    def test_anthropic_needs_a_key(self):
        with self.env(KAI_MODEL_PROVIDER="anthropic"), self.assertRaises(RuntimeError) as cm:
            get_chat_model()
        self.assertIn("DASHSCOPE_API_KEY", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
