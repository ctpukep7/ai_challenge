"""Unit tests use a fake client; no external model calls are made."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from .agent import ContextComparisonAgent
from .store import SQLiteExperimentStore


class FakeCreate:
    def __init__(self): self.options = []
    def create(self, **options):
        self.options.append(options)
        is_summary = any("сжимаешь историю" in item["content"] for item in options["messages"] if item["role"] == "system")
        answer = "Сводка: важные факты сохранены." if is_summary else "Ответ модели."
        return SimpleNamespace(
            model="deepseek-v4-flash",
            choices=[SimpleNamespace(message=SimpleNamespace(content=answer), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=100 + len(options["messages"]), completion_tokens=20, total_tokens=120 + len(options["messages"])),
        )


class FakeClient:
    def __init__(self): self.chat = SimpleNamespace(completions=FakeCreate())


class Day09Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "context.sqlite3"
        self.store = SQLiteExperimentStore(self.path)
        self.client = FakeClient()
        self.agent = ContextComparisonAgent(self.store, client=self.client)
        self.experiment = self.store.ensure_experiment()

    def tearDown(self): self.temp.cleanup()

    def ask(self, prompt):
        return self.agent.reply_pair(self.experiment["id"], prompt, {"provider": "deepseek"}, 10, 6)

    def test_same_prompt_is_sent_to_both_branches(self):
        self.ask("Одинаковый вопрос")
        full, compressed = self.client.chat.completions.options[:2]
        self.assertEqual(full["messages"][-1]["content"], "Одинаковый вопрос")
        self.assertEqual(compressed["messages"][-1]["content"], "Одинаковый вопрос")
        stored = self.store.get_experiment(self.experiment["id"])
        self.assertEqual(stored["turns"][0]["prompt"], "Одинаковый вопрос")

    def test_compression_archives_a_batch_and_next_call_uses_summary(self):
        for number in range(1, 9): self.ask(f"Вопрос {number}")
        compressed_id = self.store.branch_id(self.experiment["id"], "compressed")
        compressed = self.store.branch_messages(compressed_id)
        self.assertEqual(sum(message["archived"] for message in compressed), 10)
        summary = self.store.latest_summary(compressed_id)
        self.assertIsNotNone(summary)
        summary_options = next(options for options in self.client.chat.completions.options if any("сжимаешь историю" in item["content"] for item in options["messages"] if item["role"] == "system"))
        self.assertNotIn("max_tokens", summary_options)
        self.ask("Проверка после сжатия")
        compressed_options = self.client.chat.completions.options[-1]
        self.assertEqual(len(compressed_options["messages"]), 8)  # summary + 6 последних + новый вопрос
        self.assertIn("Сжатая история", compressed_options["messages"][0]["content"])
        self.assertEqual(compressed_options["messages"][-1]["content"], "Проверка после сжатия")

    def test_summary_failure_does_not_archive_messages(self):
        class BrokenSummary(FakeCreate):
            def create(self, **options):
                if any("сжимаешь историю" in item["content"] for item in options["messages"] if item["role"] == "system"):
                    raise RuntimeError("summary provider failed")
                return super().create(**options)
        client = FakeClient(); client.chat.completions = BrokenSummary()
        agent = ContextComparisonAgent(self.store, client=client)
        for number in range(1, 9): agent.reply_pair(self.experiment["id"], f"Вопрос {number}", {"provider": "deepseek"}, 10, 6)
        messages = self.store.branch_messages(self.store.branch_id(self.experiment["id"], "compressed"))
        self.assertEqual(sum(message["archived"] for message in messages), 0)

    def test_restart_restores_summary_and_metrics(self):
        for number in range(1, 14): self.ask(f"Вопрос {number}")
        restored = SQLiteExperimentStore(self.path)
        experiment = restored.get_experiment(self.experiment["id"])
        self.assertEqual(len(experiment["turns"]), 13)
        self.assertIsNotNone(experiment["branches"]["compressed"]["summary"])
        self.assertEqual(len(experiment["branches"]["compressed"]["summaries"]), 2)
        stats = restored.experiment_stats(self.experiment["id"])
        self.assertGreater(stats["compressed"]["summary"]["total_tokens"], 0)


if __name__ == "__main__": unittest.main()
