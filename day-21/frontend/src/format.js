export const phaseLabels = {
  queued: 'Ожидание запуска',
  reading: 'Чтение документов',
  loading_model: 'Загрузка модели и токенизатора',
  chunking_fixed: 'Фиксированное разбиение',
  chunking_structure: 'Разбиение по разделам',
  embedding: 'Генерация эмбеддингов',
  sqlite: 'Проверка и сохранение SQLite',
  complete: 'Готово',
  failed: 'Сборка прервана',
}

export function number(value, fallback = '—') {
  return Number.isFinite(Number(value)) && value !== null && value !== undefined
    ? new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(Number(value))
    : fallback
}

export function percentage(completed, total) {
  if (!Number.isFinite(Number(completed)) || !Number.isFinite(Number(total)) || Number(total) <= 0) return null
  return Math.min(100, Math.max(0, Math.round(Number(completed) / Number(total) * 100)))
}

export function score(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(3) : '—'
}

export function strategyName(value) {
  return value === 'fixed' ? 'Фиксированный размер' : 'По структуре'
}

export function buildStatus(value) {
  return { idle: 'Не запускалась', running: 'Идёт сборка', completed: 'Сборка завершена', failed: 'Ошибка сборки' }[value] || 'Нет данных'
}
