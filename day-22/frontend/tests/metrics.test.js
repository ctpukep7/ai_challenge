import test from 'node:test'
import assert from 'node:assert/strict'
import { batchRequest, formatDuration, friendlyError, mergeRetry, missingModes, sameEnvironment, summarize } from '../src/metrics.js'

test('only complete answer pairs enter denominator', () => {
  const results = [
    { without_rag: { answer: 'a', evaluation: { fact_ratio: 0.5 } },
      with_rag: { answer: 'b', evaluation: { fact_ratio: 1, expected_source_retrieved: true, has_citation_marker: true } } },
    { without_rag: { answer: null }, with_rag: { answer: 'c' } },
  ]
  assert.deepEqual(summarize(results), { total: 1, without: 50, with: 100, sourceHits: 1, citations: 1 })
})

test('empty run has no percentages', () => {
  assert.deepEqual(summarize([]), { total: 0, without: null, with: null, sourceHits: 0, citations: 0 })
})

test('API error text does not expose response internals', () => {
  assert.equal(friendlyError({ detail: 'Сначала постройте индекс' }, 409), 'Сначала постройте индекс')
  assert.equal(friendlyError({}, 503), 'Ошибка API: HTTP 503')
})

test('retry keeps a successful answer and its source while sending only the failed mode', () => {
  const previous = {
    question_id: 'q07', strategy: 'fixed', top_k: 3,
    chunks: [{ chunk_id: 'source-1' }], context: '[1] source-1',
    without_rag: { answer: null, error: 'Превышено время ожидания ответа DeepSeek' },
    with_rag: { answer: 'Три человека и телескоп [1]', evaluation: { fact_ratio: 1 },
      elapsed_ms: 75000, prompt_chars: 3720 },
  }
  const config = { strategy: 'fixed', top_k: 3 }
  assert.deepEqual(batchRequest({ id: 'q07', question: 'Что находится на базе?' }, config, previous), {
    question: 'Что находится на базе?', question_id: 'q07', strategy: 'fixed', top_k: 3, modes: ['without_rag'],
  })
  const retried = {
    question_id: 'q07', strategy: 'fixed', top_k: 3,
    chunks: [{ chunk_id: 'new-retrieval' }], context: '[1] new-retrieval',
    without_rag: { answer: 'Три человека и телескоп', evaluation: { fact_ratio: 1 },
      elapsed_ms: 820, prompt_chars: 180 },
    with_rag: null,
  }
  const merged = mergeRetry(previous, retried)
  assert.equal(merged.retried, true)
  assert.deepEqual(missingModes(merged), [])
  assert.equal(merged.with_rag.answer, previous.with_rag.answer)
  assert.equal(merged.with_rag.elapsed_ms, 75000)
  assert.equal(merged.with_rag.prompt_chars, 3720)
  assert.equal(merged.without_rag.elapsed_ms, 820)
  assert.deepEqual(merged.chunks, previous.chunks)
  assert.equal(merged.context, previous.context)
  assert.equal(merged.strategy, 'fixed')
  assert.equal(merged.top_k, 3)
  assert.equal(summarize([merged]).total, 1)
})

test('durations format consistently for successes and errors', () => {
  assert.equal(formatDuration(125.4), '125 мс')
  assert.equal(formatDuration(75000), '75,0 с')
  assert.equal(formatDuration(null), '—')
})

test('resume requires the same model and index snapshot', () => {
  const state = { model: 'deepseek-flash', index: { manifest: { revision: 1 } } }
  const config = { model: 'deepseek-flash', index: JSON.stringify(state.index.manifest) }
  assert.equal(sameEnvironment(config, state), true)
  assert.equal(sameEnvironment(config, { ...state, model: 'another-model' }), false)
  assert.equal(sameEnvironment(config, { ...state, index: { manifest: { revision: 2 } } }), false)
})
