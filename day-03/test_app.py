import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import app


def completion(answer="Решение", tokens=42, finish_reason="stop", reasoning_tokens=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=answer), finish_reason=finish_reason)],
        usage=SimpleNamespace(
            completion_tokens=tokens,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        ),
    )


class Day03ApiTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def post_with_mock(self, strategy, controls=None, responses=None, environment=None):
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = responses or [completion()]
        environment = environment if environment is not None else {"OPENROUTER_API_KEY": "router-key"}
        with patch.dict(os.environ, environment, clear=True), patch("app.OpenAI", return_value=fake_client) as openai:
            response = self.client.post("/api/solve", json={"prompt": "Логическая задача", "strategy": strategy, "controls": controls or {}})
        return response, fake_client.chat.completions.create.call_args_list, openai.call_args_list

    def test_page_is_served_by_flask(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Машина", response.get_data(as_text=True))

    def test_direct_request_contains_only_the_task(self):
        response, calls, _ = self.post_with_mock("direct")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls[0].kwargs["messages"], [{"role": "user", "content": "Логическая задача"}])
        self.assertNotIn("extra_body", calls[0].kwargs)
        self.assertEqual(response.get_json()["provider"], "openrouter")
        self.assertEqual(response.get_json()["instruction_mode"], "system")
        self.assertGreaterEqual(response.get_json()["elapsed_ms"], 0)

    def test_step_and_experts_add_instructions_to_user_prompt(self):
        user_controls = {"instruction_mode": "user"}
        _, step_calls, _ = self.post_with_mock("step_by_step", user_controls)
        _, expert_calls, _ = self.post_with_mock("experts", user_controls)
        self.assertIn("Решай пошагово", step_calls[0].kwargs["messages"][-1]["content"])
        expert_prompt = expert_calls[0].kwargs["messages"][-1]["content"]
        self.assertIn("Аналитик", expert_prompt)
        self.assertIn("Инженер", expert_prompt)
        self.assertIn("Критик", expert_prompt)

    def test_controls_are_forwarded_without_key(self):
        controls = {"provider": "deepseek", "format_instruction": "Ровно 3 пункта", "max_tokens": "120", "stop_sequences": ["<<<END>>>", "<<<SECOND>>>"], "thinking_mode": "enabled"}
        response, calls, _ = self.post_with_mock("step_by_step", controls, environment={"DEEPSEEK_API_KEY": "test-key"})
        body = response.get_json()["request_chain"][0]
        self.assertEqual(calls[0].kwargs["max_tokens"], 120)
        self.assertEqual(calls[0].kwargs["stop"], ["<<<END>>>", "<<<SECOND>>>"])
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertNotIn("test-key", str(body))

    def test_auto_instruction_has_two_requests(self):
        response, calls, _ = self.post_with_mock("auto_instruction", {"max_tokens": "80", "instruction_mode": "user"}, [completion("Созданная инструкция"), completion("Финальное решение")])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["max_tokens"], 400)
        self.assertEqual(calls[0].kwargs["messages"][0]["role"], "user")
        self.assertIn("Логическая задача", calls[0].kwargs["messages"][0]["content"])
        self.assertEqual(calls[1].kwargs["messages"][-1], {"role": "user", "content": "Созданная инструкция"})
        self.assertEqual(response.get_json()["generated_prompt"], "Созданная инструкция")

    def test_system_mode_moves_method_instruction_to_system_message(self):
        response, calls, _ = self.post_with_mock("step_by_step", {"instruction_mode": "system"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls[0].kwargs["messages"], [
            {"role": "system", "content": "Решай пошагово."},
            {"role": "user", "content": "Логическая задача"},
        ])

    def test_openrouter_uses_its_key_and_skips_deepseek_thinking(self):
        controls = {"provider": "openrouter", "model": "openrouter/free", "thinking_mode": "enabled", "max_tokens": "100"}
        response, calls, openai_calls = self.post_with_mock(
            "direct", controls, environment={"OPENROUTER_API_KEY": "router-key"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(openai_calls[0].kwargs, {"api_key": "router-key", "base_url": "https://openrouter.ai/api/v1"})
        self.assertEqual(calls[0].kwargs["model"], "openrouter/free")
        self.assertNotIn("extra_body", calls[0].kwargs)
        body = response.get_json()["request_chain"][0]
        self.assertNotIn("thinking", body)
        self.assertNotIn("router-key", str(body))
        self.assertEqual(response.get_json()["thinking_applied"], None)

    def test_openrouter_without_its_key_is_reported(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post("/api/solve", json={
                "prompt": "Задача",
                "strategy": "direct",
                "controls": {"provider": "openrouter"},
            })
        self.assertEqual(response.status_code, 503)
        self.assertIn("OPENROUTER_API_KEY", response.get_json()["error"])

    def test_invalid_data_and_missing_key_are_safe(self):
        self.assertEqual(self.client.post("/api/solve", json={"prompt": "", "strategy": "direct"}).status_code, 400)
        self.assertEqual(self.client.post("/api/solve", json={"prompt": "Задача", "strategy": "wrong"}).status_code, 400)
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post("/api/solve", json={"prompt": "Задача", "strategy": "direct"})
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
