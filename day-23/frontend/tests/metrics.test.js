import test from 'node:test'
import assert from 'node:assert/strict'
import { buildRequest, callBudget, corpusSummary, formatDuration, formatScore, recoverActiveRun, recoveredSettings, validateSettings } from '../src/metrics.js'

test('source panel describes current README corpus before building, ignoring stale manifest', () => {
  const index = {
    ready: false,
    sources: ['README.md', 'day-01/README.md'],
    corpus: { files: 2, words: 800, sections: 7 },
    manifest: { corpus: { files: 4, words: 24000, chapters: 9 }, sources: ['old-book.md'] },
  }
  assert.deepEqual(corpusSummary(index), { sources: index.sources, files: 2, words: 800, sections: 7 })
  assert.equal(corpusSummary(null).files, undefined)
})

test('source panel can use persisted corpus metadata without inventing source counts', () => {
  assert.deepEqual(corpusSummary({ manifest: { sources: ['README.md'], corpus: { words: 321, sections: 4 } } }),
    { sources: ['README.md'], files: 1, words: 321, sections: 4 })
})

test('search-only overrides LLM rewriting without changing form settings', () => {
  const settings = { strategy: 'structure', top_k_before: '12', top_k_after: '3', threshold: '.6', rewrite_method: 'llm' }
  const request = buildRequest(settings, { question: '  Что такое MCP? ', questionId: 'q1' })
  assert.equal(request.rewrite_method, 'local')
  assert.equal(request.generate_answers, false)
  assert.equal(request.question, 'Что такое MCP?')
  assert.equal(request.question_id, 'q1')
  assert.equal(request.top_k_before, 12)
  assert.equal(settings.rewrite_method, 'llm')
})

test('answer benchmark preserves explicit LLM choice and omits stale question', () => {
  const request = buildRequest({ rewrite_method: 'llm', threshold: .6, top_k_before: 12, top_k_after: 3 }, { answers: true, benchmark: true, question: 'unused', questionId: 'stale' })
  assert.equal(request.rewrite_method, 'llm')
  assert.equal(request.generate_answers, true)
  assert.equal('question' in request, false)
  assert.equal('question_id' in request, false)
})

test('call budget includes explicit LLM rewrite and gives zero for local search', () => {
  assert.equal(callBudget(13, false, 'local'), 0)
  assert.equal(callBudget(13, true, 'local'), 39)
  assert.equal(callBudget(13, true, 'llm'), 52)
})

test('settings reject nonfinite thresholds and inconsistent K', () => {
  const valid = { top_k_before: 12, top_k_after: 3, threshold: .6 }
  assert.equal(validateSettings(valid), '')
  assert.match(validateSettings({ ...valid, threshold: NaN }), /Порог/)
  assert.match(validateSettings({ ...valid, threshold: '' }), /Порог/)
  assert.match(validateSettings({ ...valid, threshold: Infinity }), /Порог/)
  assert.match(validateSettings({ ...valid, top_k_after: 13 }), /Top-K после/)
  assert.match(validateSettings({ ...valid, top_k_before: 2 }), /Top-K после/)
})

test('diagnostics preserve negative cosine and absent durations', () => {
  assert.equal(formatScore(-.12345), '-0,123')
  assert.equal(formatDuration(null), '—')
  assert.equal(formatDuration(1234), '1,2 с')
})

test('reload recovery reads the existing job once and never starts a new request', async () => {
  const calls = [], snapshot = { id: 'active', status: 'running', settings: { threshold: .71 } }
  const result = await recoverActiveRun({ id: 'active' }, async (...args) => {
    calls.push(args)
    return snapshot
  })
  assert.equal(result, snapshot)
  assert.deepEqual(calls, [['/api/runs/active']])
  assert.equal(await recoverActiveRun(null, () => { throw Error('unexpected fetch') }), null)
})

test('reload recovery tolerates evicted jobs but surfaces other service failures', async () => {
  const fail = status => async () => { throw Object.assign(Error('failure'), { status }) }
  assert.equal(await recoverActiveRun({ id: 'gone' }, fail(404)), null)
  await assert.rejects(recoverActiveRun({ id: 'active' }, fail(500)), /failure/)
})

test('recovered controls use the job snapshot and exclude unrelated API fields', () => {
  const original = { threshold: .6, top_k_before: 12, rewrite_method: 'local' }
  const restored = recoveredSettings(original, { threshold: 0, top_k_before: 8, generate_answers: true, model: 'model' })
  assert.deepEqual(restored, { threshold: 0, top_k_before: 8, rewrite_method: 'local' })
  assert.equal(original.threshold, .6)
})
