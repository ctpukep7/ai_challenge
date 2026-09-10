<script setup>
import { computed, nextTick, onMounted, ref, watch } from "vue";

const MAX_STOPS = 16;
const defaults = { deepseek: "deepseek-v4-flash", openrouter: "openrouter/free" };
const conversations = ref([]);
const activeConversation = ref(null);
const stats = ref(null);
const prompt = ref("");
const error = ref("");
const errorDetails = ref(null);
const errorInfo = ref(null);
const status = ref("Загрузка");
const sending = ref(false);
const sidebarOpen = ref(true);
const settingsOpen = ref(false);
const preview = ref(null);
const sessionMenuId = ref(null);
const rename = ref(null);
const metadata = ref(null);
const metadataLoading = ref(false);
const modelOptions = ref([]);
const syntheticCopies = ref(1);
const MAX_SYNTHETIC_COPIES = 64;
const threadElement = ref(null);
const presetsOpen = ref(false);
const controls = ref({ provider: "deepseek", model: "deepseek-v4-flash", format_instruction: "", temperature: "0", max_tokens: "", thinking_mode: "disabled", stop_sequences: [""] });

const promptPresets = [
  {
    title: "Старт: короткий факт",
    description: "Первый небольшой ход для короткого диалога.",
    prompt: "Запомни: мой любимый напиток — чай с лимоном. Ответь одним коротким предложением.",
  },
  {
    title: "Наращивание контекста",
    description: "Попросит большой ответ, который станет частью следующего контекста.",
    prompt: "Продолжи предыдущую тему развёрнутым объяснением на 700–900 слов. Раздели ответ на 6 коротких разделов: определение, причины, пример, исключения, практические советы и вывод. Не повторяй предыдущие формулировки.",
  },
  {
    title: "Преобразование истории",
    description: "Использует всю накопленную переписку и добавляет ещё один объёмный ответ.",
    prompt: "Используя всю предыдущую переписку, составь таблицу из 12 строк: тезис, объяснение, пример и типичная ошибка. Не сокращай важные детали из истории.",
  },
  {
    title: "Большой самостоятельный ответ",
    description: "Быстро увеличивает output tokens, даже в пустом диалоге.",
    prompt: "Подготовь подробный учебный конспект «Как работает HTTP-запрос» в 8 разделах. В каждом разделе дай объяснение, пример и типичную ошибку. В конце добавь 10 вопросов для самопроверки с ответами.",
  },
  {
    title: "Критический разбор",
    description: "Добавляет альтернативные объяснения и опирается на всю историю.",
    prompt: "Проанализируй всю предыдущую переписку критически. Найди 8 спорных тезисов, для каждого приведи контраргумент, уточнение и более точную формулировку.",
  },
  {
    title: "План урока",
    description: "Создаёт структурированный материал с практикой и проверкой.",
    prompt: "На основе всей переписки подготовь план 90-минутного урока: цели, 6 этапов, упражнения, вопросы преподавателя, ожидаемые ответы и критерии оценки.",
  },
  {
    title: "Карточки повторения",
    description: "Повторно использует контекст и превращает его в 20 пар вопрос–ответ.",
    prompt: "Используя всю историю диалога, составь 20 карточек для повторения. Для каждой дай вопрос, краткий ответ, подробное объяснение и пример применения.",
  },
  {
    title: "Сравнение подходов",
    description: "Подходит для накопленной темы: таблица, плюсы, минусы и рекомендации.",
    prompt: "Сравни 5 подходов, упомянутых или логично следующих из предыдущей переписки. Для каждого опиши принцип, преимущества, ограничения, пример и рекомендацию по применению.",
  },
  {
    title: "Код: спроектировать API",
    description: "Архитектура, маршруты, схема данных и примеры запросов.",
    prompt: "Спроектируй REST API для сервиса задач на Python. Дай структуру проекта, модели данных, 8 маршрутов, примеры JSON-запросов и ответов, обработку ошибок и обоснование архитектурных решений.",
  },
  {
    title: "Код: реализовать модуль",
    description: "Просит полный пример с типами, валидацией и комментариями.",
    prompt: "Напиши полный Python-модуль для менеджера задач: dataclass-модель, in-memory репозиторий, CRUD-операции, валидация, пользовательские исключения и примеры использования. Объясни каждую часть кода после листинга.",
  },
  {
    title: "Код: ревью истории",
    description: "Использует накопленный код и ищет ошибки, крайние случаи и улучшения.",
    prompt: "Выступи как senior code reviewer. Используя весь код и контекст из предыдущей переписки, составь подробный review: корректность, безопасность, читаемость, производительность, крайние случаи и конкретные исправления с примерами кода.",
  },
  {
    title: "Код: тесты и документация",
    description: "Добавляет тестовые случаи, фикстуры и README-описание.",
    prompt: "На основе всей предыдущей переписки подготовь набор unit-тестов на pytest: позитивные сценарии, ошибки валидации, крайние случаи и мокирование. Затем напиши раздел README с установкой, запуском и примерами API.",
  },
];

const thinkingAvailable = computed(() => controls.value.provider === "deepseek");
const temperatureDisabled = computed(() => thinkingAvailable.value && controls.value.thinking_mode === "enabled");
const activeMessages = computed(() => activeConversation.value?.messages || []);
const canDoubleSyntheticContext = computed(() => activeMessages.value.length > 0 && syntheticCopies.value < MAX_SYNTHETIC_COPIES);
const latest = computed(() => stats.value?.latest || null);
const displayedContextLimit = computed(() => latest.value?.context_length || metadata.value?.context_length || null);
const historyMoney = computed(() => stats.value?.turns ? money(stats.value?.totals?.cost_usd) : "станет доступно");
const contextPercent = computed(() => {
  if (!latest.value?.context_length || latest.value.prompt_tokens == null) return null;
  return Math.min(100, (latest.value.prompt_tokens / latest.value.context_length) * 100);
});
const chart = computed(() => {
  const points = stats.value?.points || [];
  if (!points.length) return [];
  const maximum = Math.max(...points.map((point) => point.cumulative_total_tokens || 0), 1);
  const divisor = Math.max(points.length - 1, 1);
  return points.map((point, index) => ({ x: 5 + (index / divisor) * 290, y: 92 - ((point.cumulative_total_tokens || 0) / maximum) * 78 }));
});
const chartPath = computed(() => chart.value.map((point) => `${point.x},${point.y}`).join(" "));

function escapeHtml(value) { const el = document.createElement("div"); el.textContent = value || ""; return el.innerHTML; }
function messageHtml(message) { return message.role === "assistant" && message.answer_html ? message.answer_html : escapeHtml(message.content).replaceAll("\n", "<br>"); }
function errorText(payload) {
  if (typeof payload?.detail === "string") return payload.detail;
  if (typeof payload?.detail?.message === "string") return payload.detail.message;
  return "Не удалось выполнить действие.";
}
function errorDetailsFrom(payload, status) {
  const detail = payload?.detail;
  return typeof detail === "object" && detail !== null
    ? { http_status: status, ...detail }
    : { http_status: status, message: errorText(payload) };
}
function showError(requestError) { error.value = requestError.message; errorDetails.value = requestError.details || null; }
function compact(value) { return typeof value === "number" ? new Intl.NumberFormat("ru-RU").format(value) : "—"; }
function money(value) { return typeof value === "number" ? `$${value < 0.0001 && value > 0 ? value.toFixed(8) : value.toFixed(5)}` : "не удалось вычислить"; }
function controlsForApi() { return { ...controls.value, stop_sequences: controls.value.stop_sequences.map((item) => item.trim()).filter(Boolean) }; }

async function api(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const requestError = new Error(errorText(payload));
    requestError.details = errorDetailsFrom(payload, response.status);
    throw requestError;
  }
  return payload;
}

async function selectConversation(id) {
  const result = await api(`/api/conversations/${id}`);
  activeConversation.value = result.conversation;
  stats.value = result.stats;
  status.value = "Ожидает вопрос";
  syntheticCopies.value = 1;
}
async function loadConversations(preferredId) {
  const result = await api("/api/conversations");
  conversations.value = result.conversations;
  const selected = conversations.value.find((item) => item.id === preferredId) || conversations.value[0];
  if (selected) await selectConversation(selected.id);
}
async function createConversation() {
  try { const result = await api("/api/conversations", { method: "POST" }); await loadConversations(result.conversation.id); }
  catch (requestError) { showError(requestError); }
}
async function deleteConversation(conversation) {
  if (!conversation || !window.confirm("Удалить диалог и всю его историю из SQLite?")) return;
  try {
    const result = await api(`/api/conversations/${conversation.id}`, { method: "DELETE" });
    sessionMenuId.value = null;
    conversations.value = result.conversations;
    await selectConversation(result.active.id);
  } catch (requestError) { showError(requestError); }
}
function openRename(conversation) { sessionMenuId.value = null; rename.value = { id: conversation.id, title: conversation.title }; }
async function saveRename() {
  try {
    const result = await api(`/api/conversations/${rename.value.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: rename.value.title }) });
    rename.value = null;
    if (activeConversation.value?.id === result.conversation.id) activeConversation.value = result.conversation;
    await loadConversations(activeConversation.value?.id);
  } catch (requestError) { showError(requestError); }
}
async function requestAnswer(text) {
    const result = await api("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ conversation_id: activeConversation.value.id, prompt: text, controls: controlsForApi(), synthetic_copies: syntheticCopies.value }) });
    activeConversation.value = result.conversation; stats.value = result.stats;
    await nextTick();
    threadElement.value?.scrollTo({ top: threadElement.value.scrollHeight, behavior: "smooth" });
    const list = await api("/api/conversations"); conversations.value = list.conversations;
}
async function send() {
  if (!prompt.value.trim()) { error.value = "Введите сообщение."; errorDetails.value = null; return; }
  if (sending.value || !activeConversation.value) return;
  const text = prompt.value;
  sending.value = true; error.value = ""; errorDetails.value = null; status.value = "Агент отвечает…";
  try {
    await requestAnswer(text); prompt.value = ""; syntheticCopies.value = 1; status.value = "Готово";
  } catch (requestError) { showError(requestError); status.value = "Ошибка"; }
  finally { sending.value = false; }
}
function keydown(event) { if (event.key === "Enter" && !event.shiftKey && !event.metaKey) { event.preventDefault(); send(); } }
function addStop() { if (controls.value.stop_sequences.length < MAX_STOPS) controls.value.stop_sequences.push(""); }
function removeStop(index) { controls.value.stop_sequences.length === 1 ? controls.value.stop_sequences[0] = "" : controls.value.stop_sequences.splice(index, 1); }
function providerChanged() { controls.value.model = defaults[controls.value.provider]; if (!thinkingAvailable.value) controls.value.thinking_mode = "disabled"; loadModelOptions(); }
async function loadMetadata() {
  metadataLoading.value = true;
  try { metadata.value = await api(`/api/model-metadata?provider=${encodeURIComponent(controls.value.provider)}&model=${encodeURIComponent(controls.value.model)}`); }
  catch (requestError) { metadata.value = { source: requestError.message, price_status: "unknown" }; }
  finally { metadataLoading.value = false; }
}
async function loadModelOptions() {
  try {
    const result = await api(`/api/model-options?provider=${encodeURIComponent(controls.value.provider)}`);
    modelOptions.value = result.models;
    if (!modelOptions.value.some((item) => item.id === controls.value.model)) controls.value.model = modelOptions.value[0]?.id || defaults[controls.value.provider];
  } catch (requestError) {
    modelOptions.value = [{ id: defaults[controls.value.provider], label: `${defaults[controls.value.provider]} · лимит неизвестен` }];
  }
}
function doubleSyntheticContext() { if (canDoubleSyntheticContext.value) syntheticCopies.value *= 2; }
function resetSyntheticContext() { syntheticCopies.value = 1; }
function scenario(text) { prompt.value = text; document.querySelector(".composer textarea")?.focus(); }
function applyPreset(preset) { presetsOpen.value = false; scenario(preset.prompt); }

watch(() => [controls.value.provider, controls.value.model], loadMetadata);
onMounted(async () => { try { await loadConversations(); await loadModelOptions(); await loadMetadata(); } catch (requestError) { showError(requestError); status.value = "Ошибка"; } });
</script>

<template>
  <div class="app-shell" :class="{ 'sidebar-closed': !sidebarOpen }">
    <aside class="session-sidebar" aria-label="Диалоги">
      <header class="sidebar-head">
        <div class="brand"><p class="eyebrow">ДИАЛОГИ</p><h1>Token<br />Lab</h1></div>
        <button class="collapse-button" :aria-label="sidebarOpen ? 'Свернуть диалоги' : 'Развернуть диалоги'" :aria-expanded="sidebarOpen" @click="sidebarOpen = !sidebarOpen">{{ sidebarOpen ? '‹' : '›' }}</button>
      </header>
      <div class="sidebar-content">
        <button class="new-chat" :disabled="sending" @click="createConversation">＋ Новый диалог</button>
        <nav class="conversation-list">
          <div v-for="conversation in conversations" :key="conversation.id" class="session-row" :class="{ active: conversation.id === activeConversation?.id }">
            <button class="conversation" :disabled="sending" @click="selectConversation(conversation.id)"><span class="dot"></span><span><b>{{ conversation.title }}</b><small>{{ conversation.answer_count || 0 }} ответов</small></span></button>
            <button class="menu-button" :disabled="sending" @click="sessionMenuId = sessionMenuId === conversation.id ? null : conversation.id">⋯</button>
            <div v-if="sessionMenuId === conversation.id" class="conversation-menu"><button @click="openRename(conversation)">✎ Переименовать</button><button class="danger-text" @click="deleteConversation(conversation)">Удалить</button></div>
          </div>
        </nav>
      </div>
      <footer><span>● AI CHALLENGE 9 · DAY 08</span><strong>Работа с токенами</strong><small>FastAPI · Vue · SQLite</small></footer>
    </aside>

    <main class="workspace">
      <header class="page-head">
        <div><p class="eyebrow">ВЫБРАННЫЙ ДИАЛОГ</p><h2>{{ activeConversation?.title || 'Загрузка…' }}</h2></div>
        <div class="head-actions"><span class="status-dot" :class="{ busy: sending, complete: status === 'Готово', 'has-error': Boolean(error) }" :aria-label="`Статус: ${status}`" role="status"></span><span class="status-label">{{ status }}</span><button class="settings" @click="settingsOpen = true">⚙ Настройки</button></div>
      </header>

      <section class="scenario-row"><div class="scenario-copy"><b>Сценарии токен-лаборатории</b><small>Пресеты только подставляют текст. Запрос отправляется только после вашего нажатия.</small></div><div class="scenario-actions"><button :disabled="sending" @click="presetsOpen = true">▦ Пресеты</button><div class="synthetic-controls"><span :title="syntheticCopies > 1 ? `Следующий запрос получит историю, повторённую ×${syntheticCopies}. Дубликаты не сохраняются в SQLite.` : 'Искусственно увеличивает историю только для следующего запроса.'">Искусственный контекст <b>×{{ syntheticCopies }}</b></span><button :disabled="sending || !canDoubleSyntheticContext" title="Удвоить историю для следующего запроса" @click="doubleSyntheticContext">×2</button><button v-if="syntheticCopies > 1" :disabled="sending" @click="resetSyntheticContext">Сбросить</button></div></div></section>

      <section ref="threadElement" class="thread" aria-live="polite">
        <div v-if="!activeMessages.length" class="thread-empty">Начните диалог. После каждого успешного ответа лаборатория покажет только фактические данные из <code>usage</code> API.</div>
        <article v-for="message in activeMessages" :key="message.id" class="message" :class="message.role">
          <p>{{ message.role === 'user' ? 'ВЫ' : 'ASSISTANTAGENT' }}</p><div class="bubble" v-html="messageHtml(message)"></div>
          <div v-if="message.role === 'assistant'" class="message-meta"><span v-if="message.usage?.prompt_tokens != null">контекст: {{ compact(message.usage.prompt_tokens) }}</span><span v-if="message.usage?.completion_tokens != null">ответ: {{ compact(message.usage.completion_tokens) }}</span><span v-if="message.usage?.elapsed_ms != null">{{ message.usage.elapsed_ms }} мс</span><span v-if="message.usage?.finish_reason">{{ message.usage.finish_reason }}</span><button @click="preview = message.request">ⓘ JSON запроса</button></div>
        </article>
      </section>

      <section class="composer"><textarea v-model="prompt" :disabled="sending" placeholder="Введите вопрос…" @keydown="keydown"></textarea><div class="send-row"><span>Enter — отправить · Shift/⌘ + Enter — новая строка</span><button class="send" :disabled="sending" @click="send">{{ sending ? 'Модель отвечает…' : 'Отправить' }}</button></div><p v-if="error" class="request-error"><span>{{ error }}</span><button v-if="errorDetails" @click="errorInfo = errorDetails">ⓘ Подробнее</button></p></section>

      <section class="token-lab" aria-label="Токены и стоимость">
        <div class="lab-heading"><div><p class="eyebrow">TOKEN LAB</p><h3>Фактические данные API</h3></div><div class="answer-summary"><span class="model-chip">{{ latest?.provider || controls.provider }} · {{ latest?.model || controls.model }}</span><small v-if="latest">{{ latest.elapsed_ms }} мс · {{ latest.finish_reason || 'причина не возвращена' }}</small></div></div>
        <div class="token-grid">
          <article><small>Контекст последнего запроса</small><b>{{ compact(latest?.prompt_tokens) }}</b><span>входные токены API</span></article>
          <article><small>Вывод последнего запроса</small><b>{{ compact(latest?.completion_tokens) }}</b><span>токены ответа модели</span></article>
          <article><small>Контекст всего чата</small><b>{{ compact(latest?.total_tokens) }}</b><span>последний контекст + ответ</span></article>
          <article><small>Суммарно обработано</small><b>{{ compact(stats?.totals?.total_tokens) }}</b><span>{{ stats?.turns || 0 }} успешных ходов</span></article>
          <article><small>Суммарная стоимость</small><b class="money">{{ historyMoney }}</b><span>{{ stats?.turns ? (stats?.totals?.has_unknown_cost ? 'есть ходы без цены' : 'по известным usage') : 'после первого успешного ответа' }}</span></article>
        </div>
        <p v-if="latest?.prompt_cache_hit_tokens != null || latest?.prompt_cache_miss_tokens != null || latest?.reasoning_tokens != null" class="usage-details"><span v-if="latest?.prompt_cache_hit_tokens != null">cache hit: {{ compact(latest.prompt_cache_hit_tokens) }}</span><span v-if="latest?.prompt_cache_miss_tokens != null">cache miss: {{ compact(latest.prompt_cache_miss_tokens) }}</span><span v-if="latest?.reasoning_tokens != null">reasoning: {{ compact(latest.reasoning_tokens) }}</span></p>
        <div class="lab-bottom">
          <div class="context-box"><div class="context-line"><span>Контекст последнего вызова</span><b>{{ contextPercent == null ? 'станет известен после ответа' : `${contextPercent.toFixed(3)}%` }}</b></div><div class="progress"><i :style="{ width: `${contextPercent || 0}%` }"></i></div><small>{{ displayedContextLimit ? `${compact(displayedContextLimit)} токенов — опубликованный лимит` : 'Лимит выбранной модели пока неизвестен' }}</small></div>
          <div class="chart-box"><div class="chart-caption"><span>Рост токенов по ходам</span><small>накопленно</small></div><svg viewBox="0 0 300 100" role="img" aria-label="График роста токенов"><path d="M5 92 H295" class="axis"/><polyline v-if="chartPath" :points="chartPath" class="line"/><circle v-for="(point, index) in chart" :key="index" :cx="point.x" :cy="point.y" r="3"/></svg></div>
        </div>
      </section>
    </main>

    <div v-if="settingsOpen" class="backdrop" @click.self="settingsOpen = false"><section class="modal settings-modal"><header><div><p class="eyebrow">ПАРАМЕТРЫ СЛЕДУЮЩЕГО ВЫЗОВА</p><h2>Настройки агента</h2></div><button class="close" @click="settingsOpen = false">×</button></header><div class="settings-grid"><label>Провайдер<select v-model="controls.provider" @change="providerChanged"><option value="deepseek">DeepSeek</option><option value="openrouter">OpenRouter</option></select></label><label>Модель <small v-if="controls.provider === 'openrouter'" class="model-order">от меньшего контекста к большему</small><select v-model="controls.model"><option v-for="model in modelOptions" :key="model.id" :value="model.id">{{ model.label }}</option></select><small class="model-help">{{ controls.model }}</small></label><label>Temperature<input v-model="controls.temperature" :disabled="temperatureDisabled" type="number" min="0" max="2" step="0.1" /></label><label>max_tokens <input v-model="controls.max_tokens" type="number" min="1" placeholder="Не задавать" /></label><label class="wide">Формат ответа<textarea v-model="controls.format_instruction" placeholder="Необязательная инструкция" /></label><label>Thinking mode<select v-model="controls.thinking_mode" :disabled="!thinkingAvailable"><option value="disabled">Выключен</option><option value="enabled">Включен</option></select></label><div class="metadata"><b>{{ metadataLoading ? 'Обновляю метаданные…' : metadata?.source }}</b><small>Контекст: {{ compact(metadata?.context_length) }} · Макс. output: {{ compact(metadata?.max_completion_tokens) }}</small></div></div><div class="stops"><div><p class="eyebrow">STOP SEQUENCES · ДО {{ MAX_STOPS }}</p><button class="add-stop" @click="addStop">＋</button></div><label v-for="(_, index) in controls.stop_sequences" :key="index" class="stop-row"><input v-model="controls.stop_sequences[index]" placeholder="Необязательный стоп-маркер" /><button @click="removeStop(index)">×</button></label></div></section></div>

    <div v-if="preview" class="backdrop" @click.self="preview = null"><section class="modal preview-modal"><header><div><p class="eyebrow">БЕЗ API-КЛЮЧА</p><h2>Фактический JSON-запрос</h2></div><button class="close" @click="preview = null">×</button></header><pre>{{ JSON.stringify(preview, null, 2) }}</pre></section></div>
    <div v-if="errorInfo" class="backdrop" @click.self="errorInfo = null"><section class="modal error-modal"><header><div><p class="eyebrow">ДИАГНОСТИКА ЗАПРОСА</p><h2>Подробности ошибки</h2></div><button class="close" @click="errorInfo = null">×</button></header><p><b>HTTP-статус:</b> {{ errorInfo.http_status || 'не получен' }}</p><p><b>Этап:</b> {{ errorInfo.category || 'клиент или сервер' }}</p><p><b>Сообщение:</b> {{ errorInfo.message }}</p><p v-if="errorInfo.provider_detail"><b>Ответ провайдера:</b> {{ errorInfo.provider_detail }}</p><small>Детали очищаются от API-ключей и не содержат полный запрос.</small></section></div>
    <div v-if="presetsOpen" class="backdrop" @click.self="presetsOpen = false"><section class="modal presets-modal"><header><div><p class="eyebrow">СЦЕНАРИИ DAY 08</p><h2>Пресеты для токен-лаборатории</h2></div><button class="close" @click="presetsOpen = false">×</button></header><p class="preset-intro">Каждый вариант только заполняет поле ввода. Вы сможете отредактировать текст перед отправкой.</p><div class="preset-list"><button v-for="preset in promptPresets" :key="preset.title" class="preset-card" @click="applyPreset(preset)"><b>{{ preset.title }}</b><span>{{ preset.description }}</span></button></div></section></div>
    <div v-if="rename" class="backdrop" @click.self="rename = null"><section class="modal small-modal"><header><h2>Переименовать диалог</h2><button class="close" @click="rename = null">×</button></header><input v-model="rename.title" maxlength="80" @keydown.enter="saveRename" /><div class="modal-actions"><button class="secondary" @click="rename = null">Отмена</button><button class="send" @click="saveRename">Сохранить</button></div></section></div>
  </div>
</template>
