import asyncio
from datetime import datetime, timedelta, timezone
import json
import pytest
from backend.config import SKILL_PATH, Settings
from backend.store import Store
from backend.scheduling import validate_schedule, next_run, parse
from backend.weather import validate_weather
from backend.mcp_client import connect,call
from backend.mcp_service import make_server
from backend.agent import AgentRuntime
from backend.runner import run_due
from scripts.cron import rewrite, cron_line, MARKER

NOW = datetime(2026,9,26,12,0,tzinfo=timezone.utc)
PAYLOAD = {"temperature":8,"humidity":65,"wind_speed":12,"weather_code":3,"observed_at":NOW.isoformat(),"fetched_at":NOW.isoformat()}

@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'weather.sqlite3')


def job(store,minutes=1,request_id='request',now=NOW,chat_token=None):
    city = store.register_cities([{"label":"Омск, Россия","latitude":54.99,"longitude":73.37,"country":"Россия","timezone":"Asia/Omsk"}])[0]
    return store.create_job(city['city_id'],'Собирай погоду в Омске','interval',minutes,None,'Asia/Omsk',SKILL_PATH.read_text(),request_id,now=now,chat_token=chat_token)


@pytest.mark.parametrize('minutes',[0,1441,True,1.2,'5'])
def test_invalid_interval(minutes):
    with pytest.raises(ValueError):
        validate_schedule('interval',minutes,None,'Asia/Omsk')


@pytest.mark.parametrize('expression',['* * * * * *','H * * * *','R * * * *','0 0 L * *','0 0 * * 1#2','@daily','* * * * MON','61 * * * *','* * * * *; curl x'])
def test_invalid_cron(expression):
    with pytest.raises(ValueError):
        validate_schedule('cron',None,expression,'Asia/Omsk')


def test_timezone_and_future_grid():
    validate_schedule('cron',None,'0 20 * * *','Asia/Omsk')
    assert next_run({'schedule_type':'cron','cron_expression':'0 20 * * *','timezone':'Asia/Omsk'},NOW) == NOW + timedelta(hours=2)
    assert next_run({'schedule_type':'interval','interval_minutes':30},NOW + timedelta(hours=3,minutes=9),NOW) == NOW + timedelta(hours=3,minutes=30)
    with pytest.raises(ValueError):
        validate_schedule('interval',1,None,'does/not/exist')


def test_scope_idempotent_persistence(store):
    job(store)
    due=NOW+timedelta(minutes=1)
    scope=store.claim_due('fake',now=due)
    assert store.claim_due('fake',now=due) is None
    result=store.observation(scope,PAYLOAD,now=due)
    assert store.data()['count']==0
    with pytest.raises(ValueError):
        store.save_result(scope,'invented',now=due)
    assert store.save_result(scope,result['result_id'],now=due)['saved']
    store.save_result(scope,result['result_id'],now=due)
    assert store.finish(scope,now=due)
    reopened=Store(store.path)
    assert reopened.data()['count']==1
    assert reopened.state()['runs'][0]['status']=='success'
    assert parse(reopened.list_jobs()[0]['next_run_at']) > due


@pytest.mark.parametrize('status',['paused','cancelled'])
def test_status_revokes_claim(store,status):
    j=job(store)
    due=NOW+timedelta(minutes=1)
    scope=store.claim_due('fake',now=due)
    observation=store.observation(scope,PAYLOAD,now=due)
    store.set_status(j['id'],status,now=due)
    with pytest.raises(ValueError):
        store.save_result(scope,observation['result_id'],now=due)
    assert not store.finish(scope,now=due)
    assert store.data()['count']==0
    assert store.claim_due('fake',now=due+timedelta(days=1)) is None
    if status=='paused':
        resumed=store.set_status(j['id'],'active',now=due+timedelta(days=1))
        assert parse(resumed['next_run_at']) > due+timedelta(days=1)
    else:
        with pytest.raises(ValueError):
            store.set_status(j['id'],'active')


def test_lease_recovery_fences_old_writer(store):
    job(store)
    due=NOW+timedelta(minutes=1)
    old=store.claim_due('fake',now=due)
    result=store.observation(old,PAYLOAD,now=due)
    new_time=due+timedelta(seconds=181)
    new=store.claim_due('fake',now=new_time)
    assert new['run_id']==old['run_id'] and new['token']!=old['token']
    with pytest.raises(ValueError):
        store.save_result(old,result['result_id'],now=new_time)
    fresh=store.observation(new,PAYLOAD,now=new_time)
    store.save_result(new,fresh['result_id'],now=new_time)
    store.finish(new,now=new_time)
    assert store.data()['count']==1


def test_recover_saved_before_finish_without_new_sample(store):
    job(store)
    due=NOW+timedelta(minutes=1)
    scope=store.claim_due('fake',now=due)
    result=store.observation(scope,PAYLOAD,now=due)
    store.save_result(scope,result['result_id'],now=due)
    assert store.claim_due('fake',now=due+timedelta(seconds=181)) is None
    assert store.data()['count']==1
    assert store.state()['runs'][0]['status']=='success'


def test_failure_advances_and_coalesces(store):
    job(store)
    late=NOW+timedelta(days=2,seconds=13)
    scope=store.claim_due('fake',now=late)
    assert not store.finish(scope,now=late)
    state=store.state()
    assert state['runs'][0]['error']=='Агент завершился без сохранённого наблюдения'
    assert parse(state['jobs'][0]['next_run_at']) > late
    assert store.claim_due('fake',now=late) is None
    assert state['total_samples']==0


def test_chat_idempotence_and_crash_recovery(store):
    initial=store.begin_chat('request','Оригинальное поручение',now=NOW)
    j=job(store,request_id='request',chat_token=initial['processing_token'])
    assert j['instruction']=='Оригинальное поручение'
    with pytest.raises(ValueError):
        store.begin_chat('request','Оригинальное поручение',now=NOW+timedelta(seconds=30))
    recovered=store.begin_chat('request','Оригинальное поручение',now=NOW+timedelta(seconds=121))
    assert 'Изменения сохранены' in recovered['answer']
    assert recovered==store.begin_chat('request','Оригинальное поручение')
    assert not store.finish_chat('request','stale',[],initial['processing_token'])
    assert len(store.state()['messages'])==2
    assert len(store.list_jobs())==1
    with pytest.raises(ValueError):
        store.begin_chat('request','Другое поручение')


def test_chat_lease_fences_old_completion(store):
    first=store.begin_chat('req','Hello',now=NOW)
    second=store.begin_chat('req','Hello',now=NOW+timedelta(seconds=121))
    assert not store.finish_chat('req','Old',[],first['processing_token'])
    assert store.finish_chat('req','New',[],second['processing_token'])
    assert store.begin_chat('req','Hello')['answer']=='New'


def test_chat_status_recovery_does_not_retoggle(store):
    j=job(store)
    started=store.begin_chat('pause','Переключи статус',now=NOW)
    store.set_status(j['id'],'paused',request_id='pause',chat_token=started['processing_token'],now=NOW)
    result=store.begin_chat('pause','Переключи статус',now=NOW+timedelta(seconds=121))
    assert result['trace'][0]['result']['status']=='paused'
    assert store.list_jobs()[0]['status']=='paused'


def test_weather_units_ranges_age():
    body={"utc_offset_seconds":0,"current_units":{"temperature_2m":"°C","relative_humidity_2m":"%","wind_speed_10m":"km/h"},"current":{"temperature_2m":8,"relative_humidity_2m":65,"wind_speed_10m":12,"weather_code":3,"time":NOW.replace(tzinfo=None).isoformat()}}
    assert validate_weather(body,NOW)==PAYLOAD
    body['current']['temperature_2m']=float('nan')
    with pytest.raises(ValueError):
        validate_weather(body,NOW)
    body['current']['temperature_2m']=8
    with pytest.raises(ValueError):
        validate_weather(body,NOW+timedelta(days=1))
    body['current_units']['wind_speed_10m']='m/s'
    with pytest.raises(ValueError):
        validate_weather(body,NOW)


def test_real_mcp_subprocess_skill_and_scope(store):
    async def scenario():
        async with connect(store.path,request_id='mcp') as session:
            assert 'create_weather_schedule' in [t.name for t in (await session.list_tools()).tools]
            with pytest.raises(ValueError,match='Сначала'):
                await call(session,'set_schedule_status',{'job_id':'missing','status':'paused'})
            assert (await call(session,'load_skill',{'name':'periodic-weather-collection'}))['content']==SKILL_PATH.read_text()
        job(store,now=datetime.now(timezone.utc)-timedelta(minutes=2))
        scope=store.claim_due('fake')
        result=store.observation(scope,PAYLOAD)
        async with connect(store.path,scope) as session:
            assert {t.name for t in (await session.list_tools()).tools}=={'get_current_weather','save_weather_result'}
            with pytest.raises(ValueError):
                await call(session,'save_weather_result',{'result_id':'invented'})
            assert (await call(session,'save_weather_result',{'result_id':result['result_id']}))['saved']
        assert store.finish(scope)
        assert store.data()['count']==1
    asyncio.run(scenario())


def test_mcp_weather_to_save_through_server(store,monkeypatch):
    from backend import weather
    job(store,now=datetime.now(timezone.utc)-timedelta(minutes=2))
    scope=store.claim_due('fake')
    async def provider(job):
        assert job['label']=='Омск, Россия'
        return PAYLOAD
    monkeypatch.setattr(weather,'current_weather',provider)
    async def scenario():
        server=make_server(store,scope)
        output=await server.call_tool('get_current_weather',{})
        if isinstance(output,tuple):
            observation=output[1]
        elif isinstance(output,dict):
            observation=output
        else:
            observation=json.loads(output[0].text)
        await server.call_tool('save_weather_result',{'result_id':observation['result_id']})
    asyncio.run(scenario())
    assert store.finish(scope)
    assert store.data()['count']==1


class FakeModel:
    def __init__(self,answers):
        self.answers=list(answers)
        self.calls=[]
    async def complete(self,messages,tools):
        self.calls.append((list(messages),tools))
        return self.answers.pop(0)


def tool(name,args,id='call-1'):
    return {'role':'assistant','tool_calls':[{'id':id,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}


def test_runtime_skill_create_uses_real_mcp(store):
    city=store.register_cities([{'label':'Омск','latitude':55.,'longitude':73.,'country':'Россия'}])[0]
    model=FakeModel([tool('load_skill',{'name':'periodic-weather-collection'}),tool('create_weather_schedule',{'city_id':city['city_id'],'instruction':'Собирай погоду','schedule_type':'interval','interval_minutes':30,'timezone':'Asia/Omsk'}),{'role':'assistant','content':'Расписание сохранено'}])
    config=Settings(store.path,'test','fake','Asia/Omsk')
    result=asyncio.run(AgentRuntime(store,model,config).run('Каждые полчаса Омск',request_id='runtime'))
    assert 'error' not in result
    assert [row['tool'] for row in result['trace']]==['load_skill','create_weather_schedule']
    assert len(store.list_jobs())==1
    assert model.calls[1][0][-1]['tool_call_id']=='call-1'


def test_scheduled_allowlist_and_no_fabricated_success(store):
    job(store,now=datetime.now(timezone.utc)-timedelta(minutes=2))
    config=Settings(store.path,'test','fake','Asia/Omsk')
    model=FakeModel([tool('create_weather_schedule',{}),{'role':'assistant','content':'Готово'}])
    runtime=AgentRuntime(store,model,config)
    result=asyncio.run(run_due(store,runtime))
    assert result['results'][0]['status']=='error'
    assert result['results'][0]['answer']=='Нет подтверждённого сохранённого наблюдения'
    assert {t['function']['name'] for t in model.calls[0][1]}=={'get_current_weather','save_weather_result'}
    assert SKILL_PATH.read_text() in model.calls[0][0][0]['content']
    assert store.data()['count']==0


def test_idle_does_not_call_model(store):
    model=FakeModel([])
    runtime=AgentRuntime(store,model,Settings(store.path,'test','fake','Asia/Omsk'))
    result=asyncio.run(run_due(store,runtime))
    assert result['executed']==0 and not model.calls
    assert store.state()['scheduler']['last_tick']


def test_missing_key_no_fake_reply(store):
    runtime=AgentRuntime(store,config=Settings(store.path,'','deepseek-flash','Asia/Omsk'))
    result=asyncio.run(runtime.run('Собирай погоду'))
    assert 'DeepSeek не подключён' in result['error']
    assert '.env' not in result['error']
    assert not store.list_jobs()


def test_cron_preserves_unrelated_and_removes_only_owned():
    other='0 4 * * * /bin/echo unrelated'
    current=other+'\n'+cron_line()+'\n'
    assert rewrite(current,False)==other+'\n'
    assert rewrite(current,True).count(MARKER)==1
    assert rewrite(current,True).startswith(other+'\n')


def test_chat_recovery_fences_mutations(store):
    j=job(store)
    old=store.begin_chat('change','Продолжи',now=NOW)
    new=store.begin_chat('change','Продолжи',now=NOW+timedelta(seconds=121))
    with pytest.raises(ValueError,match='устаревшее'):
        store.set_status(j['id'],'paused',request_id='change',chat_token=old['processing_token'],now=NOW+timedelta(seconds=122))
    assert store.set_status(j['id'],'paused',request_id='change',chat_token=new['processing_token'],now=NOW+timedelta(seconds=122))['status']=='paused'
    assert store.list_jobs()[0]['status']=='paused'


def test_cron_remove_without_environment(tmp_path,monkeypatch):
    import scripts.cron as cron
    monkeypatch.setattr(cron,'ROOT',tmp_path/'missing')
    monkeypatch.setattr(cron,'current_crontab',lambda: '0 4 * * * unrelated\n')
    monkeypatch.setattr('sys.argv',['cron.py','--remove'])
    calls=[]
    def execute(args,**kwargs):
        calls.append((args,kwargs))
        return type('Result',(),{'returncode':0})()
    monkeypatch.setattr(cron.subprocess,'run',execute)
    cron.main()
    assert calls[0][1]['input']=='0 4 * * * unrelated\n'
    assert not (tmp_path/'missing').exists()


def inprocess_connector(store,monkeypatch):
    """Keep the actual registered MCP tools; fake only the transport for source stubs."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from mcp.types import CallToolResult, TextContent
    import backend.agent as agent
    @asynccontextmanager
    async def connector(path,scope=None,request_id=None,chat_token=None):
        server=make_server(store,scope,request_id,chat_token)
        class Session:
            async def list_tools(self):
                return SimpleNamespace(tools=await server.list_tools())
            async def call_tool(self,name,arguments):
                try:
                    output=await server.call_tool(name,arguments)
                    if isinstance(output,tuple):
                        return CallToolResult(content=list(output[0]),structuredContent=output[1])
                    if isinstance(output,dict):
                        return CallToolResult(content=[TextContent(type='text',text=json.dumps(output))],structuredContent=output)
                    return CallToolResult(content=list(output))
                except Exception as exc:
                    return CallToolResult(isError=True,content=[TextContent(type='text',text=str(exc))])
        yield Session()
    monkeypatch.setattr(agent,'connect',connector)


def test_full_chat_skill_resolve_create(store,monkeypatch):
    from backend import weather
    async def resolve(query):
        assert query=='Омск'
        return [{'label':'Омск, Россия','latitude':55.,'longitude':73.,'country':'Россия','timezone':'Asia/Omsk'}]
    monkeypatch.setattr(weather,'resolve',resolve)
    inprocess_connector(store,monkeypatch)
    class Model:
        step=0
        async def complete(self,messages,tools):
            self.step+=1
            if self.step==1:
                return tool('load_skill',{'name':'periodic-weather-collection'})
            if self.step==2:
                return tool('resolve_city',{'query':'Омск'})
            if self.step==3:
                city=json.loads(messages[-1]['content'])['candidates'][0]
                return tool('create_weather_schedule',{'city_id':city['city_id'],'instruction':'Переписанное поручение','schedule_type':'interval','interval_minutes':30,'timezone':'Asia/Omsk'})
            return {'role':'assistant','content':'Расписание сохранено'}
    begin=store.begin_chat('full','Каждые полчаса собирай погоду в Омске')
    result=asyncio.run(AgentRuntime(store,Model(),Settings(store.path,'test','fake','Asia/Omsk')).run('Каждые полчаса собирай погоду в Омске',request_id='full',chat_token=begin['processing_token']))
    assert 'error' not in result
    assert [r['tool'] for r in result['trace']]==['load_skill','resolve_city','create_weather_schedule']
    assert store.list_jobs()[0]['instruction']=='Каждые полчаса собирай погоду в Омске'


def test_full_scheduled_model_weather_save(store,monkeypatch):
    from backend import weather
    job(store,now=datetime.now(timezone.utc)-timedelta(minutes=2))
    async def provider(job):
        assert job['latitude']==54.99 and job['longitude']==73.37
        return PAYLOAD
    monkeypatch.setattr(weather,'current_weather',provider)
    inprocess_connector(store,monkeypatch)
    class Model:
        step=0
        async def complete(self,messages,tools):
            self.step+=1
            if self.step==1:
                return tool('get_current_weather',{})
            if self.step==2:
                real=json.loads(messages[-1]['content'])
                return tool('save_weather_result',{'result_id':real['result_id']})
            return {'role':'assistant','content':'Погода сохранена'}
    result=asyncio.run(run_due(store,AgentRuntime(store,Model(),Settings(store.path,'test','fake','Asia/Omsk'))))
    assert result['results'][0]['status']=='success'
    assert result['collected']['count']==1
    assert [step['tool'] for step in store.state()['runs'][0]['trace']]==['get_current_weather','save_weather_result']


def test_failed_weather_not_collected(store,monkeypatch):
    from backend import weather
    job(store,now=datetime.now(timezone.utc)-timedelta(minutes=2))
    async def provider(job):
        raise ValueError('Источник погоды недоступен; наблюдение не сохранено')
    monkeypatch.setattr(weather,'current_weather',provider)
    inprocess_connector(store,monkeypatch)
    model=FakeModel([tool('get_current_weather',{}),{'role':'assistant','content':'Источник недоступен'}])
    result=asyncio.run(run_due(store,AgentRuntime(store,model,Settings(store.path,'test','fake','Asia/Omsk'))))
    assert result['results'][0]['status']=='error'
    assert store.data()['count']==0
    assert store.state()['runs'][0]['trace'][0]['ok'] is False


def test_http_api_readonly_chat_error_and_real_mcp_status(store,monkeypatch):
    from fastapi.testclient import TestClient
    from backend import main
    monkeypatch.setattr(main,'get_store',lambda:store)
    monkeypatch.setattr(main,'settings',lambda:Settings(store.path,'','fake','Asia/Omsk'))
    monkeypatch.setattr(main,'cron_status',lambda:{'installed':False,'cron_line':cron_line()})
    monkeypatch.setattr(main,'AgentRuntime',lambda store:AgentRuntime(store,config=Settings(store.path,'','fake','Asia/Omsk')))
    j=job(store)
    with TestClient(main.app) as client:
        result=client.get('/api/state').json()
        assert result['config']['configured'] is False
        assert result['scheduler']['last_tick'] is None
        assert result['scheduler']['installed'] is False
        assert client.get('/api/skill').json()['content']==SKILL_PATH.read_text()
        assert client.post('/api/chat',json={'prompt':'   ','request_id':'ff3651b2-1041-4fc7-8e42-3e4ee789c9ad'}).status_code==422
        body={'prompt':'Собирай погоду','request_id':'ff3651b2-1041-4fc7-8e42-3e4ee789c9ad'}
        first=client.post('/api/chat',json=body)
        assert first.status_code==200 and 'DeepSeek не подключён' in first.json()['answer']
        second=client.post('/api/chat',json=body)
        assert second.status_code==200 and second.json()['answer']==first.json()['answer']
        assert len(second.json()['state']['messages'])==2
        status=client.post(f'/api/jobs/{j["id"]}/status',json={'status':'paused'})
        assert status.status_code==200 and status.json()['state']['jobs'][0]['status']=='paused'
        assert client.get('/api/state').json()['scheduler']['last_tick'] is None


def test_chat_recovery_reports_latest_action(store):
    j=job(store)
    started=store.begin_chat('change-twice','Сначала приостанови, потом продолжи',now=NOW)
    store.set_status(j['id'],'paused',request_id='change-twice',chat_token=started['processing_token'],now=NOW)
    final=store.set_status(j['id'],'active',request_id='change-twice',chat_token=started['processing_token'],now=NOW+timedelta(seconds=1))
    recovered=store.begin_chat('change-twice','Сначала приостанови, потом продолжи',now=NOW+timedelta(seconds=121))
    assert recovered['trace'][0]['result']['status']=='active'
    assert recovered['trace'][0]['result']['next_run_at']==final['next_run_at']
    assert store.list_jobs()[0]['status']=='active'
    assert len(recovered['trace'])==1
