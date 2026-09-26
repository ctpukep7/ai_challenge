<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { api } from './api'
import StageDetails from './StageDetails.vue'

const storageKey = 'day19-pending-request'
const state = ref(null)
const selectedId = ref('')
const prompt = ref('')
const promptElement = ref(null)
const loading = ref(true)
const refreshing = ref(false)
const submitting = ref(false)
const connectionError = ref('')
const actionError = ref('')
const pending = ref(null)
const acceptedRunning = ref(false)
const activeDetails = ref('')
let disposed = false
let timer
let revision = 0

const stages = [
  { name: 'search', title: 'Найти источники', description: 'Поиск Tavily · до 3 источников' },
  { name: 'summarize', title: 'Собрать сводку', description: 'DeepSeek · из найденного текста' },
  { name: 'saveToFile', title: 'Сохранить файл', description: 'Markdown · точный текст сводки' },
]
const examples = [
  { label: 'MCP', prompt: 'Найди, что такое Model Context Protocol (MCP), кратко объясни его назначение и сохрани сводку с источниками.' },
  { label: 'Память агента', prompt: 'Найди основные виды памяти AI-агента, сравни их назначение и сохрани краткую сводку с источниками.' },
  { label: 'Векторные базы', prompt: 'Найди, как векторные базы данных используются в RAG, кратко объясни процесс и сохрани сводку с источниками.' },
]
const names = { pending: 'Ожидает', running: 'Выполняется', completed: 'Готово', failed: 'Ошибка', interrupted: 'Прервано' }
const runs = computed(() => state.value?.runs || [])
const selected = computed(() => runs.value.find(run => run.run_id === selectedId.value) || null)
const configuration = computed(() => state.value?.configuration)
const configured = computed(() => configuration.value?.deepseek && configuration.value?.tavily)
const running = computed(() => acceptedRunning.value || state.value?.busy || runs.value.some(run => run.status === 'running'))
const locked = computed(() => loading.value || !state.value || submitting.value || running.value)
const canStart = computed(() => !locked.value && configured.value && prompt.value.trim().length > 0)
const sources = computed(() => selected.value?.search_result?.sources || [])
const summary = computed(() => selected.value?.summary_result?.markdown || '')
const file = computed(() => selected.value?.file_result || null)
const completedCount = computed(() => selected.value?.steps?.filter(step => step.status === 'completed').length || 0)
const missing = computed(() => [!configuration.value?.deepseek && 'DEEPSEEK_API_KEY', !configuration.value?.tavily && 'TAVILY_API_KEY'].filter(Boolean))
const download = computed(() => {
  if (!selected.value || selected.value.status !== 'completed' || !file.value) return ''
  return `/api/runs/${encodeURIComponent(selected.value.run_id)}/download`
})

function status(value) { return names[value] || value || 'Ожидает' }
function step(name) { return selected.value?.steps?.find(item => item.name === name) }
function toggleDetails(name) { activeDetails.value = activeDetails.value === name ? '' : name }
function stamp(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }).format(date)
}
function duration(value) { return typeof value === 'number' ? `${(value / 1000).toFixed(1)} с` : '' }
function json(value) { return JSON.stringify(value, null, 2) }
function sourceUrl(value) {
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : '' } catch { return '' }
}
function sourceHost(value) { try { return new URL(value).hostname } catch { return 'Источник' } }
function savePending(value) {
  pending.value = value
  try {
    if (value) sessionStorage.setItem(storageKey, JSON.stringify(value))
    else sessionStorage.removeItem(storageKey)
  } catch { /* Storage may be unavailable; the current tab still preserves the UUID. */ }
}
function schedulePoll() {
  clearTimeout(timer)
  if (!disposed && running.value) timer = setTimeout(refreshState, 1000)
}
async function refreshState() {
  if (disposed || refreshing.value) return
  refreshing.value = true
  const generation = revision
  try {
    const value = await api('/api/state')
    if (disposed || generation !== revision) return
    state.value = value
    const recovered = pending.value && value.runs.find(run => run.request_id === pending.value.request_id)
    if (recovered) {
      selectedId.value = recovered.run_id
      savePending(null)
      actionError.value = ''
    }
    if (!selectedId.value || !value.runs.some(run => run.run_id === selectedId.value)) {
      selectedId.value = value.runs.find(run => run.status === 'running')?.run_id || value.runs[0]?.run_id || ''
    }
    // POST has acknowledged a run; state now supplies its actual terminal status.
    acceptedRunning.value = false
    connectionError.value = ''
  } catch (error) {
    if (!disposed && generation === revision) connectionError.value = error.message
  } finally {
    refreshing.value = false
    loading.value = false
    schedulePoll()
  }
}
async function fillExample(example) {
  if (locked.value || pending.value) return
  prompt.value = example.prompt
  await nextTick()
  promptElement.value?.focus()
}
async function start() {
  if (!canStart.value) return
  const text = prompt.value.trim()
  if (!pending.value) savePending({ prompt: text, request_id: crypto.randomUUID() })
  submitting.value = true
  actionError.value = ''
  revision += 1
  clearTimeout(timer)
  try {
    const result = await api('/api/runs', { method: 'POST', body: JSON.stringify(pending.value) })
    selectedId.value = result.run_id
    acceptedRunning.value = result.status === 'running'
    savePending(null)
  } catch (error) {
    actionError.value = error.message
    if (error.code === 'busy') acceptedRunning.value = true
    // A lost response retains the original UUID and prompt for an idempotent retry.
  } finally {
    submitting.value = false
    revision += 1
    if (refreshing.value) {
      // Allow a stale in-flight GET to finish before fetching the new state.
      timer = setTimeout(refreshState, 1000)
    } else await refreshState()
  }
}
function changePrompt() {
  if (locked.value) return
  savePending(null)
  actionError.value = ''
  promptElement.value?.focus()
}
function selectRun(run) { selectedId.value = run.run_id }
function keyboard(event) {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.isComposing) {
    event.preventDefault()
    start()
  }
}
onMounted(async () => {
  try {
    const stored = JSON.parse(sessionStorage.getItem(storageKey) || 'null')
    if (stored && typeof stored.prompt === 'string' && stored.prompt.length <= 2000 && /^[0-9a-f-]{36}$/i.test(stored.request_id)) {
      pending.value = stored
      prompt.value = stored.prompt
    }
  } catch { /* Ignore invalid or inaccessible session storage. */ }
  await refreshState()
})
onUnmounted(() => { disposed = true; clearTimeout(timer) })
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand"><span class="day-mark">19</span><span><strong>AI Challenge</strong><small>Композиция инструментов MCP</small></span></div>
      <span class="topbar-note">Одно поручение → три инструмента</span>
      <span class="model-label">{{ configuration?.model || 'DeepSeek' }}</span>
    </header>

    <main>
      <div class="page-heading">
        <div><span class="eyebrow">ДЕНЬ 19 / MCP PIPELINE</span><h1>От поиска — к готовому файлу</h1><p>Поручите агенту исследовать тему. Он найдёт источники, соберёт сводку и сохранит её в Markdown.</p></div>
        <span class="heading-note"><b>3</b> последовательных этапа</span>
      </div>

      <div v-if="connectionError" class="notice error" role="alert"><div><strong>Состояние недоступно</strong><p>{{ connectionError }}</p></div><button class="secondary" :disabled="refreshing" @click="refreshState">{{ refreshing ? 'Обновляем…' : 'Обновить состояние' }}</button></div>
      <div v-if="configuration && !configured" class="notice" role="status"><div><strong>Для запуска нужны два ключа API</strong><p>Добавьте {{ missing.join(' и ') }} в локальный .env и перезапустите API. История доступна без ключей.</p></div><span class="config-flags"><span :class="{ ready: configuration.deepseek }">DeepSeek {{ configuration.deepseek ? '✓' : '—' }}</span><span :class="{ ready: configuration.tavily }">Tavily {{ configuration.tavily ? '✓' : '—' }}</span></span></div>

      <div class="workspace">
        <div class="pipeline-workspace">
          <section class="request-panel" aria-labelledby="request-heading">
            <div class="section-heading"><h2 id="request-heading">Ваше поручение</h2><span class="small muted">Поиск → сводка → файл</span></div>
            <form @submit.prevent="start">
              <label class="sr-only" for="prompt">Тема исследования и желаемая сводка</label>
              <textarea id="prompt" ref="promptElement" v-model="prompt" maxlength="2000" :readonly="locked || Boolean(pending)" placeholder="Например: найди, как работает MCP, и сохрани краткую сводку с источниками…" @keydown="keyboard"></textarea>
              <div class="composer-bottom"><span class="small muted">{{ prompt.length }} / 2000 <span class="keyboard-hint">· Ctrl / ⌘ + Enter</span></span><button type="submit" class="primary" :disabled="!canStart"><span v-if="submitting" class="spinner" aria-hidden="true"></span>{{ submitting ? 'Отправляем…' : running ? 'Цепочка выполняется…' : pending ? 'Повторить отправку' : 'Запустить' }}<span v-if="!submitting && !running" aria-hidden="true"> ↗</span></button></div>
            </form>
            <div class="examples"><span>Попробовать тему:</span><button v-for="example in examples" :key="example.label" :disabled="locked || Boolean(pending)" @click="fillExample(example)">{{ example.label }} <span aria-hidden="true">↗</span></button></div>
            <p class="authorization">«Запустить» запускает всю цепочку, включая обращения к DeepSeek и Tavily.</p>
            <div v-if="pending && !running" class="retry-note"><p>Сохранено поручение для повторной отправки с тем же ID. Это защищает от второго запуска при потерянном ответе.</p><button class="text-button" :disabled="locked" @click="changePrompt">Изменить поручение</button></div>
            <p v-if="actionError" class="action-error" role="alert">{{ actionError }}</p>
            <p v-if="running && selected?.status !== 'running'" class="busy-note" role="status">Другой запуск выполняется. Новое поручение можно отправить после его завершения.</p>
          </section>

          <section class="execution" aria-labelledby="execution-heading">
            <div class="section-heading"><h2 id="execution-heading">Цепочка инструментов</h2><span class="small muted" aria-live="polite">{{ selected ? `${completedCount} из 3 · ${status(selected.status)}` : 'Готова к запуску' }}</span></div>
            <ol class="stages">
              <li v-for="(stage, index) in stages" :key="stage.name" class="stage" :class="[step(stage.name)?.status || 'pending', { 'details-active': activeDetails === stage.name }]">
                <div class="stage-top"><span class="step-number" aria-hidden="true">{{ step(stage.name)?.status === 'completed' ? '✓' : index + 1 }}</span><span class="stage-status"><span v-if="step(stage.name)?.status === 'running'" class="spinner" aria-hidden="true"></span>{{ status(step(stage.name)?.status) }}</span></div>
                <h3>{{ stage.title }}</h3><p>{{ stage.description }}</p><div class="stage-bottom"><code>{{ stage.name }}</code><span>{{ duration(step(stage.name)?.duration_ms) }}</span></div>
                <p v-if="step(stage.name)?.error" class="stage-error">{{ step(stage.name).error }}</p>
                <button class="stage-details-button" :aria-expanded="activeDetails === stage.name" aria-controls="stage-details" @click="toggleDetails(stage.name)">JSON и детали <span aria-hidden="true">{{ activeDetails === stage.name ? '−' : '+' }}</span></button>
              </li>
            </ol>
            <StageDetails v-if="activeDetails" :key="`${selected?.run_id || 'empty'}-${activeDetails}`" :stage="stages.find(stage => stage.name === activeDetails)" :run="selected" @close="activeDetails = ''" />

            <div v-if="selected" class="run-caption"><div><span class="eyebrow">ПОРУЧЕНИЕ ЭТОГО ЗАПУСКА</span><p>{{ selected.prompt }}</p></div><time :datetime="selected.created_at">{{ stamp(selected.created_at) }}</time></div>
            <div v-if="selected?.error" class="notice error run-error" role="alert"><div><strong>{{ selected.status === 'interrupted' ? 'Запуск прерван' : 'Цепочка остановилась' }}</strong><p>{{ selected.error }}</p><small v-if="selected.error_code">Код: {{ selected.error_code }}</small></div></div>

            <div v-if="!selected && !loading" class="empty-results"><span class="empty-mark" aria-hidden="true">↳</span><div><h3>Результат появится здесь</h3><p>Сначала — найденные страницы, затем — сводка и готовый файл. Каждый этап передаёт следующему сохранённый результат.</p></div></div>
            <p v-if="loading" class="loading-state" role="status">Загружаем состояние и историю…</p>

            <div v-if="selected" class="results">
              <section class="source-section" aria-labelledby="sources-heading"><div class="section-heading"><h2 id="sources-heading">Источники</h2><span class="small muted">{{ sources.length ? `${sources.length} найдено` : 'Пока нет' }}</span></div><p class="source-note">Фрагменты поисковой выдачи Tavily</p><ol v-if="sources.length" class="sources"><li v-for="(source, index) in sources" :key="`${index}-${source.url}`"><span class="source-number">0{{ index + 1 }}</span><div><span class="source-domain">{{ sourceHost(source.url) }}</span><h3><a v-if="sourceUrl(source.url)" :href="sourceUrl(source.url)" target="_blank" rel="noopener noreferrer">{{ source.title || source.url }} <span aria-hidden="true">↗</span></a><span v-else>{{ source.title || 'Без названия' }}</span></h3><p>{{ source.text?.slice(0, 420) }}{{ source.text?.length > 420 ? '…' : '' }}</p><details v-if="source.text?.length > 420" class="excerpt-details"><summary>Весь фрагмент</summary><p>{{ source.text }}</p></details></div></li></ol><p v-else class="result-placeholder">{{ step('search')?.status === 'running' ? 'Ищем страницы по вашей теме…' : 'Источники появятся после успешного поиска.' }}</p></section>
              <section class="summary-section" aria-labelledby="summary-heading"><div class="section-heading"><h2 id="summary-heading">Сводка</h2><span class="small muted">Markdown</span></div><pre v-if="summary" class="markdown-preview">{{ summary }}</pre><p v-else class="result-placeholder">{{ step('summarize')?.status === 'running' ? 'Собираем сводку из найденных источников…' : 'Сводка появится после обработки источников.' }}</p></section>
            </div>

            <section v-if="file" class="file-receipt" aria-label="Сохранённый файл"><div class="file-icon" aria-hidden="true">MD</div><div class="file-info"><h3>{{ file.filename }}</h3><p>{{ file.bytes }} байт · {{ selected?.status === 'completed' ? 'Файл сохранён на сервере · SHA-256 проверен' : 'Получена квитанция сохранения на сервере' }}</p></div><a v-if="download" class="primary download" :href="download" download>Скачать Markdown <span aria-hidden="true">↓</span></a></section>

            <details v-if="selected" class="mcp-details"><summary>Детали MCP <span>События, идентификаторы и SHA-256</span></summary><div class="details-body"><div class="metadata"><div><span>Run ID</span><code>{{ selected.run_id }}</code></div><div><span>Request ID</span><code>{{ selected.request_id }}</code></div><div v-if="file"><span>SHA-256 файла</span><code>{{ file.sha256 }}</code></div></div><h3>Передача результатов между инструментами</h3><div v-for="stage in stages" :key="stage.name" class="lineage"><h4>{{ stage.name }}</h4><dl><template v-for="key in ['input_id', 'output_id', 'input_sha256', 'output_sha256']" :key="key"><dt>{{ key }}</dt><dd><code>{{ step(stage.name)?.[key] || '—' }}</code></dd></template></dl></div><h3>Фактические события MCP</h3><pre v-if="selected.trace?.length">{{ json(selected.trace) }}</pre><p v-else class="small muted">События инструментов пока не зарегистрированы.</p></div></details>
          </section>
        </div>

        <aside class="history" aria-labelledby="history-heading"><div class="section-heading"><h2 id="history-heading">История</h2><span class="small muted">{{ runs.length }} / 10</span></div><p class="history-intro">Последние запуски сохраняются на сервере.</p><div v-if="!runs.length" class="history-empty"><span aria-hidden="true">◷</span><p>Здесь появятся ваши исследования.</p></div><ol v-else class="history-list"><li v-for="run in runs" :key="run.run_id"><button :class="{ selected: selectedId === run.run_id }" :aria-pressed="selectedId === run.run_id" @click="selectRun(run)"><span class="history-top"><time :datetime="run.created_at">{{ stamp(run.created_at) }}</time><span class="status-dot" :class="run.status" :aria-label="status(run.status)"></span></span><span class="history-prompt">{{ run.prompt }}</span><span class="history-status" :class="run.status">{{ status(run.status) }} <span aria-hidden="true">{{ selectedId === run.run_id ? '←' : '↗' }}</span></span></button></li></ol><button class="text-button refresh-button" :disabled="refreshing" @click="refreshState">{{ refreshing ? 'Обновляем…' : 'Обновить историю' }}</button></aside>
      </div>
    </main>
    <footer><span>AI Challenge · День 19</span><span>Tavily → DeepSeek → Markdown · реальные результаты MCP</span></footer>
  </div>
</template>
