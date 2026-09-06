const strategies = [
  { id: 'direct', title: 'Прямой ответ', description: 'Только задача, без подсказок.', accent: '#4f78cc', apiCalls: 1 },
  { id: 'step_by_step', title: 'Пошагово', description: 'Явный разбор каждого вывода.', accent: '#25a788', apiCalls: 1 },
  { id: 'auto_instruction', title: 'Автоинструкция', description: 'Сначала создаётся рабочий промпт.', accent: '#9b63d9', apiCalls: 2 },
  { id: 'experts', title: 'Группа экспертов', description: 'Аналитик, инженер и критик.', accent: '#e19a37', apiCalls: 1 },
];

const taskPresets = [
  {
    title: 'Четыре свечи', type: 'ЛОГИЧЕСКАЯ', description: 'Короткая задача с подвохом: различите погасшие и исчезнувшие свечи.',
    prompt: `В комнате горело 4 свечи. Две из них погасли. Сколько свечей осталось?`,
  },
  {
    title: 'Маршрут с ограничениями', type: 'АЛГОРИТМИЧЕСКАЯ', description: 'Ищем кратчайший допустимый путь, а не просто самый быстрый.',
    prompt: `Робот едет из A в G по двусторонним дорогам. У каждой дороги указаны время и расход энергии:

A–B: 2 мин, 2 ед.; A–C: 2 мин, 1 ед.; B–D: 3 мин, 3 ед.; B–E: 5 мин, 1 ед.; C–D: 2 мин, 4 ед.; C–E: 4 мин, 2 ед.; C–F: 2 мин, 8 ед.; D–F: 3 мин, 1 ед.; E–F: 2 мин, 3 ед.; D–G: 7 мин, 0 ед.; E–G: 6 мин, 1 ед.; F–G: 2 мин, 1 ед.

Найди маршрут с минимальным временем при трёх условиях: суммарный расход энергии не больше 8; нужно посетить ровно одну из станций D или E; дороги можно проходить не более одного раза. Укажи алгоритм, маршрут, время и расход энергии.`,
  },
  {
    title: 'Выбор кампании', type: 'АНАЛИТИЧЕСКАЯ', description: 'Считаем три сценария с разной скидкой и структурой затрат.',
    prompt: `Онлайн-сервис имеет 1 200 активных клиентов. Стандартная цена — 990 ₽ в месяц, переменные затраты — 220 ₽ на клиента, постоянные расходы — 620 000 ₽ в месяц.

Сравни три сценария на один месяц:
1. Без кампании: все 1 200 клиентов платят стандартную цену.
2. Кампания A: 240 новых клиентов платят на 15% меньше; 12% текущих клиентов тоже получают скидку 15%; у новых клиентов переменные затраты 240 ₽, у текущих не меняются; стоимость кампании — 110 000 ₽.
3. Кампания B: 400 новых клиентов и 30% текущих получают скидку 20%; у новых клиентов переменные затраты 260 ₽, у текущих не меняются; стоимость кампании — 180 000 ₽.

Рассчитай операционную прибыль каждого сценария, разницу с базовым и дай рекомендацию. Округляй суммы до целого рубля.`,
  },
];

const comparisonPresets = [
  { title: 'Все стратегии', description: 'Полная учебная сверка: пять API-вызовов.', strategies: ['direct', 'step_by_step', 'auto_instruction', 'experts'], controls: { format_instruction: '', max_tokens: '', stop_sequences: [''], thinking_mode: 'disabled' } },
  { title: 'Быстрый контраст', description: 'Только прямой и пошаговый методы, короткие ответы.', strategies: ['direct', 'step_by_step'], controls: { format_instruction: '', max_tokens: '250', stop_sequences: [''], thinking_mode: 'disabled' } },
  { title: 'Критическая проверка', description: 'Автоинструкция и эксперты с кратким отчётом.', strategies: ['auto_instruction', 'experts'], controls: { format_instruction: 'Кратко: решение, ключевой аргумент и проверка вывода.', max_tokens: '350', stop_sequences: [''], thinking_mode: 'disabled' } },
];

const state = { selected: new Set(strategies.map((strategy) => strategy.id)), stops: [''], runId: 0, analysis: null };
const $ = (selector) => document.querySelector(selector);
const controls = () => ({
  format_instruction: $('#format-instruction').value,
  max_tokens: $('#max-tokens').value,
  stop_sequences: state.stops.map((_, index) => $(`#stop-${index}`).value),
  thinking_mode: $('#thinking-mode').value,
  provider: $('#provider').value,
  model: $('#model-id').value,
  instruction_mode: $('#instruction-mode').value,
});

function selectedStrategies() { return strategies.filter((strategy) => state.selected.has(strategy.id)); }
function apiCalls() { return selectedStrategies().reduce((total, strategy) => total + strategy.apiCalls, 0); }
function summary() { return `${state.selected.size} метода · ${apiCalls()} API-вызовов`; }
function updateSummary() { $('#run-summary').textContent = summary(); $('#sidebar-summary').textContent = summary(); }

function updateProviderUi(resetModel = false) {
  const isOpenRouter = $('#provider').value === 'openrouter';
  const model = $('#model-id');
  const thinking = $('#thinking-mode');
  if (resetModel) model.value = isOpenRouter ? 'openrouter/free' : 'deepseek-v4-flash';
  model.readOnly = !isOpenRouter;
  thinking.disabled = isOpenRouter;
  $('#provider-hint').textContent = isOpenRouter
    ? 'Можно оставить Free Router или выбрать MiniMax M3 / Nemotron 3 Ultra из подсказок поля. Для своего варианта укажите точный slug с :free. Thinking в OpenRouter не передаётся.'
    : 'DeepSeek V4 Flash. Thinking можно включить в параметрах ответа; выбранная роль определяет, где лежат инструкции методов.';
}

function renderMethods() {
  $('#method-chips').replaceChildren(...strategies.map((strategy) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.classList.toggle('active', state.selected.has(strategy.id));
    button.innerHTML = `<span style="background:${strategy.accent}"></span>${strategy.title}`;
    button.addEventListener('click', () => {
      state.selected.has(strategy.id) ? state.selected.delete(strategy.id) : state.selected.add(strategy.id);
      renderMethods(); updateSummary();
    });
    return button;
  }));
}

function renderStops() {
  const list = $('#stop-list');
  list.replaceChildren(...state.stops.map((value, index) => {
    const row = document.createElement('div');
    row.className = 'stop-row';
    const input = document.createElement('input');
    input.id = `stop-${index}`;
    input.value = value;
    input.placeholder = 'Маркер завершения';
    input.addEventListener('input', () => { state.stops[index] = input.value; });
    const remove = document.createElement('button');
    remove.type = 'button'; remove.textContent = '×'; remove.setAttribute('aria-label', 'Удалить stop sequence');
    remove.addEventListener('click', () => { state.stops = state.stops.length === 1 ? [''] : state.stops.filter((_, item) => item !== index); renderStops(); });
    row.append(input, remove); return row;
  }));
}

function escapeHtml(value) { return value.replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' })[char]); }
function inlineMarkdown(value) { return value.replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/\*([^*]+)\*/g, '<em>$1</em>'); }
function renderMarkdown(value) {
  const chunks = escapeHtml(value).split(/(```[\s\S]*?```)/g);
  return chunks.map((chunk) => {
    if (chunk.startsWith('```')) return `<pre><code>${chunk.slice(3, -3).trim()}</code></pre>`;
    let html = ''; let inList = false;
    for (const line of chunk.split('\n')) {
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      const bullet = line.match(/^\s*[-*]\s+(.+)$/);
      if (heading) { if (inList) { html += '</ul>'; inList = false; } const level = heading[1].length; html += `<h${level}>${inlineMarkdown(heading[2])}</h${level}>`; }
      else if (bullet) { if (!inList) { html += '<ul>'; inList = true; } html += `<li>${inlineMarkdown(bullet[1])}</li>`; }
      else if (line.trim()) { if (inList) { html += '</ul>'; inList = false; } html += `<p>${inlineMarkdown(line)}</p>`; }
    }
    return `${html}${inList ? '</ul>' : ''}`;
  }).join('');
}

function makeCard(strategy) {
  const card = document.createElement('article');
  card.className = 'result-card'; card.style.setProperty('--accent', strategy.accent);
  card.innerHTML = `<div class="card-topline"><span class="status-dot"></span><p>${strategy.title}</p><button class="info" type="button" aria-label="Показать JSON-запрос">i</button></div><p class="card-description">${strategy.description}</p><div class="card-body"><div class="loading-copy"><span></span><span></span><span></span> Машина вычисляет ответ…</div></div>`;
  return card;
}

function showInfo(strategy, requestChain) {
  $('#info-title').textContent = `${strategy.title} · JSON`;
  $('#info-json').textContent = JSON.stringify(requestChain || [], null, 2);
  $('#info-dialog').showModal();
}

function showError(message) { const node = $('#page-error'); node.textContent = message; node.hidden = false; }
function clearError() { $('#page-error').hidden = true; }

function settingChip(label, value) {
  const chip = document.createElement('span');
  const labelNode = document.createElement('b'); labelNode.textContent = `${label}: `;
  chip.append(labelNode, document.createTextNode(value));
  return chip;
}

function evaluationSelect(value, options, onChange) {
  const select = document.createElement('select');
  select.className = 'evaluation-select';
  options.forEach((option) => {
    const item = document.createElement('option');
    item.value = option; item.textContent = option; item.selected = option === value;
    select.append(item);
  });
  select.addEventListener('change', () => { onChange(select.value); renderAnalysis(); });
  return select;
}

function verdictItem(title, value, tone = '') {
  const item = document.createElement('p');
  if (tone) item.className = tone;
  const heading = document.createElement('b'); heading.textContent = `${title}: `;
  item.append(heading, document.createTextNode(value));
  return item;
}

function renderAnalysis() {
  const run = state.analysis;
  if (!run) return;
  const completed = run.records.filter((record) => record.status !== 'Выполняется').length;
  $('#analysis-status').textContent = completed === run.records.length
    ? `Готово: ${completed} из ${run.records.length} ответов`
    : `Получено ${completed} из ${run.records.length} ответов`;

  const stopMarkers = run.controls.stop_sequences.filter(Boolean);
  const thinking = { disabled: 'выключен', enabled: 'включён', '': 'по умолчанию' }[run.controls.thinking_mode] ?? 'по умолчанию';
  const provider = run.controls.provider === 'openrouter' ? 'OpenRouter' : 'DeepSeek';
  const appliedThinking = run.controls.provider === 'openrouter' ? 'не передаётся в OpenRouter' : thinking;
  const instructionMode = run.controls.instruction_mode === 'system' ? 'system prompt' : 'user prompt';
  $('#analysis-control-chips').replaceChildren(
    settingChip('Провайдер', provider),
    settingChip('Модель', run.controls.model),
    settingChip('Инструкции', instructionMode),
    settingChip('Формат', run.controls.format_instruction || 'не задан'),
    settingChip('max_tokens', run.controls.max_tokens || 'без лимита'),
    settingChip('stop', stopMarkers.length ? stopMarkers.join(' · ') : 'не задан'),
    settingChip('thinking', appliedThinking),
  );

  $('#analysis-rows').replaceChildren(...run.records.map((record) => {
    const row = document.createElement('tr');
    const values = [
      record.strategy.title,
      record.status,
    ];
    values.forEach((value, index) => {
      const cell = document.createElement('td');
      cell.textContent = value;
      if (index === 1) { cell.className = 'analysis-status-cell'; cell.dataset.status = record.status; }
      row.append(cell);
    });
    const correctness = document.createElement('td');
    correctness.append(evaluationSelect(record.correctness || 'Не оценено', ['Не оценено', 'Верно', 'Частично', 'Ошибка'], (value) => { record.correctness = value; }));
    const explanation = document.createElement('td');
    explanation.append(evaluationSelect(record.explanation || 'Не оценено', ['Не оценено', 'Ясное', 'Есть пробелы', 'Слабое'], (value) => { record.explanation = value; }));
    row.append(correctness, explanation);
    [
      record.elapsed_ms == null ? '—' : `${record.elapsed_ms} мс`,
      record.api_calls ?? record.strategy.apiCalls,
      record.completion_tokens ?? '—',
      record.finish_reason ?? '—',
      record.reasoning_tokens ?? '—',
    ].forEach((value) => { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); });
    return row;
  }));

  const ready = run.records.filter((record) => record.status === 'Готово');
  const correct = run.records.filter((record) => record.correctness === 'Верно').map((record) => record.strategy.title);
  const clear = run.records.filter((record) => record.explanation === 'Ясное').map((record) => record.strategy.title);
  const fastest = ready.length ? ready.reduce((best, record) => record.elapsed_ms < best.elapsed_ms ? record : best) : null;
  const leanest = ready.filter((record) => record.completion_tokens != null).reduce((best, record) => !best || record.completion_tokens < best.completion_tokens ? record : best, null);
  $('#analysis-verdict').replaceChildren(
    verdictItem('Правильность', correct.length ? `верные ответы: ${correct.join(', ')}` : 'отметьте ответы после сверки с эталоном.'),
    verdictItem('Объяснение', clear.length ? `самые ясные: ${clear.join(', ')}` : 'оцените, насколько вывод следует из условий.'),
    verdictItem('Скорость', fastest ? `быстрее всех: ${fastest.strategy.title} (${fastest.elapsed_ms} мс)` : 'ожидаем ответы.', 'automatic'),
    verdictItem('Экономичность', leanest ? `меньше выходных токенов: ${leanest.strategy.title} (${leanest.completion_tokens})` : 'ожидаем данные о токенах.', 'automatic'),
  );
}

function startAnalysis(selected, payloadControls) {
  state.analysis = {
    controls: { ...payloadControls, stop_sequences: [...payloadControls.stop_sequences] },
    records: selected.map((strategy) => ({ strategy, status: 'Выполняется', correctness: 'Не оценено', explanation: 'Не оценено' })),
  };
  $('#analysis').hidden = false;
  renderAnalysis();
  return state.analysis;
}

async function submit() {
  const prompt = $('#prompt').value.trim();
  if (!prompt) return showError('Введите задачу или выберите один из пресетов.');
  if (!state.selected.size) return showError('Включите хотя бы один метод на панели управления.');
  clearError();
  const runId = ++state.runId;
  const selected = selectedStrategies(); const payloadControls = controls();
  const results = $('#results'); results.replaceChildren(...selected.map(makeCard));
  const analysisRun = startAnalysis(selected, payloadControls);
  $('#launch').disabled = true;
  await Promise.all(selected.map(async (strategy, index) => {
    const card = results.children[index]; const body = card.querySelector('.card-body'); let requestChain = [];
    const record = analysisRun.records[index];
    try {
      const response = await fetch('/api/solve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt, strategy: strategy.id, controls: payloadControls }) });
      const data = await response.json(); if (runId !== state.runId) return;
      requestChain = data.request_chain || [];
      if (!response.ok || data.error) throw new Error(data.error || 'Не удалось получить ответ.');
      const generatedPrompt = data.generated_prompt
        ? `<section class="generated-prompt"><p class="step-label">ШАГ 1 · СОЗДАННЫЙ ПРОМПТ</p><div class="generated-prompt-text">${renderMarkdown(data.generated_prompt)}</div></section><p class="step-label final-step">ШАГ 2 · ОТВЕТ ПО ЭТОМУ ПРОМПТУ</p>`
        : '';
      const providerLabel = data.provider === 'openrouter' ? 'OpenRouter' : 'DeepSeek';
      const instructionLabel = data.instruction_mode === 'system' ? 'system' : 'user';
      const thinkingLabel = data.thinking_applied == null ? 'не передаётся' : ({ disabled: 'выключен', enabled: 'включён', '': 'по умолчанию' }[data.thinking_applied] || 'по умолчанию');
      const requestedModel = data.model || '';
      const resolvedModel = data.resolved_model || requestedModel;
      const modelLabel = resolvedModel === requestedModel ? resolvedModel : `${requestedModel} → ${resolvedModel}`;
      body.innerHTML = `${generatedPrompt}<div class="answer">${renderMarkdown(data.answer || 'Модель не вернула финальный ответ.')}</div><p class="metadata">${providerLabel} · ${escapeHtml(modelLabel)} · Инструкции: ${instructionLabel} · Thinking: ${thinkingLabel}<br>API-вызовы: ${data.api_calls ?? strategy.apiCalls} · Время: ${data.elapsed_ms ?? '—'} мс · Токены: ${data.completion_tokens ?? '—'} · Завершение: ${data.finish_reason ?? '—'} · Reasoning: ${data.reasoning_tokens ?? '—'}</p>`;
      Object.assign(record, { status: 'Готово', elapsed_ms: data.elapsed_ms, api_calls: data.api_calls, completion_tokens: data.completion_tokens, finish_reason: data.finish_reason, reasoning_tokens: data.reasoning_tokens });
    } catch (error) {
      if (runId !== state.runId) return;
      body.innerHTML = `<p class="card-error">${escapeHtml(error.message || 'Не удалось связаться с сервером приложения.')}</p>`;
      Object.assign(record, { status: 'Ошибка' });
    }
    renderAnalysis();
    card.querySelector('.info').addEventListener('click', () => showInfo(strategy, requestChain));
  }));
  if (runId === state.runId) $('#launch').disabled = false;
}

function openPresets(kind) {
  const isTasks = kind === 'tasks'; const items = isTasks ? taskPresets : comparisonPresets;
  $('#modal-eyebrow').textContent = isTasks ? 'БИБЛИОТЕКА' : 'КОНФИГУРАЦИИ';
  $('#modal-title').textContent = isTasks ? 'Пресеты задач' : 'Пресеты сравнения';
  $('#preset-list').replaceChildren(...items.map((item) => {
    const button = document.createElement('button'); button.type = 'button';
    button.innerHTML = `${isTasks ? `<span>${item.type}</span>` : ''}<strong>${item.title}</strong><small>${item.description}</small>`;
    button.addEventListener('click', () => {
      if (isTasks) $('#prompt').value = item.prompt;
      else {
        state.selected = new Set(item.strategies); state.stops = [...item.controls.stop_sequences];
        $('#format-instruction').value = item.controls.format_instruction; $('#max-tokens').value = item.controls.max_tokens; $('#thinking-mode').value = item.controls.thinking_mode;
        renderMethods(); renderStops(); updateSummary();
      }
      $('#preset-dialog').close();
    }); return button;
  }));
  $('#preset-dialog').showModal();
}

function toggleSidebar() {
  const collapsed = $('#control-sidebar').classList.toggle('collapsed');
  $('#machine-layout').classList.toggle('sidebar-is-collapsed', collapsed);
  $('#sidebar-content').hidden = collapsed;
  const button = $('#sidebar-toggle'); button.setAttribute('aria-expanded', String(!collapsed)); button.innerHTML = collapsed ? 'Развернуть <span>‹</span>' : 'Свернуть <span>›</span>';
}

$('#task-presets').addEventListener('click', () => openPresets('tasks'));
$('#comparison-presets').addEventListener('click', () => openPresets('comparison'));
$('#launch').addEventListener('click', submit);
$('#sidebar-toggle').addEventListener('click', toggleSidebar);
$('#add-stop').addEventListener('click', () => { if (state.stops.length < 16) { state.stops.push(''); renderStops(); } });
$('#provider').addEventListener('change', () => updateProviderUi(true));
document.querySelectorAll('[data-close-dialog]').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
renderMethods(); renderStops(); updateProviderUi(); updateSummary();
