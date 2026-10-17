// An explicit empty plural list is authoritative (for example after an error).
export function taskChanges(task) {
  if (Array.isArray(task?.changes)) return task.changes;
  return task?.change ? [task.change] : [];
}
