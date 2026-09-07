import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app as app_module
from agent import AgentConfigurationError, AgentReply, AgentRequestError, AssistantAgent


def completion(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class AssistantAgentTests(unittest.TestCase):
    def test_reply_sends_only_normalized_user_message(self):
        client = MagicMock()
        client.chat.completions.create.return_value = completion("Готовый ответ")
        agent = AssistantAgent(client=client)

        result = agent.reply("  Привет, агент!  ", {"temperature": 0.7, "max_tokens": 120})
        self.assertEqual(result.answer, "Готовый ответ")
        self.assertEqual(result.request["temperature"], 0.7)
        self.assertEqual(result.request["max_tokens"], 120)
        client.chat.completions.create.assert_called_once_with(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "Привет, агент!"}],
            max_tokens=120,
            temperature=0.7,
            extra_body={"thinking": {"type": "disabled"}},
        )

    def test_agent_reports_missing_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AgentConfigurationError):
                AssistantAgent().reply("Вопрос")

    def test_agent_reports_empty_or_failed_response(self):
        empty_client = MagicMock()
        empty_client.chat.completions.create.return_value = completion(" ")
        with self.assertRaises(AgentRequestError):
            AssistantAgent(client=empty_client).reply("Вопрос")

        failed_client = MagicMock()
        failed_client.chat.completions.create.side_effect = RuntimeError("network")
        with self.assertRaises(AgentRequestError):
            AssistantAgent(client=failed_client).reply("Вопрос")


class FlaskRouteTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def test_ask_delegates_to_assistant_agent(self):
        fake_agent = MagicMock()
        fake_agent.reply.return_value = "Ответ агента"
        with patch.object(app_module, "agent", fake_agent):
            fake_agent.reply.return_value = AgentReply("Ответ агента", "DeepSeek", "deepseek-v4-flash", 10, 2, 3, None, "stop", "disabled", None, {"model": "deepseek-v4-flash"})
            response = self.client.post("/ask", json={"prompt": "Мой вопрос", "controls": {"temperature": 0}})

        self.assertEqual(response.status_code, 200)
        self.assertIn("answer_html", response.get_json())
        fake_agent.reply.assert_called_once_with("Мой вопрос", {"temperature": 0})

    def test_invalid_input_and_agent_errors_are_friendly(self):
        self.assertEqual(self.client.post("/ask", json={"prompt": "  "}).status_code, 400)
        with patch.object(app_module, "agent", MagicMock(reply=MagicMock(side_effect=AgentConfigurationError("Нет ключа")))):
            response = self.client.post("/ask", json={"prompt": "Вопрос"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"], "Нет ключа")

    def test_page_names_the_agent(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("AssistantAgent", page)
        self.assertIn("Ответ AssistantAgent", page)


if __name__ == "__main__":
    unittest.main()
