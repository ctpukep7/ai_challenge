"""Provider boundaries use the real SDK and isolated SQLite, never live accounts."""
import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI as SDKClient, APIConnectionError, APIStatusError, APITimeoutError

from backend import agent
from backend.agent import AgentRuntime, DeepSeek, public_error
from backend.config import Settings
from backend.store import Store
from test_collection import tool, inprocess_connector


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'weather.sqlite3')


def config(store):
    return Settings(store.path, 'test-key', 'test-model', 'Asia/Omsk')


def provider_error(status):
    request = httpx.Request('POST', 'https://provider.invalid')
    return APIStatusError('SECRET /private/path/.env', response=httpx.Response(status, request=request), body={'secret':'SECRET'})


@pytest.mark.parametrize('status,text',[
    (401,'отклонил ключ'), (402,'балансе'), (429,'ограничил'), (503,'недоступен'), (500,'недоступен'), (400,'обработать'),
])
def test_safe_status_errors_direct_and_nested(status,text):
    error = provider_error(status)
    for exc in [error,ExceptionGroup('stdio', [ExceptionGroup('session', [error])])]:
        result = public_error(exc)
        assert text in result
        assert 'SECRET' not in result and '/private/' not in result and '.env' not in result


def test_safe_timeout_connection_and_unknown_errors():
    request = httpx.Request('POST','https://provider.invalid')
    for exc,text in [(APITimeoutError(request),'не ответил вовремя'),
                     (APIConnectionError(message='SECRET /private/.env',request=request),'соединиться'),
                     (TimeoutError('SECRET'),'90 секунд'),
                     (ValueError('SECRET /private/.env'),'MCP')]:
        assert text in public_error(exc)
        nested = ExceptionGroup('SECRET',[exc])
        assert public_error(nested) == public_error(exc)
        assert 'SECRET' not in public_error(exc)


def test_cancellation_is_not_swallowed_even_in_group():
    with pytest.raises(asyncio.CancelledError):
        public_error(BaseExceptionGroup('stdio',[ValueError('failure'),asyncio.CancelledError()]))


class Transport(httpx.AsyncBaseTransport):
    def __init__(self,handler):
        self.handler = handler
        self.requests = []
        self.closed = False
    async def handle_async_request(self,request):
        self.requests.append(json.loads(request.content))
        return await self.handler(request,len(self.requests))
    async def aclose(self):
        self.closed = True


def install_sdk_transport(monkeypatch,handler):
    clients = []
    transports = []
    options = []
    def factory(**kwargs):
        options.append(kwargs)
        transport = Transport(handler)
        transports.append(transport)
        client = SDKClient(**kwargs,http_client=httpx.AsyncClient(transport=transport,timeout=kwargs['timeout']))
        clients.append(client)
        return client
    monkeypatch.setattr(agent,'AsyncOpenAI',factory)
    return clients,transports,options


def response(request,message):
    return httpx.Response(200,request=request,json={
        'id':'completion','object':'chat.completion','created':1,'model':'test-model',
        'choices':[{'index':0,'finish_reason':'tool_calls' if message.get('tool_calls') else 'stop','message':message}],
    })


def status_response(request,status):
    return httpx.Response(status,request=request,headers={'retry-after':'0.001'},json={'error':{'message':'SECRET /private/path/.env'}})


def test_client_reused_and_closed_retry_does_not_replay_mutation(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    city = store.register_cities([{'label':'Омск','latitude':55.,'longitude':73.,'country':'Россия'}])[0]
    async def handler(request,number):
        if number == 1:
            return status_response(request,503)
        if number == 2:
            return response(request,tool('load_skill',{'name':'periodic-weather-collection'},id='skill-id'))
        if number == 3:
            return response(request,tool('create_weather_schedule',{'city_id':city['city_id'],'instruction':'Собирай погоду','schedule_type':'interval','interval_minutes':5,'timezone':'Asia/Omsk'},id='create-id'))
        return response(request,{'role':'assistant','content':'Расписание сохранено'})
    clients,transports,options = install_sdk_transport(monkeypatch,handler)
    begin = store.begin_chat('request','Каждые пять минут собирай погоду в Омске')
    result = asyncio.run(AgentRuntime(store,config=config(store)).run('Каждые пять минут собирай погоду в Омске',request_id='request',chat_token=begin['processing_token']))
    assert 'error' not in result
    assert len(clients) == 1 and clients[0].is_closed() and transports[0].closed
    assert options[0]['max_retries'] == 1
    assert options[0]['timeout'].connect == 20 and options[0]['timeout'].read == 45
    requests = transports[0].requests
    assert len(requests) == 4 and requests[0] == requests[1]
    assert requests[2]['messages'][-1]['tool_call_id'] == 'skill-id'
    assert requests[3]['messages'][-1]['tool_call_id'] == 'create-id'
    assert len(store.list_jobs()) == 1
    assert [step['tool'] for step in result['trace']] == ['load_skill','create_weather_schedule']


def test_sdk_retry_bound_on_provider_failure(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    async def handler(request,number):
        return status_response(request,503)
    clients,transports,_ = install_sdk_transport(monkeypatch,handler)
    result = asyncio.run(AgentRuntime(store,config=config(store)).run('Собирай погоду'))
    assert len(transports[0].requests) == 2
    assert 'недоступен' in result['error']
    assert clients[0].is_closed() and transports[0].closed
    assert not store.list_jobs() and 'SECRET' not in result['answer']


def test_committed_mutation_confirmed_after_final_provider_failure(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    city = store.register_cities([{'label':'Омск','latitude':55.,'longitude':73.,'country':'Россия'}])[0]
    async def handler(request,number):
        if number == 1:
            return response(request,tool('load_skill',{'name':'periodic-weather-collection'}))
        if number == 2:
            return response(request,tool('create_weather_schedule',{'city_id':city['city_id'],'instruction':'Собирай погоду','schedule_type':'interval','interval_minutes':5,'timezone':'Asia/Omsk'},id='create-id'))
        raise httpx.ConnectError('SECRET /private/path/.env',request=request)
    clients,transports,_ = install_sdk_transport(monkeypatch,handler)
    begin = store.begin_chat('request','Собирай погоду')
    result = asyncio.run(AgentRuntime(store,config=config(store)).run('Собирай погоду',request_id='request',chat_token=begin['processing_token']))
    assert len(transports[0].requests) == 4
    assert transports[0].requests[2] == transports[0].requests[3]
    assert 'Изменения сохранены' in result['answer'] and 'сбор включён' in result['answer']
    assert 'соединиться' in result['error'] and 'SECRET' not in result['answer']
    assert len(store.list_jobs()) == 1
    assert [step['tool'] for step in result['trace']].count('create_weather_schedule') == 1
    assert clients[0].is_closed()


def test_ledger_recovers_commit_when_mcp_reply_is_lost(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    city = store.register_cities([{'label':'Омск','latitude':55.,'longitude':73.,'country':'Россия'}])[0]
    original_call = agent.call
    async def lost_reply(session,name,args):
        await original_call(session,name,args)
        if name == 'create_weather_schedule':
            raise ExceptionGroup('stdio',[RuntimeError('SECRET /private/path')])
        return {'content':'skill content'}
    monkeypatch.setattr(agent,'call',lost_reply)
    class Model:
        step = 0
        async def complete(self,messages,tools):
            self.step += 1
            if self.step == 1:
                return tool('load_skill',{'name':'periodic-weather-collection'})
            return tool('create_weather_schedule',{'city_id':city['city_id'],'instruction':'Собирай погоду','schedule_type':'interval','interval_minutes':5,'timezone':'Asia/Omsk'})
    begin = store.begin_chat('request','Собирай погоду')
    result = asyncio.run(AgentRuntime(store,Model(),config(store)).run('Собирай погоду',request_id='request',chat_token=begin['processing_token']))
    assert len(store.list_jobs()) == 1
    assert 'Изменения сохранены' in result['answer']
    assert result['trace'][-1]['tool'] == 'create_weather_schedule' and result['trace'][-1]['ok']
    assert 'SECRET' not in result['answer']


def test_provider_client_closed_on_cancellation(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    async def scenario():
        entered = asyncio.Event()
        async def handler(request,number):
            entered.set()
            await asyncio.Event().wait()
        clients,transports,_ = install_sdk_transport(monkeypatch,handler)
        task = asyncio.create_task(AgentRuntime(store,config=config(store)).run('Собирай погоду'))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert clients[0].is_closed() and transports[0].closed
    asyncio.run(scenario())


def test_global_deadline_bounds_request_and_closes_client(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    actual_timeout = asyncio.timeout
    limits = []
    def brief_timeout(seconds):
        limits.append(seconds)
        return actual_timeout(0.02)
    monkeypatch.setattr(agent.asyncio,'timeout',brief_timeout)
    async def handler(request,number):
        await asyncio.Event().wait()
    clients,transports,_ = install_sdk_transport(monkeypatch,handler)
    result = asyncio.run(AgentRuntime(store,config=config(store)).run('Собирай погоду'))
    assert limits == [90] and '90 секунд' in result['error']
    assert clients[0].is_closed() and transports[0].closed
    assert len(transports[0].requests) == 1


def test_concurrent_runs_and_new_event_loops_never_share_client(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    async def handler(request,number):
        await asyncio.sleep(0)
        return response(request,{'role':'assistant','content':'Уточните город'})
    clients,transports,_ = install_sdk_transport(monkeypatch,handler)
    runtime = AgentRuntime(store,config=config(store))
    async def concurrent():
        results = await asyncio.gather(runtime.run('Первый'),runtime.run('Второй'))
        assert all('error' not in result for result in results)
    asyncio.run(concurrent())
    assert 'error' not in asyncio.run(runtime.run('Третий'))
    assert len(clients) == 3 and len({id(client) for client in clients}) == 3
    assert all(client.is_closed() for client in clients)
    assert all(transport.closed for transport in transports)


def test_saved_weather_still_success_when_final_provider_reply_fails(store,monkeypatch):
    from datetime import datetime, timedelta, timezone
    from backend import weather
    from backend.runner import run_due
    from test_collection import job, PAYLOAD
    inprocess_connector(store,monkeypatch)
    job(store,now=datetime.now(timezone.utc)-timedelta(minutes=2))
    async def current(job):
        return PAYLOAD
    monkeypatch.setattr(weather,'current_weather',current)
    class Model:
        step = 0
        async def complete(self,messages,tools):
            self.step += 1
            if self.step == 1:
                return tool('get_current_weather',{})
            if self.step == 2:
                result = json.loads(messages[-1]['content'])
                return tool('save_weather_result',{'result_id':result['result_id']})
            raise ExceptionGroup('stdio',[provider_error(503)])
    result = asyncio.run(run_due(store,AgentRuntime(store,Model(),config(store))))
    assert result['results'][0]['status'] == 'success'
    assert result['results'][0]['answer'] == 'Наблюдение сохранено'
    assert store.data()['count'] == 1
    assert store.state()['runs'][0]['error'] is None
