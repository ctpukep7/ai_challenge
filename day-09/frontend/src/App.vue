<script setup>
import { computed, nextTick, onMounted, ref, watch } from "vue";

const experiments = ref([]);
const active = ref(null);
const stats = ref(null);
const prompt = ref("");
const sending = ref(false);
const error = ref(null);
const settingsOpen = ref(false);
const preview = ref(null);
const rename = ref(null);
const chatFeed = ref(null);
const status = ref("Ожидает вопрос");
const batchSize = ref(10);
const keepRecent = ref(6);
const controls = ref({ provider: "deepseek", model: "deepseek-v4-flash", temperature: 0, max_tokens: "", format_instruction: "", thinking_mode: "disabled", stop_sequences: [""] });
const presetCursor = ref(0);
const laboratoryPreset = {
  label: "Реестр лаборатории",
  batchSize: 10,
  keepRecent: 6,
  steps: [
      "Мы проверяем реестр образцов «Арктика». Запомни записи: A-17 — шкаф R1, 1,34 г, зелёная пломба; B-04 — шкаф R2, 0,86 г, красная пломба; C-91 — шкаф R4, 2,10 г, синяя пломба; D-28 — шкаф R3, 1,57 г, жёлтая пломба; E-63 — шкаф R1, 0,42 г, белая пломба; F-55 — шкаф R5, 3,08 г, чёрная пломба. Ответь только: «Принято». ",
      "Продолжи реестр: G-12 — шкаф R2, 1,19 г, серебряная пломба; H-76 — шкаф R4, 0,73 г, оранжевая пломба; J-39 — шкаф R3, 2,64 г, фиолетовая пломба; K-81 — шкаф R5, 1,02 г, бежевая пломба; L-07 — шкаф R1, 0,58 г, розовая пломба; M-44 — шкаф R2, 1,88 г, серая пломба. Ответь только: «Принято». ",
      "Ещё записи: N-26 — шкаф R4, 3,21 г, бирюзовая пломба; P-90 — шкаф R3, 0,95 г, янтарная пломба; Q-14 — шкаф R5, 1,46 г, лиловая пломба; R-68 — шкаф R1, 2,37 г, коричневая пломба; S-03 — шкаф R2, 0,67 г, салатовая пломба; T-52 — шкаф R4, 1,75 г, голубая пломба. Ответь только: «Принято». ",
      "Добавь записи: U-31 — шкаф R3, 2,92 г, золотая пломба; V-85 — шкаф R5, 0,49 г, малиновая пломба; W-09 — шкаф R1, 1,28 г, медная пломба; X-47 — шкаф R2, 3,45 г, лазурная пломба; Y-72 — шкаф R4, 0,81 г, лимонная пломба; Z-16 — шкаф R3, 2,06 г, бордовая пломба. Ответь только: «Принято». ",
      "Запиши продолжение: AA-24 — шкаф R5, 1,63 г, коралловая пломба; AB-70 — шкаф R1, 0,77 г, мятная пломба; AC-11 — шкаф R2, 2,58 г, стальная пломба; AD-49 — шкаф R4, 1,14 г, алая пломба; AE-83 — шкаф R3, 3,17 г, кремовая пломба; AF-06 — шкаф R5, 0,54 г, индиго-пломба. Ответь только: «Принято». ",
      "Добавь записи: AG-35 — шкаф R1, 2,29 г, дымчатая пломба; AH-79 — шкаф R2, 1,05 г, нефритовая пломба; AJ-18 — шкаф R4, 0,69 г, персиковая пломба; AK-62 — шкаф R3, 2,74 г, платиновая пломба; AL-40 — шкаф R5, 1,37 г, охряная пломба; AM-97 — шкаф R1, 3,03 г, сиреневая пломба. Ответь только: «Принято». ",
      "Новые записи: AN-22 — шкаф R2, 0,91 г, вишнёвая пломба; AP-66 — шкаф R4, 1,84 г, песочная пломба; AQ-13 — шкаф R3, 2,41 г, изумрудная пломба; AR-57 — шкаф R5, 0,63 г, стальная пломба; AS-88 — шкаф R1, 1,72 г, синяя пломба; AT-05 — шкаф R2, 2,95 г, медовая пломба. Ответь только: «Принято». ",
      "Последняя партия: AU-34 — шкаф R4, 1,09 г, белая пломба; AV-71 — шкаф R3, 3,28 г, красная пломба; AW-15 — шкаф R5, 0,76 г, зелёная пломба; AX-48 — шкаф R1, 2,53 г, фиолетовая пломба; AY-82 — шкаф R2, 1,31 г, чёрная пломба; AZ-20 — шкаф R4, 0,57 г, жёлтая пломба. Ответь только: «Принято». ",
      "Дополнение реестра: BA-41 — шкаф R2, 1,66 г, лавандовая пломба; BB-08 — шкаф R5, 0,72 г, рубиновая пломба; BC-53 — шкаф R1, 2,83 г, графитовая пломба; BD-19 — шкаф R4, 1,25 г, кремовая пломба; BE-74 — шкаф R3, 3,11 г, синяя пломба; BF-30 — шкаф R2, 0,46 г, оливковая пломба. Ответь только: «Принято». ",
      "Добавь: BG-64 — шкаф R5, 2,07 г, коралловая пломба; BH-02 — шкаф R1, 1,43 г, бронзовая пломба; BJ-87 — шкаф R3, 0,88 г, янтарная пломба; BK-25 — шкаф R4, 2,66 г, белая пломба; BL-60 — шкаф R2, 1,17 г, чёрная пломба; BM-36 — шкаф R5, 3,34 г, мятная пломба. Ответь только: «Принято». ",
      "Добавь: BN-93 — шкаф R1, 0,61 г, малиновая пломба; BP-17 — шкаф R4, 1,96 г, стальная пломба; BQ-58 — шкаф R3, 2,32 г, песочная пломба; BR-04 — шкаф R5, 1,08 г, фиолетовая пломба; BS-69 — шкаф R2, 2,79 г, голубая пломба; BT-12 — шкаф R1, 0,52 г, золотая пломба. Ответь только: «Принято». ",
      "Добавь: BU-80 — шкаф R3, 3,26 г, зелёная пломба; BV-29 — шкаф R2, 1,35 г, серебряная пломба; BW-45 — шкаф R4, 0,84 г, красная пломба; BX-01 — шкаф R5, 2,18 г, сиреневая пломба; BY-76 — шкаф R1, 1,59 г, бежевая пломба; BZ-33 — шкаф R3, 0,97 г, бирюзовая пломба. Ответь только: «Принято». ",
      "Добавь: CA-56 — шкаф R4, 2,47 г, чёрная пломба; CB-10 — шкаф R2, 1,12 г, лимонная пломба; CC-84 — шкаф R5, 3,05 г, алая пломба; CD-21 — шкаф R1, 0,68 г, серебряная пломба; CE-73 — шкаф R3, 1,91 г, медная пломба; CF-38 — шкаф R4, 2,54 г, розовая пломба. Ответь только: «Принято». ",
      "Добавь: CG-65 — шкаф R2, 0,79 г, индиго-пломба; CH-14 — шкаф R5, 2,94 г, белая пломба; CJ-59 — шкаф R1, 1,27 г, охряная пломба; CK-06 — шкаф R3, 3,19 г, лазурная пломба; CL-82 — шкаф R4, 0,55 г, вишнёвая пломба; CM-27 — шкаф R2, 2,14 г, нефритовая пломба. Ответь только: «Принято». ",
      "Добавь: CN-70 — шкаф R5, 1,48 г, коралловая пломба; CP-09 — шкаф R1, 2,71 г, зелёная пломба; CQ-54 — шкаф R3, 0,83 г, серая пломба; CR-16 — шкаф R4, 3,09 г, янтарная пломба; CS-87 — шкаф R2, 1,21 г, розовая пломба; CT-42 — шкаф R5, 2,36 г, стальная пломба.",
      "Добавь: CU-03 — шкаф R1, 0,64 г, фиолетовая пломба; CV-68 — шкаф R2, 1,87 г, белая пломба; CW-31 — шкаф R4, 2,59 г, чёрная пломба; CX-75 — шкаф R3, 1,16 г, медная пломба; CY-20 — шкаф R5, 3,31 г, синяя пломба; CZ-46 — шкаф R1, 0,92 г, золотая пломба.",
      "Добавь: DA-11 — шкаф R3, 2,08 г, бежевая пломба; DB-57 — шкаф R4, 1,44 г, лазурная пломба; DC-94 — шкаф R2, 0,71 г, рубиновая пломба; DD-23 — шкаф R5, 2,85 г, оливковая пломба; DE-79 — шкаф R1, 1,03 г, серебряная пломба; DF-36 — шкаф R3, 3,14 г, лимонная пломба.",
      "Добавь: DG-62 — шкаф R2, 1,57 г, малиновая пломба; DH-05 — шкаф R4, 2,22 г, кремовая пломба; DJ-48 — шкаф R5, 0,69 г, бирюзовая пломба; DK-83 — шкаф R1, 3,02 г, алая пломба; DL-27 — шкаф R3, 1,39 г, нефритовая пломба; DM-91 — шкаф R2, 2,67 г, охряная пломба.",
      "Добавь: DN-14 — шкаф R4, 0,58 г, графитовая пломба; DP-60 — шкаф R1, 1,93 г, песочная пломба; DQ-38 — шкаф R3, 2,45 г, вишнёвая пломба; DR-72 — шкаф R5, 1,07 г, индиго-пломба; DS-26 — шкаф R2, 3,22 г, мятная пломба; DT-85 — шкаф R4, 0,87 г, бронзовая пломба.",
      "Контрольный запрос. Верни таблицу ровно из 8 строк для кодов A-17, H-76, Q-14, X-47, AF-06, AM-97, AS-88 и AZ-20. Для каждого укажи шкаф, массу и цвет пломбы. Не угадывай и не добавляй другие записи."
  ],
};
const laboratorySeals = ["белая", "синяя", "янтарная", "зелёная", "фиолетовая", "чёрная", "серебряная", "коралловая", "нефритовая"];
const laboratoryStatuses = ["готов к анализу", "нужна калибровка", "холодное хранение", "ожидает сверки", "контрольный образец", "допуск ограничен"];

function denseLaboratoryDetails(stepIndex) {
  const records = Array.from({ length: 18 }, (_, row) => {
    const serial = 301 + stepIndex * 18 + row;
    const mass = (((serial * 37) % 361 + 40) / 100).toFixed(2).replace(".", ",");
    const cabinet = `S-${serial % 9 + 1}`;
    const temperature = -18 + serial % 15;
    const seal = laboratorySeals[serial % laboratorySeals.length];
    const status = laboratoryStatuses[(serial + row) % laboratoryStatuses.length];
    return `LX-${serial} — шкаф ${cabinet}, масса ${mass} г, режим ${temperature} °C, ${seal} пломба, статус: ${status}`;
  });
  const address = stepIndex === 0 ? "Обращайся ко мне: «Доктор Д». " : "";
  return `${address}Дополнительный протокол партии: ${records.join("; ")}. Запомни все поля каждой записи.`;
}

const branch = (mode) => active.value?.branches?.[mode] || { messages: [], summary: null, summaries: [] };
const fullMessages = computed(() => branch("full").messages);
const compressedMessages = computed(() => branch("compressed").messages);
const latest = (mode) => stats.value?.[mode]?.latest;
const compact = (value) => value == null ? "—" : new Intl.NumberFormat("ru-RU").format(value);
const money = (value) => value == null ? "—" : `$${Number(value).toFixed(6)}`;
const selectedModelOptions = computed(() => controls.value.provider === "deepseek"
  ? [{ id: "deepseek-v4-flash", label: "DeepSeek V4 Flash" }, { id: "deepseek-v4-pro", label: "DeepSeek V4 Pro" }]
  : [{ id: "openrouter/free", label: "OpenRouter free (маршрутизатор)" }]);

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
  const result = await api(`/api/experiments/${id}`);
  active.value = result.experiment;
  stats.value = result.stats;
  error.value = null;
}
async function newExperiment() {
  const result = await api("/api/experiments", { method: "POST" });
  presetCursor.value = 0;
  await loadList(result.experiment.id);
}
async function deleteExperiment(item = active.value) {
  if (!item || !window.confirm("Удалить пару диалогов и summaries?")) return;
  const result = await api(`/api/experiments/${item.id}`, { method: "DELETE" });
  experiments.value = result.experiments;
  await selectExperiment(result.active.id);
}
function openRename(item = active.value) { if (item) rename.value = { id: item.id, title: item.title }; }
async function saveRename() {
  const result = await api(`/api/experiments/${rename.value.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: rename.value.title }) });
  rename.value = null;
  active.value = result.experiment;
  await loadList(active.value.id);
}
function controlsForApi() {
  return { ...controls.value, stop_sequences: controls.value.stop_sequences.filter(Boolean).map((value) => value.trim()).filter(Boolean) };
}
async function send() {
  const text = prompt.value.trim();
  if (!text || !active.value || sending.value) return;
  sending.value = true; error.value = null; status.value = "Сравнение ответов…";
  try {
    const result = await api("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ experiment_id: active.value.id, prompt: text, controls: controlsForApi(), batch_size: Number(batchSize.value), keep_recent: Number(keepRecent.value) }) });
    active.value = result.experiment; stats.value = result.stats; prompt.value = "";
    const failed = Object.values(result.results).find((item) => item.error);
    if (failed) error.value = failed.error;
    status.value = failed ? "Завершено с ошибкой" : "Готово: два ответа";
    await loadList(active.value.id);
    await nextTick();
    chatFeed.value?.scrollTo({ top: chatFeed.value.scrollHeight, behavior: "smooth" });
  } catch (caught) { error.value = caught; status.value = "Ошибка"; }
  finally { sending.value = false; }
}
function messageById(mode, id) { return branch(mode).messages.find((message) => message.id === id); }
function addStop() { if (controls.value.stop_sequences.length < 16) controls.value.stop_sequences.push(""); }
function removeStop(index) { controls.value.stop_sequences.splice(index, 1); if (!controls.value.stop_sequences.length) controls.value.stop_sequences.push(""); }
function showRequest(message, summary = null) { preview.value = { answer_request: message?.request, summary_request: summary?.request || null }; }
function handleKey(event) { if (event.key === "Enter" && !event.shiftKey && !event.metaKey) { event.preventDefault(); send(); } }
function insertNextPreset() {
  const stepIndex = presetCursor.value;
  const scenario = laboratoryPreset;
  const base = scenario.steps[stepIndex].replace(/ Ответь только: «Принято»\.\s*$/, "");
  prompt.value = stepIndex === scenario.steps.length - 1 ? base : `${base}\n\n${denseLaboratoryDetails(stepIndex)}`;
  presetCursor.value = (stepIndex + 1) % scenario.steps.length;
}
function resetPresetSequence() { presetCursor.value = 0; }
watch(() => controls.value.provider, (provider) => { controls.value.model = provider === "deepseek" ? "deepseek-v4-flash" : "openrouter/free"; });
onMounted(() => loadList().catch((caught) => { error.value = caught; }));
</script>

<template>
  <main class="app-shell">
    <aside class="sidebar">
      <div class="brand"><p>AI CHALLENGE 9 · DAY 09</p><h1>Контекст<br>под контролем</h1></div>
      <button class="new" @click="newExperiment">＋ Новый эксперимент</button>
      <nav class="experiment-list" aria-label="Эксперименты">
        <div v-for="item in experiments" :key="item.id" class="experiment-row" :class="{ active: item.id === active?.id }">
          <button class="experiment" :disabled="sending" @click="selectExperiment(item.id)"><b>{{ item.title }}</b><small>{{ item.turn_count }} сравнений</small></button>
          <button class="dots" :disabled="sending" @click="openRename(item)">✎</button>
          <button class="dots danger" :disabled="sending" @click="deleteExperiment(item)">×</button>
        </div>
      </nav>
      <footer><b>FULL ↔ SUMMARY</b><span>SQLite · Python agent · Vue</span></footer>
    </aside>

    <section class="workspace">
      <header class="page-head">
        <div><p class="eyebrow">ЛАБОРАТОРИЯ СРАВНЕНИЯ</p><h2>{{ active?.title || "Загрузка…" }}</h2><p class="subtitle">Один вопрос идёт в две ветки: полная история и история с rolling-summary.</p></div>
        <div class="head-actions"><span class="status" :class="{ busy: sending, bad: error }"></span>{{ status }}<button class="settings" @click="settingsOpen = true">⚙ Настройки</button></div>
      </header>

      <section class="comparison-summary">
        <article><small>Экономия входа в последнем ходе</small><b>{{ compact(stats?.latest_saved_prompt_tokens) }}</b><span>{{ stats?.latest_saved_percent == null ? "появится после пары ответов" : `${stats.latest_saved_percent}% меньше токенов` }}</span></article>
        <article><small>Полный режим — всего</small><b>{{ compact(stats?.full?.overall?.total_tokens) }}</b><span>{{ money(stats?.full?.overall?.cost_usd) }}</span></article>
        <article><small>Сжатый режим — всего</small><b>{{ compact(stats?.compressed?.overall?.total_tokens) }}</b><span>включая summary: {{ money(stats?.compressed?.summary?.cost_usd) }}</span></article>
        <article><small>Сжатая ветка</small><b>{{ stats?.compressed?.archived_messages || 0 }} / {{ stats?.compressed?.active_messages || 0 }}</b><span>архивных / последних сообщений</span></article>
      </section>

      <section class="comparison-grid">
        <article class="branch-card full"><header><div><p class="eyebrow">РЕЖИМ A</p><h3>Полный контекст</h3></div><span>вся история</span></header><p class="branch-copy">Каждый запрос получает все прежние сообщения диалога.</p><div class="metrics"><span>Вход <b>{{ compact(latest('full')?.prompt_tokens) }}</b></span><span>Выход <b>{{ compact(latest('full')?.completion_tokens) }}</b></span><span>{{ latest('full')?.elapsed_ms || "—" }} мс</span></div></article>
        <article class="branch-card compressed"><header><div><p class="eyebrow">РЕЖИМ B</p><h3>Сжатый контекст</h3></div><span>summary + последние N</span></header><p class="branch-copy">Архив заменяется одним LLM-summary и больше не уходит в API.</p><div class="metrics"><span>Вход <b>{{ compact(latest('compressed')?.prompt_tokens) }}</b></span><span>Выход <b>{{ compact(latest('compressed')?.completion_tokens) }}</b></span><span>{{ latest('compressed')?.elapsed_ms || "—" }} мс</span></div><button v-if="branch('compressed').summaries?.length" class="summary-button" @click="preview = { summaries: branch('compressed').summaries }">▤ Все summary ({{ branch('compressed').summaries.length }})</button></article>
      </section>

      <section class="chat-zone">
      <section v-if="active?.turns?.length" ref="chatFeed" class="turns">
        <article v-for="turn in active.turns" :key="turn.id" class="turn-card"><p class="turn-prompt">{{ turn.prompt }}</p><div class="answers"><div><small>ПОЛНЫЙ КОНТЕКСТ</small><div v-if="messageById('full', turn.full_assistant_id)" class="answer" v-html="messageById('full', turn.full_assistant_id).answer_html"></div><p v-else class="failed">Ответ не получен.</p><button v-if="messageById('full', turn.full_assistant_id)" @click="showRequest(messageById('full', turn.full_assistant_id))">ⓘ JSON</button></div><div><small>СЖАТЫЙ КОНТЕКСТ</small><div v-if="messageById('compressed', turn.compressed_assistant_id)" class="answer" v-html="messageById('compressed', turn.compressed_assistant_id).answer_html"></div><p v-else class="failed">Ответ не получен.</p><button v-if="messageById('compressed', turn.compressed_assistant_id)" @click="showRequest(messageById('compressed', turn.compressed_assistant_id), branch('compressed').summary)">ⓘ JSON + summary</button></div></div></article>
      </section>
      <section v-else class="empty">Напишите первый вопрос. Он будет отправлен обеим веткам с одинаковыми настройками.</section>

      <form class="composer" @submit.prevent="send"><textarea v-model="prompt" :disabled="sending" placeholder="Введите вопрос для двух диалогов…" @keydown="handleKey"></textarea><div class="composer-actions"><div class="preset-actions"><button class="preset-button" type="button" :disabled="sending" @click="insertNextPreset">▦ Шаг {{ presetCursor + 1 }} / {{ laboratoryPreset.steps.length }}</button><button class="reset-presets" type="button" :disabled="sending || presetCursor === 0" title="Вернуть первый шаг" @click="resetPresetSequence">↺</button><small>Сжатие: {{ batchSize }} / {{ keepRecent }} · Enter — отправить</small></div><button class="send" :disabled="sending || !prompt.trim()">{{ sending ? "Модель отвечает…" : "Сравнить ответы →" }}</button></div><p v-if="error" class="error">{{ error.message }} <button v-if="error.detail" type="button" @click="preview = { error }">Подробности</button></p></form>
      </section>
    </section>

    <div v-if="settingsOpen" class="backdrop" @click.self="settingsOpen = false"><section class="modal"><header><div><p class="eyebrow">ОБЩИЕ НАСТРОЙКИ</p><h2>Одинаковые для двух веток</h2></div><button class="close" @click="settingsOpen = false">×</button></header><div class="form-grid"><label>Провайдер<select v-model="controls.provider"><option value="deepseek">DeepSeek</option><option value="openrouter">OpenRouter</option></select></label><label>Модель<select v-model="controls.model"><option v-for="model in selectedModelOptions" :key="model.id" :value="model.id">{{ model.label }}</option></select></label><label>temperature<input v-model="controls.temperature" type="number" min="0" max="2" step="0.1"></label><label>max_tokens<input v-model="controls.max_tokens" type="number" min="1" placeholder="Не задавать"></label><label>Сжимать каждые сообщения<input v-model.number="batchSize" type="number" min="2" max="100" step="2"></label><label>Оставлять последних как есть<input v-model.number="keepRecent" type="number" min="2" max="100" step="2"></label><label class="wide">Формат ответа<textarea v-model="controls.format_instruction" placeholder="Необязательная инструкция"></textarea></label><label>Thinking mode<select v-model="controls.thinking_mode" :disabled="controls.provider !== 'deepseek'"><option value="disabled">Выключен</option><option value="enabled">Включен</option></select></label></div><p class="compression-note">Сжатие создаётся после успешного ответа: старые сообщения заменяются summary, последние N остаются без изменений.</p><div class="stops"><p class="eyebrow">STOP SEQUENCES</p><label v-for="(_, index) in controls.stop_sequences" :key="index"><input v-model="controls.stop_sequences[index]" placeholder="Необязательный стоп-маркер"><button @click="removeStop(index)">×</button></label><button class="add" :disabled="controls.stop_sequences.length >= 16" @click="addStop">＋ Добавить маркер</button></div></section></div>
    <div v-if="preview" class="backdrop" @click.self="preview = null"><section class="modal preview"><header><div><p class="eyebrow">БЕЗ API-КЛЮЧА</p><h2>{{ preview.summaries ? "История summary" : preview.error ? "Ошибка запроса" : "Фактический JSON" }}</h2></div><button class="close" @click="preview = null">×</button></header><section v-if="preview.summaries" class="summary-history"><article v-for="(summary, index) in preview.summaries" :key="summary.id"><p>SUMMARY {{ index + 1 }} · архив до сообщения #{{ summary.through_message_id }}</p><div v-if="summary.summary_html" class="summary-text" v-html="summary.summary_html"></div><div v-else class="summary-text">{{ summary.content }}</div></article></section><p v-if="preview.error">{{ preview.error.message }}<br><small>{{ preview.error.detail?.provider_detail }}</small></p><pre v-if="preview.answer_request">{{ JSON.stringify(preview.answer_request, null, 2) }}</pre><pre v-if="preview.summary_request">{{ JSON.stringify(preview.summary_request, null, 2) }}</pre></section></div>
    <div v-if="rename" class="backdrop" @click.self="rename = null"><section class="modal small"><header><h2>Переименовать эксперимент</h2><button class="close" @click="rename = null">×</button></header><input v-model="rename.title" maxlength="80" @keydown.enter="saveRename"><button class="send" @click="saveRename">Сохранить</button></section></div>
  </main>
</template>
