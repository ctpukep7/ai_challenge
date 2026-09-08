<script setup>
import { computed, onMounted, ref } from "vue";

const MAX_STOPS = 16;
const models = { deepseek: "deepseek-v4-flash", openrouter: "openrouter/free" };

const conversations = ref([]);
const activeConversation = ref(null);
const prompt = ref("");
const status = ref("Загрузка");
const error = ref("");
const sending = ref(false);
const sidebarOpen = ref(true);
const settingsOpen = ref(false);
const previewOpen = ref(false);
const sessionMenuId = ref(null);
const renameOpen = ref(false);
const renameId = ref(null);
const preview = ref(null);
const newTitle = ref("");
const controls = ref({
  provider: "deepseek",
  model: "deepseek-v4-flash",
  format_instruction: "",
  temperature: "0",
  max_tokens: "",
  thinking_mode: "disabled",
  stop_sequences: [""],
});

const thinkingAvailable = computed(() => controls.value.provider === "deepseek");
const temperatureDisabled = computed(() => thinkingAvailable.value && controls.value.thinking_mode === "enabled");

function messageContent(message) {
  return message.role === "assistant" && message.answer_html
    ? message.answer_html
    : escapeHtml(message.content).replaceAll("\n", "<br>");
}

function escapeHtml(value) {
  const element = document.createElement("div");
  element.textContent = value;
  return element.innerHTML;
}

function errorText(payload) {
  if (typeof payload?.detail === "string") return payload.detail;
  if (typeof payload?.error === "string") return payload.error;
  return "Не удалось выполнить действие.";
}

async function api(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorText(payload));
  return payload;
}

async function loadConversations(preferredId) {
  const result = await api("/api/conversations");
  conversations.value = result.conversations;
  const selected = conversations.value.find((item) => item.id === preferredId) || conversations.value[0];
  if (selected) await selectConversation(selected.id);
}

async function selectConversation(id) {
  try {
    error.value = "";
    const result = await api(`/api/conversations/${id}`);
    activeConversation.value = result.conversation;
    status.value = "Ожидает вопрос";
  } catch (requestError) {
    error.value = requestError.message;
    status.value = "Ошибка";
  }
}

async function createConversation() {
  try {
    error.value = "";
    const result = await api("/api/conversations", { method: "POST" });
    await loadConversations(result.conversation.id);
  } catch (requestError) { error.value = requestError.message; }
}

async function deleteConversation(conversation = activeConversation.value) {
  if (!conversation || !window.confirm("Удалить этот диалог и всю его историю из SQLite?")) return;
  try {
    const result = await api(`/api/conversations/${conversation.id}`, { method: "DELETE" });
    sessionMenuId.value = null;
    conversations.value = result.conversations;
    await selectConversation(result.active.id);
  } catch (requestError) { error.value = requestError.message; }
}

function openRename(conversation = activeConversation.value) {
  if (!conversation) return;
  sessionMenuId.value = null;
  renameId.value = conversation.id;
  newTitle.value = conversation.title;
  renameOpen.value = true;
}

async function renameConversation() {
  if (renameId.value === null) return;
  try {
    error.value = "";
    const result = await api(`/api/conversations/${renameId.value}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: newTitle.value }),
    });
    renameOpen.value = false;
    if (activeConversation.value?.id === renameId.value) activeConversation.value = result.conversation;
    await loadConversations(activeConversation.value?.id);
    renameId.value = null;
  } catch (requestError) { error.value = requestError.message; }
}

function cleanControls() {
  return {
    ...controls.value,
    stop_sequences: controls.value.stop_sequences.map((item) => item.trim()).filter(Boolean),
  };
}

async function send() {
  if (!prompt.value.trim()) { error.value = "Введите вопрос."; return; }
  if (!activeConversation.value || sending.value) return;
  sending.value = true;
  error.value = "";
  status.value = "Агент отвечает…";
  try {
    const result = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: activeConversation.value.id, prompt: prompt.value, controls: cleanControls() }),
    });
    activeConversation.value = result.conversation;
    prompt.value = "";
    await loadConversations(activeConversation.value.id);
    status.value = "Готово";
  } catch (requestError) {
    error.value = requestError.message;
    status.value = "Ошибка";
  } finally { sending.value = false; }
}

function addStop() {
  if (controls.value.stop_sequences.length < MAX_STOPS) controls.value.stop_sequences.push("");
}
function removeStop(index) {
  if (controls.value.stop_sequences.length === 1) controls.value.stop_sequences[0] = "";
  else controls.value.stop_sequences.splice(index, 1);
}
function providerChanged() {
  controls.value.model = models[controls.value.provider];
  if (!thinkingAvailable.value) controls.value.thinking_mode = "disabled";
}
function showRequest(request) {
  preview.value = request;
  previewOpen.value = true;
}
function toggleSessionMenu(conversationId) {
  sessionMenuId.value = sessionMenuId.value === conversationId ? null : conversationId;
}
function metrics(message) {
  return [message.provider, message.model, message.elapsed_ms != null ? `${message.elapsed_ms} мс` : null,
    message.completion_tokens != null ? `${message.completion_tokens} токенов` : null,
    message.finish_reason ? `завершение: ${message.finish_reason}` : null].filter(Boolean);
}

onMounted(async () => {
  try { await loadConversations(); } catch (requestError) { error.value = requestError.message; status.value = "Ошибка"; }
});
</script>

<template>
  <div class="app-shell" :class="{ 'sidebar-closed': !sidebarOpen }">
    <aside class="session-sidebar" aria-label="Управление диалогами">
      <header class="sidebar-head">
        <div class="brand-copy"><p class="eyebrow">ДИАЛОГИ</p><h1>Память<br />агента</h1></div>
        <button class="collapse-button" type="button" :aria-expanded="sidebarOpen" :aria-label="sidebarOpen ? 'Свернуть диалоги' : 'Развернуть диалоги'" @click="sidebarOpen = !sidebarOpen">
          {{ sidebarOpen ? '‹' : '›' }}
        </button>
      </header>
      <div class="sidebar-content">
        <div class="session-actions">
          <button class="button secondary" :disabled="sending" @click="createConversation">＋ Новый</button>
        </div>
        <nav class="conversation-list" aria-label="Список диалогов">
          <div v-for="conversation in conversations" :key="conversation.id" class="session-row" :class="{ active: conversation.id === activeConversation?.id }">
            <button class="conversation" :disabled="sending" @click="selectConversation(conversation.id)">
              <span class="dot"></span><span><b>{{ conversation.title }}</b><small>{{ conversation.answer_count || 0 }} ответов</small></span>
            </button>
            <button class="conversation-menu-button" type="button" :disabled="sending" :aria-label="`Действия: ${conversation.title}`" :aria-expanded="sessionMenuId === conversation.id" @click="toggleSessionMenu(conversation.id)">⋯</button>
            <div v-if="sessionMenuId === conversation.id" class="conversation-menu">
              <button type="button" @click="openRename(conversation)">✎ Переименовать</button>
              <button class="menu-danger" type="button" @click="deleteConversation(conversation)">Удалить</button>
            </div>
          </div>
        </nav>
      </div>
      <footer class="challenge-footer"><span>● AI CHALLENGE 9 · DAY 07</span><strong>Сохранение контекста</strong><small>AssistantAgent · SQLite</small></footer>
    </aside>

    <main class="workspace">
      <section class="chat-card">
        <header class="chat-head">
          <div><p class="eyebrow">ВЫБРАННЫЙ ДИАЛОГ</p><h2>{{ activeConversation?.title || 'Загрузка…' }}</h2></div>
          <div class="chat-actions"><span class="status-dot" :class="{ busy: sending, error: error }"></span><span class="status">{{ status }}</span><button class="button settings-button" :aria-expanded="settingsOpen" @click="settingsOpen = true">⚙ Настройки</button></div>
        </header>

        <div class="thread" aria-live="polite">
          <div v-if="!activeConversation?.messages?.length" class="thread-empty">Диалог готов. После перезапуска приложения история останется в SQLite.</div>
          <article v-for="message in activeConversation?.messages" :key="message.id" class="message" :class="message.role">
            <p class="message-label">{{ message.role === 'user' ? 'ВЫ' : 'ASSISTANTAGENT' }}</p>
            <div class="bubble" v-html="messageContent(message)"></div>
            <div v-if="message.role === 'assistant'" class="message-tools">
              <div class="metrics"><span v-for="item in metrics(message)" :key="item">{{ item }}</span></div>
              <button v-if="message.request" class="request-button" @click="showRequest(message.request)">ⓘ JSON-запрос</button>
            </div>
          </article>
        </div>

        <form class="composer" @submit.prevent="send">
          <textarea v-model="prompt" :disabled="sending" placeholder="Напишите вопрос для агента" aria-label="Вопрос для агента" @keydown.enter.exact.prevent="send"></textarea>
          <div class="send-row"><span>Контекст хранится локально в SQLite.</span><button class="button primary" :disabled="sending">{{ sending ? 'Отправляем…' : 'Отправить →' }}</button></div>
          <p v-if="error" class="error-message">{{ error }}</p>
        </form>
      </section>
    </main>
  </div>

  <div v-if="settingsOpen" class="modal-backdrop" @click.self="settingsOpen = false">
    <section class="modal settings-modal" role="dialog" aria-modal="true" aria-label="Настройки агента">
      <header class="modal-head"><div><p class="eyebrow">CONTROL UNIT</p><h2>Настройки следующего ответа</h2></div><button class="icon-button" aria-label="Закрыть настройки" @click="settingsOpen = false">×</button></header>
      <p class="modal-note">Их можно менять в любой момент: новые значения применятся только к следующему сообщению.</p>
      <div class="settings-grid">
        <label>Провайдер<select v-model="controls.provider" @change="providerChanged"><option value="deepseek">DeepSeek</option><option value="openrouter">OpenRouter</option></select></label>
        <label>Модель<input v-model="controls.model" list="models" /><datalist id="models"><option value="deepseek-v4-flash" /><option value="deepseek-v4-pro" /><option value="openrouter/free" /></datalist></label>
        <label class="wide">Формат ответа<textarea v-model="controls.format_instruction" placeholder="Необязательная инструкция формата"></textarea></label>
        <label>Temperature<input v-model="controls.temperature" type="number" min="0" max="2" step="0.1" :disabled="temperatureDisabled" /></label>
        <label>Max tokens<input v-model="controls.max_tokens" type="number" min="1" max="10000" placeholder="Без лимита" /></label>
        <label>Thinking mode<select v-model="controls.thinking_mode" :disabled="!thinkingAvailable"><option value="disabled">Выключен</option><option value="enabled">Включен</option></select></label>
        <p class="thinking-note">{{ !thinkingAvailable ? 'Thinking mode доступен только в DeepSeek.' : controls.thinking_mode === 'enabled' ? 'При thinking DeepSeek не применяет temperature.' : 'Thinking выключен: temperature применяется.' }}</p>
      </div>
      <section class="stops"><div class="stops-head"><p class="eyebrow">STOP SEQUENCES · ДО {{ MAX_STOPS }}</p><button class="icon-button add-stop" :disabled="controls.stop_sequences.length >= MAX_STOPS" aria-label="Добавить условие завершения" @click="addStop">＋</button></div>
        <div v-for="(_, index) in controls.stop_sequences" :key="index" class="stop-row"><input v-model="controls.stop_sequences[index]" placeholder="Необязательный стоп-маркер" :aria-label="`Условие завершения ${index + 1}`" /><button class="remove-stop" aria-label="Удалить условие завершения" @click="removeStop(index)">×</button></div>
      </section>
    </section>
  </div>

  <div v-if="previewOpen" class="modal-backdrop" @click.self="previewOpen = false">
    <section class="modal preview-modal" role="dialog" aria-modal="true" aria-label="JSON-запрос агента"><header class="modal-head"><div><p class="eyebrow">БЕЗ API-КЛЮЧА</p><h2>JSON-запрос агента</h2></div><button class="icon-button" aria-label="Закрыть запрос" @click="previewOpen = false">×</button></header><p class="modal-note">Фактический запрос, которым был получен этот ответ.</p><pre>{{ JSON.stringify(preview, null, 2) }}</pre></section>
  </div>

  <div v-if="renameOpen" class="modal-backdrop" @click.self="renameOpen = false">
    <form class="modal rename-modal" role="dialog" aria-modal="true" aria-label="Переименовать диалог" @submit.prevent="renameConversation">
      <header class="modal-head"><div><p class="eyebrow">ДИАЛОГ</p><h2>Переименовать</h2></div><button class="icon-button" type="button" aria-label="Закрыть" @click="renameOpen = false">×</button></header>
      <p class="modal-note">Это название сохранится в SQLite и останется после перезапуска приложения.</p>
      <label class="rename-label">Название диалога<input v-model="newTitle" maxlength="80" autofocus /></label>
      <div class="rename-actions"><button class="button secondary-light" type="button" @click="renameOpen = false">Отмена</button><button class="button primary">Сохранить</button></div>
    </form>
  </div>
</template>
