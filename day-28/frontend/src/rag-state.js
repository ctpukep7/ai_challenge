export const MAX_QUESTION_CHARS = 500;
export const MAX_QUESTION_BYTES = 1000;
export const utf8Bytes = (text) => new TextEncoder().encode(text).length;
export const characterCount = (text) => Array.from(text).length;

export function createState() {
  return { draft: '', results: [], pending: null, error: '' };
}

export function questionError(question) {
  const text = question.trim();
  if (!text) return 'Напишите вопрос к документации.';
  if (characterCount(text) > MAX_QUESTION_CHARS || utf8Bytes(text) > MAX_QUESTION_BYTES) {
    return 'Сократите вопрос: максимум 500 символов и 1000 байт UTF-8.';
  }
  return '';
}

export function beginQuestion(state, mode = 'local') {
  if (state.pending) throw new Error('Дождитесь ответа на предыдущий вопрос.');
  if (!['local', 'compare'].includes(mode)) throw new Error('Неизвестный режим запроса.');
  const error = questionError(state.draft);
  if (error) throw new Error(error);
  return { ...state, draft: '', pending: { question: state.draft.trim(), mode }, error: '' };
}

export function requestPayload(state) {
  if (!state.pending) throw new Error('Нет вопроса для отправки.');
  return { question: state.pending.question };
}

export function requestEndpoint(state) {
  if (!state.pending) throw new Error('Нет вопроса для отправки.');
  return state.pending.mode === 'compare' ? '/api/compare' : '/api/ask';
}

export function insertQuestion(state, question) {
  if (state.pending || typeof question !== 'string') return state;
  return { ...state, draft: question };
}

const nonnegative = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const validError = (error) => error === null || error && typeof error.code === 'string' && typeof error.message === 'string';
const validSources = (sources) => Array.isArray(sources) && sources.every((source) => source && typeof source.marker === 'string'
  && typeof source.source === 'string' && typeof source.text === 'string'
  && typeof source.section === 'string' && typeof source.chunk_id === 'string'
  && ['anchor', 'neighbor'].includes(source.role) && typeof source.cited === 'boolean'
  && (source.score === null || typeof source.score === 'number' && Number.isFinite(source.score)));
const validArchive = (archive) => archive == null || archive && typeof archive.answer === 'string'
  && ['answered', 'unknown'].includes(archive.status) && archive.historical === true
  && typeof archive.model === 'string' && typeof archive.note === 'string';

export function normalizeResult(result) {
  const mode = result.mode ?? 'local';
  return { ...result, mode, archive: result.archive ?? (result.cloud?.historical === true ? result.cloud : null), cloud: mode === 'local' && result.cloud?.historical === true ? null : result.cloud ?? null };
}

export function validResult(result) {
  const durations = ['retrieval_seconds', 'embedding_load_seconds', 'http_seconds', 'load_seconds', 'generation_seconds', 'prompt_tokens', 'output_tokens', 'tokens_per_second', 'total_seconds'];
  const compare = result?.mode === 'compare';
  const cloud = result?.cloud;
  const validLive = !compare ? cloud == null : cloud && cloud.historical === false
    && ['answered', 'unknown', 'error'].includes(cloud.status) && typeof cloud.answer === 'string' && cloud.answer.trim()
    && typeof cloud.model === 'string' && typeof cloud.truncated === 'boolean' && typeof cloud.reason === 'string'
    && (cloud.citation_warning === null || typeof cloud.citation_warning === 'string') && validSources(cloud.sources)
    && validError(cloud.error) && (cloud.status !== 'error' || cloud.error)
    && typeof cloud.called === 'boolean'
    && (cloud.metrics === null || ['http_seconds', 'total_seconds', 'prompt_tokens', 'output_tokens'].every((key) => nonnegative(cloud.metrics?.[key])))
    && result.comparison?.identical_messages === true && typeof result.comparison.messages_sha256 === 'string'
    && Number.isInteger(result.comparison.chunks) && result.comparison.chunks >= 0 && nonnegative(result.comparison.total_seconds);
  return Boolean(result && typeof result.question === 'string'
    && ['local', 'compare'].includes(result.mode ?? 'local')
    && (['answered', 'unknown'].includes(result.status) || compare && result.status === 'error')
    && typeof result.answer === 'string' && result.answer.trim()
    && typeof result.model === 'string' && typeof result.truncated === 'boolean'
    && typeof result.reason === 'string'
    && (result.citation_warning === null || typeof result.citation_warning === 'string')
    && validSources(result.sources)
    && durations.every((key) => nonnegative(result.metrics?.[key]))
    && typeof result.metrics.embedding_cold === 'boolean'
    && (!compare || validError(result.error) && (result.status !== 'error' || result.error))
    && validArchive(result.archive) && validLive);
}

export function finishQuestion(state, result) {
  const normalized = result ? normalizeResult(result) : result;
  if (!state.pending || !validResult(normalized) || normalized.question.trim() !== state.pending.question || normalized.mode !== state.pending.mode) {
    throw new Error('Сервис вернул некорректный ответ. Вопрос сохранён для повторной отправки.');
  }
  return { ...state, results: [...state.results, normalized], pending: null, error: '' };
}

export function failQuestion(state, message) {
  return { ...state, draft: state.pending?.question ?? state.draft, pending: null, error: message };
}

export function clearResults(state) {
  return state.pending ? state : createState();
}

export function shouldSend(event) {
  return event.key === 'Enter' && !event.shiftKey && !event.ctrlKey && !event.altKey
    && !event.metaKey && !event.isComposing && event.keyCode !== 229;
}
