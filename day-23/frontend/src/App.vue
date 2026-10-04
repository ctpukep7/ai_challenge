<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import ModeCard from './components/ModeCard.vue'
import { MODES, MODE_NAMES, TERMINAL, buildRequest, callBudget, corpusSummary, formatDuration, formatScore, friendlyError, recoverActiveRun, recoveredSettings, validateSettings } from './metrics.js'

const state = ref(null), run = ref(null), error = ref(''), selectedId = ref(''), question = ref('')
const selectedResult = ref(0), candidateMode = ref('filtered'), submitting = ref(false), cancelling = ref(false)
const settings = ref({ strategy: 'structure', top_k_before: 12, top_k_after: 3, threshold: .5, rewrite_method: 'local' })
let timer, refreshing = false, disposed = false
const questions = computed(() => state.value?.questions ?? [])
const fixture = computed(() => questions.value.find(item => item.id === selectedId.value))
const building = computed(() => ['queued', 'running'].includes(state.value?.build?.status))
const running = computed(() => !!run.value && !TERMINAL.includes(run.value.status))
const locked = computed(() => running.value || building.value || submitting.value)
const ready = computed(() => !!state.value?.index?.ready)
const result = computed(() => run.value?.results?.[selectedResult.value] ?? null)
const candidates = computed(() => result.value?.modes?.[candidateMode.value]?.candidates ?? [])
const manifest = computed(() => state.value?.index?.manifest ?? {})
const corpus = computed(() => corpusSummary(state.value?.index))
const answerableCount = computed(() => questions.value.filter(item => !item.unanswerable).length)
const negativeCount = computed(() => questions.value.filter(item => item.unanswerable).length)
const summary = computed(() => run.value?.summary)
const progressPercent = computed(() => run.value?.total ? Math.min(100, 100 * (run.value.current ?? 0) / run.value.total) : 0)
const canSearch = computed(() => ready.value && !locked.value && !!question.value.trim())
const phaseNames = { queued: 'Ожидание', running: 'Выполняется', completed: 'Завершено', failed: 'Ошибка', cancelled: 'Остановлено', retrieval: 'Поиск кандидатов', rewrite: 'Переписывание запроса', generation: 'Ответ модели', rewritten_retrieval: 'Поиск нового запроса', abstention: 'Отказ по правилу', complete: 'Завершено', corpus: 'Корпус', chunking: 'Разбиение', embeddings: 'Эмбеддинги', embedding: 'Эмбеддинги', saving: 'Сохранение', indexing: 'Индексация', validate: 'Проверка' }

async function request(path, options) {
  const response = await fetch(path, options)
  let body
  try { body = await response.json() } catch { body = {} }
  if (!response.ok) {
    const cause = new Error(friendlyError(body, response.status))
    cause.status = response.status
    throw cause
  }
  return body
}
async function refreshState() {
  state.value = await request('/api/state')
}
async function poll() {
  if (refreshing || disposed) return
  refreshing = true
  try {
    if (running.value) {
      const next = await request(`/api/runs/${run.value.id}`)
      if (!disposed) run.value = next
      if (next.status === 'failed') error.value = next.error || 'Прогон завершился ошибкой.'
      if (TERMINAL.includes(next.status)) cancelling.value = false
    }
    if (building.value) await refreshState()
  } catch (cause) { if (!disposed) error.value = cause.message }
  finally { refreshing = false }
}
async function build() {
  error.value = ''; submitting.value = true
  try { await request('/api/build', { method: 'POST' }); await refreshState() }
  catch (cause) { error.value = cause.message }
  finally { submitting.value = false }
}
function choose(item) { selectedId.value = item.id; question.value = item.question; error.value = '' }
function edited() { if (fixture.value?.question !== question.value) selectedId.value = '' }
function payload(answers, benchmark) {
  return buildRequest(settings.value, { answers, benchmark, question: question.value, questionId: selectedId.value })
}
async function start(answers = false, benchmark = false) {
  const validation = validateSettings(settings.value)
  if (validation) { error.value = validation; return }
  error.value = ''; submitting.value = true
  try {
    const created = await request(benchmark ? '/api/benchmark' : '/api/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload(answers, benchmark)),
    })
    selectedResult.value = 0
    run.value = await request(`/api/runs/${created.id}`)
  } catch (cause) { error.value = cause.message }
  finally { submitting.value = false }
}
async function cancel() {
  cancelling.value = true; error.value = ''
  try { await request(`/api/runs/${run.value.id}/cancel`, { method: 'POST' }); await poll() }
  catch (cause) { error.value = cause.message; cancelling.value = false }
}
function download() {
  const link = document.createElement('a')
  const url = URL.createObjectURL(new Blob([JSON.stringify(run.value, null, 2)], { type: 'application/json' }))
  link.href = url; link.download = `day-23-${run.value.kind}-${run.value.id}.json`; link.click()
  URL.revokeObjectURL(url)
}
function percent(value) { return Number.isFinite(value) ? `${Math.round(100 * value)}%` : '—' }
function decision(item) { return item.reason || ({ selected: 'В контекст', below_threshold: 'Ниже порога', top_k: 'За лимитом Top-K' }[item.decision] ?? item.decision) }
function itemFixture(item) { return questions.value.find(q => q.id === item.question_id) }
function factText(item) { return (item?.expected ?? []).map(group => group.fact).join('; ') }
function chooseResult(index) { selectedResult.value = index }
onMounted(async () => {
  submitting.value = true
  try {
    await refreshState()
    settings.value = recoveredSettings(settings.value, state.value.defaults)
    const active = await recoverActiveRun(state.value.active_run, request)
    if (active && !disposed) {
      run.value = active
      settings.value = recoveredSettings(settings.value, active.settings)
      selectedResult.value = Math.max(0, (active.results?.length ?? 0) - 1)
      question.value = active.results?.[selectedResult.value]?.question ?? ''
      selectedId.value = active.results?.[selectedResult.value]?.question_id ?? ''
      cancelling.value = !!active.cancel_requested && !TERMINAL.includes(active.status)
    }
  }
  catch (cause) { error.value = cause.message }
  finally { submitting.value = false }
  if (!disposed) timer = window.setInterval(poll, 1100)
})
onUnmounted(() => { disposed = true; window.clearInterval(timer) })
</script>

<template>
  <div class="page">
    <header class="hero">
      <div class="hero-top"><span class="eyebrow">AI CHALLENGE · НЕДЕЛЯ RAG</span><span class="day-tag">ДЕНЬ 23</span></div>
      <h1>Найти. Отобрать.<br><span>Ответить по источникам.</span></h1>
      <p>Фильтрация релевантности и query rewrite на README-файлах текущего проекта. Три режима, один вопрос и видимые решения по каждому фрагменту.</p>
      <div class="hero-flow"><span>ВОПРОС</span><b>→</b><span>REWRITE</span><b>→</b><span>TOP-K ДО</span><b>→</b><span>ПОРОГ + TOP-K ПОСЛЕ</span><b>→</b><span>КОНТЕКСТ → LLM</span></div>
    </header>

    <main>
      <div v-if="error" class="error-banner" role="alert">{{ error }}<button aria-label="Скрыть ошибку" @click="error = ''">×</button></div>
      <section class="panel index-panel">
        <div class="section-heading"><div><span class="step">01 / ИСТОЧНИКИ</span><h2>README проекта. Локальный индекс.</h2></div><span class="status" :class="{ good: ready, warning: building }">{{ building ? 'СБОРКА' : ready ? 'ИНДЕКС ГОТОВ' : state?.index?.corpus_ready === false ? 'README НЕДОСТУПНЫ' : 'ИНДЕКС НЕ СОЗДАН' }}</span></div>
        <div class="index-body"><div><p class="muted">{{ corpus.files ?? '—' }} README-файлов · {{ corpus.words?.toLocaleString('ru-RU') ?? '—' }} слов · {{ corpus.sections ?? '—' }} разделов</p><p v-if="ready" class="muted small">{{ manifest.chunks?.fixed ?? '—' }} fixed / {{ manifest.chunks?.structure ?? '—' }} structure чанков · {{ manifest.embedding?.dimensions ?? manifest.dimension ?? '—' }} измерения</p><p v-else class="muted small">{{ state?.index?.reason || 'Постройте SQLite-индекс из README текущего репозитория.' }}</p></div><button class="secondary" :disabled="locked || !state || state.index?.corpus_ready === false" @click="build">{{ ready ? 'Перестроить индекс' : 'Построить индекс' }}</button></div>
        <details v-if="corpus.sources.length" class="corpus-sources"><summary>Файлы источников ({{ corpus.sources.length }})</summary><ul><li v-for="source in corpus.sources" :key="source"><code>{{ source }}</code></li></ul><p class="small muted">README в корне и README дней 01–20. Описания RAG-дней 21–23 исключены из базы.</p></details>
        <p class="small muted">Сборка и поиск выполняются локально. Открытие страницы не запускает индексацию или запрос к модели.</p>
        <div v-if="state?.build?.events?.length" class="event-log"><div v-for="(event, i) in state.build.events.slice(-5)" :key="i"><span class="event-dot"></span><strong>{{ phaseNames[event.phase] ?? event.phase }}</strong><span>{{ event.message }}</span><small v-if="event.total">{{ event.completed }}/{{ event.total }}</small></div></div>
        <p v-if="state?.build?.error" class="inline-error">{{ state.build.error }}</p>
      </section>

      <section class="panel experiment">
        <div class="section-heading"><div><span class="step">02 / НАСТРОЙКИ ПОИСКА</span><h2>Сколько контекста оставить?</h2></div><span class="model-tag">{{ state?.model ?? 'DeepSeek' }}</span></div>
        <div class="settings-grid">
          <label>Разбиение документов<select v-model="settings.strategy" :disabled="locked"><option value="structure">По структуре разделов</option><option value="fixed">Фиксированные окна</option></select></label>
          <label>Top-K до фильтра<input v-model.number="settings.top_k_before" type="number" min="1" max="30" step="1" :disabled="locked"><small>Кандидатов: 1–30</small></label>
          <label>Top-K после фильтра<input v-model.number="settings.top_k_after" type="number" min="1" :max="Math.min(10, settings.top_k_before)" step="1" :disabled="locked"><small>В контексте: ≤ {{ Math.min(10, settings.top_k_before) }}</small></label>
          <label>Порог cosine<input v-model.number="settings.threshold" type="number" min="-1" max="1" step="0.01" :disabled="locked"><small>Сохраняем score ≥ порога</small></label>
          <label>Query rewrite<select v-model="settings.rewrite_method" :disabled="locked"><option value="local">Локальные правила · 0 LLM</option><option value="llm">Отдельный запрос к LLM</option></select><small>При поиске без ответов всегда локальный</small></label>
        </div>
        <div class="rule-explainer"><b>Как сравниваем</b><p>А: первые {{ settings.top_k_after }} кандидата. Б: score ≥ {{ settings.threshold }}, затем до {{ settings.top_k_after }}. В: переписанный запрос → новый поиск → тот же фильтр.</p><p class="small">Cosine — сходство векторов, а не вероятность правильного ответа. При rewrite оценки относятся к новому запросу: их рост сам по себе не доказывает улучшение.</p></div>
        <div class="section-heading question-heading"><div><span class="step">03 / ВОПРОС</span><h2>Что спросим у базы?</h2></div></div>
        <div class="question-chips"><button v-for="(item, i) in questions" :key="item.id" :class="{ chosen: item.id === selectedId, negative: item.unanswerable }" :disabled="locked" :title="item.question" @click="choose(item)"><span>{{ String(i + 1).padStart(2, '0') }}</span>{{ item.label }}<small v-if="item.unanswerable">вне базы</small></button></div>
        <label for="question">Исходный вопрос</label><textarea id="question" v-model="question" rows="3" maxlength="600" placeholder="Выберите контрольный вопрос выше или задайте свой…" :disabled="locked" @input="edited"></textarea>
        <div v-if="fixture" class="expectation"><b>Ожидание:</b> {{ fixture.unanswerable ? 'Отказ по базе: в README нет ответа.' : factText(fixture) }}<p v-if="fixture.source">Источник: {{ fixture.source }} · {{ fixture.section }}</p></div>
        <div class="action-row"><button class="primary" :disabled="!canSearch" @click="start(false)">RAG <span>→</span></button><button class="secondary" :disabled="!canSearch || !state?.llm_configured" @click="start(true)">RAG + LLM <span>↗</span></button><span class="call-hint">RAG: поиск, rewrite и фильтрация · без LLM<br>RAG + LLM: те же 3 режима + ответы DeepSeek · до {{ callBudget(1, true, settings.rewrite_method) }} основных LLM-вызовов</span></div>
        <p class="small muted">Число основных LLM-вызовов не учитывает возможные HTTP-повторы. Одинаковые параметры генерации всех ответов: {{ state?.model ?? '—' }} · T={{ state?.temperature ?? 0 }} · max_tokens={{ state?.max_tokens ?? 400 }} · thinking={{ state?.thinking ?? 'disabled' }}. При пустом контексте применяется явный отказ без LLM.</p>
        <p v-if="state && !state.llm_configured" class="hint">Поиск доступен без ключа. Для ответов и LLM rewrite настройте DeepSeek в локальном .env.</p>
      </section>

      <section v-if="run" class="panel run-panel" aria-live="polite">
        <div class="section-heading"><div><span class="step">04 / ХОД ЭКСПЕРИМЕНТА</span><h2>{{ phaseNames[run.status] ?? run.status }} <span class="run-counter">{{ run.current ?? 0 }}/{{ run.total }}</span></h2></div><button v-if="running" class="stop" :disabled="cancelling" @click="cancel">{{ cancelling ? 'Остановка запрошена' : 'Остановить после этапа' }}</button><button v-else class="secondary" @click="download">Скачать JSON ↓</button></div>
        <p class="muted small">Сохранённые настройки: {{ run.settings?.strategy }} · Top-K {{ run.settings?.top_k_before }} → {{ run.settings?.top_k_after }} · порог {{ run.settings?.threshold }} · rewrite {{ run.settings?.rewrite_method }} · {{ run.settings?.generate_answers ? 'с ответами' : 'только поиск' }}.</p>
        <div class="progress-track" role="progressbar" :aria-valuenow="run.current ?? 0" :aria-valuemax="run.total" aria-valuemin="0" aria-label="Завершено вопросов"><div :style="{ width: `${progressPercent}%` }"></div></div>
        <p v-if="running" class="small muted"><span class="pulse-dot"></span>{{ phaseNames[run.phase] ?? run.phase }}. Отмена прекращает следующие шаги; уже отправленный запрос к провайдеру может завершиться.</p>
        <div class="event-log"><div v-for="(event, i) in (run.events ?? []).slice(-8)" :key="i"><span class="event-dot"></span><strong>{{ phaseNames[event.phase] ?? event.phase }}</strong><span>{{ event.message }}</span></div></div>
        <p v-if="run.error" class="inline-error">{{ run.error }}</p>
        <div v-if="run.results?.length > 1" class="result-selector"><button v-for="(item, i) in run.results" :key="item.question_id ?? i" :class="{ active: i === selectedResult }" @click="chooseResult(i)">{{ i + 1 }} · {{ itemFixture(item)?.label ?? item.question.slice(0, 30) }}</button></div>
      </section>

      <section v-if="result" class="results">
        <div class="section-heading"><div><span class="step">05 / СРАВНЕНИЕ РЕЖИМОВ</span><h2>Один вопрос. Три пути к ответу.</h2></div></div>
        <p class="asked">{{ result.question }}</p>
        <div v-if="result.rewrite" class="rewrite-panel"><div><span class="step">QUERY REWRITE · {{ result.rewrite.method }}</span><b>{{ result.rewrite.status === 'failed' ? 'Ошибка переписывания' : result.rewrite.changed ? 'Запрос изменён' : 'Запрос сохранён' }}</b></div><p><span>Было:</span> {{ result.rewrite.original ?? result.question }}</p><p><span>Стало:</span> {{ result.rewrite.query ?? 'Новый запрос не получен' }}</p><p v-if="result.rewrite.applied_rules?.length" class="small muted">Правила: {{ result.rewrite.applied_rules.join(' · ') }}</p><p v-if="result.rewrite.error" class="inline-error">{{ result.rewrite.error }}</p><p class="small muted">{{ result.rewrite.method === 'local' ? 'Детерминированные правила, без контрольных ответов и внешних вызовов.' : `Отдельный запрос к модели · ${formatDuration(result.rewrite.elapsed_ms)}.` }}</p><details v-if="result.rewrite.messages?.length"><summary>Запрос переписывания</summary><pre>{{ JSON.stringify(result.rewrite.messages, null, 2) }}</pre></details></div>
        <div class="mode-grid"><ModeCard :mode="result.modes?.baseline" :generate-answers="run.settings?.generate_answers" :run-active="running" name="Без cosine-порога" subtitle="Тот же выбор README → cosine Top-K" letter="А" variant="baseline"/><ModeCard :mode="result.modes?.filtered" :generate-answers="run.settings?.generate_answers" :run-active="running" name="С фильтром" subtitle="Тот же выбор README → cosine-порог → Top-K" letter="Б" variant="filtered"/><ModeCard :mode="result.modes?.rewritten" :generate-answers="run.settings?.generate_answers" :run-active="running" name="Rewrite + фильтр" subtitle="Те же README → новый запрос → cosine-порог → Top-K" letter="В" variant="rewritten"/></div>
      </section>

      <section v-if="result" class="panel evidence">
        <div class="section-heading"><div><span class="step">06 / РЕШЕНИЯ ПО КАНДИДАТАМ</span><h2>Какие фрагменты прошли в контекст?</h2></div></div>
        <div class="tabs" role="tablist" aria-label="Кандидаты по режимам"><button v-for="key in MODES" :key="key" role="tab" :aria-selected="candidateMode === key" :class="{ active: candidateMode === key }" @click="candidateMode = key">{{ MODE_NAMES[key] }} <span>{{ result.modes?.[key]?.counts?.kept ?? 0 }}/{{ result.modes?.[key]?.counts?.retrieved ?? 0 }}</span></button></div>
        <p class="muted small">Отсечённые фрагменты показаны для проверки; в точный prompt они не входят. Слишком высокий порог может удалить нужный раздел README.</p>
        <div v-if="candidates.length" class="table-scroll"><table><thead><tr><th scope="col">Ранг</th><th scope="col">Cosine</th><th scope="col">Решение / причина</th><th scope="col">Источник, раздел и chunk_id</th></tr></thead><tbody><tr v-for="candidate in candidates" :key="candidate.chunk_id" :class="{ kept: candidate.decision === 'selected' }"><td>{{ candidate.rank }}</td><td class="score">{{ formatScore(candidate.score) }}</td><td><span class="decision" :class="candidate.decision">{{ candidate.decision === 'selected' ? '✓ В контекст' : candidate.decision === 'below_threshold' ? '↓ Ниже порога' : 'За Top-K' }}</span><small>{{ decision(candidate) }}</small></td><td><details><summary><strong>{{ candidate.section }}</strong><small>{{ candidate.title }} · {{ candidate.source }}<br>{{ candidate.chunk_id }}</small></summary><p class="source-text">{{ candidate.text }}</p></details></td></tr></tbody></table></div>
        <p v-else class="empty-state">{{ result.modes?.[candidateMode]?.error || 'Нет кандидатов. Этап поиска ещё не завершён или контекст недоступен.' }}</p>
      </section>

      <section class="panel benchmark">
        <div class="section-heading"><div><span class="step">07 / КОНТРОЛЬНЫЙ НАБОР</span><h2>{{ questions.length || '—' }} вопросов: факты и границы базы</h2></div><span class="model-tag">{{ answerableCount }} по README + {{ negativeCount }} вне базы</span></div>
        <p class="muted">Сравним попадание в ожидаемый раздел README, пустой контекст для посторонних вопросов и, при генерации, слова-факты в ответах.</p>
        <div class="action-row"><button class="primary" :disabled="!ready || locked || !questions.length" @click="start(false, true)">Прогон поиска · 0 LLM →</button><button class="secondary" :disabled="!ready || locked || !state?.llm_configured || !questions.length" @click="start(true, true)">Прогон с ответами ↗</button><span class="call-hint">С ответами: до {{ callBudget(questions.length, true, settings.rewrite_method) }} основных LLM-вызовов<br>Пустой контекст уменьшает число вызовов</span></div>
        <div v-if="summary && run?.kind === 'benchmark'" class="summary-section"><h3>Фактические результаты текущего прогона</h3><p class="muted small">Общие успешно завершённые вопросы: поиск {{ summary.retrieval?.common_count ?? 0 }}, генерация {{ summary.generation?.common_count ?? 0 }}. Ошибки не превращаются в нулевое качество; отказ по правилу не оценивается как ответ LLM.</p><div class="summary-grid"><article v-for="key in MODES" :key="key"><h4>{{ MODE_NAMES[key] }}</h4><dl><div><dt>Нужный раздел README найден</dt><dd>{{ percent(summary.retrieval?.modes?.[key]?.chapter_hit_rate) }}</dd></div><div><dt>Доля чанков нужного раздела</dt><dd>{{ percent(summary.retrieval?.modes?.[key]?.chapter_precision_proxy) }}</dd></div><div><dt>Пусто для вопросов вне базы</dt><dd>{{ percent(summary.retrieval?.modes?.[key]?.negative_empty_rate) }}</dd></div><div><dt>Слов-фактов в ответе</dt><dd>{{ percent(summary.generation?.modes?.[key]?.fact_ratio) }}</dd></div></dl><p class="small muted">Знаменатели: по README {{ summary.retrieval?.modes?.[key]?.answerable_count ?? 0 }} · вне базы {{ summary.retrieval?.modes?.[key]?.negative_count ?? 0 }} · LLM {{ summary.generation?.modes?.[key]?.count ?? 0 }}</p></article></div><p class="muted small">Основные LLM-вызовы (без HTTP-повторов): ответы {{ summary.calls?.answer ?? 0 }} + rewrite {{ summary.calls?.rewrite ?? 0 }} = {{ summary.calls?.total ?? 0 }}.</p></div>
        <div class="fixture-details"><details v-for="(item, i) in questions" :key="item.id"><summary><span>{{ String(i + 1).padStart(2, '0') }}</span><strong>{{ item.question }}</strong><small>{{ item.unanswerable ? 'Вне базы' : 'По README' }}</small></summary><div><p><b>Ожидание:</b> {{ item.unanswerable ? 'Не находить контекст; отказаться от ответа по базе.' : factText(item) }}</p><p v-if="item.source"><b>Источник:</b> {{ item.source }} · {{ item.section }}</p></div></details></div>
        <p class="caveat">Начальный порог {{ state?.defaults?.threshold ?? '—' }} проверяется на этом же контрольном наборе; это не независимая оценка. Попадание в раздел README и поиск слов — эвристики, они не подтверждают истинность всех утверждений. Query rewrite и фильтр могут улучшить результат или потерять полезный контекст: сравнение показывает наблюдаемые показатели.</p>
      </section>
    </main>
    <footer class="page-footer"><span>AI CHALLENGE / DAY 23</span><span>Локальная учебная демонстрация RAG</span></footer>
  </div>
</template>
