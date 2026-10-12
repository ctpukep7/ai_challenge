import test from 'node:test';
import assert from 'node:assert/strict';
import { beginQuestion, createState, failQuestion, finishQuestion, insertQuestion, requestEndpoint, requestPayload } from '../src/rag-state.js';

const question = 'Как сохраняется история в дне 7?';
const sources = [{ marker: '[1]', source: 'day-07/README.md', title: 'Память', section: 'Что сохраняется', chunk_id: 'c1', text: 'История в SQLite.', score: .78, role: 'anchor', cited: true }];
const archive = { answer: 'Архивный ответ', status: 'unknown', model: 'DeepSeek (архив)', source: 'Day24', historical: true, metrics: null, note: 'Другой prompt/verifier' };
const local = () => ({
  question, mode: 'local', status: 'answered', answer: 'SQLite [1].', model: 'qwen3:4b-instruct-2507-q4_K_M', truncated: false, reason: 'evidence', citation_warning: null, sources,
  metrics: { retrieval_seconds: .1, embedding_load_seconds: .4, embedding_cold: true, http_seconds: 2, load_seconds: .2, generation_seconds: 1.8, prompt_tokens: 250, output_tokens: 70, tokens_per_second: 38.9, total_seconds: 2.5 }, cloud: null, archive,
});
const comparison = () => ({
  ...local(), mode: 'compare', error: null,
  cloud: {
    historical: false, model: 'deepseek-flash', status: 'answered', answer: 'Диалоги сохраняются в SQLite [1].', truncated: false, reason: 'evidence', citation_warning: null, sources: sources.map((source) => ({ ...source, cited: false })),
    metrics: { http_seconds: 1.8, total_seconds: 1.8, prompt_tokens: 280, output_tokens: 40 }, error: null, called: true,
  }, comparison: { identical_messages: true, messages_sha256: 'a'.repeat(64), chunks: 1, total_seconds: 2.5, temperature: .7, max_tokens: 1024, thinking: 'disabled' },
});
const pending = (mode = 'local') => beginQuestion({ ...createState(), draft: question }, mode);

test('обычная отправка всегда local; compare требует явного выбора режима', () => {
  assert.equal(pending().pending.mode, 'local');
  assert.equal(requestEndpoint(pending()), '/api/ask');
  assert.equal(requestEndpoint(pending('compare')), '/api/compare');
  assert.deepEqual(requestPayload(pending()), requestPayload(pending('compare')));
  assert.deepEqual(requestPayload(pending()), { question });
  assert.throws(() => beginQuestion({ ...createState(), draft: question }, 'auto'), /Неизвестный/);
});

test('подстановка не создаёт сравнение или локальный запрос', () => {
  const selected = insertQuestion(createState(), question);
  assert.equal(selected.pending, null);
  assert.throws(() => requestEndpoint(selected), /Нет вопроса/);
});

test('обычный ответ содержит один локальный результат и отдельный архив', () => {
  const result = finishQuestion(pending(), local()).results[0];
  assert.equal(result.mode, 'local');
  assert.equal(result.cloud, null);
  assert.equal(result.archive, archive);
  assert.equal(result.status, 'answered');
});

test('живое сравнение сохраняет разные метрики и независимые cited', () => {
  const result = finishQuestion(pending('compare'), comparison()).results[0];
  assert.equal(result.cloud.historical, false);
  assert.equal(result.comparison.identical_messages, true);
  assert.equal(result.sources[0].cited, true);
  assert.equal(result.cloud.sources[0].cited, false);
  assert.equal(result.metrics.generation_seconds, 1.8);
  assert.equal(result.cloud.metrics.generation_seconds, undefined);
  assert.equal(result.cloud.metrics.tokens_per_second, undefined);
});

test('ошибка облачной модели не скрывает локальный ответ и не заменяется архивом', () => {
  const reply = comparison();
  reply.cloud = { ...reply.cloud, status: 'error', answer: 'Облачная модель недоступна.', metrics: null, error: { code: 'cloud_timeout', message: 'DeepSeek не ответил вовремя.' } };
  const result = finishQuestion(pending('compare'), reply).results[0];
  assert.equal(result.status, 'answered');
  assert.equal(result.answer, 'SQLite [1].');
  assert.equal(result.cloud.status, 'error');
  assert.equal(result.cloud.error.message, 'DeepSeek не ответил вовремя.');
  assert.notEqual(result.cloud.answer, archive.answer);
  assert.equal(result.archive, archive);
});

test('ошибка локальной модели не скрывает ответ облачной', () => {
  const reply = { ...comparison(), status: 'error', answer: 'Ollama недоступна.', error: { code: 'unavailable', message: 'Ollama недоступна.' } };
  for (const key of Object.keys(reply.metrics)) if (key !== 'embedding_cold') reply.metrics[key] = 0;
  const result = finishQuestion(pending('compare'), reply).results[0];
  assert.equal(result.status, 'error');
  assert.equal(result.cloud.status, 'answered');
  assert.match(result.cloud.answer, /SQLite/);
  assert.equal(result.metrics.total_seconds, 0);
});

test('unknown без sharedcontext не изображает облачный вызов', () => {
  const reply = { ...comparison(), status: 'unknown', answer: 'Не знаю.', sources: [] };
  reply.cloud = { ...reply.cloud, status: 'unknown', answer: 'Сравнение не выполнено: нет источников.', called: false, sources: [], metrics: null };
  reply.comparison.chunks = 0;
  const result = finishQuestion(pending('compare'), reply).results[0];
  assert.equal(result.cloud.called, false);
  assert.equal(result.cloud.metrics, null);
  assert.equal(result.comparison.chunks, 0);
});

test('API-ошибка сравнения возвращает вопрос без результата и автоповтора', () => {
  const state = failQuestion(pending('compare'), 'Ключ DeepSeek не настроен');
  assert.equal(state.pending, null);
  assert.equal(state.draft, question);
  assert.deepEqual(state.results, []);
  assert.throws(() => requestEndpoint(state), /Нет вопроса/);
  assert.equal(requestEndpoint(beginQuestion(state)), '/api/ask');
});

test('архив не принимается за live, ошибка не принимается как local-only ответ', () => {
  assert.throws(() => finishQuestion(pending('compare'), { ...comparison(), cloud: archive }), /некорректный/);
  assert.throws(() => finishQuestion(pending(), { ...local(), status: 'error', error: { code: 'bad', message: 'Ошибка' } }), /некорректный/);
  assert.throws(() => finishQuestion(pending(), comparison()), /некорректный/);
  assert.throws(() => finishQuestion(pending('compare'), local()), /некорректный/);
});

test('невалидные live-метрики и отсутствующее доказательство sharedcontext не коммитятся', () => {
  const invalidMetrics = comparison();
  invalidMetrics.cloud.metrics.http_seconds = Infinity;
  assert.throws(() => finishQuestion(pending('compare'), invalidMetrics), /некорректный/);
  const invalidContext = comparison();
  invalidContext.comparison.identical_messages = false;
  assert.throws(() => finishQuestion(pending('compare'), invalidContext), /некорректный/);
});
