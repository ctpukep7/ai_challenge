import contextlib
import importlib.util
import io
import itertools
import json
from pathlib import Path
import socket
import sys
import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib import error, request

DAY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DAY_DIR))
spec = importlib.util.spec_from_file_location("day26_cli", DAY_DIR / "cli.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class Response:
    def __init__(self, data):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def read(self, _):
        return self.data


TAGS = {"models": [{"name": cli.MODEL, "size": 2497293803}]}
ANSWER = {"done": True, "model": cli.MODEL, "message": {"role": "assistant", "content": " Ответ "},
          "total_duration": 4_000_000_000, "load_duration": 1_000_000_000,
          "eval_duration": 2_000_000_000, "eval_count": 10, "prompt_eval_count": 7}


class ClientTests(unittest.TestCase):
    def client(self, *responses):
        opener = Mock()
        opener.open.side_effect = [Response(value) if not isinstance(value, Exception) else value for value in responses]
        return cli.OllamaClient(opener=opener, clock=iter([10, 14, 20, 23, 30, 33]).__next__)

    def test_ask_payload_and_actual_metrics(self):
        client = self.client(TAGS, ANSWER)
        result = client.ask("Привет")
        req = client.opener.open.call_args_list[1].args[0]
        self.assertEqual(json.loads(req.data), {"model": cli.MODEL, "messages": [{"role": "user", "content": "Привет"}],
                         "stream": False, "think": False, "options": {"temperature": .7, "num_predict": 512, "num_ctx": 4096}, "keep_alive": "1m"})
        self.assertEqual(client.opener.open.call_args.kwargs["timeout"], 120)
        self.assertEqual(result["answer"], "Ответ")
        self.assertEqual(result["elapsed_seconds"], 4)
        self.assertEqual(result["metrics"]["tokens_per_second"], 5)
        self.assertEqual(result["metrics"]["load_duration_seconds"], 1)

    def test_missing_model_prevents_generation(self):
        client = self.client({"models": []})
        with self.assertRaisesRegex(cli.ClientError, "отсутствует"):
            client.ask("Привет")
        self.assertEqual(client.opener.open.call_count, 1)

    def test_check_returns_capabilities_without_loading(self):
        client = self.client({"version": "0.40.2"}, TAGS, {"capabilities": ["completion", "thinking"], "details": {"quantization_level": "Q4_K_M"}}, {"models": []})
        result = client.check()
        self.assertIn("thinking", result["capabilities"])
        self.assertEqual(result["version"], "0.40.2")
        self.assertEqual(result["loaded_models"], [])
        self.assertFalse(any(call.args[0].full_url.endswith("/chat") for call in client.opener.open.call_args_list))

    def test_check_rejects_malformed_fields(self):
        for version, shown, ps in (
            ({"version": None}, {}, {"models": []}),
            ({"version": "0.40.2"}, {"capabilities": None}, {"models": []}),
            ({"version": "0.40.2"}, {"capabilities": [3]}, {"models": []}),
            ({"version": "0.40.2"}, {"details": []}, {"models": []}),
            ({"version": "0.40.2"}, {}, {"models": "bad"}),
            ({"version": "0.40.2"}, {}, {"models": [None]}),
        ):
            with self.subTest(version=version, shown=shown, ps=ps):
                with self.assertRaises(cli.ClientError):
                    self.client(version, TAGS, shown, ps).check()

    def test_unload_uses_empty_chat_and_checks_ps(self):
        client = self.client({"done": True}, {"models": []})
        self.assertTrue(client.unload()["unloaded"])
        self.assertEqual(json.loads(client.opener.open.call_args_list[0].args[0].data), {"model": cli.MODEL, "messages": [], "stream": False, "keep_alive": 0})

    def test_unload_detects_model_remaining(self):
        client = self.client({"done": True}, TAGS)
        with self.assertRaisesRegex(cli.ClientError, "ещё загружена"):
            client.unload()

    def test_demo_runs_in_order_after_unload(self):
        client = self.client(TAGS, {"done": True}, {"models": []}, ANSWER, {**ANSWER, "message": {"content": "1200 × 0.85 × 1.2 = 1224 ₽"}}, ANSWER)
        result = client.demo()
        self.assertEqual([run["load_label"] for run in result["runs"]], ["cold", "warm", "warm"])
        self.assertEqual([run["difficulty"] for run in result["runs"]], ["simple", "medium", "complex"])
        self.assertTrue(result["runs"][1]["math_check"]["mentions_expected_number"])
        self.assertEqual(client.opener.open.call_count, 6)

    def test_http_error_is_not_retried(self):
        for code in (404, 500):
            with self.subTest(code=code):
                client = self.client(error.HTTPError("http://127.0.0.1:11434/api/chat", code, "error", {}, None))
                with self.assertRaisesRegex(cli.ClientError, str(code)):
                    client.api("/api/chat", {})
                self.assertEqual(client.opener.open.call_count, 1)

    def test_timeout_and_connection_errors_are_clear(self):
        for exc, text in ((socket.timeout(), "120 секунд"), (error.URLError(socket.timeout()), "120 секунд"), (error.URLError(ConnectionRefusedError()), "недоступен")):
            with self.subTest(exc=exc):
                client = self.client(exc)
                with self.assertRaisesRegex(cli.ClientError, text):
                    client.api("/api/version")
                self.assertEqual(client.opener.open.call_count, 1)

    def test_invalid_json_or_shape(self):
        for raw in (b"", b"not json", b"\xff", b"[]", b"null", b'{"error":"failure"}', b'{"value":NaN}'):
            with self.subTest(raw=raw):
                with self.assertRaises(cli.ClientError):
                    self.client(raw).api("/api/version")

    def test_incomplete_or_empty_answer(self):
        for raw in ({"done": False, "message": {"content": "unfinished"}}, {"done": True, "message": {"content": " "}}, {"done": True, "message": None}):
            with self.subTest(raw=raw):
                with self.assertRaises(cli.ClientError):
                    self.client(raw).ask("Привет", preflight=False)

    def test_invalid_metrics_rejected_and_zero_speed_not_divided(self):
        for value in (-1, True, "3", float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(cli.ClientError):
                    cli.extract_metrics({"eval_duration": value})
        with self.assertRaises(cli.ClientError):
            cli.extract_metrics({"eval_count": 1.2})
        self.assertNotIn("tokens_per_second", cli.extract_metrics({"eval_count": 4, "eval_duration": 0}))
        self.assertEqual(cli.extract_metrics({}), {})

    def test_blank_prompt_has_no_network_calls(self):
        client = self.client()
        with self.assertRaises(cli.ClientError):
            client.ask("  ")
        client.opener.open.assert_not_called()

    def test_only_local_url_and_no_redirect(self):
        for url in ("https://localhost:11434", "http://example.org:11434", "http://127.0.0.1:80", "http://user@localhost:11434", "http://localhost:11434/api", "http://localhost:11434?x=1"):
            with self.subTest(url=url):
                with self.assertRaises(cli.ClientError):
                    cli.validate_url(url)
        self.assertEqual(cli.validate_url("http://[::1]:11434/"), "http://[::1]:11434")
        with self.assertRaisesRegex(cli.ClientError, "перенаправление"):
            cli.NoRedirect().redirect_request(None, None, 302, None, None, "https://example.org")
        real = cli.OllamaClient()
        self.assertFalse(any(isinstance(handler, request.ProxyHandler) and handler.proxies for handler in real.opener.handlers))


class CLITests(unittest.TestCase):
    def invoke(self, argv, factory):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = cli.main(argv, client_factory=factory)
        return status, out.getvalue(), err.getvalue()

    def test_json_cli_smoke(self):
        client = Mock()
        client.unload.return_value = {"command": "unload", "model": cli.MODEL, "unloaded": True}
        factory = Mock(return_value=client)
        status, output, errors = self.invoke(["unload", "--json"], factory)
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output)["unloaded"])
        self.assertEqual(errors, "")

    def test_error_and_interrupt_exit_codes(self):
        for error_value, expected in ((cli.ClientError("Недоступен"), 1), (KeyboardInterrupt(), 130)):
            client = Mock()
            client.check.side_effect = error_value
            status, _, errors = self.invoke(["check"], Mock(return_value=client))
            self.assertEqual(status, expected)
            self.assertTrue(errors)

    def test_output_outside_ignored_data_rejected_before_network(self):
        factory = Mock()
        status, _, errors = self.invoke(["check", "--output", "/tmp/day26-result.json"], factory)
        self.assertEqual(status, 1)
        self.assertIn("исключённой", errors)
        factory.assert_not_called()

    def test_output_file_matches_stdout_json(self):
        client = Mock()
        client.unload.return_value = {"command": "unload", "model": cli.MODEL, "unloaded": True}
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory).resolve()
            target = data_dir / "demo.json"
            with patch.object(cli, "DATA_DIR", data_dir):
                status, output, _ = self.invoke(["unload", "--json", "--output", str(target)], Mock(return_value=client))
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(target.read_text()), json.loads(output))

    def test_foreground_script_passes_fixed_resource_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "ollama"
            fake.write_text("#!/bin/sh\n/usr/bin/env\necho ARGS=$*\n")
            fake.chmod(0o755)
            env = {**os.environ, "OLLAMA_BIN": str(fake), "OLLAMA_NO_CLOUD": "0", "OLLAMA_HOST": "0.0.0.0:1"}
            script = Path(cli.__file__).parent / "run-server.sh"
            run = subprocess.run([str(script)], env=env, capture_output=True, text=True, check=True)
            values = dict(line.split("=", 1) for line in run.stdout.splitlines() if "=" in line)
            self.assertEqual(values["ARGS"], "serve")
            for name, value in {"OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_NO_CLOUD": "1", "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_CONTEXT_LENGTH": "4096", "OLLAMA_KEEP_ALIVE": "1m"}.items():
                self.assertEqual(values[name], value)
            self.assertEqual(values["OLLAMA_MODELS"], str(Path(cli.__file__).parent.parent / ".local-llm/models"))

    def test_formatted_answer_has_metrics(self):
        result = {"command": "ask", "answer": "Ответ", "elapsed_seconds": 4, "metrics": cli.extract_metrics(ANSWER)}
        text = cli.format_result(result)
        self.assertIn("Выходные токены: 10", text)
        self.assertIn("Загрузка: 1.000 с", text)


class ChatTests(unittest.TestCase):
    def client(self, responses):
        opener = Mock()
        opener.open.side_effect = [Response(value) if not isinstance(value, BaseException) else value for value in [TAGS, *responses]]
        return cli.OllamaClient(opener=opener, clock=itertools.count().__next__)

    def input_sequence(self, values):
        values = iter(values)
        def read(_):
            value = next(values, EOFError())
            if isinstance(value, BaseException):
                raise value
            return value
        return read

    def interact(self, client, inputs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = cli.interactive_chat(client, self.input_sequence(inputs))
        return result, out.getvalue()

    def chat_payloads(self, client):
        return [json.loads(call.args[0].data) for call in client.opener.open.call_args_list if call.args[0].full_url.endswith("/api/chat")]

    def answer(self, text):
        return {**ANSWER, "message": {"content": text}}

    def test_successful_turns_include_real_history_and_fixed_options(self):
        client = self.client([self.answer("Первый ответ"), self.answer("Второй ответ")])
        status, output = self.interact(client, ["Первый вопрос", "Второй вопрос", "/exit"])
        payloads = self.chat_payloads(client)
        self.assertEqual(status, 0)
        self.assertEqual(payloads[1]["messages"], [{"role": "user", "content": "Первый вопрос"}, {"role": "assistant", "content": "Первый ответ"}, {"role": "user", "content": "Второй вопрос"}])
        self.assertEqual(payloads[1]["options"], cli.OPTIONS)
        self.assertFalse(payloads[1]["think"])
        self.assertEqual(payloads[1]["keep_alive"], "1m")
        self.assertEqual(client.opener.open.call_count, 3)  # tags только при входе
        self.assertIn("Модель отвечает…", output)
        self.assertIn("Модель: Второй ответ", output)
        self.assertIn("ответ: 10 токенов", output)

    def test_clear_empty_and_slash_commands_do_not_generate(self):
        client = self.client([self.answer("Ответ 1"), self.answer("Ответ 2")])
        status, output = self.interact(client, ["", "   ", "/help", "/unknown", "Вопрос 1", "/clear", "Вопрос 2", "/exit"])
        self.assertEqual(status, 0)
        self.assertEqual(self.chat_payloads(client)[1]["messages"], [{"role": "user", "content": "Вопрос 2"}])
        self.assertEqual(len(self.chat_payloads(client)), 2)
        self.assertIn("История очищена", output)
        self.assertIn("Неизвестная команда", output)

    def test_failures_do_not_commit_history_or_retry(self):
        client = self.client([self.answer("Ответ 1"), {"done": True, "message": {"content": ""}}, self.answer("Ответ 3")])
        status, output = self.interact(client, ["Вопрос 1", "Неудачный вопрос", "Вопрос 3", "/exit"])
        self.assertEqual(status, 0)
        payloads = self.chat_payloads(client)
        self.assertEqual(len(payloads), 3)
        self.assertEqual([m["content"] for m in payloads[2]["messages"]], ["Вопрос 1", "Ответ 1", "Вопрос 3"])
        self.assertIn("пустой ответ", output)

    def test_api_failure_continues_to_next_input(self):
        client = self.client([socket.timeout(), self.answer("Ответ")])
        status, output = self.interact(client, ["Вопрос 1", "Вопрос 2", "/exit"])
        self.assertEqual(status, 0)
        self.assertIn("120 секунд", output)
        self.assertEqual(self.chat_payloads(client)[1]["messages"], [{"role": "user", "content": "Вопрос 2"}])

    def test_byte_budget_trims_whole_pair_and_notifies(self):
        client = self.client([self.answer("ю" * 500), self.answer("Готово")])
        _, output = self.interact(client, ["я" * 500, "x" * 1500, "/exit"])
        messages = self.chat_payloads(client)[1]["messages"]
        self.assertEqual(messages, [{"role": "user", "content": "x" * 1500}])
        self.assertIn("пропущено старых пар диалога: 1", output)

    def test_failed_trim_does_not_change_existing_history(self):
        client = self.client([self.answer("ю" * 500), socket.timeout(), self.answer("Готово")])
        self.interact(client, ["я" * 500, "x" * 1500, "Короткий вопрос", "/exit"])
        messages = self.chat_payloads(client)[2]["messages"]
        self.assertEqual([m["content"] for m in messages], ["я" * 500, "ю" * 500, "Короткий вопрос"])

    def test_pair_limit_keeps_four_latest_complete_pairs(self):
        client = self.client([self.answer(f"Ответ{i}") for i in range(6)])
        _, output = self.interact(client, [*[f"Вопрос{i}" for i in range(6)], "/exit"])
        payloads = self.chat_payloads(client)
        self.assertEqual([len(p["messages"]) for p in payloads], [1, 3, 5, 7, 9, 9])
        self.assertEqual(payloads[-1]["messages"][0], {"role": "user", "content": "Вопрос1"})
        self.assertIn("Из истории удалено старых пар диалога: 1", output)

    def test_oversized_current_rejected_before_http_boundary_accepted(self):
        client = self.client([self.answer("Ответ")])
        status, output = self.interact(client, ["я" * 1501, "я" * 1500, "/exit"])
        self.assertEqual(status, 0)
        self.assertIn("превышает 3000 UTF-8 байт", output)
        self.assertEqual(len(self.chat_payloads(client)), 1)
        self.assertEqual(len(self.chat_payloads(client)[0]["messages"][0]["content"].encode("utf-8")), 3000)

    def test_eof_and_input_interrupt_exit_zero(self):
        for ending in (EOFError(), KeyboardInterrupt()):
            with self.subTest(ending=ending):
                client = self.client([])
                status, output = self.interact(client, [ending])
                self.assertEqual(status, 0)
                self.assertEqual(client.opener.open.call_count, 1)
                self.assertIn("Чат завершён", output)

    def test_generation_interrupt_uses_existing_warning_and_exit_130(self):
        client = self.client([KeyboardInterrupt()])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch("builtins.input", self.input_sequence(["Вопрос"])):
            status = cli.main(["chat"], client_factory=Mock(return_value=client))
        self.assertEqual(status, 130)
        self.assertIn("Сервер может ещё завершать генерацию", err.getvalue())
        self.assertEqual(len(self.chat_payloads(client)), 1)

    def test_chat_never_saves_and_rejects_json_output_flags(self):
        client = self.client([self.answer("Ответ")])
        with patch.object(cli.Path, "write_text") as write, patch.object(cli, "output_path") as output_path, patch("builtins.input", self.input_sequence(["Вопрос", "/exit"])), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["chat"], client_factory=Mock(return_value=client)), 0)
        write.assert_not_called()
        output_path.assert_not_called()
        for option in (["chat", "--json"], ["chat", "--output", "data/chat.json"]):
            with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    cli.main(option, client_factory=Mock())
                self.assertEqual(exc.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
