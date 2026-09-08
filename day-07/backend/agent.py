"""A configurable conversational agent that restores context from SQLite."""

import os
from dataclasses import asdict, dataclass
from time import perf_counter

from openai import OpenAI


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MAX_ALLOWED_TOKENS = 10_000
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
    """Base error the web interface can safely show."""


class AgentInputError(AgentError):
    pass


class AgentConfigurationError(AgentError):
    pass


class AgentRequestError(AgentError):
    pass


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
    finish_reason: str | None
    request: dict

    def to_dict(self):
        return asdict(self)


def read_attribute(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


class AssistantAgent:
    """Loads, sends and persists each turn of one SQLite conversation."""

    def __init__(self, store, client=None, client_factory=OpenAI):
        self.store = store
        self.client = client
        self.client_factory = client_factory

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

    def messages_for(self, prompt, controls, history):
        instructions = []
        if controls.format_instruction:
            instructions.append(f"Следуй инструкции формата ответа: {controls.format_instruction}")
        if controls.stop_sequences:
            instructions.append("После основного ответа напиши на отдельной строке одну из точных последовательностей: " + ", ".join(controls.stop_sequences) + ". Не добавляй текст после этой последовательности.")
        messages = [{"role": item["role"], "content": item["content"]} for item in history]
        messages.append({"role": "user", "content": prompt})
        if instructions:
            messages.insert(0, {"role": "system", "content": "\n\n".join(instructions)})
        return messages

    def reply(self, conversation_id, prompt, raw_controls=None):
        if not isinstance(prompt, str) or not (prompt := prompt.strip()):
            raise AgentInputError("Введите непустой текст вопроса.")
        conversation = self.store.get_conversation(conversation_id)
        if not conversation:
            raise AgentInputError("Диалог не найден. Выберите или создайте другой.")
        controls = self.controls_from(raw_controls)
        provider = PROVIDERS[controls.provider]
        key = os.getenv(provider.key_name)
        if self.client is None and not key:
            raise AgentConfigurationError(f"Не задан {provider.key_name}. Укажите ключ в .env и перезапустите приложение.")
        messages = self.messages_for(prompt, controls, conversation["messages"])
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
        request = {"provider": provider.label, "model": controls.model, "messages": messages}
        for key_name in ("temperature", "max_tokens", "stop"):
            if key_name in options:
                request[key_name] = options[key_name]
        if provider.supports_thinking:
            request["thinking"] = {"type": controls.thinking_mode}
        client = self.client or self.client_factory(api_key=key, base_url=provider.base_url)
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
            raise AgentRequestError("Модель вернула пустой ответ. Попробуйте ещё раз.")
        answer = answer.strip()
        self.store.append_turn(conversation_id, prompt, answer, request)
        usage = getattr(completion, "usage", None)
        return AgentReply(
            answer=answer,
            provider=provider.label,
            model=read_attribute(completion, "model") or controls.model,
            elapsed_ms=round((perf_counter() - started) * 1000),
            prompt_tokens=read_attribute(usage, "prompt_tokens"),
            completion_tokens=read_attribute(usage, "completion_tokens"),
            finish_reason=read_attribute(choice, "finish_reason"),
            request=request,
        )
