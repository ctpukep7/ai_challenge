<script setup>
import { computed, nextTick, onMounted, ref, watch } from "vue";

const experiments = ref([]);
const active = ref(null);
const stats = ref(null);
const prompt = ref("");
const sending = ref(false);
const error = ref(null);
const status = ref("Ожидает общий вопрос");
const target = ref("all");
const windowSize = ref(6);
const settingsOpen = ref(false);
const preview = ref(null);
const rename = ref(null);
const chatFeed = ref(null);
const cursors = ref({ common: 0, "Ветка A": 0, "Ветка B": 0 });
const controls = ref({
  provider: "deepseek",
  model: "deepseek-v4-flash",
  temperature: 0,
  max_tokens: "",
  format_instruction: "",
  thinking_mode: "disabled",
  stop_sequences: [""],
});

const demo = {
  common: [
    "Мы собираем ТЗ для сервиса бронирования переговорных комнат в офисе. Главная цель — сократить время на поиск свободной комнаты.",
    "Пользователи: сотрудники, офис-менеджер и администратор. Сотрудники создают и отменяют бронирования, администратор управляет комнатами.",
    "Первый релиз нужен через 8 недель. Бюджет разработки ограничен 900 000 ₽.",
    "Обязательные функции: календарь свободных комнат, бронирование по времени, отмена и уведомление за 10 минут до встречи.",
    "Интеграция с корпоративным SSO обязательна. Внешние сервисы аналитики и трекинга подключать нельзя.",
    "Нельзя хранить содержание встреч и персональные заметки. Можно хранить имя сотрудника, время и выбранную комнату.",
    "На первом этапе поддерживаем только веб-версию для десктопа. Интерфейс должен быть светлым, минималистичным и доступным с клавиатуры.",
    "Сформируй краткое ТЗ первого релиза. Обязательно укажи: цель, роли, срок, бюджет, обязательные функции, SSO, запреты на данные, платформу и то, что сознательно не делаем. Не придумывай новые условия.",
  ],
  "Ветка A": [
    "Для ветки A добавь экран администратора: он может закрыть комнату на ремонт и объяснить причину сотрудникам.",
    "В ветке A уведомления должны приходить только в корпоративный email, без мессенджеров.",
    "Сформируй краткое ТЗ ветки A: цель, роли, функции, ограничения и отличия от общего checkpoint.",
  ],
  "Ветка B": [
    "Для ветки B приоритет — сотрудники в мобильном браузере. Нужна адаптивная вёрстка, но нативное приложение не создаём.",
    "В ветке B можно бронировать комнату максимум на 2 часа; администратор не получает отдельный экран ремонта.",
    "Сформируй краткое ТЗ ветки B: цель, роли, функции, ограничения и отличия от общего checkpoint.",
  ],
};

const modelOptions = computed(() => controls.value.provider === "deepseek"
  ? [
      { id: "deepseek-v4-flash", label: "DeepSeek V4 Flash" },
      { id: "deepseek-v4-pro", label: "DeepSeek V4 Pro" },
    ]
  : [{ id: "openrouter/free", label: "OpenRouter free" }]);
const checkpoint = computed(() => active.value?.strategies?.branching?.checkpoint);
const branchData = computed(() => active.value?.strategies?.branching || { threads: [], messages: [] });
const activeBranch = computed(() => branchData.value.threads?.find((thread) => thread.is_active));
const currentFacts = computed(() => active.value?.strategies?.facts?.facts?.values || {});
const turns = computed(() => active.value?.turns || []);
const canCreateCheckpoint = computed(() => !checkpoint.value && branchData.value.messages?.some((item) => item.role === "assistant") && !sending.value);
const compact = (value) => value == null ? "—" : new Intl.NumberFormat("ru-RU").format(value);
const money = (value) => value == null ? "—" : "$" + Number(value).toFixed(6);

function message(id) {
  return id ? active.value?.messages?.[String(id)] : null;
}

function factsSnapshotForAnswer(assistantId) {
  const factsBranch = active.value?.strategies?.facts;
  const messages = factsBranch?.messages || [];
  const index = messages.findIndex((item) => item.id === assistantId);
  const userId = index > 0 ? messages[index - 1].id : null;
  return factsBranch?.facts_history?.find((item) => item.through_user_message_id === userId) || null;
}

function api(path, options = {}) {
  return fetch(path, options).then(async (response) => {
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw { message: body.detail?.message || body.detail || "Ошибка запроса", detail: body.detail };
    return body;
  });
}

async function loadList(preferredId) {
  const result = await api("/api/experiments");
  experiments.value = result.experiments;
  const selected = result.experiments.find((item) => item.id === preferredId) || result.experiments[0];
  if (selected) await selectExperiment(selected.id);
}

async function selectExperiment(id) {
  const result = await api("/api/experiments/" + id);
  active.value = result.experiment;
  stats.value = result.stats;
  error.value = null;
  target.value = checkpoint.value ? "branching" : "all";
}

async function newExperiment() {
  const result = await api("/api/experiments", { method: "POST" });
  cursors.value = { common: 0, "Ветка A": 0, "Ветка B": 0 };
  await loadList(result.experiment.id);
}

async function deleteExperiment(item = active.value) {
  if (!item || !window.confirm("Удалить эксперимент и все его контексты?")) return;
  const result = await api("/api/experiments/" + item.id, { method: "DELETE" });
  experiments.value = result.experiments;
  await selectExperiment(result.active.id);
}

function openRename(item = active.value) {
  if (item) rename.value = { id: item.id, title: item.title };
}

async function saveRename() {
  const result = await api("/api/experiments/" + rename.value.id, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: rename.value.title }),
  });
  rename.value = null;
  active.value = result.experiment;
  await loadList(active.value.id);
}

function controlsForApi() {
  return {
    ...controls.value,
    stop_sequences: controls.value.stop_sequences.filter(Boolean).map((value) => value.trim()).filter(Boolean),
  };
}

async function send() {
  const text = prompt.value.trim();
  if (!text || !active.value || sending.value) return;
  sending.value = true;
  error.value = null;
  status.value = target.value === "all" ? "Три стратегии отвечают…" : "Стратегия отвечает…";
  try {
    const result = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        experiment_id: active.value.id,
        target: target.value,
        prompt: text,
        controls: controlsForApi(),
        window_size: Number(windowSize.value),
      }),
    });
    active.value = result.experiment;
    stats.value = result.stats;
    prompt.value = "";
    const failed = Object.values(result.results).find((item) => item.error || item.facts_error);
    error.value = failed?.error || failed?.facts_error || null;
    status.value = error.value ? "Завершено с предупреждением" : "Готово";
    await loadList(active.value.id);
    await nextTick();
    chatFeed.value?.scrollTo({ top: chatFeed.value.scrollHeight, behavior: "smooth" });
  } catch (caught) {
    error.value = caught;
    status.value = "Ошибка";
  } finally {
    sending.value = false;
  }
}

async function createCheckpoint() {
  if (!active.value || !canCreateCheckpoint.value) return;
  try {
    const result = await api("/api/experiments/" + active.value.id + "/checkpoint", { method: "POST" });
    active.value = result.experiment;
    stats.value = result.stats;
    target.value = "branching";
    status.value = "Checkpoint создан: выберите ветку A или B";
  } catch (caught) {
    error.value = caught;
  }
}

async function selectBranch(threadId) {
  if (!active.value || sending.value) return;
  const result = await api("/api/experiments/" + active.value.id + "/active-branch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId }),
  });
  active.value = result.experiment;
  stats.value = result.stats;
  target.value = "branching";
  await nextTick();
  chatFeed.value?.scrollTo({ top: chatFeed.value.scrollHeight, behavior: "smooth" });
}

function insertPreset() {
  const scope = checkpoint.value ? activeBranch.value?.label : "common";
  if (!scope || !demo[scope]?.length) return;
  const cursor = cursors.value[scope] || 0;
  prompt.value = demo[scope][cursor];
  cursors.value[scope] = (cursor + 1) % demo[scope].length;
}

function addStop() {
  if (controls.value.stop_sequences.length < 16) controls.value.stop_sequences.push("");
}

function removeStop(index) {
  controls.value.stop_sequences.splice(index, 1);
  if (!controls.value.stop_sequences.length) controls.value.stop_sequences.push("");
}

function showJson(item, extra = null) {
  preview.value = { request: item?.request, extra };
}

function handleKey(event) {
  if (event.key === "Enter" && !event.shiftKey && !event.metaKey) {
    event.preventDefault();
    send();
  }
}

watch(() => controls.value.provider, (provider) => {
  controls.value.model = provider === "deepseek" ? "deepseek-v4-flash" : "openrouter/free";
});

onMounted(() => loadList().catch((caught) => { error.value = caught; }));
</script>

<template>
  <main class="app-shell">
    <aside class="sidebar">
      <div class="brand"><p>AI CHALLENGE 9 · DAY 10</p><h1>Контекст<br>по правилам</h1></div>
      <button class="new" @click="newExperiment">＋ Новый эксперимент</button>
      <nav class="experiment-list" aria-label="Эксперименты">
        <div v-for="item in experiments" :key="item.id" class="experiment-row" :class="{ active: item.id === active?.id }">
          <button class="experiment" :disabled="sending" @click="selectExperiment(item.id)"><b>{{ item.title }}</b><small>{{ item.turn_count }} ходов</small></button>
          <button class="dots" :disabled="sending" aria-label="Переименовать" @click="openRename(item)">✎</button>
          <button class="dots danger" :disabled="sending" aria-label="Удалить" @click="deleteExperiment(item)">×</button>
        </div>
      </nav>
      <footer><b>WINDOW · FACTS · BRANCH</b><span>SQLite · Python agent · Vue</span></footer>
    </aside>

    <section class="workspace">
      <header class="page-head">
        <div><p class="eyebrow">ЛАБОРАТОРИЯ СТРАТЕГИЙ</p><h2>{{ active?.title || "Загрузка…" }}</h2><p class="subtitle">Один общий вопрос сопоставляет три способа управлять контекстом без summary.</p></div>
        <div class="head-actions"><span class="status" :class="{ busy: sending, bad: error }"></span>{{ status }}<button class="settings" @click="settingsOpen = true">⚙ Настройки</button></div>
      </header>

      <section class="strategy-grid">
        <article class="strategy-card window"><header><div><p class="eyebrow">СТРАТЕГИЯ 1</p><h3>Sliding Window</h3></div><span>последние N</span></header><p>В API идут только последние {{ windowSize }} сообщений; полная история остаётся в SQLite.</p><div class="metrics"><span>Вход <b>{{ compact(stats?.sliding?.latest?.prompt_tokens) }}</b></span><span>Выход <b>{{ compact(stats?.sliding?.latest?.completion_tokens) }}</b></span><span>{{ stats?.sliding?.latest?.elapsed_ms || "—" }} мс</span></div></article>
        <article class="strategy-card facts"><header><div><p class="eyebrow">СТРАТЕГИЯ 2</p><h3>Sticky Facts</h3></div><span>key-value</span></header><p>Facts + последние {{ windowSize }} сообщений. Обновление facts учитывается в стоимости.</p><div class="metrics"><span>Вход <b>{{ compact(stats?.facts?.latest?.prompt_tokens) }}</b></span><span>Выход <b>{{ compact(stats?.facts?.latest?.completion_tokens) }}</b></span><span>facts <b>{{ compact(stats?.facts?.facts?.total_tokens) }}</b></span></div><button v-if="active?.strategies?.facts?.facts" class="info-button" @click="preview = { facts: currentFacts, factsHistory: active.strategies.facts.facts_history }">▤ Facts</button></article>
        <article class="strategy-card branching"><header><div><p class="eyebrow">СТРАТЕГИЯ 3</p><h3>Branching</h3></div><span>{{ checkpoint ? "fork создан" : "корень" }}</span></header><p v-if="!checkpoint">Создайте checkpoint после общего контекста, чтобы открыть независимые A и B.</p><div v-else class="branch-tabs"><button v-for="thread in branchData.threads" :key="thread.id" :class="{ selected: thread.is_active }" :disabled="sending" @click="selectBranch(thread.id)">{{ thread.label }}</button></div><div class="metrics"><span>Вход <b>{{ compact(stats?.branching?.latest?.prompt_tokens) }}</b></span><span>Выход <b>{{ compact(stats?.branching?.latest?.completion_tokens) }}</b></span><span>{{ stats?.branching?.latest?.elapsed_ms || "—" }} мс</span></div><button v-if="!checkpoint" class="checkpoint" :disabled="!canCreateCheckpoint" @click="createCheckpoint">⌘ Создать checkpoint</button></article>
      </section>

      <section class="stats-panel">
        <article v-for="name in ['sliding', 'facts', 'branching']" :key="name"><small>{{ name === 'sliding' ? 'WINDOW' : name === 'facts' ? 'FACTS' : 'BRANCH' }} — всего</small><b>{{ compact(stats?.[name]?.overall?.total_tokens) }}</b><span>{{ money(stats?.[name]?.overall?.cost_usd) }}</span></article>
      </section>

      <section class="chat-zone">
        <section v-if="turns.length" ref="chatFeed" class="turns">
          <article v-for="turn in turns" :key="turn.id" class="turn-card">
            <p class="turn-prompt">{{ turn.prompt }}</p>
            <div class="answers">
              <div><small>SLIDING WINDOW</small><div v-if="message(turn.sliding_assistant_id)" class="answer" v-html="message(turn.sliding_assistant_id).answer_html"></div><p v-else class="muted">Не запускался.</p><button v-if="message(turn.sliding_assistant_id)" @click="showJson(message(turn.sliding_assistant_id))">ⓘ JSON</button></div>
              <div><small>STICKY FACTS</small><div v-if="message(turn.facts_assistant_id)" class="answer" v-html="message(turn.facts_assistant_id).answer_html"></div><p v-else class="muted">Не запускался.</p><button v-if="message(turn.facts_assistant_id)" @click="showJson(message(turn.facts_assistant_id), factsSnapshotForAnswer(turn.facts_assistant_id))">ⓘ JSON + facts</button></div>
              <div><small>BRANCHING {{ turn.branching_thread_id ? '· ' + (branchData.threads.find((item) => item.id === turn.branching_thread_id)?.label || '') : '' }}</small><div v-if="message(turn.branching_assistant_id)" class="answer" v-html="message(turn.branching_assistant_id).answer_html"></div><p v-else class="muted">Не запускался.</p><button v-if="message(turn.branching_assistant_id)" @click="showJson(message(turn.branching_assistant_id))">ⓘ JSON</button></div>
            </div>
          </article>
        </section>
        <section v-else class="empty">Начните общий диалог: один вопрос будет отправлен Sliding Window, Sticky Facts и Branching.</section>

        <form class="composer" @submit.prevent="send">
          <textarea v-model="prompt" :disabled="sending" placeholder="Введите вопрос…" @keydown="handleKey"></textarea>
          <div class="composer-tools">
            <div class="target-row">
              <label>Отправить в</label>
              <select v-model="target" :disabled="sending">
                <option value="all" :disabled="Boolean(checkpoint)">Сравнить все стратегии</option>
                <option value="sliding">Sliding Window</option>
                <option value="facts">Sticky Facts</option>
                <option value="branching">Branching{{ checkpoint ? ' · ' + activeBranch?.label : '' }}</option>
              </select>
              <button class="preset-button" type="button" :disabled="sending" @click="insertPreset">▦ Следующий шаг</button>
              <small v-if="checkpoint">Активна {{ activeBranch?.label }}. Общий режим закрыт после fork.</small>
              <small v-else>Общий сценарий: {{ cursors.common + 1 }} / {{ demo.common.length }} · Enter — отправить</small>
            </div>
            <button class="send" :disabled="sending || !prompt.trim()">{{ sending ? "Модель отвечает…" : "Отправить →" }}</button>
          </div>
          <p v-if="error" class="error">{{ error.message }} <button v-if="error.detail" type="button" @click="preview = { error }">Подробности</button></p>
        </form>
      </section>
    </section>

    <div v-if="settingsOpen" class="backdrop" @click.self="settingsOpen = false"><section class="modal"><header><div><p class="eyebrow">ОБЩИЕ НАСТРОЙКИ</p><h2>Одинаковые для стратегий</h2></div><button class="close" @click="settingsOpen = false">×</button></header><div class="form-grid"><label>Провайдер<select v-model="controls.provider"><option value="deepseek">DeepSeek</option><option value="openrouter">OpenRouter</option></select></label><label>Модель<select v-model="controls.model"><option v-for="model in modelOptions" :key="model.id" :value="model.id">{{ model.label }}</option></select></label><label>Последних N сообщений<input v-model.number="windowSize" type="number" min="2" max="100" step="2"></label><label>temperature<input v-model="controls.temperature" type="number" min="0" max="2" step="0.1"></label><label>max_tokens<input v-model="controls.max_tokens" type="number" min="1" placeholder="Не задавать"></label><label>Thinking mode<select v-model="controls.thinking_mode" :disabled="controls.provider !== 'deepseek'"><option value="disabled">Выключен</option><option value="enabled">Включен</option></select></label><label class="wide">Формат ответа<textarea v-model="controls.format_instruction" placeholder="Необязательная инструкция"></textarea></label></div><div class="stops"><p class="eyebrow">STOP SEQUENCES</p><label v-for="(_, index) in controls.stop_sequences" :key="index"><input v-model="controls.stop_sequences[index]" placeholder="Необязательный стоп-маркер"><button @click="removeStop(index)">×</button></label><button class="add" :disabled="controls.stop_sequences.length >= 16" @click="addStop">＋ Добавить маркер</button></div></section></div>
    <div v-if="preview" class="backdrop" @click.self="preview = null"><section class="modal preview"><header><div><p class="eyebrow">БЕЗ API-КЛЮЧА</p><h2>{{ preview.factsHistory ? 'Sticky Facts' : preview.error ? 'Ошибка запроса' : 'Фактический JSON' }}</h2></div><button class="close" @click="preview = null">×</button></header><section v-if="preview.factsHistory" class="facts-history"><article><p>ТЕКУЩИЕ FACTS</p><pre>{{ JSON.stringify(preview.facts, null, 2) }}</pre></article><article v-for="(snapshot, index) in preview.factsHistory" :key="snapshot.id"><p>ОБНОВЛЕНИЕ {{ index + 1 }}</p><pre>{{ JSON.stringify(snapshot.values, null, 2) }}</pre></article></section><p v-if="preview.error">{{ preview.error.message }}<br><small>{{ preview.error.detail?.provider_detail }}</small></p><pre v-if="preview.extra">{{ JSON.stringify(preview.extra.values || preview.extra, null, 2) }}</pre><pre v-if="preview.extra?.request">{{ JSON.stringify(preview.extra.request, null, 2) }}</pre><pre v-if="preview.request">{{ JSON.stringify(preview.request, null, 2) }}</pre></section></div>
    <div v-if="rename" class="backdrop" @click.self="rename = null"><section class="modal small"><header><h2>Переименовать эксперимент</h2><button class="close" @click="rename = null">×</button></header><input v-model="rename.title" maxlength="80" @keydown.enter="saveRename"><button class="send" @click="saveRename">Сохранить</button></section></div>
  </main>
</template>
