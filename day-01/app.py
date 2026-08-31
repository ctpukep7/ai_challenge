import os

from flask import Flask, jsonify, request
from openai import OpenAI
from dotenv import load_dotenv


app = Flask(__name__)
load_dotenv()

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-v4-flash"


PAGE = """<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>DeepSeek web chat</title>
    <style>
      body { max-width: 720px; margin: 48px auto; padding: 0 16px; font-family: Arial, sans-serif; }
      textarea { box-sizing: border-box; width: 100%; min-height: 130px; padding: 12px; font: inherit; }
      button { margin-top: 12px; padding: 10px 18px; font: inherit; cursor: pointer; }
      #answer { white-space: pre-wrap; margin-top: 24px; padding: 16px; background: #f4f4f4; border-radius: 8px; }
      #answer:empty { display: none; }
    </style>
  </head>
  <body>
    <h1>Вопрос к DeepSeek</h1>
    <form id="chat-form">
      <textarea id="prompt" placeholder="Напишите вопрос" required></textarea>
      <br>
      <button id="submit" type="submit">Отправить</button>
    </form>
    <div id="answer" aria-live="polite"></div>

    <script>
      const form = document.getElementById("chat-form");
      const promptInput = document.getElementById("prompt");
      const submitButton = document.getElementById("submit");
      const answer = document.getElementById("answer");

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const prompt = promptInput.value.trim();
        if (!prompt) {
          answer.textContent = "Введите вопрос.";
          return;
        }

        submitButton.disabled = true;
        answer.textContent = "DeepSeek думает...";

        try {
          const response = await fetch("/ask", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt })
          });
          const data = await response.json();
          answer.textContent = data.answer || data.error || "Не удалось получить ответ.";
        } catch (error) {
          answer.textContent = "Не удалось связаться с сервером приложения.";
        } finally {
          submitButton.disabled = false;
        }
      });
    </script>
  </body>
</html>
"""


@app.get("/")
def index():
    """Return the one-page chat interface."""
    return PAGE


@app.post("/ask")
def ask():
    """Send one user prompt to DeepSeek and return its text response."""
    payload = request.get_json(silent=True)
    prompt = payload.get("prompt") if isinstance(payload, dict) else None

    if not isinstance(prompt, str) or not prompt.strip():
        return jsonify(error="Введите непустой текст вопроса."), 400

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return jsonify(error="Не задан DEEPSEEK_API_KEY. Укажите ключ и перезапустите приложение."), 503

    try:
        client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        completion = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt.strip()}],
        )
        answer = completion.choices[0].message.content
        if not answer:
            answer = "Модель вернула пустой ответ. Попробуйте задать вопрос ещё раз."
        return jsonify(answer=answer)
    except Exception:
        app.logger.exception("DeepSeek request failed")
        return jsonify(error="DeepSeek временно недоступен. Попробуйте ещё раз позже."), 502


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
