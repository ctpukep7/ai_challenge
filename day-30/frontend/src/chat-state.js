export function initialSession() { return {token:'',limits:null,history:[]}; }
export function clearSession() { return initialSession(); }
export function requestHistory(history, maximum=4) {
  return history.slice(-maximum).map(({user,assistant})=>({user,assistant}));
}
export function appendAnswer(history, prompt, response) {
  return [...history,{user:prompt,assistant:response.answer,response}];
}
export function errorMessage(status, body, retryAfter) {
  if(status===401) return 'Доступ не подтверждён. Войдите снова с токеном сервиса.';
  if(status===429) {
    const seconds=Number(retryAfter);
    return Number.isFinite(seconds)&&seconds>0 ? `Слишком много запросов. Повторите через ${Math.ceil(seconds)} с.` : 'Слишком много запросов. Подождите минуту перед следующим запросом.';
  }
  if(status===409) return 'Модель занята другим запросом. Попробуйте позже.';
  if(status===413) return 'Контекст слишком большой. Сократите вопрос или начните новый чат.';
  return body?.error?.message || 'Сервис не смог обработать запрос.';
}
export function failureMessage(cause) {
  if(cause?.name==='AbortError') return 'Время ожидания истекло. Запрос автоматически не повторяется.';
  if(cause instanceof TypeError) return 'Не удалось подключиться к сервису. Проверьте сеть, запуск сервера и доверие HTTPS-сертификату.';
  return cause?.message || 'Не удалось подключиться к сервису.';
}
export function serviceLimits(limits) {
  return [
    ['История в запросе',`${limits.max_history_pairs} пары`],
    ['Вход с инструкцией',`${limits.max_context_bytes} байт UTF-8`],
    ['Контекст модели',`${limits.context_window} токенов`],
    ['Максимум ответа',`${limits.max_output_tokens} токенов`],
    ['Частота запросов',`${limits.rate_limit_per_minute} в минуту`],
    ['Одновременно',`${limits.concurrency} запрос`],
  ];
}
function number(value, unit, decimals=2) {
  if(typeof value!=='number'||!Number.isFinite(value)||value<0) return 'Нет данных';
  if(decimals&&value>0&&value<0.01) return `< 0,01 ${unit}`;
  return `${value.toLocaleString('ru-RU',{maximumFractionDigits:decimals})} ${unit}`.trim();
}
export function statistics(metrics={}) {
  return [
    ['Полное время',number(metrics.total_seconds,'с')],
    ['Загрузка модели',number(metrics.load_seconds,'с')],
    ['Генерация',number(metrics.generation_seconds,'с')],
    ['Токены запроса',number(metrics.prompt_tokens,'',0)],
    ['Токены ответа',number(metrics.output_tokens,'',0)],
    ['Токены всего',number(metrics.total_tokens,'',0)],
    ['Скорость генерации',number(metrics.tokens_per_second,'ток/с')],
  ];
}
export async function apiRequest(path, token, payload, {fetcher=fetch,signal}={}) {
  const response=await fetcher(path, {method:payload===undefined?'GET':'POST',headers:{Authorization:`Bearer ${token}`,...(payload===undefined?{}:{'Content-Type':'application/json'})},...(payload===undefined?{}:{body:JSON.stringify(payload)}),cache:'no-store',credentials:'omit',redirect:'error',signal});
  let body;
  try {body=await response.json();} catch { throw Object.assign(new Error('Сервис вернул некорректный JSON.'),{status:response.status}); }
  if(!response.ok) throw Object.assign(new Error(errorMessage(response.status,body,response.headers.get('Retry-After'))),{status:response.status});
  return body;
}
