import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import perf_counter

import bleach
import markdown
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from openai import OpenAI


app = Flask(__name__)
load_dotenv()

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
TEMPERATURE = 0
MAX_ALLOWED_TOKENS = 10_000

TASK_PRESETS = (
    {
        "id": "alphabetical-order",
        "title": "Алфавитный порядок",
        "description": "Выписать слова из стихотворения по русскому алфавиту.",
        "prompt": "В лесу родилась ёлочка,\nВ лесу она росла.\nЗимой и летом стройная,\nЗелёная была.\n\nВыпиши все слова из этого текста в алфавитном порядке.",
    },
    {
        "id": "animal-crossing",
        "title": "Переправа животных",
        "description": "Кошка, собака и морская свинка у дороги.",
        "prompt": "Хозяин хочет перевести через автодорогу своих домашних кошку, собаку и морскую свинку. Но он может взять в руки только двоих за один раз. Если кошка и собака останутся без присмотра на одной стороне, собака нападёт на кошку. Если кошка и морская свинка останутся без присмотра на одной стороне, кошка съест морскую свинку. Как хозяину переправить их всех через дорогу, никого не потеряв?",
    },
    {
        "id": "letter-constraint",
        "title": "Запрет буквы «а»",
        "description": "Два предложения о зиме без единой буквы «а».",
        "prompt": "Напиши два предложения про зимний пейзаж за окном, выбирая слова так, чтобы ни разу не использовать букву «а».",
    },
)


@dataclass(frozen=True)
class ModelSpec:
    id: str
    role: str
    title: str
    provider: str
    model: str
    base_url: str
    key_name: str
    accent: str
    soft: str
    input_cache_hit_price: float = 0
    input_cache_miss_price: float = 0
    output_price: float = 0


MODELS = (
    ModelSpec(
        id="weak", role="СЛАБАЯ", title="LFM 2.5 · 2.6B", provider="OpenRouter",
        model="liquid/lfm-2.5-2.6b:free", base_url=OPENROUTER_BASE_URL,
        key_name="OPENROUTER_API_KEY", accent="#4f78cc", soft="#ebf2ff",
    ),
    ModelSpec(
        id="medium", role="СРЕДНЯЯ", title="DeepSeek V4 Flash", provider="DeepSeek",
        model="deepseek-v4-flash", base_url=DEEPSEEK_BASE_URL,
        key_name="DEEPSEEK_API_KEY", accent="#20a184", soft="#e5f7f1",
        input_cache_hit_price=0.0028, input_cache_miss_price=0.14, output_price=0.28,
    ),
    ModelSpec(
        id="strong", role="СИЛЬНАЯ", title="DeepSeek V4 Pro", provider="DeepSeek",
        model="deepseek-v4-pro", base_url=DEEPSEEK_BASE_URL,
        key_name="DEEPSEEK_API_KEY", accent="#9d63d9", soft="#f2eaff",
        input_cache_hit_price=0.003625, input_cache_miss_price=0.435, output_price=0.87,
    ),
)

ALLOWED_MARKDOWN_TAGS = [
    "p", "br", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "hr", "table", "thead", "tbody", "tr", "th", "td",
]


PAGE = """<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Day 05 — Версии моделей</title>
    <style>
      :root { color: #22375c; font-family: Inter, ui-sans-serif, system-ui, sans-serif; } * { box-sizing: border-box; }
      body { min-width: 320px; margin: 0; background: #f5f8fe; } main { width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 52px 0 64px; }
      .eyebrow { margin: 0; color: #6985b8; font-size: .72rem; font-weight: 850; letter-spacing: .14em; } h1 { margin: 10px 0; color: #172d57; font-size: clamp(2.3rem, 5.4vw, 4.8rem); letter-spacing: -.07em; line-height: .9; }.lead { max-width: 700px; margin: 0; color: #647493; line-height: 1.65; }
      .panel, .card, .guide { border: 1px solid #dce6f5; border-radius: 18px; background: rgba(255,255,255,.92); box-shadow: 0 14px 36px rgba(43,76,132,.08); }.panel { margin-top: 30px; padding: 22px; }.panel-top { display: flex; align-items: end; justify-content: space-between; gap: 18px; } label { display: block; color: #385173; font-size: .86rem; font-weight: 800; } label span { display: block; margin-top: 4px; color: #7b8ba6; font-size: .78rem; font-weight: 500; } textarea { width: 100%; min-height: 190px; margin-top: 11px; padding: 14px; border: 1px solid #cddcf3; border-radius: 11px; background: #fbfdff; color: #263a5c; font: inherit; line-height: 1.55; outline: none; resize: vertical; } textarea:focus { border-color: #5d8de1; box-shadow: 0 0 0 3px #eaf1ff; }
      button { border: 0; border-radius: 10px; padding: 11px 15px; background: #2e67ca; color: #fff; font: inherit; font-weight: 800; cursor: pointer; box-shadow: 0 6px 16px rgba(46,103,202,.24); } button:disabled { cursor: not-allowed; opacity: .55; }.secondary { background: #edf3fd; color: #476da8; box-shadow: none; }.controls { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 13px; }.request-note { margin: 12px 0 0; color: #74839d; font-size: .79rem; }.request-note code, .metric code { padding: 2px 5px; border-radius: 4px; background: #edf3fd; color: #46699f; }
      .results { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 15px; margin-top: 20px; }.card { min-height: 405px; padding: 18px; border-top: 4px solid var(--accent); }.card-top { display: flex; justify-content: space-between; gap: 10px; align-items: start; }.badge { display: inline-block; padding: 5px 8px; border-radius: 999px; background: var(--soft); color: var(--accent); font-size: .7rem; font-weight: 850; letter-spacing: .06em; }.provider { margin: 6px 0 0; color: #7d8ba3; font-size: .76rem; }.card h2 { margin: 9px 0 0; color: #253b64; font-size: 1.14rem; }.model-id { margin: 5px 0 13px; color: #70809b; font: .7rem/1.35 ui-monospace, monospace; overflow-wrap: anywhere; }.answer { color: #354866; line-height: 1.6; }.answer > :first-child { margin-top: 0; }.answer > :last-child { margin-bottom: 0; }.answer h1, .answer h2, .answer h3 { color: #263f6d; line-height: 1.25; }.answer h1 { font-size: 1.16rem; }.answer h2 { font-size: 1.05rem; }.answer h3 { font-size: .98rem; }.answer p, .answer ul, .answer ol, .answer blockquote { margin: .7em 0; }.answer code { padding: .1em .3em; border-radius: 4px; background: #edf3fd; color: #254a86; font: .86em ui-monospace, monospace; }.answer pre { overflow: auto; padding: 10px; border-radius: 8px; background: #142440; color: #dceaff; }.answer pre code { padding: 0; background: transparent; color: inherit; }.answer blockquote { padding-left: 10px; border-left: 3px solid #9eb9ec; color: #667897; }.waiting { color: #8492a9; font-style: italic; }.error { color: #b12648; line-height: 1.55; }
      .metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; margin-top: 16px; padding-top: 11px; border-top: 1px solid #e3ebf8; }.metric { min-width: 0; color: #78869e; font-size: .7rem; }.metric strong { display: block; margin-top: 2px; color: #3f5476; font-size: .82rem; overflow-wrap: anywhere; }.metric.wide { grid-column: 1 / -1; } details { margin-top: 12px; } summary { color: #4d70a5; font-size: .76rem; font-weight: 750; cursor: pointer; } pre { overflow: auto; margin: 8px 0 0; padding: 10px; border-radius: 8px; background: #142440; color: #dceaff; font: .7rem/1.5 ui-monospace, monospace; white-space: pre-wrap; }
      .guide { margin-top: 20px; padding: 21px; }.guide h2 { margin: 0; color: #28416d; font-size: 1.08rem; }.guide > p { margin: 6px 0 0; color: #71809a; font-size: .85rem; }.guide-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 14px; }.guide-grid div { padding: 13px; border-radius: 10px; background: #f6f9ff; }.guide-grid strong { display: block; color: #446aab; font-size: .83rem; }.guide-grid p { margin: 5px 0 0; color: #6b7b96; font-size: .79rem; line-height: 1.45; }
      @media (max-width: 880px) { .results { grid-template-columns: 1fr; }.card { min-height: 0; }.guide-grid { grid-template-columns: 1fr; } } @media (max-width: 640px) { main { width: min(100% - 24px, 1180px); padding-top: 34px; }.panel { padding: 16px; }.panel-top { display: block; }.controls button { flex: 1; }.controls .secondary { flex-basis: 100%; }.results { gap: 12px; } }
    </style>
  </head>
  <body>
    <main>
      <p class="eyebrow">AI CHALLENGE 9 · DAY 05</p>
      <h1>Лаборатория<br>версий моделей</h1>
      <p class="lead">Одна логическая задача, три уровня моделей и прозрачное сравнение качества, времени, токенов и стоимости.</p>
      <section class="panel" aria-label="Задача для сравнения">
        <div class="panel-top"><label for="prompt">Одинаковый промпт для всех моделей<span>Логический пресет имеет эталонный результат: виновен Антон.</span></label></div>
        <textarea id="prompt" placeholder="Введите задачу или загрузите логический пресет"></textarea>
        <div class="controls"><button id="preset" class="secondary" type="button">Логический пресет</button><button id="compare" type="button">Запустить сравнение →</button></div>
        <p class="request-note">Во всех вызовах одинаковы system prompt, user prompt, <code>temperature: 0</code> и <code>max_tokens: 800</code>. DeepSeek работает без thinking mode.</p>
      </section>
      <section id="results" class="results" aria-label="Результаты сравнения моделей"></section>
      <section class="guide"><h2>Лист ручной оценки</h2><p>Сверьте ответы с эталоном и зафиксируйте вывод для видео.</p><div class="guide-grid"><div><strong>Точность</strong><p>Кто правильно назвал Антона и исключил остальные гипотезы?</p></div><div><strong>Качество</strong><p>У кого объяснение короче, понятнее и не пропускает проверку?</p></div><div><strong>Ресурсы</strong><p>Сопоставьте время, токены и стоимость. Самая сильная модель не обязана быть оптимальной.</p></div></div></section>
    </main>
    <script>
      const models = [
        { role: "СЛАБАЯ", title: "LFM 2.5 · 2.6B", provider: "OpenRouter", model: "liquid/lfm-2.5-2.6b:free", accent: "#4f78cc", soft: "#ebf2ff" },
        { role: "СРЕДНЯЯ", title: "DeepSeek V4 Flash", provider: "DeepSeek", model: "deepseek-v4-flash", accent: "#20a184", soft: "#e5f7f1" },
        { role: "СИЛЬНАЯ", title: "DeepSeek V4 Pro", provider: "DeepSeek", model: "deepseek-v4-pro", accent: "#9d63d9", soft: "#f2eaff" }
      ];
      const logicPreset = {{ logic_preset|tojson }};
      const promptInput = document.getElementById("prompt"), compareButton = document.getElementById("compare"), presetButton = document.getElementById("preset"), results = document.getElementById("results");
      const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[character]);
      const metric = (label, value, wide = false) => `<div class="metric ${wide ? "wide" : ""}">${label}<strong>${escapeHtml(value)}</strong></div>`;
      function renderCards(items) { results.innerHTML = items.map((item, index) => { const model = models[index]; const content = item.error ? `<p class="error">${escapeHtml(item.error)}</p>` : item.waiting ? `<p class="waiting">Модель отвечает…</p>` : `<div class="answer">${item.answer_html}</div>`; const metrics = item.waiting || item.error ? "" : `<div class="metrics">${metric("Время", item.elapsed_ms == null ? "—" : `${(item.elapsed_ms / 1000).toFixed(2)} с`)}${metric("Входные токены", item.prompt_tokens ?? "—")}${metric("Выходные токены", item.completion_tokens ?? "—")}${metric("Стоимость", item.cost_usd == null ? "Не удалось вычислить" : `$${item.cost_usd.toFixed(6)}`)}${metric("Причина завершения", item.finish_reason ?? "—", true)}</div>`; const json = item.request ? `<details><summary>JSON-запрос без ключа</summary><pre>${escapeHtml(JSON.stringify(item.request, null, 2))}</pre></details>` : ""; return `<article class="card" style="--accent:${model.accent};--soft:${model.soft}"><div class="card-top"><div><span class="badge">${model.role}</span><h2>${model.title}</h2><p class="provider">${model.provider}</p></div></div><p class="model-id">${model.model}</p>${content}${metrics}${json}</article>`; }).join(""); }
      async function compare() { const prompt = promptInput.value.trim(); if (!prompt) { renderCards(models.map(() => ({ error: "Введите задачу или нажмите «Логический пресет»." }))); return; } compareButton.disabled = true; renderCards(models.map(() => ({ waiting: true }))); try { const response = await fetch("/compare", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt }) }); const data = await response.json(); renderCards(data.results ?? models.map(() => ({ error: data.error || "Не удалось получить ответ." }))); } catch (_) { renderCards(models.map(() => ({ error: "Не удалось связаться с сервером приложения." }))); } finally { compareButton.disabled = false; } }
      presetButton.addEventListener("click", () => { promptInput.value = logicPreset; promptInput.focus(); }); compareButton.addEventListener("click", compare); renderCards(models.map(() => ({ waiting: true })));
    </script>
  </body>
</html>"""


def value_or_attribute(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def usage_value(usage, name):
    value = value_or_attribute(usage, name)
    return value if isinstance(value, int) else None


def validate_controls(payload):
    raw = payload.get("controls", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return None, "Настройки должны быть объектом."

    raw_temperature = raw.get("temperature", TEMPERATURE)
    raw_max_tokens = raw.get("max_tokens", "")
    if isinstance(raw_temperature, bool):
        return None, "temperature должна быть числом от 0 до 2."
    try:
        temperature = float(raw_temperature)
    except (TypeError, ValueError):
        return None, "temperature должна быть числом от 0 до 2."
    if not 0 <= temperature <= 2:
        return None, "temperature должна быть числом от 0 до 2."
    if raw_max_tokens is None or (isinstance(raw_max_tokens, str) and not raw_max_tokens.strip()):
        max_tokens = None
    else:
        if isinstance(raw_max_tokens, bool):
            return None, "max_tokens должен быть целым числом."
        try:
            max_tokens = int(raw_max_tokens)
        except (TypeError, ValueError):
            return None, "max_tokens должен быть целым числом."
        if not 1 <= max_tokens <= MAX_ALLOWED_TOKENS:
            return None, f"max_tokens должен быть от 1 до {MAX_ALLOWED_TOKENS}."
    return {"temperature": temperature, "max_tokens": max_tokens}, None


def messages_for(prompt):
    return [{"role": "user", "content": prompt}]


def public_request(spec, prompt, controls=None):
    controls = controls or {"temperature": TEMPERATURE, "max_tokens": None}
    body = {
        "provider": spec.provider,
        "model": spec.model,
        "messages": messages_for(prompt),
        "temperature": controls["temperature"],
    }
    if controls["max_tokens"] is not None:
        body["max_tokens"] = controls["max_tokens"]
    if spec.provider == "DeepSeek":
        body["thinking"] = {"type": "disabled"}
    return body


def render_markdown(answer):
    html = markdown.markdown(answer, extensions=["extra", "sane_lists"])
    return bleach.clean(html, tags=ALLOWED_MARKDOWN_TAGS, attributes={}, strip=True)


def deepseek_cost(spec, usage):
    cache_hit = usage_value(usage, "prompt_cache_hit_tokens")
    cache_miss = usage_value(usage, "prompt_cache_miss_tokens")
    completion_tokens = usage_value(usage, "completion_tokens")
    if None in (cache_hit, cache_miss, completion_tokens):
        return None
    return round(
        (cache_hit * spec.input_cache_hit_price + cache_miss * spec.input_cache_miss_price
         + completion_tokens * spec.output_price) / 1_000_000,
        9,
    )


def error_result(spec, prompt, controls, message):
    return {"id": spec.id, "error": message, "request": public_request(spec, prompt, controls)}


def run_model(spec, prompt, controls):
    api_key = os.getenv(spec.key_name)
    if not api_key:
        return error_result(spec, prompt, controls, f"Не задан {spec.key_name} в файле .env.")

    options = {
        "model": spec.model,
        "messages": messages_for(prompt),
        "temperature": controls["temperature"],
    }
    if controls["max_tokens"] is not None:
        options["max_tokens"] = controls["max_tokens"]
    if spec.provider == "DeepSeek":
        options["extra_body"] = {"thinking": {"type": "disabled"}}

    client = OpenAI(api_key=api_key, base_url=spec.base_url)
    started_at = perf_counter()
    try:
        completion = client.chat.completions.create(**options)
    except Exception:
        app.logger.exception("Model request failed: %s", spec.model)
        return error_result(spec, prompt, controls, "Модель временно недоступна. Попробуйте ещё раз позже.")

    elapsed_ms = round((perf_counter() - started_at) * 1000)
    usage = getattr(completion, "usage", None)
    answer = completion.choices[0].message.content
    answer = answer.strip() if isinstance(answer, str) else ""
    prompt_tokens = usage_value(usage, "prompt_tokens")
    if prompt_tokens is None:
        cache_hit = usage_value(usage, "prompt_cache_hit_tokens")
        cache_miss = usage_value(usage, "prompt_cache_miss_tokens")
        if cache_hit is not None and cache_miss is not None:
            prompt_tokens = cache_hit + cache_miss

    return {
        "id": spec.id,
        "answer_html": render_markdown(answer or "Модель не вернула финальный текст."),
        "elapsed_ms": elapsed_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": usage_value(usage, "completion_tokens"),
        "finish_reason": completion.choices[0].finish_reason,
        "cost_usd": 0 if spec.provider == "OpenRouter" else deepseek_cost(spec, usage),
        "request": public_request(spec, prompt, controls),
    }


@app.get("/")
def index():
    return render_template("index.html", task_presets=TASK_PRESETS)


@app.post("/compare")
def compare():
    payload = request.get_json(silent=True)
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        return jsonify(error="Введите непустой промпт."), 400
    controls, error = validate_controls(payload)
    if error:
        return jsonify(error=error), 400

    clean_prompt = prompt.strip()
    with ThreadPoolExecutor(max_workers=len(MODELS)) as executor:
        results = list(executor.map(lambda spec: run_model(spec, clean_prompt, controls), MODELS))
    return jsonify(results=results)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5004, debug=True)
