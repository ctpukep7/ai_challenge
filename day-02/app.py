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
      body { max-width: 840px; margin: 40px auto; padding: 0 16px 48px; background: #fafafa; }
      h1 { margin-bottom: 8px; }
      textarea, input, select { box-sizing: border-box; width: 100%; padding: 10px; font: inherit; }
      textarea { min-height: 150px; resize: vertical; }
      button { padding: 10px 16px; border: 0; border-radius: 7px; background: #1267e3; color: #fff; font: inherit; cursor: pointer; }
      button:disabled { cursor: not-allowed; opacity: .55; }
      .actions { display: flex; align-items: center; gap: 10px; margin-top: 12px; }
      .gear { min-width: 42px; padding: 10px; font-size: 1.15rem; }
      .section { margin-top: 22px; padding: 16px; border: 1px solid #d6d6d6; border-radius: 8px; background: #fff; }
      .section h2 { margin-top: 0; font-size: 1.1rem; }
      pre { overflow-x: auto; margin: 0; padding: 12px; border-radius: 6px; background: #1f2430; color: #d7e3ff; white-space: pre-wrap; }
      .answer { min-height: 28px; white-space: pre-wrap; line-height: 1.5; }
      .meta { color: #555; font-size: .9em; }
      .error { color: #b00020; }
      dialog { width: min(560px, calc(100vw - 32px)); padding: 0; border: 0; border-radius: 10px; box-shadow: 0 16px 40px rgba(0, 0, 0, .28); }
      dialog::backdrop { background: rgba(0, 0, 0, .38); }
      .modal { padding: 20px; }
      .modal-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
      .modal-header h2 { margin: 0; }
      .close { padding: 4px 9px; background: #e7e7e7; color: #222; font-size: 1.25rem; }
      label { display: block; margin-top: 16px; font-weight: 700; }
      label span { display: block; margin-top: 4px; color: #555; font-size: .9em; font-weight: 400; }
      .modal textarea { min-height: 90px; }
      .hint { color: #555; font-size: .9em; }
      .stop-list { display: grid; gap: 8px; margin-top: 8px; }
      .stop-row { display: grid; grid-template-columns: 1fr auto auto; gap: 8px; align-items: center; }
      .stop-row button { min-width: 38px; padding: 8px 10px; font-size: 1.1rem; }
      .stop-remove { background: #e7e7e7; color: #222; }
    </style>
  </head>
  <body>
    <h1>Контроль ответа DeepSeek</h1>
    <p>Отправьте промпт без настроек или добавьте нужные ограничения через шестерёнку.</p>

    <label for="prompt">Ваш промпт</label>
    <textarea id="prompt" placeholder="Напишите вопрос для DeepSeek"></textarea>
    <div class="actions">
      <button id="send" type="button">Отправить</button>
      <button id="settings" class="gear" type="button" aria-label="Открыть настройки" title="Настройки">⚙</button>
    </div>

    <section class="section">
      <h2>Превью JSON-запроса</h2>
      <pre id="preview">Введите промпт, чтобы увидеть запрос.</pre>
    </section>

    <section class="section">
      <h2>Ответ</h2>
      <div id="answer" class="answer">Ответ появится здесь.</div>
      <p id="meta" class="meta"></p>
    </section>

    <dialog id="settings-dialog">
      <div class="modal">
        <div class="modal-header">
          <h2>Настройки ответа</h2>
          <button id="close-settings" class="close" type="button" aria-label="Закрыть настройки">×</button>
        </div>
        <p class="hint">Пустые поля не добавляются в API-запрос.</p>

        <label for="format-instruction">Формат ответа
          <span>Например: «Заголовок и ровно 3 маркированных пункта».</span>
        </label>
        <textarea id="format-instruction" placeholder="Оставьте пустым, чтобы не задавать формат"></textarea>

        <label for="max-tokens">Длина ответа (`max_tokens`)
          <span>Максимальное количество генерируемых токенов.</span>
        </label>
        <input id="max-tokens" type="number" min="1" max="10000" placeholder="Например: 120">

        <label for="stop-sequences">Условия завершения (`stop`)
          <span>Добавляйте уникальные маркеры. API остановится при первом из них; максимум 16 за запрос.</span>
        </label>
        <div id="stop-sequences" class="stop-list">
          <div class="stop-row">
            <input class="stop-sequence" type="text" placeholder="Например: &lt;&lt;&lt;END_OF_ANSWER&gt;&gt;&gt;">
            <button class="stop-add" data-add-stop type="button" aria-label="Добавить условие завершения" title="Добавить условие">＋</button>
          </div>
        </div>

        <label for="thinking-mode">Thinking mode
          <span>По умолчанию выключен, чтобы токены сразу шли на финальный ответ.</span>
        </label>
        <select id="thinking-mode">
          <option value="disabled" selected>Выключить (по умолчанию)</option>
          <option value="enabled">Включить</option>
          <option value="">Не задавать (режим DeepSeek)</option>
        </select>
      </div>
    </dialog>

    <script>
      const promptInput = document.getElementById("prompt");
      const sendButton = document.getElementById("send");
      const settingsButton = document.getElementById("settings");
      const settingsDialog = document.getElementById("settings-dialog");
      const closeSettingsButton = document.getElementById("close-settings");
      const formatInput = document.getElementById("format-instruction");
      const maxTokensInput = document.getElementById("max-tokens");
      const stopInputsContainer = document.getElementById("stop-sequences");
      const thinkingInput = document.getElementById("thinking-mode");
      const preview = document.getElementById("preview");
      const answer = document.getElementById("answer");
      const meta = document.getElementById("meta");

      function stopSequences() {
        return [...stopInputsContainer.querySelectorAll(".stop-sequence")]
          .map((input) => input.value.trim())
          .filter(Boolean);
      }

      function addStopRow(afterRow) {
        const row = document.createElement("div");
        row.className = "stop-row";

        const input = document.createElement("input");
        input.className = "stop-sequence";
        input.type = "text";
        input.placeholder = "Ещё один маркер";

        const addButton = document.createElement("button");
        addButton.className = "stop-add";
        addButton.dataset.addStop = "";
        addButton.type = "button";
        addButton.setAttribute("aria-label", "Добавить условие завершения");
        addButton.title = "Добавить условие";
        addButton.textContent = "＋";

        const removeButton = document.createElement("button");
        removeButton.className = "stop-remove";
        removeButton.dataset.removeStop = "";
        removeButton.type = "button";
        removeButton.setAttribute("aria-label", "Удалить условие завершения");
        removeButton.title = "Удалить условие";
        removeButton.textContent = "×";

        row.append(input, addButton, removeButton);
        afterRow.after(row);
        input.focus();
        updatePreview();
      }

      function currentSettings() {
        return {
          format_instruction: formatInput.value.trim(),
          max_tokens: maxTokensInput.value.trim(),
          stop_sequences: stopSequences(),
          thinking_mode: thinkingInput.value
        };
      }

      function systemMessage(settings) {
        const instructions = [];
        if (settings.format_instruction) {
          instructions.push(`Следуй инструкции формата ответа: ${settings.format_instruction}`);
        }
        if (settings.stop_sequences.length) {
          instructions.push(
            `После основного ответа напиши на отдельной строке одну из точных последовательностей: ` +
            `${settings.stop_sequences.join(", ")}. ` +
            "Не добавляй текст после этой последовательности."
          );
        }
        return instructions.join("\\n\\n");
      }

      function previewPayload() {
        const prompt = promptInput.value.trim();
        const settings = currentSettings();
        const messages = [{ role: "user", content: prompt }];
        const instruction = systemMessage(settings);
        if (instruction) messages.unshift({ role: "system", content: instruction });

        const payload = { model: "deepseek-v4-flash", messages };
        if (settings.max_tokens) payload.max_tokens = Number(settings.max_tokens);
        if (settings.stop_sequences.length) payload.stop = settings.stop_sequences;
        if (settings.thinking_mode) payload.thinking = { type: settings.thinking_mode };
        return payload;
      }

      function updatePreview() {
        const prompt = promptInput.value.trim();
        preview.textContent = prompt
          ? JSON.stringify(previewPayload(), null, 2)
          : "Введите промпт, чтобы увидеть запрос.";
      }

      function showResult(data) {
        answer.classList.remove("error");
        if (data.error) {
          answer.textContent = data.error;
          answer.classList.add("error");
          meta.textContent = "";
          return;
        }
        answer.textContent = data.answer;
        const reasoning = data.reasoning_tokens == null ? "—" : data.reasoning_tokens;
        meta.textContent =
          `Выходные токены: ${data.completion_tokens ?? "—"}; ` +
          `причина завершения: ${data.finish_reason ?? "—"}; ` +
          `thinking: ${data.thinking_mode}; ` +
          `reasoning-токены: ${reasoning}`;
      }

      async function sendPrompt() {
        const prompt = promptInput.value.trim();
        if (!prompt) {
          showResult({ error: "Введите промпт." });
          return;
        }

        sendButton.disabled = true;
        showResult({ answer: "DeepSeek отвечает...", thinking_mode: "—" });
        try {
          const response = await fetch("/ask", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt, settings: currentSettings() })
          });
          showResult(await response.json());
        } catch (error) {
          showResult({ error: "Не удалось связаться с сервером приложения." });
        } finally {
          sendButton.disabled = false;
        }
      }

      settingsButton.addEventListener("click", () => settingsDialog.showModal());
      closeSettingsButton.addEventListener("click", () => settingsDialog.close());
      sendButton.addEventListener("click", sendPrompt);
      stopInputsContainer.addEventListener("click", (event) => {
        const addButton = event.target.closest("[data-add-stop]");
        if (addButton) {
          addStopRow(addButton.closest(".stop-row"));
          return;
        }
        const removeButton = event.target.closest("[data-remove-stop]");
        if (removeButton) {
          removeButton.closest(".stop-row").remove();
          updatePreview();
        }
      });
      stopInputsContainer.addEventListener("input", updatePreview);
      [promptInput, formatInput, maxTokensInput, thinkingInput].forEach((input) => {
        input.addEventListener("input", updatePreview);
        input.addEventListener("change", updatePreview);
      });
    </script>
  </body>
</html>
"""


def value_or_attribute(value, name):
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def get_client():
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)


def validate_prompt(payload):
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        return None, "Введите непустой промпт."
    return prompt.strip(), None


def validate_settings(payload):
    raw_settings = payload.get("settings", {}) if isinstance(payload, dict) else {}
    if raw_settings is None:
        raw_settings = {}
    if not isinstance(raw_settings, dict):
        return None, "Настройки должны быть объектом."

    format_instruction = raw_settings.get("format_instruction", "")
    raw_stop_sequences = raw_settings.get(
        "stop_sequences", raw_settings.get("stop_sequence", "")
    )
    # DeepSeek включает рассуждения по умолчанию. Для учебного приложения
    # безопаснее отключать их, пока пользователь явно не выберет другой режим.
    thinking_mode = raw_settings.get("thinking_mode", "disabled")
    raw_max_tokens = raw_settings.get("max_tokens", "")

    if not isinstance(format_instruction, str):
        return None, "Инструкция формата должна быть строкой."
    if isinstance(raw_stop_sequences, str):
        raw_stop_sequences = raw_stop_sequences.splitlines()
    if not isinstance(raw_stop_sequences, list) or not all(
        isinstance(sequence, str) for sequence in raw_stop_sequences
    ):
        return None, "Условия завершения должны быть списком строк."
    stop_sequences = list(dict.fromkeys(
        sequence.strip() for sequence in raw_stop_sequences if sequence.strip()
    ))
    if len(stop_sequences) > 16:
        return None, "Можно указать не более 16 условий завершения."
    if thinking_mode not in ("", "enabled", "disabled"):
        return None, "Thinking mode должен быть enabled, disabled или пустым."

    if raw_max_tokens in (None, ""):
        max_tokens = None
    elif isinstance(raw_max_tokens, bool):
        return None, "max_tokens должен быть целым числом."
    else:
        try:
            max_tokens = int(raw_max_tokens)
        except (TypeError, ValueError):
            return None, "max_tokens должен быть целым числом."
        if str(max_tokens) != str(raw_max_tokens).strip() and not isinstance(raw_max_tokens, int):
            return None, "max_tokens должен быть целым числом."
        if not 1 <= max_tokens <= MAX_ALLOWED_TOKENS:
            return None, f"max_tokens должен быть от 1 до {MAX_ALLOWED_TOKENS}."

    return {
        "format_instruction": format_instruction.strip(),
        "stop_sequences": stop_sequences,
        "thinking_mode": thinking_mode,
        "max_tokens": max_tokens,
    }, None


def system_message(settings):
    instructions = []
    if settings["format_instruction"]:
        instructions.append(f"Следуй инструкции формата ответа: {settings['format_instruction']}")
    if settings["stop_sequences"]:
        instructions.append(
            "После основного ответа напиши на отдельной строке одну из точных "
            f"последовательностей: {', '.join(settings['stop_sequences'])}. "
            "Не добавляй текст после этой последовательности."
        )
    return "\n\n".join(instructions)


def thinking_label(thinking_mode):
    return {
        "enabled": "включён",
        "disabled": "выключен",
        "": "по умолчанию DeepSeek (включён)",
    }[thinking_mode]


def completion_data(completion, thinking_mode):
    choice = completion.choices[0]
    content = choice.message.content
    if content:
        answer = content
    elif choice.finish_reason == "length":
        answer = (
            "Финальный текст не сформирован: лимит токенов исчерпан до ответа. "
            "Увеличьте max_tokens или выключите thinking mode."
        )
    else:
        answer = "Модель вернула пустой финальный ответ."

    usage = getattr(completion, "usage", None)
    details = value_or_attribute(usage, "completion_tokens_details")
    return {
        "answer": answer,
        "completion_tokens": value_or_attribute(usage, "completion_tokens"),
        "finish_reason": choice.finish_reason,
        "thinking_mode": thinking_label(thinking_mode),
        "reasoning_tokens": value_or_attribute(details, "reasoning_tokens"),
    }


def request_completion(prompt, settings):
    client = get_client()
    if not client:
        return None, (jsonify(error="Не задан DEEPSEEK_API_KEY. Укажите ключ и перезапустите приложение."), 503)

    messages = [{"role": "user", "content": prompt}]
    instruction = system_message(settings)
    if instruction:
        messages.insert(0, {"role": "system", "content": instruction})

    options = {}
    if settings["max_tokens"] is not None:
        options["max_tokens"] = settings["max_tokens"]
    if settings["stop_sequences"]:
        options["stop"] = settings["stop_sequences"]
    if settings["thinking_mode"]:
        options["extra_body"] = {"thinking": {"type": settings["thinking_mode"]}}

    try:
        completion = client.chat.completions.create(model=MODEL, messages=messages, **options)
        return completion_data(completion, settings["thinking_mode"]), None
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
    settings, error = validate_settings(payload)
    if error:
        return jsonify(error=error), 400

    data, api_error = request_completion(prompt, settings)
    if api_error:
        return api_error
    return jsonify(data)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
