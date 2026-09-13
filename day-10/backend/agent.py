"""LLM agent with Sliding Window, Sticky Facts, and Branching context strategies."""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from time import perf_counter
from typing import Any

from openai import OpenAI


DEEPSEEK_URL = "https://api.deepseek.com"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
FACT_KEYS = ("goal", "constraints", "preferences", "decisions", "agreements", "open_questions")


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    key_name: str
    base_url: str
    default_model: str
    supports_thinking: bool


PROVIDERS = {
    "deepseek": Provider("deepseek", "DeepSeek", "DEEPSEEK_API_KEY", DEEPSEEK_URL, "deepseek-v4-flash", True),
    "openrouter": Provider("openrouter", "OpenRouter", "OPENROUTER_API_KEY", OPENROUTER_URL, "openrouter/free", False),
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


def attr(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def safe_error(error: Exception):
    detail = str(error).strip() or error.__class__.__name__
    detail = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[скрыто]", detail)
    detail = re.sub(r"\b(sk|gho)_[A-Za-z0-9_-]+", "[скрыто]", detail)
    return detail[:1000]


def empty_facts():
    return {key: [] for key in FACT_KEYS}


@dataclass(frozen=True)
class Controls:
    provider: str
    model: str
    temperature: float
    max_tokens: int | None
    format_instruction: str
    stop_sequences: list[str]
    thinking_mode: str


@dataclass(frozen=True)
class Reply:
    answer: str
    request: dict[str, Any]
    usage: dict[str, Any]

    def to_dict(self):
        return asdict(self)


class ContextStrategiesAgent:
    def __init__(self, store, client=None, client_factory=OpenAI):
        self.store = store
        self.client = client
        self.client_factory = client_factory

    def controls_from(self, raw: dict | None):
        raw = raw or {}
        provider_id = raw.get("provider", "deepseek")
        if provider_id not in PROVIDERS:
            raise AgentInputError("Неизвестный провайдер.")
        provider = PROVIDERS[provider_id]
        model = raw.get("model", provider.default_model)
        model = model.strip() if isinstance(model, str) else provider.default_model
        try:
            temperature = float(raw.get("temperature", 0))
        except (ValueError, TypeError) as error:
            raise AgentInputError("temperature должна быть числом от 0 до 2.") from error
        if not 0 <= temperature <= 2:
            raise AgentInputError("temperature должна быть числом от 0 до 2.")
        maximum = raw.get("max_tokens", "")
        try:
            maximum = None if maximum in (None, "") else int(maximum)
        except (ValueError, TypeError) as error:
            raise AgentInputError("max_tokens должен быть целым числом.") from error
        if maximum is not None and not 1 <= maximum <= 100_000:
            raise AgentInputError("max_tokens должен быть от 1 до 100000.")
        stops = raw.get("stop_sequences", [])
        if not isinstance(stops, list):
            raise AgentInputError("Stop sequences должны быть списком.")
        stops = list(dict.fromkeys(item.strip() for item in stops if isinstance(item, str) and item.strip()))
        if len(stops) > 16:
            raise AgentInputError("Можно указать до 16 stop sequences.")
        thinking = raw.get("thinking_mode", "disabled")
        if thinking not in ("enabled", "disabled"):
            raise AgentInputError("Некорректный thinking mode.")
        instruction = raw.get("format_instruction", "")
        if not isinstance(instruction, str):
            raise AgentInputError("Инструкция формата должна быть строкой.")
        return Controls(provider_id, model or provider.default_model, temperature, maximum, instruction.strip(), stops, thinking)

    def _client(self, provider):
        key = os.getenv(provider.key_name)
        if self.client is None and not key:
            raise AgentConfigurationError(f"Не задан {provider.key_name}. Укажите его в .env.")
        return self.client or self.client_factory(api_key=key, base_url=provider.base_url)

    @staticmethod
    def _system(controls):
        parts = []
        if controls.format_instruction:
            parts.append(f"Следуй инструкции формата ответа: {controls.format_instruction}")
        if controls.stop_sequences:
            parts.append("Заверши ответ одной из последовательностей: " + ", ".join(controls.stop_sequences))
        return "\n\n".join(parts)

    def _options(self, provider, controls, messages):
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

    @staticmethod
    def _fallback_cost(provider_id, model, prompt, completion):
        if not isinstance(prompt, int) or not isinstance(completion, int):
            return None
        prices = {"deepseek-v4-flash": (.44, 1.32), "deepseek-v4-pro": (1.32, 3.96)}
        if provider_id == "openrouter" and ":free" in model:
            return 0.0
        if provider_id != "deepseek" or model not in prices:
            return None
        input_price, output_price = prices[model]
        return round((prompt * input_price + completion * output_price) / 1_000_000, 10)

    def _usage(self, completion, choice, elapsed_ms, provider, model):
        raw = attr(completion, "usage")
        prompt = attr(raw, "prompt_tokens")
        completion_tokens = attr(raw, "completion_tokens")
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion_tokens,
            "total_tokens": attr(raw, "total_tokens"),
            "cost_usd": attr(raw, "cost") if isinstance(attr(raw, "cost"), (int, float)) else self._fallback_cost(provider.id, model, prompt, completion_tokens),
            "elapsed_ms": elapsed_ms,
            "finish_reason": attr(choice, "finish_reason"),
            "provider": provider.label,
            "model": attr(completion, "model") or model,
        }

    def _complete(self, provider, controls, messages, request_kind="answer"):
        options = self._options(provider, controls, messages)
        preview = {"kind": request_kind, "provider": provider.label, "model": controls.model, "messages": messages}
        for key in ("temperature", "max_tokens", "stop"):
            if key in options:
                preview[key] = options[key]
        if provider.supports_thinking:
            preview["thinking"] = {"type": controls.thinking_mode}
        started = perf_counter()
        try:
            completion = self._client(provider).chat.completions.create(**options)
            choice = completion.choices[0]
            answer = choice.message.content
        except (IndexError, AttributeError, TypeError) as error:
            raise AgentRequestError("Модель вернула ответ в неожиданном формате.") from error
        except Exception as error:
            raise AgentRequestError(f"{provider.label} временно недоступен или не принял запрос.", safe_error(error)) from error
        if not isinstance(answer, str) or not answer.strip():
            raise AgentRequestError("Модель вернула пустой ответ.")
        return Reply(answer.strip(), preview, self._usage(completion, choice, round((perf_counter() - started) * 1000), provider, controls.model))

    @staticmethod
    def _normalise_facts(value):
        if not isinstance(value, dict):
            raise AgentRequestError("Facts-модель вернула не объект JSON.")
        result = empty_facts()
        for key in FACT_KEYS:
            items = value.get(key, [])
            if isinstance(items, str):
                items = [items]
            if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                raise AgentRequestError("Facts-модель вернула неверную структуру JSON.")
            result[key] = list(dict.fromkeys(item.strip() for item in items if item.strip()))
        return result

    def _facts_update(self, provider, controls, previous, prompt):
        messages = [
            {
                "role": "system",
                "content": (
                    "Обнови память диалога. Верни только валидный JSON без Markdown и без новых фактов. "
                    "Разрешённые ключи: goal, constraints, preferences, decisions, agreements, open_questions. "
                    "Значение каждого ключа — массив коротких строк. Сохраняй прежние релевантные данные, "
                    "добавляй или исправляй их только по новому сообщению пользователя."
                ),
            },
            {
                "role": "user",
                "content": f"Текущие facts:\n{json.dumps(previous, ensure_ascii=False)}\n\nНовое сообщение пользователя:\n{prompt}",
            },
        ]
        facts_controls = Controls(controls.provider, controls.model, 0, None, "", [], "disabled")
        reply = self._complete(provider, facts_controls, messages, "facts_update")
        raw = reply.answer.strip()
        if raw.startswith(chr(96) * 3):
            raw = re.sub(r"^" + chr(96) * 3 + r"(?:json)?\s*|\s*" + chr(96) * 3 + r"$", "", raw, flags=re.IGNORECASE)
        try:
            return self._normalise_facts(json.loads(raw)), reply
        except json.JSONDecodeError as error:
            raise AgentRequestError("Facts-модель вернула невалидный JSON.") from error

    def _answer_messages(self, controls, history, prompt, facts=None):
        messages = [{"role": item["role"], "content": item["content"]} for item in history]
        if facts is not None:
            messages.insert(0, {"role": "system", "content": "Sticky Facts — важная память диалога:\n" + json.dumps(facts, ensure_ascii=False)})
        system = self._system(controls)
        if system:
            messages.insert(0, {"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _reply_sliding(self, experiment_id, prompt, provider, controls, window_size):
        thread_id = self.store.strategy_root(experiment_id, "sliding")
        history = self.store.thread_messages(thread_id)[-window_size:]
        reply = self._complete(provider, controls, self._answer_messages(controls, history, prompt), "sliding_window")
        ids = self.store.append_turn(thread_id, prompt, reply.answer, reply.request, reply.usage)
        return {"assistant_id": ids["assistant_id"], **reply.to_dict()}

    def _reply_facts(self, experiment_id, prompt, provider, controls, window_size):
        thread_id = self.store.strategy_root(experiment_id, "facts")
        previous = self.store.latest_facts(thread_id)
        facts = previous["values"] if previous else empty_facts()
        facts_reply, facts_error = None, None
        try:
            facts, facts_reply = self._facts_update(provider, controls, facts, prompt)
        except AgentError as error:
            facts_error = {"message": str(error), "detail": getattr(error, "provider_detail", None)}
        history = self.store.thread_messages(thread_id)[-window_size:]
        reply = self._complete(provider, controls, self._answer_messages(controls, history, prompt, facts), "sticky_facts")
        ids = self.store.append_turn(thread_id, prompt, reply.answer, reply.request, reply.usage)
        if facts_reply:
            self.store.save_facts(thread_id, ids["user_id"], facts, facts_reply.request, facts_reply.usage)
        result = {"assistant_id": ids["assistant_id"], "facts": facts, **reply.to_dict()}
        if facts_reply:
            result["facts_update"] = facts_reply.to_dict()
        if facts_error:
            result["facts_error"] = facts_error
        return result

    def _reply_branching(self, experiment_id, prompt, provider, controls):
        thread_id = self.store.active_branch(experiment_id)
        history = self.store.branch_lineage(thread_id)
        reply = self._complete(provider, controls, self._answer_messages(controls, history, prompt), "branching")
        ids = self.store.append_turn(thread_id, prompt, reply.answer, reply.request, reply.usage)
        return {"assistant_id": ids["assistant_id"], "branch_thread_id": thread_id, **reply.to_dict()}

    def ask(self, experiment_id, target, prompt, raw_controls=None, window_size=6):
        if not isinstance(prompt, str) or not (prompt := prompt.strip()):
            raise AgentInputError("Введите непустой вопрос.")
        if not isinstance(window_size, int) or window_size < 2 or window_size > 100 or window_size % 2:
            raise AgentInputError("Размер окна должен быть чётным числом от 2 до 100.")
        if target not in ("all", "sliding", "facts", "branching"):
            raise AgentInputError("Неизвестная стратегия.")
        if not self.store.get_experiment(experiment_id):
            raise AgentInputError("Эксперимент не найден.")
        if target == "all" and self.store.checkpoint(experiment_id):
            raise AgentInputError("После checkpoint продолжайте выбранную ветку Branching отдельно.")
        controls = self.controls_from(raw_controls)
        provider = PROVIDERS[controls.provider]
        handlers = {
            "sliding": lambda: self._reply_sliding(experiment_id, prompt, provider, controls, window_size),
            "facts": lambda: self._reply_facts(experiment_id, prompt, provider, controls, window_size),
            "branching": lambda: self._reply_branching(experiment_id, prompt, provider, controls),
        }
        requested = ("sliding", "facts", "branching") if target == "all" else (target,)
        results = {}
        with ThreadPoolExecutor(max_workers=len(requested)) as executor:
            futures = {executor.submit(handlers[name]): name for name in requested}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except AgentError as error:
                    results[name] = {"error": {"message": str(error), "detail": getattr(error, "provider_detail", None)}}
        self.store.add_turn(experiment_id, prompt, results)
        return {"results": results, "experiment": self.store.get_experiment(experiment_id), "stats": self.store.experiment_stats(experiment_id)}
