export async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = payload?.detail
    throw new Error(typeof detail === 'string' ? detail : `Ошибка сервера (${response.status})`)
  }
  return payload
}

export function withWorkspace(path, workspace) {
  const [pathname, query = ''] = path.split('?')
  const params = new URLSearchParams(query)
  params.set('workspace', workspace)
  return `${pathname}?${params}`
}

export function scopedApi(path, workspace, options = {}) {
  return api(withWorkspace(path, workspace), options)
}
