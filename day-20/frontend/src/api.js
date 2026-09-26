export async function api(path, options = {}) {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 30000)
  try {
    const response = await fetch(path, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...options.headers },
      signal: controller.signal,
    })
    const body = await response.json().catch(() => null)
    if (!response.ok) {
      const detail = body?.detail
      const message = typeof detail === 'string' ? detail : detail?.message || body?.error
      throw new Error(message || `Сервер вернул ошибку ${response.status}.`)
    }
    return body
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('Ответ сервера задержался. Повторите запрос: его идентификатор сохранён.')
    if (error instanceof TypeError) throw new Error('Нет связи с сервером. Проверьте, запущен ли backend на порту 5019.')
    throw error
  } finally {
    clearTimeout(timeout)
  }
}
