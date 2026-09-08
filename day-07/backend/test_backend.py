import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import backend.main as app_module
from backend.agent import AssistantAgent
from backend.store import SQLiteConversationStore


def completion(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
        model="deepseek-v4-flash",
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=5),
    )


class PersistentContextTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "context.sqlite3"
        self.store = SQLiteConversationStore(self.database_path)
        self.conversation = self.store.ensure_conversation()

    def tearDown(self):
        self.temp_directory.cleanup()

    def test_agent_saves_and_reloads_history_after_restart(self):
        first_client = MagicMock()
        first_client.chat.completions.create.return_value = completion("Первый ответ")
        AssistantAgent(self.store, client=first_client).reply(self.conversation["id"], "Первый вопрос", {"temperature": 0})

        restarted_store = SQLiteConversationStore(self.database_path)
        second_client = MagicMock()
        second_client.chat.completions.create.return_value = completion("Второй ответ")
        AssistantAgent(restarted_store, client=second_client).reply(self.conversation["id"], "Продолжи", {"temperature": 0})

        sent = second_client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(sent, [
            {"role": "user", "content": "Первый вопрос"},
            {"role": "assistant", "content": "Первый ответ"},
            {"role": "user", "content": "Продолжи"},
        ])
        self.assertEqual(len(restarted_store.get_conversation(self.conversation["id"])["messages"]), 4)

    def test_store_creates_and_deletes_sessions(self):
        extra = self.store.create_conversation()
        self.store.append_turn(extra["id"], "Вопрос", "Ответ", {"model": "safe"})
        self.assertEqual(len(self.store.get_conversation(extra["id"])["messages"]), 2)
        self.store.delete_conversation(extra["id"])
        self.assertIsNone(self.store.get_conversation(extra["id"]))

    def test_request_preview_is_persisted_with_assistant_message(self):
        self.store.append_turn(self.conversation["id"], "Вопрос", "Ответ", {"model": "deepseek-v4-flash", "messages": []})
        message = self.store.get_conversation(self.conversation["id"])["messages"][1]
        self.assertEqual(message["request"]["model"], "deepseek-v4-flash")


class FastAPISessionTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.store = SQLiteConversationStore(Path(self.temp_directory.name) / "context.sqlite3")
        self.client = MagicMock()
        self.client.chat.completions.create.return_value = completion("Ответ API")
        self.agent = AssistantAgent(self.store, client=self.client)
        self.http = TestClient(app_module.app)
        self.store_patch = patch.object(app_module, "store", self.store)
        self.agent_patch = patch.object(app_module, "agent", self.agent)
        self.store_patch.start()
        self.agent_patch.start()

    def tearDown(self):
        self.agent_patch.stop()
        self.store_patch.stop()
        self.temp_directory.cleanup()

    def test_session_routes_and_ask_persist_turn(self):
        created = self.http.post("/api/conversations").json()["conversation"]
        answer = self.http.post("/api/ask", json={"conversation_id": created["id"], "prompt": "Запомни меня", "controls": {"temperature": 0}})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(self.store.get_conversation(created["id"])["messages"][0]["content"], "Запомни меня")
        renamed = self.http.put(f"/api/conversations/{created['id']}", json={"title": "Проверка памяти"})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["conversation"]["title"], "Проверка памяти")
        self.assertEqual(self.http.delete(f"/api/conversations/{created['id']}").status_code, 200)


if __name__ == "__main__":
    unittest.main()
