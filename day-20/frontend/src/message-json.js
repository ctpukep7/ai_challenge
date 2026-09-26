export function jsonMessageOwners(messages = []) {
  const owners = new Map()
  for (const message of messages) {
    if (!message.run_id || !['user', 'assistant'].includes(message.role)) continue
    const previous = owners.get(message.run_id)
    if (!previous || message.role === 'assistant') owners.set(message.run_id, message)
  }
  return new Set([...owners.values()].map(message => message.id))
}

export function jsonRunActive(id, runs = [], plans = [], pendingPlanIds = new Set(), currentRun = null) {
  if (!id) return false
  const status = currentRun?.id === id ? currentRun.status : runs.find(run => run.id === id)?.status
  if (['queued', 'running'].includes(status)) return true
  return plans.some(plan => plan.run_id === id && (
    pendingPlanIds.has(plan.id) || ['approved', 'writing'].includes(plan.status)
  ))
}
