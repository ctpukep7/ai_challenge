import asyncio
import anyio
import copy
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from backend.config import ROOT
from backend.json_requests import capture_metadata

@dataclass
class Entry:
    server_id: str
    name: str
    schema: dict
    description: str
    session: object
    @property
    def alias(self):return self.server_id+'__'+self.name

class Registry:
    """The owner task opens, dispatches and closes every SDK context itself."""
    def __init__(self, settings, observer=None):
        self.settings=settings;self.observer=observer;self.entries={};self.queue=asyncio.Queue();self.ready=None;self.task=None;self.catalog=[];self.connected=False;self.stopping=False
    async def start(self):
        self.stopping=False;self.ready=asyncio.get_running_loop().create_future();self.task=asyncio.create_task(self._owner());await self.ready
    async def close(self):
        if not self.task:return
        self.stopping=True;await self.queue.put(None)
        await self.task
        self.task=None;self.connected=False
    def add(self, server_id, tool, session):
        schema=copy.deepcopy(tool.inputSchema);schema.setdefault('additionalProperties',False)
        entry=Entry(server_id,tool.name,schema,tool.description or '',session)
        if entry.alias in self.entries:raise ValueError('Дублирующее имя MCP инструмента')
        self.entries[entry.alias]=entry
    async def discover(self, server_id, session):
        await asyncio.wait_for(session.initialize(),self.settings.tool_timeout);cursor=None;seen=set()
        while True:
            if cursor in seen:raise ValueError('MCP tools/list повторил курсор страницы')
            seen.add(cursor)
            result=await asyncio.wait_for(session.list_tools(cursor=cursor),self.settings.tool_timeout)
            for tool in result.tools:self.add(server_id,tool,session)
            cursor=result.nextCursor
            if not cursor:break
    def tools(self):
        result=[]
        for alias,entry in self.entries.items():
            if entry.name=='create_trip_event':continue
            schema=copy.deepcopy(entry.schema);schema.get('properties',{}).pop('run_id',None);schema['required']=[n for n in schema.get('required',[]) if n!='run_id']
            result.append(dict(type='function',function=dict(name=alias,description=entry.description,parameters=schema)))
        return result
    def registry(self):
        return [dict(id=id,name=name,status='connected',tools=[dict(alias=e.alias,name=e.name,confirmation_only=e.name=='create_trip_event') for e in self.entries.values() if e.server_id==id]) for id,name in [('weather','Погода'),('calendar','Календарь'),('plans','Планы')]]
    async def call(self, alias, arguments, run_id=None, approval=False):
        if alias not in self.entries:raise ValueError('Неизвестный инструмент')
        entry=self.entries[alias];args=dict(arguments)
        if entry.name=='create_trip_event' and not approval:raise ValueError('Календарная запись доступна только после подтверждения карточки')
        if 'run_id' in entry.schema.get('properties',{}):
            if not run_id:raise ValueError('Инструмент требует текущий запуск')
            if 'run_id' in args:raise ValueError('Модель не может задавать run_id')
            args['run_id']=run_id
        errors=list(Draft202012Validator(entry.schema).iter_errors(args))
        if errors:raise ValueError('Аргументы не соответствуют JSON Schema: '+','.join(str(e.validator) for e in errors))
        if not self.connected:raise RuntimeError('MCP соединения закрыты')
        observation={'id':None}
        future=asyncio.get_running_loop().create_future();await self.queue.put((entry,args,future,capture_metadata(),observation))
        try:return await asyncio.wait_for(asyncio.shield(future),timeout=self.settings.tool_timeout)
        except BaseException as error:
            if self.observer:self.observer.finish(observation['id'],'interrupted' if isinstance(error,asyncio.CancelledError) else 'error',error='MCP обмен прерван' if isinstance(error,asyncio.CancelledError) else 'Истёк срок MCP обмена')
            future.cancel()
            raise
    async def _owner(self):
        try:
            async with AsyncExitStack() as stack:
                for server_id in ('weather','calendar','plans'):
                    env={name:os.environ[name] for name in ('PATH','SYSTEMROOT','WINDIR','TMPDIR','TEMP','TMP','LANG') if name in os.environ}
                    env.update(DAY20_DB_PATH=self.settings.db_path,TIMEZONE=self.settings.timezone,PYTHONPATH=str(ROOT))
                    if self.settings.fixture_path:env['DAY20_FIXTURE_PATH']=self.settings.fixture_path
                    if server_id=='calendar':env.update(YANDEX_CALDAV_USERNAME=self.settings.calendar_username,YANDEX_CALDAV_APP_PASSWORD=self.settings.calendar_password,YANDEX_CALDAV_URL=self.settings.calendar_url)
                    streams=await stack.enter_async_context(stdio_client(StdioServerParameters(command=sys.executable,args=['-m','backend.mcp_server',server_id],env=env,cwd=str(ROOT))))
                    session=await stack.enter_async_context(ClientSession(*streams))
                    await self.discover(server_id,session)
                self.connected=True;self.ready.set_result(None)
                while True:
                    item=await self.queue.get()
                    if item is None:break
                    entry,args,future,metadata,observation=item
                    if future.cancelled():continue
                    exchange_id=self.observer.begin('mcp',dict(method='tools/call',params=dict(name=entry.name,arguments=args)),metadata,server_id=entry.server_id,alias=entry.alias) if self.observer else None
                    observation['id']=exchange_id
                    try:
                        result=await asyncio.wait_for(entry.session.call_tool(entry.name,args),timeout=self.settings.tool_timeout)
                        if self.observer:self.observer.finish(exchange_id,'error' if result.isError else 'completed',response=result.model_dump(exclude_none=True),error='MCP инструмент отклонил запрос' if result.isError else None)
                        if result.isError:
                            texts=[c.text for c in result.content if c.type=='text']
                            raise ValueError((' '.join(texts))[:500] or 'MCP инструмент отклонил запрос')
                        if result.structuredContent is not None:body=result.structuredContent
                        else:
                            texts=[c.text for c in result.content if c.type=='text'];body=json.loads('\n'.join(texts)) if texts else {}
                        if not future.done():future.set_result(body)
                    except BaseException as error:
                        if self.observer:self.observer.finish(exchange_id,'interrupted' if isinstance(error,asyncio.CancelledError) else 'error',error='MCP обмен прерван' if isinstance(error,asyncio.CancelledError) else 'MCP обмен завершился ошибкой: '+type(error).__name__)
                        if not future.done():future.set_exception(error if isinstance(error,ValueError) else TimeoutError('Истёк срок MCP вызова') if isinstance(error,(TimeoutError,asyncio.CancelledError)) else ValueError('MCP источник недоступен'))
                        if isinstance(error,asyncio.CancelledError):raise
        except BaseException as error:
            def expected_shutdown(value):
                if isinstance(value,BaseExceptionGroup):return all(expected_shutdown(e) for e in value.exceptions)
                return isinstance(value,(anyio.BrokenResourceError,anyio.ClosedResourceError))
            if self.stopping and expected_shutdown(error):return
            if self.ready and not self.ready.done():self.ready.set_exception(RuntimeError('Не удалось подключить MCP серверы'))
            raise
        finally:
            self.connected=False
            while not self.queue.empty():
                item=self.queue.get_nowait()
                if item and not item[2].done():item[2].set_exception(RuntimeError('MCP соединения закрыты'))
