"""Fixed loopback Ollama. No cloud, proxy, redirects, fallback or retry."""
import math

import httpx

from .errors import APIError
from .profiles import OPTION_KEYS, effective_parameters

MODEL = "qwen3:4b-instruct-2507-q4_K_M"
URL = "http://127.0.0.1:11434"
# Native stops from the installed Qwen model; settings.stop holds additional user stops.
NATIVE_STOPS = ("<|im_start|>", "<|im_end|>")


def number(value, positive=False, integer=False):
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        return None
    try:
        return value if math.isfinite(value) and (value > 0 if positive else value >= 0) else None
    except OverflowError:
        return None


async def provider_json(client, path, payload=None):
    try:
        response = await client.request("GET" if payload is None else "POST", path, json=payload)
    except httpx.TimeoutException:
        raise APIError(504, "timeout", "Ollama не ответил вовремя. Повторите запрос самостоятельно.") from None
    except httpx.RequestError:
        raise APIError(503, "unavailable", "Ollama недоступен. Проверьте локальный сервер.") from None
    if response.is_error or response.is_redirect:
        raise APIError(502, "provider_error", "Локальный сервер модели вернул ошибку.")
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        raise APIError(502, "invalid_response", "Ollama вернул некорректный JSON.") from None
    if not isinstance(data, dict) or "error" in data:
        raise APIError(502, "invalid_response", "Получен некорректный ответ Ollama.")
    return data


async def require_model(client):
    models = (await provider_json(client, "/api/tags")).get("models")
    if not isinstance(models, list) or any(not isinstance(item, dict) for item in models):
        raise APIError(502, "invalid_response", "Некорректный список моделей Ollama.")
    if not any(item.get("name") == MODEL or item.get("model") == MODEL for item in models):
        raise APIError(503, "missing_model", "Выбранная локальная модель отсутствует. Используйте установку Day26.")


def payload(parameters, messages):
    effective = effective_parameters(parameters)
    options = {key: effective[key] for key in OPTION_KEYS}
    options["stop"] = list(dict.fromkeys([*NATIVE_STOPS, *parameters["stop"]]))
    request = {"model": MODEL, "messages": messages, "stream": False, "think": False,
               "keep_alive": "1m", "options": options}
    if messages and effective.get("format") is not None:
        request["format"] = effective["format"]
    return request


async def preload(client, parameters):
    data = await provider_json(client, "/api/chat", payload(parameters, []))
    if data.get("done") is not True:
        raise APIError(502, "invalid_response", "Ollama не подтвердил предварительную загрузку модели.")
    duration = number(data.get("load_duration"), integer=True)
    return duration / 1e9 if duration is not None else None


async def generate(client, messages, parameters):
    data = await provider_json(client, "/api/chat", payload(parameters, messages))
    message = data.get("message")
    if data.get("done") is not True or not isinstance(message, dict) or not isinstance(message.get("content"), str) or not message["content"].strip():
        raise APIError(502, "invalid_response", "Модель вернула пустой или незавершённый ответ.")
    metrics = {}
    for key in ("load_duration", "eval_duration", "prompt_eval_count", "eval_count"):
        value = number(data.get(key), positive=key != "load_duration", integer=True)
        if value is None:
            raise APIError(502, "invalid_response", "Модель вернула некорректные метрики.")
        metrics[key] = value
    seconds = metrics["eval_duration"] / 1e9
    normalized = {"load_seconds": metrics["load_duration"] / 1e9, "generation_seconds": seconds,
                  "prompt_tokens": metrics["prompt_eval_count"], "output_tokens": metrics["eval_count"],
                  "tokens_per_second": metrics["eval_count"] / seconds}
    if not all(number(value) is not None for value in normalized.values()):
        raise APIError(502, "invalid_response", "Модель вернула некорректные метрики.")
    return message["content"].strip(), normalized, data.get("done_reason") == "length"


async def metadata(client):
    try:
        data = await provider_json(client, "/api/show", {"model": MODEL})
        details = data.get("details")
        value = details.get("quantization_level") if isinstance(details, dict) else None
        return value if isinstance(value, str) and value else None
    except APIError:
        return None


async def resource_sample(client, num_ctx):
    """Observation is optional. Reject another runner/context, retain null measurements."""
    try:
        data = await provider_json(client, "/api/ps")
        models = data.get("models")
        if not isinstance(models, list):
            return None
        for item in models:
            if not isinstance(item, dict) or not (item.get("name") == MODEL or item.get("model") == MODEL):
                continue
            if number(item.get("context_length"), positive=True, integer=True) != num_ctx:
                continue
            details = item.get("details")
            quantization = details.get("quantization_level") if isinstance(details, dict) else None
            return {"size_bytes": number(item.get("size"), integer=True),
                    "size_vram_bytes": number(item.get("size_vram"), integer=True),
                    "context_length": num_ctx,
                    "quantization": quantization if isinstance(quantization, str) else None}
    except APIError:
        pass
    return None
