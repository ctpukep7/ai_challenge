// Persisted runs are returned in creation order, oldest first.
export function latestRun(runs = []) { return runs.at(-1) || null }

export function restorePending(value) {
  if (!value || typeof value !== 'object') return null
  const body = value.body || value // Migrate earlier continuation/message envelopes to their original wire body.
  if (typeof body.prompt !== 'string' || !body.prompt.trim() || typeof body.request_id !== 'string' || !body.request_id.trim()) return null
  if (body.calendar_id !== undefined && body.calendar_id !== null && typeof body.calendar_id !== 'string') return null
  return {
    prompt: body.prompt,
    request_id: body.request_id,
    ...(Object.hasOwn(body, 'calendar_id') ? { calendar_id: body.calendar_id } : {}),
  }
}

export function prepareChatRequest(previous, prompt, createId) {
  // An acknowledged response may have been lost: replay the same prompt with its exact former body.
  // A different message always gets a new UUID and never inherits a previous calendar selection.
  return previous?.prompt === prompt ? previous : { prompt, request_id: createId() }
}
