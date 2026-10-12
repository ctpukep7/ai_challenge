const missing = 'Не предоставлено';
const number = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const count = (value) => Number.isSafeInteger(value) && value >= 0;
const format = (value, digits = 2) => value.toLocaleString('ru-RU', { maximumFractionDigits: digits });
const seconds = (value) => number(value) ? value > 0 && value < .01 ? '< 0,01 с' : `${format(value)} с` : missing;
const tokens = (value) => count(value) ? format(value, 0) : missing;
const speed = (value) => number(value) ? format(value, 1) : missing;
const bytes = (value) => number(value) ? value === 0 ? '0 ГиБ' : value < 10737418 ? '< 0,01 ГиБ' : `${format(value / 1073741824)} ГиБ` : missing;
export function answerStatistics(provider) {
  const own = provider.metrics ?? {}, resources = provider.resources ?? {};
  const failed = provider.status === 'error';
  const placeholder = failed ? 'Нет данных' : provider.called === false ? 'Не выполнялось' : null;
  const model = (value) => failed ? value === missing ? 'Нет данных' : value : placeholder ?? value;
  const total = number(own.total_seconds) ? seconds(own.total_seconds) : placeholder ?? missing;
  const totalTokens = count(own.prompt_tokens) && count(own.output_tokens) ? tokens(own.prompt_tokens + own.output_tokens) : missing;
  const received = number(own.http_seconds) && own.http_seconds === 0 && count(own.output_tokens) ? 'Не вычисляется (HTTP = 0)' : number(own.http_seconds) && count(own.output_tokens) ? speed(own.output_tokens / own.http_seconds) : missing;
  const row = (key, label, value) => ({ key, label, value });
  return [
    row('total', 'Полное время ответа', total), row('http', 'Запрос к модели (HTTP)', model(seconds(own.http_seconds))),
    row('preload', 'Предварительная загрузка (HTTP)', model(seconds(own.preload_seconds))), row('preload_load', 'Загрузка runner при прогреве', model(seconds(own.preload_load_seconds))),
    row('load', 'Загрузка в запросе генерации', model(seconds(own.load_seconds))), row('generation', 'Генерация', model(seconds(own.generation_seconds))),
    row('prompt', 'Входные токены', model(tokens(own.prompt_tokens))), row('output', 'Токены ответа', model(tokens(own.output_tokens))), row('tokens', 'Всего токенов', model(totalTokens)),
    row('received_speed', 'Скорость получения ответа (ток/с)', model(received)), row('generation_speed', 'Скорость генерации (ток/с)', model(speed(own.tokens_per_second))),
    row('size', 'Наблюдаемый размер модели', model(bytes(resources.peak_size_bytes))), row('vram', 'Размещение на GPU', model(bytes(resources.peak_size_vram_bytes))),
    row('context', 'Фактическое окно runner (токены)', model(tokens(resources.context_length))), row('samples', 'Измерения размещения /api/ps', model(tokens(resources.samples))),
  ];
}
