<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { api } from './api'
import { date, statusLabel } from './format'
import ServerCatalog from './components/ServerCatalog.vue'
import TracePanel from './components/TracePanel.vue'
import PlanCard from './components/PlanCard.vue'
import MarkdownMessage from './components/MarkdownMessage.vue'
import JsonRequests from './components/JsonRequests.vue'
import { latestRun, prepareChatRequest, restorePending } from './chat-request'
import { jsonMessageOwners, jsonRunActive } from './message-json'

const state = ref(null)
const run = ref(null)
const runId = ref('')
const composer = ref('')
const loading = ref(true)
const submitting = ref(false)
const connectionError = ref('')
const actionError = ref('')
const planErrors = ref({})
const pendingPlans = ref(new Set())
const messageArea = ref(null)
const composerElement = ref(null)
const pendingRequest = ref(null)
const terminalStatuses = new Set(['completed', 'incomplete', 'error', 'interrupted'])
const activeRun = computed(() => run.value && !terminalStatuses.has(run.value.status))
const busy = computed(() => submitting.value || Boolean(activeRun.value))
const modelReady = computed(() => state.value?.config?.model_ready === true)
const plans = computed(() => state.value?.plans || [])
const messages = computed(() => state.value?.messages || [])
const runs = computed(() => state.value?.runs || [])
const jsonOwners = computed(() => jsonMessageOwners(messages.value))
let timer
let polling = false
let disposed = false
let selectionVersion = 0

const examples = [
  { title: 'Поездка по погоде', description: 'Погода + календарь + сохранённый план', prompt: 'Найди время для трёхчасовой поездки в Омске в ближайшие семь дней, между 10:00 и 18:00 по омскому времени. Нужно не ниже +5 °C, вероятность осадков до 30% и ветер до 8 м/с. Проверь мой календарь, сохрани план и предложи событие.' },
  { title: 'Только погода', description: 'Проверить выбор Weather', prompt: 'Покажи почасовой прогноз погоды в Омске на завтра: температуру, вероятность осадков и ветер.' },
  { title: 'Только календарь', description: 'Проверить выбор Calendar', prompt: 'Покажи мои календари и помоги найти свободные интервалы на завтра с 10:00 до 18:00 по омскому времени. Если календарей несколько, сначала уточни выбор.' },
]

function rememberRun(id) {
  try { sessionStorage.setItem('day20:lastRun', id) } catch { /* Session storage is optional. */ }
}
function rememberPending(value) {
  try {
    if (value) sessionStorage.setItem('day20:pendingRequest', JSON.stringify(value))
    else sessionStorage.removeItem('day20:pendingRequest')
  } catch { /* UUID remains stable in memory when storage is unavailable. */ }
}
async function scrollMessages() {
  await nextTick()
  if (messageArea.value) messageArea.value.scrollTop = messageArea.value.scrollHeight
}
function messageJsonActive(id) { return jsonRunActive(id, runs.value, plans.value, pendingPlans.value, run.value) }
async function refreshState() {
  const result = await api('/api/state')
  if (!disposed) state.value = result
}
function mergePlan(plan) {
  if (!plan || !state.value) return
  const current = state.value.plans || []
  state.value = { ...state.value, plans: [plan, ...current.filter(item => item.id !== plan.id)] }
  if (run.value?.plan?.id === plan.id) run.value = { ...run.value, plan }
}
async function refreshSelectedRun() {
  const selectedId = runId.value
  const version = selectionVersion
  if (!selectedId) return
  const current = await api(`/api/runs/${encodeURIComponent(selectedId)}`)
  if (!disposed && version === selectionVersion && selectedId === runId.value) {
    run.value = current
    mergePlan(current.plan)
  }
}
async function poll() {
  if (polling || disposed || submitting.value || loading.value) return
  const pendingCalendarWrite = pendingPlans.value.size > 0 || state.value?.plans?.some(plan => ['approved', 'writing'].includes(plan.status))
  const needsState = Boolean(connectionError.value) || !state.value || pendingCalendarWrite
  if (!activeRun.value && !needsState) return
  polling = true
  const version = selectionVersion
  try {
    if (runId.value && activeRun.value) {
      const current = await api(`/api/runs/${encodeURIComponent(runId.value)}`)
      if (!disposed && version === selectionVersion) {
        run.value = current
        mergePlan(current.plan)
        if (terminalStatuses.has(current.status)) {
          await refreshState()
          await scrollMessages()
        }
      }
    }
    if (needsState) {
      await refreshState()
      if (pendingCalendarWrite && !activeRun.value) await refreshSelectedRun()
      if (!run.value && runs.value.length) {
        const active = runs.value.find(item => ['queued', 'running'].includes(item.status))
        await selectRun(active?.id || latestRun(runs.value).id)
      }
    }
    connectionError.value = ''
  } catch (error) {
    if (!disposed) connectionError.value = error.message
  } finally { polling = false }
}
async function initialize() {
  loading.value = true
  connectionError.value = ''
  try {
    await refreshState()
    let savedRun = ''
    try {
      savedRun = sessionStorage.getItem('day20:lastRun') || ''
      pendingRequest.value = restorePending(JSON.parse(sessionStorage.getItem('day20:pendingRequest') || 'null'))
      if (pendingRequest.value) {
        composer.value = pendingRequest.value.prompt
        actionError.value = 'Предыдущая отправка не получила ответа. Повторите её: идентификатор запроса сохранён.'
      }
    } catch { pendingRequest.value = null }
    const active = runs.value.find(item => ['queued', 'running'].includes(item.status))
    const latest = active?.id || (runs.value.some(item => item.id === savedRun) ? savedRun : '') || latestRun(runs.value)?.id
    if (latest) await selectRun(latest)
    await scrollMessages()
  } catch (error) { connectionError.value = error.message }
  finally { loading.value = false }
}
async function selectRun(id) {
  if (!id || (busy.value && id !== runId.value)) return
  const version = ++selectionVersion
  try {
    const current = await api(`/api/runs/${encodeURIComponent(id)}`)
    if (!disposed && version === selectionVersion) {
      runId.value = id
      run.value = current
      rememberRun(id)
    }
  } catch (error) { if (!disposed) connectionError.value = error.message }
}
async function fillExample(example) {
  if (busy.value) return
  composer.value = example.prompt
  await nextTick()
  composerElement.value?.focus()
}
async function send() {
  const prompt = composer.value.trim()
  if (!prompt || busy.value || !modelReady.value) return
  pendingRequest.value = prepareChatRequest(pendingRequest.value, prompt, () => crypto.randomUUID())
  const request = pendingRequest.value
  rememberPending(pendingRequest.value)
  submitting.value = true
  actionError.value = ''
  connectionError.value = ''
  selectionVersion += 1
  try {
    const result = await api('/api/chat', { method: 'POST', body: JSON.stringify(request) })
    runId.value = result.run_id
    run.value = { id: result.run_id, status: 'queued', trace: [], answer: '', plan: null, prompt, calendar_id: request.calendar_id || null }
    rememberRun(result.run_id)
    composer.value = ''
    pendingRequest.value = null
    rememberPending(null)
    await refreshState()
    await scrollMessages()
  } catch (error) { actionError.value = error.message }
  finally {
    submitting.value = false
    if (!disposed) await poll()
  }
}
async function planAction(plan, action) {
  if (pendingPlans.value.has(plan.id)) return
  pendingPlans.value = new Set([...pendingPlans.value, plan.id])
  planErrors.value = { ...planErrors.value, [plan.id]: '' }
  try {
    const result = await api(`/api/plans/${encodeURIComponent(plan.id)}/${action}`, { method: 'POST' })
    mergePlan(result)
    await refreshState()
    await refreshSelectedRun()
  } catch (error) {
    planErrors.value = { ...planErrors.value, [plan.id]: error.message }
    // A lost HTTP response does not prove a failed calendar write. Fetch persisted state.
    try {
      await refreshState()
      await refreshSelectedRun()
    } catch { /* Keep the action error visible until connectivity returns. */ }
  } finally {
    pendingPlans.value = new Set([...pendingPlans.value].filter(id => id !== plan.id))
  }
}
onMounted(() => {
  initialize()
  timer = setInterval(poll, 1000)
})
onUnmounted(() => { disposed = true; clearInterval(timer) })
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <a class="brand" href="/" aria-label="День 20, главная"><span class="day-mark">20</span><span><strong>AI CHALLENGE</strong><small>Orchestration MCP</small></span></a>
      <span class="topbar-section">Поездка с агентом</span>
      <span class="model-label">{{ state?.config?.model || 'deepseek-flash' }} <span class="model-dot" :class="{ ready: modelReady }"></span></span>
    </header>
    <main>
      <div class="page-heading">
        <div><span class="eyebrow">ДЕНЬ 20 / ОРКЕСТРАЦИЯ</span><h1>Один запрос. Несколько инструментов.</h1><p>Агент сверит погоду и календарь, выберет время и сохранит план поездки.</p></div>
        <div class="heading-count"><strong>03</strong><span>MCP-сервера<br>в одном флоу</span></div>
      </div>
      <div v-if="connectionError" class="notice error connection-error" role="alert"><span>{{ connectionError }}</span><button class="secondary" @click="initialize">Обновить</button></div>
      <div v-if="state && !modelReady" class="notice" role="status"><strong>Модель не настроена</strong><span>Добавьте собственный DEEPSEEK_API_KEY в day-20/.env и перезапустите приложение.</span></div>
      <div v-if="state && !state.config?.calendar_ready" class="notice" role="status"><strong>Календарь не настроен</strong><span>Укажите логин и пароль приложения Яндекса в day-20/.env. Погода доступна независимо от календаря.</span></div>
      <div v-if="loading && !state" class="initial-state"><span class="spinner"></span><p>Подключаем MCP-серверы…</p></div>
      <div v-else-if="state" class="workspace">
        <section class="panel chat-panel" aria-labelledby="chat-heading">
          <div class="section-heading"><h2 id="chat-heading">Чат с агентом</h2><span class="subtle">{{ busy ? 'Выполняется запрос' : 'Готов к задаче' }}</span></div>
          <div ref="messageArea" class="messages" aria-live="polite">
            <div v-if="!messages.length" class="chat-empty"><span class="empty-icon" aria-hidden="true">↗</span><span class="eyebrow">ПЛАНИРУЕМ ВМЕСТЕ</span><h3>Когда лучше выбраться<br>из города?</h3><p>Укажите город, длительность и погодные условия. Агент найдёт свободное время и предложит конкретный план.</p><div class="example-list"><button v-for="example in examples" :key="example.title" class="example" :disabled="busy" @click="fillExample(example)"><span><strong>{{ example.title }}</strong><small>{{ example.description }}</small></span><span aria-hidden="true">↗</span></button></div><small class="subtle">Пример заполнит поле ввода. Отправка — по кнопке.</small></div>
            <article v-for="message in messages" :key="message.id" class="message" :class="message.role">
              <span class="message-label">{{ message.role === 'user' ? 'Вы' : 'Агент' }}</span>
              <div class="bubble"><MarkdownMessage :content="message.content" /></div>
              <JsonRequests v-if="jsonOwners.has(message.id)" :run-id="message.run_id" :active="messageJsonActive(message.run_id)" />
            </article>
            <div v-if="busy" class="working"><span class="spinner"></span> Агент работает с инструментами. Вызовы появляются в трассе.</div>
          </div>
          <div class="composer-wrap">
            <div v-if="messages.length" class="quick-examples"><button v-for="example in examples" :key="example.title" :disabled="busy" @click="fillExample(example)">{{ example.title }}</button></div>
            <form class="composer" @submit.prevent="send"><label class="sr-only" for="prompt">Задача для агента</label><textarea id="prompt" ref="composerElement" v-model="composer" :disabled="busy" placeholder="Например, спланируй поездку в Омске на три часа…" rows="3" @keydown.ctrl.enter.prevent="send" @keydown.meta.enter.prevent="send"></textarea><button class="primary send-button" type="submit" :disabled="!composer.trim() || busy || !modelReady">{{ submitting ? 'Отправляем…' : busy ? 'В работе…' : 'Отправить' }} <span v-if="!busy" aria-hidden="true">↗</span></button></form>
            <p v-if="actionError" class="inline-error" role="alert">{{ actionError }}</p>
            <span class="composer-hint">Ctrl / ⌘ + Enter — отправить · Событие создаётся только после подтверждения карточки</span>
          </div>
        </section>
        <aside class="right-column">
          <ServerCatalog :servers="state.registry || []" />
          <label v-if="runs.length > 1" class="run-picker">Трасса запроса<select :value="runId" :disabled="busy" @change="selectRun($event.target.value)"><option v-for="item in runs" :key="item.id" :value="item.id">{{ item.prompt?.slice(0, 55) || date(item.created_at) || item.id.slice(0, 8) }} · {{ statusLabel(item.status) }}</option></select></label>
          <TracePanel :run="run" :loading="busy" />
        </aside>
        <section class="plans-section" aria-labelledby="plans-heading"><div class="section-heading"><div><h2 id="plans-heading">Сохранённые планы</h2><p class="section-caption">Проверьте детали перед созданием события</p></div><span class="subtle">{{ plans.length }} {{ plans.length === 1 ? 'проект' : 'проектов' }}</span></div><div v-if="!plans.length" class="plans-empty"><span aria-hidden="true">▤</span><div><h3>Здесь будет план поездки</h3><p>Он появится, когда агент проверит прогноз и свободное время и сохранит подходящий вариант.</p></div></div><div v-else class="plan-grid"><PlanCard v-for="plan in plans" :key="plan.id" :plan="plan" :busy="pendingPlans.has(plan.id)" :error="planErrors[plan.id] || ''" @confirm="planAction($event, 'confirm')" @reject="planAction($event, 'reject')" /></div></section>
      </div>
    </main>
    <footer><span>AI Challenge · День 20</span><span>Weather → Calendar → Plans · {{ state?.config?.timezone || 'Asia/Omsk' }}</span></footer>
  </div>
</template>
