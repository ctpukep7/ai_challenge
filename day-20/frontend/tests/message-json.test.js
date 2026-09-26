import test from 'node:test'
import assert from 'node:assert/strict'
import { jsonMessageOwners, jsonRunActive } from '../src/message-json.js'

test('each completed run exposes JSON under its assistant message with no user duplicate', () => {
  const owners = jsonMessageOwners([
    { id: 'u1', role: 'user', run_id: 'one' }, { id: 'a1', role: 'assistant', run_id: 'one' },
    { id: 'u2', role: 'user', run_id: 'two' }, { id: 'a2', role: 'assistant', run_id: 'two' },
  ])
  assert.deepEqual([...owners], ['a1', 'a2'])
})

test('an unanswered failed or running run exposes JSON under user until an assistant appears', () => {
  const user = { id: 'user', role: 'user', run_id: 'run' }
  assert.deepEqual([...jsonMessageOwners([user])], ['user'])
  assert.deepEqual([...jsonMessageOwners([user, { id: 'answer', role: 'assistant', run_id: 'run' }])], ['answer'])
})

test('multiple messages and legacy messages cannot duplicate or misattribute JSON', () => {
  assert.deepEqual([...jsonMessageOwners([
    { id: 'legacy', role: 'assistant' }, { id: 'u', role: 'user', run_id: 'r' },
    { id: 'a1', role: 'assistant', run_id: 'r' }, { id: 'a2', role: 'assistant', run_id: 'r' },
    { id: 'tool', role: 'tool', run_id: 'r' },
  ])], ['a2'])
})

test('JSON activity belongs to its own run independently of selected trace', () => {
  const runs = [{ id: 'active', status: 'running' }, { id: 'old', status: 'completed' }]
  assert.equal(jsonRunActive('active', runs, [], new Set(), { id: 'old', status: 'completed' }), true)
  assert.equal(jsonRunActive('old', runs, [], new Set(), { id: 'active', status: 'running' }), false)
  assert.equal(jsonRunActive('active', runs, [], new Set(), { id: 'active', status: 'completed' }), false)
})

test('confirmation keeps only its own run active until final calendar outcome', () => {
  const runs = [{ id: 'one', status: 'completed' }, { id: 'two', status: 'completed' }]
  const plans = [{ id: 'p1', run_id: 'one', status: 'draft' }, { id: 'p2', run_id: 'two', status: 'created' }]
  assert.equal(jsonRunActive('one', runs, plans, new Set(['p1'])), true)
  assert.equal(jsonRunActive('two', runs, plans, new Set(['p1'])), false)
  for (const status of ['approved', 'writing']) assert.equal(jsonRunActive('one', runs, [{ ...plans[0], status }]), true)
  for (const status of ['created', 'rejected', 'conflict', 'uncertain']) assert.equal(jsonRunActive('one', runs, [{ ...plans[0], status }]), false)
})
