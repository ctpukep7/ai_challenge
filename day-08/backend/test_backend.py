"""Tests use a fake client: no request spends tokens."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from .agent import AssistantAgent, AgentInputError, AgentRequestError
from .catalog import ModelMetadataService, fallback_cost
from .store import SQLiteConversationStore


class FakeCreate:
    def __init__(self):
        self.options = []

    def create(self, **options):
        self.options.append(options)
        return SimpleNamespace(
            model="deepseek-v4-flash",
            choices=[SimpleNamespace(message=SimpleNamespace(content="Ответ"), finish_reason="stop")],
            usage=SimpleNamespace(
                prompt_tokens=14,
                completion_tokens=6,
                total_tokens=20,
                prompt_cache_hit_tokens=3,
                prompt_cache_miss_tokens=11,
                reasoning_tokens=2,
            ),
        )


class FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeCreate())


class TokenLabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteConversationStore(Path(self.temp.name) / "context.sqlite3")
        self.client = FakeClient()
        self.agent = AssistantAgent(self.store, client=self.client)
        self.conversation = self.store.ensure_conversation()

    def tearDown(self):
        self.temp.cleanup()

    def test_usage_is_persisted_and_history_is_sent(self):
        first = self.agent.reply(self.conversation["id"], "Первый вопрос", {"provider": "deepseek"})
        second = self.agent.reply(self.conversation["id"], "Второй вопрос", {"provider": "deepseek"})
        self.assertEqual(first.usage["prompt_tokens"], 14)
        self.assertEqual(first.usage["reasoning_tokens"], 2)
        messages = self.client.chat.completions.options[1]["messages"]
        self.assertEqual([item["content"] for item in messages], ["Первый вопрос", "Ответ", "Второй вопрос"])
        restored = SQLiteConversationStore(Path(self.temp.name) / "context.sqlite3")
        stats = restored.token_stats(self.conversation["id"])
        self.assertEqual(stats["turns"], 2)
        self.assertEqual(stats["totals"]["total_tokens"], 40)
        self.assertGreater(stats["totals"]["cost_usd"], 0)
        self.assertEqual(second.usage["finish_reason"], "stop")

    def test_synthetic_context_repeats_only_the_api_history(self):
        self.agent.reply(self.conversation["id"], "Первый вопрос", {"provider": "deepseek"})
        reply = self.agent.reply(self.conversation["id"], "Вопрос с виртуальной историей", {"provider": "deepseek"}, synthetic_copies=4)

        sent = self.client.chat.completions.options[-1]["messages"]
        self.assertEqual([item["content"] for item in sent], ["Первый вопрос", "Ответ"] * 4 + ["Вопрос с виртуальной историей"])
        saved = self.store.get_conversation(self.conversation["id"])["messages"]
        self.assertEqual(len(saved), 4)
        self.assertEqual(reply.request["synthetic_context"]["history_copy_multiplier"], 4)
        self.assertEqual(reply.request["context_summary"]["base_history_messages"], 2)
        self.assertEqual(len(reply.request["messages"]), 3)

    def test_empty_prompt_is_rejected_without_writing(self):
        with self.assertRaises(AgentInputError):
            self.agent.reply(self.conversation["id"], "  ", {})
        self.assertEqual(self.store.get_conversation(self.conversation["id"])["messages"], [])

    def test_provider_error_keeps_safe_diagnostic_detail(self):
        class FailingCreate:
            def create(self, **options):
                raise RuntimeError("401 Authorization: Bearer sk-private-token")

        failed_client = SimpleNamespace(chat=SimpleNamespace(completions=FailingCreate()))
        agent = AssistantAgent(self.store, client=failed_client)
        with self.assertRaises(AgentRequestError) as raised:
            agent.reply(self.conversation["id"], "Проверка", {"provider": "deepseek"})
        self.assertIn("401", raised.exception.provider_detail)
        self.assertNotIn("sk-private-token", raised.exception.provider_detail)
        self.assertEqual(self.store.get_conversation(self.conversation["id"])["messages"], [])

    def test_catalog_cost_uses_actual_usage_only(self):
        self.assertIsNone(fallback_cost({"prompt_tokens": None, "completion_tokens": 2}, {"pricing": {"prompt_cache_hit": 1, "prompt_cache_miss": 2, "completion": 3}}))
        self.assertEqual(fallback_cost({"prompt_tokens": 10, "completion_tokens": 2, "prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 5}, {"pricing": {"prompt_cache_hit": 1, "prompt_cache_miss": 2, "completion": 3}}), 0.000021)

    def test_openrouter_catalog_is_read_dynamically(self):
        class Response:
            def raise_for_status(self): pass
            def json(self): return {"data": {"id": "demo/model", "context_length": 50000, "top_provider": {"max_completion_tokens": 1024}, "pricing": {"prompt": "0.000001", "completion": "0.000002"}}}
        service = ModelMetadataService(http_get=lambda *args, **kwargs: Response())
        data = service.for_model("openrouter", "demo/model", "key")
        self.assertEqual(data["context_length"], 50000)
        self.assertEqual(data["max_completion_tokens"], 1024)
        self.assertEqual(data["pricing"]["completion"], 2.0)

    def test_openrouter_picker_is_context_sorted_short_to_long_and_text_only(self):
        class Response:
            def raise_for_status(self): pass
            def json(self):
                return {"data": [
                    {"id": "small/free", "name": "Small", "context_length": 8_000,
                     "pricing": {"prompt": "0", "completion": "0"},
                     "architecture": {"output_modalities": ["text"]}},
                    {"id": "large/free", "name": "Large", "context_length": 262_144,
                     "pricing": {"prompt": "0", "completion": "0"},
                     "architecture": {"output_modalities": ["text"]}},
                    {"id": "audio/free", "name": "Audio", "context_length": 1_000_000,
                     "pricing": {"prompt": "0", "completion": "0"},
                     "architecture": {"output_modalities": ["text", "audio"]}},
                ]}

        options = ModelMetadataService(http_get=lambda *args, **kwargs: Response()).model_options("openrouter")
        self.assertEqual([option["id"] for option in options], ["small/free", "openrouter/free", "large/free"])
        self.assertNotIn("audio/free", [option["id"] for option in options])
        self.assertIn("бесплатно", options[0]["label"])


if __name__ == "__main__":
    unittest.main()
