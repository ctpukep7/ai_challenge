import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import app


def fake_completion(answer="Тестовый ответ", tokens=12, finish_reason="stop", reasoning_tokens=None):
    details = SimpleNamespace(reasoning_tokens=reasoning_tokens)
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=answer),
            finish_reason=finish_reason,
        )],
        usage=SimpleNamespace(
            completion_tokens=tokens,
            completion_tokens_details=details,
        ),
    )


class Day02ApiTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def post_with_client(self, settings, completion=None):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = completion or fake_completion()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True), patch(
            "app.OpenAI", return_value=fake_client
        ):
            response = self.client.post(
                "/ask", json={"prompt": "Одинаковый вопрос", "settings": settings}
            )
        return response, fake_client.chat.completions.create.call_args.kwargs

    def test_base_request_has_only_user_message_and_no_controls(self):
        response, request_kwargs = self.post_with_client({})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request_kwargs["messages"], [{"role": "user", "content": "Одинаковый вопрос"}])
        self.assertNotIn("max_tokens", request_kwargs)
        self.assertNotIn("stop", request_kwargs)
        self.assertNotIn("extra_body", request_kwargs)
        self.assertEqual(response.get_json()["thinking_mode"], "по умолчанию DeepSeek (включён)")

    def test_format_adds_a_system_instruction(self):
        response, request_kwargs = self.post_with_client({"format_instruction": "Ровно 3 пункта"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("Ровно 3 пункта", request_kwargs["messages"][0]["content"])
        self.assertEqual(request_kwargs["messages"][1]["content"], "Одинаковый вопрос")

    def test_max_tokens_is_optional_and_forwarded(self):
        response, request_kwargs = self.post_with_client({"max_tokens": "120"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request_kwargs["max_tokens"], 120)

    def test_stop_adds_instruction_and_api_parameter(self):
        response, request_kwargs = self.post_with_client({"stop_sequence": "<<<END>>>"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request_kwargs["stop"], ["<<<END>>>"])
        self.assertIn("<<<END>>>", request_kwargs["messages"][0]["content"])

    def test_thinking_mode_is_forwarded_and_reported(self):
        response, request_kwargs = self.post_with_client(
            {"thinking_mode": "disabled"},
            completion=fake_completion(reasoning_tokens=0),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request_kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(response.get_json()["thinking_mode"], "выключен")
        self.assertEqual(response.get_json()["reasoning_tokens"], 0)

    def test_all_controls_are_forwarded_together(self):
        settings = {
            "format_instruction": "Ровно 3 пункта",
            "max_tokens": 120,
            "stop_sequence": "<<<END>>>",
            "thinking_mode": "enabled",
        }
        response, request_kwargs = self.post_with_client(settings)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request_kwargs["max_tokens"], 120)
        self.assertEqual(request_kwargs["stop"], ["<<<END>>>"])
        self.assertEqual(request_kwargs["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertIn("Ровно 3 пункта", request_kwargs["messages"][0]["content"])

    def test_length_without_visible_content_explains_the_problem(self):
        response, _ = self.post_with_client(
            {"max_tokens": 500, "thinking_mode": "enabled"},
            completion=fake_completion(answer=None, tokens=500, finish_reason="length", reasoning_tokens=500),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("лимит токенов исчерпан", response.get_json()["answer"])
        self.assertEqual(response.get_json()["reasoning_tokens"], 500)

    def test_settings_validation_and_missing_key_are_safe(self):
        invalid = self.client.post(
            "/ask", json={"prompt": "Вопрос", "settings": {"thinking_mode": "unknown"}}
        )
        self.assertEqual(invalid.status_code, 400)

        with patch.dict(os.environ, {}, clear=True):
            missing_key = self.client.post("/ask", json={"prompt": "Вопрос", "settings": {}})
        self.assertEqual(missing_key.status_code, 503)
        self.assertIn("DEEPSEEK_API_KEY", missing_key.get_json()["error"])

    def test_page_preview_does_not_expose_key(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("DEEPSEEK_API_KEY", page)
        self.assertIn("Превью JSON-запроса", page)


if __name__ == "__main__":
    unittest.main()
