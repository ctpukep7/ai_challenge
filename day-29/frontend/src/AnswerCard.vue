<script setup>
import { computed } from 'vue';
import { parameterRows } from './optimization-state.js';
import { answerStatistics } from './answer-statistics.js';
const props = defineProps({ provider: { type: Object, required: true } });
const statistics = computed(() => answerStatistics(props.provider));
const parameters = computed(() => parameterRows(props.provider.parameters));
const failed = computed(() => props.provider.status === 'error');
const number = (value, digits = 2) => typeof value === 'number' ? value.toLocaleString('ru-RU', { maximumFractionDigits: digits }) : '—';
</script>
<template>
  <article class="answer-card" :class="{ 'local-answer': provider.profile === 'optimized', 'baseline-answer': provider.profile === 'baseline', 'provider-failed': failed }">
    <div class="answer-heading"><div><h3>{{ provider.label }} · локально</h3><div class="model-tag">{{ provider.model }}</div></div><span class="answer-tag" :class="{ failed }">{{ failed ? 'Ошибка модели' : 'Свежий ответ' }}</span></div>
    <div class="profile-options">temperature {{ number(provider.parameters.temperature) }} · ответ до {{ provider.parameters.num_predict }} · контекст {{ provider.parameters.num_ctx }}</div>
    <div class="answer-text" :class="{ 'provider-error-text': failed }" :role="failed ? 'status' : undefined">{{ failed ? provider.error?.message || provider.answer : provider.answer }}</div>
    <p v-if="provider.truncated" class="response-warning">Ответ достиг лимита длины и мог закончиться раньше времени.</p>
    <p v-if="provider.context_warning" class="response-warning">{{ provider.context_warning }}</p>
    <p v-if="!provider.called && !failed" class="generation-note">Генерация не выполнялась.</p><p v-if="failed" class="provider-error-note">Ответ этой модели не получен. Автоматического повтора нет.</p>
    <section class="answer-statistics" aria-label="Статистика ответа"><h4>Статистика ответа</h4><dl><div v-for="row in statistics" :key="row.key"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></div></dl><p>Время модели исключает ожидание другого ответа. Прогрев runner измерен отдельно от HTTP генерации. Размер по /api/ps — размещение модели, а не полный расход RAM системы.</p></section>
    <details class="prompt-details"><summary>Параметры и системная инструкция</summary><dl class="profile-settings"><div v-for="row in parameters" :key="row.key"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></div><div><dt>keep_alive</dt><dd>{{ provider.parameters.keep_alive || 'Не предоставлено' }}</dd></div><div><dt>Квантование</dt><dd>{{ provider.resources?.quantization || 'Не предоставлено' }}</dd></div></dl><pre>{{ provider.system || 'Системная инструкция отсутствует: модели передан только ваш запрос.' }}</pre></details>

  </article>
</template>
