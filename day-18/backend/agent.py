"""One bounded tool loop is shared by chat and cron execution."""
import asyncio
import json
from contextlib import asynccontextmanager
import httpx
from jsonschema import validate, ValidationError
from openai import AsyncOpenAI, APIConnectionError, APIStatusError, APITimeoutError
from .config import settings, ROOT
from .mcp_client import connect, call
from .diagnostics import DiagnosticLog, completion_body

INTERACTIVE = {"load_skill","resolve_city","create_weather_schedule","list_schedules","set_schedule_status","delete_weather_schedule"}
SCHEDULED = {"get_current_weather","save_weather_result"}
MUTATIONS = {"create_weather_schedule","set_schedule_status","delete_weather_schedule"}


class AgentError(ValueError):
    """Our own fixed, safe validation messages; never provider exception text."""


def exception_leaves(exc):
    # MCP stdio uses TaskGroups and may wrap an otherwise useful provider error.
    if isinstance(exc,BaseExceptionGroup):
        for nested in exc.exceptions:
            yield from exception_leaves(nested)
    else:
        yield exc


def public_error(exc):
    errors = list(exception_leaves(exc))
    for error in errors:
        if isinstance(error,(asyncio.CancelledError,KeyboardInterrupt,SystemExit)):
            raise error
    for error in errors:
        if isinstance(error,APITimeoutError):
            return "DeepSeek не ответил вовремя. Попробуйте отправить сообщение ещё раз."
        if isinstance(error,APIConnectionError):
            return "Не удалось соединиться с DeepSeek. Проверьте подключение к интернету и повторите сообщение."
        if isinstance(error,APIStatusError):
            return {
                401:"DeepSeek отклонил ключ доступа. Проверьте настройки подключения.",
                402:"Недостаточно средств на балансе DeepSeek. Пополните баланс и повторите сообщение.",
                429:"DeepSeek временно ограничил число запросов. Попробуйте немного позже.",
                500:"Сервис DeepSeek временно недоступен. Попробуйте немного позже.",
                503:"Сервис DeepSeek временно недоступен. Попробуйте немного позже.",
            }.get(error.status_code,"DeepSeek не смог обработать запрос. Попробуйте ещё раз.")
    for error in errors:
        if isinstance(error,TimeoutError):
            return "Превышен лимит выполнения 90 секунд. Попробуйте отправить сообщение ещё раз."
        if isinstance(error,AgentError):
            return str(error)
    return "Не удалось завершить запрос агента. Попробуйте ещё раз; если ошибка повторяется, проверьте подключение к MCP."


class DeepSeek:
    def __init__(self,config):
        self.config = config

    @asynccontextmanager
    async def session(self):
        if not self.config.api_key:
            raise AgentError("DeepSeek не подключён. Добавьте ключ доступа в настройках приложения.")
        # A client belongs to one run/event loop. It is reused between model steps,
        # while the runtime's 90s deadline bounds requests, retries and tool calls.
        async with AsyncOpenAI(api_key=self.config.api_key,base_url="https://api.deepseek.com",
                               timeout=httpx.Timeout(45,connect=20),max_retries=1) as client:
            yield DeepSeekSession(client,self.config)


class DeepSeekSession:
    def __init__(self,client,config):
        self.client = client
        self.config = config

    async def complete(self,messages,tools):
        body = completion_body(self.config,messages,tools)
        thinking = body.pop("thinking")
        response = await self.client.chat.completions.create(**body,extra_body={"thinking":thinking})
        return response.choices[0].message.model_dump(exclude_none=True)


class AgentRuntime:
    def __init__(self,store,model=None,config=None):
        self.store = store
        self.config = config or settings()
        self.model = model or DeepSeek(self.config)

    async def run(self,prompt,request_id=None,scope=None,history=None,chat_token=None):
        trace = []
        diagnostics = DiagnosticLog([self.config.api_key,str(ROOT),self.store.path,chat_token,
                                     scope.get("token") if scope else None])
        try:
            async with asyncio.timeout(90):
                if isinstance(self.model,DeepSeek):
                    async with self.model.session() as model:
                        return await self._run(prompt,request_id,scope,history,trace,chat_token,model,diagnostics)
                return await self._run(prompt,request_id,scope,history,trace,chat_token,self.model,diagnostics)
        except BaseException as exc:
            try:
                message = public_error(exc)
            except asyncio.CancelledError as cancelled:
                cancelled.json_requests = diagnostics.records
                cancelled.trace = trace
                raise
            answer = message
            if scope is None and request_id:
                actions = self.store.chat_actions(request_id)
                if actions:
                    # The transaction ledger is authoritative even if the tool reply
                    # or the final model response failed after a committed mutation.
                    known = {(step["tool"],step.get("result",{}).get("id")):step for step in trace if step.get("ok")}
                    for action in actions:
                        key = (action["tool"],action["result"]["id"])
                        if key in known:
                            known[key]["result"] = action["result"]
                        else:
                            trace.append(action)
                    statuses = {"active":"сбор включён","paused":"сбор приостановлен","cancelled":"расписание удалено"}
                    saved = "; ".join(f"{action['result']['label']} — {statuses[action['result']['status']]}" for action in actions)
                    reason = message.split(". ",1)[0].rstrip(".")
                    answer = f"Изменения сохранены: {saved}. Не удалось получить завершающий ответ агента. {reason}. Состояние расписаний обновлено в интерфейсе."
            return {"answer":answer,"trace":trace,"error":message,"json_requests":diagnostics.records}

    async def _run(self,prompt,request_id,scope,history,trace,chat_token,model,diagnostics):
        scheduled = scope is not None
        allowed = SCHEDULED if scheduled else INTERACTIVE
        loaded = scheduled
        if scheduled:
            job = scope["job"]
            instruction = ("Режим выполнения по расписанию. Доступны только получение погоды и сохранение результата. "
                           "Не создавай summary и не меняй задания. Успех требует сохранения через MCP. "
                           f"Сохранённый skill (hash={job['skill_hash']}):\n{job['skill_body']}\n"
                           f"Закреплённое задание: {json.dumps({key:job[key] for key in ('label','instruction','timezone','schedule_type','interval_minutes','cron_expression')},ensure_ascii=False)}")
        else:
            instruction = ("Ты агент для управления расписаниями сбора погоды. Отвечай по-русски. Выполняй только периодический сбор погоды. "
                           "Отвечай простым языком: город, периодичность и подтверждённый результат. "
                           "Не включай в ответ технические UUID, координаты, UTC timestamps, пути и инструкции об открытии папок. "
                           "Время следующего сбора показывается в карточке расписания: предложи посмотреть его там. "
                           "Не рассчитывай преобразование времени между часовыми поясами самостоятельно, если инструмент не вернул готовое местное время. "
                           "За одно сообщение создавай не более одного расписания. Навык в каталоге: periodic-weather-collection — настройка расписания сбора погоды. "
                           "Обязательно вызови load_skill до любых изменений. На неоднозначный город или неизвестный период спроси уточнение. "
                           "Управляй уже сохранёнными расписаниями: останови, прекрати, не собирай, отключи, пауза означают set_schedule_status paused; "
                           "продолжи, возобнови, включи сбор означают set_schedule_status active; удали крон задачу, удали расписание, отмени окончательно означают delete_weather_schedule. "
                           "Перед каждым изменением статуса или удалением обязательно вызови актуальный list_schedules и выбери job_id только из его результата. "
                           "При одном текущем расписании команда без города относится к нему. При нескольких подходящих заданиях спроси уточнение без изменений; "
                           "если пользователь явно просит все расписания, выполни действие для каждого конкретного задания, обновляя список перед каждым действием. "
                           "Удаление архивирует расписание и сохраняет данные; удалённое не возобновляется. Общий системный cron не меняй. "
                           "Инструменты доступны через MCP. Используй city_id только из resolve_city, никаких shell-команд. "
                           "Не выполняй summary, напоминания и установку системного cron. Наличие API не означает, что cron установлен. "
                           f"Часовой пояс по умолчанию {self.config.timezone}. Сегодня UTC {__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}. "
                           "Вызывай инструменты строго с аргументами их схемы. Не утверждай об успехе без успешного результата инструмента.")
        messages = [{"role":"system","content":instruction}]
        messages.extend(history or [])
        messages.append({"role":"user","content":prompt})
        async with connect(self.store.path,scope,request_id,chat_token) as session:
            discovered = await session.list_tools()
            tools = [{"type":"function","function":{"name":tool.name,"description":tool.description,"parameters":tool.inputSchema}} for tool in discovered.tools if tool.name in allowed]
            schemas = {tool["function"]["name"]:tool["function"]["parameters"] for tool in tools}
            invalid = 0
            for _ in range(8):
                model_record = diagnostics.begin("deepseek","DeepSeek — запрос модели",completion_body(self.config,messages,tools))
                try:
                    answer = await model.complete(messages,tools)
                except BaseException as exc:
                    try:
                        message = public_error(exc)
                    except asyncio.CancelledError:
                        message = "Запрос прерван"
                    diagnostics.failure(model_record,message)
                    raise
                tool_calls = answer.get("tool_calls",[])
                # Preserve assistant tool call ids and arguments verbatim for the provider.
                assistant = {key:value for key,value in answer.items() if key in ("role","content","tool_calls")}
                assistant.setdefault("role","assistant")
                diagnostics.success(model_record,assistant)
                messages.append(assistant)
                if not tool_calls:
                    return {"answer":answer.get("content") or "Агент завершил запрос","trace":trace,"json_requests":diagnostics.records}
                if len(tool_calls) > 5:
                    raise AgentError("Агент запросил слишком много инструментов за один шаг")
                for item in tool_calls:
                    function = item.get("function",{})
                    name = function.get("name","")
                    tool_record = None
                    try:
                        args = json.loads(function.get("arguments","{}"))
                        if name not in allowed or name not in schemas or not isinstance(args,dict):
                            raise ValueError("Инструмент или его аргументы не разрешены в текущем режиме")
                        schema = schemas[name]
                        try:
                            validate(args,schema)
                        except ValidationError:
                            raise ValueError("Аргументы инструмента не соответствуют схеме") from None
                        if set(args) - set(schema.get("properties",{})) or set(schema.get("required",[])) - set(args):
                            raise ValueError("Аргументы инструмента не соответствуют схеме")
                        if name in MUTATIONS and not loaded:
                            raise ValueError("Сначала вызовите load_skill")
                        tool_record = diagnostics.begin("mcp",f"MCP — {name}",{"method":"tools/call","params":{"name":name,"arguments":args}})
                        try:
                            result = await call(session,name,args)
                        except BaseException as exc:
                            try:
                                message = public_error(exc)
                            except asyncio.CancelledError:
                                message = "Запрос прерван"
                            diagnostics.failure(tool_record,message)
                            raise
                        diagnostics.success(tool_record,result)
                        if name == "load_skill":
                            loaded = True
                        trace.append({"tool":name,"ok":True,"result":result})
                    except (ValueError,TypeError,json.JSONDecodeError) as exc:
                        invalid += 1
                        result = {"error":str(exc)[:300]}
                        trace.append({"tool":name,"ok":False,"result":result})
                        if invalid > 1:
                            raise AgentError("Агент дважды получил ошибку инструмента; запрос остановлен") from None
                    messages.append({"role":"tool","tool_call_id":item["id"],"content":json.dumps(result,ensure_ascii=False)})
            raise AgentError("Достигнут лимит восьми шагов агента")
