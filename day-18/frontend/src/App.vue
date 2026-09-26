<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { api } from './api'
import JsonRequests from './JsonRequests.vue'

const state = ref(null)
const tab = ref('agent')
const loading = ref(true)
const pollError = ref('')
const actionError = ref('')
const failedChat = ref(false)
const busy = ref(false)
const composer = ref('')
const composerElement = ref(null)
const messagesElement = ref(null)
const skill = ref('')
const skillError = ref('')
const skillLoading = ref(false)
let draftRequest = null
let revision = 0
let pollInFlight = false
let interval
let disposed = false

const jobs = computed(() => state.value?.jobs || [])
const currentJobs = computed(() => jobs.value.filter(job => job.status !== 'cancelled'))
const archivedJobs = computed(() => jobs.value.filter(job => job.status === 'cancelled'))
const messages = computed(() => state.value?.messages || [])
const samples = computed(() => state.value?.samples || [])
const runs = computed(() => state.value?.runs || [])
const activeCount = computed(() => jobs.value.filter(job => job.status === 'active').length)
const timezone = computed(() => state.value?.config?.timezone || 'UTC')
const latest = computed(() => samples.value[0] || null)
const cronStatus = computed(() => state.value?.scheduler?.installed === true ? 'Cron установлен' : state.value?.scheduler?.installed === false ? 'Cron не установлен' : 'Статус cron недоступен')
const statusNames = { active: 'Активно', paused: 'На паузе', cancelled: 'Удалено', running: 'Выполняется', success: 'Сохранено', error: 'Ошибка', abandoned: 'Прервано' }
const examples = [
  'Каждые полчаса собирай погоду в Омске',
  'Собирай погоду в Москве каждый день в 9:00 по московскому времени',
]
const managementExamples = [
  'Останови сбор погоды в Омске',
  'Возобнови сбор погоды в Омске',
  'Удали расписание для Омска',
]

function date(value, zone = timezone.value) {
  if (!value) return 'Пока нет'
  try {
    return new Intl.DateTimeFormat('ru-RU', {
      day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
      timeZone: zone,
    }).format(new Date(value))
  } catch { return String(value) }
}
function number(value, unit = '') {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(value)}${unit}`
    : '—'
}
function schedule(job) {
  if (job.schedule_type === 'interval') return `Каждые ${job.interval_minutes} мин`
  return `По расписанию ${job.cron_expression || ''}`
}
function traceText(value) { return typeof value === 'string' ? value : JSON.stringify(value, null, 2) }
function hasTrace(value) { return value && (Array.isArray(value) ? value.length > 0 : Object.keys(value).length > 0) }
function source(sample) { return sample.source || 'Open-Meteo' }
function applyState(value) {
  if (value && !disposed) state.value = value
}
async function poll() {
  if (pollInFlight || busy.value || disposed) return
  const generation = revision
  pollInFlight = true
  try {
    const value = await api('/api/state')
    if (generation === revision && !busy.value && !disposed) {
      applyState(value)
      pollError.value = ''
    }
  } catch (error) {
    if (generation === revision && !disposed) pollError.value = error.message
  } finally {
    pollInFlight = false
    if (!disposed) loading.value = false
  }
}
async function fillExample(example) {
  composer.value = example
  await nextTick()
  composerElement.value?.focus()
}
async function scrollMessages() {
  await nextTick()
  if (messagesElement.value) messagesElement.value.scrollTop = messagesElement.value.scrollHeight
}
async function send() {
  const prompt = composer.value.trim()
  if (!prompt || busy.value || !state.value?.config?.configured) return
  if (!draftRequest || draftRequest.prompt !== prompt) draftRequest = { prompt, request_id: crypto.randomUUID() }
  busy.value = true
  revision += 1
  actionError.value = ''
  failedChat.value = false
  try {
    const result = await api('/api/chat', { method: 'POST', body: JSON.stringify(draftRequest) })
    applyState(result.state)
    composer.value = ''
    draftRequest = null
    await scrollMessages()
  } catch (error) {
    actionError.value = error.message
    failedChat.value = true
    // Keep the same request id when a response is lost; the server can deduplicate it.
  } finally {
    busy.value = false
    revision += 1
  }
}
async function changeStatus(job, status) {
  if (busy.value) return
  busy.value = true
  revision += 1
  actionError.value = ''
  failedChat.value = false
  try {
    const result = await api(`/api/jobs/${encodeURIComponent(job.id)}/status`, {
      method: 'POST', body: JSON.stringify({ status }),
    })
    applyState(result.state)
  } catch (error) { actionError.value = error.message }
  finally {
    busy.value = false
    revision += 1
  }
}
async function deleteJob(job) {
  if (busy.value) return
  busy.value = true
  revision += 1
  actionError.value = ''
  failedChat.value = false
  try {
    const result = await api(`/api/jobs/${encodeURIComponent(job.id)}/delete`, { method: 'POST' })
    applyState(result.state)
  } catch (error) { actionError.value = error.message }
  finally {
    busy.value = false
    revision += 1
  }
}
function keydown(event) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    send()
  }
}
async function loadSkill(event) {
  if (!event.target.open || skill.value || skillLoading.value) return
  skillLoading.value = true
  skillError.value = ''
  try {
    const result = await api('/api/skill')
    skill.value = result.content || result.body || result.skill?.content || (typeof result === 'string' ? result : JSON.stringify(result, null, 2))
  } catch (error) { skillError.value = error.message }
  finally { skillLoading.value = false }
}
onMounted(async () => {
  await poll()
  await scrollMessages()
  interval = setInterval(poll, 3000)
})
onUnmounted(() => {
  disposed = true
  clearInterval(interval)
})
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <a class="brand" href="#" @click.prevent="tab = 'agent'" aria-label="День 18 — агент">
        <span class="day-mark">18</span>
        <span><strong>AI Challenge</strong><small>Сбор погоды по расписанию</small></span>
      </a>
      <nav aria-label="Разделы приложения" class="tabs">
        <button :class="{ active: tab === 'agent' }" :aria-current="tab === 'agent' ? 'page' : undefined" @click="tab = 'agent'">Агент</button>
        <button :class="{ active: tab === 'data' }" :aria-current="tab === 'data' ? 'page' : undefined" @click="tab = 'data'">Данные<span v-if="state" class="tab-count">{{ state.total_samples || 0 }}</span></button>
      </nav>
      <span class="model-label">{{ state?.config?.model || 'DeepSeek' }}</span>
    </header>

    <main>
      <div v-if="loading && !state" class="initial-state" role="status">Подключаемся к агенту…</div>
      <div v-if="pollError" class="notice error connection-error" role="alert">
        <span>{{ pollError }}<small v-if="state">Показаны последние полученные данные.</small></span>
        <button class="text-button" @click="poll" :disabled="busy">Повторить</button>
      </div>
      <template v-if="state">
        <div v-if="!state.config?.configured" class="notice config-notice">
          <strong>DeepSeek ещё не подключён</strong>
          <span>Сервис агента ещё не настроен. После подключения DeepSeek здесь можно будет создавать расписания и выполнять сбор.</span>
        </div>
        <div v-if="actionError" class="notice error" role="alert">{{ actionError }}<small v-if="failedChat">Текст запроса сохранён. Вы можете повторить отправку.</small></div>

        <template v-if="tab === 'agent'">
          <section class="page-heading">
            <div><span class="eyebrow">ДЕНЬ 18 / ФОНОВЫЕ ЗАДАЧИ</span><h1>Поручите агенту собирать погоду</h1><p>Задайте город и периодичность. Агент сохранит расписание, а новые записи появятся в разделе «Данные».</p></div>
            <span class="heading-note"><b>{{ activeCount }}</b> активных расписаний</span>
          </section>
          <div class="agent-layout">
            <section class="chat-panel" aria-labelledby="chat-title">
              <header class="panel-heading"><h2 id="chat-title">Диалог с агентом</h2><span>Город · периодичность</span></header>
              <div class="messages" ref="messagesElement" :aria-busy="busy">
                <div v-if="!messages.length" class="chat-empty">
                  <span class="empty-kicker">ОДНО ПОРУЧЕНИЕ — РЕГУЛЯРНЫЙ СБОР</span>
                  <h3>С какого города начнём?</h3>
                  <p>Напишите, где и как часто получать погоду. Например, каждые полчаса или ежедневно в определённое время.</p>
                  <div class="example-list"><button v-for="example in examples" :key="example" class="example" @click="fillExample(example)" :disabled="busy">{{ example }}</button></div>
                  <small>Пример заполнит поле. Отправка — только по вашему нажатию.</small>
                </div>
                <article v-for="message in messages" :key="message.id" :class="['message', message.role === 'user' ? 'user' : 'assistant']">
                  <span class="message-label">{{ message.role === 'user' ? 'Вы' : 'Агент' }}</span>
                  <div class="bubble">{{ message.content }}</div>
                  <details v-if="hasTrace(message.trace)" class="trace"><summary>Действия агента</summary><pre>{{ traceText(message.trace) }}</pre></details>
                  <JsonRequests v-if="message.role === 'assistant' && message.has_json_requests" scope="message" :id="message.id" />
                </article>
                <div v-if="busy" class="working" role="status">Агент обрабатывает запрос…</div>
              </div>
              <form class="composer-wrap" @submit.prevent="send">
                <label for="prompt" class="sr-only">Поручение агенту</label>
                <div class="composer">
                  <textarea id="prompt" ref="composerElement" v-model="composer" @keydown="keydown" placeholder="Каждые полчаса собирай погоду в Омске" rows="3" :disabled="busy" maxlength="2000"></textarea>
                  <button class="primary" type="submit" :disabled="busy || !composer.trim() || !state.config?.configured">{{ busy ? 'Обработка…' : 'Отправить' }}</button>
                </div>
                <span class="composer-hint">Enter — отправить · Shift + Enter — новая строка</span>
              </form>
            </section>

            <aside class="schedule-panel" aria-labelledby="schedules-title">
              <div class="section-title"><h2 id="schedules-title">Расписания</h2><span>{{ currentJobs.length }}</span></div>
              <div v-if="!currentJobs.length" class="schedule-empty"><h3>Текущих расписаний пока нет</h3><p>После нового поручения здесь появятся город, время следующего сбора и управление задачей.</p></div>
              <article v-for="job in currentJobs" :key="job.id" class="job-card">
                <header><h3>{{ job.label }}<small v-if="job.country && !job.label.includes(job.country)">{{ job.country }}</small></h3><span :class="['status', job.status]">{{ statusNames[job.status] || job.status }}</span></header>
                <p class="job-schedule">{{ schedule(job) }}</p>
                <span class="zone">{{ job.timezone }}</span>
                <dl class="job-times"><div><dt>Следующий сбор</dt><dd>{{ job.status === 'active' ? date(job.next_run_at, job.timezone) : 'Не запланирован' }}</dd></div><div><dt>Последний запуск</dt><dd>{{ date(job.last_run_at, job.timezone) }}</dd></div></dl>
                <p v-if="job.last_error" class="job-error">{{ job.last_error }}</p>
                <div class="job-actions">
                  <button v-if="job.status === 'active'" class="secondary" :disabled="busy" @click="changeStatus(job, 'paused')">Пауза</button>
                  <button v-if="job.status === 'paused'" class="secondary" :disabled="busy" @click="changeStatus(job, 'active')">Продолжить</button>
                  <button class="text-button cancel" :disabled="busy" @click="deleteJob(job)">Удалить</button>
                </div>
              </article>
              <details class="management-details">
                <summary>Управление расписаниями</summary>
                <p>Остановить, возобновить или удалить расписание можно кнопкой в карточке или поручением агенту.</p>
                <div class="example-list"><button v-for="example in managementExamples" :key="example" class="example" @click="fillExample(example)" :disabled="busy">{{ example }}</button></div>
                <small>Пример заполнит поле. Отправьте поручение, когда будете готовы.</small>
              </details>
              <details v-if="archivedJobs.length" class="archived-schedules">
                <summary>Удалённые расписания · {{ archivedJobs.length }}</summary>
                <p>Сбор по этим расписаниям прекращён. Ранее полученные данные и история запусков сохранены.</p>
                <article v-for="job in archivedJobs" :key="job.id" class="archived-job">
                  <div><h3>{{ job.label }}</h3><span class="status cancelled">{{ statusNames[job.status] }}</span></div>
                  <p>{{ schedule(job) }} · {{ job.timezone }}</p>
                </article>
              </details>
              <section class="scheduler-section" aria-labelledby="cron-title">
                <div class="section-title"><h2 id="cron-title">Запуск по расписанию</h2><span :class="['status', state.scheduler?.installed ? 'active' : 'paused']">{{ cronStatus }}</span></div>
                <p>{{ state.scheduler?.installed === true ? 'Cron проверяет задачи каждую минуту. Компьютер должен быть включён и подключён к сети.' : state.scheduler?.installed === false ? 'Расписания сохранены, но автоматический запуск ещё не подключён.' : 'Не удалось проверить автоматический запуск. Время последней проверки показывает, когда планировщик обращался к задачам.' }}</p>
                <dl class="scheduler-times"><div><dt>Последняя проверка</dt><dd>{{ date(state.scheduler?.last_tick) }}</dd></div><div><dt>Последнее выполнение</dt><dd>{{ date(state.scheduler?.last_execution) }}</dd></div></dl>
                <details class="setup-details"><summary>Как работает автоматический сбор</summary><p>Планировщик проверяет сохранённые расписания каждую минуту и запускает агента, когда наступает время сбора. Агент получает погоду и сохраняет новую запись в разделе «Данные».</p><p>Сбор продолжится при закрытой странице. Для приостановки используйте кнопку «Пауза» в расписании.</p></details>
              </section>
              <details class="skill-details" @toggle="loadSkill"><summary>Инструкция, которую читает агент</summary><p v-if="skillLoading" role="status">Загружаем инструкцию…</p><p v-if="skillError" class="job-error">{{ skillError }}</p><pre v-if="skill">{{ skill }}</pre></details>
            </aside>
          </div>
        </template>

        <template v-else>
          <section class="page-heading"><div><span class="eyebrow">СОХРАНЁННЫЕ НАБЛЮДЕНИЯ</span><h1>Данные о погоде</h1><p>Реальные записи, полученные агентом по расписанию. Время показано в {{ timezone }}.</p></div></section>
          <div class="data-overview">
            <section class="record-count"><span class="eyebrow">ВСЕГО СОХРАНЕНО</span><strong>{{ state.total_samples || 0 }}</strong><span>записей погоды</span></section>
            <section v-if="latest" class="latest-record" aria-label="Последняя запись"><div><span class="eyebrow">ПОСЛЕДНЯЯ ЗАПИСЬ</span><h2>{{ latest.label }}</h2><span class="muted">{{ date(latest.observed_at) }} · {{ source(latest) }}</span></div><dl><div><dt>Температура</dt><dd>{{ number(latest.temperature, ' °C') }}</dd></div><div><dt>Влажность</dt><dd>{{ number(latest.humidity, ' %') }}</dd></div><div><dt>Ветер</dt><dd>{{ number(latest.wind_speed, ' км/ч') }}</dd></div></dl></section>
            <section v-else class="latest-empty"><h2>Ожидаем первую запись</h2><p>Здесь появится погода после успешного запуска по расписанию.</p></section>
          </div>
          <section class="records-panel" aria-labelledby="records-title"><header class="panel-heading"><h2 id="records-title">История наблюдений</h2><span>Последние {{ samples.length }} из {{ state.total_samples || 0 }} записей</span></header>
            <div v-if="!samples.length" class="data-empty"><h3>Данные ещё не собраны</h3><p>Создайте расписание в разделе «Агент». После успешного сбора новые записи появятся здесь без перезагрузки.</p><button class="secondary" @click="tab = 'agent'">Перейти к агенту</button></div>
            <div v-else class="table-scroll" tabindex="0" aria-label="Таблица наблюдений, можно прокручивать горизонтально"><table><thead><tr><th>Город</th><th>Время наблюдения</th><th>Температура</th><th>Влажность</th><th>Ветер</th><th>Получено</th><th>Источник</th></tr></thead><tbody><tr v-for="sample in samples" :key="sample.id"><td class="table-city">{{ sample.label }}</td><td>{{ date(sample.observed_at) }}</td><td>{{ number(sample.temperature, ' °C') }}</td><td>{{ number(sample.humidity, ' %') }}</td><td>{{ number(sample.wind_speed, ' км/ч') }}</td><td>{{ date(sample.fetched_at) }}</td><td>{{ source(sample) }}</td></tr></tbody></table></div>
          </section>
          <section v-if="runs.length" class="run-history"><div class="section-title"><h2>Последние запуски</h2><span>Ошибки не попадают в данные погоды</span></div><details v-for="run in runs.slice(0, 15)" :key="run.id" class="run-row"><summary><span>{{ jobs.find(job => job.id === run.job_id)?.label || 'Сбор погоды' }}<small>{{ date(run.started_at || run.scheduled_for) }}</small></span><span :class="['status', run.status]">{{ statusNames[run.status] || run.status }}</span></summary><p v-if="run.error" class="job-error">{{ run.error }}</p><pre v-if="hasTrace(run.trace)">{{ traceText(run.trace) }}</pre><p v-else class="muted">Подробности действий отсутствуют.</p><JsonRequests v-if="run.has_json_requests" scope="run" :id="run.id" /></details></section>
        </template>
      </template>
    </main>
    <footer class="footer"><span>День 18 · MCP и периодический сбор данных</span><span v-if="state">{{ timezone }} · обновление каждые 3 секунды</span></footer>
  </div>
</template>
