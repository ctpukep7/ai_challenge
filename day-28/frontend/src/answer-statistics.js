const UNAVAILABLE = 'Не предоставлено';
const NO_DATA = 'Нет данных';
const NOT_CALLED = 'Не выполнялось';

const finiteNonnegative = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const tokenCount = (value) => Number.isSafeInteger(value) && value >= 0;
const formatted = (value, digits = 2) => value.toLocaleString('ru-RU', { maximumFractionDigits: digits });
const seconds = (value) => finiteNonnegative(value)
  ? value > 0 && value < .01 ? '< 0,01 с' : `${formatted(value)} с`
  : UNAVAILABLE;
const tokens = (value) => tokenCount(value) ? formatted(value, 0) : UNAVAILABLE;
const speed = (value) => finiteNonnegative(value) ? formatted(value, 1) : UNAVAILABLE;

/** Одна схема статистики; sharedMetrics содержит только общую фазу retrieval. */
export function answerStatistics(provider, { cloud = false, sharedMetrics = null } = {}) {
  const own = provider.metrics ?? {};
  const shared = cloud ? sharedMetrics ?? {} : own;
  const failed = provider.status === 'error';
  const skipped = cloud ? provider.called === false : provider.status === 'unknown'
    && ['weak_context', 'context_limit'].includes(provider.reason) && provider.sources?.length === 0;
  const modelPlaceholder = failed ? NO_DATA : skipped ? NOT_CALLED : null;
  const modelValue = (value) => modelPlaceholder ?? value;
  const measuredTotal = finiteNonnegative(own.total_seconds) && own.total_seconds > 0;
  const total = modelPlaceholder && !measuredTotal ? modelPlaceholder : seconds(own.total_seconds);
  const totalTokens = tokenCount(own.prompt_tokens) && tokenCount(own.output_tokens)
    && Number.isSafeInteger(own.prompt_tokens + own.output_tokens)
    ? tokens(own.prompt_tokens + own.output_tokens) : UNAVAILABLE;
  const receivedSpeed = finiteNonnegative(own.http_seconds) && own.http_seconds === 0
    ? 'Не вычисляется (HTTP = 0)'
    : finiteNonnegative(own.http_seconds) && own.http_seconds > 0 && tokenCount(own.output_tokens)
      ? speed(own.output_tokens / own.http_seconds) : UNAVAILABLE;
  const row = (key, label, value) => ({ key, label, value });

  return [
    row('total', 'Полное время ответа', total),
    row('http', 'Запрос к модели (HTTP)', modelValue(seconds(own.http_seconds))),
    row('retrieval', 'Поиск контекста', seconds(shared.retrieval_seconds)),
    row('embedding_load', 'Загрузка embeddings', seconds(shared.embedding_load_seconds)),
    row('embedding_cold', 'Первый запуск поиска', typeof shared.embedding_cold === 'boolean' ? shared.embedding_cold ? 'Да' : 'Нет' : UNAVAILABLE),
    row('model_load', 'Загрузка модели', modelValue(seconds(own.load_seconds))),
    row('generation', 'Генерация', modelValue(seconds(own.generation_seconds))),
    row('prompt_tokens', 'Входные токены', modelValue(tokens(own.prompt_tokens))),
    row('output_tokens', 'Токены ответа', modelValue(tokens(own.output_tokens))),
    row('total_tokens', 'Всего токенов', modelValue(totalTokens)),
    row('received_speed', 'Скорость получения ответа (ток/с)', modelValue(receivedSpeed)),
    row('generation_speed', 'Скорость генерации (ток/с)', modelValue(speed(own.tokens_per_second))),
    row('chunks', 'Фрагменты контекста', Array.isArray(provider.sources) ? formatted(provider.sources.length, 0) : NO_DATA),
  ];
}
