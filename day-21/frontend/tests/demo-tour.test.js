import test from 'node:test'
import assert from 'node:assert/strict'
import { runTour, TOUR_STEPS } from '../src/demo-tour.js'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

test('tour waits for real build and search work before moving on', async () => {
  const build = deferred()
  const search = deferred()
  const visited = []
  const running = runTour({
    enter: async id => {
      visited.push(id)
      if (id === 'build') await build.promise
      if (id === 'search') await search.promise
    },
    pause: async () => {},
  }, new AbortController().signal)
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(visited, ['overview', 'corpus', 'document', 'empty', 'build'])
  build.resolve()
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(visited.slice(5), ['chunking', 'embeddings', 'sqlite', 'comparison', 'chunk', 'search'])
  search.resolve()
  await running
  assert.deepEqual(visited, TOUR_STEPS.map(step => step.id))
})

test('cancelled tour cannot advance to the next scene', async () => {
  const controller = new AbortController()
  const waiting = deferred()
  const visited = []
  const running = runTour({
    enter: async id => { visited.push(id) },
    pause: async () => waiting.promise,
  }, controller.signal)
  await new Promise(resolve => setImmediate(resolve))
  controller.abort()
  waiting.resolve()
  await assert.rejects(running, { name: 'AbortError' })
  assert.deepEqual(visited, ['overview'])
})

test('failed build stops the tour without a success scene', async () => {
  const visited = []
  await assert.rejects(runTour({
    enter: async id => { visited.push(id); if (id === 'build') throw new Error('build failed') },
    pause: async () => {},
  }, new AbortController().signal), /build failed/)
  assert.deepEqual(visited, ['overview', 'corpus', 'document', 'empty', 'build'])
})

test('empty index scene precedes the first build', () => {
  const scenes = TOUR_STEPS.map(step => step.id)
  assert.equal(scenes.indexOf('empty'), scenes.indexOf('build') - 1)
  assert.ok(TOUR_STEPS.find(step => step.id === 'empty').seconds > 0)
})
