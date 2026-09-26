export async function api(path, options = {}) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 12000)
  try {
    const response = await fetch(path, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...options.headers },
      signal: controller.signal,
    })
    let payload
    try { payload = await response.json() } catch { payload = null }
    if (!response.ok) {
      const detail = payload?.detail || payload
      const error = new Error(typeof detail === 'string' ? detail : detail?.error || 'Не удалось выполнить запрос. Попробуйте ещё раз.')
      error.status = response.status
      error.code = detail?.error_code || ''
      throw error
    }
    if (!payload) throw new Error('Сервер вернул неполный ответ. Проверьте состояние запуска.')
    return payload
  } catch (error) {
    if (error.status) throw error
    const failure = new Error(error.name === 'AbortError'
      ? 'Сервер не ответил вовремя. Результат отправки пока неизвестен.'
      : 'Нет связи с API. Проверьте, что приложение запущено, и обновите состояние.')
    failure.uncertain = true
    throw failure
  } finally { clearTimeout(timer) }
}
