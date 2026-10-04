"""Observable citation RAG and bounded, cancellable control runs."""
from __future__ import annotations
import copy
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock, Thread
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator
from typing import Literal
from backend.day21_adapter import DB, CORPUS, CACHE, index_state, retrieve, build_cached_index
from backend.llm import DeepSeekClient, config_value, MODEL
from rag_core import GroundedRAG, DEFAULTS

QUESTIONS = json.loads((Path(__file__).resolve().parents[1] / 'questions.json').read_text())
GENERATION_INSTRUCTION = (
    'Prefer 1–3 essential claims that directly answer the question. Every factual detail of each claim '
    'must appear within its OWN exact quote in the SAME chunk_id. Neighbor text may interpret a quote, '
    'but must not supply facts absent from that quote. Select standalone complete sentences from the '
    'disclosed evidence; avoid excerpts starting mid-sentence or clipped at chunk boundaries. '
    'Cite a neighbor by its own ID when its text contains the supporting sentence. '
    'Split separate facts into separate claims with their own quotes and IDs; omit unnecessary details '
    'instead of packing unsupported details into a broad claim. If no complete supporting quote exists, '
    'do not assert that fact.'
)

class Settings(BaseModel):
    strategy: Literal['fixed','structure'] = 'structure'
    top_k_before: int = Field(default=12, ge=1, le=30, strict=True)
    top_k_after: int = Field(default=5, ge=1, le=10, strict=True)
    threshold: float = Field(default=.45, ge=-1, le=1, allow_inf_nan=False)
    @model_validator(mode='after')
    def coherent(self):
        if self.top_k_after > self.top_k_before:
            raise ValueError('top-K после фильтра не может быть больше кандидатов')
        return self

class Ask(BaseModel):
    question: str = Field(min_length=1,max_length=600)
    settings: Settings = Field(default_factory=Settings)
    @model_validator(mode='after')
    def nonblank(self):
        self.question = self.question.strip()
        if not self.question:
            raise ValueError('Введите вопрос')
        return self

class Benchmark(BaseModel):
    settings: Settings = Field(default_factory=Settings)

def summarize(rows):
    results = [r['result'] for r in rows]
    answered = [r for r in results if r['status']=='answered']
    return {'total':len(results),'answered':len(answered),'unknown':sum(r['status']=='unknown' for r in results),
            'partial_answers':sum(r.get('partial',False) for r in answered),
            'errors':sum(r['status']=='error' for r in results), 'source_denominator':len(answered),
            'with_sources':sum(bool(r['sources']) for r in answered), 'with_quotes':sum(all(s['quote'] for s in r['sources']) and bool(r['sources']) for r in answered),
            'exact_quotes':sum(r['validation']['quotes_exact'] for r in answered),
            'semantic_supported':sum(r['validation']['semantic_status']=='supported' for r in answered),
            'semantic_method':'LLM verifier; не доказательство истинности',
            'all_questions_source_denominator':len(results),
            'all_questions_with_sources':sum(bool(r['sources']) for r in results),
            'all_questions_exact_quotes':sum(r['validation']['quotes_exact'] for r in results),
            'semantic_rejected':sum(r.get('reason')=='semantic_rejected' for r in results),
            'invalid_grounding':sum(r.get('reason')=='invalid_grounding' for r in results),
            'invalid_verification':sum(r.get('reason')=='invalid_verification' for r in results),
            'weak_context':sum(r.get('reason')=='weak_context' for r in results)}

class Controller:
    def __init__(self, rag, db, corpus, cache, builder):
        self.rag,self.db,self.corpus,self.cache,self.builder=rag,db,corpus,cache,builder
        self.lock=Lock(); self.busy=False;self.jobs={}
        self.build={'status':'idle','phase':None,'completed':None,'total':None,'events':[],'error':None}
    def acquire(self):
        if self.busy: raise HTTPException(409,'Выполняется другой запрос или сборка')
        self.busy=True
    def snapshot(self):
        with self.lock:return copy.deepcopy(self.build)
    def ready(self):
        state=index_state(self.db,self.corpus)
        if not state['ready']:raise HTTPException(409,state['reason'])
    def ask(self,question,settings):
        self.ready()
        with self.lock:self.acquire()
        try:return self.rag.answer(question,settings)
        finally:
            with self.lock:self.busy=False
    def start_build(self):
        if not index_state(self.db,self.corpus)['corpus_ready']:raise HTTPException(409,'Корпус недоступен')
        with self.lock:
            self.acquire();self.build={'status':'running','phase':'queued','completed':0,'total':None,'events':[],'error':None}
        try:
            Thread(target=self.build_worker,daemon=True).start()
        except RuntimeError as exc:
            with self.lock:
                self.busy=False; self.build.update(status="failed",error="Не удалось запустить сборку")
            raise HTTPException(503,"Не удалось запустить сборку") from exc
    def build_worker(self):
        def progress(phase,completed,total,message):
            with self.lock:
                self.build.update(phase=phase,completed=completed,total=total)
                self.build['events']= [*self.build['events'],{'phase':phase,'completed':completed,'total':total,'message':message}][-80:]
        try:
            self.builder(self.db,self.corpus,self.cache,progress=progress)
            with self.lock:self.build.update(status='completed',phase='complete')
        except Exception:
            with self.lock:self.build.update(status='failed',error='Не удалось построить индекс')
        finally:
            with self.lock:self.busy=False
    def benchmark(self,settings):
        self.ready();identifier=uuid.uuid4().hex
        with self.lock:
            self.acquire()
            self.jobs[identifier]={'id':identifier,'status':'queued','current':0,'total':len(QUESTIONS),'settings':settings,
                'results':[],'summary':summarize([]),'cancel_requested':False,'error':None,'phase':'queued'}
            while len(self.jobs)>20:del self.jobs[next(iter(self.jobs))]
        try:
            Thread(target=self.worker,args=(identifier,),daemon=True).start()
        except RuntimeError as exc:
            with self.lock:
                self.busy=False; self.jobs[identifier].update(status="failed",error="Не удалось запустить прогон")
            raise HTTPException(503,"Не удалось запустить прогон") from exc
        return {'id':identifier}
    def worker(self,identifier):
        with self.lock:self.jobs[identifier]['status']='running';settings=self.jobs[identifier]['settings']
        try:
            for q in QUESTIONS:
                with self.lock:
                    if self.jobs[identifier]['cancel_requested']:
                        self.jobs[identifier].update(status='cancelled',phase='cancelled');break
                    self.jobs[identifier]['phase']='retrieval_generation_verification'
                result=self.rag.answer(q['question'],settings)
                with self.lock:
                    job=self.jobs[identifier];job['results'].append({'question_id':q['id'],'question':q['question'],'expected':q['expected'],'result':result})
                    job['current']=len(job['results']);job['summary']=summarize(job['results'])
            else:
                with self.lock:self.jobs[identifier].update(status='completed',phase='complete')
        except Exception:
            with self.lock:self.jobs[identifier].update(status='failed',error='Не удалось завершить прогон')
        finally:
            with self.lock:self.busy=False
    def run(self,identifier):
        with self.lock:
            if identifier not in self.jobs:raise HTTPException(404,'Прогон не найден')
            return copy.deepcopy(self.jobs[identifier])
    def cancel(self,identifier):
        with self.lock:
            if identifier not in self.jobs:raise HTTPException(404,'Прогон не найден')
            job=self.jobs[identifier]
            if job['status'] in ('queued','running'):job['cancel_requested']=True
            return {'id':identifier,'status':job['status'],'cancel_requested':job['cancel_requested']}

def create_app(db=DB,corpus=CORPUS,cache=CACHE,builder=build_cached_index,retriever=retrieve,llm=None,verifier=None):
    owned=[]
    if llm is None:llm=DeepSeekClient();owned.append(llm)
    if verifier is None:
        if isinstance(llm,DeepSeekClient):verifier=DeepSeekClient(max_tokens=900);owned.append(verifier)
        else:verifier=llm
    rag=GroundedRAG(lambda q,s,k,**kw:retriever(q,s,k,db,cache,corpus=corpus,**kw),llm,verifier,
                    generation_instruction=GENERATION_INSTRUCTION,allow_partial_answer=True)
    controller=Controller(rag,db,corpus,cache,builder)
    @asynccontextmanager
    async def lifespan(app):
        try:yield
        finally:
            for client in owned:client.close()
    app=FastAPI(title='Day 24 — Цитаты и источники',lifespan=lifespan)
    app.state.controller=controller;app.state.rag=rag
    @app.get('/api/state')
    def state():
        with controller.lock:
            active=next(({'id':j['id'],'status':j['status']} for j in reversed(list(controller.jobs.values())) if j['status'] in ('queued','running')),None)
        return {'index':index_state(db,corpus),'questions':QUESTIONS,'build':controller.snapshot(),'active_run':active,
                'defaults':DEFAULTS,'model':MODEL,'llm_configured':bool(config_value('DEEPSEEK_API_KEY'))}
    @app.post('/api/build',status_code=202)
    def build():controller.start_build();return {'status':'running'}
    @app.get('/api/build')
    def build_status():return controller.snapshot()
    @app.post('/api/ask')
    def ask(request:Ask):return controller.ask(request.question,request.settings.model_dump())
    @app.post('/api/benchmark',status_code=202)
    def benchmark(request:Benchmark):return controller.benchmark(request.settings.model_dump())
    @app.get('/api/runs/{identifier}')
    def run(identifier:str):return controller.run(identifier)
    @app.post('/api/runs/{identifier}/cancel')
    def cancel(identifier:str):return controller.cancel(identifier)
    return app

app=create_app()
