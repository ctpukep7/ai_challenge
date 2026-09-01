import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from openai import OpenAI


app = Flask(__name__)
load_dotenv()

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-v4-flash"
MAX_ALLOWED_TOKENS = 10_000


PAGE = """<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Day 02 — контроль ответа DeepSeek</title>
    <style>
      :root { color: #1d1d1f; font-family: Arial, sans-serif; }
      body { max-width: 900px; margin: 40px auto; padding: 0 16px 48px; background: #fafafa; }
      h1 { margin-bottom: 8px; }
      textarea, input { box-sizing: border-box; width: 100%; padding: 10px; font: inherit; }
      textarea { min-height: 130px; resize: vertical; }
      button { margin-top: 12px; padding: 10px 16px; border: 0; border-radius: 6px; background: #1267e3; color: #fff; font: inherit; cursor: pointer; }
      button:disabled { cursor: not-allowed; opacity: .55; }
      details { margin-top: 24px; padding: 14px; border: 1px solid #d6d6d6; border-radius: 8px; background: #fff; }
      summary { cursor: pointer; font-weight: 700; }
      label { display: block; margin-top: 16px; font-weight: 700; }
      label span { display: block; margin-top: 4px; font-weight: 400; color: #555; font-size: .9em; }
      .card { margin-top: 22px; padding: 16px; border: 1px solid #d6d6d6; border-radius: 8px; background: #fff; }
      .card h2 { margin-top: 0; font-size: 1.1rem; }
      .answer { min-height: 24px; white-space: pre-wrap; line-height: 1.5; }
      .meta, .hint { color: #555; font-size: .9em; }
      .error { color: #b00020; }
      pre { overflow-x: auto; padding: 12px; background: #1f2430; color: #d7e3ff; border-radius: 6px; }
      .hidden { display: none; }
      .saved-prompt { margin: 12px 0 0; padding: 10px; background: #eef5ff; border-radius: 6px; white-space: pre-wrap; }
    </style>
  </head>
  <body>
    <h1>Контроль ответа DeepSeek</h1>
    <p>Сначала получите обычный ответ, затем повторите ровно тот же промпт с настройками контроля.</p>

    <label for="prompt">Ваш промпт</label>
    <textarea id="prompt" placeholder="Напишите вопрос для DeepSeek"></textarea>
    <button id="send-standard" type="button">Отправить без ограничений</button>

    <section id="standard-card" class="card hidden">
      <h2>Стандартный ответ</h2>
      <div id="standard-answer" class="answer"></div>
      <p id="standard-meta" class="meta"></p>
    </section>

    <details id="controls-panel">
      <summary>Настройки контролируемого ответа</summary>
      <p class="hint">Они будут применены только ко второму вызову. API-ключ в браузер не передаётся.</p>

      <label for="format-instruction">Формат ответа
        <span>Текстовая инструкция для модели.</span>
      </label>
      <textarea id="format-instruction">Краткий заголовок и ровно 3 маркированных пункта.</textarea>

      <label for="max-tokens">Длина ответа (`max_tokens`)
        <span>Максимальное количество генерируемых токенов.</span>
      </label>
      <input id="max-tokens" type="number" min="1" max="10000" value="120">

      <label for="stop-sequence">Условие завершения (`stop`)
        <span>Маркер, после которого API остановит генерацию.</span>
      </label>
      <input id="stop-sequence" type="text" value="### END">

      <h3>Что будет отправлено в API</h3>
      <pre id="controlled-preview">Сначала отправьте обычный запрос.</pre>
      <button id="send-controlled" type="button" disabled>Отправить тот же промпт с настройками</button>
      <p id="saved-prompt" class="saved-prompt hidden"></p>
    </details>

    <section id="controlled-card" class="card hidden">
      <h2>Ответ с настройками</h2>
      <div id="controlled-answer" class="answer"></div>
      <p id="controlled-meta" class="meta"></p>
      <p id="controlled-config" class="meta"></p>
    </section>

    <details>
      <summary>Превью обычного запроса</summary>
      <pre id="standard-preview">Введите и отправьте промпт выше.</pre>
    </details>

    <script>
      const promptInput = document.getElementById("prompt");
      const standardButton = document.getElementById("send-standard");
      const controlledButton = document.getElementById("send-controlled");
      const formatInput = document.getElementById("format-instruction");
      const maxTokensInput = document.getElementById("max-tokens");
      const stopInput = document.getElementById("stop-sequence");
      let savedPrompt = null;

      function controlledSystemMessage(format, stopSequence) {
        return `Следуй инструкции формата ответа: ${format}\n\n` +
          `После основного ответа напиши на отдельной строке точную последовательность ${stopSequence}. ` +
          "Не добавляй текст после этой последовательности.";
      }

      function controlledPayload() {
        const format = formatInput.value.trim();
        const stopSequence = stopInput.value.trim();
        return {
          model: "deepseek-v4-flash",
          messages: [
            { role: "system", content: controlledSystemMessage(format, stopSequence) },
            { role: "user", content: savedPrompt }
          ],
          max_tokens: Number(maxTokensInput.value),
          stop: [stopSequence]
        };
      }

      function updatePreviews() {
        if (!savedPrompt) return;
        document.getElementById("standard-preview").textContent = JSON.stringify({
          model: "deepseek-v4-flash",
          messages: [{ role: "user", content: savedPrompt }]
        }, null, 2);
        document.getElementById("controlled-preview").textContent = JSON.stringify(controlledPayload(), null, 2);
      }

      function setResult(kind, data) {
        const card = document.getElementById(`${kind}-card`);
        const answer = document.getElementById(`${kind}-answer`);
        const meta = document.getElementById(`${kind}-meta`);
        card.classList.remove("hidden");

        if (data.error) {
          answer.textContent = data.error;
          answer.classList.add("error");
          meta.textContent = "";
          return;
        }

        answer.classList.remove("error");
        answer.textContent = data.answer;
        meta.textContent = `Выходные токены: ${data.completion_tokens ?? "—"}; причина завершения: ${data.finish_reason ?? "—"}`;
      }

      async function postJson(path, body) {
        const response = await fetch(path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const data = await response.json();
        return { ok: response.ok, data };
      }

      standardButton.addEventListener("click", async () => {
        const prompt = promptInput.value.trim();
        if (!prompt) {
          setResult("standard", { error: "Введите промпт." });
          return;
        }

        standardButton.disabled = true;
        setResult("standard", { answer: "DeepSeek отвечает..." });
        try {
          const result = await postJson("/ask", { prompt });
          setResult("standard", result.data);
          if (result.ok) {
            savedPrompt = prompt;
            controlledButton.disabled = false;
            document.getElementById("saved-prompt").textContent = `Для второго вызова сохранён промпт:\n${savedPrompt}`;
            document.getElementById("saved-prompt").classList.remove("hidden");
            updatePreviews();
          }
        } catch (error) {
          setResult("standard", { error: "Не удалось связаться с сервером приложения." });
        } finally {
          standardButton.disabled = false;
        }
      });

      controlledButton.addEventListener("click", async () => {
        const controls = {
          format_instruction: formatInput.value.trim(),
          max_tokens: Number(maxTokensInput.value),
          stop_sequence: stopInput.value.trim()
        };
        updatePreviews();
        controlledButton.disabled = true;
        setResult("controlled", { answer: "DeepSeek отвечает с настройками..." });
        try {
          const result = await postJson("/ask-controlled", { prompt: savedPrompt, controls });
          setResult("controlled", result.data);
          document.getElementById("controlled-config").textContent =
            `Формат: ${controls.format_instruction}; max_tokens: ${controls.max_tokens}; stop: ${controls.stop_sequence}`;
        } catch (error) {
          setResult("controlled", { error: "Не удалось связаться с сервером приложения." });
        } finally {
          controlledButton.disabled = false;
        }
      });

      [formatInput, maxTokensInput, stopInput].forEach((input) => {
        input.addEventListener("input", updatePreviews);
      });
    </script>
  </body>
</html>
"""


def get_client():
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)


def completion_data(completion):
    choice = completion.choices[0]
    answer = choice.message.content or "Модель вернула пустой ответ."
    usage = getattr(completion, "usage", None)
    return {
        "answer": answer,
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "finish_reason": choice.finish_reason,
    }


def validate_prompt(payload):
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        return None, "Введите непустой промпт."
    return prompt.strip(), None


def validate_controls(payload):
    controls = payload.get("controls") if isinstance(payload, dict) else None
    if not isinstance(controls, dict):
        return None, "Передайте настройки контролируемого ответа."

    format_instruction = controls.get("format_instruction")
    stop_sequence = controls.get("stop_sequence")
    max_tokens = controls.get("max_tokens")
    if not isinstance(format_instruction, str) or not format_instruction.strip():
        return None, "Заполните инструкцию формата ответа."
    if not isinstance(stop_sequence, str) or not stop_sequence.strip():
        return None, "Заполните stop sequence."
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
        return None, "max_tokens должен быть целым числом."
    if not 1 <= max_tokens <= MAX_ALLOWED_TOKENS:
        return None, f"max_tokens должен быть от 1 до {MAX_ALLOWED_TOKENS}."

    return {
        "format_instruction": format_instruction.strip(),
        "stop_sequence": stop_sequence.strip(),
        "max_tokens": max_tokens,
    }, None


def controlled_system_message(format_instruction, stop_sequence):
    return (
        f"Следуй инструкции формата ответа: {format_instruction}\n\n"
        f"После основного ответа напиши на отдельной строке точную последовательность "
        f"{stop_sequence}. Не добавляй текст после этой последовательности."
    )


def request_completion(messages, **options):
    client = get_client()
    if not client:
        return None, (jsonify(error="Не задан DEEPSEEK_API_KEY. Укажите ключ и перезапустите приложение."), 503)

    try:
        completion = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            **options,
        )
        return completion_data(completion), None
    except Exception:
        app.logger.exception("DeepSeek request failed")
        return None, (jsonify(error="DeepSeek временно недоступен. Попробуйте ещё раз позже."), 502)


@app.get("/")
def index():
    return PAGE


@app.post("/ask")
def ask():
    payload = request.get_json(silent=True)
    prompt, error = validate_prompt(payload)
    if error:
        return jsonify(error=error), 400

    data, api_error = request_completion([{"role": "user", "content": prompt}])
    if api_error:
        return api_error
    return jsonify(data)


@app.post("/ask-controlled")
def ask_controlled():
    payload = request.get_json(silent=True)
    prompt, error = validate_prompt(payload)
    if error:
        return jsonify(error=error), 400
    controls, error = validate_controls(payload)
    if error:
        return jsonify(error=error), 400

    messages = [
        {
            "role": "system",
            "content": controlled_system_message(
                controls["format_instruction"], controls["stop_sequence"]
            ),
        },
        {"role": "user", "content": prompt},
    ]
    data, api_error = request_completion(
        messages,
        max_tokens=controls["max_tokens"],
        stop=[controls["stop_sequence"]],
    )
    if api_error:
        return api_error
    return jsonify(data)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
