"""Paired agent: full context versus rolling LLM summary."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from time import perf_counter
from typing import Any

from openai import OpenAI


DEEPSEEK_URL = "https://api.deepseek.com"
OPENROUTER_URL = "https://openrouter.ai/api/v1"


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


class AgentError(Exception): pass
class AgentInputError(AgentError): pass
class AgentConfigurationError(AgentError): pass


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

    def to_dict(self): return asdict(self)


class ContextComparisonAgent:
    def __init__(self, store, client=None, client_factory=OpenAI):
        self.store = store
        self.client = client
        self.client_factory = client_factory

    def controls_from(self, raw: dict | None):
        raw = raw or {}
        provider_id = raw.get("provider", "deepseek")
        if provider_id not in PROVIDERS: raise AgentInputError("Неизвестный провайдер.")
        provider = PROVIDERS[provider_id]
        model = raw.get("model", provider.default_model)
        model = model.strip() if isinstance(model, str) else provider.default_model
        try: temperature = float(raw.get("temperature", 0))
        except (ValueError, TypeError) as error: raise AgentInputError("temperature должна быть числом от 0 до 2.") from error
        if not 0 <= temperature <= 2: raise AgentInputError("temperature должна быть числом от 0 до 2.")
        maximum = raw.get("max_tokens", "")
        try: maximum = None if maximum in (None, "") else int(maximum)
        except (TypeError, ValueError) as error: raise AgentInputError("max_tokens должен быть целым числом.") from error
        if maximum is not None and not 1 <= maximum <= 100_000: raise AgentInputError("max_tokens должен быть от 1 до 100000.")
        stops = raw.get("stop_sequences", [])
        if not isinstance(stops, list): raise AgentInputError("Stop sequences должны быть списком.")
        stops = list(dict.fromkeys(item.strip() for item in stops if isinstance(item, str) and item.strip()))
        if len(stops) > 16: raise AgentInputError("Можно указать до 16 stop sequences.")
        thinking = raw.get("thinking_mode", "disabled")
        if thinking not in ("enabled", "disabled"): raise AgentInputError("Некорректный thinking mode.")
        instruction = raw.get("format_instruction", "")
        if not isinstance(instruction, str): raise AgentInputError("Инструкция формата должна быть строкой.")
        return Controls(provider_id, model or provider.default_model, temperature, maximum, instruction.strip(), stops, thinking)

    def _client(self, provider):
        key = os.getenv(provider.key_name)
        if self.client is None and not key: raise AgentConfigurationError(f"Не задан {provider.key_name}. Укажите его в .env.")
        return self.client or self.client_factory(api_key=key, base_url=provider.base_url)

    @staticmethod
    def _system(controls):
        parts = []
        if controls.format_instruction: parts.append(f"Следуй инструкции формата ответа: {controls.format_instruction}")
        if controls.stop_sequences: parts.append("Заверши ответ одной из последовательностей: " + ", ".join(controls.stop_sequences))
        return "\n\n".join(parts)

    def _options(self, provider, controls, messages):
        options = {"model": controls.model, "messages": messages, "temperature": controls.temperature}
        if controls.max_tokens is not None: options["max_tokens"] = controls.max_tokens
        if controls.stop_sequences: options["stop"] = controls.stop_sequences
        if provider.supports_thinking:
            options["extra_body"] = {"thinking": {"type": controls.thinking_mode}}
            if controls.thinking_mode == "enabled": options.pop("temperature", None)
        return options

    def _usage(self, completion, choice, elapsed_ms, provider, model):
        raw = attr(completion, "usage")
        prompt = attr(raw, "prompt_tokens")
        completion_tokens = attr(raw, "completion_tokens")
        total = attr(raw, "total_tokens")
        cost = attr(raw, "cost")
        return {
            "prompt_tokens": prompt, "completion_tokens": completion_tokens, "total_tokens": total,
            "cost_usd": cost if isinstance(cost, (int, float)) else self._fallback_cost(provider.id, model, prompt, completion_tokens),
            "elapsed_ms": elapsed_ms, "finish_reason": attr(choice, "finish_reason"), "provider": provider.label, "model": attr(completion, "model") or model,
        }

    @staticmethod
    def _fallback_cost(provider_id, model, prompt, completion):
        if not isinstance(prompt, int) or not isinstance(completion, int): return None
        prices = {"deepseek-v4-flash": (.44, 1.32), "deepseek-v4-pro": (1.32, 3.96)}
        if provider_id != "deepseek" or model not in prices: return 0.0 if provider_id == "openrouter" and ":free" in model else None
        input_price, output_price = prices[model]
        return round((prompt * input_price + completion * output_price) / 1_000_000, 10)

    def _complete(self, provider, controls, messages, request_kind="answer"):
        options = self._options(provider, controls, messages)
        preview = {"kind": request_kind, "provider": provider.label, "model": controls.model, "messages": messages}
        for key in ("temperature", "max_tokens", "stop"):
            if key in options: preview[key] = options[key]
        if provider.supports_thinking: preview["thinking"] = {"type": controls.thinking_mode}
        started = perf_counter()
        try:
            completion = self._client(provider).chat.completions.create(**options)
            choice = completion.choices[0]
            answer = choice.message.content
        except (IndexError, AttributeError, TypeError) as error:
            raise AgentRequestError("Модель вернула ответ в неожиданном формате.") from error
        except Exception as error:
            raise AgentRequestError(f"{provider.label} временно недоступен или не принял запрос.", safe_error(error)) from error
        if not isinstance(answer, str) or not answer.strip(): raise AgentRequestError("Модель вернула пустой ответ.")
        usage = self._usage(completion, choice, round((perf_counter() - started) * 1000), provider, controls.model)
        return Reply(answer.strip(), preview, usage)

    def _messages(self, prompt, controls, history, summary=None):
        messages = [{"role": item["role"], "content": item["content"]} for item in history]
        if summary:
            messages.insert(0, {"role": "system", "content": "Сжатая история ранней части диалога. Сохраняй её факты и ограничения:\n" + summary})
        system = self._system(controls)
        if system: messages.insert(0, {"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _make_summary(self, provider, controls, previous, batch):
        material = "\n".join(f"{item['role'].upper()}: {item['content']}" for item in batch)
        previous_text = previous["content"] if previous else "(пока нет)"
        messages = [
            {"role": "system", "content": "Ты сжимаешь историю диалога. Верни точную компактную сводку в Markdown: используй короткие заголовки и маркированные пункты. Отдельно укажи факты, решения и ограничения, имена и важные числа, а также незавершённые вопросы, если они есть. Не добавляй новых фактов."},
            {"role": "user", "content": f"Предыдущая сводка:\n{previous_text}\n\nНовый фрагмент:\n{material}"},
        ]
        summary_controls = Controls(controls.provider, controls.model, 0, None, "", [], "disabled")
        return self._complete(provider, summary_controls, messages, "summary")

    def _compress_if_needed(self, branch_id, provider, controls, batch_size, keep_recent):
        batch = self.store.compressible_messages(branch_id, batch_size, keep_recent)
        if not batch: return None
        summary = self._make_summary(provider, controls, self.store.latest_summary(branch_id), batch)
        self.store.save_summary_and_archive(branch_id, batch, summary.answer, summary.request, summary.usage)
        return summary

    def reply_pair(self, experiment_id: int, prompt: str, raw_controls=None, batch_size=10, keep_recent=6):
        if not isinstance(prompt, str) or not (prompt := prompt.strip()): raise AgentInputError("Введите непустой вопрос.")
        if not all(isinstance(value, int) and 2 <= value <= 100 and value % 2 == 0 for value in (batch_size, keep_recent)):
            raise AgentInputError("N должны быть чётными числами от 2 до 100, чтобы не разделять вопрос и ответ.")
        if not self.store.get_experiment(experiment_id): raise AgentInputError("Эксперимент не найден.")
        controls = self.controls_from(raw_controls)
        provider = PROVIDERS[controls.provider]
        results, ids = {}, {}
        for mode in ("full", "compressed"):
            branch_id = self.store.branch_id(experiment_id, mode)
            history = self.store.branch_messages(branch_id, include_archived=(mode == "full"))
            summary = self.store.latest_summary(branch_id) if mode == "compressed" else None
            try:
                reply = self._complete(provider, controls, self._messages(prompt, controls, history, summary["content"] if summary else None))
                saved = self.store.append_turn(branch_id, prompt, reply.answer, reply.request, reply.usage)
                ids[mode] = saved["assistant_id"]
                results[mode] = reply.to_dict()
                if mode == "compressed":
                    try:
                        generated = self._compress_if_needed(branch_id, provider, controls, batch_size, keep_recent)
                        if generated: results[mode]["summary_generated"] = generated.to_dict()
                    except AgentError as error:
                        results[mode]["summary_error"] = {"message": str(error), "detail": getattr(error, "provider_detail", None)}
            except AgentError as error:
                results[mode] = {"error": {"message": str(error), "detail": getattr(error, "provider_detail", None)}}
                ids[mode] = None
        turn_id = self.store.add_turn_pair(experiment_id, prompt, ids["full"], ids["compressed"])
        return {"turn_id": turn_id, "results": results, "experiment": self.store.get_experiment(experiment_id), "stats": self.store.experiment_stats(experiment_id)}
