"""No provider selection, keys, environment fallback, redirects or retries."""
import math

import httpx

from .errors import APIError

MODEL = "qwen3:4b-instruct-2507-q4_K_M"
URL = "http://127.0.0.1:11434"
PARAMETERS = {"temperature": .7, "num_predict": 1024, "num_ctx": 4096, "keep_alive": "1m"}


async def provider_json(client, path, payload=None):
    try:
        response = await client.request("GET" if payload is None else "POST", path, json=payload)
    except httpx.TimeoutException:
        raise APIError(504, "timeout", "Локальная модель не ответила вовремя. Повторите вопрос самостоятельно.") from None
    except httpx.RequestError:
        raise APIError(503, "unavailable", "Ollama недоступен. Проверьте локальный сервер.") from None
    if response.is_error or response.is_redirect:
        raise APIError(502, "provider_error", "Локальный сервер модели вернул ошибку.")
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        raise APIError(502, "invalid_response", "Локальная модель вернула некорректный JSON.") from None
    if not isinstance(data, dict) or "error" in data:
        raise APIError(502, "invalid_response", "Получен некорректный ответ локальной модели.")
    return data


async def require_model(client):
    data = await provider_json(client, "/api/tags")
    models = data.get("models")
    if not isinstance(models, list) or any(not isinstance(item, dict) for item in models):
        raise APIError(502, "invalid_response", "Ollama вернул некорректный список моделей.")
    if not any(item.get("name") == MODEL or item.get("model") == MODEL for item in models):
        raise APIError(503, "missing_model", "Выбранная локальная модель отсутствует. Используйте установку Day26.")


async def generate(client, messages):
    await require_model(client)
    data = await provider_json(client, "/api/chat", {
        "model": MODEL, "messages": messages, "stream": False, "think": False,
        "keep_alive": PARAMETERS["keep_alive"],
        "options": {key: value for key, value in PARAMETERS.items() if key != "keep_alive"},
    })
    message = data.get("message")
    if data.get("done") is not True or not isinstance(message, dict) or not isinstance(message.get("content"), str) or not message["content"].strip():
        raise APIError(502, "invalid_response", "Модель вернула пустой или незавершённый ответ.")
    values = {}
    for key in ("load_duration", "eval_duration", "prompt_eval_count", "eval_count"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (key in {"eval_duration", "prompt_eval_count", "eval_count"} and value == 0):
            raise APIError(502, "invalid_response", "Модель вернула некорректные метрики.")
        try:
            if not math.isfinite(value):
                raise OverflowError()
        except OverflowError:
            raise APIError(502, "invalid_response", "Модель вернула некорректные метрики.") from None
        values[key] = value
    seconds = values["eval_duration"] / 1e9
    metrics = {"load_seconds": values["load_duration"] / 1e9, "generation_seconds": seconds,
               "prompt_tokens": values["prompt_eval_count"], "output_tokens": values["eval_count"],
               "tokens_per_second": values["eval_count"] / seconds}
    if not all(math.isfinite(value) for value in metrics.values()):
        raise APIError(502, "invalid_response", "Модель вернула некорректные метрики.")
    return message["content"].strip(), metrics, data.get("done_reason") == "length"
