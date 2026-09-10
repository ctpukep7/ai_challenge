"""AssistantAgent with persisted context and factual token usage."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from time import perf_counter
from typing import Any

from openai import OpenAI

from .catalog import ModelMetadataService, fallback_cost


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MAX_ALLOWED_TOKENS = 100_000
MAX_STOP_SEQUENCES = 16


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    base_url: str
    key_name: str
    default_model: str
    supports_thinking: bool


PROVIDERS = {
    "deepseek": Provider("deepseek", "DeepSeek", DEEPSEEK_BASE_URL, "DEEPSEEK_API_KEY", "deepseek-v4-flash", True),
    "openrouter": Provider("openrouter", "OpenRouter", OPENROUTER_BASE_URL, "OPENROUTER_API_KEY", "openrouter/free", False),
}


class AgentError(Exception):
    pass


class AgentInputError(AgentError):
    pass


class AgentConfigurationError(AgentError):
    pass


class AgentRequestError(AgentError):
    def __init__(self, message: str, provider_detail: str | None = None):
        super().__init__(message)
        self.provider_detail = provider_detail


def safe_error_detail(error: Exception) -> str:
    """Return a useful provider message without exposing credentials."""
    detail = str(error).strip() or error.__class__.__name__
    detail = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[скрыто]", detail)
    detail = re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;]+", r"\1[скрыто]", detail)
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[скрыто]", detail)
    return detail[:1200]


@dataclass(frozen=True)
class AgentControls:
    provider: str
    model: str
    format_instruction: str
    temperature: float
    max_tokens: int | None
    stop_sequences: list[str]
    thinking_mode: str


@dataclass(frozen=True)
class AgentReply:
    answer: str
    provider: str
    model: str
    elapsed_ms: int
    finish_reason: str | None
    request: dict[str, Any]
    usage: dict[str, Any]

    def to_dict(self):
        return asdict(self)


def read_attribute(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


class AssistantAgent:
    def __init__(self, store, client=None, client_factory=OpenAI, metadata_service=None):
        self.store = store
        self.client = client
        self.client_factory = client_factory
        self.metadata_service = metadata_service or ModelMetadataService()

    def controls_from(self, raw):
        raw = raw or {}
        if not isinstance(raw, dict):
            raise AgentInputError("Настройки должны быть объектом.")
        provider_id = raw.get("provider", "deepseek")
        if provider_id not in PROVIDERS:
            raise AgentInputError("Провайдер должен быть DeepSeek или OpenRouter.")
        provider = PROVIDERS[provider_id]
        model = raw.get("model", provider.default_model)
        model = model.strip() if isinstance(model, str) else provider.default_model
        model = model or provider.default_model
        try:
            temperature = float(raw.get("temperature", 0))
        except (TypeError, ValueError) as error:
            raise AgentInputError("temperature должна быть числом от 0 до 2.") from error
        if not 0 <= temperature <= 2:
            raise AgentInputError("temperature должна быть числом от 0 до 2.")
        maximum = raw.get("max_tokens", "")
        try:
            max_tokens = None if maximum in (None, "") else int(maximum)
        except (TypeError, ValueError) as error:
            raise AgentInputError("max_tokens должен быть целым числом.") from error
        if max_tokens is not None and not 1 <= max_tokens <= MAX_ALLOWED_TOKENS:
            raise AgentInputError(f"max_tokens должен быть от 1 до {MAX_ALLOWED_TOKENS}.")
        stops = raw.get("stop_sequences", [])
        if not isinstance(stops, list) or not all(isinstance(item, str) for item in stops):
            raise AgentInputError("Stop sequences должны быть списком строк.")
        stops = list(dict.fromkeys(item.strip() for item in stops if item.strip()))
        if len(stops) > MAX_STOP_SEQUENCES:
            raise AgentInputError(f"Можно указать не более {MAX_STOP_SEQUENCES} условий завершения.")
        thinking = raw.get("thinking_mode", "disabled")
        if thinking not in ("enabled", "disabled"):
            raise AgentInputError("Thinking mode должен быть enabled или disabled.")
        instruction = raw.get("format_instruction", "")
        if not isinstance(instruction, str):
            raise AgentInputError("Инструкция формата должна быть строкой.")
        return AgentControls(provider_id, model, instruction.strip(), temperature, max_tokens, stops, thinking)

    def messages_for(self, prompt: str, controls: AgentControls, history: list[dict]):
        instructions = []
        if controls.format_instruction:
            instructions.append(f"Следуй инструкции формата ответа: {controls.format_instruction}")
        if controls.stop_sequences:
            instructions.append("После основного ответа напиши на отдельной строке одну из последовательностей: " + ", ".join(controls.stop_sequences) + ". Не добавляй текст после неё.")
        messages = [{"role": item["role"], "content": item["content"]} for item in history]
        messages.append({"role": "user", "content": prompt})
        if instructions:
            messages.insert(0, {"role": "system", "content": "\n\n".join(instructions)})
        return messages

    def _options(self, controls: AgentControls, provider: Provider, messages: list[dict]):
        options = {"model": controls.model, "messages": messages, "temperature": controls.temperature}
        if controls.max_tokens is not None:
            options["max_tokens"] = controls.max_tokens
        if controls.stop_sequences:
            options["stop"] = controls.stop_sequences
        if provider.supports_thinking:
            options["extra_body"] = {"thinking": {"type": controls.thinking_mode}}
            if controls.thinking_mode == "enabled":
                options.pop("temperature", None)
        return options

    def _request_preview(self, options, provider: Provider, controls: AgentControls, synthetic_copies=1, base_history_count=0):
        preview_messages = options["messages"]
        if synthetic_copies > 1:
            # Keep one real copy of the history plus the new question in SQLite.
            # The actual API request still receives all copies.
            system_count = 1 if preview_messages and preview_messages[0]["role"] == "system" else 0
            preview_messages = (
                preview_messages[:system_count + base_history_count]
                + preview_messages[-1:]
            )
        preview = {"provider": provider.label, "model": controls.model, "messages": preview_messages}
        preview["context_summary"] = {
            "base_history_messages": base_history_count,
            "new_user_messages": 1,
        }
        for key in ("temperature", "max_tokens", "stop"):
            if key in options:
                preview[key] = options[key]
        if provider.supports_thinking:
            preview["thinking"] = {"type": controls.thinking_mode}
        if synthetic_copies > 1:
            # Persist only a compact description.  Repeated virtual history must
            # not be written into SQLite with the otherwise ordinary request preview.
            preview["synthetic_context"] = {
                "history_copy_multiplier": synthetic_copies,
                "base_history_messages": base_history_count,
                "virtual_history_messages_sent": base_history_count * synthetic_copies,
                "preview_contains": "одна сохранённая копия истории",
                "note": "История повторена только для этого вызова и не сохранена в SQLite.",
            }
        return preview

    def _client_for(self, provider: Provider):
        key = os.getenv(provider.key_name)
        if self.client is None and not key:
            raise AgentConfigurationError(f"Не задан {provider.key_name}. Укажите ключ в .env и перезапустите приложение.")
        return self.client or self.client_factory(api_key=key, base_url=provider.base_url), key

    def reply(self, conversation_id: int, prompt: str, raw_controls=None, synthetic_copies=1):
        if not isinstance(prompt, str) or not (prompt := prompt.strip()):
            raise AgentInputError("Введите непустой текст вопроса.")
        conversation = self.store.get_conversation(conversation_id)
        if not conversation:
            raise AgentInputError("Диалог не найден. Выберите или создайте другой.")
        if not isinstance(synthetic_copies, int) or not 1 <= synthetic_copies <= 64 or synthetic_copies & (synthetic_copies - 1):
            raise AgentInputError("Синтетический контекст должен быть степенью двойки от 1 до 64.")
        controls = self.controls_from(raw_controls)
        provider = PROVIDERS[controls.provider]
        client, key = self._client_for(provider)
        history = conversation["messages"]
        messages = self.messages_for(prompt, controls, history * synthetic_copies)
        options = self._options(controls, provider, messages)
        request = self._request_preview(options, provider, controls, synthetic_copies, len(history))
        started = perf_counter()
        try:
            completion = client.chat.completions.create(**options)
            choice = completion.choices[0]
            answer = choice.message.content
        except (IndexError, AttributeError, TypeError) as error:
            raise AgentRequestError("Модель вернула ответ в неожиданном формате.") from error
        except Exception as error:
            raise AgentRequestError(
                f"{provider.label} временно недоступен или не принял параметры.",
                safe_error_detail(error),
            ) from error
        if not isinstance(answer, str) or not answer.strip():
            raise AgentRequestError("Модель вернула пустой ответ. Попробуйте ещё раз.")
        model = read_attribute(completion, "model") or controls.model
        metadata = self.metadata_service.for_model(controls.provider, model, key)
        usage = self._usage(read_attribute(completion, "usage"), metadata)
        usage["elapsed_ms"] = round((perf_counter() - started) * 1000)
        usage["finish_reason"] = read_attribute(choice, "finish_reason")
        usage["model"] = model
        usage["provider"] = provider.label
        self.store.append_turn(conversation_id, prompt, answer.strip(), request, usage)
        return AgentReply(answer.strip(), provider.label, model, usage["elapsed_ms"], usage["finish_reason"], request, usage)

    def _usage(self, raw_usage, metadata):
        cache_details = read_attribute(raw_usage, "prompt_tokens_details") or {}
        completion_details = read_attribute(raw_usage, "completion_tokens_details") or {}
        prompt = read_attribute(raw_usage, "prompt_tokens")
        completion = read_attribute(raw_usage, "completion_tokens")
        total = read_attribute(raw_usage, "total_tokens")
        usage = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "prompt_cache_hit_tokens": read_attribute(raw_usage, "prompt_cache_hit_tokens") or read_attribute(cache_details, "cached_tokens"),
            "prompt_cache_miss_tokens": read_attribute(raw_usage, "prompt_cache_miss_tokens"),
            "reasoning_tokens": read_attribute(raw_usage, "reasoning_tokens") or read_attribute(completion_details, "reasoning_tokens"),
            "context_length": metadata.get("context_length"),
            "max_completion_tokens": metadata.get("max_completion_tokens"),
            "metadata_source": metadata.get("source"),
            "price_status": metadata.get("price_status"),
        }
        actual_cost = read_attribute(raw_usage, "cost")
        usage["cost_usd"] = actual_cost if isinstance(actual_cost, (int, float)) else fallback_cost(usage, metadata)
        return usage
