"""Lifecycle actions archive exactly the selected job and fence old collectors."""
import asyncio
from datetime import timedelta
import json

import pytest

from backend.agent import AgentRuntime
from backend.config import Settings
from backend.mcp_client import connect, call
from backend.runner import run_due
from backend.scheduling import now_utc
from backend.store import Store
from test_collection import FakeModel, NOW, PAYLOAD, inprocess_connector, job, tool


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'weather.sqlite3')


def test_real_mcp_delete_requires_skill_and_fresh_list_and_is_idempotent(store):
    selected = job(store,request_id='selected')
    other = job(store,minutes=30,request_id='other')
    async def scenario():
        async with connect(store.path) as session:
            assert 'delete_weather_schedule' in [entry.name for entry in (await session.list_tools()).tools]
            with pytest.raises(ValueError,match='load_skill'):
                await call(session,'delete_weather_schedule',{'job_id':selected['id']})
            await call(session,'load_skill',{'name':'periodic-weather-collection'})
            with pytest.raises(ValueError,match='list_schedules'):
                await call(session,'delete_weather_schedule',{'job_id':selected['id']})
            await call(session,'list_schedules',{})
            removed = await call(session,'delete_weather_schedule',{'job_id':selected['id']})
            assert removed['status'] == 'cancelled' and removed['next_run_at'] is None
            # Each lifecycle mutation consumes the previous list snapshot.
            with pytest.raises(ValueError,match='list_schedules'):
                await call(session,'set_schedule_status',{'job_id':other['id'],'status':'paused'})
            listed = await call(session,'list_schedules',{})
            assert {entry['id'] for entry in listed['jobs']} == {selected['id'],other['id']}
            repeated = await call(session,'delete_weather_schedule',{'job_id':selected['id']})
            assert repeated == removed
            await call(session,'list_schedules',{})
            with pytest.raises(ValueError,match='не найдено'):
                await call(session,'delete_weather_schedule',{'job_id':'missing'})
            with pytest.raises(ValueError,match='нельзя возобновить'):
                await call(session,'set_schedule_status',{'job_id':selected['id'],'status':'active'})
    asyncio.run(scenario())
    rows = {entry['id']:entry for entry in store.list_jobs()}
    assert rows[other['id']] == other
    assert rows[selected['id']]['status'] == 'cancelled'


def test_delete_revokes_running_claim_but_preserves_prior_observation_and_json(store):
    selected = job(store,request_id='selected')
    other = job(store,minutes=1440,request_id='other')
    first = store.claim_due('fake',now=NOW+timedelta(minutes=1))
    observation = store.observation(first,PAYLOAD,now=NOW+timedelta(minutes=1))
    store.save_result(first,observation['result_id'],now=NOW+timedelta(minutes=1))
    diagnostic = [{'protocol':'mcp','label':'MCP','request':{'method':'tools/call','params':{'name':'save_weather_result','arguments':{'result_id':observation['result_id']}}},'ok':True,'response':{'saved':True}}]
    assert store.finish(first,now=NOW+timedelta(minutes=1),json_requests=diagnostic)
    running = store.claim_due('fake',now=NOW+timedelta(minutes=2))
    draft = store.observation(running,PAYLOAD,now=NOW+timedelta(minutes=2))
    removed = store.delete_job(selected['id'],now=NOW+timedelta(minutes=2))
    assert removed['status'] == 'cancelled' and removed['next_run_at'] is None
    with pytest.raises(ValueError):
        store.save_result(running,draft['result_id'],now=NOW+timedelta(minutes=2))
    assert not store.finish(running,json_requests=[{'error':'stale'}],now=NOW+timedelta(minutes=2))
    assert store.requests('run',first['run_id']) == diagnostic
    assert store.data()['count'] == 1
    assert store.claim_due('fake',now=NOW+timedelta(minutes=3)) is None
    assert store.claim_due('fake',now=NOW+timedelta(days=1))['job_id'] == other['id']


def test_deleted_only_job_never_triggers_model(store):
    selected = job(store,now=now_utc()-timedelta(minutes=2))
    store.delete_job(selected['id'])
    model = FakeModel([])
    result = asyncio.run(run_due(store,AgentRuntime(store,model,Settings(store.path,'test-key','fake','Asia/Omsk'))))
    assert result['executed'] == 0 and model.calls == []


def test_delete_ledger_recovery_keeps_latest_action_and_fences_old_writer(store):
    selected = job(store)
    started = store.begin_chat('change','Останови, затем удали задачу',now=NOW)
    store.set_status(selected['id'],'paused',request_id='change',chat_token=started['processing_token'],now=NOW)
    store.delete_job(selected['id'],request_id='change',chat_token=started['processing_token'],now=NOW)
    actions = store.chat_actions('change')
    assert len(actions) == 1 and actions[0]['tool'] == 'delete_weather_schedule'
    recovered = store.begin_chat('change','Останови, затем удали задачу',now=NOW+timedelta(seconds=121))
    assert 'расписание удалено' in recovered['answer']
    assert recovered['trace'][0]['tool'] == 'delete_weather_schedule'
    assert recovered['trace'][0]['result']['status'] == 'cancelled'
    with pytest.raises(ValueError,match='устаревшее'):
        store.delete_job(selected['id'],request_id='change',chat_token=started['processing_token'],now=NOW+timedelta(seconds=122))


def test_chat_delete_uses_actual_mcp_and_preserves_json(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    selected = job(store,request_id='selected')
    other = job(store,request_id='other',minutes=30)
    class Model:
        step = 0
        async def complete(self,messages,tools):
            self.step += 1
            if self.step == 1:
                return tool('load_skill',{'name':'periodic-weather-collection'})
            if self.step == 2:
                return tool('list_schedules',{})
            if self.step == 3:
                listed = json.loads(messages[-1]['content'])['jobs']
                chosen = next(entry for entry in listed if entry['interval_minutes'] == 1)
                return tool('delete_weather_schedule',{'job_id':chosen['id']})
            return {'role':'assistant','content':'Расписание удалено; накопленные данные сохранены'}
    started = store.begin_chat('delete','Удали крон задачу с интервалом одна минута')
    result = asyncio.run(AgentRuntime(store,Model(),Settings(store.path,'test-key','fake','Asia/Omsk')).run('Удали крон задачу с интервалом одна минута',request_id='delete',chat_token=started['processing_token']))
    assert 'error' not in result
    assert [entry['tool'] for entry in result['trace']] == ['load_skill','list_schedules','delete_weather_schedule']
    delete_record = next(entry for entry in result['json_requests'] if entry['protocol'] == 'mcp' and entry['request']['params']['name'] == 'delete_weather_schedule')
    assert delete_record['request']['params']['arguments'] == {'job_id':selected['id']}
    assert delete_record['response']['status'] == 'cancelled' and delete_record['ok'] is True
    assert store.finish_chat('delete',result['answer'],result['trace'],started['processing_token'],json_requests=result['json_requests'])
    message = store.state()['messages'][-1]
    assert store.requests('message',message['id']) == result['json_requests']
    assert next(entry for entry in store.list_jobs() if entry['id'] == other['id']) == other


def test_delete_confirmed_when_final_model_response_fails(store,monkeypatch):
    inprocess_connector(store,monkeypatch)
    selected = job(store)
    class Model:
        step = 0
        async def complete(self,messages,tools):
            self.step += 1
            if self.step == 1:
                return tool('load_skill',{'name':'periodic-weather-collection'})
            if self.step == 2:
                return tool('list_schedules',{})
            if self.step == 3:
                return tool('delete_weather_schedule',{'job_id':selected['id']})
            raise RuntimeError('SECRET')
    started = store.begin_chat('delete','Удали задачу')
    result = asyncio.run(AgentRuntime(store,Model(),Settings(store.path,'test-key','fake','Asia/Omsk')).run('Удали задачу',request_id='delete',chat_token=started['processing_token']))
    assert 'Изменения сохранены' in result['answer'] and 'расписание удалено' in result['answer']
    assert 'SECRET' not in result['answer']
    assert store.chat_actions('delete')[0]['tool'] == 'delete_weather_schedule'


def test_http_delete_uses_real_mcp_preserves_other_jobs_and_is_idempotent(store,monkeypatch):
    from fastapi.testclient import TestClient
    from backend import main
    selected = job(store,request_id='selected')
    other = job(store,request_id='other',minutes=30)
    monkeypatch.setattr(main,'get_store',lambda:store)
    monkeypatch.setattr(main,'settings',lambda:Settings(store.path,'test-key','fake','Asia/Omsk'))
    monkeypatch.setattr(main,'cron_status',lambda:{'installed':True})
    with TestClient(main.app) as client:
        result = client.post(f'/api/jobs/{selected["id"]}/delete')
        assert result.status_code == 200
        rows = {entry['id']:entry for entry in result.json()['state']['jobs']}
        assert rows[selected['id']]['status'] == 'cancelled' and rows[selected['id']]['next_run_at'] is None
        assert rows[other['id']]['status'] == 'active'
        again = client.post(f'/api/jobs/{selected["id"]}/delete')
        assert again.status_code == 200 and len(again.json()['state']['jobs']) == 2
        assert client.post('/api/jobs/missing/delete').status_code == 400
        assert client.post(f'/api/jobs/{selected["id"]}/status',json={'status':'active'}).status_code == 400
        assert client.post(f'/api/jobs/{other["id"]}/status',json={'status':'paused'}).status_code == 200
        assert client.post(f'/api/jobs/{other["id"]}/status',json={'status':'active'}).status_code == 200
