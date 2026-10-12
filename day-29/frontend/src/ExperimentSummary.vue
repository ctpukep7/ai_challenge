<script setup>
import { computed } from 'vue';
import { qualityChecks, metricDeltas } from './presets.js';
const props = defineProps({ result: { type: Object, required: true } });
const providers = computed(() => props.result.mode === 'compare' ? [props.result.baseline, props.result.optimized] : [props.result.optimized]);
const deltas = computed(() => metricDeltas(props.result.baseline, props.result.optimized));
</script>
<template>
  <section class="experiment-summary" aria-label="Качество и изменения метрик">
    <h3>{{ result.preset ? result.preset.title : 'Свободный эксперимент' }} · целевой контракт</h3>
    <p v-if="result.preset" class="contract-note">Одинаковый пользовательский запрос. SYSTEM и формат могут различаться; точные настройки сохранены в карточках ответа. Проверяем соответствие целевому контракту.</p>
    <p v-if="result.preset?.id === 'sql-cards-16'" class="contract-note">Лимит настроенного ответа в этом запуске — {{ result.optimized.parameters.num_predict }} токенов. Полный документ может потребовать больше времени.</p>
    <div class="quality-grid" :class="{ compared: result.mode === 'compare' }">
      <div v-for="provider in providers" :key="provider.profile"><h4>{{ provider.label }}</h4><ul><li v-for="check in qualityChecks(provider, result.question, result.preset)" :key="check.label" :class="`check-${check.status}`"><span>{{ check.status === 'pass' ? '✓' : check.status === 'fail' ? '✗' : '○' }} {{ check.label }}</span><small>{{ check.detail }}</small></li></ul></div>
    </div>
    <template v-if="result.mode === 'compare'"><h4>Изменения: исходная → настроенная</h4><dl class="delta-list"><div v-for="delta in deltas" :key="delta.key"><dt>{{ delta.label }}</dt><dd>{{ delta.value }}</dd></div></dl></template>
    <p>Один запуск: настройки не гарантируют улучшение. Структура JSON не доказывает правильность фактов. Размещение по /api/ps — не полный расход RAM системы.</p>
  </section>
</template>
