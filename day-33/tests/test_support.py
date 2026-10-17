import asyncio, importlib.util, json, re
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from assistant_core import Index, ProviderError
from assistant_core.mcp_client import call_tools
DAY=Path(__file__).resolve().parents[1]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path); m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
appmod=module('day33_app',DAY/'backend/app.py')
server=module('day33_mcp',DAY/'backend/mcp_server.py')
class Embed:
    def offsets(self,text):return [(m.start(),m.end()) for m in re.finditer(r'\S+',text)]
    def embed(self,texts):return [[1.,0.] for _ in texts]
@pytest.fixture
def index(tmp_path):
    (tmp_path/'faq.md').write_text('HTTP401 требует действующего токена. Не передавайте секреты.')
    idx=Index(tmp_path/'index.sqlite',tmp_path,['faq.md'],Embed());idx.build();return idx
class Provider:
    def __init__(self):self.payloads=[]
    async def complete(self,messages):
        p=json.loads(messages[-1]['content']);self.payloads.append(p);s=p['sources'][0]
        return {'status':'answered','claims':[{'source_id':s['id'],'quote':s['text'],'text':'Проверьте действующий токен.'}]}
async def fake_tools(path,calls):
    data={};trace=[]
    for name,args in calls:
        data[name]=getattr(server,name)(**args);trace.append({'method':'tools/call','tool':name,'arguments':args,'result':data[name]})
    return data,trace

def test_corpus_explicit():
    assert len(appmod.SUPPORT_PATHS)==3
    assert all(p.startswith('docs/support/') for p in appmod.SUPPORT_PATHS)
@pytest.mark.parametrize('history',[
 [{'ticket_id':'TLS-102','role':'user','content':'private'},{'ticket_id':'TLS-102','role':'assistant','content':'other'}],
 [{'role':'user','content':'unscoped'},{'role':'assistant','content':'old'}],
 [{'ticket_id':'AUTH-101','role':'assistant','content':'bad'}]
])
def test_forged_cross_ticket_history_rejected(index,history):
    provider=Provider()
    with TestClient(appmod.create_app(index,provider,fake_tools)) as c:
        r=c.post('/api/support/ask',json={'ticket_id':'AUTH-101','question':'q','history':history})
        assert r.status_code==422
    assert not provider.payloads

def test_scoped_context_and_fresh_selection(index):
    provider=Provider()
    with TestClient(appmod.create_app(index,provider,fake_tools)) as c:
        tickets=c.get('/api/tickets').json();assert len(tickets['tickets'])==4
        for ticket in ['AUTH-101','TLS-102','NET-103','INFO-104']:
            r=c.post('/api/support/ask',json={'ticket_id':ticket,'question':'Почему не работает авторизация?'})
            assert r.status_code==200,r.text
            data=r.json();assert data['ticket_id']==ticket
            assert data['context']['ticket']['user_id']==data['context']['user']['id']
            if ticket=='INFO-104':
                assert data['status']=='unknown' and data['reason']=='missing_ticket_details'
                assert data['sources']==[] and len(provider.payloads)==3
                assert 'Уточните' in data['answer']
                continue
            assert data['sources'][0]['quote'] in data['sources'][0]['text']
            assert [t['tool'] for t in data['trace']]==['list_tickets','get_ticket','get_ticket_user']
            p=provider.payloads[-1];assert p['tool_context']['ticket']['id']==ticket
            assert len(p['tool_context'])==2
        assert c.post('/api/support/ask',json={'ticket_id':'NO-999','question':'q'}).status_code==404

def test_scoped_history_and_stale(index):
    with TestClient(appmod.create_app(index,Provider(),fake_tools)) as c:
        h=[{'ticket_id':'AUTH-101','role':'user','content':'q'},{'ticket_id':'AUTH-101','role':'assistant','content':'a'}]
        assert c.post('/api/support/ask',json={'ticket_id':'AUTH-101','question':'а дальше?','history':h}).status_code==200
        (index.root/'faq.md').write_text('changed')
        assert c.post('/api/support/ask',json={'ticket_id':'AUTH-101','question':'q'}).status_code==409
        assert c.post('/api/index/build').status_code==200

def test_provider_failure(index):
    class Broken:
        async def complete(self,messages):raise ProviderError('Unavailable')
    with TestClient(appmod.create_app(index,Broken(),fake_tools)) as c:
        assert c.post('/api/support/ask',json={'ticket_id':'AUTH-101','question':'q'}).status_code==502

def test_mcp_failure_and_forged_relationship(index):
    async def broken(*args):raise RuntimeError('MCP down')
    with TestClient(appmod.create_app(index,Provider(),broken)) as c:
        assert c.get('/api/tickets').status_code==503
        assert c.post('/api/support/ask',json={'ticket_id':'AUTH-101','question':'q'}).status_code==503
    async def wrong(path,calls):
        data,trace=await fake_tools(path,calls)
        if 'get_ticket_user' in data:data['get_ticket_user']['id']='other'
        return data,trace
    with TestClient(appmod.create_app(index,Provider(),wrong)) as c:
        assert c.post('/api/support/ask',json={'ticket_id':'AUTH-101','question':'q'}).status_code==503

def test_fixture_relationship_validation(tmp_path,monkeypatch):
    invalid=tmp_path/'fixture.json';invalid.write_text(json.dumps({'users':[],'tickets':[{'id':'x','user_id':'missing'}]}))
    monkeypatch.setattr(server,'FIXTURE',invalid)
    with pytest.raises(ValueError,match='relationships'):server.records()

def test_real_stdio_tools_and_scope():
    data,trace=asyncio.run(call_tools(DAY/'backend/mcp_server.py',[('list_tickets',{}),('get_ticket',{'ticket_id':'AUTH-101'}),('get_ticket_user',{'ticket_id':'AUTH-101'})]))
    assert set(trace[0]['tools'])=={'list_tickets','get_ticket','get_ticket_user'}
    assert data['get_ticket']['user_id']==data['get_ticket_user']['id']=='demo-01'
    assert len(data['list_tickets']['tickets'])==4
    with pytest.raises(Exception):asyncio.run(call_tools(DAY/'backend/mcp_server.py',[('get_ticket_user',{'user_id':'demo-02'})]))
    with pytest.raises(Exception):asyncio.run(call_tools(DAY/'backend/mcp_server.py',[('get_ticket',{'ticket_id':'NO-999'})]))
