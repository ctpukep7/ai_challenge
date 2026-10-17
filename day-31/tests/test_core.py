import asyncio, importlib.util, math, re, sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from assistant_core import Document, Index, load_documents, chunk_document, validate_answer, ProviderError
from assistant_core.mcp_client import call_tools
DAY=Path(__file__).resolve().parents[1]; ROOT=DAY.parent
sys.path.insert(0,str(DAY))
from backend.app import create_app
class Embed:
    max_tokens=126
    def offsets(self,text): return [(m.start(),m.end()) for m in re.finditer(r'\S+',text)]
    def embed(self,texts): return [[1.,float('other' in t)] for t in texts]
@pytest.fixture
def index(tmp_path):
    (tmp_path/'README.md').write_text('# Test\n'+' '.join('word'+str(i) for i in range(300)))
    return Index(tmp_path/'data/index.sqlite',tmp_path,['README.md'],Embed())
def test_atomic_and_stale(index):
    assert index.status()['ready'] is False
    index.build(); assert not index.status()['stale']
    old=index.path.read_bytes(); index.embedder.embed=lambda t: (_ for _ in ()).throw(ValueError('failure'))
    with pytest.raises(ValueError): index.build()
    assert index.path.read_bytes()==old
    (index.root/'README.md').write_text('changed')
    assert index.status()['stale']
    with pytest.raises(ValueError,match='устарел'): index.search('query')
def test_chunk_limits_and_lines():
    doc=Document('docs/a.md','# Heading\n'+'word\n'*350,'hash')
    chunks=chunk_document(doc,Embed())
    assert len(chunks)>2
    assert all(len(Embed().offsets(c.text))<=126 and c.end_line>=c.start_line for c in chunks)
    assert len({c.id for c in chunks})==len(chunks)
    assert chunks[1].section=='Heading'

def test_neighbors_recover_boundaries_without_fabricating_similarity(index):
    index.build()
    chunks=chunk_document(index.documents()[0],index.embedder)
    anchor={**chunks[1].__dict__,'score':.8}
    result=index.expand_neighbors([anchor])
    assert result[0]['score']==.8 and result[0]['retrieval_role']=='anchor'
    assert {s['id'] for s in result[1:]}=={chunks[0].id,chunks[2].id}
    assert all(s['path']==anchor['path'] and s['score'] is None and s['anchor_id']==anchor['id'] for s in result[1:])
@pytest.mark.parametrize('path',['.env','wiki/a.md','.memory-bank/a.md','../README.md','/tmp/a'])
def test_private_paths(tmp_path,path):
    with pytest.raises(ValueError): load_documents(tmp_path,[path])
def test_symlink(tmp_path):
    (tmp_path/'real.md').write_text('text'); (tmp_path/'README.md').symlink_to(tmp_path/'real.md')
    with pytest.raises(ValueError): load_documents(tmp_path,['README.md'])
@pytest.mark.parametrize('result',[[],{'status':'nonsense'},{'status':'unknown','clarification':[]},{'claims':'bad'},{'status':'answered','claims':[{'source_id':[], 'text':'x','quote':'actual'}]}, {'status':'answered','claims':[{'source_id':'x','text':'fact','quote':'invented'}]}])
def test_bad_provider(result):
    with pytest.raises(ProviderError): validate_answer(result,[{'id':'x','text':'actual'}])
def test_verified_sources():
    result=validate_answer({'status':'answered','claims':[{'source_id':'x','text':'fact','quote':'actual'}]},[{'id':'x','text':'actual text','path':'README.md'}])
    assert result['sources'][0]['quote']=='actual'
    assert result['status']=='answered'
class Provider:
    async def complete(self,messages,tools):
        return {'message':{'role':'assistant','content':'{"status":"unknown","clarification":"Не знаю"}'}}
class Session:
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass
    async def discover(self): return [{'type':'function','function':{'name':'git_branch','parameters':{'type':'object','properties':{}}}}]
def test_api_stale_and_history(index):
    index.build()
    with TestClient(create_app(index,Provider(),Session)) as client:
        assert client.post('/api/help',json={'question':'/help','history':[{'role':'assistant','content':'bad'}]}).status_code==422
        assert client.post('/api/help',json={'question':'/help'}).json()['status']=='unknown'
        (index.root/'README.md').write_text('changed')
        assert client.get('/api/index').json()['stale']
        # Fresh Git questions are no longer blocked by a stale documentation index.
        assert client.post('/api/help',json={'question':'Какая ветка?'}).status_code==200
        assert client.post('/api/index/build').status_code==200

def test_provider_error(index):
    class Broken:
        async def complete(self,messages,tools): raise ProviderError('provider unavailable')
    with TestClient(create_app(index,Broken(),Session)) as client:
        result=client.post('/api/help',json={'question':'question'})
        assert result.status_code==200 and result.json()['status']=='error'
        assert result.json()['metrics']['llm_calls']==1

def test_real_mcp():
    results,trace=asyncio.run(call_tools(DAY/'backend/mcp_server.py',[('git_branch',{}),('list_project_files',{})]))
    assert re.fullmatch(r'[a-f0-9]{40}',results['git_branch']['sha'])
    assert 'README.md' in results['list_project_files']['files']
    assert len(trace)==3 and trace[0]['method']=='tools/list'
    assert not any('.env' in p for p in results['list_project_files']['files'])

def test_deepseek_json_mode_disables_thinking_and_rejects_truncation(monkeypatch):
    import httpx
    from assistant_core import DeepSeek
    sent=[]
    class Client:
        def __init__(self,**kwargs):
            assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def post(self,url,**kwargs):
            sent.append(kwargs['json'])
            return httpx.Response(200,request=httpx.Request('POST',url),json={'choices':[{'finish_reason':'length','message':{'content':'{"claims":['}}]})
    monkeypatch.setattr(httpx,'AsyncClient',Client)
    with pytest.raises(ProviderError,match='обрезан'):
        asyncio.run(DeepSeek(api_key='test-only').complete([{'role':'user','content':'question'}]))
    assert sent[0]['thinking']=={'type':'disabled'}
    assert sent[0]['response_format']=={'type':'json_object'}

def test_source_scoped_search(index):
    index.build()
    assert index.search('query',source_paths=['other.md'])==[]
    assert index.search('query',source_paths=['README.md'])
