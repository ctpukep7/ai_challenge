import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import app


def fake_completion(answer="Тестовый ответ", tokens=12, finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=answer),
            finish_reason=finish_reason,
        )],
        usage=SimpleNamespace(completion_tokens=tokens),
    )


class Day02ApiTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_standard_request_has_no_control_parameters(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_completion()

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True), patch(
            "app.OpenAI", return_value=fake_client
        ):
            response = self.client.post("/ask", json={"prompt": "Одинаковый вопрос"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["answer"], "Тестовый ответ")
        request_kwargs = fake_client.chat.completions.create.call_args.kwargs
        self.assertEqual(request_kwargs["messages"], [{"role": "user", "content": "Одинаковый вопрос"}])
        self.assertNotIn("max_tokens", request_kwargs)
        self.assertNotIn("stop", request_kwargs)

    def test_controlled_request_uses_same_prompt_and_controls(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_completion(tokens=40)
        controls = {
            "format_instruction": "Ровно 3 пункта",
            "max_tokens": 120,
            "stop_sequence": "### END",
        }

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True), patch(
            "app.OpenAI", return_value=fake_client
        ):
            response = self.client.post(
                "/ask-controlled",
                json={"prompt": "Одинаковый вопрос", "controls": controls},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["completion_tokens"], 40)
        request_kwargs = fake_client.chat.completions.create.call_args.kwargs
        self.assertEqual(request_kwargs["messages"][1], {"role": "user", "content": "Одинаковый вопрос"})
        self.assertIn("Ровно 3 пункта", request_kwargs["messages"][0]["content"])
        self.assertIn("### END", request_kwargs["messages"][0]["content"])
        self.assertEqual(request_kwargs["max_tokens"], 120)
        self.assertEqual(request_kwargs["stop"], ["### END"])

    def test_controlled_request_validates_controls(self):
        response = self.client.post(
            "/ask-controlled",
            json={
                "prompt": "Вопрос",
                "controls": {
                    "format_instruction": "",
                    "max_tokens": 120,
                    "stop_sequence": "### END",
                },
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("формата", response.get_json()["error"])

    def test_missing_key_returns_safe_error(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post("/ask", json={"prompt": "Вопрос"})

        self.assertEqual(response.status_code, 503)
        self.assertIn("DEEPSEEK_API_KEY", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
