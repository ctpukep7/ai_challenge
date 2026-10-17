export function nextHistory(history, question, answer) {
  return [...history, {role: 'user', content: question}, {role: 'assistant', content: answer}].slice(-8);
}

export function groupSources(sources = []) {
  const grouped = new Map();
  for (const source of sources) {
    const key = JSON.stringify([source.id, source.kind, source.path, source.start_line, source.end_line, source.sha256]);
    if (!grouped.has(key)) grouped.set(key, {...source, groupKey: key, quotes: []});
    const target = grouped.get(key);
    if (source.quote && !target.quotes.includes(source.quote)) target.quotes.push(source.quote);
  }
  return [...grouped.values()];
}

export const toolLabels = {
  search_documentation: 'Поиск документации', git_branch: 'Текущая ветка',
  list_project_files: 'Список файлов', read_project_file: 'Чтение исходника', git_diff: 'Текущие изменения',
};
export function toolSteps(trace = []) {
  return trace.filter(item => ['tools/call', 'tool_request_rejected'].includes(item.method)).map(item => ({
    ...item, label: toolLabels[item.tool] || item.tool || 'Неизвестный инструмент',
    rejected: item.method === 'tool_request_rejected',
    failed: item.method === 'tool_request_rejected' || Boolean(item.result?.error),
  }));
}
export function argumentsLabel(args) {
  if (!args || !Object.keys(args).length) return 'Без аргументов';
  return Object.entries(args).map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`).join(' · ');
}
export function resultSummary(result = {}) {
  if (result.error) return result.error;
  if ('branch' in result) return `${result.detached ? 'Detached HEAD' : result.branch} · ${result.sha || ''}`;
  if ('public_changed_total' in result) return `Изменённых публичных файлов: ${result.public_changed_total}; патчей показано: ${result.shown_patches}`;
  if (result.files) return `Публичных файлов: ${result.files.length} из ${result.total}`;
  if (result.path) return `${result.path} · строки ${result.returned_start_line ?? '?'}–${result.returned_end_line ?? '?'}`;
  return `Получено фрагментов: ${result.evidence?.length ?? 0}`;
}
