import asyncio
import ast
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli
import prompt_presets
import terminal_input

# Тесты редактора используют ту же локальную установку, что и приложение.
if terminal_input.DEPENDENCIES.is_dir():
    sys.path.insert(0, str(terminal_input.DEPENDENCIES))
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput


class TerminalEditorTests(unittest.IsolatedAsyncioTestCase):
    async def run_keys(self, keys):
        with create_pipe_input() as pipe:
            session = terminal_input.create_session(input=pipe, output=DummyOutput())
            task = asyncio.create_task(session.prompt_async("Вы: "))
            pipe.send_text(keys)
            return await asyncio.wait_for(task, 2)

    async def wait_for_text(self, session, expected):
        async def changed():
            while session.default_buffer.text != expected:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(changed(), 2)

    async def test_tab_and_enter_expand_but_do_not_submit(self):
        for shortcut in prompt_presets.PRESETS:
            for trigger in ("\t", "\r"):
                with self.subTest(shortcut=shortcut, trigger=trigger):
                    with create_pipe_input() as pipe:
                        session = terminal_input.create_session(input=pipe, output=DummyOutput())
                        task = asyncio.create_task(session.prompt_async("Вы: "))
                        pipe.send_text(" " + shortcut + " " + trigger)
                        expected = prompt_presets.PRESETS[shortcut][1]
                        await self.wait_for_text(session, expected)
                        self.assertFalse(task.done())
                        self.assertEqual(session.default_buffer.cursor_position, len(expected))
                        pipe.send_text("\t\x1b[D!\r")
                        self.assertEqual(await asyncio.wait_for(task, 2), expected[:-1] + "!" + expected[-1])

    async def test_unknown_alias_and_embedded_code_not_expanded(self):
        for text in ("/5", "Что значит /1?", "Обычный вопрос"):
            with self.subTest(text=text):
                self.assertEqual(await self.run_keys(text + "\t\r"), text)

    async def test_arrows_edit_characters_and_backspace(self):
        self.assertEqual(await self.run_keys("абв\x1b[D\x1b[D\x1b[C\x7f!\r"), "а!в")

    async def test_control_and_escape_word_navigation(self):
        for left, right in (("\x1b[1;5D", "\x1b[1;5C"), ("\x1bb", "\x1bf"), ("\x1b\x1b[D", "\x1b\x1b[C")):
            with self.subTest(left=left):
                # Назад на начало «два», вперёд до его конца.
                self.assertEqual(await self.run_keys("один два" + left + "!" + right + "?\r"), "один !два?")

    async def test_newline_and_vertical_navigation(self):
        for newline in ("\x1b\r", "\x0f"):
            with self.subTest(newline=newline):
                self.assertEqual(await self.run_keys("abc" + newline + "def\x1b[A!\x1b[B?\r"), "abc!\ndef?")

    async def test_wrapped_text_is_one_logical_line(self):
        # DummyOutput имеет ширину 80: длинный текст переносится визуально,
        # но builtin Up/Down перескакивают только между явными переводами строки.
        text = "я" * 180
        self.assertEqual(await self.run_keys(text + "\x1b[A!\r"), text + "!")

    async def test_line_home_end_and_control_a_e(self):
        for home, end in (("\x1b[H", "\x1b[F"), ("\x01", "\x05")):
            with self.subTest(home=home):
                self.assertEqual(await self.run_keys("abc\x0fdef" + home + "!" + end + "?\r"), "abc\n!def?")

    async def test_control_home_end_move_across_entire_buffer(self):
        self.assertEqual(await self.run_keys("abc\x0fdef\x1b[1;5H!\x1b[1;5F?\r"), "!abc\ndef?")

    async def test_bracketed_paste_keeps_newlines_without_submission(self):
        with create_pipe_input() as pipe:
            session = terminal_input.create_session(input=pipe, output=DummyOutput())
            task = asyncio.create_task(session.prompt_async("Вы: "))
            pipe.send_text("\x1b[200~строка 1\nстрока 2\n/exit\x1b[201~")
            expected = "строка 1\nстрока 2\n/exit"
            await self.wait_for_text(session, expected)
            self.assertFalse(task.done())
            pipe.send_text("\r")
            self.assertEqual(await asyncio.wait_for(task, 2), expected)

    async def test_eof_and_control_c_preserve_exceptions(self):
        for keys, exception in (("\x04", EOFError), ("\x03", KeyboardInterrupt)):
            with self.subTest(keys=keys):
                # KeyboardInterrupt из Task прерывает loop; перехват внутри coroutine.
                with create_pipe_input() as pipe:
                    session = terminal_input.create_session(input=pipe, output=DummyOutput())
                    async def read():
                        try:
                            await session.prompt_async("Вы: ")
                        except BaseException as exc:
                            return exc
                    task = asyncio.create_task(read())
                    pipe.send_text(keys)
                    self.assertIsInstance(await asyncio.wait_for(task, 2), exception)

    async def test_no_previous_question_recalled_in_same_session(self):
        with create_pipe_input() as pipe:
            session = terminal_input.create_session(input=pipe, output=DummyOutput())
            self.assertIsInstance(session.history, DummyHistory)
            first = asyncio.create_task(session.prompt_async("Вы: "))
            pipe.send_text("Личный вопрос\r")
            self.assertEqual(await asyncio.wait_for(first, 2), "Личный вопрос")
            second = asyncio.create_task(session.prompt_async("Вы: "))
            pipe.send_text("\x1b[A\r")
            self.assertEqual(await asyncio.wait_for(second, 2), "")
            self.assertEqual(session.history.get_strings(), [])


class InputSelectionTests(unittest.TestCase):
    def test_injected_and_non_tty_input_do_not_load_library(self):
        reader = Mock()
        with patch.object(terminal_input, "create_session") as create:
            self.assertIs(terminal_input.select_reader(reader), reader)
            with patch.object(terminal_input.sys.stdin, "isatty", return_value=False):
                self.assertIs(terminal_input.select_reader(), input)
            create.assert_not_called()

    def test_tty_selects_session_and_missing_dependency_has_basic_fallback(self):
        session = Mock()
        with patch.object(terminal_input.sys.stdin, "isatty", return_value=True), patch.object(terminal_input.sys.stdout, "isatty", return_value=True), contextlib.redirect_stdout(io.StringIO()):
            # redirect_stdout заменяет stdout, поэтому patch применяем на новом объекте.
            with patch.object(terminal_input.sys.stdout, "isatty", return_value=True), patch.object(terminal_input, "create_session", return_value=session):
                self.assertEqual(terminal_input.select_reader(), session.prompt)
            error = io.StringIO()
            with patch.object(terminal_input.sys.stdout, "isatty", return_value=True), patch.object(terminal_input, "create_session", side_effect=ImportError()), contextlib.redirect_stderr(error):
                self.assertIs(terminal_input.select_reader(), input)
            self.assertIn("--target ../.local-llm/cli-deps", error.getvalue())

    def test_prompts_and_help_do_not_generate_or_write(self):
        client = Mock()
        inputs = iter(["/prompts", "/help", "/exit"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(Path, "write_text") as write:
            self.assertEqual(cli.interactive_chat(client, lambda _: next(inputs)), 0)
        client.generate_messages.assert_not_called()
        client.require_model.assert_called_once_with()
        write.assert_not_called()
        for code, (title, _) in prompt_presets.PRESETS.items():
            self.assertIn(f"{code} — {title}", output.getvalue())
        self.assertIn("следующий Enter", output.getvalue())

    def test_importing_cli_keeps_other_commands_dependency_free(self):
        result = subprocess.run([sys.executable, "-S", "-c", "import cli, sys; assert 'prompt_toolkit' not in sys.modules; assert 'terminal_input' not in sys.modules"], cwd=Path(cli.__file__).parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_presets_match_original_days_without_importing_apps(self):
        root = Path(cli.__file__).resolve().parents[1]
        tree = ast.parse((root / "day-05/app.py").read_text())
        original = next(ast.literal_eval(statement.value) for statement in tree.body if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "TASK_PRESETS" for target in statement.targets))
        self.assertEqual(prompt_presets.PRESETS["/2"][1], original[1]["prompt"])
        self.assertEqual(prompt_presets.PRESETS["/3"][1], original[2]["prompt"])
        self.assertEqual(prompt_presets.PRESETS["/4"][1], original[0]["prompt"])
        self.assertIn(prompt_presets.PRESETS["/1"][1], (root / "day-03/static/app.js").read_text())


if __name__ == "__main__":
    unittest.main()
