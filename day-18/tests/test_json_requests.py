"""Diagnostic copies must explain real requests without changing or leaking them."""
import asyncio
from datetime import timedelta
import json
import sqlite3

import httpx
import pytest

from backend import agent, main
from backend.agent import AgentRuntime
from backend.config import ROOT, Settings
from backend.diagnostics import DiagnosticLog, MAX_BODY_BYTES
from backend.scheduling import now_utc
from backend.store import Store
from test_collection import FakeModel, inprocess_connector, job, PAYLOAD, tool
from test_agent_reliability import install_sdk_transport, provider_error, response


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'weather.sqlite3')


def config(store,key='test-key'):
    return Settings(store.path,key,'test-model','Asia/Omsk')


def test_snapshot_matches_real_sdk_body_and_stays_immutable(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    async def handler(request,number):
        if number == 1:
            return response(request,tool('load_skill',{'name':'periodic-weather-collection'},id='skill-id'))
        if number == 2:
            return response(request,tool('list_schedules',{},id='list-id'))
        return response(request,{'role':'assistant','content':'Заданий нет'})
    _,transports,_ = install_sdk_transport(monkeypatch,handler)
    result = asyncio.run(AgentRuntime(store,config=config(store)).run('Покажи расписания'))
    records = result['json_requests']
    model_records = [record for record in records if record['protocol'] == 'deepseek']
    assert len(records) == 5 and len(model_records) == 3
    assert [record['request'] for record in model_records] == transports[0].requests
    assert [len(record['request']['messages']) for record in model_records] == [2,4,6]
    assert all(record['ok'] is True for record in records)
    assert records[1]['request'] == {'method':'tools/call','params':{'name':'load_skill','arguments':{'name':'periodic-weather-collection'}}}
    assert records[1]['response']['name'] == 'periodic-weather-collection'
    assert records[3]['response'] == {'jobs':[]}
    assert records[-1]['response'] == {'role':'assistant','content':'Заданий нет'}
    assert [step['tool'] for step in result['trace']] == ['load_skill','list_schedules']


def test_secrets_server_paths_and_embedded_json_strings_are_redacted(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    key = 'secret"key\\value'
    token = 'private-chat-token'
    model = FakeModel([{'role':'assistant','content':f'{key} {ROOT} {store.path} {token}'}])
    prompt = json.dumps({'api_key':key,'path':str(ROOT),'db_path':store.path,'processing_token':token})
    result = asyncio.run(AgentRuntime(store,model,config(store,key)).run(prompt,chat_token=token))
    serialized = json.dumps(result['json_requests'],ensure_ascii=False)
    for forbidden in [key,json.dumps(key)[1:-1],str(ROOT),store.path,token]:
        assert forbidden not in serialized
    # Diagnostic redaction does not change what the model receives.
    assert model.calls[0][0][-1]['content'] == prompt
    assert '[скрыто]' in serialized
    direct = DiagnosticLog([key]).snapshot({'headers':{'Authorization':key},'env':'private','db_path':'private','nested':{'token':'private'}})
    assert direct == {'headers':'[скрыто]','env':'[скрыто]','db_path':'[скрыто]','nested':{'token':'[скрыто]'}}


def test_large_diagnostic_copy_is_bounded_valid_json_and_does_not_change_model(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    prompt = 'я' * MAX_BODY_BYTES
    model = FakeModel([{'role':'assistant','content':'Уточните город'}])
    result = asyncio.run(AgentRuntime(store,model,config(store)).run(prompt))
    request = result['json_requests'][0]['request']
    assert request['_truncated'] is True and request['original_bytes'] > MAX_BODY_BYTES
    assert len(json.dumps(request,ensure_ascii=False).encode()) < MAX_BODY_BYTES
    assert model.calls[0][0][-1]['content'] == prompt


def finish_chat(store,result,request='request',started=None):
    started = started or store.begin_chat(request,'Покажи расписания')
    assert store.finish_chat(request,result['answer'],result['trace'],started['processing_token'],json_requests=result['json_requests'])
    return store.state()['messages'][-1]['id']


def test_chat_requests_roundtrip_lazy_api_and_polling(store,monkeypatch):
    from fastapi.testclient import TestClient
    inprocess_connector(store,monkeypatch)
    monkeypatch.setattr(main,'get_store',lambda:store)
    monkeypatch.setattr(main,'settings',lambda:config(store))
    monkeypatch.setattr(main,'cron_status',lambda:{'installed':True})
    result = asyncio.run(AgentRuntime(store,FakeModel([tool('list_schedules',{}),{'role':'assistant','content':'Готово'}]),config(store)).run('Покажи расписания'))
    message_id = finish_chat(store,result)
    reopened = Store(store.path)
    assert reopened.requests('message',message_id) == result['json_requests']
    with TestClient(main.app) as client:
        assert client.get(f'/api/messages/{message_id}/requests').json() == {'requests':result['json_requests']}
        state = client.get('/api/state').json()
        assert state['messages'][-1]['has_json_requests'] is True
        assert state['messages'][0]['has_json_requests'] is False
        assert all('json_requests' not in item for item in state['messages'] + state['runs'])
        assert all('request' not in item for item in state['messages'] + state['runs'])
        assert client.get(f'/api/messages/{state["messages"][0]["id"]}/requests').json() == {'requests':[]}
        assert client.get('/api/messages/unknown/requests').status_code == 404
        assert client.get('/api/runs/unknown/requests').status_code == 404


def test_nested_provider_failure_persists_partial_safe_json(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    class Model:
        step = 0
        async def complete(self,messages,tools):
            self.step += 1
            if self.step == 1:
                return tool('list_schedules',{})
            raise ExceptionGroup('SECRET path',[provider_error(503)])
    started = store.begin_chat('request','Покажи расписания')
    result = asyncio.run(AgentRuntime(store,Model(),config(store)).run('Покажи расписания',request_id='request',chat_token=started['processing_token']))
    message_id = finish_chat(store,result,started=started)
    records = store.requests('message',message_id)
    assert len(records) == 3 and records[-1]['ok'] is False and 'response' not in records[-1]
    assert 'недоступен' in records[-1]['error']
    assert 'SECRET' not in json.dumps(records) and '/private/' not in json.dumps(records)


def test_mcp_failure_is_safe_and_preserves_request(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    async def broken(*args):
        raise ExceptionGroup('SECRET',[RuntimeError('SECRET /private/db_path')])
    monkeypatch.setattr(agent,'call',broken)
    result = asyncio.run(AgentRuntime(store,FakeModel([tool('list_schedules',{})]),config(store)).run('Покажи расписания'))
    record = result['json_requests'][-1]
    assert record['protocol'] == 'mcp' and record['ok'] is False
    assert record['request']['params'] == {'name':'list_schedules','arguments':{}}
    assert 'SECRET' not in record['error'] and 'MCP' in record['error']


def test_run_requests_roundtrip_and_fenced_stale_finish(store,monkeypatch):
    from fastapi.testclient import TestClient
    from backend import weather
    from backend.runner import run_due
    inprocess_connector(store,monkeypatch)
    monkeypatch.setattr(main,'get_store',lambda:store)
    job(store,now=now_utc()-timedelta(minutes=2))
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
                return tool('save_weather_result',{'result_id':json.loads(messages[-1]['content'])['result_id']})
            return {'role':'assistant','content':'Сохранено'}
    result = asyncio.run(run_due(store,AgentRuntime(store,Model(),config(store))))
    run = store.state()['runs'][0]
    assert result['results'][0]['status'] == 'success' and run['has_json_requests'] is True
    records = Store(store.path).requests('run',run['id'])
    assert len(records) == 5 and records[3]['response']['saved'] is True
    with TestClient(main.app) as client:
        assert client.get(f'/api/runs/{run["id"]}/requests').json() == {'requests':records}
    # A stale completion cannot overwrite the diagnostics of this finished run.
    stale = {'run_id':run['id'],'job_id':run['job_id'],'token':'stale'}
    assert not store.finish(stale,json_requests=[{'error':'stale'}])
    assert store.requests('run',run['id']) == records


def test_chat_fence_keeps_diagnostics_atomic(store):
    start = now_utc()
    first = store.begin_chat('request','Покажи расписания',now=start)
    second = store.begin_chat('request','Покажи расписания',now=start+timedelta(seconds=121))
    assert not store.finish_chat('request','stale',[],first['processing_token'],json_requests=[{'error':'stale'}])
    assert len(store.state()['messages']) == 1
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM json_requests').fetchone()[0] == 0
    assert store.finish_chat('request','current',[],second['processing_token'],json_requests=[{'protocol':'deepseek','label':'Model','request':{},'ok':True,'response':{}}])
    message = store.state()['messages'][-1]
    assert message['has_json_requests'] is True
    assert store.requests('message',message['id'])[0]['label'] == 'Model'


def test_existing_database_records_remain_without_invented_requests(store):
    start = store.begin_chat('legacy','Старый запрос')
    assert store.finish_chat('legacy','Старый ответ',[],start['processing_token'])
    job(store,now=now_utc()-timedelta(minutes=2))
    scope = store.claim_due('fake')
    store.finish(scope,error='Legacy failure')
    # Simulate the previous schema: only the new auxiliary table is absent.
    with store.connection(True) as db:
        db.execute('DROP TABLE json_requests')
    reopened = Store(store.path)
    state = reopened.state()
    assert all(row['has_json_requests'] is False for row in state['messages'] + state['runs'])
    assert reopened.requests('message',state['messages'][-1]['id']) == []
    assert reopened.requests('run',state['runs'][0]['id']) == []
    assert state['messages'][-1]['content'] == 'Старый ответ'


def test_cancelled_request_retains_partial_diagnostics_without_swallowing(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    async def scenario():
        entered = asyncio.Event()
        class Model:
            async def complete(self,messages,tools):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(AgentRuntime(store,Model(),config(store)).run('Покажи расписания'))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        records = cancelled.value.json_requests
        assert len(records) == 1 and records[0]['ok'] is False
        assert records[0]['error'] == 'Запрос прерван'
    asyncio.run(scenario())
