"""A configurable, self-contained agent for one LLM conversation turn."""

import os
from dataclasses import asdict, dataclass
from time import perf_counter

from openai import OpenAI


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MAX_ALLOWED_TOKENS = 10_000
MAX_STOP_SEQUENCES = 16
MAX_MODEL_NAME_LENGTH = 200


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
DEEPSEEK_PRICES = {
    "deepseek-v4-flash": {"cache_hit": 0.0028, "cache_miss": 0.14, "output": 0.28},
    "deepseek-v4-pro": {"cache_hit": 0.003625, "cache_miss": 0.435, "output": 0.87},
}


class AgentError(Exception):
    """Base exception for errors the web interface can show to the user."""


class AgentInputError(AgentError):
    """The user message or controls cannot be sent to the model."""


class AgentConfigurationError(AgentError):
    """The agent does not have the configuration needed to run."""


class AgentRequestError(AgentError):
    """The LLM provider did not return a usable answer."""


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
    prompt_tokens: int | None
    completion_tokens: int | None
    reasoning_tokens: int | None
    finish_reason: str | None
    thinking_mode: str | None
    cost_usd: float | None
    request: dict

    def to_dict(self):
        return asdict(self)


def value_or_attribute(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


class AssistantAgent:
    """Encapsulates one configurable question-and-answer interaction with an LLM."""

    def __init__(self, client=None, client_factory=OpenAI):
        self.client = client
        self.client_factory = client_factory

    def controls_from(self, raw_controls):
        raw = raw_controls or {}
        if not isinstance(raw, dict):
            raise AgentInputError("Настройки должны быть объектом.")
        provider_id = raw.get("provider", "deepseek")
        if provider_id not in PROVIDERS:
            raise AgentInputError("Провайдер должен быть DeepSeek или OpenRouter.")
        provider = PROVIDERS[provider_id]
        model = raw.get("model", provider.default_model)
        if not isinstance(model, str) or not (model := model.strip()):
            model = provider.default_model
        if len(model) > MAX_MODEL_NAME_LENGTH:
            raise AgentInputError(f"Название модели должно быть не длиннее {MAX_MODEL_NAME_LENGTH} символов.")
        format_instruction = raw.get("format_instruction", "")
        if not isinstance(format_instruction, str):
            raise AgentInputError("Инструкция формата должна быть строкой.")
        raw_temperature = raw.get("temperature", 0)
        if isinstance(raw_temperature, bool):
            raise AgentInputError("temperature должна быть числом от 0 до 2.")
        try:
            temperature = float(raw_temperature)
        except (TypeError, ValueError) as error:
            raise AgentInputError("temperature должна быть числом от 0 до 2.") from error
        if not 0 <= temperature <= 2:
            raise AgentInputError("temperature должна быть числом от 0 до 2.")
        raw_max = raw.get("max_tokens", "")
        if raw_max in (None, ""):
            max_tokens = None
        else:
            if isinstance(raw_max, bool):
                raise AgentInputError("max_tokens должен быть целым числом.")
            try:
                max_tokens = int(raw_max)
            except (TypeError, ValueError) as error:
                raise AgentInputError("max_tokens должен быть целым числом.") from error
            if not 1 <= max_tokens <= MAX_ALLOWED_TOKENS:
                raise AgentInputError(f"max_tokens должен быть от 1 до {MAX_ALLOWED_TOKENS}.")
        raw_stops = raw.get("stop_sequences", [])
        if isinstance(raw_stops, str):
            raw_stops = raw_stops.splitlines()
        if not isinstance(raw_stops, list) or not all(isinstance(item, str) for item in raw_stops):
            raise AgentInputError("Stop sequences должны быть списком строк.")
        stops = list(dict.fromkeys(item.strip() for item in raw_stops if item.strip()))
        if len(stops) > MAX_STOP_SEQUENCES:
            raise AgentInputError(f"Можно указать не более {MAX_STOP_SEQUENCES} условий завершения.")
        thinking = raw.get("thinking_mode", "disabled")
        if thinking not in ("enabled", "disabled"):
            raise AgentInputError("Thinking mode должен быть enabled или disabled.")
        return AgentControls(provider_id, model, format_instruction.strip(), temperature, max_tokens, stops, thinking)

    def messages_for(self, prompt, controls):
        instructions = []
        if controls.format_instruction:
            instructions.append(f"Следуй инструкции формата ответа: {controls.format_instruction}")
        if controls.stop_sequences:
            instructions.append("После основного ответа напиши на отдельной строке одну из точных последовательностей: " + ", ".join(controls.stop_sequences) + ". Не добавляй текст после этой последовательности.")
        messages = [{"role": "user", "content": prompt}]
        if instructions:
            messages.insert(0, {"role": "system", "content": "\n\n".join(instructions)})
        return messages

    def options_for(self, controls, provider, messages):
        options = {"model": controls.model, "messages": messages}
        if controls.max_tokens is not None:
            options["max_tokens"] = controls.max_tokens
        if controls.stop_sequences:
            options["stop"] = controls.stop_sequences
        if provider.supports_thinking:
            options["extra_body"] = {"thinking": {"type": controls.thinking_mode}}
            if controls.thinking_mode == "disabled":
                options["temperature"] = controls.temperature
        else:
            options["temperature"] = controls.temperature
        return options

    def public_request(self, controls, provider, messages, options):
        body = {"provider": provider.label, "model": controls.model, "messages": messages}
        for name in ("temperature", "max_tokens", "stop"):
            if name in options:
                body[name] = options[name]
        if provider.supports_thinking:
            body["thinking"] = {"type": controls.thinking_mode}
        return body

    def estimated_cost(self, controls, usage):
        if controls.provider == "openrouter" and controls.model.endswith(":free"):
            return 0
        price = DEEPSEEK_PRICES.get(controls.model)
        if not price:
            return None
        hit = value_or_attribute(usage, "prompt_cache_hit_tokens")
        miss = value_or_attribute(usage, "prompt_cache_miss_tokens")
        output = value_or_attribute(usage, "completion_tokens")
        if not all(isinstance(value, int) for value in (hit, miss, output)):
            return None
        return round((hit * price["cache_hit"] + miss * price["cache_miss"] + output * price["output"]) / 1_000_000, 9)

    def reply(self, prompt, raw_controls=None):
        """Normalize input, call the provider and return answer plus safe metrics."""
        if not isinstance(prompt, str) or not (message := prompt.strip()):
            raise AgentInputError("Введите непустой текст вопроса.")
        controls = self.controls_from(raw_controls)
        provider = PROVIDERS[controls.provider]
        api_key = os.getenv(provider.key_name)
        if self.client is None and not api_key:
            raise AgentConfigurationError(f"Не задан {provider.key_name}. Укажите ключ в .env и перезапустите приложение.")
        messages = self.messages_for(message, controls)
        options = self.options_for(controls, provider, messages)
        request = self.public_request(controls, provider, messages, options)
        client = self.client or self.client_factory(api_key=api_key, base_url=provider.base_url)
        started = perf_counter()
        try:
            completion = client.chat.completions.create(**options)
            choice = completion.choices[0]
            answer = choice.message.content
        except (IndexError, AttributeError, TypeError) as error:
            raise AgentRequestError("Модель вернула ответ в неожиданном формате.") from error
        except Exception as error:
            raise AgentRequestError(f"{provider.label} временно недоступен или не принял параметры.") from error
        if not isinstance(answer, str) or not answer.strip():
            raise AgentRequestError("Модель вернула пустой ответ. Попробуйте задать вопрос ещё раз.")
        usage = getattr(completion, "usage", None)
        details = value_or_attribute(usage, "completion_tokens_details")
        prompt_tokens = value_or_attribute(usage, "prompt_tokens")
        if not isinstance(prompt_tokens, int):
            hit, miss = value_or_attribute(usage, "prompt_cache_hit_tokens"), value_or_attribute(usage, "prompt_cache_miss_tokens")
            prompt_tokens = hit + miss if isinstance(hit, int) and isinstance(miss, int) else None
        return AgentReply(
            answer.strip(), provider.label, value_or_attribute(completion, "model") or controls.model,
            round((perf_counter() - started) * 1000), prompt_tokens, value_or_attribute(usage, "completion_tokens"),
            value_or_attribute(details, "reasoning_tokens"), value_or_attribute(choice, "finish_reason"),
            controls.thinking_mode if provider.supports_thinking else None, self.estimated_cost(controls, usage), request,
        )
