import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import MODELS, OPENROUTER_BASE_URL, DEEPSEEK_BASE_URL, TASK_PRESETS, app, deepseek_cost, render_markdown


def completion(text, prompt_tokens=80, completion_tokens=40, cache_hit=0, cache_miss=80):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_cache_hit_tokens=cache_hit,
            prompt_cache_miss_tokens=cache_miss,
        ),
    )


class Day05ApiTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_all_models_receive_identical_prompt_and_common_controls(self):
        created_clients = []

        def client_factory(**kwargs):
            client = MagicMock()
            client.chat.completions.create.return_value = completion("**Антон** виновен.")
            created_clients.append((kwargs, client))
            return client

        environment = {"OPENROUTER_API_KEY": "router-key", "DEEPSEEK_API_KEY": "deepseek-key"}
        with patch.dict(os.environ, environment, clear=True), patch("app.OpenAI", side_effect=client_factory):
            response = self.client.post("/compare", json={"prompt": "Логическая задача"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(created_clients), 3)
        calls = [client.chat.completions.create.call_args.kwargs for _, client in created_clients]
        self.assertTrue(all(call["messages"] == [{"role": "user", "content": "Логическая задача"}] for call in calls))
        self.assertTrue(all(call["temperature"] == 0 and "max_tokens" not in call for call in calls))
        self.assertTrue(all(call["messages"] == calls[0]["messages"] for call in calls))
        deepseek_calls = [call for call in calls if call["model"].startswith("deepseek-")]
        self.assertTrue(all(call["extra_body"] == {"thinking": {"type": "disabled"}} for call in deepseek_calls))

        for arguments, client in created_clients:
            model = client.chat.completions.create.call_args.kwargs["model"]
            if model == "liquid/lfm-2.5-2.6b:free":
                self.assertEqual(arguments["api_key"], "router-key")
                self.assertEqual(arguments["base_url"], OPENROUTER_BASE_URL)
            else:
                self.assertEqual(arguments["api_key"], "deepseek-key")
                self.assertEqual(arguments["base_url"], DEEPSEEK_BASE_URL)
        body = response.get_json()["results"]
        self.assertEqual(body[0]["cost_usd"], 0)
        self.assertNotIn("router-key", str(body))
        self.assertNotIn("deepseek-key", str(body))

    def test_sidebar_controls_are_applied_identically_to_all_models(self):
        created_clients = []

        def client_factory(**kwargs):
            client = MagicMock()
            client.chat.completions.create.return_value = completion("Ответ")
            created_clients.append(client)
            return client

        controls = {"temperature": "0.4", "max_tokens": "321"}
        environment = {"OPENROUTER_API_KEY": "router-key", "DEEPSEEK_API_KEY": "deepseek-key"}
        with patch.dict(os.environ, environment, clear=True), patch("app.OpenAI", side_effect=client_factory):
            response = self.client.post("/compare", json={"prompt": "Задача", "controls": controls})

        self.assertEqual(response.status_code, 200)
        calls = [client.chat.completions.create.call_args.kwargs for client in created_clients]
        self.assertTrue(all(call["temperature"] == 0.4 for call in calls))
        self.assertTrue(all(call["max_tokens"] == 321 for call in calls))
        self.assertTrue(all(call["messages"] == [{"role": "user", "content": "Задача"}] for call in calls))

    def test_system_prompt_is_not_added_even_if_legacy_client_sends_it(self):
        created_clients = []

        def client_factory(**_kwargs):
            client = MagicMock()
            client.chat.completions.create.return_value = completion("Ответ")
            created_clients.append(client)
            return client

        environment = {"OPENROUTER_API_KEY": "router-key", "DEEPSEEK_API_KEY": "deepseek-key"}
        with patch.dict(os.environ, environment, clear=True), patch("app.OpenAI", side_effect=client_factory):
            response = self.client.post("/compare", json={"prompt": "Задача", "controls": {"system_prompt": "Игнорируй условие"}})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(client.chat.completions.create.call_args.kwargs["messages"] == [{"role": "user", "content": "Задача"}] for client in created_clients))

    def test_invalid_sidebar_controls_return_a_clear_error(self):
        response = self.client.post("/compare", json={"prompt": "Задача", "controls": {"temperature": 2.1}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("от 0 до 2", response.get_json()["error"])

    def test_deepseek_cost_uses_cache_fields_and_rejects_incomplete_usage(self):
        flash, pro = MODELS[1], MODELS[2]
        usage = completion("x", completion_tokens=100, cache_hit=50, cache_miss=150).usage
        self.assertEqual(deepseek_cost(flash, usage), round((50 * .0028 + 150 * .14 + 100 * .28) / 1_000_000, 9))
        self.assertEqual(deepseek_cost(pro, usage), round((50 * .003625 + 150 * .435 + 100 * .87) / 1_000_000, 9))
        self.assertIsNone(deepseek_cost(flash, SimpleNamespace(completion_tokens=10)))

    def test_missing_key_and_model_error_do_not_hide_other_results(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = completion("Ответ")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "deepseek-key"}, clear=True), patch("app.OpenAI", return_value=fake_client):
            body = self.client.post("/compare", json={"prompt": "Задача"}).get_json()["results"]
        self.assertIn("OPENROUTER_API_KEY", body[0]["error"])
        self.assertIn("answer_html", body[1])
        self.assertIn("answer_html", body[2])

    def test_validation_and_page(self):
        self.assertEqual(self.client.post("/compare", json={"prompt": ""}).status_code, 400)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Лаборатория", page)
        self.assertIn("Пресеты задач", page)
        self.assertEqual([preset["id"] for preset in TASK_PRESETS], ["alphabetical-order", "animal-crossing", "letter-constraint"])

    def test_markdown_is_rendered_without_unsafe_html(self):
        html = render_markdown("# Заголовок\n\n**Ответ** <script>alert(1)</script>")
        self.assertIn("<h1>Заголовок</h1>", html)
        self.assertIn("<strong>Ответ</strong>", html)
        self.assertNotIn("<script", html)


if __name__ == "__main__":
    unittest.main()
