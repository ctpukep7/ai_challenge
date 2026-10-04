import sys
import time
from pathlib import Path
from threading import Event
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
import backend.app as module
from backend.app import create_app
from test_grounding import CHUNK,gen,verify


def make(monkeypatch,llm=lambda _:gen(),verifier=lambda _:verify()):
    monkeypatch.setattr(module,'index_state',lambda *a,**kw:{'ready':True,'corpus_ready':True,'reason':None})
    return TestClient(create_app(retriever=lambda *a,**kw:[CHUNK],llm=llm,verifier=verifier))

def test_api_schema_and_source(monkeypatch):
    with make(monkeypatch) as client:
        assert client.post('/api/ask',json={'question':'  '}).status_code==422
        assert client.post('/api/ask',json={'question':'x','settings':{'top_k_before':2,'top_k_after':3}}).status_code==422
        result=client.post('/api/ask',json={'question':'Day 7'}).json()
        assert result['status']=='answered' and result['sources'][0]['source']==CHUNK['source']

def test_day24_records_specific_quote_instruction_in_actual_generation_prompt(monkeypatch):
    sent=[]
    def generation(messages):
        sent.append(messages)
        return gen()
    with make(monkeypatch,llm=generation) as client:
        result=client.post('/api/ask',json={'question':'Как в дне 7 сохраняется контекст диалога после перезапуска?'}).json()
    assert result['status']=='answered'
    assert result['messages']==sent[0]
    prompt=sent[0][0]['content']
    assert module.GENERATION_INSTRUCTION in prompt
    assert 'OWN exact quote in the SAME chunk_id' in prompt
    assert 'avoid excerpts starting mid-sentence' in prompt
    assert 'Split separate facts into separate claims' in prompt

def test_day24_enables_partial_mode(monkeypatch):
    with make(monkeypatch) as client:
        assert client.app.state.rag.allow_partial_answer is True

def test_stale_index_blocks_calls(monkeypatch):
    with make(monkeypatch,llm=lambda _:(_ for _ in ()).throw(AssertionError())) as client:
        monkeypatch.setattr(module,'index_state',lambda *a:{'ready':False,'reason':'hash mismatch'})
        assert client.post('/api/ask',json={'question':'x'}).status_code==409

def test_cancel_keeps_finished_request_and_stops_next(monkeypatch):
    entered,release=Event(),Event();calls=[]
    def slow(_):calls.append(1);entered.set();release.wait(2);return gen()
    with make(monkeypatch,llm=slow) as client:
        identifier=client.post('/api/benchmark',json={}).json()['id']
        assert entered.wait(2)
        assert client.post('/api/ask',json={'question':'x'}).status_code==409
        client.post(f'/api/runs/{identifier}/cancel')
        release.set()
        for _ in range(100):
            result=client.get(f'/api/runs/{identifier}').json()
            if result['status']=='cancelled':break
            time.sleep(.01)
        assert result['status']=='cancelled' and len(calls)==1
        assert result['summary']['answered']==1 and result['summary']['total']==1


def test_summary_denominators_count_unknown_separately():
    from backend.app import summarize
    r={'status':'unknown','sources':[],'validation':{'quotes_exact':False,'semantic_status':'not_run'}}
    summary=summarize([{'result':r}])
    assert summary['source_denominator']==0 and summary['all_questions_source_denominator']==1
    assert summary['unknown']==1 and summary['with_sources']==0
