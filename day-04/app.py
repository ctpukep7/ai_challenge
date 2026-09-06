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
MAX_STOP_SEQUENCES = 16
MAX_MODEL_NAME_LENGTH = 200


@dataclass(frozen=True)
class TemperatureRun:
    value: float
    label: str
    description: str


@dataclass(frozen=True)
class Provider:
    label: str
    base_url: str
    api_key_env: str
    default_model: str
    supports_thinking: bool


RUNS = (
    TemperatureRun(0, "0", "Максимум повторяемости и фокуса."),
    TemperatureRun(0.7, "0.7", "Баланс устойчивости и вариативности."),
    TemperatureRun(1.2, "1.2", "Больше необычных формулировок и идей."),
    TemperatureRun(1.5, "1.5", "Высокая вариативность и более смелые идеи."),
    TemperatureRun(1.7, "1.7", "Очень смелые формулировки — проверяйте факты."),
    TemperatureRun(1.9, "1.9", "Максимальный разброс идей — результат требует проверки."),
)

PROVIDERS = {
    "deepseek": Provider("DeepSeek", "https://api.deepseek.com", "DEEPSEEK_API_KEY", "deepseek-v4-flash", True),
    "openrouter": Provider("OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "openrouter/free", False),
}


def value_or_attribute(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def get_client(provider):
    api_key = os.getenv(provider.api_key_env)
    return OpenAI(api_key=api_key, base_url=provider.base_url) if api_key else None


def validate_controls(payload):
    raw = payload.get("controls", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return None, "Настройки должны быть объектом."

    format_instruction = raw.get("format_instruction", "")
    raw_stops = raw.get("stop_sequences", [])
    raw_max_tokens = raw.get("max_tokens", "")
    thinking_mode = raw.get("thinking_mode", "disabled")
    provider_id = raw.get("provider", "deepseek")
    model = raw.get("model", "")

    if not isinstance(format_instruction, str):
        return None, "Инструкция формата должна быть строкой."
    if isinstance(raw_stops, str):
        raw_stops = raw_stops.splitlines()
    if not isinstance(raw_stops, list) or not all(isinstance(item, str) for item in raw_stops):
        return None, "Stop sequences должны быть списком строк."
    stop_sequences = list(dict.fromkeys(item.strip() for item in raw_stops if item.strip()))
    if len(stop_sequences) > MAX_STOP_SEQUENCES:
        return None, f"Можно указать не более {MAX_STOP_SEQUENCES} stop sequence."
    if thinking_mode not in ("", "enabled", "disabled"):
        return None, "Thinking mode должен быть enabled, disabled или пустым."
    if provider_id not in PROVIDERS:
        return None, "Выберите DeepSeek или OpenRouter."
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


def messages_for(prompt, controls):
    messages = []
    instruction = control_instruction(controls)
    if instruction:
        messages.append({"role": "system", "content": instruction})
    messages.append({"role": "user", "content": prompt})
    return messages


def completion_options(controls, provider):
    options = {}
    if controls["max_tokens"] is not None:
        options["max_tokens"] = controls["max_tokens"]
    if controls["stop_sequences"]:
        options["stop"] = controls["stop_sequences"]
    if provider.supports_thinking and controls["thinking_mode"]:
        options["extra_body"] = {"thinking": {"type": controls["thinking_mode"]}}
    return options


def public_request(model, messages, temperature, options):
    """Safe request preview: it never contains an API key."""
    body = {"model": model, "messages": messages, "temperature": temperature}
    if "max_tokens" in options:
        body["max_tokens"] = options["max_tokens"]
    if "stop" in options:
        body["stop"] = options["stop"]
    if "extra_body" in options:
        body.update(options["extra_body"])
    return body


def completion_result(completion, run, request_body, elapsed_ms, controls, provider):
    message = completion.choices[0].message
    answer = message.content.strip() if isinstance(message.content, str) else ""
    usage = getattr(completion, "usage", None)
    resolved_model = value_or_attribute(completion, "model") or controls["model"]
    return {
        "temperature": run.value,
        "answer": answer or "Модель не вернула финальный текст.",
        "completion_tokens": value_or_attribute(usage, "completion_tokens"),
        "finish_reason": completion.choices[0].finish_reason,
        "elapsed_ms": elapsed_ms,
        "provider": controls["provider"],
        "model": controls["model"],
        "resolved_model": resolved_model,
        "thinking_applied": controls["thinking_mode"] if provider.supports_thinking else None,
        "request": request_body,
    }


def run_for_temperature(client, prompt, controls, provider, run):
    """Run one temperature experiment and return a safe result for the browser."""
    messages = messages_for(prompt, controls)
    options = completion_options(controls, provider)
    request_body = public_request(controls["model"], messages, run.value, options)
    started_at = perf_counter()
    try:
        completion = client.chat.completions.create(
            model=controls["model"], messages=messages, temperature=run.value, **options
        )
        elapsed_ms = round((perf_counter() - started_at) * 1000)
        return completion_result(completion, run, request_body, elapsed_ms, controls, provider)
    except Exception:
        app.logger.exception("%s request failed for temperature %s", provider.label, run.value)
        return {
            "temperature": run.value,
            "error": f"{provider.label} временно недоступен или не принял параметры.",
            "request": request_body,
        }


def request_context(payload):
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        return None, None, None, None, (jsonify(error="Введите непустой промпт."), 400)
    controls, error = validate_controls(payload)
    if error:
        return None, None, None, None, (jsonify(error=error), 400)

    provider = PROVIDERS[controls["provider"]]
    client = get_client(provider)
    if not client:
        return None, None, None, None, (
            jsonify(error=f"Не задан {provider.api_key_env}. Добавьте ключ в файл .env и перезапустите приложение."),
            503,
        )
    return prompt.strip(), controls, provider, client, None


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/compare")
def compare():
    prompt, controls, provider, client, response = request_context(request.get_json(silent=True))
    if response:
        return response
    return jsonify(results=[run_for_temperature(client, prompt, controls, provider, run) for run in RUNS])


@app.post("/run")
def run():
    """Execute exactly one temperature run so the UI can reveal results in sequence."""
    payload = request.get_json(silent=True)
    prompt, controls, provider, client, response = request_context(payload)
    if response:
        return response
    raw_temperature = payload.get("temperature")
    if isinstance(raw_temperature, bool) or not isinstance(raw_temperature, (int, float)):
        return jsonify(error="temperature должна быть 0, 0.7, 1.2, 1.5, 1.7 или 1.9."), 400
    selected_run = next((run for run in RUNS if run.value == raw_temperature), None)
    if not selected_run:
        return jsonify(error="temperature должна быть 0, 0.7, 1.2, 1.5, 1.7 или 1.9."), 400
    return jsonify(result=run_for_temperature(client, prompt, controls, provider, selected_run))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5003, debug=True)
