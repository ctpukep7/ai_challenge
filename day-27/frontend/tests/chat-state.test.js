import test from 'node:test';
import assert from 'node:assert/strict';
import {
  chatPayload, clearChat, completeSubmission, createChatState,
  failSubmission, shouldSubmitKey, startSubmission, utf8Bytes,
} from '../src/chat-state.js';

const reply = (answer = 'Ответ', dropped = 0) => ({
  answer, model: 'qwen3:4b-instruct-2507-q4_K_M', dropped_pairs: dropped, truncated: false,
  metrics: { total_seconds: 2.4, load_seconds: .2, generation_seconds: 2, prompt_tokens: 18, output_tokens: 50, tokens_per_second: 25 },
});
const pair = (number) => [{ role: 'user', content: `Вопрос ${number}` }, { role: 'assistant', content: `Ответ ${number}` }];

test('отправка временная: история и сообщения не меняются до ответа', () => {
  const original = { ...createChatState(), draft: '  Мой вопрос  ' };
  const pending = startSubmission(original, original.draft);
  assert.equal(pending.pending, 'Мой вопрос');
  assert.equal(pending.draft, '');
  assert.deepEqual(pending.history, []);
  assert.deepEqual(pending.messages, []);
  assert.deepEqual(chatPayload(pending), { prompt: 'Мой вопрос', history: [] });
  assert.equal(original.draft, '  Мой вопрос  ');
});

test('успех добавляет ровно одну полную пару и метрики', () => {
  const result = completeSubmission(startSubmission(createChatState(), 'Привет'), reply());
  assert.deepEqual(result.history, [{ role: 'user', content: 'Привет' }, { role: 'assistant', content: 'Ответ' }]);
  assert.equal(result.messages.length, 2);
  assert.equal(result.messages[1].metrics.output_tokens, 50);
  assert.equal(result.pending, null);
});

test('ошибка возвращает вопрос и не меняет подтверждённую беседу', () => {
  const original = { ...createChatState(), history: pair(1), messages: pair(1) };
  const failed = failSubmission(startSubmission(original, 'Повторить меня'), 'Сервер недоступен');
  assert.deepEqual(failed.history, original.history);
  assert.deepEqual(failed.messages, original.messages);
  assert.equal(failed.draft, 'Повторить меня');
  assert.equal(failed.pending, null);
  assert.equal(failed.error, 'Сервер недоступен');
});

test('пятая пара ограничивает контекст, сохраняя видимую беседу', () => {
  const history = [1, 2, 3, 4].flatMap(pair);
  const result = completeSubmission(startSubmission({ ...createChatState(), history, messages: history }, 'Пятый'), reply());
  assert.equal(result.history.length, 8);
  assert.equal(result.history[0].content, 'Вопрос 2');
  assert.equal(result.messages.length, 10);
  assert.match(result.notice, /1/);
});

test('удаление старых пар сервером учитывается в следующем запросе', () => {
  const history = [1, 2, 3].flatMap(pair);
  const result = completeSubmission(startSubmission({ ...createChatState(), history, messages: history }, 'Новый'), reply('Новый ответ', 2));
  assert.equal(result.history.length, 4);
  assert.equal(result.history[0].content, 'Вопрос 3');
  assert.equal(result.messages.length, 8);
  assert.match(result.notice, /2/);
});

test('лимит считает UTF-8 байты: кириллицу и emoji', () => {
  assert.equal(utf8Bytes('Привет'), 12);
  assert.equal(utf8Bytes('🙂'), 4);
  assert.doesNotThrow(() => startSubmission(createChatState(), 'я'.repeat(1500)));
  assert.throws(() => startSubmission(createChatState(), 'я'.repeat(1501)), /3000/);
  assert.throws(() => startSubmission(createChatState(), '🙂'.repeat(751)), /3000/);
  assert.throws(() => startSubmission(createChatState(), ' \n '), /Напишите/);
});

test('pending нельзя повторно отправить или очистить', () => {
  const pending = startSubmission(createChatState(), 'Вопрос');
  assert.throws(() => startSubmission(pending, 'Второй'), /Дождитесь/);
  assert.equal(clearChat(pending), pending);
});

test('новый чат удаляет всё локальное состояние', () => {
  const populated = { ...createChatState(), messages: pair(1), history: pair(1), draft: 'Черновик', error: 'Ошибка', notice: 'Забыто' };
  assert.deepEqual(clearChat(populated), createChatState());
});

test('невалидный ответ не коммитится, включая пустой текст и бесконечные метрики', () => {
  const pending = startSubmission(createChatState(), 'Вопрос');
  for (const data of [null, reply('  '), { ...reply(), dropped_pairs: 1 }, { ...reply(), metrics: { ...reply().metrics, tokens_per_second: Infinity } }]) {
    assert.throws(() => completeSubmission(pending, data), /некорректный/);
  }
  assert.deepEqual(pending.history, []);
});

test('лимит ответа отмечается для пользователя', () => {
  const result = completeSubmission(startSubmission(createChatState(), 'Вопрос'), { ...reply(), truncated: true });
  assert.equal(result.messages[1].truncated, true);
  assert.match(result.notice, /лимита длины/);
});

test('Enter отправляет, Shift+Enter и ввод IME не отправляют', () => {
  assert.equal(shouldSubmitKey({ key: 'Enter' }), true);
  assert.equal(shouldSubmitKey({ key: 'Enter', shiftKey: true }), false);
  assert.equal(shouldSubmitKey({ key: 'Enter', isComposing: true }), false);
  assert.equal(shouldSubmitKey({ key: 'Enter', keyCode: 229 }), false);
  assert.equal(shouldSubmitKey({ key: 'a' }), false);
});
