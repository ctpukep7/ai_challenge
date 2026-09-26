<script>
const requestCache = new Map()
const cacheLimit = 100
</script>

<script setup>
import { computed, ref, watch } from 'vue'
import { api } from './api'

const props = defineProps({
  scope: { type: String, required: true, validator: value => ['message', 'run'].includes(value) },
  id: { type: String, required: true },
})
const cacheKey = computed(() => `${props.scope}:${props.id}`)
const requests = ref(requestCache.get(cacheKey.value) ?? null)
const loading = ref(false)
const error = ref('')
const opened = ref(false)
let generation = 0

function json(value) { return JSON.stringify(value, null, 2) }
function protocol(value) { return value === 'deepseek' ? 'DeepSeek' : value === 'mcp' ? 'MCP' : value }
function hasResponse(item) { return Object.prototype.hasOwnProperty.call(item, 'response') }

async function load() {
  if (loading.value || requests.value !== null) return
  const currentGeneration = generation
  const currentKey = cacheKey.value
  const collection = props.scope === 'message' ? 'messages' : 'runs'
  const id = props.id
  loading.value = true
  error.value = ''
  try {
    const result = await api(`/api/${collection}/${encodeURIComponent(id)}/requests`)
    if (!Array.isArray(result.requests)) throw new Error('Не удалось прочитать JSON-запросы. Попробуйте ещё раз.')
    if (currentGeneration !== generation) return
    requests.value = result.requests
    requestCache.set(currentKey, result.requests)
    if (requestCache.size > cacheLimit) requestCache.delete(requestCache.keys().next().value)
  } catch (failure) {
    if (currentGeneration === generation) error.value = failure.message
  } finally {
    if (currentGeneration === generation) loading.value = false
  }
}
function toggle(event) {
  opened.value = event.target.open
  if (opened.value) load()
}
watch(cacheKey, key => {
  generation += 1
  requests.value = requestCache.get(key) ?? null
  loading.value = false
  error.value = ''
  if (opened.value) load()
})
</script>

<template>
  <details class="json-requests" @toggle="toggle">
    <summary>JSON-запросы</summary>
    <p class="json-note">Тела запросов к DeepSeek и MCP. Ответы показаны в нормализованном виде.</p>
    <p v-if="loading" class="json-note" role="status">Загружаем JSON-запросы…</p>
    <div v-else-if="error" class="json-load-error" role="alert">
      <p>{{ error }}</p>
      <button class="text-button" type="button" @click="load">Повторить</button>
    </div>
    <p v-else-if="requests?.length === 0" class="json-note">JSON-запросы для этого выполнения не сохранены.</p>
    <ol v-else-if="requests" class="json-step-list">
      <li v-for="(item, index) in requests" :key="index" class="json-step">
        <header class="json-step-heading"><span>Шаг {{ index + 1 }} · {{ protocol(item.protocol) }}</span><span :class="['json-result', { failed: !item.ok }]">{{ item.ok ? 'Успешно' : 'Ошибка' }}</span></header>
        <p class="json-label">{{ item.label }}</p>
        <h4>Запрос</h4>
        <pre tabindex="0" :aria-label="`JSON запроса, шаг ${index + 1}`">{{ json(item.request) }}</pre>
        <template v-if="hasResponse(item)">
          <h4>Ответ</h4>
          <pre tabindex="0" :aria-label="`JSON ответа, шаг ${index + 1}`">{{ json(item.response) }}</pre>
        </template>
        <p v-if="item.error" class="json-step-error">Ошибка: {{ item.error }}</p>
        <p v-else-if="!hasResponse(item)" class="json-note">Ответ не получен.</p>
      </li>
    </ol>
  </details>
</template>

<style scoped>
.json-requests { min-width: 0; margin: 10px 0; color: #586d83; }
.json-requests > summary { cursor: pointer; font-size: 12px; }
.json-note { margin: 12px 0; color: #7a899b; font-size: 11px; line-height: 1.6; }
.json-load-error { margin: 12px 0; color: #a53f43; font-size: 12px; }
.json-step-list { margin: 16px 0 0; padding: 0; list-style: none; }
.json-step { min-width: 0; margin: 12px 0; padding: 14px; border: 1px solid var(--line); border-radius: 10px; }
.json-step-heading { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; font-size: 11px; font-weight: 600; }
.json-result { color: #357d74; }
.json-result.failed, .json-step-error { color: #a53f43; }
.json-label { margin: 10px 0; overflow-wrap: anywhere; font-size: 12px; }
.json-step h4 { margin: 14px 0 6px; font-size: 11px; font-weight: 600; }
.json-step pre { max-width: 100%; max-height: 320px; margin: 6px 0; overflow-x: hidden; overflow-y: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; }
.json-step-error { margin: 12px 0 0; overflow-wrap: anywhere; font-size: 12px; }
@media (max-width: 600px) {
  .json-step { padding: 10px; }
  .json-step pre { max-height: 240px; padding: 10px; }
}
</style>
