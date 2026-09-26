<script setup>
import { json, statusLabel } from '../format'
defineProps({ run: { type: Object, default: null }, loading: Boolean })
const labels = { weather: 'Погода', calendar: 'Календарь', plans: 'Планы' }
function duration(ms) { return typeof ms === 'number' ? `${(ms / 1000).toFixed(1)} с` : 'В работе' }
</script>

<template>
  <section class="panel trace-panel" aria-labelledby="trace-heading" aria-live="polite">
    <div class="section-heading">
      <div><h2 id="trace-heading">Трасса вызовов</h2><p class="section-caption">Выбор инструментов и порядок выполнения</p></div>
      <span v-if="run" class="status" :class="run.status">{{ statusLabel(run.status) }}</span><span v-else class="subtle">Ожидание запроса</span>
    </div>
    <div v-if="!run?.trace?.length" class="trace-empty">
      <div class="route-illustration" aria-hidden="true"><span>Агент</span><i>→</i><span>MCP</span><i>→</i><span>Результат</span></div>
      <p>{{ loading ? 'Агент выбирает первый инструмент…' : 'Здесь появится последовательность реальных вызовов — с сервером, аргументами и результатом каждого шага.' }}</p>
    </div>
    <ol v-else class="trace-list">
      <li v-for="(step, index) in run.trace" :key="step.id || step.sequence || index" class="trace-step" :class="step.status">
        <span class="step-number">{{ step.sequence || index + 1 }}</span>
        <div class="step-content">
          <div class="step-topline"><span class="server-label" :class="step.server_id">{{ labels[step.server_id] || step.server_id }}</span><span class="step-duration">{{ duration(step.duration_ms) }}</span></div>
          <h3><code>{{ step.alias || step.tool }}</code><span v-if="step.status === 'running'" class="spinner" aria-label="Выполняется"></span><span v-else-if="step.status === 'error' || step.error" class="step-outcome bad">Ошибка</span><span v-else class="step-outcome">{{ step.status === 'success' || step.status === 'completed' ? 'Готово' : statusLabel(step.status) }}</span></h3>
          <p v-if="step.error" class="inline-error">{{ step.error }}</p>
          <p v-else-if="step.result_summary" class="step-summary">{{ typeof step.result_summary === 'string' ? step.result_summary : json(step.result_summary) }}</p>
          <details><summary>Аргументы и результат</summary><div class="trace-details"><h4>Аргументы</h4><pre>{{ json(step.arguments || {}) }}</pre><template v-if="step.result_summary"><h4>Результат</h4><pre>{{ json(step.result_summary) }}</pre></template></div></details>
        </div>
      </li>
    </ol>
    <p v-if="run?.error" class="inline-error run-error">{{ run.error }}</p>
    <p v-if="run && ['interrupted', 'incomplete', 'failed', 'error'].includes(run.status)" class="small trace-warning">Флоу не завершён. Сохранённые проекты доступны ниже; этот статус не означает успешное создание события.</p>
  </section>
</template>
