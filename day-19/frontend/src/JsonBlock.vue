<script setup>
import { computed } from 'vue'
const props = defineProps({
  title: { type: String, required: true },
  value: { required: true },
  open: { type: Boolean, default: false },
})
function hasTruncation(value) {
  if (!value || typeof value !== 'object') return false
  return value._diagnostic_truncated === true || Object.values(value).some(hasTruncation)
}
const truncated = computed(() => hasTruncation(props.value))
</script>

<template>
  <details class="json-block" :open="open">
    <summary>{{ title }} <span>{{ truncated ? 'JSON · сокращено' : 'JSON' }}</span></summary>
    <p v-if="truncated" class="json-truncation">Диагностическая запись сокращена: часть данных не сохранена из-за ограничения объёма.</p>
    <pre>{{ JSON.stringify(value, null, 2) }}</pre>
  </details>
</template>
