<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  MAX_PROMPT_BYTES, chatPayload, clearChat, completeSubmission, createChatState,
  failSubmission, shouldSubmitKey, startSubmission, utf8Bytes,
} from './chat-state.js';
import { PROMPT_PRESETS, insertPreset, shortcutForKey } from './prompt-presets.js';

const chat = ref(createChatState());
const health = ref(null);
const checking = ref(false);
const transcript = ref(null);
const composer = ref(null);
const elapsed = ref(0);
let timer;
let activeController;
let healthController;
const ready = computed(() => health.value?.status === 'ready');
const questionBytes = computed(() => utf8Bytes(chat.value.draft.trim()));
const tooLong = computed(() => questionBytes.value > MAX_PROMPT_BYTES);
const canSend = computed(() => ready.value && !chat.value.pending && !tooLong.value && questionBytes.value > 0);
const statusTitle = computed(() => checking.value ? 'Проверяем модель' : ready.value ? 'Модель готова' : 'Модель недоступна');
const formatted = (value, digits = 1) => value.toLocaleString('ru-RU', { maximumFractionDigits: digits });

async function readJson(response) {
  try { return await response.json(); }
  catch { throw new Error('Сервис вернул некорректный ответ. Попробуйте ещё раз.'); }
}

async function refreshHealth() {
  if (checking.value) return;
  checking.value = true;
  const controller = new AbortController();
  healthController = controller;
  const deadline = setTimeout(() => controller.abort(), 7000);
  try {
    const response = await fetch('/api/health', { signal: controller.signal });
    const data = await readJson(response);
    if (!response.ok || !['ready', 'missing_model', 'unavailable'].includes(data.status)) throw new Error();
    health.value = data;
  } catch {
    health.value = { status: 'unavailable', message: 'Не удалось связаться с локальным сервисом. Проверьте, что сервер приложения запущен, затем обновите состояние.' };
  } finally {
    clearTimeout(deadline);
    healthController = null;
    checking.value = false;
  }
}

async function send() {
  if (!canSend.value) return;
  chat.value = startSubmission(chat.value, chat.value.draft);
  const controller = new AbortController();
  activeController = controller;
  const started = performance.now();
  elapsed.value = 0;
  timer = setInterval(() => { elapsed.value = Math.floor((performance.now() - started) / 1000); }, 1000);
  const deadline = setTimeout(() => controller.abort(), 125000);
  try {
    const response = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(chatPayload(chat.value)), signal: controller.signal,
    });
    const data = await readJson(response);
    if (!response.ok) {
      if (data.error?.code === 'unavailable' || data.error?.code === 'missing_model') {
        health.value = { ...health.value, status: data.error.code, message: data.error.message };
      }
      throw new Error(typeof data.error?.message === 'string' ? data.error.message : 'Не удалось получить ответ. Попробуйте ещё раз.');
    }
    chat.value = completeSubmission(chat.value, data);
  } catch (error) {
    const message = error.name === 'AbortError'
      ? 'Время ожидания истекло. Сервер ещё может завершать запрос. Вопрос сохранён: повторите отправку самостоятельно.'
      : error instanceof TypeError
        ? 'Связь с локальным сервисом прервалась. Проверьте сервер и повторите отправку.'
        : error.message;
    chat.value = failSubmission(chat.value, message);
  } finally {
    clearInterval(timer);
    clearTimeout(deadline);
    activeController = null;
    await nextTick();
    composer.value?.focus();
  }
}

function newChat() {
  chat.value = clearChat(chat.value);
  nextTick(() => composer.value?.focus());
}

async function choosePreset(shortcut) {
  if (chat.value.pending) return;
  chat.value = insertPreset(chat.value, shortcut);
  await nextTick();
  const input = composer.value;
  if (!input) return;
  input.style.height = 'auto';
  input.style.height = `${Math.min(180, input.scrollHeight)}px`;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  input.scrollTop = input.scrollHeight;
}

function onKeydown(event) {
  const shortcut = shortcutForKey(event, chat.value.draft);
  if (shortcut) {
    event.preventDefault();
    choosePreset(shortcut);
    return;
  }
  if (shouldSubmitKey(event)) { event.preventDefault(); send(); }
}

watch(() => [chat.value.messages.length, chat.value.pending, chat.value.error, chat.value.notice], async () => {
  await nextTick();
  if (transcript.value) transcript.value.scrollTop = transcript.value.scrollHeight;
});

onMounted(refreshHealth);
onBeforeUnmount(() => {
  clearInterval(timer);
  activeController?.abort();
  healthController?.abort();
});
</script>

<template>
  <div class="app-shell">
    <header class="page-header">
      <div class="brand">
        <div class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M5 5h14v11H9l-4 3V5Z" /><path d="M9 9h6M9 12h4" /></svg>
        </div>
        <div><div class="eyebrow">AI CHALLENGE · ДЕНЬ 27</div><h1>Локальный чат</h1></div>
      </div>
      <div class="device-label"><span class="status-dot" :class="{ ready, checking }" aria-hidden="true"></span>На этом ноутбуке</div>
    </header>

    <div class="workspace">
      <aside class="model-panel" aria-label="Сведения о модели">
        <div class="section-label">ВАШ СОБЕСЕДНИК</div>
        <div class="model-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="3" /><path d="M9 1v4m6-4v4M9 19v4m6-4v4M1 9h4m-4 6h4m14-6h4m-4 6h4"/><rect x="9" y="9" width="6" height="6" rx="1"/></svg>
        </div>
        <h2>Qwen3 <span>4B</span></h2>
        <p class="model-description">Компактная модель для ваших вопросов и повседневных задач.</p>
        <div class="model-status" :class="{ ready }" role="status"><span class="status-dot" :class="{ ready, checking }" aria-hidden="true"></span>{{ statusTitle }}</div>
        <button class="text-button refresh" :disabled="checking || Boolean(chat.pending)" @click="refreshHealth">{{ checking ? 'Проверяем…' : 'Обновить состояние' }}</button>
        <div class="sidebar-divider"></div>
        <div class="privacy-note">
          <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 4v3"/></svg>
          <strong>Беседа остаётся здесь</strong>
          <p>Обработка на ноутбуке. Сообщения хранятся в памяти этой вкладки и исчезнут при обновлении страницы.</p>
        </div>
        <div class="context-note"><span class="section-label">ПАМЯТЬ ДИАЛОГА</span><p>До 4 последних пар сообщений. При длинной беседе старый контекст постепенно забывается.</p></div>
      </aside>

      <main class="chat-panel">
        <div class="chat-header">
          <div><h2>Ваша беседа</h2><p>Qwen3 4B · {{ chat.pending ? 'готовит ответ' : 'ваши вопросы, ваши идеи' }}</p></div>
          <button class="secondary new-chat" :disabled="Boolean(chat.pending)" @click="newChat"><span aria-hidden="true">+</span> Новый чат</button>
        </div>
        <div v-if="!ready && !checking" class="connection-banner" role="status">
          <span>{{ health?.message || 'Локальная модель пока недоступна.' }}</span>
          <button class="text-button" :disabled="Boolean(chat.pending)" @click="refreshHealth">Проверить</button>
        </div>
        <div v-if="checking" class="mobile-check" role="status">Проверяем локальную модель…</div>

        <div ref="transcript" class="transcript" role="log" aria-label="Сообщения беседы" aria-live="polite" :aria-busy="Boolean(chat.pending)">
          <div v-if="!chat.messages.length && !chat.pending" class="empty-state">
            <div class="empty-symbol" aria-hidden="true"><svg viewBox="0 0 32 32"><path d="m16 4 2.7 8.3L27 15l-8.3 2.7L16 26l-2.7-8.3L5 15l8.3-2.7L16 4Z"/><path d="m26 3 1 3 3 1-3 1-1 3-1-3-3-1 3-1 1-3Z"/></svg></div>
            <h3>Начните со своего вопроса</h3>
            <p>Здесь можно обсудить идею, разобраться в задаче<br class="desktop-break" /> или просто поговорить с локальной моделью.</p>
            <span class="empty-caption">Без облака. В вашем темпе.</span>
          </div>

          <article v-for="(message, index) in chat.messages" :key="index" class="message" :class="message.role">
            <div class="message-label"><span class="message-avatar" aria-hidden="true">{{ message.role === 'user' ? 'В' : 'Q' }}</span>{{ message.role === 'user' ? 'Вы' : 'Qwen3' }}</div>
            <div class="message-text">{{ message.content }}</div>
            <div v-if="message.metrics" class="answer-metrics" aria-label="Метрики ответа">
              <span>{{ formatted(message.metrics.total_seconds) }} с всего</span>
              <span>{{ message.metrics.output_tokens }} токенов</span>
              <span>{{ formatted(message.metrics.tokens_per_second) }} ток/с</span>
              <details><summary>Подробнее</summary><div>Загрузка: {{ formatted(message.metrics.load_seconds, 2) }} с · генерация: {{ formatted(message.metrics.generation_seconds, 2) }} с · вход: {{ message.metrics.prompt_tokens }} токенов</div></details>
            </div>
            <div v-if="message.truncated" class="truncation-note">Ответ достиг лимита длины. Можно попросить продолжить.</div>
          </article>

          <template v-if="chat.pending">
            <article class="message user"><div class="message-label"><span class="message-avatar" aria-hidden="true">В</span>Вы</div><div class="message-text">{{ chat.pending }}</div></article>
            <article class="message assistant pending"><div class="message-label"><span class="message-avatar" aria-hidden="true">Q</span>Qwen3</div><div class="pending-line"><span class="typing-dots" aria-hidden="true"><i></i><i></i><i></i></span><span>Готовлю ответ<span v-if="elapsed"> · {{ elapsed }} с</span></span></div><p class="pending-hint">Первый ответ может занять больше времени: модель загружается в память.</p></article>
          </template>
          <div v-if="chat.notice" class="notice" role="status">{{ chat.notice }}</div>
          <div v-if="chat.error" class="error-banner" role="alert">{{ chat.error }}</div>
        </div>

        <form class="composer-area" @submit.prevent="send">
          <div class="prompt-presets" aria-label="Подготовленные вопросы">
            <button v-for="preset in PROMPT_PRESETS" :key="preset.shortcut" type="button" :disabled="Boolean(chat.pending)" @click="choosePreset(preset.shortcut)"><span>{{ preset.title }}</span><kbd>{{ preset.shortcut }}</kbd></button>
          </div>
          <p class="presets-hint">Выберите вопрос или введите /1–/4 + Tab. Текст можно изменить; отправка вручную.</p>
          <label class="sr-only" for="question">Ваше сообщение</label>
          <div class="composer-box" :class="{ 'has-error': tooLong }">
            <textarea id="question" ref="composer" v-model="chat.draft" placeholder="Напишите своё сообщение…" rows="2" :disabled="Boolean(chat.pending)" :aria-invalid="tooLong" aria-describedby="composer-hint" @keydown="onKeydown"></textarea>
            <button class="send-button" type="submit" :disabled="!canSend" aria-label="Отправить сообщение"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6" /></svg></button>
          </div>
          <div id="composer-hint" class="composer-hint"><span :class="{ 'length-error': tooLong }">{{ tooLong ? `Слишком длинный вопрос: ${questionBytes} / 3000 байт` : 'Enter — отправить · Shift + Enter — новая строка' }}</span><span class="context-count">{{ chat.history.length / 2 }} / 4 пары в контексте</span></div>
          <p class="composer-footnote">Модель может ошибаться. История очищается при обновлении страницы.</p>
        </form>
      </main>
    </div>
  </div>
</template>
