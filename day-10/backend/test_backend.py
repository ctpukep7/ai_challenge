"""Tests run without external API calls."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from .agent import ContextStrategiesAgent
from .store import SQLiteStrategyStore


class FakeCreate:
    def __init__(self):
        self.options = []

    def create(self, **options):
        self.options.append(options)
        messages = options["messages"]
        facts_call = any("Обнови память диалога" in item["content"] for item in messages if item["role"] == "system")
        answer = json.dumps(
            {
                "goal": ["Собрать ТЗ"],
                "constraints": ["Бюджет ограничен"],
                "preferences": [],
                "decisions": ["Использовать веб"],
                "agreements": [],
                "open_questions": [],
            },
            ensure_ascii=False,
        ) if facts_call else "Ответ модели."
        return SimpleNamespace(
            model="deepseek-v4-flash",
            choices=[SimpleNamespace(message=SimpleNamespace(content=answer), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=100 + len(messages), completion_tokens=20, total_tokens=120 + len(messages)),
        )


class FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeCreate())


class Day10Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "context.sqlite3"
        self.store = SQLiteStrategyStore(self.path)
        self.client = FakeClient()
        self.agent = ContextStrategiesAgent(self.store, client=self.client)
        self.experiment = self.store.ensure_experiment()

    def tearDown(self):
        self.temp.cleanup()

    def ask(self, target, prompt, window=2):
        return self.agent.ask(self.experiment["id"], target, prompt, {"provider": "deepseek"}, window)

    def test_sliding_window_sends_only_last_messages(self):
        self.ask("sliding", "Первый")
        self.ask("sliding", "Второй")
        self.ask("sliding", "Третий")
        latest = self.client.chat.completions.options[-1]["messages"]
        self.assertEqual([item["content"] for item in latest], ["Второй", "Ответ модели.", "Третий"])

    def test_facts_update_is_saved_and_used(self):
        self.ask("facts", "Цель — собрать ТЗ")
        self.ask("facts", "Бюджет ограничен")
        facts_id = self.store.strategy_root(self.experiment["id"], "facts")
        latest_facts = self.store.latest_facts(facts_id)
        self.assertEqual(latest_facts["values"]["goal"], ["Собрать ТЗ"])
        self.assertEqual(len(self.store.facts_history(facts_id)), 2)
        fact_requests = [
            item for item in self.client.chat.completions.options
            if any("Обнови память диалога" in message["content"] for message in item["messages"] if message["role"] == "system")
        ]
        self.assertEqual(len(fact_requests), 2)
        answer_request = self.client.chat.completions.options[-1]["messages"]
        self.assertIn("Sticky Facts", answer_request[0]["content"])

    def test_branching_keeps_children_independent(self):
        self.ask("all", "Общий факт", window=2)
        self.store.create_checkpoint(self.experiment["id"])
        branches = self.store.get_experiment(self.experiment["id"])["strategies"]["branching"]["threads"]
        branch_a = next(item for item in branches if item["label"] == "Ветка A")
        branch_b = next(item for item in branches if item["label"] == "Ветка B")
        self.store.set_active_branch(self.experiment["id"], branch_a["id"])
        self.ask("branching", "Только A")
        self.store.set_active_branch(self.experiment["id"], branch_b["id"])
        self.ask("branching", "Только B")
        a_text = " ".join(item["content"] for item in self.store.branch_lineage(branch_a["id"]))
        b_text = " ".join(item["content"] for item in self.store.branch_lineage(branch_b["id"]))
        self.assertIn("Общий факт", a_text)
        self.assertIn("Общий факт", b_text)
        self.assertNotIn("Только B", a_text)
        self.assertNotIn("Только A", b_text)

    def test_restart_restores_context_and_stats(self):
        self.ask("all", "Запомни ограничение", window=2)
        restored = SQLiteStrategyStore(self.path)
        experiment = restored.get_experiment(self.experiment["id"])
        self.assertEqual(len(experiment["turns"]), 1)
        self.assertTrue(experiment["strategies"]["facts"]["facts"])
        stats = restored.experiment_stats(self.experiment["id"])
        self.assertGreater(stats["facts"]["facts"]["total_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
