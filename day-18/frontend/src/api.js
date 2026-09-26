export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message)
    this.status = status
  }
}

function detailMessage(detail) {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map(item => item.msg || String(item)).join('; ')
  if (detail && typeof detail === 'object') return detail.message || detail.error || JSON.stringify(detail)
  return ''
}

export async function api(path, options = {}) {
  let response
  try {
    response = await fetch(path, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...options.headers },
    })
  } catch {
    throw new ApiError('Нет связи с сервером. Проверьте, что приложение запущено, и повторите запрос.')
  }
  let body
  try {
    body = await response.json()
  } catch {
    throw new ApiError('Сервер вернул некорректный ответ. Попробуйте ещё раз.', response.status)
  }
  if (!response.ok) {
    throw new ApiError(detailMessage(body.detail) || detailMessage(body.error) || `Не удалось выполнить запрос (${response.status}).`, response.status)
  }
  return body
}
