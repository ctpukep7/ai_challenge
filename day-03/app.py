import os
from dataclasses import dataclass
from time import perf_counter

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from openai import OpenAI


ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(ROOT, ".env"))

app = Flask(__name__)

MAX_ALLOWED_TOKENS = 10_000
MAX_MODEL_NAME_LENGTH = 200


@dataclass(frozen=True)
class Strategy:
    label: str
    api_calls: int


@dataclass(frozen=True)
class Provider:
    label: str
    base_url: str
    api_key_env: str
    default_model: str
    supports_thinking: bool


STRATEGIES = {
    "direct": Strategy("Прямой ответ", 1),
    "step_by_step": Strategy("Пошагово", 1),
    "auto_instruction": Strategy("Автоинструкция", 2),
    "experts": Strategy("Группа экспертов", 1),
}

PROVIDERS = {
    "deepseek": Provider(
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        default_model="deepseek-v4-flash",
        supports_thinking=True,
    ),
    "openrouter": Provider(
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        default_model="openrouter/free",
        supports_thinking=False,
    ),
}


def get_client(provider):
    api_key = os.getenv(provider.api_key_env)
    return OpenAI(api_key=api_key, base_url=provider.base_url) if api_key else None


def value_or_attribute(value, name):
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def content_of(completion):
    content = completion.choices[0].message.content
    return content.strip() if isinstance(content, str) else ""


def completion_metadata(completion):
    usage = getattr(completion, "usage", None)
    details = value_or_attribute(usage, "completion_tokens_details")
    return {
        "completion_tokens": value_or_attribute(usage, "completion_tokens"),
        "finish_reason": completion.choices[0].finish_reason,
        "reasoning_tokens": value_or_attribute(details, "reasoning_tokens"),
    }


def resolved_model_of(completion, fallback):
    model = value_or_attribute(completion, "model")
    return model if isinstance(model, str) and model else fallback


def validate_controls(payload):
    raw = payload.get("controls", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return None, "Настройки должны быть объектом."

    format_instruction = raw.get("format_instruction", "")
    raw_stops = raw.get("stop_sequences", [])
    thinking_mode = raw.get("thinking_mode", "disabled")
    raw_max_tokens = raw.get("max_tokens", "")
    provider_id = raw.get("provider", "openrouter")
    model = raw.get("model", "")
    instruction_mode = raw.get("instruction_mode", "system")

    if not isinstance(format_instruction, str):
        return None, "Инструкция формата должна быть строкой."
    if isinstance(raw_stops, str):
        raw_stops = raw_stops.splitlines()
    if not isinstance(raw_stops, list) or not all(isinstance(item, str) for item in raw_stops):
        return None, "Stop sequences должны быть списком строк."
    stop_sequences = list(dict.fromkeys(item.strip() for item in raw_stops if item.strip()))
    if len(stop_sequences) > 16:
        return None, "Можно указать не более 16 stop sequence."
    if thinking_mode not in ("", "enabled", "disabled"):
        return None, "Thinking mode должен быть enabled, disabled или пустым."
    if provider_id not in PROVIDERS:
        return None, "Выберите DeepSeek или OpenRouter."
    if instruction_mode not in ("user", "system"):
        return None, "Инструкции методов должны передаваться в user или system сообщении."
    if not isinstance(model, str):
        return None, "Название модели должно быть строкой."
    model = model.strip() or PROVIDERS[provider_id].default_model
    if len(model) > MAX_MODEL_NAME_LENGTH:
        return None, f"Название модели не должно быть длиннее {MAX_MODEL_NAME_LENGTH} символов."

    if raw_max_tokens in (None, ""):
        max_tokens = None
    elif isinstance(raw_max_tokens, bool):
        return None, "max_tokens должен быть целым числом."
    else:
        try:
            max_tokens = int(raw_max_tokens)
        except (TypeError, ValueError):
            return None, "max_tokens должен быть целым числом."
        if not 1 <= max_tokens <= MAX_ALLOWED_TOKENS:
            return None, f"max_tokens должен быть от 1 до {MAX_ALLOWED_TOKENS}."

    return {
        "format_instruction": format_instruction.strip(),
        "max_tokens": max_tokens,
        "stop_sequences": stop_sequences,
        "thinking_mode": thinking_mode,
        "provider": provider_id,
        "model": model,
        "instruction_mode": instruction_mode,
    }, None


def control_instruction(controls):
    instructions = []
    if controls["format_instruction"]:
        instructions.append(f"Следуй формату ответа: {controls['format_instruction']}")
    if controls["stop_sequences"]:
        instructions.append(
            "После основного ответа напиши на отдельной строке одну из точных последовательностей: "
            f"{', '.join(controls['stop_sequences'])}. Не добавляй текст после неё."
        )
    return "\n\n".join(instructions)


def final_options(controls, provider):
    options = {}
    if controls["max_tokens"] is not None:
        options["max_tokens"] = controls["max_tokens"]
    if controls["stop_sequences"]:
        options["stop"] = controls["stop_sequences"]
    if provider.supports_thinking and controls["thinking_mode"]:
        options["extra_body"] = {"thinking": {"type": controls["thinking_mode"]}}
    return options


def public_request(model, messages, options):
    body = {"model": model, "messages": messages}
    if "max_tokens" in options:
        body["max_tokens"] = options["max_tokens"]
    if "stop" in options:
        body["stop"] = options["stop"]
    if "extra_body" in options:
        body.update(options["extra_body"])
    return body


def create_completion(client, model, messages, options):
    return client.chat.completions.create(model=model, messages=messages, **options)


def control_messages(controls):
    messages = []
    instruction = control_instruction(controls)
    if instruction:
        messages.append({"role": "system", "content": instruction})
    return messages


def messages_for(prompt, strategy, controls):
    messages = control_messages(controls)
    if strategy == "direct":
        method_instruction = ""
    elif strategy == "step_by_step":
        method_instruction = "Решай пошагово."
    elif strategy == "experts":
        method_instruction = (
            "Решите задачу как группа экспертов. Дайте отдельные разделы "
            "«Аналитик», «Инженер» и «Критик», затем «Общий вывод». "
            "Каждая роль должна проверить решение."
        )
    else:
        raise ValueError("Неизвестная стратегия")
    if method_instruction and controls["instruction_mode"] == "system":
        messages.append({"role": "system", "content": method_instruction})
        user_prompt = prompt
    elif method_instruction:
        user_prompt = f"{prompt}\n\n{method_instruction}"
    else:
        user_prompt = prompt
    messages.append({"role": "user", "content": user_prompt})
    return messages


def solve(prompt, strategy, controls):
    provider = PROVIDERS[controls["provider"]]
    client = get_client(provider)
    if not client:
        return None, (jsonify(error=f"Не задан {provider.api_key_env}. Добавьте ключ в .env и перезапустите сервер."), 503)

    request_chain = []
    generated_prompt = None
    started_at = perf_counter()
    try:
        if strategy == "auto_instruction":
            builder_instruction = (
                "Создай самодостаточную и точную инструкцию для решения задачи пользователя. "
                "Включи условие задачи и требования к финальному ответу ниже. Не решай задачу, "
                "верни только готовую инструкцию.\n\nТребования к финальному ответу:\n"
                f"{control_instruction(controls) or 'Нет дополнительных требований.'}"
            )
            if controls["instruction_mode"] == "system":
                builder_messages = [
                    {"role": "system", "content": builder_instruction},
                    {"role": "user", "content": f"Исходная задача:\n{prompt}"},
                ]
            else:
                builder_messages = [{"role": "user", "content": f"{builder_instruction}\n\nИсходная задача:\n{prompt}"}]
            builder_options = {"max_tokens": 400}
            if provider.supports_thinking and controls["thinking_mode"]:
                builder_options["extra_body"] = {"thinking": {"type": controls["thinking_mode"]}}
            request_chain.append(public_request(controls["model"], builder_messages, builder_options))
            instruction_completion = create_completion(client, controls["model"], builder_messages, builder_options)
            generated_instruction = content_of(instruction_completion)
            if not generated_instruction:
                return None, (jsonify(error="Модель не смогла создать инструкцию для решения."), 502)
            messages = control_messages(controls)
            messages.append({"role": "user", "content": generated_instruction})
            generated_prompt = generated_instruction
        else:
            messages = messages_for(prompt, strategy, controls)

        options = final_options(controls, provider)
        request_chain.append(public_request(controls["model"], messages, options))
        completion = create_completion(client, controls["model"], messages, options)
    except Exception:
        app.logger.exception("%s request failed", provider.label)
        return None, (jsonify(error=f"{provider.label} временно недоступен или не принял параметры. Попробуйте ещё раз позже."), 502)

    answer = content_of(completion) or "Модель не вернула финальный ответ. Попробуйте сформулировать задачу иначе."
    result = {
        "answer": answer,
        "strategy_label": STRATEGIES[strategy].label,
        "api_calls": STRATEGIES[strategy].api_calls,
        "elapsed_ms": round((perf_counter() - started_at) * 1000),
        "request_chain": request_chain,
        "provider": controls["provider"],
        "model": controls["model"],
        "resolved_model": resolved_model_of(completion, controls["model"]),
        "instruction_mode": controls["instruction_mode"],
        "thinking_applied": controls["thinking_mode"] if provider.supports_thinking else None,
        **completion_metadata(completion),
    }
    if generated_prompt is not None:
        result["generated_prompt"] = generated_prompt
    return result, None


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/solve")
def api_solve():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="Ожидается JSON-запрос."), 400
    prompt = payload.get("prompt")
    strategy = payload.get("strategy")
    if not isinstance(prompt, str) or not prompt.strip():
        return jsonify(error="Введите непустую задачу."), 400
    if strategy not in STRATEGIES:
        return jsonify(error="Выберите одну из доступных стратегий."), 400
    controls, error = validate_controls(payload)
    if error:
        return jsonify(error=error), 400
    result, error = solve(prompt.strip(), strategy, controls)
    if error:
        return error
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5002, debug=True)
