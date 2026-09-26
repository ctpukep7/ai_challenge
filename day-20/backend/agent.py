import asyncio
import json
import time
import httpx
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo
from openai import AsyncOpenAI
from backend.store import uid
from backend.json_requests import capture_scope

class Model:
    def __init__(self, settings, observer=None, http_client=None):
        self.settings=settings;self.observer=observer
        self._span=ContextVar('day20_model_http_span',default=None)
        http_client=http_client or httpx.AsyncClient(timeout=40)
        http_client.event_hooks.setdefault('request',[]).append(self._observe_request)
        http_client.event_hooks.setdefault('response',[]).append(self._observe_response)
        self.client=AsyncOpenAI(api_key=settings.api_key or 'not-configured',base_url=settings.base_url,timeout=40,max_retries=0,http_client=http_client)

    async def _observe_request(self,request):
        if not self.observer:return
        try:
            body=json.loads((await request.aread()).decode())
            id=self.observer.begin('model',dict(method=request.method,path=request.url.path,body=body))
            request.extensions['day20_json_request_id']=id
            span=self._span.get()
            if span is not None:span['id']=id
        except Exception:pass

    async def _observe_response(self,response):
        if not self.observer:return
        try:
            data=json.loads((await response.aread()).decode())
            normalized={k:data[k] for k in ('id','model','created','object','usage') if k in data}
            normalized['choices']=[dict(index=choice.get('index'),finish_reason=choice.get('finish_reason'),message={k:choice.get('message',{}).get(k) for k in ('role','content','tool_calls') if k in choice.get('message',{})}) for choice in data.get('choices',[])]
            if 'error' in data:normalized['error']=data['error']
            self.observer.finish(response.request.extensions.get('day20_json_request_id'),'completed' if response.is_success else 'error',response=normalized,error=None if response.is_success else f'HTTP {response.status_code}')
        except Exception:pass

    async def complete(self,messages,tools):
        if not self.settings.api_key:raise ValueError('Настройте DEEPSEEK_API_KEY в отдельном .env Day 20')
        span={'id':None};token=self._span.set(span)
        try:
            response=await self.client.chat.completions.create(model=self.settings.model,messages=messages,tools=tools,temperature=0.2,extra_body={'thinking':{'type':'disabled'}})
            message=response.choices[0].message
            return dict(content=message.content,tool_calls=[call.model_dump(exclude_none=True) for call in message.tool_calls or []])
        except BaseException as error:
            if self.observer:self.observer.finish(span['id'],'interrupted' if isinstance(error,asyncio.CancelledError) else 'error',error='HTTP обмен прерван' if isinstance(error,asyncio.CancelledError) else 'HTTP обмен завершился ошибкой: '+type(error).__name__)
            raise
        finally:self._span.reset(token)
    async def close(self):await self.client.close()

SYSTEM='''Ты помощник по планированию поездок. Отвечай по-русски. Сам выбирай обнаруженные MCP инструменты; не выдумывай их результаты и идентификаторы. Для обычного вопроса о погоде/календаре используй только нужный сервер.
Время передавай в ISO8601 с часовым поясом. Относительные даты вычисляй по текущему времени ниже. resolve_city предшествует get_hourly_forecast; list_calendars предшествует find_free_slots; настоящий прогноз и доступность предшествуют save_trip_draft; затем ОБЯЗАТЕЛЬНО get_trip_plan. Независимые чтения календаря и прогноза разрешено менять местами.
При неоднозначном городе уточни регион/страну. При нескольких календарях перечисли полные имена и ID и попроси ответить обычным сообщением в чате полным именем или ID, например «Используй календарь Личный»; не выбирай первый сам. После ответа пользователя заново вызови list_calendars: selected_calendar_id — проверенный сервером выбор, requires_selection=true означает, что нужно уточнение. Не предлагай выбор в интерфейсе или отдельные карточки для уточнений. Если не хватает длительности, дневного окна или важных ограничений погоды — задай вопрос. Не ослабляй указанные пользователем ограничения. Сохранить проект можно только с покрытием каждого часа прогноза и свободным интервалом указанной длительности. Условия save_trip_draft должны точно совпадать с запросом пользователя. Не создавай вымышленный план при отсутствии подходящего окна.
run_id передаётся средой, никогда не задавай его. Если пользователь отвечает на твой вопрос коротким именем города, календаря или условием, продолжи исходную задачу из истории с сохранением её остальных условий. Для нового запуска заново получи все необходимые run-scoped источники; не используй старые IDs. Выбор календаря проверяет сервер по текущему сообщению и свежему каталогу; не подставляй свой выбор вместо пользователя. calendar_id ниже задан только для совместимости с прежним API.
Последний запрос пользователя имеет приоритет. Если он задаёт отдельный вопрос только о погоде или только о календаре, не продолжай старую поездку и не обращайся к другим источникам без необходимости.
Создание события возможно только кнопкой подтверждения точной карточки проекта. Согласие текстом в чате не является подтверждением. Ты не можешь создать событие. Различай сохранённый проект и запись в календарь. Не заявляй об успехе сохранения без get_trip_plan. Выводы поясняй кратко по фактическим данным, не показывай внутренние рассуждения.'''

class Agent:
    def __init__(self,store,registry,model,settings):self.store=store;self.registry=registry;self.model=model;self.settings=settings
    async def run(self,run_id):
        run=self.store.get('run',run_id);self.store.update_run(run_id,status='running')
        # Keep prior user-facing chat to handle a clarification, never replay earlier tool IDs/evidence.
        completed=[r['id'] for r in self.store.all('run') if r['id']!=run_id and r['status']=='completed'][-6:]
        persisted_messages=self.store.all('message');history=[]
        for previous_id in completed:
            pair=[m for m in persisted_messages if m['run_id']==previous_id]
            if {m['role'] for m in pair}>={'user','assistant'}:
                history.extend(dict(role=m['role'],content=m['content']) for m in pair)
        context=f"\nСейчас: {datetime.now(ZoneInfo(self.settings.timezone)).isoformat()}. Часовой пояс: {self.settings.timezone}. Явно выбранный calendar_id: {run['calendar_id'] or 'не выбран'}."
        messages=[dict(role='system',content=SYSTEM+context),*history,dict(role='user',content=run['prompt'])]
        call_count=0;failures=0;sequence=0;read_plans=set()
        try:
            async with asyncio.timeout(self.settings.run_timeout):
                for round_index in range(12):
                    with capture_scope(run_id):
                        response=await self.model.complete(messages,self.registry.tools())
                    calls=response.get('tool_calls') or []
                    if not calls:
                        plan=self.store.plan_for_run(run_id)
                        if plan and plan['id'] not in read_plans:raise ValueError('Модель не прочитала сохранённый проект; флоу не завершён')
                        answer=response.get('content') or 'Не удалось получить ответ модели'
                        if failures:
                            error=self.store.get('run',run_id)['trace'][-1]['error']
                            self.store.update_run(run_id,status='incomplete',answer='Флоу не завершён: '+error,error=error,plan=plan)
                            return
                        # Authoritative UI card derives solely from persisted data.
                        if plan:
                            notes={'draft':'Проект сохранён. Событие появится в календаре только после подтверждения карточки.',
                                   'approved':'Проект подтверждён. Календарная операция ещё выполняется.',
                                   'created':'Проект сохранён. Событие создано в календаре.',
                                   'rejected':'Проект отклонён. Событие по нему не создавалось.',
                                   'conflict':'Проект сохранён, но календарь изменился: запись не выполнена. Нужен новый проект.',
                                   'uncertain':'Проект сохранён. Исход календарной записи неизвестен; требуется сверка UID.'}
                            answer+='\n\n'+notes.get(plan['status'],'Текущее состояние проекта указано в карточке.')
                        self.store.update_run(run_id,status='completed',answer=answer,plan=plan)
                        self.store.put('message',dict(id=uid(),role='assistant',content=answer,run_id=run_id));return
                    if len(calls)>3 or call_count+len(calls)>20:raise ValueError('Исчерпан лимит вызовов инструментов')
                    messages.append(dict(role='assistant',content=response.get('content'),tool_calls=calls))
                    for call in calls:
                        call_count+=1;function=call['function'];alias=function['name'];started=time.monotonic()
                        trace=dict(sequence=0,server_id=alias.split('__')[0] if '__' in alias else 'unknown',alias=alias,tool=alias.split('__')[-1],status='running',duration_ms=0,arguments={},result_summary='',error=None)
                        trace=self.store.allocate_trace(run_id,trace)
                        try:
                            args=json.loads(function['arguments'])
                            if not isinstance(args,dict):raise ValueError('Аргументы должны быть JSON объектом')
                            # Whitelist public protocol arguments; unknown user-supplied keys never enter traces.
                            allowed={'name','city_id','city_index','start','end','calendars_id','calendar_id','duration_hours','day_start','day_end','forecast_id','availability_id','plan_id','title','reason','min_temperature_c','max_precipitation_probability','max_wind_ms'}
                            trace['arguments']={k:v for k,v in args.items() if k in allowed}
                            self.store.trace(run_id,trace)
                            with capture_scope(run_id,trace['sequence'],call['id']):
                                result=await self.registry.call(alias,args,run_id=run_id)
                            if alias=='plans__get_trip_plan':read_plans.add(result['id'])
                            failures=0;trace.update(status='completed',result_summary=self.summary(alias,result))
                            tool_content=json.dumps(result,ensure_ascii=False)
                        except (ValueError,TimeoutError) as error:
                            failures+=1;trace.update(status='error',error=str(error),result_summary='Вызов не выполнен')
                            tool_content=json.dumps(dict(error=str(error)),ensure_ascii=False)
                        finally:
                            if trace['status']=='running':trace.update(status='error',error='Вызов прерван или истёк срок',result_summary='Нет подтверждённого результата')
                            trace['duration_ms']=round((time.monotonic()-started)*1000);self.store.trace(run_id,trace)
                        messages.append(dict(role='tool',tool_call_id=call['id'],content=tool_content))
                        if failures>=3:raise ValueError('Две попытки исправления аргументов не помогли')
                raise ValueError('Исчерпан лимит 12 обращений к модели')
        except asyncio.CancelledError:
            self.store.update_run(run_id,status='interrupted',error='Запуск остановлен',plan=self.store.plan_for_run(run_id));raise
        except TimeoutError:
            self.store.update_run(run_id,status='incomplete',error='Истёк общий срок 180 секунд',plan=self.store.plan_for_run(run_id))
        except ValueError as error:
            self.store.update_run(run_id,status='incomplete',error=str(error),plan=self.store.plan_for_run(run_id))
        except Exception:
            self.store.update_run(run_id,status='error',error='Модель или внешний источник недоступны; частичный результат сохранён',plan=self.store.plan_for_run(run_id))

    @staticmethod
    def summary(alias,result):
        if alias.endswith('resolve_city'):return f"Найдено городов: {len(result.get('candidates',[]))}"
        if alias.endswith('get_hourly_forecast'):return f"Получено часов прогноза: {len(result.get('hours',[]))}"
        if alias.endswith('list_calendars'):return f"Доступно календарей: {len(result.get('calendars',[]))}"
        if alias.endswith('find_free_slots'):return f"Свободных интервалов: {len(result.get('slots',[]))}"
        if alias.endswith('save_trip_draft'):return 'Проект сохранён в SQLite'
        if alias.endswith('get_trip_plan'):return 'Сохранённый проект прочитан'
        return 'Вызов завершён'
