import test from 'node:test'
import assert from 'node:assert/strict'
import { number, percentage, score } from '../src/format.js'

test('progress appears only with measurable total', () => {
  assert.equal(percentage(2, 5), 40)
  assert.equal(percentage(0, 0), null)
  assert.equal(percentage(undefined, undefined), null)
  assert.equal(percentage(10, 5), 100)
})

test('missing measurements are visibly absent', () => {
  assert.equal(number(null), '—')
  assert.equal(score(undefined), '—')
  assert.equal(score(0.912345), '0.912')
})
