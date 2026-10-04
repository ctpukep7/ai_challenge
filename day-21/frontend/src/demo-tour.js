export const TOUR_STEPS = [
  { id: 'overview', seconds: 7 },
  { id: 'corpus', seconds: 7 },
  { id: 'document', seconds: 7 },
  { id: 'empty', seconds: 8 },
  { id: 'build', seconds: 6 },
  { id: 'chunking', seconds: 7 },
  { id: 'embeddings', seconds: 7 },
  { id: 'sqlite', seconds: 7 },
  { id: 'comparison', seconds: 7 },
  { id: 'chunk', seconds: 7 },
  { id: 'search', seconds: 7 },
  { id: 'search-structure', seconds: 6 },
  { id: 'finished', seconds: 0 },
]

export function throwIfAborted(signal) {
  if (signal.aborted) throw new DOMException('Показ остановлен', 'AbortError')
}

export async function runTour({ enter, pause }, signal) {
  for (const [index, step] of TOUR_STEPS.entries()) {
    throwIfAborted(signal)
    await enter(step.id, index + 1, signal)
    throwIfAborted(signal)
    if (step.seconds) await pause(step.seconds * 1000, signal)
  }
}
