import test from 'node:test'
import assert from 'node:assert/strict'
import { withWorkspace } from '../src/api.js'

test('workspace is sent with all scoped endpoints and preserves filters', () => {
  assert.equal(withWorkspace('/api/state', 'demo'), '/api/state?workspace=demo')
  const path = withWorkspace('/api/chunks?strategy=structure&page=2', 'main')
  const [, query] = path.split('?')
  const params = new URLSearchParams(query)
  assert.equal(params.get('strategy'), 'structure')
  assert.equal(params.get('page'), '2')
  assert.equal(params.get('workspace'), 'main')
})
