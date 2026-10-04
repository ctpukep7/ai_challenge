"""Scenario pacing starts after the answer, and cancellation interrupts the pause."""
import time
from threading import Event
from fastapi.testclient import TestClient
from backend.app import create_app

SCENARIO={'id':'paced','title':'Pacing','goal':'','turns':[{'question':'Первый?'},{'question':'Второй?'}]}

class RAG:
    def __init__(self):
        self.started=Event();self.release=Event();self.calls=[];self.completed=[]
    def answer(self,question,**kwargs):
        self.calls.append((question,time.monotonic()))
        if len(self.calls)==1:
            self.started.set()
            assert self.release.wait(2)
        self.completed.append(time.monotonic())
        return {'status':'unknown','answer':'Не знаю','sources':[]}

def make(tmp_path,rag,pause):
    return create_app(chat_db=tmp_path/'chat.sqlite',rag=rag,pause_seconds=pause,
        query_fitter=lambda q,g:q,index_reader=lambda *a:{'ready':True})

def poll(c,jid,predicate):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        job=c.get('/api/runs/'+jid).json()
        if predicate(job):return job
        time.sleep(.003)
    raise AssertionError('Scenario state did not arrive')

def test_question_pending_then_answer_then_pause_and_no_last_pause(tmp_path):
    rag=RAG();app=make(tmp_path,rag,.2)
    with TestClient(app) as c:
        jid=app.state.controller.run(SCENARIO,{})
        assert rag.started.wait(1)
        job=c.get('/api/runs/'+jid).json()
        assert job['phase']=='answering' and job['current']==0
        assert job['current_question']=='Первый?' and job['current_question_number']==1
        assert job['next_question_at'] is None
        session=c.get('/api/sessions/'+job['session_id']).json()
        assert session['messages'][0]['status']=='pending'
        assert session['messages'][0]['question']=='Первый?'
        # Holding the first answer does not consume the inter-answer pause.
        time.sleep(.03)
        assert len(rag.calls)==1
        rag.release.set()
        waiting=poll(c,jid,lambda j:j['phase']=='waiting')
        assert waiting['current']==1 and waiting['next_question_at']
        assert waiting['pause_seconds']==.2
        active=c.get('/api/state').json()['active_run']
        assert active['phase']=='waiting' and active['session_id']==job['session_id']
        assert c.get('/api/sessions/'+job['session_id']).json()['messages'][0]['status']=='unknown'
        final=poll(c,jid,lambda j:j['status']=='completed')
        assert rag.calls[1][1]-rag.completed[0]>=.19
        assert time.monotonic()-rag.completed[-1]<.15 # No pause after the last response.
        assert final['phase']=='completed' and final['current']==2
        assert final['current_question_number']==2 and final['next_question_at'] is None


def test_cancel_wait_interrupts_default_five_seconds_and_blocks_manual_edits(tmp_path):
    rag=RAG();app=make(tmp_path,rag,5)
    with TestClient(app) as c:
        jid=app.state.controller.run(SCENARIO,{})
        assert rag.started.wait(1);rag.release.set()
        job=poll(c,jid,lambda j:j['phase']=='waiting')
        sid=job['session_id']
        assert c.post(f'/api/sessions/{sid}/messages',json={'question':'Ручной','request_id':'manual'}).status_code==409
        assert c.patch(f'/api/sessions/{sid}/task',json={'revision':1,'task_state':{}}).status_code==409
        assert c.delete('/api/sessions/'+sid).status_code==409
        start=time.monotonic()
        c.post('/api/runs/'+jid+'/cancel')
        final=poll(c,jid,lambda j:j['status']=='cancelled')
        assert time.monotonic()-start<1
        assert final['phase']=='cancelled' and final['next_question_at'] is None
        assert final['current']==1 and len(rag.calls)==1
        assert c.get('/api/state').json()['active_run'] is None
        assert c.delete('/api/sessions/'+sid).status_code==200
