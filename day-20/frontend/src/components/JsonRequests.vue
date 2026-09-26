<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../api'
import { json, statusLabel } from '../format'

const props = defineProps({ runId: { type: String, required: true }, active: Boolean })
const opened = ref(false)
const snapshot = ref(null)
const loading = ref(false)
const loadError = ref('')
const copyError = ref('')
const copiedId = ref('')
const exchanges = computed(() => snapshot.value?.exchanges || [])
let generation = 0
let timer
let disposed = false
let refreshAgain = false

async function refresh(force = false) {
  if (!opened.value || !props.runId || disposed) return
  if (loading.value) {
    if (force) refreshAgain = true
    return
  }
  const version = generation
  const selectedId = props.runId
  loading.value = true
  try {
    const result = await api(`/api/runs/${encodeURIComponent(selectedId)}/json-requests`)
    if (!disposed && version === generation && selectedId === props.runId && opened.value) {
      if (result.run_id !== selectedId) throw new Error('Сервер вернул снимки другого запуска. Повторите загрузку.')
      snapshot.value = result
      loadError.value = ''
    }
  } catch (error) {
    if (!disposed && version === generation) loadError.value = error.message
  } finally {
    if (!disposed && version === generation) {
      loading.value = false
      if (refreshAgain) {
        refreshAgain = false
        refresh()
      }
    }
  }
}
function duration(value) {
  return typeof value === 'number' ? `${(value / 1000).toFixed(1)} с` : 'В работе'
}
async function copy(exchange) {
  const version = generation
  copyError.value = ''
  copiedId.value = ''
  try {
    if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable')
    await navigator.clipboard.writeText(json({ request: exchange.request, response: exchange.response, error: exchange.error }))
    if (!disposed && version === generation) copiedId.value = exchange.id
  } catch {
    if (!disposed && version === generation) copyError.value = 'Не удалось скопировать JSON. Выделите текст блока и скопируйте его вручную.'
  }
}
function toggle(event) { opened.value = event.currentTarget.open }
watch(() => [props.runId, opened.value], ([id, open], previous) => {
  generation += 1
  loading.value = false
  loadError.value = ''
  copyError.value = ''
  copiedId.value = ''
  refreshAgain = false
  if (!previous || id !== previous[0]) snapshot.value = null
  if (open && id) refresh()
}, { immediate: true })
watch(() => props.active, () => { if (opened.value) refresh(true) })
onMounted(() => { timer = setInterval(() => { if (opened.value && props.active) refresh() }, 1000) })
onUnmounted(() => { disposed = true; generation += 1; clearInterval(timer) })
</script>

<template>
  <details class="json-requests" @toggle="toggle">
    <summary>JSON-запросы</summary>
    <div v-if="opened" class="json-content">
      <p class="json-note">Сохранённые снимки запросов без ключей, приватных полей календаря и внутренних рассуждений. Ответы нормализованы; MCP transport id не добавляется.</p>
      <p v-if="!runId" class="json-empty">Отправьте запрос или выберите сохранённый запуск, чтобы посмотреть JSON.</p>
      <p v-else-if="loading && !snapshot" class="json-loading" role="status"><span class="spinner"></span> Загружаем JSON выбранного запуска…</p>
      <div v-if="loadError" class="json-error" role="alert"><p>{{ loadError }}</p><button class="secondary" :disabled="loading" @click="refresh">Повторить загрузку</button></div>
      <p v-if="copyError" class="inline-error" role="alert">{{ copyError }}</p>
      <p v-if="snapshot && !snapshot.capture_available" class="json-empty">{{ active ? 'Записанных обменов пока нет.' : 'Для этого запуска снимки JSON не записаны. Старые запросы не восстанавливаются из трассы инструментов.' }}</p>
      <p v-else-if="snapshot && !exchanges.length" class="json-empty">Записанных обменов пока нет.</p>
      <ol v-if="exchanges.length" class="json-exchanges">
        <li v-for="exchange in exchanges" :key="exchange.id" class="json-exchange">
            <header class="json-exchange-heading">
              <span><strong>Шаг {{ exchange.sequence }} · {{ exchange.kind === 'model' ? 'Модель' : 'MCP' }}<template v-if="exchange.server_id"> · {{ exchange.server_id }}</template></strong><code v-if="exchange.alias">{{ exchange.alias }}</code><small v-if="exchange.trace_sequence">Шаг трассы {{ exchange.trace_sequence }}</small></span>
              <span class="json-exchange-meta"><span class="status" :class="exchange.status">{{ statusLabel(exchange.status) }}</span><small>{{ duration(exchange.duration_ms) }}</small></span>
            </header>
            <div class="json-exchange-body">
              <p v-if="exchange.truncated" class="json-bounded">Снимок сокращён: размер сохраняемых данных ограничен.</p>
              <dl v-if="exchange.tool_call_id" class="json-identifiers"><dt>tool_call_id</dt><dd><code>{{ exchange.tool_call_id }}</code></dd></dl>
              <div class="json-block-heading"><h3>Запрос</h3><button class="text-button" @click="copy(exchange)">{{ copiedId === exchange.id ? 'Скопировано' : 'Скопировать JSON' }}</button></div>
              <pre v-if="exchange.request !== null" class="json-code" tabindex="0">{{ json(exchange.request) }}</pre>
              <p v-else class="json-empty">Тело запроса недоступно.</p>
              <h3>Нормализованный ответ</h3>
              <pre v-if="exchange.response !== null" class="json-code" tabindex="0">{{ json(exchange.response) }}</pre>
              <p v-else class="json-empty">{{ exchange.status === 'running' ? 'Ответ ещё не получен.' : 'Ответ не записан.' }}</p>
              <p v-if="exchange.error" class="inline-error">Ошибка: {{ exchange.error }}</p>
            </div>
        </li>
      </ol>
      <p v-if="runId && !loadError" class="json-refresh-note">{{ active ? 'Обновляется каждую секунду, пока список раскрыт.' : 'Снимки этого запуска.' }}<button class="text-button" :disabled="loading" @click="refresh">{{ loading ? 'Обновляем…' : 'Обновить' }}</button></p>
    </div>
  </details>
</template>

<style scoped>
.json-requests { width: 100%; max-width: 100%; min-width: 0; margin: 10px 0 0; color: #586d83; }
.json-requests > summary { cursor: pointer; font-size: 11px; line-height: 1.6; }
.json-content { min-width: 0; padding-top: 3px; }
.json-note { margin: 12px 0; color: #8796a4; font-size: 10px; line-height: 1.7; }
.json-empty, .json-loading { color: #7e8d9b; font-size: 10px; margin-bottom: 13px; }
.json-loading { display: flex; align-items: center; gap: 9px; }
.json-error p { font-size: 11px; color: #a05b57; overflow-wrap: anywhere; }
.json-exchanges { margin: 14px 0 0; padding: 0; list-style: none; }
.json-exchange { min-width: 0; margin: 12px 0; padding: 14px; border: 1px solid var(--line); border-radius: 10px; }
.json-exchange-heading { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; align-items: start; }
.json-exchange-heading > span { min-width: 0; }
.json-exchange-heading strong { display: block; color: #486577; font-size: 11px; }
.json-exchange-heading code { display: block; margin-top: 6px; color: #7a8e9b; font-size: 9px; overflow-wrap: anywhere; }
.json-exchange-heading small { display: block; color: #8796a4; font-size: 9px; font-weight: 400; margin-top: 6px; }
.json-exchange-meta { text-align: right; flex-shrink: 0; }
.json-exchange-body { padding-top: 3px; min-width: 0; }
.json-exchange-body h3 { font-size: 10px; font-weight: 600; color: #657d8f; margin: 13px 0 7px; }
.json-block-heading { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px; }
.json-block-heading h3 { margin-bottom: 7px; }
.json-code { margin: 0 0 11px; max-width: 100%; max-height: 320px; overflow-x: hidden; overflow-y: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 10px; line-height: 1.65; }
.json-code:focus-visible { outline: 2px solid #67b8b0; outline-offset: 2px; }
.json-bounded { padding: 8px 10px; background: #fff5e2; border-radius: 5px; color: #977844; font-size: 9px; margin: 10px 0 0; }
.json-identifiers { margin: 11px 0; }
.json-identifiers dd { font-size: 9px; }
.json-refresh-note { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; margin: 10px 0 0; color: #8a9ca8; font-size: 9px; }
@media (max-width: 600px) { .json-exchange { padding: 10px; } .json-code { max-height: 240px; padding: 10px; } }
</style>
