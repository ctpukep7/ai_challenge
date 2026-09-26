export const statusNames = {
  running: 'Выполняется', queued: 'В очереди', completed: 'Завершён', success: 'Завершён',
  incomplete: 'Не завершён', failed: 'Ошибка', error: 'Ошибка', interrupted: 'Прерван',
  draft: 'Нужно подтверждение', pending: 'Нужно подтверждение', approved: 'Подтверждён',
  writing: 'Записывается', confirmed: 'Событие создано', created: 'Событие создано',
  rejected: 'Отклонён', conflict: 'Новое пересечение', uncertain: 'Требуется сверка',
  ready: 'Подключён', unavailable: 'Недоступен', disabled: 'Не настроен',
}
export function statusLabel(value) { return statusNames[value] || value || 'Неизвестно' }
export function date(value, timezone = 'Asia/Omsk') {
  if (!value) return '—'
  try {
    return new Intl.DateTimeFormat('ru-RU', {
      day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: timezone,
    }).format(new Date(value))
  } catch { return String(value) }
}
export function json(value) { return typeof value === 'string' ? value : JSON.stringify(value, null, 2) }
