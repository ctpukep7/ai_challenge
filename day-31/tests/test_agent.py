import asyncio
import copy
import json
import re
import sys
from pathlib import Path
import pytest

DAY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DAY))
from backend.agent import Agent
from backend.mcp_session import ProjectSession
from backend import project_tools as p
from backend.provider import DeepSeek
from assistant_core import ProviderError

TOOLS = [{'type': 'function', 'function': {'name': 'git_branch', 'description': 'branch', 'parameters': {'type': 'object', 'properties': {}}}}]
def tool(name='git_branch', args='{}', cid='call1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': cid, 'type': 'function', 'function': {'name': name, 'arguments': args}}]}
def final(sid='e1', quote='main'):
    return {'role': 'assistant', 'content': json.dumps({'status': 'answered', 'claims': [{'text': 'Текущая ветка main', 'source_id': sid, 'quote': quote}]})}
class Session:
    calls = []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def discover(self): return TOOLS
    async def call(self, name, args):
        self.calls.append((name,args))
        return {'branch': 'main', 'sha': 'a'*40, 'detached': False, 'evidence': [p.evidence('git', 'main')]}
class Provider:
    def __init__(self, messages, usage=None): self.responses=iter(messages); self.messages=[]; self.usage=usage
    async def complete(self, messages, tools):
        self.messages.append(copy.deepcopy(messages))
        value=next(self.responses)
        if isinstance(value, Exception): raise value
        return {'message': value, 'usage': self.usage}
def run(provider, session=Session): return asyncio.run(Agent(provider, session).run('Какая ветка?', []))

def test_protocol_original_calls_ledger_and_usage():
    request=tool()
    provider=Provider([request, final()], {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15})
    result=run(provider)
    assert result['status']=='answered'
    assert provider.messages[1][-2]==request
    assert provider.messages[1][-1]['tool_call_id']=='call1'
    assert result['sources'][0]['kind']=='git'
    assert result['metrics']['total_tokens']==30 and result['metrics']['tool_calls']==1
    assert result['trace'][0]['method']=='tools/list'

@pytest.mark.parametrize('name,args', [('unknown','{}'),('git_branch','{'),('git_branch','[]'),('git_branch','{"extra":1}')])
def test_bad_request_not_executed_but_protocol_paired(name,args):
    Session.calls=[]
    provider=Provider([tool(name,args),{'role':'assistant','content':'{"status":"unknown","clarification":"Нужны данные"}'}])
    result=run(provider)
    assert result['status']=='unknown' and not Session.calls
    assert result['trace'][1]['method']=='tool_request_rejected'
    assert result['metrics']['tool_calls']==0 and result['metrics']['tool_requests']==1
    assert provider.messages[1][-1]['tool_call_id']=='call1'

def test_no_regex_branch_shortcut():
    provider=Provider([{'role':'assistant','content':'{"status":"unknown","clarification":"Уточните"}'}])
    result=run(provider)
    assert result['status']=='unknown' and result['metrics']['tool_calls']==0

def test_fabricated_evidence_rejected():
    result=run(Provider([tool(),final('forged'),final('forged')]))
    assert result['status']=='unknown' and result['sources']==[]
    result=run(Provider([tool(),final(quote='other'),final(quote='other')]))
    assert result['status']=='unknown' and result['validation']['omitted_claims']==1

def test_provider_error_retains_trace_usage_unknown():
    result=run(Provider([tool(), ProviderError('unavailable')]))
    assert result['status']=='error' and result['metrics']['llm_calls']==2
    assert result['metrics']['total_tokens'] is None and len(result['trace'])==2
    assert result['metrics']['llm'][-1]['error']

def test_zero_usage_is_real_zero():
    r=run(Provider([tool(),final()],dict(prompt_tokens=0,completion_tokens=0,total_tokens=0)))
    assert r['metrics']['total_tokens']==0

def test_partial_tool_result_is_visible_without_model_disclosure():
    class Partial(Session):
        async def call(self,*args):
            return {'partial':True,'evidence':[p.evidence('git','main')]}
    result=run(Provider([tool(),final()]),Partial)
    assert result['status']=='answered'
    assert any('только часть данных' in warning for warning in result['warnings'])

def test_rounds_and_duplicate_ids():
    result=run(Provider([tool(cid=f'c{i}') for i in range(6)]))
    assert result['status']=='error' and result['metrics']['llm_calls']==6
    result=run(Provider([tool(), tool()]))
    assert result['status']=='error' and result['metrics']['tool_calls']==1

def test_tool_request_cap():
    requests=[]
    for i in range(4):
        message=tool(cid=f'c{i}a'); message['tool_calls'] += [tool(cid=f'c{i}b')['tool_calls'][0], tool(cid=f'c{i}c')['tool_calls'][0]]
        requests.append(message)
    r=run(Provider(requests))
    assert r['status']=='error' and r['metrics']['tool_calls']==9

def test_context_cap_and_unusable_evidence():
    class Huge(Session):
        async def call(self, *args): return {'evidence':[p.evidence('code','x'*90_000)]}
    r=run(Provider([tool(), final(quote='x'), final(quote='x')]), Huge)
    assert r['status']=='unknown' and r['metrics']['evidence_bytes']<=80_000
    assert not r['trace'][1]['result']['evidence']

@pytest.mark.parametrize('path',['.env','day-31/.env.example','../README.md','/tmp/a','final week.md','wiki/topics/a.md','day-31/data/a.py','day-31/frontend/node_modules/a.js','day-31/backend/private_key.py'])
def test_private_policy(path): assert not p.allowed(path)

def test_public_source_read_and_symlink(tmp_path):
    (tmp_path/'day-31/backend').mkdir(parents=True)
    source=tmp_path/'day-31/backend/a.py'; source.write_text('one\ntwo\nthree\n')
    result=p.read_project_file('day-31/backend/a.py',2,3,root=tmp_path)
    assert result['evidence'][0]['text']=='two\nthree\n' and result['evidence'][0]['start_line']==2
    source.unlink(); source.symlink_to('/etc/passwd')
    with pytest.raises(p.PublicError): p.read_project_file('day-31/backend/a.py',root=tmp_path)

def test_secret_literals_and_code_expressions():
    for path in ['day-31/backend/project_tools.py','packages/assistant_core/src/assistant_core/provider.py']:
        assert p.read_public(path)
    with pytest.raises(p.PublicError): p.safe_text(('API_KEY = "'+'aB1234'*8+'"').encode())
    with pytest.raises(p.PublicError): p.safe_text(('sk-'+'x'*40).encode())
    assert p.safe_text(b"api_key = os.getenv('API_KEY', '')")

def test_diff_prefilter_and_untracked(tmp_path,monkeypatch):
    (tmp_path/'day-31/backend').mkdir(parents=True)
    (tmp_path/'day-31/backend/new.py').write_text('print("hello")\n')
    reads=[]
    def git(*args,**kwargs):
        if args[0]=='diff':
            assert '--name-only' in args, 'Private patch must never be requested'
            return 'final week.md\0.env\0'
        return 'day-31/backend/new.py\0'
    monkeypatch.setattr(p,'git',git)
    original=p.read_public
    def reader(path,*args): reads.append(path); return original(path,*args)
    monkeypatch.setattr(p,'read_public',reader)
    result=p.git_diff(root=tmp_path)
    assert reads==['day-31/backend/new.py','day-31/backend/new.py']
    assert result['omitted_files']==2
    assert result['evidence'][1]['section']=='untracked snapshot'

def test_real_mcp_catalog_branch_read_diff():
    async def check():
        async with ProjectSession() as session:
            names={x['function']['name'] for x in await session.discover()}
            assert names=={'git_branch','list_project_files','read_project_file','git_diff','search_documentation'}
            branch=await session.call('git_branch',{})
            assert re.fullmatch('[0-9a-f]{40}',branch['sha'])
            source=await session.call('read_project_file',{'path':'packages/assistant_core/src/assistant_core/provider.py','start_line':1,'end_line':20})
            assert source['evidence'][0]['kind']=='code'
            listing=await session.call('list_project_files',{'prefix':'day-31/backend/'})
            assert 'day-31/backend/agent.py' in listing['files']
            forbidden=await session.call('read_project_file',{'path':'.env'})
            assert forbidden.get('error') and not forbidden['evidence']
            diff=await session.call('git_diff',{'path':'day-31/backend/agent.py'})
            assert 'public_changed_total' in diff and 'shown_patches' in diff
            assert all('.env' not in item['path'] for item in diff['public_changed_files'])
    asyncio.run(check())

def test_provider_tool_catalog_and_raw_calls(monkeypatch):
    import httpx
    sent=[]
    class Client:
        def __init__(self,**kwargs): assert kwargs['trust_env'] is False
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def post(self,url,**kwargs):
            sent.append(kwargs['json'])
            return httpx.Response(200,request=httpx.Request('POST',url),json={'choices':[{'finish_reason':'tool_calls','message':tool()}], 'usage': {'total_tokens':0}})
    monkeypatch.setattr(httpx,'AsyncClient',Client)
    response=asyncio.run(DeepSeek(api_key='test-only').complete([{'role':'user','content':'question'}],TOOLS))
    assert response['message']==tool() and sent[0]['tools']==TOOLS
    assert sent[0]['thinking']=={'type':'disabled'}
