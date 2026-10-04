"""DeepSeek Chat Completions, using one reusable connection pool per app."""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parents[1]
URL = "https://api.deepseek.com/chat/completions"
TEMPERATURE = 0
MAX_TOKENS = 400
THINKING = "disabled"
RETRY_DELAYS = (0.5, 1.5)
TIMEOUT = httpx.Timeout(connect=20.0, read=75.0, write=20.0, pool=20.0)


class LLMError(Exception):
    pass


def config_value(name: str) -> str | None:
    if os.getenv(name):
        return os.environ[name]
    path = HERE / ".env"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        found_name, separator, value = line.partition("=")
        if separator and found_name.strip() == name:
            return value.strip().strip('"').strip("'") or None
    return None


MODEL = config_value("DEEPSEEK_MODEL") or "deepseek-flash"


class DeepSeekClient:
    def __init__(self, client: httpx.Client | None = None, sleep=time.sleep):
        self.client = client if client is not None else httpx.Client(timeout=TIMEOUT)
        self.sleep = sleep

    def close(self) -> None:
        self.client.close()

    def __call__(self, messages: list[dict]) -> dict:
        key = config_value("DEEPSEEK_API_KEY")
        if not key:
            raise LLMError("Нет DEEPSEEK_API_KEY в day-22/.env")
        body = {"model": MODEL, "messages": messages, "temperature": TEMPERATURE,
                "max_tokens": MAX_TOKENS, "thinking": {"type": THINKING}, "stream": False}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                response = self.client.post(URL, headers=headers, json=body)
                if response.status_code in (429, 503) and attempt < len(RETRY_DELAYS):
                    self.sleep(RETRY_DELAYS[attempt])
                    continue
                response.raise_for_status()
                payload = response.json()
                answer = payload["choices"][0]["message"]["content"]
                if not isinstance(answer, str) or not answer.strip():
                    raise LLMError("Модель вернула пустой ответ")
                return {"answer": answer, "usage": payload.get("usage")}
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt < len(RETRY_DELAYS):
                    self.sleep(RETRY_DELAYS[attempt])
                    continue
                raise LLMError("Не удалось подключиться к DeepSeek после повторов") from exc
            except httpx.ReadTimeout as exc:
                # The response may have been generated and billed; do not send it again.
                raise LLMError("Превышено время ожидания ответа DeepSeek") from exc
            except httpx.HTTPStatusError as exc:
                raise LLMError(f"DeepSeek вернул HTTP {exc.response.status_code}") from exc
            except httpx.TimeoutException as exc:
                raise LLMError("Превышено время ожидания передачи запроса DeepSeek") from exc
            except (httpx.RequestError, KeyError, IndexError, TypeError, ValueError) as exc:
                raise LLMError("Не удалось получить ответ DeepSeek") from exc
