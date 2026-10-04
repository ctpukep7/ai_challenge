<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, scopedApi } from './api.js'
import { runTour, TOUR_STEPS, throwIfAborted } from './demo-tour.js'
import { buildStatus, number, percentage, phaseLabels, score, strategyName } from './format.js'

const state = ref(null)
const job = ref(null)
const workspace = ref('main')
const workspaceChanging = ref(false)
const workspaceError = ref('')
const loading = ref(true)
const connectionError = ref('')
const actionError = ref('')
const activeSource = ref('')
const overlapPercent = ref(18)
const starting = ref(false)
const chunkStrategy = ref('structure')
const chunkSource = ref('')
const chunkPage = ref(1)
const chunks = ref({ items: [], total: 0, page: 1, limit: 6 })
const chunksLoading = ref(false)
const chunksError = ref('')
const query = ref('')
const topK = ref(3)
const searchResults = ref(null)
const searching = ref(false)
const searchError = ref('')
const tourMode = ref('idle')
const tourScene = ref('')
const tourStep = ref(0)
const tourError = ref('')
const pauseActive = ref(false)
const exampleQueries = [
  'Как системный cron и расписания SQLite работают вместе в Day 18?',
  'Что происходит с историей Day 07 при ошибке API?',
  'Как в Day 20 реестр разделяет одинаковые имена MCP-инструментов?',
]
const stages = [
  { key: 'reading', title: 'Чтение', detail: 'Исходный корпус' },
  { key: 'model', title: 'Модель', detail: 'Токенизатор' },
  { key: 'chunking', title: 'Чанки', detail: '2 стратегии' },
  { key: 'embedding', title: 'Эмбеддинги', detail: 'Локальная модель' },
  { key: 'saving', title: 'SQLite', detail: 'Запись и проверка' },
]
const documents = computed(() => state.value?.corpus?.documents || [])
const corpusAvailable = computed(() => state.value?.corpus?.available === true)
const selectedDocument = computed(() => documents.value.find(item => item.source === activeSource.value) || documents.value[0] || null)
const index = computed(() => state.value?.index || null)
const manifest = computed(() => index.value?.manifest || null)
const ready = computed(() => index.value?.ready === true)
const indexFile = computed(() => index.value?.file || (workspace.value === 'demo' ? 'data/demo-index.sqlite' : 'data/index.sqlite'))
const busy = computed(() => starting.value || job.value?.status === 'running')
const indexedVectorCount = computed(() =>
  (index.value?.strategies?.fixed?.count || 0) + (index.value?.strategies?.structure?.count || 0))
const evidenceSource = computed(() => busy.value || job.value?.status === 'failed'
  ? 'ПРЕДЫДУЩИЙ СОХРАНЁННЫЙ ИНДЕКС'
  : 'СОХРАНЁННЫЙ ИНДЕКС')
const progress = computed(() => percentage(job.value?.completed, job.value?.total))
const totalPages = computed(() => Math.max(1, Math.ceil((chunks.value?.total || 0) / (chunks.value?.limit || 6))))
const phaseText = computed(() => phaseLabels[job.value?.phase] || job.value?.phase || buildStatus(job.value?.status))
const tourVisible = computed(() => tourMode.value !== 'idle')
const tourCaption = computed(() => {
  if (tourMode.value === 'error') return `Показ остановлен: ${tourError.value}`
  switch (tourScene.value) {
    case 'preparing': return 'Подготавливаем отдельную демонстрационную базу. Основной индекс остаётся на месте.'
    case 'overview': return `${number(state.value?.corpus?.files)} документа, ${number(state.value?.corpus?.chapters)} разделов, ${number(state.value?.corpus?.words)} слов — около ${number(state.value?.corpus?.pages)} страниц текста.`
    case 'corpus': return 'Корпус собран из отслеживаемых README проекта и дней 01–20. Каждый файл сохраняет путь, заголовок и разделы.'
    case 'document': return 'Открываем исходный документ: видны разделы и фрагмент текста, который попадёт в индекс.'
    case 'empty': return `Первый прогон: ${indexFile.value} отсутствует. Есть документы, но 0 чанков и 0 эмбеддингов. Основной индекс сохранён отдельно.`
    case 'build': return job.value?.status === 'running'
      ? `${phaseText.value}${progress.value === null ? '…' : `: ${number(job.value?.completed)} из ${number(job.value?.total)} (${progress.value} %)`}. Это фактический ход сборки.`
      : 'Сборка завершена: чанки и эмбеддинги проверены и сохранены в локальном SQLite.'
    case 'chunking': return `Разбиение: fixed — ${number(index.value?.strategies?.fixed?.count)} чанков и ${number(index.value?.strategies?.fixed?.crosses)} пересечений разделов; structure — ${number(index.value?.strategies?.structure?.count)} и ${number(index.value?.strategies?.structure?.crosses)}. Лимит ${number(manifest.value?.content_token_limit)} токенов, перекрытие ${number(Math.round((manifest.value?.overlap_ratio || 0) * 100))} %.`
    case 'embeddings': return `Модель ${manifest.value?.model || '—'}: ${number(indexedVectorCount.value)} сохранённых векторов по ${number(manifest.value?.dimension)} измерений.`
    case 'sqlite': return `SQLite: файл ${indexFile.value}, таблицы manifest и chunks. API прочитал ${number(indexedVectorCount.value)} строк чанков из сохранённого индекса.`
    case 'comparison': return `Один текст — два разбиения: fixed ${number(index.value?.strategies?.fixed?.count)} чанков, structure ${number(index.value?.strategies?.structure?.count)}. Сравните пересечения разделов.`
    case 'chunk': return 'Раскрытый чанк показывает текст, source, title, section, chunk_id и размерность сохранённого вектора.'
    case 'search': return 'Один запрос о содержательных именах. Слева реальные результаты fixed, справа — structure; score рассчитан по векторам.'
    case 'search-structure': return 'Те же слова запроса, но чанки выделены по границам разделов. Сравнение поиска — иллюстрация, а не оценка качества.'
    case 'finished': return 'Показ завершён. Вы начали с пустой демонстрационной базы, создали чанки и эмбеддинги, сохранили их в SQLite и проверили поиском. Основной индекс сохранён отдельно.'
    default: return ''
  }
})
let timer
let disposed = false
let polling = false
let chunkRequest = 0
let workspaceEpoch = 0
let lastJobStatus = ''
let tourController = null
let skipPause = null

function phaseGroup(phase) {
  const name = String(phase || '').toLowerCase()
  if (name.includes('read') || name.includes('corpus')) return 'reading'
  if (name.includes('model')) return 'model'
  if (name.includes('chunk') || name.includes('token')) return 'chunking'
  if (name.includes('embed') || name.includes('batch')) return 'embedding'
  if (name.includes('sql') || name.includes('sav') || name.includes('writ') || name.includes('verif')) return 'saving'
  return ''
}
function stageState(key) {
  if (job.value?.status === 'completed') return 'done'
  if (job.value?.status !== 'running') return ''
  const current = phaseGroup(job.value?.phase)
  if (!current) return ''
  const currentIndex = stages.findIndex(stage => stage.key === current)
  const stageIndex = stages.findIndex(stage => stage.key === key)
  return stageIndex < currentIndex ? 'done' : stageIndex === currentIndex ? 'active' : ''
}
function eventText(event) {
  if (typeof event === 'string') return event
  if (!event) return ''
  return event.message || event.label || event.phase || JSON.stringify(event)
}
function rememberState(data) {
  if (disposed) return
  state.value = data
  job.value = data?.build || job.value
  if (!activeSource.value && data?.corpus?.documents?.length) activeSource.value = data.corpus.documents[0].source
  if (data?.index?.manifest?.overlap_ratio != null && !starting.value) {
    overlapPercent.value = Math.round(data.index.manifest.overlap_ratio * 100)
  }
}
function currentRequest(scope, epoch) {
  return !disposed && workspace.value === scope && workspaceEpoch === epoch
}
async function refreshState(scope = workspace.value, epoch = workspaceEpoch) {
  const data = await scopedApi('/api/state', scope)
  if (currentRequest(scope, epoch)) rememberState(data)
  return data
}
async function loadChunks() {
  if (!ready.value) {
    chunks.value = { items: [], total: 0, page: 1, limit: 6 }
    return
  }
  const request = ++chunkRequest
  const scope = workspace.value
  const epoch = workspaceEpoch
  chunksLoading.value = true
  chunksError.value = ''
  const params = new URLSearchParams({ strategy: chunkStrategy.value, page: String(chunkPage.value), limit: '6' })
  if (chunkSource.value) params.set('source', chunkSource.value)
  try {
    const result = await scopedApi(`/api/chunks?${params}`, scope)
    if (currentRequest(scope, epoch) && request === chunkRequest) chunks.value = result
  } catch (error) {
    if (currentRequest(scope, epoch) && request === chunkRequest) chunksError.value = error.message
  } finally {
    if (currentRequest(scope, epoch) && request === chunkRequest) chunksLoading.value = false
  }
}
async function refreshJob() {
  if (polling || disposed || workspaceChanging.value) return
  polling = true
  const scope = workspace.value
  const epoch = workspaceEpoch
  try {
    const result = await scopedApi('/api/build', scope)
    if (!currentRequest(scope, epoch)) return
    const previous = lastJobStatus
    job.value = result
    lastJobStatus = result?.status || ''
    connectionError.value = ''
    if (previous === 'running' && result?.status !== 'running') {
      await refreshState(scope, epoch)
      if (currentRequest(scope, epoch)) await loadChunks()
    }
  } catch (error) {
    if (currentRequest(scope, epoch)) connectionError.value = error.message
  } finally { polling = false }
}
async function initialize() {
  const scope = workspace.value
  const epoch = workspaceEpoch
  loading.value = true
  connectionError.value = ''
  try {
    await refreshState(scope, epoch)
    if (currentRequest(scope, epoch)) {
      lastJobStatus = job.value?.status || ''
      await loadChunks()
    }
  } catch (error) {
    if (currentRequest(scope, epoch)) connectionError.value = error.message
  } finally { if (currentRequest(scope, epoch)) loading.value = false }
}
async function startBuild() {
  if (busy.value || workspaceChanging.value || !corpusAvailable.value) return
  const value = Number(overlapPercent.value)
  if (!Number.isFinite(value) || value < 0 || value > 40) {
    actionError.value = 'Перекрытие должно быть от 0 до 40 %.'
    return
  }
  starting.value = true
  actionError.value = ''
  const scope = workspace.value
  const epoch = workspaceEpoch
  try {
    const result = await scopedApi('/api/build', scope, { method: 'POST', body: JSON.stringify({ overlap_ratio: value / 100 }) })
    if (!currentRequest(scope, epoch)) return
    job.value = { id: result.id, status: 'running', phase: '', completed: null, total: null, events: [] }
    lastJobStatus = 'running'
    await refreshJob()
  } catch (error) {
    if (currentRequest(scope, epoch)) {
      actionError.value = error.message
      await refreshJob()
    }
  } finally { starting.value = false }
}
async function runSearch() {
  const text = query.value.trim()
  if (!ready.value || searching.value || !text) return
  searchError.value = ''
  searchResults.value = null
  searching.value = true
  const scope = workspace.value
  const epoch = workspaceEpoch
  try {
    const result = await scopedApi('/api/search', scope, {
      method: 'POST',
      body: JSON.stringify({ query: text, top_k: Number(topK.value) }),
    })
    if (currentRequest(scope, epoch)) searchResults.value = result
  } catch (error) { if (currentRequest(scope, epoch)) searchError.value = error.message }
  finally { if (currentRequest(scope, epoch)) searching.value = false }
}
function useExample(value) {
  query.value = value
  runSearch()
}
async function changeWorkspace(target, { resetDemo = false, signal } = {}) {
  if (busy.value || workspaceChanging.value) throw new Error('Дождитесь завершения текущей сборки')
  workspaceChanging.value = true
  workspaceError.value = ''
  try {
    if (resetDemo) await api('/api/demo/reset', { method: 'POST' })
    if (signal) throwIfAborted(signal)
    workspaceEpoch += 1
    chunkRequest += 1
    workspace.value = target
    state.value = null
    job.value = null
    lastJobStatus = ''
    chunks.value = { items: [], total: 0, page: 1, limit: 6 }
    chunksLoading.value = false
    chunksError.value = ''
    searchResults.value = null
    searchError.value = ''
    searching.value = false
    actionError.value = ''
    await initialize()
    if (connectionError.value) throw new Error(connectionError.value)
  } finally { workspaceChanging.value = false }
}
async function prepareEmptyDemo() {
  if (tourMode.value !== 'running') stopTour()
  try { await changeWorkspace('demo', { resetDemo: true }) }
  catch (error) { workspaceError.value = error.message }
}
async function returnToMain() {
  if (tourMode.value !== 'running') stopTour()
  try { await changeWorkspace('main') }
  catch (error) { workspaceError.value = error.message }
}
function scrollToDemo(selector) {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  document.querySelector(selector)?.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' })
}
function pauseScene(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException('Показ остановлен', 'AbortError')); return }
    pauseActive.value = true
    const finish = error => {
      clearTimeout(timeout)
      signal.removeEventListener('abort', abort)
      skipPause = null
      pauseActive.value = false
      if (error) reject(error)
      else resolve()
    }
    const abort = () => finish(new DOMException('Показ остановлен', 'AbortError'))
    const timeout = setTimeout(() => finish(), milliseconds)
    skipPause = () => finish()
    signal.addEventListener('abort', abort, { once: true })
  })
}
function waitForBuild(signal) {
  return new Promise((resolve, reject) => {
    let stop = () => {}
    let settled = false
    const finish = error => {
      if (settled) return
      settled = true
      stop()
      signal.removeEventListener('abort', abort)
      if (error) reject(error)
      else resolve()
    }
    const abort = () => finish(new DOMException('Показ остановлен', 'AbortError'))
    const inspect = () => {
      if (signal.aborted) return abort()
      if (connectionError.value) return finish(new Error(connectionError.value))
      if (job.value?.status === 'failed') return finish(new Error(job.value.error || 'Сборка не завершилась'))
      if (job.value?.status === 'completed') return finish()
    }
    stop = watch([job, connectionError], inspect)
    signal.addEventListener('abort', abort, { once: true })
    inspect()
  })
}
async function enterTourScene(id, index, signal) {
  throwIfAborted(signal)
  tourScene.value = id
  tourStep.value = index
  await nextTick()
  switch (id) {
    case 'overview':
      scrollToDemo('#top')
      break
    case 'corpus':
      scrollToDemo('#corpus')
      break
    case 'document':
      activeSource.value = documents.value[0]?.source || ''
      await nextTick()
      scrollToDemo('.source-preview')
      break
    case 'empty': {
      const data = await refreshState()
      throwIfAborted(signal)
      if (data?.index?.exists !== false || data?.index?.ready !== false || indexedVectorCount.value !== 0) {
        throw new Error('Демонстрационный индекс не пуст; первый прогон остановлен')
      }
      scrollToDemo('#empty-index')
      break
    }
    case 'build':
      scrollToDemo('#pipeline')
      if (job.value?.status !== 'running') {
        await startBuild()
        throwIfAborted(signal)
        if (actionError.value && job.value?.status !== 'running') throw new Error(actionError.value)
        if (job.value?.status === 'running') actionError.value = ''
      }
      await waitForBuild(signal)
      throwIfAborted(signal)
      await refreshState()
      await loadChunks()
      if (!ready.value) throw new Error('После сборки индекс недоступен')
      break
    case 'chunking':
    case 'embeddings':
    case 'sqlite':
      if (!ready.value || !manifest.value) throw new Error('После сборки данные индекса недоступны')
      scrollToDemo(`#evidence-${id}`)
      break
    case 'comparison':
      if (!ready.value) throw new Error('Индекс недоступен для сравнения')
      scrollToDemo('#compare')
      break
    case 'chunk': {
      chunkStrategy.value = 'structure'
      chunkSource.value = ''
      chunkPage.value = 1
      await nextTick()
      await loadChunks()
      throwIfAborted(signal)
      if (chunksError.value) throw new Error(chunksError.value)
      if (!chunks.value.items?.length) throw new Error('В индексе нет чанков для показа')
      await nextTick()
      const detail = document.querySelector('.chunk-item')
      if (!detail) throw new Error('Не удалось раскрыть чанк')
      detail.open = true
      await nextTick()
      scrollToDemo(window.innerWidth <= 620 ? '.chunk-detail dl' : '#explore')
      break
    }
    case 'search':
      query.value = exampleQueries[0]
      topK.value = 3
      scrollToDemo('#search')
      await runSearch()
      throwIfAborted(signal)
      if (searchError.value) throw new Error(searchError.value)
      if (!searchResults.value?.results?.fixed?.length || !searchResults.value?.results?.structure?.length) {
        throw new Error('Поиск не вернул результаты обеих стратегий')
      }
      await nextTick()
      scrollToDemo('.results-grid')
      break
    case 'search-structure':
      scrollToDemo('.result-column.structure')
      break
    case 'finished':
      scrollToDemo('#search')
      break
  }
}
async function startTour() {
  if (!state.value || !corpusAvailable.value || loading.value || busy.value || workspaceChanging.value || tourMode.value === 'running') return
  tourController?.abort()
  const controller = new AbortController()
  tourController = controller
  tourError.value = ''
  tourMode.value = 'running'
  tourScene.value = 'preparing'
  tourStep.value = 0
  try {
    await changeWorkspace('demo', { resetDemo: true, signal: controller.signal })
    throwIfAborted(controller.signal)
    await runTour({ enter: enterTourScene, pause: pauseScene }, controller.signal)
    if (!controller.signal.aborted) tourMode.value = 'finished'
  } catch (error) {
    if (!controller.signal.aborted) {
      tourError.value = error.message || 'Неизвестная ошибка'
      tourMode.value = 'error'
    }
  } finally {
    if (tourController === controller) tourController = null
  }
}
function stopTour() {
  tourController?.abort()
  tourController = null
  tourMode.value = 'idle'
  tourScene.value = ''
  tourStep.value = 0
  tourError.value = ''
}
watch([chunkStrategy, chunkSource], () => { chunkPage.value = 1; loadChunks() })
watch(chunkPage, () => loadChunks())
onMounted(() => {
  initialize()
  timer = setInterval(refreshJob, 1500)
})
onUnmounted(() => { stopTour(); disposed = true; clearInterval(timer); chunkRequest += 1 })
</script>

<template>
  <div class="app-shell" :class="{ 'tour-active': tourVisible }">
    <header class="topbar">
      <div class="topbar-inner">
        <a class="brand" href="#top" aria-label="День 21, к началу страницы"><span class="day-mark">21</span><span><strong>AI CHALLENGE</strong><small>ПРАКТИКА RAG</small></span></a>
        <nav aria-label="Разделы страницы"><a href="#corpus">Корпус</a><a href="#pipeline">Пайплайн</a><a href="#compare">Сравнение</a><a href="#explore">Чанки</a><a href="#search">Поиск</a></nav>
        <div class="header-status" :class="ready ? 'ready' : ''"><span class="status-dot"></span>{{ workspace === 'demo' ? 'Демо · ' : 'Основной · ' }}{{ !corpusAvailable ? 'нет корпуса' : ready ? 'индекс готов' : 'нет индекса' }}</div>
      </div>
    </header>

    <main id="top">
      <div v-if="connectionError" class="notice error" role="alert"><strong>Нет связи с локальным API.</strong><span>{{ connectionError }}</span><button class="text-button" type="button" @click="initialize">Повторить</button><button v-if="workspace === 'demo'" class="text-button" type="button" :disabled="workspaceChanging" @click="returnToMain">Вернуться к основному индексу</button></div>
      <div v-if="loading && !state" class="initial-state" role="status"><span class="spinner"></span> Загружаем данные корпуса и индекса…</div>

      <template v-if="state">
        <section class="hero" aria-labelledby="page-title">
          <div class="hero-copy"><span class="eyebrow">НЕДЕЛЯ RAG · ДЕНЬ 21</span><h1 id="page-title">Индексация документов</h1><p>От исходного текста до локального поискового индекса. Наблюдайте каждый этап, сравнивайте разбиение и изучайте сохранённые чанки.</p></div>
          <div class="hero-aside"><span class="hero-aside-label">ИСТОЧНИК КОРПУСА</span><strong>README проекта<br />AI Challenge</strong><span>Отслеживаемые файлы · дни 01–20</span><a href="#corpus">Посмотреть разделы README ↓</a></div>
        </section>
        <div v-if="!corpusAvailable" class="notice error" role="status"><strong>README-корпус недоступен.</strong><span>{{ state.corpus?.reason }} Проверьте отслеживаемые README в Git.</span></div>

        <div class="demo-launch panel">
          <span class="demo-launch-icon" aria-hidden="true">▶</span>
          <div class="demo-launch-copy"><span class="scope-pill" :class="workspace">{{ workspace === 'demo' ? 'ДЕМОНСТРАЦИОННЫЙ ИНДЕКС' : 'ОСНОВНОЙ ИНДЕКС' }}</span><strong>Первый прогон с пустой базы</strong><p>Показ очищает отдельный демонстрационный индекс, показывает 0 чанков и векторов, затем строит его заново. Основной индекс сохраняется.</p></div>
          <div class="demo-actions"><button class="primary demo-start" type="button" :disabled="!corpusAvailable || busy || workspaceChanging || tourMode === 'running'" @click="startTour">{{ tourMode === 'running' ? 'Показ идёт…' : 'Начать показ' }}</button><button class="secondary" type="button" :disabled="!corpusAvailable || busy || workspaceChanging || tourMode === 'running'" @click="prepareEmptyDemo">Подготовить пустой индекс</button><button v-if="workspace === 'demo'" class="text-button" type="button" :disabled="busy || workspaceChanging || tourMode === 'running'" @click="returnToMain">Вернуться к основному индексу</button></div>
        </div>
        <p v-if="workspaceError" class="inline-error mode-error" role="alert">{{ workspaceError }}</p>

        <section class="metrics" aria-label="Сводка корпуса">
          <div class="metric"><span>Документов</span><strong>{{ number(state.corpus?.files) }}</strong><small>README-файлы</small></div>
          <div class="metric"><span>Разделов</span><strong>{{ number(state.corpus?.chapters) }}</strong><small>Заголовки README</small></div>
          <div class="metric"><span>Объём текста</span><strong>{{ number(state.corpus?.words) }}</strong><small>Слов после извлечения</small></div>
          <div class="metric"><span>Эквивалент</span><strong>{{ number(state.corpus?.pages) }}</strong><small>Страниц по 500 слов</small></div>
        </section>

        <section id="corpus" class="section-block" aria-labelledby="corpus-title">
          <div class="section-heading"><div><span class="eyebrow">01 / ИСХОДНЫЕ ДАННЫЕ</span><h2 id="corpus-title">Корпус документов</h2><p>README проекта и дней 01–20, которые отслеживаются в Git.</p></div><span class="section-tag">GIT → README → ТЕКСТ</span></div>
          <div class="corpus-grid">
            <div class="source-list" role="group" aria-label="Выберите документ">
              <button v-for="(doc, index) in documents" :key="doc.source" type="button" class="source-card" :class="{ selected: selectedDocument?.source === doc.source }" :aria-pressed="selectedDocument?.source === doc.source" @click="activeSource = doc.source">
                <span class="source-number">{{ String(index + 1).padStart(2, '0') }}</span><span class="source-body"><strong>{{ doc.title }}</strong><small>{{ doc.source }}</small></span><span class="source-words">{{ number(doc.words) }}<small>слов</small></span>
              </button>
            </div>
            <article v-if="selectedDocument" class="source-preview panel">
              <div class="preview-top"><span class="mono-label">ПРОСМОТР ИСТОЧНИКА</span><span class="file-pill">.md</span></div>
              <h3>{{ selectedDocument.title }}</h3><p class="source-path">{{ selectedDocument.source }}</p>
              <div class="chapters"><span class="mini-label">РАЗДЕЛЫ README</span><ul><li v-for="section in selectedDocument.sections" :key="section">{{ section }}</li></ul></div>
              <div class="excerpt"><span class="mini-label">ФРАГМЕНТ ТЕКСТА</span><p>{{ selectedDocument.excerpt || 'Фрагмент недоступен.' }}</p></div>
            </article>
          </div>
          <p class="context-note">Индексируются только разрешённые README. Локальные заметки, секреты, отчёты, PDF и README дней 21–23 не входят в корпус.</p>
        </section>

        <section id="pipeline" class="section-block" aria-labelledby="pipeline-title">
          <div class="section-heading"><div><span class="eyebrow">02 / ОБРАБОТКА</span><h2 id="pipeline-title">Пайплайн индексации</h2><p>Сборка создаёт оба варианта чанков и сохраняет векторы в SQLite.</p></div><span class="section-tag">ЛОКАЛЬНО</span></div>
          <div class="pipeline-panel panel">
            <div v-if="corpusAvailable && workspace === 'demo' && !ready && index?.exists === false && job?.status !== 'running'" id="empty-index" class="empty-index-proof" :class="{ 'tour-focus': tourMode === 'running' && tourScene === 'empty' }"><div><span class="mini-label">ПЕРЕД ПЕРВЫМ ПРОГОНОМ · ОТВЕТ API</span><h3>Индекс пока пуст</h3><p>{{ indexFile }} отсутствует. Исходные документы загружены; основной индекс сохранён отдельно.</p></div><div class="empty-proof-numbers"><span><strong>0</strong><small>чанков</small></span><span><strong>0</strong><small>эмбеддингов</small></span></div></div>
            <p v-if="!ready && index?.reason" class="inline-error" role="status">{{ index.reason }}</p>
            <div class="pipeline-toolbar"><div class="build-description"><span class="build-icon" aria-hidden="true">⌘</span><div><strong>{{ busy ? 'Идёт сборка индекса' : ready ? 'Индекс готов к поиску' : 'Индекс ещё не построен' }}</strong><small>{{ busy ? phaseText : ready ? 'Можно повторить сборку с другим перекрытием' : corpusAvailable ? 'Запустите сборку, чтобы получить чанки и эмбеддинги' : 'Сначала проверьте README репозитория' }}</small></div></div><form class="build-form" @submit.prevent="startBuild"><label for="overlap">Перекрытие чанков <strong>{{ overlapPercent }} %</strong></label><input id="overlap" v-model.number="overlapPercent" type="range" min="0" max="40" step="1" :disabled="busy || !corpusAvailable" /><button class="primary" type="submit" :disabled="busy || !corpusAvailable">{{ busy ? 'Сборка выполняется…' : ready ? 'Пересобрать индекс' : 'Построить индекс' }}</button></form></div>
            <p v-if="actionError" class="inline-error" role="alert">{{ actionError }}</p>
            <div class="stages" role="list" aria-label="Этапы индексации"><div v-for="(stage, index) in stages" :key="stage.key" role="listitem" class="stage" :class="stageState(stage.key)"><span class="stage-index">{{ stageState(stage.key) === 'done' ? '✓' : `0${index + 1}` }}</span><span><strong>{{ stage.title }}</strong><small>{{ stage.detail }}</small></span></div></div>
            <div class="job-detail" aria-live="polite"><span class="job-badge" :class="job?.status">{{ buildStatus(job?.status) }}</span><strong>{{ phaseText }}</strong><span v-if="progress !== null && job?.status === 'running'">{{ number(job.completed) }} / {{ number(job.total) }} · {{ progress }} %</span></div>
            <div v-if="progress !== null && job?.status === 'running'" class="progress-track" role="progressbar" :aria-valuenow="progress" aria-valuemin="0" aria-valuemax="100" :aria-label="`Прогресс: ${phaseText}`"><span :style="{ width: `${progress}%` }"></span></div>
            <p v-if="job?.error" class="inline-error" role="alert">{{ job.error }} Существующий индекс остаётся доступным, если был построен ранее.</p>
            <div v-if="job?.events?.length" class="event-log"><span class="mini-label">ЖУРНАЛ СОБЫТИЙ</span><ol><li v-for="(event, index) in job.events" :key="index">{{ eventText(event) }}</li></ol></div>
            <div class="pipeline-evidence" aria-label="Результаты стадий пайплайна">
              <article id="evidence-chunking" class="evidence-card" :class="{ 'tour-focus': tourMode === 'running' && tourScene === 'chunking' }">
                <div class="evidence-heading"><span class="evidence-number">01</span><div><span class="mini-label">РАЗБИЕНИЕ</span><h3>Чанки</h3></div></div>
                <span class="evidence-source">{{ ready ? evidenceSource : 'ДАННЫЕ ПОСЛЕ СБОРКИ' }}</span>
                <template v-if="ready">
                  <div class="evidence-pair"><div><strong>{{ number(index.strategies?.fixed?.count) }}</strong><small>fixed</small></div><div><strong>{{ number(index.strategies?.structure?.count) }}</strong><small>structure</small></div></div>
                  <p>Границы разделов: {{ number(index.strategies?.fixed?.crosses) }} / {{ number(index.strategies?.structure?.crosses) }} пересечений.</p>
                  <p>Окно до {{ number(manifest?.content_token_limit) }} токенов · перекрытие {{ number(Math.round((manifest?.overlap_ratio || 0) * 100)) }} %.</p>
                </template>
                <p v-else class="evidence-empty">После сборки здесь появятся измеренные результаты обеих стратегий.</p>
              </article>
              <article id="evidence-embeddings" class="evidence-card" :class="{ 'tour-focus': tourMode === 'running' && tourScene === 'embeddings' }">
                <div class="evidence-heading"><span class="evidence-number">02</span><div><span class="mini-label">ГЕНЕРАЦИЯ</span><h3>Эмбеддинги</h3></div></div>
                <span class="evidence-source">{{ ready ? evidenceSource : 'ДАННЫЕ ПОСЛЕ СБОРКИ' }}</span>
                <template v-if="ready">
                  <div class="evidence-main-value"><strong>{{ number(indexedVectorCount) }}</strong><span>сохранённых векторов</span></div>
                  <p>{{ number(manifest?.dimension) }} измерения · формат float32</p>
                  <p class="evidence-model">Модель: <code>{{ manifest?.model }}</code></p>
                </template>
                <p v-else class="evidence-empty">После сборки здесь появятся число векторов, размерность и модель.</p>
              </article>
              <article id="evidence-sqlite" class="evidence-card" :class="{ 'tour-focus': tourMode === 'running' && tourScene === 'sqlite' }">
                <div class="evidence-heading"><span class="evidence-number">03</span><div><span class="mini-label">СОХРАНЕНИЕ</span><h3>SQLite</h3></div></div>
                <span class="evidence-source">{{ ready ? evidenceSource : 'ДАННЫЕ ПОСЛЕ СБОРКИ' }}</span>
                <p class="evidence-path"><small>Файл выбранного режима</small><code>{{ indexFile }}</code></p>
                <p>Таблицы <code>manifest</code> и <code>chunks</code>.</p>
                <p v-if="ready"><strong>{{ number(indexedVectorCount) }}</strong> строк чанков прочитано API из SQLite.</p>
                <p v-else class="evidence-empty">После сборки API прочитает сохранённый индекс и покажет число строк.</p>
              </article>
            </div>
          </div>
        </section>

        <section id="compare" class="section-block" aria-labelledby="compare-title">
          <div class="section-heading"><div><span class="eyebrow">03 / СРАВНЕНИЕ</span><h2 id="compare-title">Две стратегии разбиения</h2><p>Один корпус, одна модель и одинаковый лимит токенов.</p></div><span v-if="manifest" class="section-tag">{{ number(manifest.dimension) }} ИЗМЕРЕНИЯ</span></div>
          <div v-if="!ready" class="empty-panel panel">После первой сборки здесь появятся измеренные показатели обеих стратегий.</div>
          <div v-else class="comparison-grid">
            <article v-for="strategy in ['fixed', 'structure']" :key="strategy" class="compare-card panel" :class="strategy"><div class="compare-head"><span class="strategy-mark">{{ strategy === 'fixed' ? '01' : '02' }}</span><div><span class="mini-label">СТРАТЕГИЯ {{ strategy.toUpperCase() }}</span><h3>{{ strategyName(strategy) }}</h3></div></div><p class="compare-description">{{ strategy === 'fixed' ? 'Последовательные окна токенов внутри файла. Окно может захватить текст соседних разделов.' : 'Сначала границы разделов, затем окна токенов внутри каждого раздела.' }}</p><template v-if="index?.strategies?.[strategy]"><div class="chunk-count"><strong>{{ number(index.strategies[strategy].count) }}</strong><span>чанков<br />с эмбеддингами</span></div><dl class="stat-list"><div><dt>Мин. / медиана</dt><dd>{{ number(index.strategies[strategy].min) }} / {{ number(index.strategies[strategy].median) }} токенов</dd></div><div><dt>P95 / макс.</dt><dd>{{ number(index.strategies[strategy].p95) }} / {{ number(index.strategies[strategy].max) }} токенов</dd></div><div><dt>Повтор токенов</dt><dd>{{ number(index.strategies[strategy].overlap) }}</dd></div><div><dt>Пересечение разделов</dt><dd :class="strategy === 'structure' ? 'positive' : ''">{{ number(index.strategies[strategy].crosses) }}</dd></div></dl></template><p v-else class="empty-copy">Данные стратегии отсутствуют в индексе.</p></article>
          </div>
          <div v-if="ready" class="comparison-note"><strong>Что видно из сравнения</strong><p>Разбиение по структуре сохраняет границы разделов. Количество и длины чанков отражают работу алгоритма; без размеченных запросов эти показатели не доказывают качество поиска.</p><small>Модель: {{ manifest?.model || '—' }} · Перекрытие: {{ manifest?.overlap_ratio != null ? Math.round(manifest.overlap_ratio * 100) + ' %' : '—' }}</small></div>
        </section>

        <section id="explore" class="section-block" aria-labelledby="explore-title"><div class="section-heading"><div><span class="eyebrow">04 / СОХРАНЁННЫЕ ДАННЫЕ</span><h2 id="explore-title">Просмотр чанков</h2><p>Текст, метаданные и размерность вектора читаются из SQLite.</p></div><span class="section-tag">SQLITE</span></div>
          <div class="explorer panel"><div class="explorer-toolbar"><div class="field"><label for="chunk-strategy">Стратегия</label><select id="chunk-strategy" v-model="chunkStrategy" :disabled="!ready"><option value="structure">По структуре</option><option value="fixed">Фиксированный размер</option></select></div><div class="field"><label for="chunk-source">Источник</label><select id="chunk-source" v-model="chunkSource" :disabled="!ready"><option value="">Все документы</option><option v-for="doc in documents" :key="doc.source" :value="doc.source">{{ doc.title }}</option></select></div><div class="explorer-count">{{ number(chunks.total) }}<small>чанков найдено</small></div></div>
            <p v-if="chunksError" class="inline-error" role="alert">{{ chunksError }} <button class="text-button" type="button" @click="loadChunks">Повторить</button></p>
            <div v-if="!ready" class="empty-copy">Сначала постройте индекс.</div><div v-else-if="chunksLoading" class="empty-copy" role="status">Загружаем чанки…</div><div v-else-if="!chunks.items.length" class="empty-copy">По выбранному фильтру чанков нет.</div>
            <div v-else class="chunk-list"><details v-for="item in chunks.items" :key="item.chunk_id" class="chunk-item"><summary><span class="chunk-order">{{ String(item.ordinal + 1).padStart(3, '0') }}</span><span class="chunk-main"><strong>{{ item.section }}</strong><small>{{ item.source }} · {{ item.chunk_id }}</small></span><span class="token-pill">{{ number(item.token_count) }} ток.</span></summary><div class="chunk-detail"><p>{{ item.text }}</p><dl><div><dt>Источник</dt><dd>{{ item.source }}</dd></div><div><dt>Заголовок</dt><dd>{{ item.title }}</dd></div><div><dt>Раздел</dt><dd>{{ item.section }}</dd></div><div><dt>Chunk ID</dt><dd><code>{{ item.chunk_id }}</code></dd></div><div><dt>Перекрытие</dt><dd>{{ number(item.overlap_tokens) }} токенов</dd></div><div><dt>Пересечение разделов</dt><dd>{{ item.crosses_sections ? 'Да' : 'Нет' }}</dd></div><div><dt>Вектор</dt><dd>{{ number(item.vector_dimensions) }} × float32, сохранён в SQLite</dd></div></dl></div></details></div>
            <div v-if="ready && chunks.total > 0" class="pagination"><span>Страница {{ number(chunkPage) }} из {{ number(totalPages) }}</span><div><button class="secondary" type="button" :disabled="chunkPage <= 1 || chunksLoading" @click="chunkPage -= 1">← Назад</button><button class="secondary" type="button" :disabled="chunkPage >= totalPages || chunksLoading" @click="chunkPage += 1">Далее →</button></div></div></div>
        </section>

        <section id="search" class="section-block search-section" aria-labelledby="search-title"><div class="section-heading"><div><span class="eyebrow">05 / ПРОВЕРКА ИНДЕКСА</span><h2 id="search-title">Семантический поиск</h2><p>Один запрос отправляется сразу к двум сохранённым стратегиям.</p></div><span class="section-tag">COSINE SEARCH</span></div>
          <div class="search-panel panel"><form class="search-form" @submit.prevent="runSearch"><div class="field"><label for="query">Поисковый запрос</label><input id="query" v-model="query" type="search" maxlength="300" placeholder="Например, как хранится история в Day 07?" :disabled="!ready || searching" required /></div><div class="field top-k"><label for="top-k">Результатов</label><select id="top-k" v-model.number="topK" :disabled="!ready || searching"><option v-for="value in 5" :key="value" :value="value">{{ value }}</option></select></div><button class="primary" type="submit" :disabled="!ready || searching || !query.trim()">{{ searching ? 'Ищем…' : 'Найти' }}</button></form><div class="query-examples"><span>Примеры:</span><button v-for="example in exampleQueries" :key="example" type="button" :disabled="!ready || searching" @click="useExample(example)">{{ example }}</button></div><p class="context-note">Здесь показаны найденные фрагменты README. Это поиск по индексу, без генерации ответа LLM.</p><p class="search-scope">Поиск по {{ workspace === 'demo' ? 'демонстрационному' : 'основному' }} индексу · <code>{{ indexFile }}</code></p></div>
          <p v-if="searchError" class="inline-error search-error" role="alert">{{ searchError }}</p><div v-if="!ready" class="empty-panel panel">Поиск станет доступен после сборки индекса.</div><div v-else-if="searching" class="empty-panel panel" role="status"><span class="spinner"></span> Вычисляем эмбеддинг запроса и сравниваем векторы…</div><div v-else-if="!searchResults" class="empty-panel panel">Введите запрос или выберите пример, чтобы сравнить результаты.</div><div v-else class="results-grid"><section v-for="strategy in ['fixed', 'structure']" :key="strategy" class="result-column panel" :class="strategy" :aria-label="`Результаты: ${strategyName(strategy)}`"><div class="result-head"><span class="mini-label">{{ strategy.toUpperCase() }}</span><h3>{{ strategyName(strategy) }}</h3><small>{{ number(searchResults.results?.[strategy]?.length) }} совпадений</small></div><p v-if="!searchResults.results?.[strategy]?.length" class="empty-copy">Совпадений нет.</p><article v-for="(result, index) in searchResults.results?.[strategy] || []" :key="result.chunk_id" class="result-item"><div class="result-meta"><span>{{ String(index + 1).padStart(2, '0') }} · {{ result.section }}</span><strong>score {{ score(result.score) }}</strong></div><p>{{ result.text }}</p><details class="result-full"><summary>Полный текст чанка</summary><pre>{{ result.text }}</pre></details><small>{{ result.source }} · {{ result.chunk_id }}</small></article></section></div><p v-if="searchResults?.retrieval_scope" class="context-note">Этап 1 — источники: {{ searchResults.retrieval_scope.sources?.join(', ') || 'все README корпуса' }}. Явный номер дня ограничивает поиск до cosine ranking; неоднозначный запрос ищет по всему корпусу.</p><p v-if="searchResults" class="context-note">Cosine score показывает близость векторов для этого запроса. Примеры поиска не являются количественной оценкой качества retrieval.</p>
        </section>
      </template>
    </main>
    <footer><span>DAY 21 · DOCUMENT INDEXING LAB</span><span>Локальные документы · локальный SQLite · без генерации ответов LLM</span></footer>
    <aside v-if="tourVisible" class="tour-overlay" :data-tour-scene="tourScene" :role="tourMode === 'error' ? 'alert' : 'status'" aria-live="polite">
      <div class="tour-topline"><span class="tour-kicker">{{ tourMode === 'finished' ? 'ПОКАЗ ЗАВЕРШЁН' : tourMode === 'error' ? 'ПОКАЗ ОСТАНОВЛЕН' : 'АВТОПОКАЗ · ДЕНЬ 21' }}</span><span class="tour-step">{{ tourStep ? `ШАГ ${tourStep} / ${TOUR_STEPS.length}` : 'ПОДГОТОВКА' }}</span></div>
      <div class="tour-content"><strong>{{ tourCaption }}</strong><div class="tour-controls"><button v-if="tourMode === 'running' && pauseActive" class="tour-subtle" type="button" @click="skipPause?.()">Далее</button><button v-if="tourMode === 'running'" class="tour-stop" type="button" @click="stopTour">Остановить показ</button><template v-else><button class="tour-subtle" type="button" @click="stopTour">Закрыть</button><button class="tour-stop" type="button" @click="startTour">Повторить показ</button></template></div></div>
      <div class="tour-track" aria-hidden="true"><span :style="{ width: `${tourStep / TOUR_STEPS.length * 100}%` }"></span></div>
    </aside>
  </div>
</template>
