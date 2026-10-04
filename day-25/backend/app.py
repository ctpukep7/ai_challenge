"""Day 25: grounded chat and persistent memory extracted from user messages."""
from __future__ import annotations
import copy
import fcntl
import importlib.util
import json
import sys
import uuid
from collections import OrderedDict
from pathlib import Path
from threading import Event, Lock, Thread
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator
from typing import Literal
from pydantic import StrictInt
from contextlib import asynccontextmanager
from backend import day21_adapter as adapter
from backend.store import Store, Conflict
from backend.chat import ChatService
from backend.lexical import expand_context

HERE = Path(__file__).resolve().parents[1]
CHAT_DB = HERE / "data" / "chat.sqlite"
SCENARIOS = json.loads((HERE / "scenarios.json").read_text())

def load_shared(name, relative):
    spec = importlib.util.spec_from_file_location(name, HERE.parent / "day-24" / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

core = load_shared("day25_grounded_core", "rag_core.py")
provider = load_shared("day25_grounded_provider", "backend/llm.py")

GENERATION_INSTRUCTION = (
    'Prefer 1–3 essential claims that directly answer the question. Every factual detail of each claim '
    'must appear within its OWN exact quote in the SAME chunk_id. Neighbor text may interpret a quote, '
    'but must not supply facts absent from that quote. Select standalone complete sentences from the '
    'disclosed evidence; avoid excerpts starting mid-sentence or clipped at chunk boundaries. '
    'Cite contextual evidence by its own ID when its text contains the supporting sentence. '
    'Split separate facts into separate claims with their own quotes and IDs; omit unnecessary details '
    'instead of packing unsupported details into a broad claim. If no complete supporting quote exists, '
    'do not assert that fact.'
)

class TaskState(BaseModel):
    goal: str = Field(default="",max_length=1200)
    clarifications: list[str] = Field(default_factory=list,max_length=20)
    constraints: list[str] = Field(default_factory=list,max_length=20)
    terms: list[str] = Field(default_factory=list,max_length=20)
    source_days: list[StrictInt] = Field(default_factory=list,max_length=20)
    @model_validator(mode="after")
    def bounded(self):
        self.goal = self.goal.strip()
        for name in ("clarifications","constraints","terms"):
            values = [value.strip() for value in getattr(self,name)]
            if any(not value or len(value)>500 for value in values): raise ValueError("Записи памяти: 1–500 символов")
            setattr(self,name,list(dict.fromkeys(values)))
        if any(isinstance(day,bool) or day<1 or day>99 for day in self.source_days): raise ValueError("Номера дней: 1–99")
        self.source_days = sorted(set(self.source_days))
        return self
    model_config = {"extra":"forbid"}

class Settings(BaseModel):
    strategy: Literal["fixed","structure"] = "structure"
    top_k_before: int = Field(default=12,ge=1,le=30,strict=True)
    top_k_after: int = Field(default=5,ge=1,le=10,strict=True)
    threshold: float = Field(default=.45,ge=-1,le=1,allow_inf_nan=False)
    @model_validator(mode="after")
    def limits(self):
        if self.top_k_after>self.top_k_before: raise ValueError("top-K после не может превышать top-K до")
        return self
    model_config = {"extra":"forbid"}

class SessionRequest(BaseModel):
    title: str = Field(default="Новый диалог",min_length=1,max_length=120)
class TaskRequest(BaseModel):
    task_state: TaskState
    revision: int = Field(ge=0,strict=True)
class MessageRequest(BaseModel):
    question: str = Field(min_length=1,max_length=2000)
    request_id: str = Field(min_length=1,max_length=100,pattern=r"^[A-Za-z0-9_-]+$")
    settings: Settings = Field(default_factory=Settings)
    @model_validator(mode="after")
    def nonblank(self):
        self.question=self.question.strip()
        if not self.question: raise ValueError("Введите сообщение")
        return self
class ScenarioRequest(BaseModel):
    scenario_id: str
    settings: Settings = Field(default_factory=Settings)


def default_fitter(question, addition, cache=None):
    model = adapter.cached_embedder(cache or adapter.CACHE)
    if model.count(question)>model.max_tokens:
        raise ValueError("Вопрос превышает 126 токенов поисковой модели. Сократите вопрос; оригинал не обрезан.")
    words=addition.split()
    while words and model.count(question+" "+" ".join(words))>model.max_tokens:
        words.pop()
    return question+(" "+" ".join(words) if words else "")


def summarize(results):
    values=[row["result"] for row in results]
    factual=[r for r in values if r["status"]=="answered"]
    return {"turns":len(values),"answered":len(factual),"unknown":sum(r["status"]=="unknown" for r in values),
        "errors":sum(r["status"]=="error" for r in values),"memory_updates":sum(r["status"]=="memory_updated" or r.get("memory_update",{}).get("status")=="updated" for r in values),
        "answered_with_sources":sum(bool(r.get("sources")) for r in factual),"sources_denominator":len(factual),
        "goal_preserved":sum(row.get("goal_preserved",False) for row in results),"goal_available":sum(row.get("goal_available",False) for row in results),"goal_denominator":len(results),
        "expected_source_hits":sum(row.get("expected_source_hit",False) for row in results),
        "expected_source_denominator":sum(bool(row.get("expected_source")) for row in results)}

class Controller:
    def __init__(self, service, db, corpus, cache, builder, index_reader, pause_seconds=20):
        self.service,self.db,self.corpus,self.cache=service,db,corpus,cache
        self.builder,self.index_reader=builder,index_reader
        self.lock=Lock()
        self.busy=False
        self.build={"status":"idle","phase":None,"events":[],"error":None}
        self.jobs=OrderedDict()
        self.cancel_events={}
        self.pause_seconds=pause_seconds
    def state(self): return self.index_reader(self.db,self.corpus)
    def snapshot(self):
        with self.lock: return copy.deepcopy(self.build)
    def start_build(self):
        with self.lock:
            if self.busy: raise HTTPException(409,"Уже идёт сборка или сценарий")
            self.busy=True
            self.build={"status":"running","phase":"queued","events":[],"error":None}
        def worker():
            def progress(phase, completed, total, message):
                with self.lock:
                    self.build.update(phase=phase,completed=completed,total=total)
                    self.build["events"]=[*self.build["events"],{"phase":phase,"completed":completed,"total":total,"message":message}][-60:]
            try:
                self.builder(self.db,self.corpus,self.cache,progress=progress)
                with self.lock: self.build.update(status="completed",phase="complete")
            except Exception:
                with self.lock: self.build.update(status="failed",error="Не удалось построить индекс")
            finally:
                with self.lock: self.busy=False
        Thread(target=worker,daemon=True).start()
    def run(self, scenario, settings):
        if not self.state()["ready"]: raise HTTPException(409,"Сначала постройте индекс")
        with self.lock:
            if self.busy: raise HTTPException(409,"Уже идёт сборка или сценарий")
            self.busy=True
            identifier=uuid.uuid4().hex
            session=self.service.store.create(scenario["title"])["session"]["id"]
            self.cancel_events[identifier]=Event()
            self.jobs[identifier]={"id":identifier,"scenario_id":scenario["id"],"session_id":session,"status":"queued","phase":"queued","pause_seconds":self.pause_seconds,"current_question":None,"current_question_number":None,"next_question_at":None,"current":0,"total":len(scenario["turns"]),"results":[],"summary":summarize([]),"cancel_requested":False,"error":None,"settings":settings}
            while len(self.jobs)>20:
                old,_=self.jobs.popitem(last=False)
                self.cancel_events.pop(old,None)
        def worker():
            try:
                with self.lock: self.jobs[identifier]["status"]="running"
                for number, turn in enumerate(scenario["turns"],1):
                    with self.lock:
                        if self.jobs[identifier]["cancel_requested"]:
                            self.jobs[identifier].update(status="cancelled",phase="cancelled",next_question_at=None)
                            break
                        self.jobs[identifier].update(phase="answering",current_question=turn["question"],current_question_number=number,next_question_at=None)
                    response=self.service.send(session,turn["question"],f"{identifier}_{number}",settings)
                    result=response["result"]
                    row={"number":number,"question":turn["question"],"result":result,"task_state":response["task_state"],"revision":response["revision"],"expected_source":turn.get("expected_source"),"goal_preserved":response["task_state"]["goal"]==scenario["goal"],"goal_available":bool(response["task_state"]["goal"]),"expected_source_hit":bool(turn.get("expected_source")) and any(s["source"]==turn["expected_source"] for s in result.get("sources",[]))}
                    with self.lock:
                        job=self.jobs[identifier]
                        job["results"].append(row)
                        job["current"]=number
                        job["summary"]=summarize(job["results"])
                    if number < len(scenario["turns"]):
                        with self.lock:
                            job.update(phase="waiting",next_question_at=(datetime.now(timezone.utc)+timedelta(seconds=self.pause_seconds)).isoformat())
                        if self.cancel_events[identifier].wait(self.pause_seconds):
                            with self.lock:
                                job.update(status="cancelled",phase="cancelled",next_question_at=None)
                            break
                else:
                    with self.lock: self.jobs[identifier].update(status="completed",phase="completed",next_question_at=None)
            except Conflict as exc:
                with self.lock: self.jobs[identifier].update(status="failed",phase="failed",next_question_at=None,error=str(exc)[:250])
            except Exception:
                with self.lock: self.jobs[identifier].update(status="failed",phase="failed",next_question_at=None,error="Сценарий прерван ошибкой обработки")
            finally:
                with self.lock: self.busy=False
        Thread(target=worker,daemon=True).start()
        return identifier


def create_app(*, chat_db=CHAT_DB, db=adapter.DB, corpus=adapter.CORPUS, cache=adapter.CACHE, rag=None, retriever=None, llm=None, verifier=None, memory_llm=None, query_fitter=None, builder=None, index_reader=None, pause_seconds=20):
    if rag is None:
        custom_retriever = retriever is not None
        memory_llm = memory_llm or (provider.DeepSeekClient(root=HERE,max_tokens=900) if llm is None else None)
        retriever = retriever or (lambda q,strategy,top_k,**kwargs: adapter.retrieve(q,strategy,top_k,db=db,corpus=corpus,cache_dir=cache,**kwargs))
        llm=llm or provider.DeepSeekClient(root=HERE,max_tokens=1800)
        verifier=verifier or provider.DeepSeekClient(root=HERE,max_tokens=900)
        rag=core.GroundedRAG(retriever,llm,verifier,generation_instruction=GENERATION_INSTRUCTION,allow_partial_answer=True,
            context_expander=lambda q,strategy,anchors,evidence:expand_context(q,strategy,anchors,evidence,db=db) if not custom_retriever or Path(db).is_file() else [])
    store=Store(chat_db)
    service=ChatService(store,rag,lambda value:TaskState.model_validate(value).model_dump(),query_fitter or (lambda q,g:default_fitter(q,g,cache)),memory_llm=memory_llm)
    controller=Controller(service,db,corpus,cache,builder or adapter.build_cached_index,index_reader or adapter.index_state,pause_seconds=pause_seconds)
    @asynccontextmanager
    async def lifespan(_):
        # Acquiring a lifetime lock precedes restart recovery. An import or a second
        # worker must never rewrite another running application's pending request.
        lock_path=Path(str(Path(chat_db).resolve())+".lock")
        with lock_path.open("a+") as process_lock:
            try:
                fcntl.flock(process_lock.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Этот chat.sqlite уже открыт другим сервером. Используйте один worker или отдельную базу.") from exc
            try:
                store.recover_pending()
                yield
            finally:
                for client in (llm,verifier,memory_llm):
                    if hasattr(client,"close"): client.close()
                fcntl.flock(process_lock.fileno(),fcntl.LOCK_UN)
    app=FastAPI(lifespan=lifespan,title="День 25 — RAG + память задачи")
    app.state.service=service
    app.state.controller=controller
    def safe(fn,*args):
        try: return fn(*args)
        except KeyError: raise HTTPException(404,"Сессия не найдена")
        except Conflict as exc: raise HTTPException(409,str(exc))
    def allow_manual(identifier):
        with controller.lock:
            if any(job["session_id"]==identifier and job["status"] in {"queued","running"} for job in controller.jobs.values()):
                raise HTTPException(409,"В этой сессии идёт автодиалог. Остановите его перед ручным изменением.")
    @app.get("/api/state")
    def state():
        with controller.lock:
            active=next(({key:j[key] for key in ("id","status","phase","session_id","current","total","current_question","current_question_number","next_question_at","pause_seconds")} for j in reversed(controller.jobs.values()) if j["status"] in {"queued","running"}),None)
        return {"index":controller.state(),"sessions":store.list(),"scenarios":SCENARIOS,"build":controller.snapshot(),"active_run":active,"defaults":Settings().model_dump()}
    @app.post("/api/build",status_code=202)
    def build(): controller.start_build(); return controller.snapshot()
    @app.get("/api/build")
    def build_state(): return controller.snapshot()
    @app.post("/api/sessions",status_code=201)
    def create(req:SessionRequest): return store.create(req.title.strip() or "Новый диалог")
    @app.get("/api/sessions/{identifier}")
    def session(identifier:str): return safe(store.get,identifier)
    @app.delete("/api/sessions/{identifier}")
    def delete(identifier:str): allow_manual(identifier); safe(store.delete,identifier); return {"deleted":True}
    @app.patch("/api/sessions/{identifier}/task")
    def task(identifier:str,req:TaskRequest): allow_manual(identifier); return safe(store.update_task,identifier,req.task_state.model_dump(),req.revision)
    @app.post("/api/sessions/{identifier}/messages")
    def send(identifier:str,req:MessageRequest): allow_manual(identifier); return safe(service.send,identifier,req.question,req.request_id,req.settings.model_dump())
    @app.post("/api/scenarios/run",status_code=202)
    def run(req:ScenarioRequest):
        scenario=next((s for s in SCENARIOS if s["id"]==req.scenario_id),None)
        if scenario is None: raise HTTPException(422,"Неизвестный сценарий")
        return {"id":controller.run(scenario,req.settings.model_dump())}
    @app.get("/api/runs/{identifier}")
    def job(identifier:str):
        with controller.lock:
            if identifier not in controller.jobs: raise HTTPException(404,"Прогон не найден")
            return copy.deepcopy(controller.jobs[identifier])
    @app.post("/api/runs/{identifier}/cancel")
    def cancel(identifier:str):
        with controller.lock:
            if identifier not in controller.jobs: raise HTTPException(404,"Прогон не найден")
            if controller.jobs[identifier]["status"] in {"queued","running"}:
                controller.jobs[identifier]["cancel_requested"]=True
                controller.cancel_events[identifier].set()
            return copy.deepcopy(controller.jobs[identifier])
    return app

app=create_app()
