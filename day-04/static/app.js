const runs = [
  { temperature: 0, label: '0', title: 'Фокус', description: 'Максимум повторяемости и фокуса.', accent: '#4f78cc', soft: '#ebf2ff' },
  { temperature: 0.7, label: '0.7', title: 'Баланс', description: 'Баланс устойчивости и вариативности.', accent: '#25a788', soft: '#e5f7f1' },
  { temperature: 1.2, label: '1.2', title: 'Вариативность', description: 'Больше необычных формулировок и идей.', accent: '#9b63d9', soft: '#f3eafe' },
  { temperature: 1.5, label: '1.5', title: 'Поиск', description: 'Высокая вариативность и более смелые идеи.', accent: '#e49a35', soft: '#fff5df' },
  { temperature: 1.7, label: '1.7', title: 'Смелость', description: 'Очень смелые формулировки — проверяйте факты.', accent: '#d45d77', soft: '#ffedf1' },
  { temperature: 1.9, label: '1.9', title: 'Экстрим', description: 'Максимальный разброс идей — результат требует проверки.', accent: '#d34d4d', soft: '#ffeded' },
];

const demoPrompt = `Кратко, в 6–8 коротких предложениях, объясни, почему на мокрой дороге автомобилю требуется больше расстояния для торможения.

Обязательно упомяни сцепление шин с дорогой, скорость и тормозной путь.
Структурируй ответ так:
1. объяснение причины;
2. один бытовой пример;
3. список из ровно 2 разных метафор;
4. короткий вывод.`;
const state = { results: [], runId: 0, stops: [''] };
const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' })[character]);
}

function inlineMarkdown(value) {
  return value.replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/\*([^*]+)\*/g, '<em>$1</em>');
}

function renderMarkdown(value) {
  const chunks = escapeHtml(value).split(/(```[\s\S]*?```)/g);
  return chunks.map((chunk) => {
    if (chunk.startsWith('```')) return `<pre><code>${chunk.slice(3, -3).trim()}</code></pre>`;
    let html = ''; let inList = false; let inOrderedList = false;
    for (const line of chunk.split('\n')) {
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      const bullet = line.match(/^\s*[-*]\s+(.+)$/);
      const ordered = line.match(/^\s*\d+\.\s+(.+)$/);
      if (heading) {
        if (inList) { html += '</ul>'; inList = false; } if (inOrderedList) { html += '</ol>'; inOrderedList = false; }
        const level = heading[1].length; html += `<h${level}>${inlineMarkdown(heading[2])}</h${level}>`;
      } else if (bullet) {
        if (inOrderedList) { html += '</ol>'; inOrderedList = false; } if (!inList) { html += '<ul>'; inList = true; } html += `<li>${inlineMarkdown(bullet[1])}</li>`;
      } else if (ordered) {
        if (inList) { html += '</ul>'; inList = false; } if (!inOrderedList) { html += '<ol>'; inOrderedList = true; } html += `<li>${inlineMarkdown(ordered[1])}</li>`;
      } else if (line.trim()) {
        if (inList) { html += '</ul>'; inList = false; } if (inOrderedList) { html += '</ol>'; inOrderedList = false; } html += `<p>${inlineMarkdown(line)}</p>`;
      }
    }
    return `${html}${inList ? '</ul>' : ''}${inOrderedList ? '</ol>' : ''}`;
  }).join('');
}

function controls() {
  return {
    format_instruction: $('#format-instruction').value,
    max_tokens: $('#max-tokens').value,
    stop_sequences: state.stops.map((_, index) => $(`#stop-${index}`).value),
    thinking_mode: $('#thinking-mode').value,
    provider: $('#provider').value,
    model: $('#model-id').value,
  };
}

function renderStops() {
  const list = $('#stop-list');
  list.replaceChildren(...state.stops.map((value, index) => {
    const row = document.createElement('div'); row.className = 'stop-row';
    const input = document.createElement('input'); input.id = `stop-${index}`; input.value = value; input.placeholder = 'Маркер завершения';
    input.addEventListener('input', () => { state.stops[index] = input.value; });
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '×'; remove.setAttribute('aria-label', 'Удалить stop sequence');
    remove.addEventListener('click', () => { state.stops = state.stops.length === 1 ? [''] : state.stops.filter((_, item) => item !== index); renderStops(); });
    row.append(input, remove); return row;
  }));
}

function updateProviderUi(resetModel = false) {
  const isOpenRouter = $('#provider').value === 'openrouter';
  const model = $('#model-id'); const thinking = $('#thinking-mode');
  if (resetModel) model.value = isOpenRouter ? 'openrouter/free' : 'deepseek-v4-flash';
  model.readOnly = !isOpenRouter; thinking.disabled = isOpenRouter;
  $('#provider-hint').textContent = isOpenRouter
    ? 'Можно оставить Free Router или выбрать MiniMax M3 / Nemotron 3 Ultra из подсказок поля. Thinking в OpenRouter не передаётся.'
    : 'DeepSeek V4 Flash. Thinking можно включить в параметрах ответа.';
}

function makeCard(run, item = {}) {
  const card = document.createElement('article');
  card.className = 'result-card';
  card.style.setProperty('--accent', run.accent);
  card.style.setProperty('--soft', run.soft);
  const answer = item.error
    ? `<p class="card-error">${escapeHtml(item.error)}</p>`
    : `<div class="answer ${item.waiting ? 'waiting' : ''}">${item.waiting ? escapeHtml(item.answer || 'Машина ожидает запуск…') : renderMarkdown(item.answer || 'Модель не вернула финальный текст.')}</div>`;
  const provider = item.provider === 'openrouter' ? 'OpenRouter' : 'DeepSeek';
  const requestedModel = item.model || ''; const resolvedModel = item.resolved_model || requestedModel;
  const modelLabel = resolvedModel === requestedModel ? resolvedModel : `${requestedModel} → ${resolvedModel}`;
  const thinking = item.thinking_applied == null ? 'не передаётся' : ({ disabled: 'выключен', enabled: 'включён', '': 'по умолчанию' }[item.thinking_applied] || 'по умолчанию');
  const metadata = item.waiting || item.error ? '' : `<p class="metadata">${provider} · ${escapeHtml(modelLabel)} · Thinking: ${thinking}<br>Время: ${item.elapsed_ms ?? '—'} мс · Токены: ${item.completion_tokens ?? '—'} · Завершение: ${item.finish_reason ?? '—'}</p>`;
  card.innerHTML = `<div class="card-topline"><span class="status-dot"></span><p>${run.title}</p><span class="temperature-badge">T = ${run.label}</span><button class="info" type="button" aria-label="Показать JSON-запрос">i</button></div><p class="card-description">${run.description}</p>${answer}${metadata}`;
  card.querySelector('.info').addEventListener('click', () => showInfo(run, item.request || []));
  return card;
}

function renderResults(items) {
  const results = $('#results');
  results.replaceChildren(...runs.map((run, index) => makeCard(run, items[index] || {})));
}

function showInfo(run, requestBody) {
  $('#info-title').textContent = `temperature = ${run.label} · JSON`;
  $('#info-json').textContent = JSON.stringify(requestBody, null, 2);
  $('#info-dialog').showModal();
}

function renderAnalysis() {
  if (!state.results.length || state.results.some((item) => item.waiting) || state.results.every((item) => item.error)) return;
  $('#analysis').hidden = false;
  const rows = state.results.map((item, index) => {
    const row = document.createElement('tr');
    const temperature = document.createElement('td');
    temperature.textContent = `T = ${runs[index].label}`;
    row.append(temperature);
    [item.elapsed_ms == null ? '—' : `${item.elapsed_ms} мс`, item.completion_tokens ?? '—', item.finish_reason ?? '—'].forEach((value) => {
      const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
    });
    return row;
  });
  $('#analysis-rows').replaceChildren(...rows);
}

function showError(message) { $('#page-error').textContent = message; $('#page-error').hidden = false; }
function clearError() { $('#page-error').hidden = true; }

async function compare() {
  const prompt = $('#prompt').value.trim();
  if (!prompt) return showError('Введите промпт или примените демонстрационный вариант.');
  clearError();
  const runId = ++state.runId;
  const activeProvider = $('#provider').value === 'openrouter' ? 'OpenRouter' : 'DeepSeek';
  state.results = runs.map((run) => ({ answer: `${activeProvider} отвечает при T = ${run.label}…`, waiting: true }));
  $('#analysis').hidden = true;
  renderResults(state.results);
  $('#compare').disabled = true;
  $('#run-summary').textContent = '6 температур · 6 одновременных API-вызовов';
  try {
    const selectedControls = controls();
    await Promise.all(runs.map(async (run, index) => {
      if (runId !== state.runId) return;
      try {
        const response = await fetch('/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt, controls: selectedControls, temperature: run.temperature }),
        });
        const data = await response.json();
        if (runId !== state.runId) return;
        if (!response.ok || data.error) throw new Error(data.error || 'Не удалось получить ответ.');
        state.results[index] = data.result || { error: 'Сервер не вернул результат запуска.' };
      } catch (error) {
        if (runId !== state.runId) return;
        state.results[index] = { error: error.message || 'Не удалось связаться с сервером приложения.' };
      }
      renderResults(state.results);
    }));
    if (runId !== state.runId) return;
    $('#run-summary').textContent = '6 температур · 6 API-вызовов завершены';
    renderAnalysis();
  } finally {
    if (runId === state.runId) $('#compare').disabled = false;
  }
}

function toggleSidebar() {
  const collapsed = $('#control-sidebar').classList.toggle('collapsed');
  $('#machine-layout').classList.toggle('sidebar-is-collapsed', collapsed);
  $('#sidebar-content').hidden = collapsed;
  const button = $('#sidebar-toggle');
  button.setAttribute('aria-expanded', String(!collapsed));
  button.innerHTML = collapsed ? 'Развернуть <span>‹</span>' : 'Свернуть <span>›</span>';
}

$('#demo-prompt').addEventListener('click', () => { $('#prompt').value = demoPrompt; $('#prompt').focus(); });
$('#compare').addEventListener('click', compare);
$('#sidebar-toggle').addEventListener('click', toggleSidebar);
$('#add-stop').addEventListener('click', () => { if (state.stops.length < 16) { state.stops.push(''); renderStops(); } });
$('#provider').addEventListener('change', () => updateProviderUi(true));
document.querySelectorAll('[data-close-dialog]').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
renderStops(); updateProviderUi(); renderResults(runs.map(() => ({ answer: 'Введите промпт и запустите эксперимент.', waiting: true })));
