export const MODES = ['baseline', 'filtered', 'rewritten']
export const MODE_NAMES = { baseline: 'Без cosine-порога', filtered: 'С фильтром', rewritten: 'Rewrite + фильтр' }
export const TERMINAL = ['completed', 'failed', 'cancelled']

export function corpusSummary(index) {
  const corpus = index?.corpus ?? index?.manifest?.corpus ?? index?.manifest ?? {}
  const sources = index?.sources ?? corpus.sources ?? index?.manifest?.sources ?? []
  return {
    sources: sources.map(source => typeof source === 'string' ? source : source.source),
    files: corpus.files ?? (index ? sources.length : undefined),
    words: corpus.words,
    sections: corpus.sections ?? corpus.chapters,
  }
}

export function formatDuration(ms) {
  if (!Number.isFinite(ms)) return '—'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1).replace('.', ',')} с` : `${Math.round(ms)} мс`
}

export function formatScore(value) {
  return Number.isFinite(value) ? value.toFixed(3).replace('.', ',') : '—'
}

export function friendlyError(body, status) {
  if (typeof body?.detail === 'string') return body.detail
  if (Array.isArray(body?.detail)) return body.detail.map(item => item.msg).join('; ')
  return `Ошибка API: HTTP ${status}`
}

export function callBudget(count, answers, method) {
  return count * ((answers ? 3 : 0) + (method === 'llm' ? 1 : 0))
}

export function validateSettings(settings) {
  const before = Number(settings.top_k_before)
  const after = Number(settings.top_k_after)
  const threshold = Number(settings.threshold)
  if (!Number.isInteger(before) || before < 1 || before > 30) return 'Top-K до фильтра должен быть целым числом от 1 до 30.'
  if (!Number.isInteger(after) || after < 1 || after > 10 || after > before) return 'Top-K после фильтра: от 1 до 10 и не больше Top-K до фильтра.'
  if (settings.threshold === '' || settings.threshold == null || !Number.isFinite(threshold) || threshold < -1 || threshold > 1) return 'Порог cosine должен быть числом от −1 до 1.'
  return ''
}

export function buildRequest(settings, { answers = false, benchmark = false, question = '', questionId = null } = {}) {
  return {
    ...settings,
    top_k_before: Number(settings.top_k_before), top_k_after: Number(settings.top_k_after), threshold: Number(settings.threshold),
    rewrite_method: answers ? settings.rewrite_method : 'local', generate_answers: answers,
    ...(!benchmark ? { question: question.trim(), question_id: questionId || null } : {}),
  }
}

export async function recoverActiveRun(active, getRun) {
  if (!active?.id) return null
  try { return await getRun(`/api/runs/${encodeURIComponent(active.id)}`) }
  catch (cause) {
    // A job can finish and be evicted between the state and run reads.
    if (cause.status === 404) return null
    throw cause
  }
}

export function recoveredSettings(current, snapshot) {
  return Object.fromEntries(Object.entries(current).map(([key, value]) =>
    [key, snapshot?.[key] ?? value]))
}
