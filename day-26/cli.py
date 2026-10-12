#!/usr/bin/env python3
"""Day 26: локальный клиент Ollama, с необязательным редактором TTY."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import socket
import sys
import time
from urllib import error, request
from urllib.parse import urlsplit

MODEL = "qwen3:4b-instruct-2507-q4_K_M"
BASE_URL = "http://127.0.0.1:11434"
TIMEOUT = 120
OPTIONS = {"temperature": 0.7, "num_predict": 512, "num_ctx": 4096}
DATA_DIR = Path(__file__).resolve().parent / "data"
CHAT_MAX_PAIRS = 4
CHAT_MAX_BYTES = 3000
PROMPTS = (
    ("simple", "Объясни, что такое локальная LLM, ровно двумя предложениями на русском языке."),
    ("medium", "Покупка стоит 1200 ₽. Сначала применяют скидку 15%, затем к полученной сумме добавляют налог 20%. Покажи вычисления и итоговую стоимость в рублях."),
    ("complex", "Дано: учебное CLI-приложение помогает искать ответы в README проекта. На ноутбуке уже установлены одна локальная LLM и локальный индекс документов. Интернет может отсутствовать. Индекс открыт только для чтения, изменения README не пересобирают его автоматически. Вопрос пользователя может оказаться вне документации.\nСоставь рекомендацию на русском, не более180слов, с тремя разделами: «Решение», «Ограничения», «Неизвестные данные». Опиши цепочку локальный retrieval → найденный контекст → локальная генерация; переиспользуй существующую модель и индекс. Не предлагай облачные модели или дополнительные веса. Опиши действия при отсутствии подтверждения в документах и устаревшем индексе. Размер индекса, доступная RAM, скорость, качество и порядок обновления индекса не сообщены: явно отметь недостающие данные, не выдумывай измерения или гарантии."),
)


class ClientError(Exception):
    """Понятная пользователю ошибка без скрытого повтора запроса."""


def reject_json_constant(value):
    raise ValueError(f"Недопустимая JSON-константа {value}")


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError("Локальный сервер вернул перенаправление; запрос остановлен.")


def validate_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}
                 and parts.port == 11434 and not parts.username and not parts.password
                 and parts.path in {"", "/"} and not parts.query and not parts.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ClientError("Адрес должен быть локальным HTTP URL на порту 11434: 127.0.0.1, localhost или [::1].")
    return value.rstrip("/")


class OllamaClient:
    def __init__(self, base_url: str = BASE_URL, opener=None, clock=time.monotonic):
        self.base_url = validate_url(base_url)
        self.opener = opener or request.build_opener(request.ProxyHandler({}), NoRedirect())
        self.clock = clock

    def api(self, path: str, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(self.base_url + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=TIMEOUT) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except error.HTTPError as exc:
            if exc.code == 404:
                raise ClientError(f"HTTP 404: модель {MODEL} или API не найдены. Проверьте check и ollama pull.") from exc
            raise ClientError(f"Локальный сервер вернул HTTP {exc.code}. Запрос не повторялся.") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ClientError(f"Локальная модель не ответила за {TIMEOUT} секунд. Запрос не повторялся.") from exc
        except error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ClientError(f"Локальная модель не ответила за {TIMEOUT} секунд. Запрос не повторялся.") from exc
            raise ClientError("Сервер Ollama недоступен. Запустите ./run-server.sh в отдельном терминале.") from exc
        except OSError as exc:
            raise ClientError("Соединение с локальным сервером прервано. Запрос не повторялся.") from exc
        if len(raw) > 4 * 1024 * 1024:
            raise ClientError("Ответ локального сервера слишком большой.")
        try:
            result = json.loads(raw, parse_constant=reject_json_constant)
        except (ValueError, UnicodeError) as exc:
            raise ClientError("Локальный сервер вернул некорректный или пустой JSON.") from exc
        if not isinstance(result, dict):
            raise ClientError("Локальный сервер вернул JSON неожиданного формата.")
        if result.get("error"):
            raise ClientError("Локальный сервер сообщил об ошибке. Проверьте модель командой check.")
        return result

    def require_model(self):
        tags = self.api("/api/tags")
        models = tags.get("models")
        if not isinstance(models, list):
            raise ClientError("Некорректный список моделей в /api/tags.")
        if not any(isinstance(item, dict) and MODEL in {item.get("name"), item.get("model")} for item in models):
            raise ClientError(f"Модель {MODEL} отсутствует. Загрузите её командой ollama pull {MODEL}.")
        return next(item for item in models if isinstance(item, dict) and MODEL in {item.get("name"), item.get("model")})

    def check(self):
        version = self.api("/api/version")
        model = self.require_model()
        shown = self.api("/api/show", {"model": MODEL})
        ps = self.api("/api/ps")
        if not isinstance(version.get("version"), str) or not version["version"].strip():
            raise ClientError("Некорректная версия сервера в /api/version.")
        capabilities = shown.get("capabilities", [])
        if not isinstance(capabilities, list) or not all(isinstance(value, str) for value in capabilities):
            raise ClientError("Некорректный список capabilities в /api/show.")
        if not isinstance(shown.get("details", {}), dict) or not isinstance(shown.get("model_info", {}), dict) or not isinstance(shown.get("parameters", ""), str):
            raise ClientError("Некорректные сведения о модели в /api/show.")
        loaded = ps.get("models")
        if not isinstance(loaded, list) or not all(isinstance(value, dict) for value in loaded):
            raise ClientError("Некорректный список загруженных моделей в /api/ps.")
        return {"command": "check", "url": self.base_url, "model": MODEL, "version": version.get("version"),
                "installed_model": model, "details": shown.get("details", {}),
                "capabilities": shown.get("capabilities", []), "model_parameters": shown.get("parameters", ""),
                "model_info": shown.get("model_info", {}), "loaded_models": ps.get("models", []),
                "request_defaults": {"options": OPTIONS.copy(), "think": False, "stream": False, "keep_alive": "1m", "timeout_seconds": TIMEOUT}}

    def ask(self, prompt: str, *, preflight=True, label=None):
        if not prompt.strip():
            raise ClientError("Введите непустой запрос.")
        if preflight:
            self.require_model()
        result = self.generate_messages([{"role": "user", "content": prompt}])
        return {"command": "ask", "model": MODEL, "load_label": label, "prompt": prompt, **result}

    def generate_messages(self, messages):
        payload = {"model": MODEL, "messages": messages,
                   "stream": False, "think": False, "options": OPTIONS.copy(), "keep_alive": "1m"}
        started = self.clock()
        raw = self.api("/api/chat", payload)
        elapsed = self.clock() - started
        message = raw.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if raw.get("done") is not True:
            raise ClientError("Локальный сервер не завершил ответ (done != true).")
        if not isinstance(content, str) or not content.strip():
            raise ClientError("Модель вернула пустой ответ.")
        metrics = extract_metrics(raw)
        return {"answer": content.strip(), "elapsed_seconds": elapsed, "metrics": metrics,
                "payload": payload, "raw_response": raw}

    def unload(self):
        raw = self.api("/api/chat", {"model": MODEL, "messages": [], "stream": False, "keep_alive": 0})
        if raw.get("done") is not True:
            raise ClientError("Сервер не подтвердил завершение выгрузки.")
        models = self.api("/api/ps").get("models")
        if not isinstance(models, list):
            raise ClientError("Не удалось проверить выгрузку через /api/ps.")
        if any(isinstance(item, dict) and MODEL in {item.get("name"), item.get("model")} for item in models):
            raise ClientError("Модель ещё загружена: проверьте другие запросы к серверу.")
        return {"command": "unload", "model": MODEL, "unloaded": True, "loaded_models": models}

    def demo(self):
        self.require_model()
        self.unload()
        runs = []
        for index, (difficulty, prompt) in enumerate(PROMPTS):
            result = self.ask(prompt, preflight=False, label="cold" if index == 0 else "warm")
            result["difficulty"] = difficulty
            if difficulty == "medium":
                result["math_check"] = {"expected_rubles": 1224, "mentions_expected_number": "1224" in result["answer"].replace(" ", "").replace("\u00a0", ""),
                                        "limitation": "Наличие числа — эвристика; вычисления требуют ручной проверки."}
            runs.append(result)
        return {"command": "demo", "model": MODEL, "runs": runs, "quality_note": "Правильность и соблюдение условий оцениваются вручную. Cold и warm измеряются отдельно."}


def extract_metrics(raw):
    result = {}
    for key in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration", "prompt_eval_count", "eval_count"):
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ClientError(f"Некорректная метрика {key} в ответе модели.")
        if key.endswith("count") and (not isinstance(value, int)):
            raise ClientError(f"Некорректное число токенов {key}.")
        result[key] = value
        if key.endswith("duration"):
            result[key + "_seconds"] = value / 1_000_000_000
    if result.get("eval_duration", 0) > 0 and result.get("eval_count", 0) > 0:
        result["tokens_per_second"] = result["eval_count"] / result["eval_duration_seconds"]
    return result


def format_result(result):
    command = result["command"]
    if command == "check":
        defaults = result["request_defaults"]
        return (f"Сервер: {result['url']}\nOllama: {result['version']}\nМодель: {result['model']}\n"
                f"Сведения о модели: {json.dumps(result['details'], ensure_ascii=False)}\n"
                f"Возможности: {', '.join(result['capabilities'])}\n"
                f"Настройки запросов: {json.dumps(defaults, ensure_ascii=False)}\n"
                f"Загруженные модели (/api/ps): {json.dumps(result['loaded_models'], ensure_ascii=False)}")
    if command == "unload":
        return "Модель выгружена из памяти. Веса сохранены на диске."
    if command == "demo":
        return "\n\n".join(f"{run['difficulty']} / {run['load_label']}\n{format_result(run)}" for run in result["runs"]) + "\n\n" + result["quality_note"]
    metrics = result["metrics"]
    lines = [result["answer"], "", f"Время HTTP-запроса: {result['elapsed_seconds']:.3f} с"]
    for key, label in (("load_duration_seconds", "Загрузка"), ("eval_duration_seconds", "Генерация")):
        if key in metrics:
            lines.append(f"{label}: {metrics[key]:.3f} с")
    for key, label in (("prompt_eval_count", "Входные токены"), ("eval_count", "Выходные токены")):
        if key in metrics:
            lines.append(f"{label}: {metrics[key]}")
    if "tokens_per_second" in metrics:
        lines.append(f"Скорость генерации: {metrics['tokens_per_second']:.2f} токенов/с")
    return "\n".join(lines)



def prepare_chat_messages(history, prompt):
    """Собирает ограниченный запрос, не меняя подтверждённую историю."""
    if len(prompt.encode("utf-8")) > CHAT_MAX_BYTES:
        raise ClientError(f"Вопрос превышает {CHAT_MAX_BYTES} UTF-8 байт. Сократите его; запрос не отправлен.")
    retained = list(history)
    removed = 0
    while len(retained) > CHAT_MAX_PAIRS * 2:
        del retained[:2]
        removed += 1
    while sum(len(message["content"].encode("utf-8")) for message in retained) + len(prompt.encode("utf-8")) > CHAT_MAX_BYTES:
        del retained[:2]
        removed += 1
    return retained + [{"role": "user", "content": prompt}], removed


def chat_metrics(result):
    metrics = result["metrics"]
    parts = [f"Время: {result['elapsed_seconds']:.2f} с"]
    if "load_duration_seconds" in metrics:
        parts.append(f"загрузка: {metrics['load_duration_seconds']:.2f} с")
    if "eval_count" in metrics:
        parts.append(f"ответ: {metrics['eval_count']} токенов")
    if "prompt_eval_count" in metrics:
        parts.append(f"вход: {metrics['prompt_eval_count']} токенов")
    if "tokens_per_second" in metrics:
        parts.append(f"{metrics['tokens_per_second']:.2f} токенов/с")
    return " | ".join(parts)


def interactive_chat(client, read_input=None):
    client.require_model()
    from prompt_presets import prompts_help
    from terminal_input import EDITOR_HELP, select_reader
    print(f"Локальный чат • {MODEL}\nВведите свой вопрос. /help — команды; /prompts — вопросы. История хранится только в памяти.")
    read_input = select_reader(read_input)
    history = []
    while True:
        try:
            prompt = read_input("Вы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nЧат завершён.")
            return 0
        if not prompt:
            continue
        if prompt == "/exit":
            print("Чат завершён.")
            return 0
        if prompt == "/help":
            print("/help — показать команды; /prompts — вопросы; /clear — очистить историю; /exit — завершить чат.")
            print("В интерактивном редакторе:\n" + EDITOR_HELP)
            continue
        if prompt == "/prompts":
            print(prompts_help())
            continue
        if prompt == "/clear":
            history.clear()
            print("История очищена.")
            continue
        if prompt.startswith("/"):
            print("Неизвестная команда. /help — список команд.")
            continue
        try:
            messages, removed = prepare_chat_messages(history, prompt)
            if removed:
                print(f"Для этого запроса пропущено старых пар диалога: {removed} (лимит истории).")
            print("Модель отвечает…", flush=True)
            result = client.generate_messages(messages)
        except ClientError as exc:
            print(f"Ошибка: {exc}")
            continue
        # Старая история заменяется только после полностью проверенного ответа.
        history = messages + [{"role": "assistant", "content": result["answer"]}]
        overflow = max(0, len(history) // 2 - CHAT_MAX_PAIRS)
        if overflow:
            del history[:overflow * 2]
        print(f"Модель: {result['answer']}")
        print(chat_metrics(result))
        if overflow:
            print(f"Из истории удалено старых пар диалога: {overflow} (лимит {CHAT_MAX_PAIRS} пар).")


def output_path(value):
    path = Path(value).expanduser().resolve()
    if path == DATA_DIR or not path.is_relative_to(DATA_DIR):
        raise ClientError(f"Результаты можно сохранять только в исключённой из Git папке {DATA_DIR}.")
    return path


def main(argv=None, client_factory=OllamaClient):
    parser = argparse.ArgumentParser(description="Day26: запросы только к локальной Ollama")
    parser.add_argument("--url", default=BASE_URL, help="Локальный URL, порт 11434")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "ask", "demo", "unload"):
        child = sub.add_parser(command)
        child.add_argument("--json", action="store_true", help="Вывести полный результат как JSON")
        child.add_argument("--output", help="Сохранить JSON в day-26/data/ (исключено из Git)")
        if command == "ask":
            child.add_argument("prompt")
    sub.add_parser("chat", help="Общайтесь со своими вопросами, история только в памяти")
    args = parser.parse_args(argv)
    try:
        if args.command == "chat":
            return interactive_chat(client_factory(args.url))
        save_to = output_path(args.output) if args.output else None
        client = client_factory(args.url)
        result = client.ask(args.prompt) if args.command == "ask" else getattr(client, args.command)()
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if save_to:
            save_to.parent.mkdir(parents=True, exist_ok=True)
            save_to.write_text(encoded + "\n", encoding="utf-8")
        print(encoded if args.json else format_result(result))
        return 0
    except KeyboardInterrupt:
        print("Запрос прерван. Сервер может ещё завершать генерацию; повтор не выполнялся.", file=sys.stderr)
        return 130
    except (ClientError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
