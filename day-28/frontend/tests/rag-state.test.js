import test from 'node:test';
import assert from 'node:assert/strict';
import { beginQuestion, characterCount, clearResults, createState, failQuestion, finishQuestion, insertQuestion, questionError, requestEndpoint, requestPayload, shouldSend, utf8Bytes } from '../src/rag-state.js';

const response = (question = 'Как устроен день 7?') => ({
  question, status: 'answered', answer: 'История в SQLite [1].', model: 'qwen3:4b-instruct-2507-q4_K_M', truncated: false, reason: 'evidence', citation_warning: null,
  sources: [{ marker: '[1]', source: 'day-07/README.md', title: 'Память', section: 'Что сохраняется', chunk_id: 'c1', text: 'История в SQLite.', score: .78, role: 'anchor', cited: true }],
  metrics: { retrieval_seconds: .1, embedding_load_seconds: .4, embedding_cold: true, http_seconds: 2, load_seconds: .2, generation_seconds: 1.8, prompt_tokens: 250, output_tokens: 70, tokens_per_second: 38.9, total_seconds: 2.5 },
  scope: { kind: 'source', sources: ['day-07/README.md'], reason: 'явный день' }, cloud: null,
});

test('каждый запрос независим: отправляется только новый вопрос без истории', () => {
  const original = { ...createState(), results: [response()], draft: '  Что делает день 9?  ' };
  const pending = beginQuestion(original);
  assert.deepEqual(requestPayload(pending), { question: 'Что делает день 9?' });
  assert.equal(pending.results, original.results);
  assert.equal(pending.draft, '');
  assert.equal(original.draft, '  Что делает день 9?  ');
});

test('ручной выбор вопроса только заменяет черновик, не создаёт запрос', () => {
  const original = { ...createState(), draft: 'Личный вопрос', results: [response()] };
  const selected = insertQuestion(original, 'Вопрос к README');
  assert.equal(selected.pending, null);
  assert.equal(selected.results, original.results);
  assert.equal(selected.draft, 'Вопрос к README');
  assert.throws(() => requestPayload(selected), /Нет вопроса/);
});

test('успешный локальный ответ сохраняет источники и метрики без облачной карточки для своего вопроса', () => {
  const pending = beginQuestion({ ...createState(), draft: 'Как устроен день 7?' });
  const completed = finishQuestion(pending, response());
  assert.equal(completed.pending, null);
  assert.equal(completed.results.length, 1);
  assert.equal(completed.results[0].sources[0].chunk_id, 'c1');
  assert.equal(completed.results[0].metrics.embedding_cold, true);
  assert.equal(completed.results[0].cloud, null);
});

test('архив отображается только когда сервер прислал историческое сравнение', () => {
  const pending = beginQuestion({ ...createState(), draft: 'Как устроен день 7?' });
  const cloud = { answer: 'Архивный ответ', status: 'answered', model: 'DeepSeek (архив)', source: 'Day24', historical: true, metrics: null, note: 'Другой prompt; время не сохранено' };
  const normalized = finishQuestion(pending, { ...response(), cloud }).results[0];
  assert.equal(normalized.archive, cloud);
  assert.equal(normalized.cloud, null);
  assert.throws(() => finishQuestion(pending, { ...response(), cloud: { ...cloud, historical: false } }), /некорректный/);
});

test('ошибка не добавляет результат и возвращает вопрос для ручного повтора', () => {
  const results = [response()];
  const pending = beginQuestion({ ...createState(), results, draft: 'Мой новый вопрос' });
  const failed = failQuestion(pending, 'Сервис занят');
  assert.equal(failed.results, results);
  assert.equal(failed.draft, 'Мой новый вопрос');
  assert.equal(failed.pending, null);
  assert.equal(failed.error, 'Сервис занят');
});

test('во время ожидания нельзя отправить второй вопрос, вставить готовый или очистить результаты', () => {
  const pending = beginQuestion({ ...createState(), draft: 'Вопрос' });
  assert.throws(() => beginQuestion(pending), /Дождитесь/);
  assert.equal(insertQuestion(pending, 'Другой вопрос'), pending);
  assert.equal(clearResults(pending), pending);
});

test('новый чат очищает только локальное состояние', () => {
  assert.deepEqual(clearResults({ ...createState(), results: [response()], error: 'Ошибка', draft: 'Черновик' }), createState());
});

test('лимиты считают реальные Unicode-символы и UTF-8 байты', () => {
  assert.equal(characterCount('🙂'), 1);
  assert.equal(utf8Bytes('🙂'), 4);
  assert.equal(questionError('я'.repeat(500)), '');
  assert.equal(questionError('a'.repeat(500)), '');
  assert.match(questionError('a'.repeat(501)), /500/);
  assert.equal(questionError('🙂'.repeat(250)), '');
  assert.match(questionError('🙂'.repeat(251)), /1000/);
  assert.match(questionError(' \n '), /Напишите/);
});

test('unknown без генерации допустим с нулевыми метриками и пустыми источниками', () => {
  const result = { ...response(), status: 'unknown', answer: 'Не знаю.', sources: [], reason: 'missing_evidence' };
  for (const name of Object.keys(result.metrics)) if (name !== 'embedding_cold') result.metrics[name] = 0;
  assert.equal(finishQuestion(beginQuestion({ ...createState(), draft: result.question }), result).results[0].status, 'unknown');
});

test('чужой вопрос, пустой ответ, неверный source и бесконечные метрики не коммитятся', () => {
  const pending = beginQuestion({ ...createState(), draft: 'Как устроен день 7?' });
  const malformed = [response('Другой вопрос'), { ...response(), answer: ' ' }, { ...response(), sources: [{ ...response().sources[0], score: Infinity }] }, { ...response(), metrics: { ...response().metrics, total_seconds: NaN } }];
  for (const result of malformed) assert.throws(() => finishQuestion(pending, result), /некорректный/);
  assert.deepEqual(pending.results, []);
});

test('Enter отправляет, перенос строки и IME/модификаторы не отправляют', () => {
  assert.equal(shouldSend({ key: 'Enter' }), true);
  for (const modifier of ['shiftKey', 'ctrlKey', 'altKey', 'metaKey', 'isComposing']) assert.equal(shouldSend({ key: 'Enter', [modifier]: true }), false);
  assert.equal(shouldSend({ key: 'Enter', keyCode: 229 }), false);
  assert.equal(shouldSend({ key: 'Tab' }), false);
});
