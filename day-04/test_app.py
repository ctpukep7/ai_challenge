import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import app


def completion(text, tokens, reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=reason)],
        usage=SimpleNamespace(completion_tokens=tokens),
    )


class Day04ApiTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_same_prompt_is_sent_six_times_with_only_temperature_changed(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = [
            completion("Ноль", 10), completion("Баланс", 11), completion("Вариативность", 12),
            completion("Поиск", 13), completion("Смелость", 14), completion("Экстрим", 15),
        ]
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True), patch(
            "app.OpenAI", return_value=fake_client
        ):
            response = self.client.post("/compare", json={"prompt": "Один запрос"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(fake_client.chat.completions.create.call_args_list), 6)
        calls = fake_client.chat.completions.create.call_args_list
        self.assertEqual([call.kwargs["temperature"] for call in calls], [0, 0.7, 1.2, 1.5, 1.7, 1.9])
        self.assertTrue(all(call.kwargs["messages"] == [{"role": "user", "content": "Один запрос"}] for call in calls))
        self.assertTrue(all("max_tokens" not in call.kwargs for call in calls))
        self.assertTrue(all(call.kwargs["extra_body"] == {"thinking": {"type": "disabled"}} for call in calls))

        data = response.get_json()["results"]
        self.assertEqual([item["temperature"] for item in data], [0, 0.7, 1.2, 1.5, 1.7, 1.9])
        self.assertEqual(data[1]["request"]["temperature"], 0.7)
        self.assertTrue(all("max_tokens" not in item["request"] for item in data))
        self.assertNotIn("test-key", str(data))

    def test_single_run_executes_only_requested_temperature(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = completion("Баланс", 15)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True), patch(
            "app.OpenAI", return_value=fake_client
        ):
            response = self.client.post("/run", json={"prompt": "Один запрос", "temperature": 0.7})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_client.chat.completions.create.call_count, 1)
        call = fake_client.chat.completions.create.call_args
        self.assertEqual(call.kwargs["temperature"], 0.7)
        self.assertEqual(response.get_json()["result"]["answer"], "Баланс")

    def test_single_run_rejects_unknown_temperature(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            response = self.client.post("/run", json={"prompt": "Один запрос", "temperature": 0.5})
        self.assertEqual(response.status_code, 400)
        self.assertIn("1.9", response.get_json()["error"])

    def test_page_explains_the_three_temperature_experiment(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Машина", response.get_data(as_text=True))
        self.assertIn("temperature", response.get_data(as_text=True))

    def test_empty_prompt_and_missing_key_return_clear_errors(self):
        self.assertEqual(self.client.post("/compare", json={"prompt": ""}).status_code, 400)
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post("/compare", json={"prompt": "Задача"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("DEEPSEEK_API_KEY", response.get_json()["error"])

    def test_one_api_error_does_not_hide_other_temperatures(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = [
            completion("Ноль", 10), RuntimeError("network"), completion("Вариативность", 12),
            completion("Поиск", 13), completion("Смелость", 14), completion("Экстрим", 15),
        ]
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True), patch(
            "app.OpenAI", return_value=fake_client
        ):
            data = self.client.post("/compare", json={"prompt": "Задача"}).get_json()
        self.assertEqual(data["results"][0]["answer"], "Ноль")
        self.assertIn("error", data["results"][1])
        self.assertEqual(data["results"][2]["answer"], "Вариативность")
        self.assertEqual(data["results"][5]["answer"], "Экстрим")

    def test_optional_controls_are_identical_in_all_six_requests(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = [completion(letter, index) for index, letter in enumerate("ABCDEF", start=1)]
        controls = {
            "format_instruction": "Ровно 3 пункта",
            "max_tokens": "120",
            "stop_sequences": ["<<<END>>>", "<<<SECOND>>>"],
            "thinking_mode": "enabled",
        }
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True), patch("app.OpenAI", return_value=fake_client):
            response = self.client.post("/compare", json={"prompt": "Один запрос", "controls": controls})

        self.assertEqual(response.status_code, 200)
        calls = fake_client.chat.completions.create.call_args_list
        self.assertTrue(all(call.kwargs["max_tokens"] == 120 for call in calls))
        self.assertTrue(all(call.kwargs["stop"] == ["<<<END>>>", "<<<SECOND>>>"] for call in calls))
        self.assertTrue(all(call.kwargs["extra_body"] == {"thinking": {"type": "enabled"}} for call in calls))
        self.assertTrue(all(call.kwargs["messages"] == [
            {"role": "system", "content": "Следуй формату ответа: Ровно 3 пункта\n\nПосле основного ответа напиши на отдельной строке одну из точных последовательностей: <<<END>>>, <<<SECOND>>>. Не добавляй текст после неё."},
            {"role": "user", "content": "Один запрос"},
        ] for call in calls))

    def test_openrouter_uses_its_key_and_skips_deepseek_thinking(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = [completion(letter, index) for index, letter in enumerate("ABCDEF", start=1)]
        controls = {"provider": "openrouter", "model": "openrouter/free", "thinking_mode": "enabled"}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "router-key"}, clear=True), patch("app.OpenAI", return_value=fake_client) as openai:
            response = self.client.post("/compare", json={"prompt": "Задача", "controls": controls})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(openai.call_args.kwargs, {"api_key": "router-key", "base_url": "https://openrouter.ai/api/v1"})
        calls = fake_client.chat.completions.create.call_args_list
        self.assertTrue(all(call.kwargs["model"] == "openrouter/free" for call in calls))
        self.assertTrue(all("extra_body" not in call.kwargs for call in calls))
        self.assertTrue(all("thinking" not in item["request"] for item in response.get_json()["results"]))


if __name__ == "__main__":
    unittest.main()
