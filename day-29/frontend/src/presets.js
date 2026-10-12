import { cloneSettings, beginQuestion, finishQuestion } from './optimization-state.js';

function freeze(value) {
  if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); }
  return value;
}
export const PRESETS = freeze([
  {
    "id": "sql-cards-16",
    "question": "Подготовь 16 учебных карточек для новичка по SQL: SELECT, WHERE, ORDER BY, LIMIT, DISTINCT, COUNT, SUM, GROUP BY, HAVING, INNER JOIN, LEFT JOIN, NULL, UNION, EXISTS, CASE, подзапрос. Верни только JSON-объект с массивом cards из 16 элементов в указанном порядке. В каждой карточке четыре поля: number (число от 1 до 16), topic (название темы), explanation (два коротких предложения по-русски), exercise (одно практическое задание по теме). Без Markdown. Не пропускай темы и не объединяй карточки.",
    "settings": {
      "temperature": 0,
      "num_predict": 2048,
      "num_ctx": 4096,
      "top_k": 20,
      "top_p": 0.8,
      "min_p": 0,
      "repeat_penalty": 1,
      "repeat_last_n": 64,
      "seed": -1,
      "stop": [],
      "presence_penalty": 0,
      "format": {
        "type": "object",
        "properties": {
          "cards": {
            "type": "array",
            "minItems": 16,
            "maxItems": 16,
            "items": {
              "type": "object",
              "properties": {
                "number": {
                  "type": "integer"
                },
                "topic": {
                  "type": "string"
                },
                "explanation": {
                  "type": "string"
                },
                "exercise": {
                  "type": "string"
                }
              },
              "required": [
                "number",
                "topic",
                "explanation",
                "exercise"
              ],
              "additionalProperties": false
            }
          }
        },
        "required": [
          "cards"
        ],
        "additionalProperties": false
      },
      "system": "Ты создаёшь данные для учебного приложения. Верни один JSON-объект с cards: ровно 16 карточек в указанном пользователем порядке, с номерами от 1 до 16. На каждую тему отдельная карточка. Explanation — два коротких полных предложения, exercise — одно конкретное задание. Не добавляй Markdown, преамбулу, заключение, многоточие вместо карточек или HTML-сущности. Сначала следи за полнотой всех 16 тем, затем за точностью определения каждой темы. Сохрани исходные названия тем."
    },
    "title": "16 карточек SQL",
    "purpose": "Полный учебный документ вместо обрыва: бюджет ответа увеличен.",
    "changes": "T 0 · ответ 2048 · контекст 4096 · SYSTEM · JSON Schema",
    "criteria": [
      "Чистый JSON: объект cards с 16 карточками.",
      "Все темы в исходном порядке, номера 1–16 и четыре непустых поля.",
      "Правильность SQL и два предложения в explanation — проверка вручную."
    ],
    "expected": {
      "topics": [
        "SELECT",
        "WHERE",
        "ORDER BY",
        "LIMIT",
        "DISTINCT",
        "COUNT",
        "SUM",
        "GROUP BY",
        "HAVING",
        "INNER JOIN",
        "LEFT JOIN",
        "NULL",
        "UNION",
        "EXISTS",
        "CASE",
        "подзапрос"
      ]
    }
  },
  {
    "id": "billing-triage",
    "question": "Обработай обращение клиента для передачи в поддержку.\n«Заказ 7318: деньги списались два раза. Товар пока не доставлен, но срок доставки ещё не вышел. Я не прошу отменять заказ: хочу вернуть только второе списание. Пожалуйста, не оформляйте возврат товара и не меняйте адрес. Второе списание — 2490 рублей. Точную дату списания не помню».",
    "settings": {
      "temperature": 0.1,
      "num_predict": 256,
      "num_ctx": 3072,
      "top_k": 20,
      "top_p": 0.8,
      "min_p": 0,
      "repeat_penalty": 1,
      "repeat_last_n": 64,
      "seed": -1,
      "stop": [],
      "presence_penalty": 0,
      "format": {
        "type": "object",
        "properties": {
          "queue": {
            "type": "string",
            "enum": [
              "billing",
              "delivery",
              "returns",
              "general"
            ]
          },
          "order_id": {
            "type": [
              "string",
              "null"
            ]
          },
          "amount_rub": {
            "type": [
              "integer",
              "null"
            ]
          },
          "date": {
            "type": [
              "string",
              "null"
            ]
          },
          "requested_action": {
            "type": "string"
          },
          "cancel_order": {
            "type": "boolean"
          }
        },
        "required": [
          "queue",
          "order_id",
          "amount_rub",
          "date",
          "requested_action",
          "cancel_order"
        ],
        "additionalProperties": false
      },
      "system": "Ты модуль маршрутизации поддержки, а не консультант. На входе обращение, на выходе только JSON карточки. Выбирай очередь по первопричине: двойное списание → billing; опоздавшая доставка → delivery; отмена или возврат товара → returns; другое → general. Если несколько тем, первопричина финансового запроса приоритетна. Не обещай возврат, срок, письмо или проверку, не выполняй действия. amount_rub — только явно названная сумма спорного списания, order_id строкой, date null если неизвестна. requested_action кратко передаёт то, чего хочет клиент; не путай возврат второго списания с отменой заказа или возвратом товара. Верни поля queue, order_id, amount_rub, date, requested_action, cancel_order; cancel_order true только при явном запросе отмены."
    },
    "title": "Маршрутизация обращения",
    "purpose": "Универсальный ассистент → данные для службы поддержки.",
    "changes": "T 0,1 · ответ 256 · контекст 3072 · SYSTEM · JSON Schema",
    "criteria": [
      "Чистый JSON с шестью полями целевой карточки.",
      "Очередь billing, заказ 7318, сумма 2490, неизвестная дата null, отмена false.",
      "requested_action: вернуть только второе списание — проверка вручную."
    ],
    "expected": {
      "queue": "billing",
      "order_id": "7318",
      "amount_rub": 2490,
      "date": null,
      "cancel_order": false
    }
  }
]);
export function selectPreset(state, editor, id) {
  const preset = PRESETS.find(item => item.id === id);
  if (state.pending || !preset) return null;
  const settings = cloneSettings(preset.settings);
  return { chat: { ...state, draft: preset.question, error: '' }, editor: { draft: settings, initialized: true }, id,
    formatMode: settings.format === null ? 'text' : settings.format === 'json' ? 'json' : 'schema',
    schemaText: settings.format && typeof settings.format === 'object' ? JSON.stringify(settings.format, null, 2) : '' };
}
export function beginPresetQuestion(state, settings, mode, id) {
  const next = beginQuestion(state, settings, mode), preset = PRESETS.find(item => item.id === id);
  return { ...next, pending: { ...next.pending, preset: preset ? freeze({ id: preset.id, title: preset.title, question: preset.question, criteria: [...preset.criteria], expected: preset.expected ? JSON.parse(JSON.stringify(preset.expected)) : null }) : null } };
}
export function finishPresetQuestion(state, result) {
  const snapshot = state.pending?.preset, next = finishQuestion(state, result);
  if (snapshot) next.results[next.results.length - 1].preset = snapshot;
  return next;
}
const row = (label, status, detail) => ({ label, status, detail });
const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const exactKeys = (value, keys) => isObject(value) && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const nonblank = value => typeof value === 'string' && value.trim().length > 0;
export function qualityChecks(provider, question, preset) {
  if (!preset) return [row('Качество', 'manual', 'Проверка вручную: для свободного запроса эталона нет.')];
  if (!provider || provider.status !== 'answered' || provider.called !== true) return [row('Полнота ответа', 'fail', 'Ответ не получен; качество не оценено.')];
  if (provider.truncated) return [row('Полнота ответа', 'fail', 'Достигнут лимит токенов: ответ обрезан, целевой документ не подтверждён.')];
  if (question !== preset.question) return [row('Качество', 'manual', 'Проверка вручную: пример изменён, встроенный эталон не применяется.')];
  if (provider.context_warning) return [row('Качество', 'manual', 'Проверка вручную: входной контекст ограничен.')];
  if (!['sql-cards-16', 'billing-triage'].includes(preset.id) || !preset.expected) return [row('Качество', 'manual', 'Проверка вручную')];
  let parsed;
  try { parsed = JSON.parse(provider.answer); }
  catch { return [row('Чистый JSON', 'fail', 'Ответ целиком не разбирается как JSON. Исправления не применяются.'), row('Целевой контракт', 'fail', 'Не проверен: нет JSON-объекта.')]; }
  const clean = row('Чистый JSON', 'pass', 'Ответ целиком разобран без исправлений.');
  if (preset.id === 'sql-cards-16') {
    const cards = isObject(parsed) && Array.isArray(parsed.cards) ? parsed.cards : [];
    const count = cards.length === preset.expected.topics.length;
    const ordered = count && cards.every((card, index) => isObject(card) && card.topic === preset.expected.topics[index]);
    const numbered = count && cards.every((card, index) => isObject(card) && Number.isInteger(card.number) && card.number === index + 1);
    const fields = count && cards.every(card => exactKeys(card, ['number', 'topic', 'explanation', 'exercise']) && Number.isInteger(card.number) && ['topic', 'explanation', 'exercise'].every(key => nonblank(card[key])));
    return [clean, row('Объект только с полем cards', exactKeys(parsed, ['cards']) ? 'pass' : 'fail', 'Без дополнительных полей верхнего уровня.'),
      row('Полнота: 16 карточек', count ? 'pass' : 'fail', `Получено ${cards.length} из ${preset.expected.topics.length}.`),
      row('Темы и порядок', ordered ? 'pass' : 'fail', 'Все 16 исходных тем без замен, пропусков и повторов.'),
      row('Номера 1–16', numbered ? 'pass' : 'fail', 'Последовательные целые числа.'),
      row('Четыре поля каждой карточки', fields ? 'pass' : 'fail', 'number — целое; topic, explanation, exercise — непустые строки.'),
      row('Правильность SQL и задания', 'manual', 'Проверка вручную: корректность определений и практический смысл задания.'),
      row('Два коротких предложения', 'manual', 'Проверка вручную: explanation каждой карточки.')];
  }
  const keys = [...Object.keys(preset.expected), 'requested_action'];
  return [clean, row('Ровно шесть полей', exactKeys(parsed, keys) ? 'pass' : 'fail', 'queue, order_id, amount_rub, date, requested_action, cancel_order.'),
    ...Object.entries(preset.expected).map(([key, value]) => row(key, isObject(parsed) && parsed[key] === value ? 'pass' : 'fail', `Ожидается ${JSON.stringify(value)}`)),
    row('requested_action: непустая строка', isObject(parsed) && nonblank(parsed.requested_action) ? 'pass' : 'fail', 'Действие сформулировано, а не пропущено.'),
    row('Смысл requested_action', 'manual', 'Проверка вручную: вернуть только второе списание, без отмены заказа и возврата товара.')];
}
const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
export function metricDeltas(baseline, optimized) {
  const specs = [['total', 'Полное время', 'metrics', 'total_seconds', 'с', 1], ['speed', 'Скорость генерации', 'metrics', 'tokens_per_second', 'ток/с', 1], ['size', 'Размещение модели', 'resources', 'peak_size_bytes', 'ГиБ', 1073741824]];
  const number = value => value.toLocaleString('ru-RU', { maximumFractionDigits: 2 });
  return specs.map(([key, label, group, field, unit, scale]) => {
    const before = baseline?.[group]?.[field], after = optimized?.[group]?.[field];
    if (baseline?.status !== 'answered' || optimized?.status !== 'answered' || baseline?.called !== true || optimized?.called !== true || !finite(before) || !finite(after)) return { key, label, value: 'Нет данных' };
    const change = after - before, sign = change > 0 ? '+' : '';
    return { key, label, value: `${number(before / scale)} → ${number(after / scale)} ${unit} · Δ ${sign}${number(change / scale)} ${unit}${before > 0 ? ` (${change > 0 ? '+' : ''}${number(change / before * 100)}%)` : ' · процент не вычисляется (исходное = 0)'}` };
  });
}
