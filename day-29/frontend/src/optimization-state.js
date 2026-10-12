export const DEFAULT_SETTINGS = Object.freeze({ temperature: 0, num_predict: 1024, num_ctx: 4096, top_k: 20, top_p: .8, min_p: 0, repeat_penalty: 1, repeat_last_n: 64, seed: -1, stop: Object.freeze([]), system: '', presence_penalty: 0, format: null });
export const BASELINE_SETTINGS = Object.freeze({ temperature: .7, num_predict: 1024, num_ctx: 4096, top_k: 20, top_p: .8, min_p: 0, repeat_penalty: 1, repeat_last_n: 64, seed: -1, stop: Object.freeze([]), system: '', presence_penalty: 0, format: null });
export const PARAMETER_LABELS = Object.freeze({ temperature: 'Temperature', num_predict: 'Лимит ответа (num_predict)', num_ctx: 'Окно контекста (num_ctx)', top_k: 'Top K', top_p: 'Top P', min_p: 'Min P', repeat_penalty: 'Штраф за повторы', repeat_last_n: 'Окно проверки повторов', seed: 'Seed', stop: 'Стоп-фразы', presence_penalty: 'Штраф за появление (presence penalty)', format: 'Формат ответа' });
const finite = (value) => typeof value === 'number' && Number.isFinite(value);
const nonnegative = (value) => finite(value) && value >= 0;
const optionalNumber = (value) => value == null || nonnegative(value);
const inRange = (value, min, max) => finite(value) && value >= min && value <= max;
const integerRange = (value, min, max) => Number.isSafeInteger(value) && value >= min && value <= max;
const chars = (text) => Array.from(text).length;
const bytes = (text) => new TextEncoder().encode(text).length;
const cloneValue = (value) => value && typeof value === 'object' ? Array.isArray(value) ? value.map(cloneValue) : Object.fromEntries(Object.entries(value).map(([key, child]) => [key, cloneValue(child)])) : value;
export function cloneSettings(settings) { return { ...cloneValue(settings), format: Object.hasOwn(settings, 'format') ? cloneValue(settings.format) : null }; }
export function formatError(format) {
  if (format == null || format === 'json') return '';
  if (typeof format !== 'object' || Array.isArray(format) || !Object.keys(format).length) return 'JSON Schema: нужен непустой JSON-объект.';
  let count = 0, error = '';
  function visit(value, depth = 1) {
    if (++count > 256 || depth > 8) { error = 'JSON Schema: максимум 8 уровней и 256 элементов.'; return; }
    if (value && typeof value === 'object') {
      if (Object.hasOwn(value, '$ref')) { error = 'JSON Schema: ссылки $ref не поддерживаются.'; return; }
      for (const child of Object.values(value)) { if (error) break; visit(child, depth + 1); }
    } else if (!(value === null || ['string', 'boolean'].includes(typeof value) || finite(value))) error = 'JSON Schema: допустимы только значения JSON.';
  }
  visit(format);
  if (error) return error;
  const text = JSON.stringify(format);
  return chars(text) > 5000 || bytes(text) > 12000 ? 'JSON Schema: максимум 5000 символов и 12000 байт UTF-8.' : '';
}
export function parseSchema(text) {
  try { const value = JSON.parse(text); return { value, error: formatError(value) || (value === null || typeof value !== 'object' || Array.isArray(value) ? 'JSON Schema: нужен непустой JSON-объект.' : '') }; }
  catch { return { value: null, error: 'JSON Schema: некорректный JSON. Проверьте кавычки, запятые и скобки.' }; }
}
export function profileSettings(profile) {
  const parameters = profile?.parameters ?? {};
  return cloneSettings(Object.fromEntries(Object.keys(DEFAULT_SETTINGS).map((key) => [key, key === 'system' ? profile?.system : key === 'presence_penalty' && !Object.hasOwn(parameters, key) ? 0 : key === 'format' && !Object.hasOwn(parameters, key) ? null : parameters[key]])));
}
export function createSettingsEditor() { return { draft: null, initialized: false }; }
export function refreshSettingsEditor(editor, service) {
  return editor.initialized ? editor : { draft: profileSettings(service.profiles.optimized), initialized: true };
}
export function resetSettingsEditor(editor, service, pending = false) {
  return pending || !service?.profiles?.optimized ? editor : { draft: profileSettings(service.profiles.optimized), initialized: true };
}
export function stopToText(stops) {
  return stops.map((text) => text.replace(/\\/g, '\\\\').replace(/\n/g, '\\n').replace(/\r/g, '\\r')).join('\n');
}
export function stopFromText(text) {
  return text === '' ? [] : text.split('\n').map((phrase) => phrase.replace(/\\([\\nr])/g, (_match, symbol) => symbol === 'n' ? '\n' : symbol === 'r' ? '\r' : '\\'));
}
export function settingsError(settings) {
  if (!settings || typeof settings !== 'object' || Array.isArray(settings) || Object.keys(settings).some((key) => !Object.hasOwn(DEFAULT_SETTINGS, key))) return 'Сервис или форма содержат неизвестные параметры.';
  const formatProblem = formatError(settings.format); if (formatProblem) return formatProblem;
  if (!inRange(settings.temperature, 0, 2)) return 'Temperature: число от 0 до 2.';
  if (![256, 512, 768, 1024, 1536, 2048].includes(settings.num_predict)) return 'Лимит ответа: 256, 512, 768, 1024, 1536 или 2048 токенов.';
  if (![3072, 4096, 6144, 8192].includes(settings.num_ctx)) return 'Окно контекста: 3072, 4096, 6144 или 8192 токена.';
  if (!integerRange(settings.top_k, 0, 100)) return 'Top K: целое число от 0 до 100.';
  if (!inRange(settings.top_p, 0, 1)) return 'Top P: число от 0 до 1.';
  if (!inRange(settings.min_p, 0, 1)) return 'Min P: число от 0 до 1.';
  if (!inRange(Object.hasOwn(settings, 'presence_penalty') ? settings.presence_penalty : 0, 0, 2)) return 'Presence penalty: число от 0 до 2.';
  if (!inRange(settings.repeat_penalty, .8, 1.5)) return 'Штраф за повторы: число от 0,8 до 1,5.';
  if (!integerRange(settings.repeat_last_n, 0, 2048)) return 'Окно повторов: целое число от 0 до 2048.';
  if (!integerRange(settings.seed, -1, 2147483647)) return 'Seed: целое число от −1 до 2147483647.';
  if (!Array.isArray(settings.stop) || settings.stop.length > 4 || settings.stop.some((text) => typeof text !== 'string' || text.length === 0 || chars(text) > 80 || bytes(text) > 256)) return 'Стоп-фразы: до 4 непустых строк, каждая до 80 символов и 256 байт. Пустые строки удалите.';
  if (new Set(settings.stop).size !== settings.stop.length) return 'Стоп-фразы не должны повторяться.';
  if (typeof settings.system !== 'string' || chars(settings.system) > 3000 || bytes(settings.system) > 8000) return 'Системная инструкция: до 3000 символов и 8000 байт UTF-8; можно оставить пустой.';
  return '';
}
export function parameterRows(parameters) {
  return Object.entries(PARAMETER_LABELS).map(([key, label]) => ({ key, label, value: key === 'format' ? parameters?.format == null ? 'Обычный текст' : JSON.stringify(parameters.format) : key === 'stop' ? Array.isArray(parameters?.stop) ? JSON.stringify(parameters.stop) : 'Не предоставлено' : finite(parameters?.[key]) ? Number.isInteger(parameters[key]) ? parameters[key].toLocaleString('ru-RU', { maximumFractionDigits: 0 }) : String(parameters[key]).replace('.', ',') : 'Не предоставлено' }));
}
export function questionError(question) {
  const text = question.trim();
  if (!text) return 'Напишите свой запрос.';
  if (chars(text) > 4000 || bytes(text) > 10000) return 'Сократите вопрос: максимум 4000 символов и 10000 байт UTF-8.';
  return '';
}
export function createState() { return { draft: '', results: [], pending: null, error: '' }; }
export function beginQuestion(state, settings, mode = 'compare') {
  if (state.pending) throw new Error('Дождитесь предыдущего ответа.');
  if (!['compare', 'optimized'].includes(mode)) throw new Error('Неизвестный режим запроса.');
  const error = questionError(state.draft) || settingsError(settings);
  if (error) throw new Error(error);
  return { ...state, pending: { question: state.draft.trim(), settings: cloneSettings(settings), mode }, error: '' };
}
export function requestPayload(state) {
  if (!state.pending) throw new Error('Нет вопроса для отправки.');
  return { question: state.pending.question, settings: cloneSettings(state.pending.settings) };
}
export function requestEndpoint(state) { return state.pending?.mode === 'compare' ? '/api/compare' : '/api/ask'; }
export function shouldSend(event) { return event.key === 'Enter' && !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey && !event.isComposing && event.keyCode !== 229; }
const metricNames = ['total_seconds', 'preload_seconds', 'preload_load_seconds', 'http_seconds', 'load_seconds', 'generation_seconds', 'prompt_tokens', 'output_tokens', 'tokens_per_second'];
export function validProvider(provider, profile) {
  return Boolean(provider && provider.profile === profile && typeof provider.label === 'string' && typeof provider.model === 'string' && !settingsError(profileSettings(provider)) && (profile !== 'baseline' || provider.system === '' && profileSettings(provider).format === null)
    && ['answered', 'error'].includes(provider.status) && typeof provider.answer === 'string' && provider.answer.trim() && typeof provider.called === 'boolean'
    && typeof provider.truncated === 'boolean' && typeof provider.reason === 'string' && (provider.context_warning == null || typeof provider.context_warning === 'string')
    && (provider.status !== 'error' || provider.error && typeof provider.error.message === 'string')
    && (provider.metrics == null || typeof provider.metrics === 'object' && !Array.isArray(provider.metrics) && metricNames.every((key) => optionalNumber(provider.metrics[key])))
    && (provider.resources == null || typeof provider.resources === 'object' && !Array.isArray(provider.resources) && ['peak_size_bytes', 'peak_size_vram_bytes', 'context_length', 'samples'].every((key) => optionalNumber(provider.resources[key]))));
}
export function validService(data) {
  return Boolean(data && ['ready', 'unavailable'].includes(data.status) && typeof data.model === 'string' && typeof data.message === 'string'
    && data.profiles?.baseline?.system === '' && profileSettings(data.profiles.baseline).format === null && ['baseline', 'optimized'].every((id) => data.profiles?.[id] && !settingsError(profileSettings(data.profiles[id]))));
}
export function validResult(result) {
  return Boolean(result && typeof result.question === 'string' && ['compare', 'optimized'].includes(result.mode) && !settingsError(result.settings)
    && validProvider(result.optimized, 'optimized') && (result.mode !== 'compare' || validProvider(result.baseline, 'baseline'))
    && result.comparison?.same_input === true && typeof result.comparison.input_sha256 === 'string' && /^[a-f0-9]{64}$/.test(result.comparison.input_sha256)
    && Array.isArray(result.order) && result.order.length === (result.mode === 'compare' ? 2 : 1) && new Set(result.order).size === result.order.length && result.order.includes('optimized') && (result.mode !== 'compare' || result.order.includes('baseline')));
}
const compatibleValue = (settings, key) => key === 'format' && !Object.hasOwn(settings ?? {}, key) ? null : key === 'presence_penalty' && !Object.hasOwn(settings ?? {}, key) ? 0 : settings?.[key];
const equalJson = (a, b) => a === b || Boolean(a && b && typeof a === 'object' && typeof b === 'object' && Array.isArray(a) === Array.isArray(b) && Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(key => Object.hasOwn(b, key) && equalJson(a[key], b[key])));
const sameSettings = (a, b) => Object.keys(DEFAULT_SETTINGS).every((key) => key === 'format' ? equalJson(compatibleValue(a, key), compatibleValue(b, key)) : key === 'stop' ? Array.isArray(a?.stop) && Array.isArray(b?.stop) && a.stop.length === b.stop.length && a.stop.every((text, index) => text === b.stop[index]) : compatibleValue(a, key) === compatibleValue(b, key));
export function finishQuestion(state, result) {
  if (!state.pending || !validResult(result) || result.question.trim() !== state.pending.question || result.mode !== state.pending.mode
    || !sameSettings(result.settings, state.pending.settings) || !sameSettings(profileSettings(result.optimized), result.settings)
    || result.mode === 'compare' && !sameSettings(profileSettings(result.baseline), BASELINE_SETTINGS)) throw new Error('Сервис вернул некорректный ответ. Вопрос и параметры сохранены.');
  return { ...state, draft: '', results: [...state.results, structuredClone(result)], pending: null, error: '' };
}
export function failQuestion(state, message) { return { ...state, pending: null, draft: state.pending?.question ?? state.draft, error: message }; }
export function clearResults(state) { return state.pending ? state : createState(); }
