import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest
from fastapi.testclient import TestClient
from backend.app import create_app, core, SCENARIOS, Settings, TaskState, default_fitter, summarize
from backend.chat import retrieval_plan
from backend.store import Store, Conflict, EMPTY_TASK

class FakeRAG:
    def __init__(self): self.calls=[]
    def answer(self,question,**kwargs):
        self.calls.append((question,kwargs))
        scope=kwargs['settings']['retrieval_scope']
        source=(scope.get('sources') or ['README.md'])[0]
        return {'status':'answered','answer':'Подтверждённый ответ','sources':[{'source':source,'section':'Test','chunk_id':'id','quote':'Подтверждение'}],'retrieval_scope':scope,'validation':{'quotes_exact':True,'semantic_status':'supported'}}

@pytest.fixture
def fixture(tmp_path):
    rag=FakeRAG()
    app=create_app(chat_db=tmp_path/'chat.sqlite',rag=rag,query_fitter=lambda q,g:q+(' '+g if g else ''),index_reader=lambda *args:{'ready':True},pause_seconds=0)
    with TestClient(app) as client: yield client,rag,app

def new(client): return client.post('/api/sessions',json={'title':'Тест'}).json()['session']['id']
def send(client,sid,q,key='key'):
    return client.post(f'/api/sessions/{sid}/messages',json={'question':q,'request_id':key})

def test_persistent_isolated_history_and_task(fixture,tmp_path):
    c,rag,app=fixture; a,b=new(c),new(c)
    response=send(c,a,'Цель: Изучить память').json()
    assert response['result']['status']=='memory_updated'
    send(c,a,'Источники: 7','2')
    send(c,a,'Как работает история?','3')
    assert len(rag.calls)==1
    assert rag.calls[0][1]['task_state']['goal']=='Изучить память'
    assert rag.calls[0][1]['settings']['retrieval_scope']['sources']==['day-07/README.md']
    restarted=Store(tmp_path/'chat.sqlite')
    assert restarted.get(a)['task_state']['goal']=='Изучить память'
    assert len(restarted.get(a)['messages'])==3
    assert restarted.get(b)['task_state']==EMPTY_TASK
    assert restarted.get(b)['messages']==[]

def test_idempotency_payload_and_revision(fixture):
    c,rag,app=fixture; sid=new(c)
    first=send(c,sid,'Вопрос').json(); again=send(c,sid,'Вопрос').json()
    assert again['replayed'] and again['message']['id']==first['message']['id']
    assert len(rag.calls)==1 and again['revision']==first['revision']
    assert send(c,sid,'Другой вопрос').status_code==409
    task=TaskState(goal='Новая цель').model_dump()
    assert c.patch(f'/api/sessions/{sid}/task',json={'revision':0,'task_state':task}).status_code==409
    assert c.patch(f'/api/sessions/{sid}/task',json={'revision':first['revision'],'task_state':task}).status_code==200

def test_pending_parallel_and_late_answer(tmp_path):
    store=Store(tmp_path/'chat.sqlite'); sid=store.create('X')['session']['id']
    row,revision,_=store.reserve(sid,'A','1')
    with pytest.raises(Conflict): store.reserve(sid,'B','2')
    with pytest.raises(Conflict): store.update_task(sid,EMPTY_TASK,revision)
    with pytest.raises(Conflict): store.delete(sid)
    store.finish(sid,row['id'],revision,{'status':'answered','answer':'A','sources':[]})
    with pytest.raises(Conflict): store.finish(sid,row['id'],revision,{'status':'answered','answer':'Late'})
    assert len(store.get(sid)['messages'])==1

def test_restart_marks_pending_interrupted(tmp_path):
    path=tmp_path/'chat.sqlite'; store=Store(path); sid=store.create('X')['session']['id']
    row,revision,_=store.reserve(sid,'Question','1')
    restarted=Store(path); restarted.recover_pending(); message=restarted.get(sid)['messages'][0]
    assert message['status']=='interrupted' and message['result']['reason']=='interrupted'
    with pytest.raises(Conflict): store.finish(sid,row['id'],revision,{'status':'answered'})
    restarted.reserve(sid,'Retry','2')

def test_scope_changes_and_short_followup(fixture):
    c,rag,app=fixture; sid=new(c)
    send(c,sid,'Цель: История диалога','a');send(c,sid,'Источники: 7','b')
    send(c,sid,'Как хранится история?','c')
    send(c,sid,'Как работает summary в Day 9?','d')
    send(c,sid,'А при ошибке?','e')
    sources=[row[1]['settings']['retrieval_scope']['sources'] for row in rag.calls]
    assert sources==[['day-07/README.md'],['day-09/README.md'],['day-09/README.md']]
    assert 'Day 9' in rag.calls[-1][1]['retrieval_query']
    assert rag.calls[-1][0]=='А при ошибке?'
    assert c.get(f'/api/sessions/{sid}').json()['task_state']['source_days']==[7]
    send(c,sid,'Источники: 14','f');send(c,sid,'А теперь?','g')
    assert rag.calls[-1][1]['settings']['retrieval_scope']['sources']==['day-14/README.md']

def test_ambiguous_negation_not_forced_to_task_days():
    task=TaskState(goal='История',source_days=[7]).model_dump()
    q,scope=retrieval_plan('Не про Day 9, а другой вопрос',task,lambda q,g:q)
    assert scope['sources'] is None

def test_limits_invalid_memory_and_reset(fixture):
    c,rag,app=fixture; sid=new(c)
    assert send(c,sid,'Источники: не числа').json()['result']['status']=='error'
    assert send(c,sid,'Цель: '+'x'*1201,'2').json()['result']['status']=='error'
    assert send(c,sid,'  ','3').status_code==422
    assert send(c,sid,'Question','bad request id').status_code==422
    view=c.get(f'/api/sessions/{sid}').json()
    assert c.patch(f'/api/sessions/{sid}/task',json={'revision':view['revision'],'task_state':{'source_days':[True]}}).status_code==422
    valid=c.patch(f'/api/sessions/{sid}/task',json={'revision':view['revision'],'task_state':TaskState(goal='Goal').model_dump()}).json()
    reset=c.patch(f'/api/sessions/{sid}/task',json={'revision':valid['revision'],'task_state':EMPTY_TASK}).json()
    assert reset['task_state']==EMPTY_TASK

def test_history_last_twelve_full_storage(fixture):
    c,rag,app=fixture; sid=new(c)
    for n in range(9):send(c,sid,f'Вопрос {n}',str(n))
    assert len(rag.calls[-1][1]['history'])==12
    assert len(c.get(f'/api/sessions/{sid}').json()['messages'])==9
    assert rag.calls[-1][1]['history'][0]['content']=='Вопрос 2'

def test_delete_only_chat(tmp_path):
    index=tmp_path/'index.sqlite';index.write_bytes(b'do not delete')
    app=create_app(chat_db=tmp_path/'chat.sqlite',db=index,rag=FakeRAG(),query_fitter=lambda q,g:q,index_reader=lambda *a:{'ready':True})
    with TestClient(app) as c:
        sid=new(c); assert c.delete(f'/api/sessions/{sid}').status_code==200
        assert c.get(f'/api/sessions/{sid}').status_code==404
    assert index.read_bytes()==b'do not delete'

def test_provider_error_retained_and_retry(fixture):
    c,rag,app=fixture; sid=new(c)
    original=rag.answer
    rag.answer=lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('secret key'))
    failed=send(c,sid,'Вопрос').json()
    assert failed['result']['status']=='error' and 'secret key' not in json.dumps(failed)
    rag.answer=original
    assert send(c,sid,'Вопрос','retry').json()['result']['status']=='answered'

def test_scenarios_run_fresh_rag_goal_and_sources(fixture):
    c,rag,app=fixture
    def extract(messages):
        current=json.loads(messages[-1]['content'])['new_user_message']
        scenario=next((item for item in SCENARIOS if item['turns'][0]['question']==current),None)
        updates=[] if scenario is None else [{'field':'goal','action':'set','value':scenario['goal'],'evidence':current.split('.')[0]}]
        return {'answer':json.dumps({'updates':updates})}
    app.state.service.memory.llm=extract
    for scenario in SCENARIOS:
        response=c.post('/api/scenarios/run' ,json={'scenario_id':scenario['id']})
        assert response.status_code==202
        identifier=response.json()['id']
        for _ in range(100):
            job=c.get('/api/runs/'+identifier).json()
            if job['status'] not in {'running','queued'}:break
            time.sleep(.01)
        total=len(scenario['turns'])
        assert job['status']=='completed' and len(job['results'])==total
        summary=job['summary']
        assert summary['goal_preserved']==total
        assert summary['answered']==total and summary['memory_updates']==1
        assert summary['goal_available']==total
        assert summary['answered_with_sources']==summary['sources_denominator']==total
        assert summary['expected_source_denominator']==total and summary['unknown']==summary['errors']==0
        assert all(row['result']['retrieval_scope']['sources'] is None or row['expected_source'] in row['result']['retrieval_scope']['sources'] for row in job['results'])
        assert job['results'][0]['result']['retrieval_scope']['sources']==[scenario['turns'][0]['expected_source']]
        assert len(c.get('/api/sessions/'+job['session_id']).json()['messages'])==total
    assert len(rag.calls)==sum(len(item['turns']) for item in SCENARIOS)
    assert all(call[1]['task_state']['goal'] for call in rag.calls)
    assert all(len(call[1]['history'])<=12 for call in rag.calls)

def test_cancel_stops_before_next_turn(fixture):
    c,rag,app=fixture; started,release=Event(),Event()
    original=rag.answer
    def blocked(*a,**kw):started.set();release.wait(2);return original(*a,**kw)
    rag.answer=blocked
    identifier=c.post('/api/scenarios/run',json={'scenario_id':'history'}).json()['id']
    assert started.wait(2)
    c.post('/api/runs/'+identifier+'/cancel'); release.set()
    for _ in range(100):
        job=c.get('/api/runs/'+identifier).json()
        if job['status']=='cancelled':break
        time.sleep(.01)
    assert job['status']=='cancelled' and job['current']==1 and len(rag.calls)==1

def test_unknown_and_errors_not_counted_success():
    summary=summarize([{'result':{'status':'unknown','sources':[]}},{'result':{'status':'error','sources':[]}},{'result':{'status':'memory_updated','sources':[]}}])
    assert summary['answered_with_sources']==summary['sources_denominator']==0
    assert summary['unknown']==summary['errors']==summary['memory_updates']==1

def test_real_core_integrates_history_and_verbatim_source(tmp_path):
    retrievals=[];generated=[]
    def retrieve(q,strategy,k,**kw):
        retrievals.append((q,kw))
        return [{'chunk_id':'c1','source':'day-07/README.md','section':'История','text':'SQLite хранит сообщения диалога.','score':.9}]
    def llm(messages):
        generated.append(messages)
        return {'answer':json.dumps({'status':'answered','claims':[{'text':'Сообщения сохраняются в SQLite.','chunk_id':'c1','quote':'SQLite хранит сообщения диалога.'}],'clarification':''})}
    verifier=lambda m:{'answer':json.dumps({'checks':[{'index':0,'status':'supported','reason':'Цитата подтверждает'}]})}
    app=create_app(chat_db=tmp_path/'chat.sqlite',retriever=retrieve,llm=llm,verifier=verifier,query_fitter=lambda q,g:q+' '+g,index_reader=lambda *a:{'ready':True})
    with TestClient(app) as c:
        sid=new(c);send(c,sid,'Цель: Сохранить контекст','0');send(c,sid,'Источники: 7','1')
        first=send(c,sid,'Как хранится история?','2').json()['result'];second=send(c,sid,'А после перезапуска?','3').json()['result']
        assert first['status']==second['status']=='answered'
        assert second['sources'][0]['chunk_id']=='c1' and second['validation']['quotes_exact']
        assert len(retrievals)==len(generated)==2
        payload=json.loads(generated[-1][-1]['content'])
        assert payload['question']=='А после перезапуска?' and payload['task_state']['goal']=='Сохранить контекст'
        assert any(m['role']=='assistant' and m['content']=='Сообщения сохраняются в SQLite.' for m in generated[-1])

def test_simultaneous_http_turn_and_memory_conflicts(fixture):
    c,rag,app=fixture; sid=new(c);started,release=Event(),Event()
    original=rag.answer
    def blocked(*a,**kw):started.set();release.wait(2);return original(*a,**kw)
    rag.answer=blocked
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(send,c,sid,'Первый','a')
        assert started.wait(2)
        assert send(c,sid,'Другой','b').status_code==409
        assert send(c,sid,'Первый','a').status_code==409
        assert c.patch(f'/api/sessions/{sid}/task',json={'revision':0,'task_state':EMPTY_TASK}).status_code==409
        release.set();assert first.result().status_code==200
    assert len(rag.calls)==1

def test_request_id_binds_settings(fixture):
    c,rag,app=fixture; sid=new(c)
    send(c,sid,'Вопрос','a')
    response=c.post(f'/api/sessions/{sid}/messages',json={'question':'Вопрос','request_id':'a','settings':{'threshold':.1}})
    assert response.status_code==409 and len(rag.calls)==1

def test_failed_memory_command_does_not_break_next_question(fixture):
    c,rag,app=fixture; sid=new(c)
    assert send(c,sid,'Уточнение:','a').json()['result']['status']=='error'
    assert send(c,sid,'Как работает Day 7?','b').json()['result']['status']=='answered'
    assert send(c,sid,'А после перезапуска?','c').json()['result']['status']=='answered'

def test_query_fit_keeps_original_while_dropping_optional_goal(monkeypatch):
    from backend import app as app_module
    class Tokens:
        max_tokens=5
        def count(self,value):return len(value.split())
    monkeypatch.setattr(app_module.adapter,'cached_embedder',lambda *a:Tokens())
    assert default_fitter('Исходный вопрос','длинная цель с лишними словами')=='Исходный вопрос длинная цель с'
    with pytest.raises(ValueError,match='оригинал не обрезан'):
        default_fitter('слишком длинный исходный вопрос из многих слов','Цель')

def test_nonquestion_memory_turns_never_invoke_retrieval(fixture):
    c,rag,app=fixture; sid=new(c)
    for i,q in enumerate(['Цель: Разобраться','Уточнение: История','Ограничение: Только документация','Термин: tail означает хвост','Источники: 7,9']):
        assert send(c,sid,q,str(i)).json()['result']['status']=='memory_updated'
    assert not rag.calls

def test_explicit_task_source_edit_resets_conversational_referent(fixture):
    c,rag,app=fixture;sid=new(c)
    send(c,sid,'Что делает Day 9?','a')
    view=c.get(f'/api/sessions/{sid}').json()
    task={**view['task_state'],'source_days':[14]}
    c.patch(f'/api/sessions/{sid}/task',json={'revision':view['revision'],'task_state':task})
    result=send(c,sid,'А при нарушении?','b').json()['result']
    assert result['retrieval_scope']['sources']==['day-14/README.md']
    assert result['retrieval_scope']['reason']=='confirmed_task_memory'
    assert len(c.get(f'/api/sessions/{sid}').json()['messages'])==2

def test_own_adapter_adds_real_boundary_provenance_without_reranking(tmp_path,monkeypatch):
    import sqlite3
    from backend import day21_adapter as adapter
    path=tmp_path/'index.sqlite'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE chunks(strategy TEXT,source TEXT,section TEXT,chunk_id TEXT,ordinal INTEGER,text TEXT)')
        conn.executemany('INSERT INTO chunks VALUES(?,?,?,?,?,?)',[
          ('structure','day-07/README.md','История','prev',0,'Не сохраняет в'),
          ('structure','day-07/README.md','История','selected',1,'localStorage. Сообщения в SQLite.'),
          ('structure','day-07/README.md','Другой раздел','next-section',2,'Другой текст'),
          ('fixed','day-07/README.md','История','different-strategy',0,'Не подходящий сосед'),
          ('structure','day-09/README.md','История','other-file',2,'Не сосед из другого файла')])
    candidate={'source':'day-07/README.md','section':'История','chunk_id':'selected','text':'localStorage. Сообщения в SQLite.','score':.91}
    monkeypatch.setattr(adapter,'index_state',lambda *a:{'ready':True})
    monkeypatch.setattr(adapter,'cached_embedder',lambda *a:object())
    monkeypatch.setattr(adapter,'search',lambda *a,**kw:[dict(candidate)])
    result=adapter.retrieve('Вопрос','structure',12,db=path,cache_dir=tmp_path)
    assert len(result)==1
    assert {key:result[0][key] for key in candidate}==candidate
    context=result[0]['boundary_context']
    assert context['previous']['chunk_id']=='prev' and context['previous']['text']=='Не сохраняет в'
    assert context['previous']['source']=='day-07/README.md' and context['previous']['strategy']=='structure'
    assert context['next'] is None

def test_import_style_store_and_app_construction_preserve_active_request(tmp_path):
    path=tmp_path/'chat.sqlite'
    store=Store(path);sid=store.create('Активная сессия')['session']['id']
    message,revision,_=store.reserve(sid,'Долгий вопрос','running')
    # An unrelated test import/global app construction has no server lifespan.
    reader=Store(path)
    app=create_app(chat_db=path,rag=FakeRAG(),query_fitter=lambda q,g:q,index_reader=lambda *a:{'ready':True})
    assert reader.get(sid)['messages'][0]['status']=='pending'
    assert app.state.service.store.get(sid)['messages'][0]['status']=='pending'
    store.finish(sid,message['id'],revision,{'status':'answered','answer':'Ответ','sources':[]})
    assert store.get(sid)['messages'][0]['status']=='answered'

def test_real_lifespan_restart_recovery_and_exclusive_database(tmp_path):
    path=tmp_path/'chat.sqlite';store=Store(path)
    sid=store.create('Перезапуск')['session']['id']
    store.reserve(sid,'Старый незавершённый вопрос','old')
    make=lambda:create_app(chat_db=path,rag=FakeRAG(),query_fitter=lambda q,g:q,index_reader=lambda *a:{'ready':True})
    with TestClient(make()) as first:
        assert first.get('/api/sessions/'+sid).json()['messages'][0]['status']=='interrupted'
        active,revision,_=store.reserve(sid,'Новый активный запрос','active')
        second=make()
        assert store.get(sid)['messages'][-1]['status']=='pending'
        with pytest.raises(RuntimeError,match='другим сервером'):
            with TestClient(second):pass
        assert store.get(sid)['messages'][-1]['status']=='pending'
        store.finish(sid,active['id'],revision,{'status':'answered','answer':'Ответ','sources':[]})
    # The shutdown releases the lock; a genuine next startup can acquire it.
    with TestClient(make()) as restarted:
        assert restarted.get('/api/sessions/'+sid).json()['messages'][-1]['status']=='answered'
