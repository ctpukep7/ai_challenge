import test from 'node:test';
import assert from 'node:assert/strict';
import { answerStatistics } from '../src/answer-statistics.js';

const shared = { retrieval_seconds: .5, embedding_load_seconds: .4, embedding_cold: true };
const local = () => ({ status: 'answered', reason: '', sources: [{}, {}], metrics: { ...shared, total_seconds: 8.5, http_seconds: 8, load_seconds: 1, generation_seconds: 4, prompt_tokens: 200, output_tokens: 80, tokens_per_second: 20 } });
const cloud = () => ({ status: 'answered', called: true, sources: [{}, {}], metrics: { total_seconds: 1.6, http_seconds: 1.1, prompt_tokens: 230, output_tokens: 55 } });
const values = (rows) => Object.fromEntries(rows.map((row) => [row.key, row.value]));
const cloudValues = (provider, metrics = shared) => values(answerStatistics(provider, { cloud: true, sharedMetrics: metrics }));

test('обе модели имеют 13 одинаковых подписей в одинаковом порядке', () => {
  const a = answerStatistics(local());
  const b = answerStatistics(cloud(), { cloud: true, sharedMetrics: shared });
  assert.equal(a.length, 13);
  assert.deepEqual(a.map(({ key, label }) => ({ key, label })), b.map(({ key, label }) => ({ key, label })));
});

test('cloud получает только общие retrieval/embeddings, а не total/http/usage локальной модели', () => {
  const rows = cloudValues(cloud(), local().metrics);
  assert.equal(rows.total, '1,6 с');
  assert.equal(rows.http, '1,1 с');
  assert.equal(rows.retrieval, '0,5 с');
  assert.equal(rows.embedding_load, '0,4 с');
  assert.equal(rows.embedding_cold, 'Да');
  assert.equal(rows.prompt_tokens, '230');
  assert.equal(rows.output_tokens, '55');
  assert.equal(rows.total_tokens, '285');
  assert.equal(rows.model_load, 'Не предоставлено');
  assert.equal(rows.generation, 'Не предоставлено');
  assert.equal(rows.generation_speed, 'Не предоставлено');
  // Embeddingload уже входит в retrieval; полного времени не прибавляем заново.
  assert.notEqual(rows.total, '2 с');
});

test('скорость получения по HTTP отличается от чистой скорости генерации', () => {
  const rows = values(answerStatistics(local()));
  assert.equal(rows.received_speed, '10');
  assert.equal(rows.generation_speed, '20');
  assert.equal(rows.total_tokens, '280');
  assert.equal(rows.chunks, '2');
});

test('измеренные нули и false отображаются честно, HTTP0 не приводит к делению', () => {
  const provider = local();
  provider.metrics = Object.fromEntries(Object.keys(provider.metrics).map((key) => [key, key === 'embedding_cold' ? false : 0]));
  const rows = values(answerStatistics(provider));
  assert.equal(rows.total, '0 с');
  assert.equal(rows.retrieval, '0 с');
  assert.equal(rows.embedding_load, '0 с');
  assert.equal(rows.model_load, '0 с');
  assert.equal(rows.embedding_cold, 'Нет');
  assert.equal(rows.total_tokens, '0');
  assert.equal(rows.generation_speed, '0');
  assert.equal(rows.received_speed, 'Не вычисляется (HTTP = 0)');
});

test('ошибка модели скрывает placeholder0, но оставляет общую фазу и измеренное время', () => {
  const provider = local();
  provider.status = 'error';
  provider.metrics = { ...provider.metrics, http_seconds: 0, load_seconds: 0, generation_seconds: 0, prompt_tokens: 0, output_tokens: 0, tokens_per_second: 0 };
  const rows = values(answerStatistics(provider));
  assert.equal(rows.total, '8,5 с');
  for (const key of ['http', 'model_load', 'generation', 'prompt_tokens', 'output_tokens', 'total_tokens', 'received_speed', 'generation_speed']) assert.equal(rows[key], 'Нет данных');
  assert.equal(rows.retrieval, '0,5 с');
  assert.equal(rows.chunks, '2');
  assert.equal(cloudValues({ ...cloud(), status: 'error', metrics: null }).total, 'Нет данных');
  assert.equal(cloudValues({ ...cloud(), status: 'error', metrics: null }).retrieval, '0,5 с');
});

test('при отказе до генерации поля модели не изображают выполненный запрос', () => {
  for (const reason of ['weak_context', 'context_limit']) {
    const provider = { ...local(), status: 'unknown', reason, sources: [] };
    const rows = values(answerStatistics(provider));
    assert.equal(rows.http, 'Не выполнялось');
    assert.equal(rows.prompt_tokens, 'Не выполнялось');
    assert.equal(rows.received_speed, 'Не выполнялось');
    assert.equal(rows.retrieval, '0,5 с');
    assert.equal(rows.chunks, '0');
  }
  const rows = cloudValues({ ...cloud(), status: 'unknown', called: false, sources: [], metrics: null });
  assert.equal(rows.total, 'Не выполнялось');
  assert.equal(rows.generation, 'Не выполнялось');
  assert.equal(rows.retrieval, '0,5 с');
});

test('unknown после генерации сохраняет измерения, это не техническая ошибка', () => {
  const provider = { ...local(), status: 'unknown', reason: 'missing_evidence' };
  assert.equal(values(answerStatistics(provider)).http, '8 с');
  assert.equal(values(answerStatistics(provider)).prompt_tokens, '200');
});

test('отсутствующие и некорректные числа не заменяются нулями', () => {
  const provider = cloud();
  provider.metrics = { http_seconds: NaN, total_seconds: Infinity, prompt_tokens: 1.5, output_tokens: -1 };
  const rows = cloudValues(provider, null);
  for (const key of ['total', 'http', 'retrieval', 'embedding_load', 'embedding_cold', 'prompt_tokens', 'output_tokens', 'total_tokens', 'received_speed']) assert.equal(rows[key], 'Не предоставлено');
});

test('числа форматируются ru-RU: секунды до2, скорость до1 знака', () => {
  const provider = local();
  provider.metrics.http_seconds = 3;
  provider.metrics.output_tokens = 100;
  provider.metrics.total_seconds = 3.456;
  provider.metrics.tokens_per_second = 40.567;
  const rows = values(answerStatistics(provider));
  assert.equal(rows.total, '3,46 с');
  assert.equal(rows.received_speed, '33,3');
  assert.equal(rows.generation_speed, '40,6');
});

test('малые измеренные длительности не округляются до фиктивного нуля', () => {
  const provider = local();
  provider.metrics.load_seconds = .0022;
  provider.metrics.embedding_load_seconds = 0;
  provider.metrics.generation_seconds = .01;
  const rows = values(answerStatistics(provider));
  assert.equal(rows.model_load, '< 0,01 с');
  assert.equal(rows.embedding_load, '0 с');
  assert.equal(rows.generation, '0,01 с');
});
