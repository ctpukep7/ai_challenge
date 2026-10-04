<script setup>
import { formatDuration, formatScore } from '../metrics.js'
defineProps({ mode: Object, name: String, subtitle: String, letter: String, variant: String, generateAnswers: Boolean, runActive: Boolean })
</script>

<template>
  <article class="mode-card" :class="variant">
    <div class="mode-heading"><span class="letter">{{ letter }}</span><div><h3>{{ name }}</h3><p>{{ subtitle }}</p></div></div>
    <template v-if="mode">
      <p v-if="mode.retrieval_scope" class="muted small">Этап 1 — источники: {{ mode.retrieval_scope.sources?.join(', ') || 'все README корпуса' }}. Ограничение задано исходным вопросом, до cosine поиска.</p>
      <p class="query-label">Поисковый запрос</p><p class="mode-query">{{ mode.query || 'Запрос не получен' }}</p>
      <div v-if="mode.counts" class="mode-counts"><div><b>{{ mode.counts.retrieved }}</b><span>найдено</span></div><div><b>{{ mode.counts.below_threshold }}</b><span>ниже порога</span></div><div><b>{{ mode.counts.kept }}</b><span>в контексте</span></div></div>
      <p v-if="mode.counts" class="muted small">{{ variant === 'baseline' ? 'Без отсечения по порогу' : 'Прошли порог' }}: {{ mode.counts.passed }} · за лимитом Top-K: {{ mode.counts.over_top_k }}. Всего исключено: {{ mode.counts.retrieved - mode.counts.kept }}.</p>
      <p v-if="mode.error" class="inline-error" role="alert">{{ mode.error }}</p>
      <p v-else-if="mode.answer_status === 'abstained'" class="policy-label">Отказ по правилу · 0 вызовов LLM</p>
      <p v-else-if="mode.answer_status === 'not_requested'" class="search-label">{{ generateAnswers ? (runActive ? 'Ответ модели запланирован, ещё не получен' : 'Генерация этого ответа не завершена') : 'Только поиск · ответ модели не запрашивался' }}</p>
      <p v-else-if="!mode.answer_status" class="search-label">Этап выполняется…</p>
      <p v-if="mode.answer" class="answer-text">{{ mode.answer }}</p>
      <div v-if="mode.retrieval_evaluation" class="evaluation">
        <span v-if="mode.retrieval_evaluation.unanswerable">{{ mode.retrieval_evaluation.correct_empty ? '✓ Контекст пуст для постороннего вопроса' : 'Контекст для постороннего вопроса не пуст' }}</span>
        <span v-else>{{ mode.retrieval_evaluation.chapter_hit ? '✓ Ожидаемый раздел README найден' : 'Ожидаемый раздел README не найден' }}</span>
        <span v-if="mode.evaluation && mode.generated_by === 'llm'">Факт-эвристика: {{ Math.round((mode.evaluation.fact_ratio ?? 0) * 100) }}%</span>
      </div>
      <footer><span>Поиск {{ formatDuration(mode.retrieval_ms) }}</span><span v-if="mode.elapsed_ms != null && mode.generated_by !== 'policy'">LLM {{ formatDuration(mode.elapsed_ms) }}</span><span>{{ mode.context_chars ?? 0 }} симв. контекста</span><span v-if="Number.isFinite(mode.usage?.total_tokens)">{{ mode.usage.total_tokens }} токенов</span></footer>
      <details v-if="mode.chunks?.length" class="card-details"><summary>Источники в контексте ({{ mode.chunks.length }})</summary><details v-for="(chunk, i) in mode.chunks" :key="chunk.chunk_id" class="source-detail"><summary>{{ chunk.marker || `[${i + 1}]` }} {{ chunk.section }} <small>cosine {{ formatScore(chunk.score) }}</small></summary><p class="source-meta">{{ chunk.source }} · {{ chunk.chunk_id }}</p><p class="source-text">{{ chunk.text }}</p></details></details>
      <details v-if="mode.messages?.length" class="card-details"><summary>{{ mode.answer_status === 'completed' ? 'Точный отправленный запрос' : mode.answer_status === 'failed' && mode.elapsed_ms != null ? 'Запрос попытки генерации' : 'Подготовленные сообщения · не отправлены' }}</summary><p class="muted small">{{ mode.prompt_chars ?? '—' }} символов system + user. {{ mode.generated_by === 'policy' ? 'Внешний запрос не выполнялся.' : 'Исходный вопрос одинаков во всех режимах.' }}</p><pre>{{ JSON.stringify(mode.messages, null, 2) }}</pre></details>
    </template>
    <p v-else class="muted">Ожидание этапа поиска…</p>
  </article>
</template>
