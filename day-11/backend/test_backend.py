"""Behavioural tests for Day 11 memory isolation and controlled context."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from .agent import AGENT_CONSTANTS, AgentInputError, AgentRequestError, StatefulMemoryAgent
from .main import create_app
from .store import SQLiteMemoryStore


class FakeCompletions:
    def __init__(self, answer="Готовый **ответ**"):
        self.answer = answer
        self.calls = []

    def create(self, **options):
        self.calls.append(options)
        system_chunks = [item["content"] for item in options["messages"] if item["role"] == "system"]
        system_text = "\n".join(system_chunks)
        if "Обновi память" in system_text or "Обнови память" in system_text:
            content = json.dumps(
                {
                    "goal": ["Подготовить релиз"],
                    "constraints": ["Без персональных данных"],
                    "decisions": [],
                    "agreements": [],
                    "open_questions": [],
                },
                ensure_ascii=False,
            )
        elif "сжимаешь историю" in system_text:
            content = "Сводка: зафиксированы срок, ограничения и решения."
        else:
            content = self.answer
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=17, completion_tokens=5, total_tokens=22),
        )


class FailingCompletions:
    def create(self, **_):
        raise RuntimeError("Bearer sk_this_must_not_be_shown")


def fake_client(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


class MemoryLayersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteMemoryStore(Path(self.temp.name) / "memory.sqlite3")
        self.completions = FakeCompletions()
        self.agent = StatefulMemoryAgent(self.store, client=fake_client(self.completions))

    def tearDown(self):
        self.temp.cleanup()

    def task(self, goal="Спланировать релиз"):
        return {"goal": goal, "hard_constraints": "Не отправлять персональные данные", "task_data": "Срок — пятница", "open_questions": "Кто проверяет?"}

    def test_objects_are_saved_only_in_designated_layers(self):
        entry = self.store.add_long_term("Публикуем только после review.")
        self.store.add_long_term("Релизный чек-лист хранится в wiki.")
        self.store.save_task(self.task())
        self.agent.ask("Что важно для релиза?", {"window_size": 2})

        with self.store.connection() as connection:
            self.assertFalse(connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_profiles'").fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM long_term_entries").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages WHERE content = ?", (entry["content"],)).fetchone()[0], 0)

    def test_preview_has_fixed_order_and_only_window_history(self):
        self.store.add_long_term("Используем review.")
        self.store.add_long_term("Срок фиксирован.")
        self.store.save_task(self.task())
        active = self.store.active_session()
        self.store.append_turn(active["id"], "Старый вопрос", "Старый ответ", {"messages": []})
        result = self.agent.ask("Текущий вопрос", {"window_size": 2, "model": "deepseek-flash", "compression_mode": "sliding_window"})
        messages = result["preview"]["messages"]

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn(AGENT_CONSTANTS["role"], messages[0]["content"])
        self.assertTrue(messages[1]["content"].startswith("Долговременная память — явные записи:"))
        self.assertEqual(
            json.loads(messages[1]["content"].split("\n", 1)[1]),
            {"entries": ["Используем review.", "Срок фиксирован."]},
        )
        self.assertIn("Рабочая память", messages[2]["content"])
        self.assertEqual(messages[3:], [
            {"role": "user", "content": "Старый вопрос"},
            {"role": "assistant", "content": "Старый ответ"},
            {"role": "user", "content": "Текущий вопрос"},
        ])
        self.assertEqual(result["preview"]["compression_mode"], "sliding_window")

    def test_full_session_includes_all_messages_without_window_cut(self):
        self.store.save_task(self.task())
        active = self.store.active_session()
        self.store.append_turn(active["id"], "Первый", "Ответ 1", {"messages": []})
        self.store.append_turn(active["id"], "Второй", "Ответ 2", {"messages": []})
        self.store.append_turn(active["id"], "Третий", "Ответ 3", {"messages": []})

        result = self.agent.ask("Четвёртый", {"window_size": 2, "compression_mode": "full_session"})
        history = [item for item in result["preview"]["messages"] if item["role"] in ("user", "assistant")]

        self.assertEqual([item["content"] for item in history], [
            "Первый", "Ответ 1", "Второй", "Ответ 2", "Третий", "Ответ 3", "Четвёртый",
        ])
        self.assertEqual(result["preview"]["compression_mode"], "full_session")
        self.assertEqual(result["preview"]["provider"], "deepseek")
        self.assertNotIn("window_size", result["preview"])
        self.assertNotIn("batch_size", result["preview"])
        self.assertNotIn("keep_recent", result["preview"])
        self.assertNotIn("max_tokens", result["preview"])

    def test_preview_reflects_sliding_window_settings_from_ui(self):
        self.store.save_task(self.task())
        active = self.store.active_session()
        for index in range(4):
            self.store.append_turn(active["id"], f"Q{index}", f"A{index}", {"messages": []})

        result = self.agent.ask(
            "Пятый",
            {"provider": "deepseek", "compression_mode": "sliding_window", "window_size": 8, "model": "deepseek-flash"},
        )
        history = [item for item in result["preview"]["messages"] if item["role"] in ("user", "assistant")]

        self.assertEqual(result["preview"]["compression_mode"], "sliding_window")
        self.assertEqual(result["preview"]["window_size"], 8)
        self.assertNotIn("batch_size", result["preview"])
        self.assertEqual(len(history), 9)
        self.assertEqual(history[-1]["content"], "Пятый")

    def test_explicit_only_skips_short_term_history(self):
        self.store.add_long_term("Review обязателен.")
        self.store.save_task(self.task())
        active = self.store.active_session()
        self.store.append_turn(active["id"], "Старый вопрос", "Старый ответ", {"messages": []})

        result = self.agent.ask("Новый вопрос", {"window_size": 6, "compression_mode": "explicit_only"})
        messages = result["preview"]["messages"]

        self.assertIn("Долговременная память", messages[1]["content"])
        self.assertIn("Рабочая память", messages[2]["content"])
        self.assertEqual(messages[3:], [{"role": "user", "content": "Новый вопрос"}])

    def test_invalid_compression_mode_is_rejected(self):
        with self.assertRaises(AgentInputError):
            self.agent.controls_from({"compression_mode": "unknown_mode"})

    def test_sticky_facts_adds_facts_block_and_persists_snapshot(self):
        active = self.store.active_session()
        self.store.append_turn(active["id"], "Первый", "Ответ", {"messages": []})
        result = self.agent.ask(
            "Запомни цель релиза",
            {"window_size": 2, "compression_mode": "sticky_facts", "model": "deepseek-flash"},
        )
        facts_blocks = [item for item in result["preview"]["messages"] if item["content"].startswith("Sticky Facts")]
        self.assertEqual(len(facts_blocks), 1)
        self.assertEqual(self.store.latest_facts(active["id"])["values"]["goal"], ["Подготовить релиз"])

        follow_up = self.agent.ask("Продолжим", {"window_size": 2, "compression_mode": "sticky_facts", "model": "deepseek-flash"})
        updated_block = next(item for item in follow_up["preview"]["messages"] if item["content"].startswith("Sticky Facts"))
        self.assertIn("Подготовить релиз", updated_block["content"])

    def test_rolling_summary_archives_batch_after_successful_answer(self):
        active = self.store.active_session()
        controls = {"compression_mode": "rolling_summary", "batch_size": 2, "keep_recent": 2, "model": "deepseek-flash"}
        self.agent.ask("Первый", controls)
        self.agent.ask("Второй", controls)
        self.agent.ask("Третий", controls)
        self.agent.ask("Четвёртый", controls)

        summary = self.store.latest_summary(active["id"])
        self.assertIsNotNone(summary)
        self.assertIn("Сводка", summary["content"])
        archived = sum(message["archived"] for message in self.store.active_session_messages(active["id"], include_archived=True))
        self.assertGreaterEqual(archived, 2)

        result = self.agent.ask("Пятый", controls)
        self.assertTrue(any(item["content"].startswith("Сжатая история") for item in result["preview"]["messages"]))

    def test_branch_session_copies_short_term_state_and_stays_independent(self):
        active = self.store.active_session()
        self.store.append_turn(active["id"], "Общий факт", "Общий ответ", {"messages": []})
        branched = self.store.branch_session(active["id"])

        self.assertNotEqual(branched["id"], active["id"])
        self.assertEqual(branched["branched_from_id"], active["id"])
        self.assertTrue(branched["is_active"])
        self.assertEqual([item["content"] for item in self.store.session_messages(branched["id"])], ["Общий факт", "Общий ответ"])

        self.store.activate_session(active["id"])
        self.agent.ask("Только в исходной", {"window_size": 2})
        self.store.activate_session(branched["id"])
        self.agent.ask("Только в ветке", {"window_size": 2})

        original = " ".join(item["content"] for item in self.store.session_messages(active["id"]))
        fork = " ".join(item["content"] for item in self.store.session_messages(branched["id"]))
        self.assertIn("Только в исходной", original)
        self.assertNotIn("Только в ветке", original)
        self.assertIn("Только в ветке", fork)
        self.assertNotIn("Только в исходной", fork)
        self.assertIn("Общий факт", fork)

    def test_reset_session_does_not_touch_other_layers(self):
        self.store.add_long_term("Без рекламы.")
        self.store.save_task(self.task())
        session = self.store.active_session()
        self.store.append_turn(session["id"], "Вопрос", "Ответ", {"messages": []})

        self.assertTrue(self.store.reset_session(session["id"]))
        self.assertEqual(self.store.session_messages(session["id"]), [])
        self.assertEqual(len(self.store.long_term_entries()), 1)
        self.assertEqual(self.store.task()["goal"], "Спланировать релиз")

    def test_activating_session_selects_the_real_short_term_scope_only(self):
        self.store.add_long_term("Нужен review.")
        self.store.save_task(self.task())
        first = self.store.active_session()
        self.store.append_turn(first["id"], "Вопрос из первого", "Ответ из первого", {"messages": []})
        second = self.store.create_session()
        self.store.append_turn(second["id"], "Вопрос из второго", "Ответ из второго", {"messages": []})

        client = TestClient(create_app(self.store, self.agent))
        response = client.post(f"/api/sessions/{first['id']}/activate")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["active_session"]["id"], first["id"])
        self.assertTrue(next(item for item in response.json()["sessions"] if item["id"] == first["id"])["is_active"])
        self.assertFalse(next(item for item in response.json()["sessions"] if item["id"] == second["id"])["is_active"])

        result = self.agent.ask("Продолжи первый диалог", {"window_size": 2})
        context = result["preview"]["messages"]
        history = [item["content"] for item in context if item["role"] in ("user", "assistant")]
        self.assertEqual(result["session_id"], first["id"])
        self.assertEqual(history, ["Вопрос из первого", "Ответ из первого", "Продолжи первый диалог"])
        self.assertEqual(self.store.session_messages(second["id"])[0]["content"], "Вопрос из второго")
        self.assertEqual(self.store.task()["goal"], "Спланировать релиз")

    def test_existing_database_gets_one_persistent_active_session_on_migration(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO sessions(label) VALUES ('Старый диалог');
            """
        )
        connection.close()

        migrated = SQLiteMemoryStore(legacy_path)

        self.assertEqual(migrated.active_session()["label"], "Старый диалог")
        self.assertTrue(migrated.session(1)["is_active"])
        with migrated.connection() as migrated_connection:
            columns = {row["name"] for row in migrated_connection.execute("PRAGMA table_info(sessions)")}
        self.assertIn("is_active", columns)

    def test_typed_long_term_table_migrates_without_losing_entry_identity(self):
        legacy_path = Path(self.temp.name) / "typed-memory.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE long_term_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_type TEXT NOT NULL CHECK (entry_type IN ('decision', 'knowledge')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO long_term_entries(id, entry_type, content, created_at)
            VALUES
              (4, 'knowledge', '«Фокус» — учебный проект.', '2026-09-10 12:00:00'),
              (9, 'decision', 'Запускать только после QA.', '2026-09-11 13:30:00');
            CREATE TABLE memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL CHECK (layer IN ('short_term', 'working', 'long_term')),
                action TEXT NOT NULL,
                actor TEXT NOT NULL CHECK (actor IN ('user', 'agent', 'system')),
                object_type TEXT NOT NULL,
                object_id INTEGER,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO memory_events(layer, action, actor, object_type, object_id)
            VALUES ('long_term', 'save', 'user', 'knowledge', 4);
            """
        )
        connection.close()

        migrated = SQLiteMemoryStore(legacy_path)

        self.assertEqual(
            migrated.long_term_entries(),
            [
                {"id": 4, "content": "«Фокус» — учебный проект.", "created_at": "2026-09-10 12:00:00"},
                {"id": 9, "content": "Запускать только после QA.", "created_at": "2026-09-11 13:30:00"},
            ],
        )
        with migrated.connection() as migrated_connection:
            columns = [row["name"] for row in migrated_connection.execute("PRAGMA table_info(long_term_entries)")]
        self.assertEqual(columns, ["id", "content", "created_at"])
        migrated_long_term_event = next(event for event in migrated.events() if event["layer"] == "long_term")
        self.assertEqual(migrated_long_term_event["object_type"], "entry")

    def test_long_term_api_accepts_free_text_and_enforces_shared_boundaries(self):
        client = TestClient(create_app(self.store, self.agent))

        created = client.post(
            "/api/long-term",
            json={"content": "Любой полезный контекст", "entry_type": "legacy-value-is-ignored"},
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["long_term"][0]["content"], "Любой полезный контекст")
        self.assertNotIn("entry_type", created.json()["long_term"][0])
        self.assertEqual(created.json()["metrics"]["long_term_entries"], 1)
        self.assertNotIn("decisions", created.json()["metrics"])
        self.assertNotIn("knowledge", created.json()["metrics"])

        entry_id = created.json()["long_term"][0]["id"]
        self.assertEqual(client.delete(f"/api/long-term/{entry_id}").status_code, 200)
        self.assertEqual(client.post("/api/long-term", json={"content": "   "}).status_code, 400)
        self.assertEqual(client.post("/api/long-term", json={"content": "x" * 501}).status_code, 400)
        self.assertEqual(
            client.post("/api/long-term", json={"content": "api_key=sk_testsecret123456789"}).status_code,
            400,
        )

        for index in range(40):
            self.store.add_long_term(f"Запись {index + 1}")
        overflow = client.post("/api/long-term", json={"content": "Сорок первая запись"})
        self.assertEqual(overflow.status_code, 400)
        self.assertIn("40", overflow.json()["detail"]["message"])

    def test_replacing_task_does_not_inherit_working_data_but_keeps_long_term(self):
        self.store.add_long_term("Нужен review.")
        self.store.save_task(self.task())
        self.store.save_task({"goal": "Новая задача", "hard_constraints": "До 10 минут", "task_data": "", "open_questions": ""})

        task = self.store.task()
        self.assertEqual(task["goal"], "Новая задача")
        self.assertEqual(task["hard_constraints"], "До 10 минут")
        self.assertEqual(task["task_data"], "")
        self.assertEqual(task["open_questions"], "")
        self.assertEqual([item["content"] for item in self.store.long_term_entries()], ["Нужен review."])

    def test_state_and_context_do_not_expose_a_profile(self):
        self.store.add_long_term("Срок зафиксирован в договоре.")
        client = TestClient(create_app(self.store, self.agent))
        response = client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertNotIn("profile", payload)
        self.assertNotIn("profile_fields", payload["metrics"])

        result = self.agent.ask("Что мы знаем?", {"compression_mode": "explicit_only"})
        serialized = json.dumps(result["preview"]["messages"], ensure_ascii=False).lower()
        self.assertNotIn("profile", serialized)
        self.assertNotIn("профил", serialized)

    def test_profile_endpoints_are_absent(self):
        client = TestClient(create_app(self.store, self.agent))
        self.assertIn(client.put("/api/profile", json={}).status_code, (404, 405))

    def test_clear_all_memory_endpoint_wipes_layers(self):
        self.store.add_long_term("Review обязателен.")
        self.store.save_task(self.task())
        active = self.store.active_session()
        self.store.append_turn(active["id"], "Вопрос", "Ответ", {"messages": []})

        client = TestClient(create_app(self.store, self.agent))
        response = client.post("/api/memory/clear")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["long_term"], [])
        self.assertNotIn("profile", payload)
        self.assertFalse(payload["task"])
        self.assertEqual(payload["metrics"]["short_term_messages"], 0)
        self.assertEqual(payload["metrics"]["sessions"], 1)
        self.assertEqual(payload["active_session"]["label"], "Диалог 1")
        self.assertEqual(payload["active_session"]["messages"], [])
        self.assertEqual(payload["events"], [])

        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM session_threads").fetchone()[0], 1)

    def test_legacy_profile_table_is_inert_and_does_not_break_clear(self):
        with self.store.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE agent_profiles (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    user_context TEXT,
                    language TEXT,
                    preferences TEXT
                );
                INSERT INTO agent_profiles(id, name, user_context, language, preferences)
                VALUES (1, 'Legacy', 'Старые данные', 'Русский', 'Кратко');
                """
            )
            connection.execute(
                """INSERT INTO memory_events(layer, action, actor, object_type, object_id, details_json)
                   VALUES ('long_term', 'replace', 'user', 'profile', 1, '{}')"""
            )

        migrated = SQLiteMemoryStore(self.store.database_path)
        self.assertFalse(any(event["object_type"] == "profile" for event in migrated.events()))
        migrated.clear_all_memory()
        state = migrated.state()

        self.assertNotIn("profile", state)
        with migrated.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_profiles").fetchone()[0], 1)

    def test_deepseek_model_alias_is_normalized(self):
        controls = StatefulMemoryAgent.controls_from({"model": "deepseek-v4-flash"})
        self.assertEqual(controls.model, "deepseek-flash")

    def test_deepseek_completion_disables_thinking(self):
        controls = StatefulMemoryAgent.controls_from({"model": "deepseek-v4-pro"})
        options = StatefulMemoryAgent._completion_options(controls, [{"role": "user", "content": "Hi"}])
        self.assertEqual(options["extra_body"], {"thinking": {"type": "disabled"}})

    def test_deepseek_flash_completion_uses_selected_model_and_preview(self):
        controls = StatefulMemoryAgent.controls_from({"model": "deepseek-flash"})
        options = StatefulMemoryAgent._completion_options(controls, [{"role": "user", "content": "Hi"}])
        self.assertEqual(controls.model, "deepseek-flash")
        self.assertEqual(options["model"], "deepseek-flash")

        result = self.agent.ask("Проверь выбранную модель", {"model": "deepseek-flash"})
        self.assertEqual(self.completions.calls[-1]["model"], "deepseek-flash")
        self.assertEqual(result["preview"]["model"], "deepseek-flash")

    def test_provider_error_creates_no_half_turn_or_other_memory_write(self):
        failing = StatefulMemoryAgent(self.store, client=fake_client(FailingCompletions()))
        self.store.add_long_term("Безопасное правило.")
        self.store.save_task(self.task())
        before_events = len(self.store.events(100))

        with self.assertRaises(AgentRequestError):
            failing.ask("Не сохраняй полуход", {"window_size": 2})

        self.assertEqual(self.store.session_messages(self.store.active_session()["id"]), [])
        self.assertEqual(len(self.store.long_term_entries()), 1)
        self.assertEqual(self.store.task()["goal"], "Спланировать релиз")
        self.assertEqual(len(self.store.events(100)), before_events)

    def test_api_never_serializes_key_and_sanitizes_markdown(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk_a_realistic_secret_for_test"
        app = create_app(self.store, self.agent)
        client = TestClient(app)
        self.completions.answer = "<script>alert('x')</script> **безопасно**"
        response = client.post("/api/ask", json={"prompt": "Проверь ответ", "controls": {"window_size": 2}})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(os.environ["DEEPSEEK_API_KEY"], serialized)
        self.assertNotIn("<script", payload["answer_html"])
        self.assertIn("<strong>безопасно</strong>", payload["answer_html"])
        self.assertNotIn("api_key", json.dumps(payload["preview"]).lower())

    def test_store_recovers_after_database_file_is_replaced(self):
        self.store.create_session()
        db_path = self.store.database_path
        os.remove(db_path)
        Path(db_path).touch()
        state = self.store.state()
        self.assertIsNotNone(state["active_session"])
        self.assertEqual(len(state["sessions"]), 1)


if __name__ == "__main__":
    unittest.main()
