"""Редактор для TTY. prompt_toolkit импортируется только при его создании."""
from pathlib import Path
import sys

from prompt_presets import PRESETS


DEPENDENCIES = Path(__file__).resolve().parents[1] / ".local-llm" / "cli-deps"
INSTALL_HINT = (
    "Редактор недоступен: используется обычный ввод. Для установки из day-26: "
    "python3 -m pip install --no-cache-dir --target ../.local-llm/cli-deps -r requirements.txt"
)
EDITOR_HELP = (
    "/1–/4 + Tab или Enter — подставить вопрос; следующий Enter — отправить.\n"
    "←/→ — символы; ↑/↓ — строки, разделённые переводом строки.\n"
    "Автоматический перенос длинного текста не создаёт отдельную строку ввода.\n"
    "Ctrl+←/→, Option+←/→ (если терминал передаёт Meta)\n"
    "или Esc, затем b/f — слова.\n"
    "Home/End или Ctrl+A/E — начало/конец строки; Ctrl+Home/End — весь текст.\n"
    "Esc, затем Enter или Ctrl+O — новая строка. /prompts — список вопросов."
)


def create_session(*, input=None, output=None):
    # Проектная установка не меняет глобальное Python-окружение.
    if DEPENDENCIES.is_dir() and str(DEPENDENCIES) not in sys.path:
        sys.path.insert(0, str(DEPENDENCIES))
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import DummyHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.key_binding.bindings.named_commands import (
        backward_word, forward_word,
    )

    keys = KeyBindings()

    def expand(buffer):
        preset = PRESETS.get(buffer.text.strip())
        if preset is None:
            return False
        buffer.text = preset[1]
        buffer.cursor_position = len(buffer.text)
        return True

    @keys.add("tab")
    def complete(event):
        expand(event.current_buffer)

    @keys.add("enter")
    def submit(event):
        if not expand(event.current_buffer):
            event.current_buffer.validate_and_handle()

    @keys.add("escape", "enter")
    @keys.add("c-o")
    def newline(event):
        event.current_buffer.insert_text("\n")

    @keys.add("c-left")
    @keys.add("escape", "left")
    @keys.add("escape", "b")
    def previous_word(event):
        backward_word(event)

    @keys.add("c-right")
    @keys.add("escape", "right")
    @keys.add("escape", "f")
    def next_word(event):
        forward_word(event)

    @keys.add("home")
    @keys.add("c-a")
    def line_start(event):
        buffer = event.current_buffer
        buffer.cursor_position += buffer.document.get_start_of_line_position()

    @keys.add("end")
    @keys.add("c-e")
    def line_end(event):
        buffer = event.current_buffer
        buffer.cursor_position += buffer.document.get_end_of_line_position()

    @keys.add("c-home")
    def start(event):
        event.current_buffer.cursor_position = 0

    @keys.add("c-end")
    def end(event):
        event.current_buffer.cursor_position = len(event.current_buffer.text)

    return PromptSession(
        multiline=True, wrap_lines=True, key_bindings=keys,
        history=DummyHistory(), input=input, output=output,
        enable_open_in_editor=False,
    )


def select_reader(read_input=None):
    """Внедрённый ввод и pipe не требуют сторонних библиотек."""
    if read_input is not None:
        return read_input
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return input
    try:
        session = create_session()
    except ImportError:
        print(INSTALL_HINT, file=sys.stderr)
        return input
    print(EDITOR_HELP)
    return session.prompt
