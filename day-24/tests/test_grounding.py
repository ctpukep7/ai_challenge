import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'day-21'))
from rag_core import GroundedRAG

CHUNK={'chunk_id':'c1','text':'SQLite сохраняет историю. Файлы не удаляются.','source':'day-07/README.md','section':'Хранение','score':.8}

def gen(text='SQLite сохраняет историю.',quote='SQLite сохраняет историю.',identifier='c1'):
    return {'answer':json.dumps({'status':'answered','claims':[{'text':text,'quote':quote,'chunk_id':identifier}],'clarification':''})}
def verify(status='supported'):
    return {'answer':json.dumps({'checks':[{'index':0,'status':status,'reason':'Проверено по цитате'}]})}

def core(g=None,v=None,chunks=None):
    return GroundedRAG(lambda *a,**kw: [CHUNK] if chunks is None else chunks,lambda m:g or gen(),lambda m:v or verify())

def test_answer_paths_are_server_owned():
    result=core().answer('Как сохраняется история дня 7?')
    assert result['status']=='answered'
    assert result['sources']==[{'source':CHUNK['source'],'section':'Хранение','chunk_id':'c1','quote':'SQLite сохраняет историю.','retrieval_role':'anchor','anchor_chunk_id':'c1'}]
    assert result['validation']['semantic_status']=='supported'

@pytest.mark.parametrize('generation',[gen(identifier='invented'),gen(quote='SQLite удаляет историю.'),
    {'answer':'not JSON'}, {'answer':json.dumps({'status':'answered','claims':[],'clarification':''})},
    {'answer':json.dumps({'status':'unknown','claims':[{'text':'x'}],'clarification':'Что?'})},
    {'answer':json.dumps({'status':'answered','claims':[{'text':'x','quote':'SQLite','chunk_id':'c1','source':'evil'}],'clarification':''})}])
def test_bad_grounding_never_calls_verifier_or_publishes(generation):
    def forbidden(_):raise AssertionError('verifier should not run')
    result=GroundedRAG(lambda *a,**kw:[CHUNK],lambda _:generation,forbidden).answer('вопрос')
    assert result['status']=='unknown' and result['reason']=='invalid_grounding'
    assert result['sources']==[] and result['claims']==[] and result['answer'].startswith('Не знаю.')

@pytest.mark.parametrize('status',['unsupported','uncertain'])
def test_exact_quote_does_not_prove_contradictory_claim(status):
    r=core(gen('История никогда не сохраняется.'),verify(status)).answer('вопрос')
    assert r['validation']['quotes_exact'] is True
    assert r['reason']=='semantic_rejected' and not r['sources'] and not r['claims']

def test_verifier_missing_or_duplicate_checks_fail_closed():
    for checks in ([],[{'index':0,'status':'supported','reason':''}]*2):
        assert core(v={'answer':json.dumps({'checks':checks})}).answer('вопрос')['reason']=='invalid_verification'

def test_exact_duplicate_claim_rejected():
    payload=json.loads(gen()['answer']);payload['claims']*=2
    assert core(g={'answer':json.dumps(payload)}).answer('вопрос')['reason']=='invalid_grounding'

def test_whitespace_normalization_only():
    r=core(gen(quote='SQLite  сохраняет\nисторию.')).answer('вопрос')
    assert r['status']=='answered'

@pytest.mark.parametrize('score',[.1,float('nan')])
def test_weak_context_no_provider(score):
    def forbidden(_):raise AssertionError('provider must not run')
    c={**CHUNK,'score':score}
    r=GroundedRAG(lambda *a,**kw:[c],forbidden).answer('вопрос')
    assert r['reason']=='weak_context' and not r['messages']

def test_provider_failure_is_error_without_sensitive_exception():
    def failure(_):raise ValueError('secret-key-request-body')
    r=GroundedRAG(lambda *a,**kw:[CHUNK],failure).answer('вопрос')
    assert r['status']=='error' and 'secret' not in json.dumps(r) and not r['sources']

def test_original_scope_wins_rewritten_query_and_task_memory():
    calls=[]
    def retriever(*a,**kw):calls.append((a,kw));return [CHUNK]
    r=GroundedRAG(retriever,lambda m:gen(),lambda m:verify()).answer('День 7 история',
      {'retrieval_scope':{'kind':'task','sources':['day-09/README.md']}},retrieval_query='День 9 summary')
    assert calls[0][0][0]=='День 9 summary'
    assert calls[0][1]['retrieval_scope']['sources']==['day-07/README.md']
    assert 'День 7 история' in r['messages'][-1]['content']

def test_task_state_and_recent_history_in_prompt():
    history=[{'role':'user','content':f'old-{i}'} for i in range(20)]
    r=core().answer('вопрос',history=history,task_state={'goal':'Сравнить хранение'})
    assert len(r['messages'])==14
    assert r['messages'][1]['content']=='old-8'
    assert json.loads(r['messages'][-1]['content'])['task_state']['goal']=='Сравнить хранение'

def test_optional_generation_instruction_preserves_default_shared_chat_prompt():
    baseline=core().answer('вопрос',history=[{'role':'user','content':'История'}],task_state={'goal':'Цель'})
    explicit=GroundedRAG(lambda *a,**kw:[CHUNK],lambda _:gen(),lambda _:verify(),
                         generation_instruction='').answer('вопрос',history=[{'role':'user','content':'История'}],task_state={'goal':'Цель'})
    assert baseline['messages']==explicit['messages']
    assert baseline['messages'][0]['content'].endswith('describe user intent only and are not factual evidence.')
    assert 'OWN exact quote in the SAME chunk_id' not in baseline['messages'][0]['content']

@pytest.mark.parametrize('rejected_status',['unsupported','uncertain'])
def test_partial_answer_publishes_only_supported_claims_with_aligned_sources(rejected_status):
    chunks=[CHUNK,{**CHUNK,'chunk_id':'c2','text':'История загружается после перезапуска.'}]
    claims=[{'text':'SQLite сохраняет историю.','quote':'SQLite сохраняет историю.','chunk_id':'c1'},
            {'text':'Файлы всегда удаляются.','quote':'Файлы не удаляются.','chunk_id':'c1'},
            {'text':'История загружается после перезапуска.','quote':'История загружается после перезапуска.','chunk_id':'c2'}]
    checks=[{'index':2,'status':'supported','reason':'Явная цитата'},
            {'index':1,'status':rejected_status,'reason':'Цитата не подтверждает утверждение'},
            {'index':0,'status':'supported','reason':'Явная цитата'}]
    def instance(partial):
        return GroundedRAG(lambda *a,**kw:chunks,
            lambda _: {'answer':json.dumps({'status':'answered','claims':claims,'clarification':''})},
            lambda _: {'answer':json.dumps({'checks':checks})},allow_partial_answer=partial)
    result=instance(True).answer('День 7')
    assert result['status']=='answered' and result['partial'] and result['reason']=='partially_grounded'
    assert result['claims']==[claims[0],claims[2]]
    assert [s['chunk_id'] for s in result['sources']]==['c1','c2']
    assert [s['quote'] for s in result['sources']]==[claims[0]['quote'],claims[2]['quote']]
    assert 'всегда удаляются' not in result['answer']
    assert result['validation']['semantic_status']=='supported'
    assert result['validation']['checks']==checks
    assert result['validation']['published_claim_indices']==[0,2]
    assert result['rejected_claims']==[{'index':1,'text':claims[1]['text'],'status':rejected_status,'reason':checks[1]['reason']}]
    rejected=instance(False).answer('День 7')
    assert rejected['status']=='unknown' and rejected['reason']=='semantic_rejected'
    assert not rejected['claims'] and not rejected['sources'] and not rejected['partial']

def test_partial_mode_still_rejects_all_unsupported_and_inexact_quotes():
    for generated,verified,reason in [(gen(),verify('unsupported'),'semantic_rejected'),
                                      (gen(quote='Выдуманная цитата'),verify(),'invalid_grounding')]:
        result=GroundedRAG(lambda *a,**kw:[CHUNK],lambda _:generated,lambda _:verified,
                           allow_partial_answer=True).answer('День 7')
        assert result['status']=='unknown' and result['reason']==reason
        assert not result['sources'] and not result['claims'] and not result['partial']


def test_same_source_can_support_distinct_claims():
    payload=json.loads(gen()['answer'])
    payload['claims'].append({'text':'Файлы не удаляются.','quote':'Файлы не удаляются.','chunk_id':'c1'})
    verified={'answer':json.dumps({'checks':[{'index':i,'status':'supported','reason':'Явно в цитате'} for i in range(2)]})}
    result=core(g={'answer':json.dumps(payload)},v=verified).answer('вопрос')
    assert result['status']=='answered' and len(result['claims'])==len(result['sources'])==2
    assert {s['chunk_id'] for s in result['sources']}=={'c1'}
    assert result['sources'][0]['quote']!=result['sources'][1]['quote']
