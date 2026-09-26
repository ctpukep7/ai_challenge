import test from 'node:test'
import assert from 'node:assert/strict'
import { latestRun, prepareChatRequest, restorePending } from '../src/chat-request.js'

test('new manual chat messages send only the prompt and a fresh UUID', () => {
  assert.deepEqual(prepareChatRequest(null, 'Используй календарь «Личный»', () => 'new-id'), { prompt: 'Используй календарь «Личный»', request_id: 'new-id' })
})

test('lost response retries and reload preserve the same request body and UUID', () => {
  const first = prepareChatRequest(null, 'Поездка на три часа', () => 'stable-id')
  const reloaded = restorePending(JSON.parse(JSON.stringify(first)))
  const retry = prepareChatRequest(reloaded, first.prompt, () => { throw new Error('Retry must not create a UUID') })
  assert.deepEqual(retry, first)
})

test('legacy accepted continuation and message envelopes migrate without changing their wire body', () => {
  const body = { prompt: 'Исходное поручение', request_id: 'accepted-id', calendar_id: 'personal' }
  for (const kind of ['message', 'continuation']) {
    const pending = restorePending({ kind, body, source_run_id: 'old', composer_snapshot: 'Черновик' })
    assert.deepEqual(pending, body)
    assert.deepEqual(prepareChatRequest(pending, body.prompt, () => { throw new Error('Must replay accepted legacy request') }), body)
    assert.ok(!('kind' in pending))
    assert.ok(!('source_run_id' in pending))
  }
})

test('a different manual reply never inherits a legacy calendar_id or UUID', () => {
  const legacy = restorePending({ kind: 'continuation', body: { prompt: 'Исходное поручение', request_id: 'legacy-id', calendar_id: 'personal' } })
  const next = prepareChatRequest(legacy, 'Используй календарь «Поездки»', () => 'manual-choice-id')
  assert.deepEqual(next, { prompt: 'Используй календарь «Поездки»', request_id: 'manual-choice-id' })
  assert.ok(!('calendar_id' in next))
})

test('legacy explicit null and empty calendar values survive retries exactly', () => {
  for (const calendar_id of [null, '']) {
    const body = { prompt: 'Повтор', request_id: 'same-id', calendar_id }
    assert.deepEqual(restorePending(body), body)
    assert.deepEqual(prepareChatRequest(restorePending(body), body.prompt, () => 'unused'), body)
  }
})

test('malformed pending data is discarded and latest trace uses creation order', () => {
  for (const value of [null, 'bad', {}, { prompt: '', request_id: 'id' }, { prompt: 'Задача', request_id: '' }, { body: { prompt: 'Задача', request_id: 'id', calendar_id: {} } }]) assert.equal(restorePending(value), null)
  assert.equal(latestRun([]), null)
  assert.equal(latestRun([{ id: 'first' }, { id: 'last' }]).id, 'last')
})
