<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { dialogStepsForStory, longTermSuggestions, stories, storyList } from "./demo-stories.js";

const state = ref(null);
const error = ref(null);
const status = ref("Загружаем память…");
const sending = ref(false);
const preview = ref(null);
const settingsOpen = ref(false);
const prompt = ref("");
const messagesFeed = ref(null);
const promptField = ref(null);
const selectedStoryId = ref(null);
const dialogStepIndex = ref({ game_of_thrones: 0, the_witcher: 0 });
const shownDialogStep = ref(0);
const applyingStory = ref(false);
const task = reactive({ goal: "", hard_constraints: "", task_data: "", open_questions: "" });
const memoryDraft = reactive({ content: "" });
const workingMemoryDraft = ref("");
const controls = reactive({
  provider: "deepseek",
  model: "deepseek-flash",
  temperature: 0,
  max_tokens: "",
  window_size: 6,
  batch_size: 10,
  keep_recent: 6,
  compression_mode: "full_session",
});

const compressionModes = [
  {
    id: "sliding_window",
    label: "Sliding Window",
    hint: "Day 10 — последние N сообщений выбранной сессии.",
    settings: "window",
    windowTitle: "Окно кратковременной памяти",
    windowHint: "В запрос попадут только последние N сообщений активной сессии.",
  },
  {
    id: "sticky_facts",
    label: "Sticky Facts",
    hint: "Day 10 — JSON facts + последние N; facts обновляет модель после ответа.",
    settings: "window",
    windowTitle: "Окно истории рядом с facts",
    windowHint: "Facts передаются целиком; из переписки берутся только последние N сообщений.",
  },
  {
    id: "rolling_summary",
    label: "Rolling summary",
    hint: "Day 09 — LLM-сводка архива + последние N; summary создаётся после ответа.",
    settings: "summary",
    summaryHint: "Когда в сессии накопится batch + recent сообщений, старый batch сожмётся в summary отдельным LLM-вызовом.",
  },
  {
    id: "full_session",
    label: "Полная сессия",
    hint: "Вся история активной сессии без обрезки.",
    settings: "none",
    info: "Дополнительные параметры не нужны: в запрос идёт вся история активной сессии, включая архивные сообщения.",
  },
  {
    id: "explicit_only",
    label: "Без истории сессии",
    hint: "Day 11 — только долговременная и рабочая память.",
    settings: "none",
    info: "Краткосрочная история не попадает в запрос. Остаются константы, долговременная память и рабочая задача.",
  },
];

const windowSizeOptions = [2, 4, 6, 8, 10, 12, 14, 16, 18, 20];
const activeCompression = computed(() => compressionModes.find((item) => item.id === controls.compression_mode) || compressionModes[0]);
const showWindowSettings = computed(() => activeCompression.value.settings === "window");
const showSummarySettings = computed(() => activeCompression.value.settings === "summary");
const showCompressionInfo = computed(() => activeCompression.value.settings === "none");

const MEMORY_PANEL_MIN = 280;
const MEMORY_PANEL_MAX = 560;
const MEMORY_PANEL_DEFAULT = 330;
const STORY_STORAGE_KEY = "day11.selectedStoryId";
const memoryPanelWidth = ref(MEMORY_PANEL_DEFAULT);
const resizableLayout = ref(false);
const resizing = ref(false);

const compressionLabel = computed(() => compressionModes.find((item) => item.id === controls.compression_mode)?.label || controls.compression_mode);
const modelOptions = computed(() => controls.provider === "deepseek"
  ? [
      { id: "deepseek-flash", label: "DeepSeek Flash" },
      { id: "deepseek-v4-pro", label: "DeepSeek V4 Pro" },
    ]
  : [{ id: "openrouter/free", label: "OpenRouter Free" }]);
const shellStyle = computed(() => (resizableLayout.value ? { "--memory-panel-width": `${memoryPanelWidth.value}px` } : null));

const storyTaskFields = ["goal", "hard_constraints", "task_data", "open_questions"];

function normalizedTaskValue(value) {
  return typeof value === "string" ? value.trim() : "";
}

function storyIdFor(value) {
  if (!value?.goal) return null;
  return storyList.find(({ id }) => storyTaskFields.every((field) =>
    normalizedTaskValue(value[field]) === normalizedTaskValue(stories[id][field]),
  ))?.id || null;
}

function syncStorySelection(value) {
  const matchedStoryId = storyIdFor(value);
  const selectionChanged = selectedStoryId.value !== matchedStoryId;
  selectedStoryId.value = matchedStoryId;
  if (matchedStoryId) {
    localStorage.setItem(STORY_STORAGE_KEY, matchedStoryId);
  } else {
    localStorage.removeItem(STORY_STORAGE_KEY);
  }
  if (selectionChanged) shownDialogStep.value = 0;
}

const activeStoryId = computed(() => storyIdFor(state.value?.task));
const activeStory = computed(() => stories[activeStoryId.value]);
const activeDialogSteps = computed(() => activeStoryId.value ? dialogStepsForStory(activeStoryId.value) : []);
const activeDialogStepCount = computed(() => activeDialogSteps.value.length);
const activeStoryLabel = computed(() => activeStory.value?.label || "Пользовательская задача");
const shownDialogItem = computed(() => activeDialogSteps.value[shownDialogStep.value] || null);
const nextDialogItem = computed(() => {
  if (!activeStoryId.value || !activeDialogSteps.value.length) return null;
  const index = dialogStepIndex.value[activeStoryId.value] % activeDialogSteps.value.length;
  return activeDialogSteps.value[index];
});
const needsFreshSession = computed(() => Boolean(
  nextDialogItem.value?.requiresFreshSession
  && messages.value.length,
));

const activeSession = computed(() => state.value?.active_session);
const sessions = computed(() => state.value?.sessions || []);
const entries = computed(() => state.value?.long_term || []);
const messages = computed(() => activeSession.value?.messages || []);
const stateReady = computed(() => Boolean(state.value));
const sessionCompression = computed(() => activeSession.value?.compression || {});
const canBranchSession = computed(() => Boolean(messages.value.length) && !sending.value);
const latestFacts = computed(() => sessionCompression.value.facts?.values || null);
const metrics = computed(() => state.value?.metrics || {});
const metricCards = computed(() => [
  { value: metrics.value.short_term_messages || 0, label: "сообщений в выбранной сессии", tone: "short" },
  { value: metrics.value.sessions || 0, label: "сессий кратковременной памяти", tone: "short" },
  { value: metrics.value.long_term_entries || 0, label: "записей долговременной памяти", tone: "long" },
  { value: metrics.value.working_task ? "есть" : "нет", label: "рабочая задача", tone: "working" },
  { value: metrics.value.summaries || 0, label: "rolling summaries", tone: "short" },
  { value: metrics.value.facts_updates || 0, label: "обновлений sticky facts", tone: "short" },
]);
const memoryEvents = computed(() => (state.value?.events || []).slice(0, 12));
const taskStateItems = computed(() => [
  { label: "Цель", value: task.goal },
  { label: "Ограничения", value: task.hard_constraints },
  { label: "Данные", value: task.task_data },
  { label: "Открытые вопросы", value: task.open_questions },
].filter((item) => item.value));

function api(path, options = {}, timeoutMs = path === "/api/ask" ? 90000 : 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(path, { ...options, signal: controller.signal })
    .then(async (response) => {
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw body.detail || {
          message: response.status >= 500
            ? "Сервер временно недоступен. Перезапустите npm run dev, если недавно очищали память."
            : "Не удалось выполнить запрос.",
          status: response.status,
        };
      }
      return body;
    })
    .catch((caught) => {
      if (caught?.name === "AbortError") {
        throw {
          message: "Превышено время ожидания сервера. Перезапустите npm run dev в папке day-11.",
          category: "timeout",
        };
      }
      throw caught;
    })
    .finally(() => clearTimeout(timer));
}

function applyState(next) {
  state.value = next.state || next;
  const storedTask = state.value.task || {};
  Object.assign(task, {
    goal: storedTask.goal || "",
    hard_constraints: storedTask.hard_constraints || "",
    task_data: storedTask.task_data || "",
    open_questions: storedTask.open_questions || "",
  });
  syncStorySelection(storedTask);
}

async function loadState() {
  try {
    applyState(await api("/api/state", {}, 8000));
    status.value = "Память готова";
    error.value = null;
  } catch (caught) {
    error.value = caught instanceof TypeError || caught?.message === "Failed to fetch"
      ? { message: "Нет соединения с API. Запустите npm run dev в папке day-11." }
      : caught;
    status.value = error.value?.message?.startsWith("Нет соединения") || error.value?.category === "timeout"
      ? "Сервер не отвечает"
      : "Ошибка сервера";
  }
}

async function addMemoryEntry(content) {
  applyState(await api("/api/long-term", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  }));
}

async function addMemory() {
  if (!memoryDraft.content.trim()) return;
  try {
    await addMemoryEntry(memoryDraft.content.trim());
    memoryDraft.content = "";
    status.value = "Долговременная память обновлена";
    error.value = null;
  } catch (caught) { error.value = caught; }
}

async function applyStory(storyId) {
  if (sending.value || applyingStory.value) return;
  const story = stories[storyId];
  if (!story) return;
  applyingStory.value = true;
  error.value = null;
  try {
    dialogStepIndex.value[storyId] = 0;
    shownDialogStep.value = 0;
    Object.assign(task, {
      goal: story.goal,
      hard_constraints: story.hard_constraints,
      task_data: story.task_data,
      open_questions: story.open_questions,
    });
    applyState(await api("/api/task", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(task) }));
    status.value = `${story.label}: сохранена только рабочая задача`;
    error.value = null;
  } catch (caught) {
    error.value = caught;
  } finally {
    applyingStory.value = false;
  }
}

function fillMemorySuggestion(suggestion) {
  if (!suggestion) return;
  memoryDraft.content = suggestion.content;
  status.value = `Подсказка «${suggestion.label}» добавлена в поле — нажмите «Добавить», чтобы сохранить`;
  nextTick(() => document.querySelector(".memory-add input")?.focus());
}

async function removeMemory(id) {
  try {
    applyState(await api(`/api/long-term/${id}`, { method: "DELETE" }));
    status.value = "Запись удалена";
    error.value = null;
  } catch (caught) { error.value = caught; }
}

async function saveTask() {
  const goal = workingMemoryDraft.value.trim();
  if (!goal) return;
  try {
    applyState(await api("/api/task", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal, hard_constraints: "", task_data: "", open_questions: "" }),
    }));
    workingMemoryDraft.value = "";
    status.value = "Рабочая задача заменена";
    error.value = null;
  } catch (caught) { error.value = caught; }
}

async function newSession() {
  try {
    applyState(await api("/api/sessions", { method: "POST" }));
    status.value = "Открыт новый краткосрочный диалог";
    error.value = null;
    return true;
  } catch (caught) { error.value = caught; }
  return false;
}

async function activateSession(sessionId) {
  if (sending.value || activeSession.value?.id === sessionId) return;
  try {
    applyState(await api(`/api/sessions/${sessionId}/activate`, { method: "POST" }));
    preview.value = null;
    status.value = "Выбрана кратковременная память сессии";
    error.value = null;
    await nextTick();
    messagesFeed.value?.scrollTo({ top: messagesFeed.value.scrollHeight, behavior: "smooth" });
  } catch (caught) { error.value = caught; }
}

async function resetSession() {
  if (!activeSession.value || !window.confirm("Удалить только сообщения текущего диалога? Рабочая задача и записи долговременной памяти останутся.")) return;
  try {
    applyState(await api(`/api/sessions/${activeSession.value.id}/reset`, { method: "POST" }));
    preview.value = null;
    status.value = "Краткосрочный диалог сброшен";
    error.value = null;
  } catch (caught) { error.value = caught; }
}

async function clearAllMemory() {
  if (sending.value || applyingStory.value) return;
  if (!window.confirm("Очистить всю память? Записи долговременной памяти, рабочая задача, все сессии и журнал MEMORY_EVENTS будут удалены.")) return;
  try {
    applyState(await api("/api/memory/clear", { method: "POST" }));
    selectedStoryId.value = null;
    localStorage.removeItem(STORY_STORAGE_KEY);
    dialogStepIndex.value = { game_of_thrones: 0, the_witcher: 0 };
    shownDialogStep.value = 0;
    memoryDraft.content = "";
    workingMemoryDraft.value = "";
    preview.value = null;
    prompt.value = "";
    status.value = "Память очищена";
    error.value = null;
    settingsOpen.value = false;
  } catch (caught) {
    error.value = caught;
  }
}

function controlsForApi() {
  const payload = {
    provider: controls.provider,
    model: controls.model,
    temperature: Number(controls.temperature),
    compression_mode: controls.compression_mode,
  };
  if (controls.max_tokens !== "" && controls.max_tokens != null) {
    payload.max_tokens = Number(controls.max_tokens);
  }
  if (controls.compression_mode === "sliding_window" || controls.compression_mode === "sticky_facts") {
    payload.window_size = Number(controls.window_size);
  }
  if (controls.compression_mode === "rolling_summary") {
    payload.batch_size = Number(controls.batch_size);
    payload.keep_recent = Number(controls.keep_recent);
  }
  return payload;
}

async function branchSession() {
  if (!activeSession.value || !canBranchSession.value) return;
  try {
    applyState(await api(`/api/sessions/${activeSession.value.id}/branch`, { method: "POST" }));
    preview.value = null;
    status.value = "Создана ветка — копия текущей сессии";
    error.value = null;
    await nextTick();
    messagesFeed.value?.scrollTo({ top: messagesFeed.value.scrollHeight, behavior: "smooth" });
  } catch (caught) { error.value = caught; }
}

function showSummaries() {
  preview.value = { summaries: sessionCompression.value.summaries || [] };
}

function showFacts() {
  preview.value = { facts: latestFacts.value, factsHistory: sessionCompression.value.facts_history || [] };
}

function clampMemoryPanelWidth(value) {
  return Math.min(MEMORY_PANEL_MAX, Math.max(MEMORY_PANEL_MIN, value));
}

function updateResizableLayout() {
  resizableLayout.value = window.matchMedia("(min-width: 1041px)").matches;
}

function persistMemoryPanelWidth() {
  localStorage.setItem("day11.memoryPanelWidth", String(memoryPanelWidth.value));
}

function onResizeMove(event) {
  if (!resizing.value) return;
  memoryPanelWidth.value = clampMemoryPanelWidth(window.innerWidth - event.clientX);
}

function stopResize() {
  if (!resizing.value) return;
  resizing.value = false;
  document.body.classList.remove("is-resizing");
  persistMemoryPanelWidth();
  window.removeEventListener("pointermove", onResizeMove);
  window.removeEventListener("pointerup", stopResize);
}

function startResize(event) {
  if (!resizableLayout.value || event.button !== 0) return;
  event.preventDefault();
  resizing.value = true;
  document.body.classList.add("is-resizing");
  window.addEventListener("pointermove", onResizeMove);
  window.addEventListener("pointerup", stopResize);
}

async function ask() {
  const text = prompt.value.trim();
  if (!text || sending.value) return;
  sending.value = true;
  status.value = "Запрос к DeepSeek…";
  error.value = null;
  try {
    const result = await api("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt: text, controls: controlsForApi() }) });
    applyState(result);
    prompt.value = "";
    status.value = "Ответ сохранён только в выбранной сессии";
    await nextTick();
    messagesFeed.value?.scrollTo({ top: messagesFeed.value.scrollHeight, behavior: "smooth" });
  } catch (caught) {
    error.value = caught;
    if (caught.category === "configuration") {
      status.value = "Нужен ключ провайдера";
    } else if (caught.provider_detail) {
      status.value = caught.provider_detail;
    } else if (caught.message) {
      status.value = caught.message;
    } else {
      status.value = "Ответ не получен — память не изменена";
    }
  } finally { sending.value = false; }
}

function previewFor(message) {
  if (message.preview) preview.value = message.preview;
}

function showJson(message) {
  if (message.preview) preview.value = { rawJson: message.preview };
}

function sourceFor(item) {
  if (item.role === "user") return "Текущий диалог / пользователь";
  if (item.role === "assistant") return "Текущий диалог / агент";
  if (item.content.startsWith("Неизменяемые константы")) return "Константы агента";
  if (item.content.startsWith("Долговременная память")) return "Долговременная память";
  if (item.content.startsWith("Рабочая память")) return "Рабочая задача";
  if (item.content.startsWith("Сжатая история")) return "Rolling summary (Day 09)";
  if (item.content.startsWith("Sticky Facts")) return "Sticky Facts (Day 10)";
  return "Системный блок";
}

function handleKey(event) {
  if (event.key === "Enter" && !event.shiftKey && !event.metaKey) {
    event.preventDefault();
    ask();
  }
}

function switchProvider() {
  controls.model = controls.provider === "deepseek" ? "deepseek-flash" : "openrouter/free";
}

function insertDialogStep() {
  const steps = activeDialogSteps.value;
  if (!steps.length) return;
  const storyId = activeStoryId.value;
  const index = dialogStepIndex.value[storyId] % steps.length;
  const item = steps[index];
  if (item.requiresFreshSession && messages.value.length) {
    status.value = `${activeStoryLabel.value}: перед первым вопросом создайте новую сессию`;
    return;
  }
  prompt.value = item.text;
  shownDialogStep.value = index;
  dialogStepIndex.value[storyId] = index + 1;
  const progress = `вопрос ${item.stepNumber}/${item.stepsInDialog}`;
  status.value = item.before
    ? `${activeStoryLabel.value} · ${progress}: ${item.before}`
    : item.hint
      ? `${activeStoryLabel.value} · ${progress}: ${item.hint}`
      : `${activeStoryLabel.value} · ${progress} — отправьте, когда будете готовы`;
  nextTick(() => promptField.value?.focus());
}

async function startNextDialog() {
  if (sending.value || !needsFreshSession.value) return;
  if (await newSession()) insertDialogStep();
}

function auditLayerLabel(layer) {
  return {
    short_term: "краткосрочная",
    working: "рабочая",
    long_term: "долговременная",
  }[layer] || layer;
}

function auditActionLabel(action) {
  return {
    save: "сохранено",
    update: "обновлено",
    replace: "заменено",
    delete: "удалено",
    reset: "сброшено",
    clear: "очищено",
    create: "создано",
    activate: "выбрано",
    branch: "ветвление",
  }[action] || action;
}

function auditActorLabel(actor) {
  return actor === "agent" ? "агент" : actor === "user" ? "пользователь" : actor;
}

function auditTime(value) {
  return value ? value.replace("T", " ").slice(0, 16) : "без времени";
}

onMounted(() => {
  const stored = Number.parseInt(localStorage.getItem("day11.memoryPanelWidth") || "", 10);
  if (Number.isFinite(stored)) memoryPanelWidth.value = clampMemoryPanelWidth(stored);
  updateResizableLayout();
  window.addEventListener("resize", updateResizableLayout);
  loadState();
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", updateResizableLayout);
  stopResize();
});
</script>

<template>
  <div class="app-page">
    <main class="app-shell" :class="{ resizable: resizableLayout, resizing }" :style="shellStyle">
    <aside class="sidebar" aria-label="Кратковременная память">
      <div class="brand"><p>AI CHALLENGE 9 · DAY 11</p><h1>Память<br>по слоям</h1></div>
      <div class="sidebar-heading"><p>КРАТКОВРЕМЕННАЯ ПАМЯТЬ</p><span>сессии</span></div>
      <button class="new-session" :disabled="sending" @click="newSession">＋ Новая сессия</button>
      <button class="branch-session" :disabled="!canBranchSession" title="Скопировать текущую сессию в новую ветку" @click="branchSession">⎇ Ветка</button>
      <nav class="session-list" aria-label="Сессии кратковременной памяти">
        <div v-for="session in sessions" :key="session.id" class="session-row" :class="{ active: session.id === activeSession?.id, branch: session.branched_from_id }">
          <button class="session" :disabled="sending" @click="activateSession(session.id)"><b>{{ session.label }}</b><small>{{ session.message_count }} {{ session.message_count === 1 ? 'сообщение' : session.message_count < 5 ? 'сообщения' : 'сообщений' }}<template v-if="session.branched_from_id"> · от #{{ session.branched_from_id }}</template></small></button>
        </div>
      </nav>
      <footer><b>SHORT-TERM SCOPE</b><span>Выбранная сессия — единственная история в следующем запросе.</span></footer>
    </aside>

    <section class="workspace" :aria-busy="!stateReady">
      <section class="chat-screen" aria-label="Чат с агентом">
        <section class="dialog-card">
          <header class="dialog-head">
            <div><p class="eyebrow">КРАТКОСРОЧНАЯ · SESSION #{{ activeSession?.id }}</p><h1>{{ activeSession?.label || 'Текущий диалог' }}</h1><div class="status-line"><i :class="{ busy: sending, bad: error }"></i><span>{{ status }}</span></div></div>
            <div class="dialog-actions">
              <button v-if="controls.compression_mode === 'rolling_summary' && sessionCompression.summaries?.length" class="info-button" type="button" @click="showSummaries">▤ Summary ({{ sessionCompression.summaries.length }})</button>
              <button v-if="controls.compression_mode === 'sticky_facts' && latestFacts" class="info-button" type="button" @click="showFacts">▤ Facts</button>
              <button class="settings" type="button" :disabled="sending" @click="settingsOpen = true">⚙ Настройки</button>
              <button class="danger" type="button" :disabled="sending || !messages.length" @click="resetSession">Сбросить</button>
            </div>
          </header>

          <section v-if="messages.length" ref="messagesFeed" class="messages" aria-live="polite"><article v-for="message in messages" :key="message.id" class="message" :class="message.role"><header><span>{{ message.role === 'user' ? 'ПОЛЬЗОВАТЕЛЬ' : 'АГЕНТ' }}</span><div v-if="message.role === 'assistant' && message.preview" class="message-actions"><button type="button" @click="previewFor(message)">Контекст запроса</button><button type="button" @click="showJson(message)">ⓘ JSON</button></div></header><p v-if="message.role === 'user'">{{ message.content }}</p><div v-else v-html="message.answer_html"></div></article></section>
          <section v-else ref="messagesFeed" class="empty-dialog"><b>Сессия пока пуста.</b><span>Первый вопрос сохранится только после успешного ответа агента.</span></section>

          <form class="composer" @submit.prevent="ask"><textarea ref="promptField" v-model="prompt" :disabled="sending" maxlength="4000" placeholder="Спросите агента о текущей задаче…" @keydown="handleKey"></textarea><div class="composer-tools"><div class="scenario-tools"><button v-if="needsFreshSession" class="preset-button next-session-button" type="button" :disabled="sending" @click="startNextDialog">＋ Новая сессия для задачи 2</button><button v-else class="preset-button" type="button" :disabled="sending || !activeDialogStepCount" @click="insertDialogStep">▦ Следующий вопрос</button><span>{{ activeStoryLabel }}<template v-if="shownDialogItem"> · {{ shownDialogItem.stepNumber }}/{{ shownDialogItem.stepsInDialog }}</template></span><small v-if="activeStory">{{ needsFreshSession ? "Сравните память: прежний диалог исчезнет, новая рабочая тема и long-term останутся." : shownDialogItem?.hint || shownDialogItem?.before || "Три вопроса для текущей темы." }}</small><small v-else>У пользовательской задачи нет готового сценария: вопрос вводится вручную.</small></div><div class="send-tools"><span>{{ prompt.length }} / 4000 · Enter — отправить</span><button class="send" :disabled="sending || !prompt.trim()">{{ sending ? 'Агент отвечает…' : 'Спросить агента →' }}</button></div></div><p v-if="error" class="error"><b>{{ error.message || 'Ошибка' }}</b><span v-if="error.provider_detail">{{ error.provider_detail }}</span></p></form>
        </section>
      </section>
    </section>

    <div
      v-if="resizableLayout"
      class="panel-resizer"
      role="separator"
      aria-orientation="vertical"
      aria-label="Изменить ширину правой панели"
      :aria-valuenow="memoryPanelWidth"
      aria-valuemin="280"
      aria-valuemax="560"
      tabindex="0"
      @pointerdown="startResize"
    ></div>

    <aside class="memory-panel" aria-label="Долгосрочная и рабочая память">
      <section class="memory-block long-memory">
        <header>
          <p class="eyebrow">ДОЛГОСРОЧНАЯ ПАМЯТЬ</p>
          <p>Остаётся при смене сессии и рабочей задачи.</p>
        </header>
        <div class="memory-add" role="group" aria-label="Добавить в долговременную память">
          <input v-model="memoryDraft.content" maxlength="500" aria-label="Текст для долговременной памяти" placeholder="Напишите, что нужно запомнить" @keydown.enter.prevent="addMemory">
          <button class="secondary" type="button" :disabled="!memoryDraft.content.trim()" @click="addMemory">Добавить</button>
        </div>
        <div class="preset-toolbar suggestion-toolbar">
          <small>Два независимых примера — сохраните один или оба:</small>
          <div class="preset-row">
            <button
              v-for="suggestion in longTermSuggestions"
              :key="suggestion.label"
              class="preset-button"
              type="button"
              :disabled="sending"
              @click="fillMemorySuggestion(suggestion)"
            >{{ suggestion.label }}</button>
          </div>
        </div>
        <section class="memory-state" aria-label="Состояние долговременной памяти">
          <div class="memory-state-head"><b>Состояние памяти</b><span>{{ entries.length }} / 40</span></div>
          <div class="entry-list"><p v-if="!entries.length" class="muted">Пока пусто.</p><article v-for="entry in entries" :key="entry.id" class="entry"><p>{{ entry.content }}</p><button type="button" :aria-label="`Удалить запись ${entry.id}`" @click="removeMemory(entry.id)">×</button></article></div>
        </section>
      </section>

      <section class="memory-block working-memory">
        <header>
          <p class="eyebrow">РАБОЧАЯ ПАМЯТЬ</p>
          <p>Новый текст полностью заменит текущую задачу.</p>
        </header>
        <div class="memory-add working-add" role="group" aria-label="Изменить рабочую память">
          <input v-model="workingMemoryDraft" maxlength="500" aria-label="Текст рабочей памяти" placeholder="Напишите текущую задачу" @keydown.enter.prevent="saveTask">
          <button class="secondary working-button" type="button" :disabled="!workingMemoryDraft.trim()" @click="saveTask">{{ task.goal ? 'Заменить' : 'Добавить' }}</button>
        </div>
        <div class="preset-toolbar">
          <small>Или выберите тему разговора:</small>
          <div class="preset-row">
            <button
              v-for="storyItem in storyList"
              :key="storyItem.id"
              class="preset-button working-button"
              :class="{ active: activeStoryId === storyItem.id && selectedStoryId === storyItem.id }"
              type="button"
              :disabled="sending || applyingStory"
              @click="applyStory(storyItem.id)"
            >▦ {{ storyItem.label }}</button>
          </div>
          <div class="experiment-steps" aria-label="Эксперимент с памятью">
            <span><b>1</b> сохранить роль эксперта</span>
            <span><b>2</b> выбрать тему разговора</span>
            <span><b>3</b> задать три вопроса</span>
            <span><b>4</b> сменить тему в новой сессии</span>
          </div>
        </div>
        <section class="memory-state" aria-label="Состояние рабочей памяти">
          <div class="memory-state-head"><b>Состояние памяти</b><span>{{ taskStateItems.length ? 'заполнена' : 'пусто' }}</span></div>
          <dl v-if="taskStateItems.length" class="task-state">
            <div v-for="item in taskStateItems" :key="item.label"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></div>
          </dl>
          <p v-else class="muted">Текущей задачи нет.</p>
        </section>
      </section>
    </aside>

    <div v-if="settingsOpen" class="backdrop" @click.self="settingsOpen = false">
      <section class="modal" role="dialog" aria-modal="true" aria-label="Настройки запроса">
        <header>
          <div>
            <p class="eyebrow">ГРАНИЦЫ ВЫЗОВА</p>
            <h2>Настройки запроса</h2>
            <p>Режим сжатия применяется к следующему обращению и не записывается в SQLite как постоянная настройка.</p>
          </div>
          <button class="close" type="button" aria-label="Закрыть настройки" @click="settingsOpen = false">×</button>
        </header>
        <div class="form-grid">
          <label>Провайдер<select v-model="controls.provider" @change="switchProvider"><option value="deepseek">DeepSeek</option><option value="openrouter">OpenRouter</option></select></label>
          <label>Модель<select v-model="controls.model"><option v-for="model in modelOptions" :key="model.id" :value="model.id">{{ model.label }}</option></select></label>
          <label>temperature<input v-model.number="controls.temperature" type="number" min="0" max="2" step="0.1"></label>
          <label>max_tokens<input v-model="controls.max_tokens" type="number" min="1" max="4096" placeholder="Не задавать"></label>
          <label class="wide">Сжатие краткосрочной памяти<select v-model="controls.compression_mode"><option v-for="mode in compressionModes" :key="mode.id" :value="mode.id">{{ mode.label }}</option></select><small>{{ activeCompression.hint }}</small></label>
        </div>

        <section v-if="showWindowSettings" class="compression-settings" aria-live="polite">
          <p class="eyebrow">ПАРАМЕТРЫ {{ activeCompression.label.toUpperCase() }}</p>
          <div class="form-grid">
            <label class="wide">{{ activeCompression.windowTitle }}<select v-model.number="controls.window_size"><option v-for="n in windowSizeOptions" :key="n" :value="n">{{ n }} сообщений</option></select><small>{{ activeCompression.windowHint }}</small></label>
          </div>
        </section>

        <section v-else-if="showSummarySettings" class="compression-settings" aria-live="polite">
          <p class="eyebrow">ПАРАМЕТРЫ ROLLING SUMMARY</p>
          <div class="form-grid">
            <label>Сжимать каждые<input v-model.number="controls.batch_size" type="number" min="2" max="20" step="2"><small>Чётное число сообщений, которое уйдёт в новый summary.</small></label>
            <label>Оставлять последних<input v-model.number="controls.keep_recent" type="number" min="2" max="20" step="2"><small>Столько последних сообщений останется в запросе без архивации.</small></label>
            <label class="wide"><small>{{ activeCompression.summaryHint }}</small></label>
          </div>
        </section>

        <section v-else-if="showCompressionInfo" class="compression-settings compression-info" aria-live="polite">
          <p class="eyebrow">БЕЗ ДОПОЛНИТЕЛЬНЫХ ПАРАМЕТРОВ</p>
          <p>{{ activeCompression.info }}</p>
        </section>

        <section class="compression-settings compression-info memory-clear-settings">
          <p class="eyebrow">СБРОС ПАМЯТИ</p>
          <p>Удалит записи долговременной памяти, рабочую задачу, все сессии и журнал MEMORY_EVENTS. Откроется пустой «Диалог 1».</p>
          <button class="danger" type="button" :disabled="sending || applyingStory" @click="clearAllMemory">Очистить память</button>
        </section>
      </section>
    </div>

    <div v-if="preview" class="backdrop" @click.self="preview = null"><section class="modal preview-panel" role="dialog" aria-modal="true" aria-label="Контекст запроса"><header><div><p class="eyebrow">БЕЗ API-КЛЮЧА · ФАКТИЧЕСКИЙ JSON</p><h2>{{ preview.rawJson ? 'Фактический JSON' : preview.summaries ? 'История summary' : preview.factsHistory ? 'Sticky Facts' : 'Контекст запроса' }}</h2><p>{{ preview.rawJson ? 'Полный JSON контекста запроса без ключа.' : preview.summaries ? 'Цепочка rolling-summary для активной сессии.' : preview.factsHistory ? 'Текущие facts и история обновлений.' : 'Порядок ниже — ровно тот, что получил провайдер.' }}</p></div><button class="close" type="button" aria-label="Закрыть preview" @click="preview = null">×</button></header><section v-if="preview.summaries" class="summary-history"><article v-for="(summary, index) in preview.summaries" :key="summary.id"><p>SUMMARY {{ index + 1 }} · до сообщения #{{ summary.through_message_id }}</p><pre>{{ summary.content }}</pre></article></section><section v-else-if="preview.factsHistory" class="facts-history"><article><p>ТЕКУЩИЕ FACTS</p><pre>{{ JSON.stringify(preview.facts, null, 2) }}</pre></article><article v-for="(snapshot, index) in preview.factsHistory" :key="snapshot.id"><p>ОБНОВЛЕНИЕ {{ index + 1 }}</p><pre>{{ JSON.stringify(snapshot.values, null, 2) }}</pre></article></section><section v-else-if="preview.rawJson" class="json-preview"><pre>{{ JSON.stringify(preview.rawJson, null, 2) }}</pre></section><template v-else><div class="preview-settings"><span>{{ preview.provider }} · {{ preview.model }}</span><span>temperature {{ preview.temperature }}<template v-if="preview.max_tokens != null"> · max {{ preview.max_tokens }}</template> · {{ preview.compression_label || compressionLabel }}</span><span v-if="preview.compression_mode === 'sliding_window' || preview.compression_mode === 'sticky_facts'">окно {{ preview.window_size }}</span><span v-if="preview.compression_mode === 'rolling_summary'">batch {{ preview.batch_size }} · recent {{ preview.keep_recent }}</span></div><ol class="context-list"><li v-for="(item, index) in preview.messages" :key="index" :class="item.role"><header><b>{{ index + 1 }}. {{ sourceFor(item) }}</b><span>{{ item.role }}</span></header><pre>{{ item.content }}</pre></li></ol></template></section></div>
    </main>

    <section v-if="stateReady" class="after-chat-info" aria-label="Статистика памяти и аудит">
      <header class="after-chat-heading">
        <div>
          <p class="eyebrow">ПОСЛЕ ЧАТА · БЕЗ СОДЕРЖИМОГО ДАННЫХ</p>
          <h2>Статистика и аудит памяти</h2>
          <p>Сводка показывает размер слоёв, а журнал — только факт изменения: без вопросов, ответов и текста записей.</p>
        </div>
        <span class="audit-limit">последние {{ memoryEvents.length }} событий</span>
      </header>

      <section class="metrics-grid" aria-label="Метрики памяти">
        <article v-for="metric in metricCards" :key="metric.label" class="metric-card" :class="metric.tone">
          <b>{{ metric.value }}</b>
          <span>{{ metric.label }}</span>
        </article>
      </section>

      <section class="audit-card" aria-label="Аудит изменений памяти">
        <header>
          <div><p class="eyebrow">MEMORY_EVENTS</p><h3>Последние изменения</h3></div>
          <span>Данные не дублируются</span>
        </header>
        <ol v-if="memoryEvents.length" class="audit-list">
          <li v-for="event in memoryEvents" :key="event.id">
            <span class="audit-layer" :class="event.layer">{{ auditLayerLabel(event.layer) }}</span>
            <div><b>{{ auditActionLabel(event.action) }}</b><span>{{ auditActorLabel(event.actor) }} · {{ event.object_type }}<template v-if="event.object_id != null"> #{{ event.object_id }}</template></span></div>
            <time :datetime="event.created_at">{{ auditTime(event.created_at) }}</time>
          </li>
        </ol>
        <p v-else class="muted">Изменений памяти пока нет.</p>
      </section>
    </section>
  </div>
</template>
