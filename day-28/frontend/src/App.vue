<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { beginQuestion, clearResults, createState, failQuestion, finishQuestion, insertQuestion, questionError, requestEndpoint, requestPayload, shouldSend } from './rag-state.js';
import AnswerCard from './AnswerCard.vue';

const chat = ref(createState());
const service = ref(null);
const checking = ref(false);
const chosenQuestion = ref('');
const composer = ref(null);
const transcript = ref(null);
const elapsed = ref(0);
let requestController;
let stateController;
let timer;
const ready = computed(() => service.value?.status === 'ready' && service.value?.index?.ready);
const inputError = computed(() => chat.value.draft.trim() ? questionError(chat.value.draft) : '');
const canSend = computed(() => ready.value && !chat.value.pending && chat.value.draft.trim() && !inputError.value);
const canCompare = computed(() => canSend.value && service.value?.comparison?.available === true);
const number = (value, digits = 2) => typeof value === 'number' ? value.toLocaleString('ru-RU', { maximumFractionDigits: digits }) : '—';
const chunkCount = computed(() => {
  const chunks = service.value?.index?.chunks;
  if (typeof chunks === 'number') return chunks;
  return chunks?.structure ?? '—';
});

async function json(response) {
  try { return await response.json(); }
  catch { throw new Error('Локальный сервис вернул некорректный ответ.'); }
}

async function refreshState() {
  if (checking.value) return;
  checking.value = true;
  const controller = new AbortController();
  stateController = controller;
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch('/api/state', { signal: controller.signal });
    const data = await json(response);
    if (!response.ok || !['ready', 'unavailable'].includes(data?.status) || !Array.isArray(data.questions)) throw new Error();
    service.value = data;
  } catch {
    service.value = { status: 'unavailable', message: 'Не удалось связаться с локальным RAG. Проверьте сервер приложения и обновите состояние.', questions: [], index: { ready: false }, cloud: { available: false } };
  } finally {
    clearTimeout(timeout);
    stateController = null;
    checking.value = false;
  }
}

async function focusComposer(expand = false) {
  await nextTick();
  const input = composer.value;
  if (!input) return;
  if (expand) {
    input.style.height = 'auto';
    input.style.height = `${Math.min(150, input.scrollHeight)}px`;
  }
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function chooseQuestion() {
  if (chat.value.pending) return;
  const question = service.value?.questions.find((item) => item.id === chosenQuestion.value);
  if (!question) return;
  chat.value = insertQuestion(chat.value, question.question);
  chosenQuestion.value = '';
  focusComposer(true);
}

function newChat() {
  if (chat.value.pending) return;
  chat.value = clearResults(chat.value);
  chosenQuestion.value = '';
  focusComposer();
}

function onKeydown(event) {
  if (shouldSend(event)) { event.preventDefault(); send('local'); }
}

async function send(mode = 'local') {
  if (!canSend.value || mode === 'compare' && !canCompare.value) return;
  chat.value = beginQuestion(chat.value, mode);
  const controller = new AbortController();
  requestController = controller;
  const started = performance.now();
  elapsed.value = 0;
  timer = setInterval(() => { elapsed.value = Math.floor((performance.now() - started) / 1000); }, 1000);
  const timeout = setTimeout(() => controller.abort(), 125000);
  try {
    const response = await fetch(requestEndpoint(chat.value), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestPayload(chat.value)), signal: controller.signal,
    });
    const data = await json(response);
    if (!response.ok) {
      throw new Error(typeof data?.error?.message === 'string' ? data.error.message : 'Не удалось получить ответ. Повторите запрос самостоятельно.');
    }
    chat.value = finishQuestion(chat.value, data);
  } catch (error) {
    chat.value = failQuestion(chat.value, error.name === 'AbortError'
      ? 'Время ожидания истекло. Сервер ещё может завершать поиск или генерацию. Вопрос сохранён; автоматического повтора нет.'
      : error instanceof TypeError ? 'Связь с локальным сервисом прервалась. Проверьте сервер и отправьте вопрос ещё раз.' : error.message);
  } finally {
    clearInterval(timer);
    clearTimeout(timeout);
    requestController = null;
    focusComposer();
  }
}

watch(() => [chat.value.results.length, chat.value.pending, chat.value.error], async () => {
  await nextTick();
  if (transcript.value) transcript.value.scrollTop = transcript.value.scrollHeight;
});
onMounted(refreshState);
onBeforeUnmount(() => { clearInterval(timer); requestController?.abort(); stateController?.abort(); });
</script>

<template>
  <div class="app-shell">
    <header class="page-header">
      <div class="brand"><div class="brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M5 4h10l4 4v12H5V4Zm10 0v5h4M8 12h8m-8 4h5" /></svg></div><div><div class="eyebrow">AI CHALLENGE · ДЕНЬ 28</div><h1>Ответы по README</h1></div></div>
      <div class="local-badge"><span class="status-dot" :class="{ ready }" aria-hidden="true"></span>Локальный RAG</div>
    </header>
    <div class="workspace">
      <aside class="model-panel" aria-label="Состояние локального RAG">
        <div class="section-label">МОДЕЛЬ И ДОКУМЕНТЫ</div><h2>Qwen3 <span>4B</span></h2>
        <p class="muted model-description">Ищем нужные фрагменты локально, затем модель отвечает по ним.</p>
        <div class="service-status"><span class="status-dot" :class="{ ready }" aria-hidden="true"></span>{{ checking ? 'Проверяем сервис…' : ready ? 'Готов к вопросам' : 'Сервис недоступен' }}</div>
        <button class="text-button" :disabled="checking || Boolean(chat.pending)" @click="refreshState">Обновить состояние</button>
        <div class="sidebar-divider"></div>
        <div class="section-label">СНИМОК ИНДЕКСА</div>
        <dl class="index-stats"><div><dt>README</dt><dd>{{ service?.index?.files ?? '—' }}</dd></div><div><dt>Фрагментов</dt><dd>{{ chunkCount }}</dd></div><div><dt>Размерность</dt><dd>{{ service?.index?.dimension ?? '—' }}</dd></div></dl>
        <p class="tiny">Используем существующий индекс недели RAG без пересборки.</p>
        <div class="sidebar-divider"></div>
        <div class="section-label">СРАВНЕНИЕ</div><p class="tiny">Обычная отправка — только локально. Каждое сравнение запрашивает свежие ответы Qwen3 и DeepSeek с одинаковым RAG-контекстом.</p><p class="tiny comparison-config">{{ service?.comparison?.available ? 'Доступ настроен. Подключение к облаку проверяется при запросе.' : service?.comparison?.message || 'Облачное сравнение не настроено. Локальные ответы доступны без него.' }}</p>
        <div class="privacy-note"><strong>Только в этой вкладке</strong><p>Результаты хранятся в памяти и исчезнут при обновлении страницы.</p></div>
      </aside>
      <main class="chat-panel">
        <div class="chat-header"><div><h2>Вопросы к документации</h2><p>Каждый вопрос ищется отдельно — история не передаётся модели.</p></div><button class="secondary" :disabled="Boolean(chat.pending)" @click="newChat">+ Новый чат</button></div>
        <div class="mobile-index"><span class="status-dot" :class="{ ready }" aria-hidden="true"></span>Qwen3 4B · {{ service?.index?.files ?? '—' }} README · {{ chunkCount }} фрагментов</div>
        <div v-if="service?.index?.snapshot_differs" class="snapshot-warning" role="status">Индекс содержит сохранённый снимок README. Текущие файлы изменились: ответы могут отличаться от актуальной документации.</div>
        <div v-if="!ready && !checking" class="connection-warning" role="status"><span>{{ service?.message || 'Локальный сервис пока недоступен.' }}</span><button class="text-button" :disabled="Boolean(chat.pending)" @click="refreshState">Проверить</button></div>
        <div v-if="checking" class="checking-state" role="status">Проверяем модель и индекс…</div>
        <div ref="transcript" class="transcript" role="log" aria-label="Вопросы и ответы" aria-live="polite" :aria-busy="Boolean(chat.pending)">
          <div v-if="!chat.results.length && !chat.pending" class="empty-state"><div class="empty-symbol" aria-hidden="true"><svg viewBox="0 0 32 32"><path d="M7 5h13l5 5v17H7V5Zm13 0v6h5M11 15h10m-10 5h7" /></svg></div><h3>Что хотите найти в проекте?</h3><p>Задайте свой вопрос к README.<br />Ответ придёт с фрагментами, которые получила модель.</p><span>Локальный поиск → локальная генерация</span></div>
          <section v-for="(result, index) in chat.results" :key="index" class="result-entry">
            <div class="user-question"><span class="message-label">Вы</span><div>{{ result.question }}</div></div>
            <div v-if="result.mode === 'compare'" class="comparison-caption">Свежие ответы · одинаковые источники и инструкции · {{ result.comparison.chunks }} фрагментов</div>
            <div class="answer-grid" :class="{ compared: result.mode === 'compare' }">
              <AnswerCard :provider="result" />
              <AnswerCard v-if="result.mode === 'compare'" :provider="result.cloud" :shared-metrics="result.metrics" cloud />
            </div>
          </section>
          <template v-if="chat.pending"><div class="user-question"><span class="message-label">Вы</span><div>{{ chat.pending.question }}</div></div><div class="pending-card"><span class="typing-dots" aria-hidden="true"><i></i><i></i><i></i></span><span>{{ chat.pending.mode === 'compare' ? 'Ищем источники и сравниваем две модели' : 'Ищем источники и готовим ответ' }}<span v-if="elapsed"> · {{ elapsed }} с</span></span><p>Первый вопрос может занять больше времени: загружаются embeddings и модель.</p></div></template>
          <div v-if="chat.error" class="error-banner" role="alert">{{ chat.error }}</div>
        </div>
        <form class="composer-area" @submit.prevent="send('local')">
          <label class="prepared-label" for="prepared-question">Готовый вопрос к README</label><select id="prepared-question" v-model="chosenQuestion" :disabled="Boolean(chat.pending) || !service?.questions?.length" @change="chooseQuestion"><option value="">Выберите, чтобы вставить и отредактировать…</option><option v-for="question in service?.questions || []" :key="question.id" :value="question.id">{{ question.label || question.question }}</option></select><p class="prepared-hint">Выбор только подставляет текст. Отправьте его вручную.</p>
          <label class="sr-only" for="question">Ваш вопрос к документации</label><div class="composer-box" :class="{ invalid: Boolean(inputError) }"><textarea id="question" ref="composer" v-model="chat.draft" rows="2" placeholder="Например, как сохраняется история в дне 7?" :disabled="Boolean(chat.pending)" :aria-invalid="Boolean(inputError)" aria-describedby="composer-hint" @keydown="onKeydown"></textarea><button class="send-button" type="submit" :disabled="!canSend" aria-label="Отправить вопрос"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6" /></svg></button></div>
          <div class="compare-actions"><button class="compare-button" type="button" :disabled="!canCompare" @click="send('compare')">Сравнить с DeepSeek</button><p class="compare-notice">Каждое сравнение — новые запросы Qwen3 и DeepSeek с одинаковым RAG-контекстом. Вопрос и найденные README-фрагменты будут отправлены DeepSeek.<span v-if="!service?.comparison?.available"> Сравнение не настроено.</span></p></div>
          <div id="composer-hint" class="composer-hint" :class="{ invalid: Boolean(inputError) }">{{ inputError || 'Enter — отправить · Shift + Enter — новая строка · каждый вопрос независимый' }}</div>
        </form>
      </main>
    </div>
  </div>
</template>
