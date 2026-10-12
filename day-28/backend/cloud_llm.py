"""Explicit DeepSeek comparison only; no fallback, redirects or retries."""
import os
import math
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .errors import APIError

URL = "https://api.deepseek.com/chat/completions"
HERE = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class CloudConfig:
    api_key: str = field(default="", repr=False)
    model: str = "deepseek-flash"


def load_config():
    values = {}
    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"):
        if os.environ.get(name, "").strip():
            values[name] = os.environ[name].strip()
    for path in (HERE / ".env", HERE.parent / "day-24/.env"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            name, separator, value = line.removeprefix("export ").partition("=")
            name = name.strip()
            if separator and name in {"DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"} and name not in values:
                value = value.strip().strip('"').strip("'")
                if value:
                    values[name] = value
    return CloudConfig(api_key=values.get("DEEPSEEK_API_KEY", ""), model=values.get("DEEPSEEK_MODEL", "deepseek-flash"))


def comparison_state(config):
    return {"available": bool(config.api_key), "model": config.model, "temperature": .7, "max_tokens": 1024,
            "thinking": "disabled", "message": "DeepSeek настроен; соединение ещё не проверялось. Запрос выполняется только по кнопке сравнения." if config.api_key else "DeepSeek не настроен. Локальные вопросы работают без облака."}


async def generate(client, config, messages):
    try:
        response = await client.post(URL, json={"model": config.model, "messages": messages, "stream": False,
                                     "thinking": {"type": "disabled"}, "temperature": .7, "max_tokens": 1024},
                                     headers={"Authorization": "Bearer " + config.api_key})
    except httpx.TimeoutException:
        raise APIError(504, "cloud_timeout", "DeepSeek не ответил вовремя. Автоматический повтор не выполнялся.") from None
    except (httpx.RequestError, ValueError, UnicodeError):
        raise APIError(503, "cloud_unavailable", "Не удалось подключиться к DeepSeek. Локальный ответ сохраняется.") from None
    if response.is_error or response.is_redirect:
        raise APIError(502, "cloud_provider_error", "DeepSeek вернул ошибку. Автоматический повтор не выполнялся.")
    try:
        data = response.json()
        choices = data["choices"]
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError()
        answer = choices[0]["message"]["content"]
        finish = choices[0]["finish_reason"]
        usage = data["usage"]
        if not isinstance(answer, str) or not answer.strip() or finish not in {"stop", "length"} or not isinstance(usage, dict):
            raise ValueError()
        answer.encode("utf-8")
        for name in ("prompt_tokens", "completion_tokens"):
            if type(usage.get(name)) is not int or usage[name] <= 0 or not math.isfinite(usage[name]):
                raise ValueError()
        if "total_tokens" in usage and (type(usage["total_tokens"]) is not int or usage["total_tokens"] < 0 or not math.isfinite(usage["total_tokens"])):
            raise ValueError()
        model = data.get("model", config.model)
        if not isinstance(model, str) or not model.strip():
            raise ValueError()
        model.encode("utf-8")
        return {"answer": answer.strip(), "model": model.strip(), "truncated": finish == "length",
                "prompt_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]}
    except (ValueError, UnicodeError, KeyError, TypeError, IndexError, OverflowError):
        raise APIError(502, "cloud_invalid_response", "DeepSeek вернул пустой или некорректный ответ.") from None
