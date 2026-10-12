export const MAX_PROMPT_BYTES = 3000;
export const MAX_HISTORY_MESSAGES = 8;

export function utf8Bytes(text) {
  return new TextEncoder().encode(text).length;
}

export function createChatState() {
  return { messages: [], history: [], draft: '', pending: null, error: '', notice: '' };
}

export function startSubmission(state, prompt) {
  if (state.pending) throw new Error('Дождитесь ответа на предыдущий вопрос.');
  const trimmed = prompt.trim();
  if (!trimmed) throw new Error('Напишите вопрос.');
  if (utf8Bytes(trimmed) > MAX_PROMPT_BYTES) {
    throw new Error('Вопрос слишком длинный: максимум 3000 байт UTF-8. Сократите текст.');
  }
  return { ...state, draft: '', pending: trimmed, error: '', notice: '' };
}

export function chatPayload(state) {
  if (!state.pending) throw new Error('Нет вопроса для отправки.');
  return { prompt: state.pending, history: state.history.map(({ role, content }) => ({ role, content })) };
}

export function validReply(reply, history) {
  const names = ['total_seconds', 'load_seconds', 'generation_seconds', 'prompt_tokens', 'output_tokens', 'tokens_per_second'];
  return Boolean(reply && typeof reply.answer === 'string' && reply.answer.trim()
    && typeof reply.model === 'string' && reply.model
    && typeof reply.truncated === 'boolean'
    && Number.isInteger(reply.dropped_pairs) && reply.dropped_pairs >= 0
    && reply.dropped_pairs <= history.length / 2
    && names.every((name) => typeof reply.metrics?.[name] === 'number'
      && Number.isFinite(reply.metrics[name]) && reply.metrics[name] >= 0));
}

export function completeSubmission(state, reply) {
  if (!state.pending || !validReply(reply, state.history)) {
    throw new Error('Сервис вернул некорректный ответ. Попробуйте отправить вопрос ещё раз.');
  }
  const pair = [{ role: 'user', content: state.pending }, { role: 'assistant', content: reply.answer }];
  const retained = state.history.slice(reply.dropped_pairs * 2);
  const nextHistory = [...retained, ...pair];
  const overflowPairs = Math.max(0, (nextHistory.length - MAX_HISTORY_MESSAGES) / 2);
  const dropped = reply.dropped_pairs + overflowPairs;
  const notices = [];
  if (dropped) notices.push(`Для следующего ответа забыто старых пар сообщений: ${dropped}. Они остаются на экране.`);
  if (reply.truncated) notices.push('Ответ достиг лимита длины и мог закончиться раньше времени.');
  return {
    ...state,
    messages: [...state.messages, pair[0], { ...pair[1], metrics: { ...reply.metrics }, truncated: reply.truncated }],
    history: nextHistory.slice(-MAX_HISTORY_MESSAGES),
    pending: null,
    error: '',
    notice: notices.join(' '),
  };
}

export function failSubmission(state, message) {
  return { ...state, draft: state.pending ?? state.draft, pending: null, error: message, notice: '' };
}

export function clearChat(state) {
  if (state.pending) return state;
  return createChatState();
}

export function shouldSubmitKey(event) {
  return event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229;
}
