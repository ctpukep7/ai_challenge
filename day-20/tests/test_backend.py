"""Real stdio discovery and the model-selected six-tool path, offline only."""
import asyncio
from datetime import datetime,timedelta
import json
from types import SimpleNamespace
import pytest
from backend.config import Settings
from backend.store import Store
from backend.registry import Registry
from backend.agent import Agent

START='2099-10-01T10:00:00+06:00';END='2099-10-01T13:00:00+06:00'

def fixture(tmp_path):
    start=datetime.fromisoformat('2099-10-01T00:00:00+06:00')
    hours=[dict(time=(start+timedelta(hours=i)).isoformat(),temperature_c=12,precipitation_probability=10,wind_ms=2) for i in range(24)]
    data=dict(cities=[dict(name='Омск',country='Россия',region='Омская область',latitude=54.99,longitude=73.36,timezone='Asia/Omsk')],forecast=dict(hours=hours,timezone='Asia/Omsk',available_start=hours[0]['time'],available_end=(start+timedelta(days=1)).isoformat()),calendars=[dict(id='test-calendar',name='Тестовый')])
    path=tmp_path/'fixture.json';path.write_text(json.dumps(data));return str(path)

class ScriptModel:
    def __init__(self):self.step=0;self.received=[]
    async def complete(self,messages,tools):
        self.received.append((messages.copy(),tools))
        results=[json.loads(m['content']) for m in messages if m['role']=='tool']
        aliases=[t['function']['name'] for t in tools]
        assert 'calendar__create_trip_event' not in aliases
        # Any source order is selected by model, never by a backend workflow.
        operations=[('weather__resolve_city',dict(name='Омск')),('calendar__list_calendars',{}),
                    ('calendar__find_free_slots',dict(calendars_id=results[1]['calendars_id'],calendar_id='test-calendar',start=START,end='2099-10-01T18:00:00+06:00',duration_hours=3,day_start='10:00',day_end='18:00')) if len(results)>1 else ('',{}),
                    ('weather__get_hourly_forecast',dict(city_id=results[0]['city_id'],city_index=0,start=START,end='2099-10-01T18:00:00+06:00')) if results else ('',{}),
                    ('plans__save_trip_draft',dict(forecast_id=results[3]['forecast_id'],availability_id=results[2]['availability_id'],start=START,end=END,title='Поездка в Омск',reason='Погода подходит, календарь свободен',min_temperature_c=5,max_precipitation_probability=30,max_wind_ms=8)) if len(results)>3 else ('',{}),
                    ('plans__get_trip_plan',dict(plan_id=results[4]['id'])) if len(results)>4 else ('',{})]
        if self.step>=len(operations):return dict(content='Выбран подходящий интервал',tool_calls=[])
        alias,args=operations[self.step];self.step+=1
        return dict(content=None,tool_calls=[dict(id=f'call-{self.step}',type='function',function=dict(name=alias,arguments=json.dumps(args)))])

def test_real_three_stdio_processes_and_six_tool_flow(tmp_path):
    async def scenario():
        settings=Settings(db_path=str(tmp_path/'db.sqlite'),fixture_path=fixture(tmp_path),tool_timeout=15)
        store=Store(settings.db_path);registry=Registry(settings);model=ScriptModel()
        run,_=store.new_run('request','fingerprint','Запланируй поездку',None)
        await registry.start()
        try:
            assert len(registry.entries)==7
            assert all(server['status']=='connected' for server in registry.registry())
            await Agent(store,registry,model,settings).run(run['id'])
            final=store.get('run',run['id'])
            assert final['status']=='completed',final
            assert len(final['trace'])==6
            assert {t['server_id'] for t in final['trace']}=={'weather','calendar','plans'}
            assert final['plan']['status']=='draft'
            assert json.loads(open(settings.fixture_path).read()).get('writes',0)==0
            for index,(messages,_) in enumerate(model.received[1:]):
                tools=[m for m in messages if m['role']=='tool']
                assert tools[-1]['tool_call_id']==f'call-{index+1}'
            with pytest.raises(ValueError):await registry.call('calendar__create_trip_event',{'plan_id':final['plan']['id']})
            with pytest.raises(ValueError):await registry.call('weather__resolve_city',{'name':'Омск','api_key':'do-not-log'},run_id=run['id'])
        finally:await registry.close()
        assert registry.task is None
    asyncio.run(scenario())


def test_paginated_discovery_alias_collision_and_schemas(tmp_path):
    class Session:
        def __init__(self):self.calls=[]
        async def initialize(self):self.calls.append('initialize')
        async def list_tools(self,cursor=None):
            self.calls.append(cursor)
            return SimpleNamespace(tools=[SimpleNamespace(name='shared' if not cursor else 'second',description='test',inputSchema={'type':'object','properties':{'value':{'type':'string'}},'required':['value']})],nextCursor='page2' if not cursor else None)
    async def scenario():
        registry=Registry(Settings(db_path=str(tmp_path/'db.sqlite')));a=Session();b=Session()
        await registry.discover('weather',a);await registry.discover('calendar',b)
        assert registry.entries['weather__shared'].session is a
        assert registry.entries['calendar__shared'].session is b
        assert a.calls==['initialize',None,'page2']
        with pytest.raises(ValueError):await registry.call('weather__shared',{'value':99})
        with pytest.raises(ValueError):await registry.call('unknown__shared',{})
    asyncio.run(scenario())


def test_real_stdio_tool_timeout_then_owner_closes_connections(tmp_path):
    async def scenario():
        path=fixture(tmp_path);data=json.loads(open(path).read());data['resolve_delay']=0.4;open(path,'w').write(json.dumps(data))
        settings=Settings(db_path=str(tmp_path/'db.sqlite'),fixture_path=path,tool_timeout=15)
        store=Store(settings.db_path);run,_=store.new_run('request','fingerprint','Погода',None)
        registry=Registry(settings);await registry.start()
        owner=registry.task;settings.tool_timeout=0.05
        with pytest.raises(TimeoutError):await registry.call('weather__resolve_city',dict(name='Омск'),run_id=run['id'])
        await registry.close()
        assert owner.done() and not registry.connected and registry.queue.empty()
        with pytest.raises(RuntimeError):await registry.call('weather__resolve_city',dict(name='Омск'),run_id=run['id'])
    asyncio.run(scenario())


def test_real_stdio_cancelled_run_keeps_terminal_trace_and_closes(tmp_path):
    class Model:
        async def complete(self,messages,tools):return dict(content=None,tool_calls=[dict(id='cancel-call',type='function',function=dict(name='weather__resolve_city',arguments='{"name":"Омск"}'))])
    async def scenario():
        path=fixture(tmp_path);data=json.loads(open(path).read());data['resolve_delay']=0.3;open(path,'w').write(json.dumps(data))
        settings=Settings(db_path=str(tmp_path/'db.sqlite'),fixture_path=path,tool_timeout=15)
        store=Store(settings.db_path);run,_=store.new_run('request','fingerprint','Погода',None)
        registry=Registry(settings);await registry.start()
        task=asyncio.create_task(Agent(store,registry,Model(),settings).run(run['id']))
        for _ in range(100):
            if store.get('run',run['id'])['trace']:break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        final=store.get('run',run['id']);assert final['status']=='interrupted';assert final['trace'][-1]['status']=='error'
        await registry.close();assert registry.task is None and registry.queue.empty()
    asyncio.run(scenario())


def test_malformed_private_ics_never_reaches_mcp_model_trace_or_logs(tmp_path, capfd):
    sentinel='SYNTHETIC_PRIVATE_SUMMARY_NEVER_DISPLAY'
    class CalendarModel:
        def __init__(self):self.messages=[];self.step=0
        async def complete(self,messages,tools):
            self.messages=messages
            if self.step==0:alias='calendar__list_calendars';args={}
            elif self.step==1:
                calendars=json.loads([m for m in messages if m['role']=='tool'][-1]['content'])
                alias='calendar__find_free_slots';args=dict(calendars_id=calendars['calendars_id'],calendar_id='test-calendar',start=START,end=END,duration_hours=3,day_start='10:00',day_end='18:00')
            else:return dict(content='Источник недоступен',tool_calls=[])
            self.step+=1;return dict(content=None,tool_calls=[dict(id=f'call-{self.step}',type='function',function=dict(name=alias,arguments=json.dumps(args)))])
    async def scenario():
        path=fixture(tmp_path);data=json.loads(open(path).read());data['ics']=['BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nSUMMARY '+sentinel+'\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'];open(path,'w').write(json.dumps(data))
        settings=Settings(db_path=str(tmp_path/'db.sqlite'),fixture_path=path,tool_timeout=15);store=Store(settings.db_path);model=CalendarModel()
        run,_=store.new_run('request','fingerprint','Проверь занятость',None);registry=Registry(settings);await registry.start()
        try:
            await Agent(store,registry,model,settings).run(run['id'])
            final=store.get('run',run['id'])
            assert final['trace'][-1]['status']=='error'
            assert sentinel not in json.dumps(final)
            assert sentinel not in json.dumps(model.messages)
        finally:await registry.close()
    asyncio.run(scenario())
    out,err=capfd.readouterr();assert sentinel not in out+err


def test_recovery_before_approval_dispatch_is_explicit_uncertainty(tmp_path):
    store=Store(tmp_path/'db.sqlite')
    store.put('plan',dict(id='p',status='approved'))
    store.put('operation',dict(id='p',plan_id='p',status='pending',approved_at='2099-10-01'))
    store.recover()
    assert store.get('operation','p')['status']=='uncertain'
    assert store.get('plan','p')['status']=='uncertain'


def test_identical_raw_names_dispatch_to_correct_real_stdio_process(tmp_path,monkeypatch):
    from contextlib import asynccontextmanager
    import backend.registry as module
    script=tmp_path/'collision_server.py'
    script.write_text('''import sys
from mcp.server.fastmcp import FastMCP
server=FastMCP("collision-"+sys.argv[1])
@server.tool()
def echo(value: str) -> dict:
    return {"source":sys.argv[1],"value":value}
server.run(transport="stdio")
''')
    original=module.stdio_client
    @asynccontextmanager
    async def stdio(parameters):
        server_id=parameters.args[-1]
        if server_id in ('weather','calendar'):parameters.args=[str(script),server_id]
        async with original(parameters) as streams:yield streams
    monkeypatch.setattr(module,'stdio_client',stdio)
    async def scenario():
        settings=Settings(db_path=str(tmp_path/'db.sqlite'),tool_timeout=15);Store(settings.db_path)
        registry=Registry(settings);await registry.start()
        try:
            weather=await registry.call('weather__echo',dict(value='погода'))
            calendar=await registry.call('calendar__echo',dict(value='календарь'))
            assert weather==dict(source='weather',value='погода')
            assert calendar==dict(source='calendar',value='календарь')
            assert registry.entries['weather__echo'].session is not registry.entries['calendar__echo'].session
        finally:await registry.close()
    asyncio.run(scenario())
