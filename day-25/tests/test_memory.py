"""Automatic memory must be evidence-backed, atomic and idempotent."""
import json
import pytest
from fastapi.testclient import TestClient
from backend.app import create_app, TaskState
from backend.memory import apply_patch
from backend.store import Store, EMPTY_TASK

class RAG:
    def __init__(self): self.calls=[]; self.fail=False
    def answer(self,question,**kwargs):
        self.calls.append((question,kwargs))
        if self.fail: raise RuntimeError('provider secret')
        return {'status':'unknown','answer':'Не знаю','sources':[]}

class Memory:
    def __init__(self): self.calls=[]; self.updates=[]; self.fail=False
    def __call__(self,messages):
        self.calls.append(messages)
        if self.fail: raise RuntimeError('api secret')
        return {'answer':json.dumps({'updates':self.updates},ensure_ascii=False)}

def patch(field,action,value,evidence):
    return dict(field=field,action=action,value=value,evidence=evidence)

@pytest.fixture
def setup(tmp_path):
    rag,memory=RAG(),Memory()
    app=create_app(chat_db=tmp_path/'chat.sqlite',rag=rag,memory_llm=memory,
        query_fitter=lambda q,g:q+(' '+g if g else ''),index_reader=lambda *a:{'ready':True})
    with TestClient(app) as client:
        sid=client.post('/api/sessions',json={'title':'Личный'}).json()['session']['id']
        yield client,sid,rag,memory,tmp_path/'chat.sqlite'

def send(c,sid,text,key):
    response=c.post(f'/api/sessions/{sid}/messages',json={'question':text,'request_id':key})
    assert response.status_code==200
    return response.json()

def test_natural_goal_constraints_fresh_rag_persistence_and_replay(setup):
    c,sid,rag,memory,path=setup
    q='Хочу разобраться в истории Day 07. Объясняй кратко.'
    memory.updates=[patch('goal','set','Разобраться в истории Day 07','Хочу разобраться в истории Day 07.'),patch('constraints','add','Объясняй кратко','Объясняй кратко.')]
    first=send(c,sid,q,'1')
    assert first['result']['memory_update']['status']=='updated'
    assert first['task_state']['constraints']==['Объясняй кратко']
    assert rag.calls[0][1]['task_state']==first['task_state']
    assert rag.calls[0][1]['settings']['retrieval_scope']['sources']==['day-07/README.md']
    assert Store(path).get(sid)['task_state']==first['task_state']
    assert first['result']['status']=='unknown' # User memory is independent of answer success.
    replay=send(c,sid,q,'1')
    assert replay['replayed'] and len(memory.calls)==len(rag.calls)==1
    other=c.post('/api/sessions',json={'title':'Другой'}).json()['session']['id']
    assert c.get('/api/sessions/'+other).json()['task_state']==EMPTY_TASK


def test_correction_removes_old_constraint_and_persists_even_rag_error(setup):
    c,sid,rag,memory,path=setup
    memory.updates=[patch('constraints','add','Без кода','Без кода')]
    send(c,sid,'Без кода','1')
    memory.updates=[patch('constraints','remove','Без кода','Теперь можно с кодом'),patch('constraints','add','С примерами кода','Теперь можно с кодом')]
    rag.fail=True
    response=send(c,sid,'Теперь можно с кодом','2')
    assert response['result']['status']=='error'
    assert response['task_state']['constraints']==['С примерами кода']
    assert Store(path).get(sid)['task_state']['constraints']==['С примерами кода']
    assert 'provider secret' not in json.dumps(response)


def test_invalid_patch_is_all_or_nothing_then_rag_continues(setup):
    c,sid,rag,memory,path=setup
    memory.updates=[patch('goal','set','Разобраться','Хочу разобраться'),patch('terms','add','Ложное утверждение','Выдуманная цитата')]
    response=send(c,sid,'Хочу разобраться','1')
    assert response['task_state']==EMPTY_TASK
    assert response['result']['memory_update']['status']=='warning'
    assert rag.calls[0][1]['task_state']==EMPTY_TASK
    memory.fail=True
    next_turn=send(c,sid,'А после перезапуска?','2')
    assert next_turn['result']['memory_update']['status']=='warning'
    assert 'api secret' not in json.dumps(next_turn)
    assert len(rag.calls)==2


def test_no_assistant_facts_in_extractor_history_and_new_quote_required(setup):
    c,sid,rag,memory,path=setup
    send(c,sid,'Расскажи о памяти','1')
    memory.updates=[patch('goal','set','Расскажи о памяти','Расскажи о памяти')]
    second=send(c,sid,'Спасибо','2')
    assert second['result']['memory_update']['status']=='warning'
    payload=json.loads(memory.calls[-1][-1]['content'])
    assert payload['recent_user_messages']==['Расскажи о памяти']
    assert payload['new_user_message']=='Спасибо'
    assert 'Не знаю' not in json.dumps(payload,ensure_ascii=False)


def test_auto_source_change_resets_old_short_followup_referent(setup):
    c,sid,rag,memory,path=setup
    send(c,sid,'Что делает Day 09?','1')
    memory.updates=[patch('source_days','set',[14],'Теперь только день 14')]
    response=send(c,sid,'Теперь только день 14','2')
    assert response['task_state']['source_days']==[14]
    memory.updates=[]
    send(c,sid,'А при нарушении?','3')
    assert rag.calls[-1][1]['settings']['retrieval_scope']['sources']==['day-14/README.md']
    assert rag.calls[-1][1]['settings']['retrieval_scope']['reason']=='confirmed_task_memory'


@pytest.mark.parametrize('change',[
    patch('unknown','set','x','Вопрос'),
    patch('goal','add','x','Вопрос'),
    patch('goal','set','x'*1201,'Вопрос'),
    patch('terms','set',[1],'Вопрос'),
    patch('terms','remove','not present','Вопрос'),
    patch('source_days','set',[7],'Вопрос'),
    patch('source_days','set',[True],'Вопрос'),
    patch('source_days','set',[0],'день 0'),
    {'field':'goal','action':'set','value':'x','evidence':'Вопрос','extra':True},
])
def test_strict_schema_types_fields_and_limits(change):
    with pytest.raises(ValueError):
        apply_patch({'updates':[change]},EMPTY_TASK,'Вопрос день 0',lambda task:TaskState.model_validate(task).model_dump())
    assert EMPTY_TASK['goal']==''


def test_invalid_legacy_command_cannot_persist_unvalidated_task(setup):
    c,sid,rag,memory,path=setup
    response=send(c,sid,'Цель: '+'x'*1201,'1')
    assert response['result']['status']=='error'
    assert Store(path).get(sid)['task_state']==EMPTY_TASK
    assert not memory.calls


def test_extracted_memory_reaches_real_generation_prompt(tmp_path):
    generated=[]
    def retrieve(q,strategy,k,**kwargs):
        return [{'chunk_id':'c1','source':'day-07/README.md','section':'История','text':'История загружается из SQLite.','score':.9}]
    def generation(messages):
        generated.append(messages)
        return {'answer':json.dumps({'status':'answered','claims':[{'text':'История загружается из SQLite.','chunk_id':'c1','quote':'История загружается из SQLite.'}],'clarification':''})}
    def verifier(messages):
        return {'answer':json.dumps({'checks':[{'index':0,'status':'supported','reason':'Подтверждено'}]})}
    memory=Memory()
    memory.updates=[patch('goal','set','Понять историю Day 07','Хочу понять историю Day 07'),patch('constraints','add','Кратко','Объясняй кратко')]
    app=create_app(chat_db=tmp_path/'chat.sqlite',retriever=retrieve,llm=generation,verifier=verifier,memory_llm=memory,query_fitter=lambda q,g:q,index_reader=lambda *a:{'ready':True})
    with TestClient(app) as c:
        sid=c.post('/api/sessions',json={'title':'Чат'}).json()['session']['id']
        response=send(c,sid,'Хочу понять историю Day 07. Объясняй кратко','1')
        assert response['result']['status']=='answered'
        payload=json.loads(generated[-1][-1]['content'])
        assert payload['task_state']['goal']=='Понять историю Day 07'
        assert payload['task_state']['constraints']==['Кратко']
        assert response['result']['sources'][0]['chunk_id']=='c1'
