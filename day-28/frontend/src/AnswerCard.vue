<script setup>
import { computed } from 'vue';
import { answerStatistics } from './answer-statistics.js';
const props = defineProps({ provider: { type: Object, required: true }, cloud: { type: Boolean, default: false }, sharedMetrics: { type: Object, default: null } });
const failed = computed(() => props.provider.status === 'error');
const statistics = computed(() => answerStatistics(props.provider, { cloud: props.cloud, sharedMetrics: props.sharedMetrics }));
const number = (value, digits = 2) => typeof value === 'number' ? value.toLocaleString('ru-RU', { maximumFractionDigits: digits }) : '—';
</script>

<template>
  <article class="answer-card" :class="{ 'local-answer': !cloud, 'cloud-answer': cloud, unknown: provider.status === 'unknown', 'provider-failed': failed }">
    <div class="answer-heading"><div><h3>{{ cloud ? 'DeepSeek · облако' : 'Qwen3 · локально' }}</h3><div class="model-tag">{{ provider.model }}</div></div><span class="answer-tag" :class="{ unknown: provider.status === 'unknown', failed }">{{ failed ? 'Ошибка модели' : provider.status === 'unknown' ? 'Нет подтверждения' : 'По найденным фрагментам' }}</span></div>
    <div class="answer-text" :class="{ 'provider-error-text': failed }" :role="failed ? 'status' : undefined">{{ failed ? provider.error?.message || provider.answer : provider.answer }}</div>
    <p v-if="provider.truncated" class="response-warning">Ответ достиг лимита длины и мог закончиться раньше времени.</p>
    <p v-if="provider.citation_warning" class="response-warning">{{ provider.citation_warning }}</p>

    <p v-if="cloud && !provider.called" class="no-sources">Облачный запрос не выполнялся: недостаточно контекста для сравнения.</p>
    <p v-if="failed" class="provider-error-note">Ответ этой модели не получен. Автоматического повтора нет.</p>
    <section class="answer-statistics" aria-label="Статистика ответа"><h4>Статистика ответа</h4><dl><div v-for="row in statistics" :key="row.key"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></div></dl><p>При сравнении поиск общий. Загрузка embeddings уже входит в поиск. HTTP включает обработку входа и получение ответа. Отдельные загрузка и генерация доступны, если их сообщает провайдер. Токенизаторы моделей различаются.</p></section>

    <details v-if="provider.sources.length" class="sources-details"><summary>Источники{{ failed ? ' запроса' : ' ответа' }} · {{ provider.sources.length }}</summary><p class="source-disclaimer">Это фрагменты, переданные модели. Ссылка в ответе сама по себе не гарантирует правильность вывода.</p><article v-for="source in provider.sources" :key="source.chunk_id" class="source-card"><div class="source-heading"><b>{{ source.marker }} {{ source.source }}</b><span class="source-tag" :class="{ cited: source.cited }">{{ source.cited ? 'Указан в ответе' : 'Без ссылки в ответе' }}</span></div><strong class="source-section">{{ source.section || source.title }}</strong><div class="source-meta">{{ source.role === 'neighbor' ? 'Соседний фрагмент' : 'Найден по сходству' }}<span v-if="source.score !== null"> · cosine {{ number(source.score, 3) }}</span> · {{ source.chunk_id }}</div><div class="source-text">{{ source.text }}</div></article></details>
    <p v-else-if="!failed && !cloud" class="no-sources">Подходящие источники не найдены или не поместились в контекст.</p>
  </article>
</template>
