<script setup>
import { computed } from 'vue'
import JsonBlock from './JsonBlock.vue'

const props = defineProps({ stage: { type: Object, required: true }, run: { type: Object, default: null } })
defineEmits(['close'])

const roles = {
  search: 'Агент выбирает поисковый запрос и вызывает MCP search. Сервер обращается к Tavily и сохраняет снимок найденных источников с идентификатором и SHA-256.',
  summarize: 'Агент передаёт только search_result_id. MCP-сервер загружает сохранённый снимок поиска и отправляет его в отдельный запрос DeepSeek без инструментов. Готовая сводка получает свой ID и SHA-256.',
  saveToFile: 'Агент передаёт только summary_id. MCP-сервер загружает готовую сводку и сохраняет её точные UTF-8 байты в Markdown на сервере. Скачивание на ваш компьютер — отдельное действие.',
}
const kindNames = { mcp: 'MCP · вызов инструмента', deepseek_agent: 'DeepSeek · решение агента', deepseek_summary: 'DeepSeek · создание сводки', tavily: 'Tavily · поиск' }
const statusNames = { pending: 'Ожидает', running: 'Выполняется', completed: 'Готово', failed: 'Ошибка', interrupted: 'Прервано' }
const step = computed(() => props.run?.steps?.find(item => item.name === props.stage.name))
const legacy = computed(() => props.run && !Array.isArray(props.run.exchanges))
const exchanges = computed(() => (props.run?.exchanges || []).filter(item => item.stage === props.stage.name))
const mcp = computed(() => exchanges.value.filter(item => item.kind === 'mcp'))
const http = computed(() => exchanges.value.filter(item => item.kind !== 'mcp'))
const setup = computed(() => props.stage.name === 'search' ? (props.run?.exchanges || []).filter(item => item.stage === 'setup') : [])
const trace = computed(() => (props.run?.trace || []).filter(item => item.tool === props.stage.name))
const output = computed(() => props.run?.[{ search: 'search_result', summarize: 'summary_result', saveToFile: 'file_result' }[props.stage.name]] || null)
const input = computed(() => props.stage.name === 'summarize' ? props.run?.search_result : props.stage.name === 'saveToFile' ? props.run?.summary_result : null)
const inputKind = computed(() => props.stage.name === 'summarize' ? 'Снимок поиска' : 'Готовая сводка')
const inputIdKey = computed(() => props.stage.name === 'summarize' ? 'search_result_id' : 'summary_id')
const confirmedInput = computed(() => input.value && step.value?.input_id === input.value[inputIdKey.value])
const reconstructedRequest = computed(() => {
  const event = trace.value.find(item => item.status === 'started' && item.arguments && typeof item.arguments === 'object')
  return event ? { method: 'tools/call', params: { name: props.stage.name, arguments: event.arguments } } : null
})
function status(value) { return statusNames[value] || value || 'Ожидает' }
function exchangeStatus(exchange) {
  return exchange.status === 'running' && props.run?.status !== 'running' ? 'Запись не завершена' : status(exchange.status)
}
function responsePlaceholder(exchange) {
  if (exchange.status !== 'running') return 'Ответ не записан.'
  return props.run?.status === 'running' ? 'Ответ ещё не получен.' : 'Запись ответа не завершена. Этот запуск уже остановлен.'
}
function date(value) {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '—' : new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(parsed)
}
function duration(value) { return typeof value === 'number' ? `${(value / 1000).toFixed(2)} с` : '—' }
</script>

<template>
  <section id="stage-details" class="stage-details-panel" :aria-labelledby="`details-title-${stage.name}`">
    <header class="stage-details-heading">
      <div><span class="eyebrow">JSON И ДЕТАЛИ / {{ stage.name }}</span><h3 :id="`details-title-${stage.name}`">{{ stage.title }}</h3></div>
      <button class="details-close" @click="$emit('close')" aria-label="Закрыть детали этапа">×</button>
    </header>
    <p class="stage-role">{{ roles[stage.name] }}</p>
    <p v-if="!run" class="diagnostic-note">Запустите исследование, чтобы увидеть реальные запросы, ответы и артефакты этого этапа.</p>
    <template v-else>
      <dl class="stage-metadata">
        <dt>Статус</dt><dd>{{ status(step?.status) }}</dd>
        <dt>Начало</dt><dd>{{ date(step?.started_at) }}</dd>
        <dt>Завершение</dt><dd>{{ date(step?.completed_at) }}</dd>
        <dt>Длительность</dt><dd>{{ duration(step?.duration_ms) }}</dd>
        <template v-for="key in ['input_id', 'output_id', 'input_sha256', 'output_sha256']" :key="key"><dt>{{ key }}</dt><dd><code>{{ step?.[key] || '—' }}</code></dd></template>
      </dl>
      <p v-if="step?.error" class="diagnostic-error" role="status">{{ step.error }}</p>

      <div v-if="input" class="diagnostic-section">
        <h4>Входной артефакт · {{ inputKind }}</h4>
        <p class="diagnostic-note">{{ confirmedInput ? `Этап получил ${inputIdKey}: сервер загрузил сохранённый артефакт по этому ID.` : 'Артефакт сохранён на предыдущем этапе. Передача его ID этому инструменту пока не подтверждена.' }}</p>
        <JsonBlock :title="`${inputKind} · полный сохранённый JSON`" :value="input" />
      </div>

      <div class="diagnostic-section">
        <h4>Вызов MCP</h4>
        <p class="diagnostic-note">Параметры вызова и нормализованный результат SDK. Это представление tools/call, а не полный JSON-RPC пакет транспорта.</p>
        <div v-if="legacy" class="legacy-note">Этот запуск создан до записи JSON-обменов. Представление ниже восстановлено из фактических trace args и сохранённых артефактов. HTTP-запросы и ответы не записывались.</div>
        <div v-else-if="!mcp.length && (reconstructedRequest || output)" class="legacy-note">MCP-обмен этого этапа не записан. Представление ниже восстановлено из фактических trace args и сохранённых артефактов; это не запись пакета транспорта.</div>
        <article v-for="exchange in mcp" :key="exchange.exchange_id" class="exchange">
          <div class="exchange-heading"><strong>{{ kindNames[exchange.kind] }}</strong><span :class="`exchange-status ${exchange.status}`">{{ exchangeStatus(exchange) }}</span></div>
          <p class="exchange-time">{{ date(exchange.started_at) }} → {{ date(exchange.completed_at) }} · {{ duration(exchange.duration_ms) }}</p>
          <p class="exchange-id">Exchange ID <code>{{ exchange.exchange_id }}</code></p>
          <JsonBlock title="Запрос · параметры SDK" :value="exchange.request" :open="true" />
          <JsonBlock v-if="exchange.response !== null && exchange.response !== undefined" title="Ответ · нормализованный результат SDK" :value="exchange.response" />
          <p v-else class="diagnostic-note">{{ responsePlaceholder(exchange) }}</p>
          <p v-if="exchange.error_code" class="diagnostic-error">Код ошибки: {{ exchange.error_code }}</p>
        </article>
        <template v-if="legacy || (!mcp.length && (reconstructedRequest || output))">
          <JsonBlock v-if="reconstructedRequest" title="Запрос · восстановлено из trace args" :value="reconstructedRequest" :open="true" />
          <JsonBlock v-if="output" title="Ответ · восстановлено из сохранённого артефакта" :value="{ isError: false, structuredContent: output }" />
          <p v-if="!reconstructedRequest && !output" class="diagnostic-note">Параметры и результат этого инструмента не сохранены.</p>
        </template>
        <p v-else-if="!mcp.length" class="diagnostic-note">{{ step?.status === 'pending' ? 'Инструмент пока не вызывался.' : step?.status === 'running' ? 'Ожидаем запись вызова инструмента.' : 'Записанного MCP-обмена для этого этапа нет.' }}</p>
        <JsonBlock v-if="trace.length" title="События этапа · фактический trace" :value="trace" />
      </div>

      <div v-if="output" class="diagnostic-section"><h4>{{ stage.name === 'saveToFile' ? 'Квитанция файла на сервере' : 'Выходной артефакт' }}</h4><p v-if="stage.name === 'saveToFile'" class="diagnostic-note">Квитанция подтверждает сохранение на сервере. Кнопка «Скачать Markdown» ниже переносит файл на ваш компьютер.</p><JsonBlock title="Сохранённый результат · полный JSON" :value="output" /></div>

      <div class="diagnostic-section">
        <h4>HTTP-обмены этапа</h4>
        <p class="diagnostic-note">Реальные тела запросов и нормализованные ответы DeepSeek / Tavily. Ключи и заголовки авторизации исключены. Ограничение диагностического объёма отмечается внутри JSON.</p>
        <p v-if="legacy" class="diagnostic-note">В этом запуске HTTP-обмены не записывались.</p>
        <p v-else-if="!http.length" class="diagnostic-note">{{ stage.name === 'saveToFile' && step?.status === 'completed' ? 'Сохранение выполняется локальным MCP-сервером. HTTP-обмен для решения агента не записан.' : run.status === 'running' ? 'HTTP-обмены этого этапа пока не записаны.' : 'HTTP-обмены этого этапа не записаны.' }}</p>
        <article v-for="exchange in http" :key="exchange.exchange_id" class="exchange">
          <div class="exchange-heading"><strong>{{ kindNames[exchange.kind] || exchange.kind }}</strong><span :class="`exchange-status ${exchange.status}`">{{ exchangeStatus(exchange) }}</span></div>
          <p class="exchange-time">{{ date(exchange.started_at) }} → {{ date(exchange.completed_at) }} · {{ duration(exchange.duration_ms) }}</p>
          <p class="exchange-id">Exchange ID <code>{{ exchange.exchange_id }}</code></p>
          <JsonBlock title="HTTP-запрос · записанные данные без ключей" :value="exchange.request" />
          <JsonBlock v-if="exchange.response !== null && exchange.response !== undefined" title="HTTP-ответ · нормализованные данные" :value="exchange.response" />
          <p v-else class="diagnostic-note">{{ responsePlaceholder(exchange) }}</p>
          <p v-if="exchange.error_code" class="diagnostic-error">Код ошибки: {{ exchange.error_code }}</p>
        </article>
      </div>

      <details v-if="setup.length" class="setup-exchanges"><summary>Инициализация MCP · discovery</summary><article v-for="exchange in setup" :key="exchange.exchange_id" class="exchange"><div class="exchange-heading"><strong>MCP · обнаружение инструментов</strong><span :class="`exchange-status ${exchange.status}`">{{ exchangeStatus(exchange) }}</span></div><p class="exchange-time">{{ date(exchange.started_at) }} → {{ date(exchange.completed_at) }} · {{ duration(exchange.duration_ms) }}</p><JsonBlock title="Запрос discovery · параметры SDK" :value="exchange.request" /><JsonBlock v-if="exchange.response !== null && exchange.response !== undefined" title="Ответ discovery · схемы инструментов" :value="exchange.response" /><p v-else class="diagnostic-note">{{ responsePlaceholder(exchange) }}</p><p v-if="exchange.error_code" class="diagnostic-error">Код ошибки: {{ exchange.error_code }}</p></article></details>
    </template>
  </section>
</template>
