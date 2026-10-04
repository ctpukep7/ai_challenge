export const MODES = ['without_rag', 'with_rag']

export function missingModes(result) {
  return MODES.filter(mode => !result?.[mode]?.answer)
}

export function mergeRetry(previous, next) {
  const combined = { ...next, retried: true }
  for (const mode of MODES) {
    if (previous?.[mode]?.answer && !next?.[mode]?.answer) combined[mode] = previous[mode]
  }
  if (previous?.with_rag?.answer && !next?.with_rag?.answer) {
    combined.chunks = previous.chunks
    combined.context = previous.context
  }
  return combined
}

export function batchRequest(item, config, previous = null) {
  return {
    question: item.question, question_id: item.id,
    strategy: config.strategy, top_k: config.top_k,
    ...(previous ? { modes: missingModes(previous) } : {}),
  }
}

export function sameEnvironment(config, state) {
  return config?.model === state?.model &&
    config?.index === JSON.stringify(state?.index?.manifest)
}

export function formatDuration(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return '—'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1).replace('.', ',')} с` : `${Math.round(ms)} мс`
}

export function summarize(results) {
  const completed = results.filter(item => missingModes(item).length === 0)
  const total = completed.length
  const fact = mode => completed.reduce((sum, item) => sum + (item[mode].evaluation?.fact_ratio ?? 0), 0)
  return {
    total,
    without: total ? Math.round(100 * fact('without_rag') / total) : null,
    with: total ? Math.round(100 * fact('with_rag') / total) : null,
    sourceHits: completed.filter(item => item.with_rag.evaluation?.expected_source_retrieved).length,
    citations: completed.filter(item => item.with_rag.evaluation?.has_citation_marker).length,
  }
}

export function friendlyError(body, status) {
  if (typeof body?.detail === 'string') return body.detail
  return `Ошибка API: HTTP ${status}`
}
