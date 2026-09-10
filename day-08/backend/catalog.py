"""Model metadata used by the token lab.

Only API usage is treated as exact consumption.  This module supplies context
limits and a cost fallback for providers that do not return a cost in usage.
"""

from __future__ import annotations

import time
from typing import Any

import httpx


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/model"
OPENROUTER_MODELS_LIST_URL = "https://openrouter.ai/api/v1/models"
CACHE_SECONDS = 600

# Prices are USD per million tokens. DeepSeek has a weekday peak window in UTC;
# its active price is selected when an answer is recorded.
DEEPSEEK_V4 = {
    "deepseek-v4-flash": {
        "context_length": 1_000_000,
        "max_completion_tokens": 384_000,
        "pricing_peak_per_million": {
            "prompt_cache_hit": 0.014,
            "prompt_cache_miss": 0.44,
            "completion": 1.32,
        },
    },
    "deepseek-v4-pro": {
        "context_length": 1_000_000,
        "max_completion_tokens": 384_000,
        "pricing_peak_per_million": {
            "prompt_cache_hit": 0.044,
            "prompt_cache_miss": 1.32,
            "completion": 3.96,
        },
    },
}


class ModelMetadataService:
    """Loads public OpenRouter metadata and caches it for one local process."""

    def __init__(self, http_get=httpx.get):
        self.http_get = http_get
        self.cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

    def for_model(self, provider: str, model: str, api_key: str | None = None) -> dict[str, Any]:
        if provider == "deepseek":
            return self._deepseek(model)
        if provider == "openrouter":
            return self._openrouter(model, api_key)
        return self.unknown(provider, model)

    def model_options(self, provider: str, api_key: str | None = None) -> list[dict[str, Any]]:
        """Return safe, current options for a compact model picker."""
        if provider == "deepseek":
            return [
                {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash · 1M ctx", "context_length": 1_000_000},
                {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro · 1M ctx", "context_length": 1_000_000},
            ]
        if provider != "openrouter":
            return []
        key = ("openrouter-options", "free-text")
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        fallback = [{
            "id": "openrouter/free",
            "label": "Free Models Router · бесплатно · 200K ctx",
            "context_length": 200_000,
            "tier": "free",
        }]
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            response = self.http_get(OPENROUTER_MODELS_LIST_URL, headers=headers, timeout=8)
            response.raise_for_status()
            data = response.json().get("data", [])
            options = fallback[:]
            for model in data:
                identifier = model.get("id")
                pricing = model.get("pricing") or {}
                architecture = model.get("architecture") or {}
                modalities = architecture.get("output_modalities") or []
                is_free = str(pricing.get("prompt", "")) in ("0", "0.0", "0.000000") and str(pricing.get("completion", "")) in ("0", "0.0", "0.000000")
                # This application sends ordinary text chat completions. Exclude
                # audio/image generators even when they can also return text.
                is_text_chat = modalities == ["text"]
                if identifier and is_free and is_text_chat and identifier != "openrouter/free":
                    context_length = self._integer(model.get("context_length"))
                    options.append({
                        "id": identifier,
                        "label": f"{model.get('name', identifier)} · бесплатно · {self._context_label(context_length)} ctx",
                        "context_length": context_length,
                        "tier": "free",
                    })
            # Shortest context first: this makes limited-context models easy to
            # find when demonstrating context growth and overflow.
            result = sorted(
                options,
                key=lambda item: (item["context_length"] or float("inf"), item["label"].casefold()),
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            result = fallback
        self.cache[key] = (time.monotonic() + CACHE_SECONDS, result)
        return result

    @staticmethod
    def unknown(provider: str, model: str) -> dict[str, Any]:
        return {
            "provider": provider,
            "model": model,
            "context_length": None,
            "max_completion_tokens": None,
            "pricing": None,
            "price_status": "unknown",
            "source": "Нет данных для этой модели",
        }

    def _deepseek(self, model: str) -> dict[str, Any]:
        data = DEEPSEEK_V4.get(model)
        if not data:
            return self.unknown("deepseek", model)
        peak = self._deepseek_peak_now()
        pricing = data["pricing_peak_per_million"]
        active_pricing = pricing if peak else {key: value / 2 for key, value in pricing.items()}
        return {
            "provider": "deepseek",
            "model": model,
            "context_length": data["context_length"],
            "max_completion_tokens": data["max_completion_tokens"],
            "pricing": active_pricing,
            "price_status": "catalog",
            "source": f"Официальный каталог DeepSeek V4 · {'peak' if peak else 'off-peak'} UTC",
        }

    @staticmethod
    def _deepseek_peak_now() -> bool:
        """Peak: weekdays 01:00–04:00 and 06:00–10:00 UTC."""
        now = time.gmtime()
        return now.tm_wday < 5 and (1 <= now.tm_hour < 4 or 6 <= now.tm_hour < 10)

    def _openrouter(self, model: str, api_key: str | None) -> dict[str, Any]:
        if model == "openrouter/free":
            result = self.unknown("openrouter", model)
            result["source"] = "Алиас: фактическая модель и лимит станут известны после ответа"
            return result
        key = ("openrouter", model)
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            response = self.http_get(f"{OPENROUTER_MODELS_URL}/{model}", headers=headers, timeout=8)
            response.raise_for_status()
            payload = response.json().get("data", response.json())
            pricing = payload.get("pricing") or {}
            result = {
                "provider": "openrouter",
                "model": payload.get("id", model),
                "context_length": self._integer(payload.get("context_length") or payload.get("top_provider", {}).get("context_length")),
                "max_completion_tokens": self._integer(payload.get("top_provider", {}).get("max_completion_tokens")),
                "pricing": self._pricing(pricing),
                "price_status": "catalog" if pricing else "unknown",
                "source": "OpenRouter Models API",
            }
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            result = self.unknown("openrouter", model)
            result["source"] = "Не удалось получить каталог OpenRouter"
        self.cache[key] = (time.monotonic() + CACHE_SECONDS, result)
        return result

    @staticmethod
    def _integer(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _context_label(value: int | None) -> str:
        if not value:
            return "лимит неизвестен"
        if value >= 1_000_000:
            return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
        if value >= 1_000:
            return f"{value // 1_000}K"
        return str(value)

    @staticmethod
    def _pricing(pricing: dict[str, Any]) -> dict[str, float] | None:
        """OpenRouter catalog prices are USD per token, convert to per million."""
        try:
            result = {
                "prompt_cache_hit": float(pricing.get("input_cache_read", pricing.get("prompt", 0))) * 1_000_000,
                "prompt_cache_miss": float(pricing.get("prompt", 0)) * 1_000_000,
                "completion": float(pricing.get("completion", 0)) * 1_000_000,
            }
        except (TypeError, ValueError):
            return None
        return result


def fallback_cost(usage: dict[str, Any], metadata: dict[str, Any]) -> float | None:
    """Calculate a price only when all needed actual token data is available."""
    pricing = metadata.get("pricing")
    if not pricing:
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None or completion is None:
        return None
    cache_hit = usage.get("prompt_cache_hit_tokens") or 0
    cache_miss = usage.get("prompt_cache_miss_tokens")
    if cache_miss is None:
        cache_miss = max(0, prompt - cache_hit)
    return round(
        (cache_hit * pricing["prompt_cache_hit"] + cache_miss * pricing["prompt_cache_miss"] + completion * pricing["completion"]) / 1_000_000,
        10,
    )
