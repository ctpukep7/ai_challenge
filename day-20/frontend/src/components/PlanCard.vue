<script setup>
import { computed } from 'vue'
import { date, statusLabel } from '../format'
const props = defineProps({ plan: { type: Object, required: true }, busy: Boolean, error: { type: String, default: '' } })
defineEmits(['confirm', 'reject'])
const canApprove = computed(() => ['draft', 'pending'].includes(props.plan.status))
const weather = computed(() => props.plan.weather_summary)
function number(value, unit) {
  return typeof value === 'number' ? `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(value)} ${unit}` : '—'
}
</script>

<template>
  <article class="plan-card">
    <header class="plan-heading"><span class="eyebrow">ПЛАН ПОЕЗДКИ</span><span class="status" :class="plan.status">{{ statusLabel(plan.status) }}</span></header>
    <h3>{{ plan.title }}</h3>
    <dl class="plan-fields">
      <div><dt>Город</dt><dd>{{ plan.city }}</dd></div>
      <div><dt>Календарь</dt><dd>{{ plan.calendar_name || plan.calendar_id }}</dd></div>
      <div><dt>Начало</dt><dd>{{ date(plan.start, plan.timezone) }}</dd></div>
      <div><dt>Окончание</dt><dd>{{ date(plan.end, plan.timezone) }}</dd></div>
      <div><dt>Часовой пояс</dt><dd>{{ plan.timezone }}</dd></div>
    </dl>
    <p v-if="plan.reason" class="plan-reason">{{ plan.reason }}</p>
    <div v-if="weather" class="weather-summary"><span>Прогноз на всё время поездки</span><p v-if="typeof weather === 'string'">{{ weather }}</p><div v-else class="weather-metrics"><span><small>Температура от</small><strong>{{ number(weather.min_temperature_c, '°C') }}</strong></span><span><small>Осадки до</small><strong>{{ number(weather.max_precipitation_probability, '%') }}</strong></span><span><small>Ветер до</small><strong>{{ number(weather.max_wind_ms, 'м/с') }}</strong></span></div></div>
    <template v-if="canApprove">
      <p class="approval-note">Подтверждение создаст событие с указанными названием, городом, календарём и временем. Сначала сервер повторно проверит занятость.</p>
      <div class="plan-actions"><button class="primary" :disabled="busy" @click="$emit('confirm', plan)">{{ busy ? 'Проверяем…' : 'Подтвердить и создать событие' }}</button><button class="secondary" :disabled="busy" @click="$emit('reject', plan)">Отклонить</button></div>
    </template>
    <div v-else-if="plan.status === 'uncertain'"><p class="approval-note warning">Результат записи пока неизвестен. Проверьте событие по сохранённому идентификатору: эта проверка не создаёт новое событие.</p><button class="secondary" :disabled="busy" @click="$emit('confirm', plan)">{{ busy ? 'Сверяем…' : 'Сверить событие' }}</button></div>
    <p v-else-if="plan.status === 'conflict'" class="approval-note warning">В календаре появилось пересечение. Запросите новый план и подтвердите новое время.</p>
    <p v-else-if="plan.status === 'rejected'" class="approval-note">Проект отклонён. Событие не создавалось.</p>
    <p v-else-if="['confirmed', 'created'].includes(plan.status)" class="approval-note saved">Событие сохранено в выбранном календаре.</p>
    <p v-else-if="['approved', 'writing'].includes(plan.status)" class="approval-note">Подтверждение принято. Проверяем результат записи в календарь.</p>
    <p v-if="error || plan.error" class="inline-error" role="alert">{{ error || plan.error }}</p>
    <span class="plan-id">Проект {{ plan.id?.slice(0, 8) }}</span>
  </article>
</template>
