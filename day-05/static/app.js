const models = [
  { id: 'weak', role: 'СЛАБАЯ', title: 'LFM 2.5 · 2.6B', url: 'https://openrouter.ai/liquid/lfm-2.5-2.6b%3Afree', provider: 'OpenRouter', model: 'liquid/lfm-2.5-2.6b:free', description: 'Бесплатная компактная модель Liquid.', accent: '#4f78cc', soft: '#ebf2ff' },
  { id: 'medium', role: 'СРЕДНЯЯ', title: 'DeepSeek V4 Flash', url: 'https://api-docs.deepseek.com/quick_start/pricing', provider: 'DeepSeek', model: 'deepseek-v4-flash', description: 'Быстрая модель DeepSeek.', accent: '#20a184', soft: '#e5f7f1' },
  { id: 'strong', role: 'СИЛЬНАЯ', title: 'DeepSeek V4 Pro', url: 'https://api-docs.deepseek.com/quick_start/pricing', provider: 'DeepSeek', model: 'deepseek-v4-pro', description: 'Старшая модель DeepSeek.', accent: '#9d63d9', soft: '#f2eaff' },
];

const state = { runId: 0, results: [] };
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' })[character]);

function controls() {
  return { temperature: $('#temperature').value, max_tokens: $('#max-tokens').value };
}

function showInfo(model, requestBody) {
  $('#info-title').textContent = `${model.title} · JSON`;
  $('#info-json').textContent = JSON.stringify(requestBody, null, 2);
  $('#info-dialog').showModal();
}

function openPresetDialog() {
  const list = $('#preset-list');
  list.replaceChildren(...window.day05.taskPresets.map((preset) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'preset-choice';
    button.innerHTML = `<strong>${escapeHtml(preset.title)}</strong><span>${escapeHtml(preset.description)}</span>`;
    button.addEventListener('click', () => {
      $('#prompt').value = preset.prompt;
      $('#preset-dialog').close();
      $('#prompt').focus();
    });
    return button;
  }));
  $('#preset-dialog').showModal();
}

function makeCard(model, item = {}) {
  const card = document.createElement('article');
  card.className = 'result-card';
  card.style.setProperty('--accent', model.accent);
  card.style.setProperty('--soft', model.soft);
  const content = item.error
    ? `<p class="card-error">${escapeHtml(item.error)}</p>`
    : item.pending
      ? '<p class="waiting">Ожидает запуска</p>'
    : item.waiting
      ? '<p class="waiting">Модель отвечает…</p>'
      : `<div class="answer">${item.answer_html || 'Модель не вернула финальный текст.'}</div>`;
  const cost = item.cost_usd == null ? 'Не удалось вычислить' : `$${Number(item.cost_usd).toFixed(6)}`;
  const metadata = item.pending || item.waiting || item.error ? '' : `<p class="metadata">${escapeHtml(model.provider)} · ${escapeHtml(model.model)}<br>Время: ${item.elapsed_ms ?? '—'} мс · Вход: ${item.prompt_tokens ?? '—'} · Выход: ${item.completion_tokens ?? '—'}<br>Стоимость: ${cost} · Завершение: ${escapeHtml(item.finish_reason ?? '—')}</p>`;
  card.innerHTML = `<div class="card-topline"><span class="status-dot"></span><p><a class="model-link" href="${escapeHtml(model.url)}" target="_blank" rel="noreferrer" aria-label="Открыть страницу модели ${escapeHtml(model.title)}">${escapeHtml(model.title)} <span aria-hidden="true">↗</span></a></p><span class="model-badge">${model.role}</span><button class="info" type="button" aria-label="Показать JSON-запрос">i</button></div><p class="card-description">${model.description}</p><p class="model-id">${model.model}</p>${content}${metadata}`;
  card.querySelector('.info').addEventListener('click', () => showInfo(model, item.request || {}));
  return card;
}

function renderCards(items) {
  $('#results').replaceChildren(...models.map((model, index) => makeCard(model, items[index] || {})));
}

function displayTime(value) { return value == null ? '—' : `${(value / 1000).toFixed(2)} с`; }
function displayCost(value) { return value == null ? '—' : `$${Number(value).toFixed(6)}`; }

function renderAnalysis() {
  if (state.results.length !== models.length || state.results.every((item) => item.waiting || item.error)) return;
  const completed = state.results.map((item, index) => ({ ...item, model: models[index] })).filter((item) => !item.waiting && !item.error);
  if (!completed.length) return;
  $('#analysis').hidden = false;
  const fastest = completed.reduce((best, item) => (item.elapsed_ms ?? Infinity) < (best.elapsed_ms ?? Infinity) ? item : best);
  const leastTokens = completed.reduce((best, item) => ((item.completion_tokens ?? Infinity) < (best.completion_tokens ?? Infinity) ? item : best));
  const knownCosts = completed.filter((item) => item.cost_usd != null);
  const totalCost = knownCosts.length === completed.length ? knownCosts.reduce((sum, item) => sum + Number(item.cost_usd), 0) : null;
  $('#summary-strip').innerHTML = `<div class="summary-chip"><span>САМЫЙ БЫСТРЫЙ</span><strong>${escapeHtml(fastest.model.title)} · ${displayTime(fastest.elapsed_ms)}</strong></div><div class="summary-chip"><span>МЕНЬШЕ ВСЕГО ВЫХОДНЫХ ТОКЕНОВ</span><strong>${escapeHtml(leastTokens.model.title)} · ${leastTokens.completion_tokens ?? '—'}</strong></div><div class="summary-chip"><span>ОБЩАЯ СТОИМОСТЬ</span><strong>${displayCost(totalCost)}</strong></div>`;
  const rows = state.results.map((item, index) => `<tr><td>${escapeHtml(models[index].title)}</td><td>${displayTime(item.elapsed_ms)}</td><td>${item.prompt_tokens ?? '—'}</td><td>${item.completion_tokens ?? '—'}</td><td>${displayCost(item.cost_usd)}</td><td>${escapeHtml(item.finish_reason ?? (item.error ? 'ошибка' : '—'))}</td></tr>`);
  $('#analysis-rows').innerHTML = rows.join('');
}

function showError(message) { $('#page-error').textContent = message; $('#page-error').hidden = false; }
function clearError() { $('#page-error').hidden = true; }

async function compare() {
  const prompt = $('#prompt').value.trim();
  if (!prompt) return showError('Введите задачу или выберите один из пресетов.');
  clearError();
  const runId = ++state.runId;
  state.results = models.map(() => ({ waiting: true }));
  $('#analysis').hidden = true;
  $('#run-summary').textContent = '3 модели · запросы выполняются параллельно';
  $('#compare').disabled = true;
  renderCards(state.results);
  try {
    const response = await fetch('/compare', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt, controls: controls() }) });
    const data = await response.json();
    if (runId !== state.runId) return;
    if (!response.ok || data.error) throw new Error(data.error || 'Не удалось получить ответы.');
    state.results = data.results || models.map(() => ({ error: 'Сервер не вернул результат.' }));
    $('#run-summary').textContent = '3 модели · 3 API-вызова завершены';
    renderCards(state.results);
    renderAnalysis();
  } catch (error) {
    if (runId !== state.runId) return;
    state.results = models.map(() => ({ error: error.message || 'Не удалось связаться с сервером приложения.' }));
    renderCards(state.results);
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

$('#preset').addEventListener('click', openPresetDialog);
$('#compare').addEventListener('click', compare);
$('#sidebar-toggle').addEventListener('click', toggleSidebar);
document.querySelectorAll('[data-close-dialog]').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
renderCards(models.map(() => ({ pending: true })));
