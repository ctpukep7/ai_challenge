<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { batchRequest, formatDuration, friendlyError, mergeRetry, missingModes, sameEnvironment, summarize } from './metrics.js'

const state = ref(null)
const selectedId = ref('')
const question = ref('')
const strategy = ref('structure')
const topK = ref(5)
const current = ref(null)
const currentIndex = ref(null)
const currentFromBatch = ref(false)
const batch = ref([])
const batchConfig = ref(null)
const busy = ref(false)
const building = ref(false)
const progress = ref({ done: 0, total: 10 })
const error = ref('')
const activeTab = ref('chunks')
let poller

const questions = computed(() => state.value?.questions ?? [])
const chosen = computed(() => questions.value.find(item => item.id === selectedId.value))
const summary = computed(() => summarize(batch.value))
const pendingCount = computed(() => batch.value.filter(item => missingModes(item).length).length)
const ready = computed(() => !!state.value?.index?.ready)
const job = computed(() => state.value?.build)

async function request(path, options) {
  const response = await fetch(path, options)
  let body
  try { body = await response.json() } catch { body = {} }
  if (!response.ok) throw new Error(friendlyError(body, response.status))
  return body
}

async function refresh() {
  try {
    const next = await request('/api/state')
    state.value = next
    building.value = next.build.status === 'running'
    if (next.build.status === 'failed') error.value = next.build.error
    return true
  } catch (cause) {
    error.value = cause.message
    return false
  }
}

function selectFixture(item) {
  selectedId.value = item.id
  question.value = item.question
  current.value = null
  error.value = ''
}

function onQuestionInput() {
  if (chosen.value?.question !== question.value) selectedId.value = ''
}

async function buildIndex() {
  error.value = ''
  try {
    await request('/api/build', { method: 'POST' })
    building.value = true
    await refresh()
  } catch (cause) { error.value = cause.message }
}

async function compare(item = null) {
  if (!ready.value || building.value || !question.value.trim()) return
  busy.value = true
  progress.value = { done: 0, total: 10 }
  error.value = ''
  current.value = null
  currentIndex.value = null
  currentFromBatch.value = false
  try {
    const result = await request('/api/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question: question.value.trim(), strategy: strategy.value,
        top_k: Number(topK.value), question_id: (item?.id ?? selectedId.value) || null,
      }),
    })
    current.value = result
    currentIndex.value = JSON.stringify(state.value?.index?.manifest)
    activeTab.value = 'chunks'
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}

async function retryCurrent() {
  const previous = current.value
  if (!previous || !missingModes(previous).length || !ready.value || building.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    if (!(await refresh())) return
    if (!ready.value || building.value ||
        !sameEnvironment({ model: previous.model, index: currentIndex.value }, state.value)) {
      error.value = 'Модель или индекс изменились. Запустите новое сравнение.'
      return
    }
    const result = await request('/api/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(batchRequest(
        { id: previous.question_id, question: previous.question }, previous, previous,
      )),
    })
    const combined = mergeRetry(previous, result)
    current.value = combined
    if (currentFromBatch.value) {
      const index = batch.value.findIndex(item => item.question_id === combined.question_id)
      if (index >= 0) batch.value.splice(index, 1, combined)
    }
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}

async function runBatch(retry = false) {
  if (!ready.value || building.value || busy.value) return
  if (retry && !batchConfig.value) return
  if (retry && !(await refresh())) return
  if (!ready.value || building.value) return
  const runStrategy = retry ? batchConfig.value.strategy : strategy.value
  const runTopK = retry ? batchConfig.value.top_k : Number(topK.value)
  const runModel = state.value?.model
  const runIndex = JSON.stringify(state.value?.index?.manifest)
  if (retry && !sameEnvironment(batchConfig.value, state.value)) {
    error.value = 'Модель или индекс изменились. Запустите новый полный прогон.'
    return
  }
  const pending = retry
    ? questions.value.filter(item => missingModes(batch.value.find(result => result.question_id === item.id)).length)
    : questions.value
  busy.value = true
  if (!retry) {
    batch.value = []
    current.value = null
    currentFromBatch.value = false
    batchConfig.value = { strategy: runStrategy, top_k: runTopK, model: runModel, index: runIndex }
  }
  error.value = ''
  progress.value = { done: 0, total: pending.length }
  for (const item of pending) {
    const previous = batch.value.find(result => result.question_id === item.id)
    try {
      const result = await request('/api/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(batchRequest(item, batchConfig.value, retry ? previous : null)),
      })
      const combined = retry && previous ? mergeRetry(previous, result) : result
      const index = batch.value.findIndex(row => row.question_id === item.id)
      if (index < 0) batch.value.push(combined)
      else batch.value.splice(index, 1, combined)
      current.value = combined
      currentIndex.value = runIndex
      currentFromBatch.value = true
    } catch (cause) {
      const failed = { ...(previous || {}), question_id: item.id, question: item.question, error: cause.message }
      const index = batch.value.findIndex(row => row.question_id === item.id)
      if (index < 0) batch.value.push(failed)
      else batch.value.splice(index, 1, failed)
    }
    progress.value.done += 1
  }
  busy.value = false
}

function exportBatch() {
  const payload = { type: 'day-22-rag-comparison',
    strategy: batchConfig.value.strategy, top_k: batchConfig.value.top_k,
    model: batchConfig.value.model,
    summary: summary.value, results: batch.value }
  const link = document.createElement('a')
  link.href = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }))
  link.download = 'day-22-rag-comparison.json'
  link.click()
  URL.revokeObjectURL(link.href)
}

function fixtureFor(id) { return questions.value.find(item => item.id === id) }
function verdict(result, mode) {
  const evaluation = result[mode]?.evaluation
  return evaluation ? `${evaluation.fact_count}/${evaluation.fact_total} фактов` : result[mode]?.error || 'Нет ответа'
}
onMounted(() => {
  refresh()
  poller = window.setInterval(() => { if (building.value) refresh() }, 1200)
})
onUnmounted(() => window.clearInterval(poller))
</script>

<template>
  <div class="page">
    <header class="hero">
      <div class="hero-top"><span class="eyebrow">AI CHALLENGE · НЕДЕЛЯ RAG</span><span class="day-badge">DAY 22</span></div>
      <h1>Первый RAG-запрос<span class="accent">.</span></h1>
      <p>Один вопрос по README проекта AI Challenge. Два независимых ответа одной модели. Во втором запросе — найденные фрагменты README-корпуса.</p>
      <div class="flow"><span>ВОПРОС</span><b>→</b><span>ПОИСК ЧАНКОВ</span><b>→</b><span>КОНТЕКСТ + ВОПРОС</span><b>→</b><span>LLM</span></div>
    </header>

    <main>
      <section class="card index-card">
        <div class="section-heading"><div><span class="step">01 / ПОДГОТОВКА</span><h2>Локальный индекс</h2></div><span class="status" :class="{ good: ready, warning: building }">{{ building ? 'ИДЁТ СБОРКА' : ready ? 'ГОТОВ' : state?.index?.corpus_ready === false ? 'НЕТ КОРПУСА' : 'НЕТ ИНДЕКСА' }}</span></div>
        <p v-if="ready" class="muted">Корпус Day 21 · {{ state.index.manifest.files }} документа · {{ state.index.manifest.words?.toLocaleString('ru-RU') }} слов · {{ state.index.manifest.chunks.fixed }} fixed / {{ state.index.manifest.chunks.structure }} structure · {{ state.index.manifest.dimension }} измерения</p>
        <p v-else-if="state?.index?.corpus_ready === false" class="muted">Проверьте наличие отслеживаемых README репозитория. После этого здесь можно построить отдельный SQLite-индекс.</p>
        <p v-else class="muted">Первый запуск: индекс ещё не создан. Кнопка ниже построит Day 22 SQLite из тех же документов и тем же алгоритмом Day 21.</p>
        <p v-if="!ready && state?.index?.reason" class="hint">{{ state.index.reason }}</p>
        <button class="secondary" :disabled="building || busy || state?.index?.corpus_ready === false" @click="buildIndex">{{ ready ? 'Перестроить индекс' : 'Построить индекс' }}</button>
        <div v-if="job?.events?.length" class="events">
          <div v-for="(event, i) in job.events.slice(-7)" :key="i"><span class="event-dot"></span><strong>{{ event.phase }}</strong><span>{{ event.message }}</span><small v-if="event.total">{{ event.completed }}/{{ event.total }}</small></div>
        </div>
      </section>

      <section class="card question-card">
        <div class="section-heading"><div><span class="step">02 / ЭКСПЕРИМЕНТ</span><h2>Задайте вопрос</h2></div><span class="model-tag">{{ state?.model || 'deepseek-flash' }}</span></div>
        <div class="fixture-list"><button v-for="(item, i) in questions" :key="item.id" class="fixture" :class="{ chosen: selectedId === item.id }" :title="item.question" @click="selectFixture(item)"><span>{{ String(i + 1).padStart(2, '0') }}</span>{{ item.label }}</button></div>
        <p v-if="chosen" class="expected"><b>Ожидание:</b> {{ chosen.expected.map(group => group.fact).join('; ') }} <span>· {{ chosen.source }} / {{ chosen.section }}</span></p>
        <label for="question">Вопрос по README проекта AI Challenge</label>
        <textarea id="question" v-model="question" maxlength="300" rows="3" placeholder="Выберите контрольный вопрос или напишите свой..." @input="onQuestionInput"></textarea>
        <div class="controls"><label>Стратегия <select v-model="strategy" :disabled="busy || building"><option value="structure">По структуре</option><option value="fixed">Фиксированные окна</option></select></label><label>Top-k <select v-model="topK" :disabled="busy || building"><option v-for="n in 5" :key="n" :value="n">{{ n }}</option></select></label><button class="primary" :disabled="!ready || building || busy || !question.trim()" @click="compare()">Сравнить ответы <span>↗</span></button></div>
        <p class="muted small">Оба режима: thinking {{ state?.thinking || 'disabled' }}, max_tokens {{ state?.max_tokens ?? 400 }}, T={{ state?.temperature ?? 0 }}. Top-k задаёт число фрагментов в RAG-контексте и влияет на размер запроса. Размер prompt — сумма символов system и user сообщений. Вызовы модели независимы.</p>
      </section>

      <div v-if="error" class="error" role="alert">{{ error }}</div>
      <div v-if="busy" class="loading"><span class="spinner"></span>{{ progress.done ? `Контрольный прогон: ${progress.done}/${progress.total}` : 'Поиск фрагментов и два вызова модели…' }}</div>

      <section v-if="current" class="results">
        <div class="section-heading"><div><span class="step">03 / РЕЗУЛЬТАТ</span><h2>Два ответа на один вопрос</h2></div><span class="muted small">Top-{{ current.top_k }} · {{ current.strategy }} · поиск {{ formatDuration(current.retrieval_ms) }}</span></div>
        <p class="asked">{{ current.question }}</p>
        <p v-if="current.retrieval_scope" class="muted small">Этап 1 RAG — источники: {{ current.retrieval_scope.sources?.join(', ') || 'все README корпуса' }}; затем cosine Top-K. Ответ без RAG получает только вопрос.</p>
        <p class="muted small">Фактические параметры: {{ current.model }} · thinking {{ current.thinking }} · max_tokens {{ current.max_tokens }} · T={{ current.temperature }}. Более длинный контекст может увеличить задержку, но сам по себе не объясняет каждый таймаут.</p>
        <p v-if="current.retried" class="muted small">После повтора времена режимов относятся к разным попыткам; время поиска — к последнему запросу.</p>
        <div class="answer-grid">
          <article class="answer-card baseline"><div class="answer-label"><span>А</span><div><strong>Без RAG</strong><small>Только вопрос → LLM</small></div></div><p v-if="current.without_rag.answer" class="answer">{{ current.without_rag.answer }}</p><p v-else class="error-inline">{{ current.without_rag.error }}</p><p class="diagnostics">DeepSeek API {{ formatDuration(current.without_rag.elapsed_ms) }} · prompt (system + user) {{ current.without_rag.prompt_chars?.toLocaleString('ru-RU') ?? '—' }} символов</p><footer><span>{{ verdict(current, 'without_rag') }}</span><span v-if="current.without_rag.usage">{{ current.without_rag.usage.total_tokens }} токенов</span></footer></article>
          <article class="answer-card augmented"><div class="answer-label"><span>Б</span><div><strong>С RAG</strong><small>Вопрос + {{ current.chunks.length }} фрагментов → LLM</small></div></div><p v-if="current.with_rag.answer" class="answer">{{ current.with_rag.answer }}</p><p v-else class="error-inline">{{ current.with_rag.error }}</p><p class="diagnostics">DeepSeek API {{ formatDuration(current.with_rag.elapsed_ms) }} · prompt (system + user) {{ current.with_rag.prompt_chars?.toLocaleString('ru-RU') ?? '—' }} символов</p><footer><span>{{ verdict(current, 'with_rag') }}</span><span v-if="current.with_rag.evaluation?.expected_source_retrieved">Нужный раздел найдена</span><span v-if="current.with_rag.evaluation?.citation_markers_valid">Маркеры ссылок допустимы</span><span v-else-if="current.with_rag.evaluation?.has_citation_marker">Некорректный номер ссылки</span><span v-if="current.with_rag.usage">{{ current.with_rag.usage.total_tokens }} токенов</span></footer></article>
        </div>
        <button v-if="missingModes(current).length" class="secondary retry-current" :disabled="busy || building" @click="retryCurrent">Повторить неудавшийся ответ ↗</button>
      </section>

      <section v-if="current" class="card evidence-card">
        <div class="section-heading"><div><span class="step">04 / ПРОВЕРКА</span><h2>Что модель получила на вход</h2></div></div>
        <div class="tabs"><button :class="{ active: activeTab === 'chunks' }" @click="activeTab = 'chunks'">Найденные чанки</button><button :class="{ active: activeTab === 'prompt' }" @click="activeTab = 'prompt'">Точный RAG-запрос</button><button :class="{ active: activeTab === 'baseline' }" @click="activeTab = 'baseline'">Запрос без RAG</button></div>
        <div v-if="activeTab === 'chunks'" class="chunks"><details v-for="chunk in current.chunks" :key="chunk.chunk_id" class="chunk"><summary><span class="marker">{{ chunk.marker }}</span><span><strong>{{ chunk.section }}</strong><small>{{ chunk.source }} · cosine {{ chunk.score.toFixed(3) }} · {{ chunk.chunk_id }}</small></span><span class="chevron">⌄</span></summary><p>{{ chunk.text }}</p></details></div>
        <pre v-else>{{ JSON.stringify(activeTab === 'prompt' ? current.with_rag.messages : current.without_rag.messages, null, 2) }}</pre>
        <p class="muted small">Чанки загружены из локального SQLite. Текст источника рассматривается как данные, а не команды модели.</p>
      </section>

      <section class="card benchmark">
        <div class="section-heading"><div><span class="step">05 / КОНТРОЛЬНЫЙ НАБОР</span><h2>10 вопросов по базе</h2></div><div class="batch-actions"><button v-if="pendingCount" class="secondary" :disabled="!ready || building || busy" @click="runBatch(true)">Повторить неполные ({{ pendingCount }}) ↗</button><button class="secondary" :disabled="!ready || building || busy" @click="runBatch(false)">Запустить все 10 ↗</button></div></div>
        <p class="muted">Для каждого вопроса заранее заданы проверяемые факты и раздел-источник. Один прогон делает 20 отдельных вызовов DeepSeek и может занять несколько минут.</p>
        <p v-if="batchConfig" class="muted small">Параметры текущего прогона: {{ batchConfig.strategy }} · top-{{ batchConfig.top_k }}. Повтор использует их, даже если настройки выше изменились. Успешные ответы не отправляются повторно.</p>
        <div v-if="busy && progress.done" class="progress"><div :style="{ width: `${100 * progress.done / progress.total}%` }"></div></div>
        <div v-if="batch.length" class="score-grid"><div><strong>{{ summary.total }}/10</strong><span>полных пар ответов</span></div><div><strong>{{ summary.without ?? '—' }}%</strong><span>факты без RAG</span></div><div><strong>{{ summary.with ?? '—' }}%</strong><span>факты с RAG</span></div><div><strong>{{ summary.sourceHits }}/{{ summary.total }}</strong><span>нужный раздел в top-k</span></div></div>
        <div v-if="batch.length" class="batch-list"><details v-for="(item, i) in batch" :key="i"><summary><span>{{ String(i + 1).padStart(2, '0') }}</span><strong>{{ item.question }}</strong><small>{{ missingModes(item).length ? 'Неполный ответ' : `${verdict(item, 'without_rag')} → ${verdict(item, 'with_rag')}` }}</small></summary><div v-if="item.error" class="error-inline">{{ item.error }}</div><div class="batch-detail"><p v-if="item.model" class="diagnostics">Поиск {{ formatDuration(item.retrieval_ms) }} · {{ item.model }} · thinking {{ item.thinking }} · max_tokens {{ item.max_tokens }}</p><p><b>Ожидание:</b> {{ fixtureFor(item.question_id)?.expected.map(group => group.fact).join('; ') }}</p><p><b>Источник:</b> {{ fixtureFor(item.question_id)?.source }} / {{ fixtureFor(item.question_id)?.section }} · {{ item.with_rag?.evaluation?.expected_source_retrieved ? 'найден' : item.chunks ? 'не найден' : 'поиск не завершён' }}</p><p v-if="item.with_rag?.answer"><b>Маркеры ссылок:</b> {{ item.with_rag.evaluation?.citation_markers_valid ? 'номера допустимы' : item.with_rag.evaluation?.has_citation_marker ? 'есть неверный номер' : 'отсутствуют' }}</p><p><b>Без RAG:</b> {{ item.without_rag?.answer || item.without_rag?.error || 'Нет ответа' }}</p><p><b>С RAG:</b> {{ item.with_rag?.answer || item.with_rag?.error || 'Нет ответа' }}</p><p v-if="item.without_rag" class="diagnostics">Без RAG: DeepSeek API {{ formatDuration(item.without_rag.elapsed_ms) }} · prompt (system + user) {{ item.without_rag.prompt_chars?.toLocaleString('ru-RU') ?? '—' }} символов</p><p v-if="item.with_rag" class="diagnostics">С RAG: DeepSeek API {{ formatDuration(item.with_rag.elapsed_ms) }} · prompt (system + user) {{ item.with_rag.prompt_chars?.toLocaleString('ru-RU') ?? '—' }} символов</p><p v-if="item.retried" class="muted small">После повтора времена режимов относятся к разным попыткам; время поиска — к последнему запросу.</p></div></details></div>
        <button v-if="batch.length" class="export" @click="exportBatch">Скачать результаты JSON ↓</button>
        <p class="caveat">Оценка — эвристика: ищет ожидаемые слова в ответе и нужную раздел среди top-k. Она не проверяет смысл, истинность каждого утверждения или корректность цитаты. Неполные пары не входят в средний показатель. Источник — отслеживаемые README проекта и дней 01–20.</p>
      </section>
    </main>
    <footer class="page-footer">AI CHALLENGE / DAY 22 <span>Локальная учебная демонстрация</span></footer>
  </div>
</template>
