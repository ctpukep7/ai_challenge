<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { DEFAULT_SETTINGS, createSettingsEditor, refreshSettingsEditor, resetSettingsEditor, cloneSettings, parameterRows, stopToText, stopFromText, clearResults, createState, failQuestion, questionError, requestEndpoint, requestPayload, settingsError, parseSchema, shouldSend, validService } from './optimization-state.js';
import AnswerCard from './AnswerCard.vue';
import ExperimentSummary from './ExperimentSummary.vue';
import { PRESETS, selectPreset, beginPresetQuestion, finishPresetQuestion } from './presets.js';
const selectedPreset = ref(null);
const chat = ref(createState()), settingsEditor = ref(createSettingsEditor()), service = ref(null), checking = ref(false), composer = ref(null), transcript = ref(null), elapsed = ref(0);
let requestController, stateController, timer;
const fallbackSettings = cloneSettings(DEFAULT_SETTINGS);
const settings = computed(() => settingsEditor.value.draft ?? fallbackSettings);
const stopInputText = ref(null);
// Preserve a partially typed escape; otherwise typing "\" would immediately render "\\".
const stopText = computed({ get: () => stopInputText.value ?? stopToText(settings.value.stop), set: (text) => { stopInputText.value = text; settings.value.stop = stopFromText(text); } });
const baselineRows = computed(() => parameterRows(service.value?.profiles?.baseline?.parameters));
const advancedNumbers = [
  { key: 'top_k', label: 'Top K', min: 0, max: 100, step: 1, hint: 'Число вероятных токенов для выбора: 0–100. 0 отключает этот фильтр.' },
  { key: 'top_p', label: 'Top P', min: 0, max: 1, step: 'any', hint: 'Суммарная вероятность вариантов для выбора, от 0 до 1. Меньше — уже выбор.' },
  { key: 'min_p', label: 'Min P', min: 0, max: 1, step: 'any', hint: 'Отсекает маловероятные варианты относительно лучшего: 0–1. 0 отключает фильтр.' },
  { key: 'repeat_penalty', label: 'Штраф за повторы', min: .8, max: 1.5, step: 'any', hint: 'От 0,8 до 1,5. 1 — без штрафа; больше 1 уменьшает повторы.' },
  { key: 'presence_penalty', label: 'Штраф за появление (presence penalty)', min: 0, max: 2, step: 'any', hint: 'От 0 до 2. Снижает вероятность уже встречавшихся токенов независимо от числа повторов. Высокое значение может ухудшать русский язык.' },
  { key: 'repeat_last_n', label: 'Окно проверки повторов', min: 0, max: 2048, step: 1, hint: 'Целое число 0…2048: 0 отключает проверку. Значение −1 не поддерживается установленным runner.' },
  { key: 'seed', label: 'Seed', min: -1, max: 2147483647, step: 1, hint: '−1 — случайный seed. Фиксированное значение помогает сравнивать повторы, но не гарантирует точного совпадения.' },
];
const ready = computed(() => service.value?.status === 'ready');
const inputError = computed(() => chat.value.draft.trim() ? questionError(chat.value.draft) : '');
const formatMode = ref('text'), schemaText = ref('');
const schemaParsed = computed(() => parseSchema(schemaText.value));
const requestSettings = computed(() => ({ ...settings.value, format: formatMode.value === 'text' ? null : formatMode.value === 'json' ? 'json' : schemaParsed.value.value }));
const parameterError = computed(() => formatMode.value === 'schema' && schemaParsed.value.error || settingsError(requestSettings.value));
const canSend = computed(() => ready.value && !chat.value.pending && chat.value.draft.trim() && !inputError.value && !parameterError.value);

async function json(response) { try { return await response.json(); } catch { throw new Error('Локальный сервис вернул некорректный JSON. Вопрос сохранён.'); } }
async function refreshState() {
  if (checking.value || chat.value.pending) return;
  checking.value = true;
  const controller = new AbortController(); stateController = controller;
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch('/api/state', { signal: controller.signal }), data = await json(response);
    if (!response.ok || !validService(data)) throw new Error();
    service.value = data;
    settingsEditor.value = refreshSettingsEditor(settingsEditor.value, data);
  } catch { service.value = { status: 'unavailable', message: 'Не удалось связаться с локальным сервисом. Проверьте сервер приложения и обновите состояние.' }; }
  finally { clearTimeout(timeout); stateController = null; checking.value = false; }
}
function resetSettings() { if (!chat.value.pending) { selectedPreset.value = null; stopInputText.value = null; formatMode.value = 'text'; schemaText.value = ''; } settingsEditor.value = resetSettingsEditor(settingsEditor.value, service.value, Boolean(chat.value.pending)); }

async function focusComposer() {
  await nextTick(); const input = composer.value; if (!input) return;
  input.focus(); input.setSelectionRange(input.value.length, input.value.length);
}
function applyPreset(id) {
  const chosen = selectPreset(chat.value, settingsEditor.value, id); if (!chosen || !service.value?.profiles?.optimized) return;
  chat.value = chosen.chat; settingsEditor.value = chosen.editor; selectedPreset.value = chosen.id;
  formatMode.value = chosen.formatMode; schemaText.value = chosen.schemaText; stopInputText.value = null; focusComposer();
}
function newChat() { chat.value = clearResults(chat.value); if (!chat.value.pending) { selectedPreset.value = null; focusComposer(); } }
function onKeydown(event) {
  if (chat.value.pending) return;
  if (shouldSend(event)) { event.preventDefault(); send('compare'); }
}
async function send(mode = 'compare') {
  if (!canSend.value) return;
  chat.value = beginPresetQuestion(chat.value, requestSettings.value, mode, selectedPreset.value);
  const controller = new AbortController(); requestController = controller;
  const started = performance.now(); elapsed.value = 0;
  timer = setInterval(() => { elapsed.value = Math.floor((performance.now() - started) / 1000); }, 1000);
  const timeout = setTimeout(() => controller.abort(), mode === 'compare' ? 245000 : 125000);
  try {
    const response = await fetch(requestEndpoint(chat.value), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(requestPayload(chat.value)), signal: controller.signal });
    const data = await json(response);
    if (!response.ok) throw new Error(typeof data?.error?.message === 'string' ? data.error.message : 'Не удалось получить ответ. Вопрос и настройки сохранены для ручного повтора.');
    chat.value = finishPresetQuestion(chat.value, data);
  } catch (error) {
    chat.value = failQuestion(chat.value, error.name === 'AbortError' ? 'Время ожидания истекло. Сервер ещё может завершать запрос. Вопрос сохранён; автоматического повтора нет.' : error instanceof TypeError ? 'Связь с локальным сервисом прервалась. Вопрос сохранён для ручной отправки.' : error.message);
  } finally { clearInterval(timer); clearTimeout(timeout); requestController = null; focusComposer(); }
}
watch(() => [chat.value.results.length, chat.value.pending, chat.value.error], async () => { await nextTick(); if (transcript.value) transcript.value.scrollTop = transcript.value.scrollHeight; });
onMounted(refreshState);
onBeforeUnmount(() => { clearInterval(timer); requestController?.abort(); stateController?.abort(); });
</script>
<template>
  <div class="app-shell"><header class="page-header"><div class="brand"><div class="brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M5 4h10l4 4v12H5V4Zm10 0v5h4M8 12h8m-8 4h5" /></svg></div><div><div class="eyebrow">AI CHALLENGE · ДЕНЬ 29</div><h1>Настройка локальной LLM</h1></div></div><div class="local-badge"><span class="status-dot" :class="{ ready }"></span>Одна локальная модель</div></header>
    <div class="workspace">
      <aside class="model-panel" aria-label="Параметры модели"><div class="section-label">ПАРАМЕТРЫ МОДЕЛИ</div><h2>Qwen3 <span>4B</span></h2><p class="model-description muted">Одна модель, разные настройки.<br />Введите любой запрос.</p><div class="model-tag">{{ service?.model }}</div><div class="service-status"><span class="status-dot" :class="{ ready }"></span>{{ ready ? 'Локальный сервис готов' : checking || !service ? 'Проверяем готовность' : 'Сервис недоступен' }}</div><button class="text-button" :disabled="checking || Boolean(chat.pending)" @click="refreshState">{{ checking ? 'Проверяем…' : 'Обновить состояние' }}</button><dl class="index-stats"><div><dt>Квантование</dt><dd>{{ service?.quantization || 'Не предоставлено' }}</dd></div></dl>
        <p class="tiny quantization-note">Используется установленное Q4_K_M. Другая квантованная версия не установлена; сравниваем настройки одной копии весов.</p>
        <div class="sidebar-divider"></div><h3 class="settings-heading">Два примера для сравнения</h3><p class="tiny">Пресет заполняет настройки и запрос. Затем нажмите «Сравнить модели».</p><div class="preset-list"><button v-for="preset in PRESETS" :key="preset.id" type="button" class="preset-card" :class="{ selected: selectedPreset === preset.id }" :aria-pressed="selectedPreset === preset.id" :disabled="Boolean(chat.pending) || !service?.profiles?.optimized" @click="applyPreset(preset.id)"><strong>{{ preset.title }}</strong><span>{{ preset.purpose }}</span><small>{{ preset.changes }}</small></button></div>
        <div class="sidebar-divider"></div><h3 class="settings-heading">Исходная модель</h3><p class="tiny">temperature 0,7 · ответ до 1024 токенов · контекст 4096.<br />Неизменяемые исходные настройки.</p><details class="baseline-prompt"><summary>Все исходные параметры</summary><dl class="baseline-settings"><div v-for="row in baselineRows" :key="row.key"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></div></dl><pre>{{ service?.profiles?.baseline ? 'Системная инструкция отсутствует: модели передан только ваш запрос.' : 'Ожидаем состояние сервиса' }}</pre></details>
        <div class="sidebar-divider"></div><h3 class="settings-heading">Настроенная модель</h3><p class="tiny">Измените параметры перед отправкой запроса.</p><fieldset class="tuning-fields" :disabled="Boolean(chat.pending) || !service?.profiles?.optimized"><label for="temperature">Temperature <input id="temperature" aria-label="Temperature" aria-describedby="temperature-hint" v-model.number="settings.temperature" type="number" min="0" max="2" step="any" /><span id="temperature-hint" class="setting-hint">От 0 до 2: больше — разнообразнее выбор, но правильность не гарантируется.</span></label><label for="num-predict">Лимит ответа (токены)<select id="num-predict" v-model.number="settings.num_predict"><option v-for="value in [256, 512, 768, 1024, 1536, 2048]" :key="value" :value="value">{{ value }}</option></select></label><label for="num-ctx">Окно контекста (токены)<select id="num-ctx" aria-label="Окно контекста (токены)" :aria-describedby="settings.num_ctx >= 6144 ? 'context-memory-hint' : undefined" v-model.number="settings.num_ctx"><option v-for="value in [3072, 4096, 6144, 8192]" :key="value" :value="value">{{ value }}</option></select><span id="context-memory-hint" v-if="settings.num_ctx >= 6144" class="setting-hint memory-note">Большое окно увеличивает расход памяти модели.</span></label>
          <label for="response-format">Формат ответа<select id="response-format" aria-label="Формат ответа" v-model="formatMode"><option value="text">Обычный текст</option><option value="json">JSON</option><option value="schema">JSON Schema</option></select></label>
          <label v-if="formatMode === 'schema'" class="system-setting" for="format-schema">JSON Schema<textarea id="format-schema" aria-label="JSON Schema" v-model="schemaText" class="settings-textarea" rows="6" placeholder="Введите JSON Schema" spellcheck="false"></textarea><span class="setting-hint">Непустой JSON-объект: до 5000 символов / 12000 байт, 8 уровней и 256 элементов. Ссылки $ref не поддерживаются.</span></label>
          <label class="system-setting" for="system-prompt">Системная инструкция<textarea id="system-prompt" aria-label="Системная инструкция" aria-describedby="system-prompt-hint" v-model="settings.system" class="settings-textarea system-textarea" rows="5" :disabled="Boolean(chat.pending)" placeholder="Можно оставить пустой"></textarea><span id="system-prompt-hint" class="setting-hint">До 3000 символов и 8000 байт UTF-8. Общие правила ответа; входные данные вставляйте в запрос. Можно изменить или оставить пустым.</span></label>
          <details class="advanced-settings"><summary>Расширенные параметры</summary><div class="advanced-fields"><label v-for="option in advancedNumbers" :key="option.key" :for="option.key">{{ option.label }}<input :id="option.key" :aria-label="option.label" :aria-describedby="`${option.key}-hint`" v-model.number="settings[option.key]" type="number" :min="option.min" :max="option.max" :step="option.step" /><span :id="`${option.key}-hint`" class="setting-hint">{{ option.hint }}</span></label><label for="stop-phrases">Стоп-фразы<textarea id="stop-phrases" aria-label="Стоп-фразы" aria-describedby="stop-phrases-hint" v-model="stopText" class="settings-textarea" rows="3" placeholder="Одна фраза на строку" :disabled="Boolean(chat.pending)"></textarea><span id="stop-phrases-hint" class="setting-hint">Останавливают ответ при появлении фразы. До 4 строк: каждая до 80 символов и 256 байт. \n — перенос строки; \r — возврат каретки; \\ — обратный слеш. Пробелы сохраняются; пустые строки и повторы запрещены. Пустое поле — без дополнительных стоп-фраз.</span></label></div></details>
          <button class="text-button" type="button" @click="resetSettings">Сбросить настройки</button></fieldset><p v-if="parameterError" class="parameter-error" role="alert">{{ parameterError }}</p>
        <div class="privacy-note"><strong>Всё выполняется локально</strong><p>Без облачных запросов. Используем одну копию весов. Каждый запрос независимый; ответы хранятся только в этой вкладке.</p></div>
      </aside>
      <main class="chat-panel"><header class="chat-header"><div><h2>До и после настройки</h2><p>Одинаковый запрос для исходной и настроенной модели. Ответы последовательные; каждый запрос независимый.</p></div><button class="secondary" :disabled="Boolean(chat.pending)" @click="newChat">+ Новый эксперимент</button></header>
        <div v-if="checking && !service" class="checking-state">Проверяем локальную модель…</div><div v-if="service && !ready" class="connection-warning" role="alert"><span>{{ service.message }}</span><button class="text-button" :disabled="checking || Boolean(chat.pending)" @click="refreshState">Обновить</button></div>
        <div ref="transcript" class="transcript" role="log" aria-label="Результаты сравнения">
          <div v-if="!chat.results.length && !chat.pending && !chat.error" class="empty-state"><div class="empty-symbol" aria-hidden="true"><svg viewBox="0 0 32 32"><path d="M7 5h13l5 5v17H7V5Zm13 0v6h5M11 15h10m-10 5h7" /></svg></div><h3>Что вы хотите узнать?</h3><p>Выберите пример слева или введите свою задачу.<br />Обе модели получат один и тот же запрос.</p><span>Сравните ответы, формат и скорость.</span></div>
          <section v-for="(result, index) in chat.results" :key="index" class="result-entry"><div class="user-question"><span class="message-label">Вы</span><div>{{ result.question }}</div></div><div class="comparison-caption">{{ result.mode === 'compare' ? 'Свежие ответы · одинаковый запрос · порядок: ' + result.order.map(profile => profile === 'baseline' ? 'исходный' : 'настроенный').join(' → ') : 'Только настроенная модель' }}<details><summary>Хеш запроса</summary><code>{{ result.comparison.input_sha256 }}</code></details></div><ExperimentSummary :result="result" /><div class="answer-grid" :class="{ compared: result.mode === 'compare' }"><AnswerCard v-if="result.mode === 'compare'" :provider="result.baseline" /><AnswerCard :provider="result.optimized" /></div></section>
          <template v-if="chat.pending"><div class="user-question"><span class="message-label">Вы</span><div>{{ chat.pending.question }}</div></div><div class="pending-card" role="status"><span class="typing-dots" aria-hidden="true"><i></i><i></i><i></i></span><span>{{ chat.pending.mode === 'compare' ? 'Прогреваем и последовательно сравниваем ответы' : 'Прогреваем и готовим ответ' }} · {{ elapsed }} с</span><p>Прогрев каждой модели измеряется отдельно. Параметры этого запроса зафиксированы.</p><p v-if="chat.pending.preset">Пример: {{ chat.pending.preset.title }}. Критерии также сохранены для этого запроса.</p></div></template><div v-if="chat.error" class="error-banner" role="alert">{{ chat.error }}</div>
        </div>
        <form class="composer-area" @submit.prevent="send('compare')"><label class="sr-only" for="question">Ваш запрос</label><div class="composer-box" :class="{ invalid: Boolean(inputError) }"><textarea id="question" ref="composer" v-model="chat.draft" rows="2" placeholder="Введите свой запрос…" :disabled="Boolean(chat.pending)" :aria-invalid="Boolean(inputError)" aria-describedby="composer-hint" @keydown="onKeydown"></textarea><button class="send-button" type="submit" :disabled="!canSend" aria-label="Сравнить модели"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6" /></svg></button></div><div class="compare-actions"><button class="compare-button" type="submit" :disabled="!canSend">Сравнить модели</button><button class="compare-button" type="button" :disabled="!canSend" @click="send('optimized')">Только настроенная модель</button></div><div id="composer-hint" class="composer-hint" :class="{ invalid: Boolean(inputError) }">{{ inputError || 'Enter — сравнить · Shift + Enter — новая строка · каждый вопрос независимый' }}</div></form>
      </main>
    </div>
  </div>
</template>
