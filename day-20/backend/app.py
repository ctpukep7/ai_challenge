import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from uuid import UUID
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from backend.config import Settings
from backend.store import Store, now
from backend.registry import Registry
from backend.agent import Agent, Model
from backend.services import payload_hash
from backend.json_requests import JsonObserver, capture_scope
from backend.config import ROOT

class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1,max_length=5000)
    request_id: UUID
    calendar_id: str | None = None


def create_app(settings=None,model=None,registry=None):
    settings=settings or Settings.from_env();store=Store(settings.db_path)
    observer=JsonObserver(store,secrets=(settings.api_key,settings.calendar_username,settings.calendar_password),paths=(str(ROOT),settings.db_path))
    registry=registry or Registry(settings,observer=observer);model=model or Model(settings,observer=observer)
    if isinstance(registry,Registry):registry.observer=observer
    if isinstance(model,Model):model.observer=observer
    agent=Agent(store,registry,model,settings)
    tasks=set();confirmation_locks={}
    @asynccontextmanager
    async def lifespan(app):
        store.recover();await registry.start()
        try:yield
        finally:
            for task in list(tasks):task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            await registry.close()
            if hasattr(model,'close'):await model.close()
    app=FastAPI(title='Day 20 — Orchestration MCP',lifespan=lifespan)
    app.add_middleware(CORSMiddleware,allow_origins=['http://127.0.0.1:5186','http://localhost:5186'],allow_methods=['GET','POST'],allow_headers=['Content-Type'])
    app.state.store=store;app.state.registry=registry;app.state.agent=agent
    @app.get('/api/state')
    async def state():
        calendars={}
        for e in store.all('evidence'):
            if e['kind']=='calendars':
                for c in e['data']['calendars']:calendars[c['id']]=c
        return dict(messages=store.all('message'),registry=registry.registry(),plans=store.all('plan'),calendars=list(calendars.values()),runs=[{k:r[k] for k in ('id','status','prompt','created_at')} for r in store.all('run')],config=dict(timezone=settings.timezone,model=settings.model,model_ready=bool(settings.api_key),calendar_ready=bool(settings.calendar_username and settings.calendar_password)))
    @app.post('/api/chat',status_code=202)
    async def chat(body: ChatRequest):
        prompt=body.prompt.strip()
        if not prompt:raise HTTPException(422,'Введите запрос')
        fingerprint=hashlib.sha256(json.dumps([prompt,body.calendar_id],ensure_ascii=False).encode()).hexdigest()
        try:run,new=store.new_run(str(body.request_id),fingerprint,prompt,body.calendar_id)
        except ValueError as error:raise HTTPException(409,str(error)) from None
        if new:
            task=asyncio.create_task(agent.run(run['id']));tasks.add(task);task.add_done_callback(tasks.discard)
        return dict(run_id=run['id'])
    @app.get('/api/runs/{run_id}')
    async def get_run(run_id: str):
        try:run=store.get('run',run_id)
        except ValueError:raise HTTPException(404,'Запуск не найден') from None
        run['plan']=store.plan_for_run(run_id)
        return run
    @app.get('/api/runs/{run_id}/json-requests')
    async def get_json_requests(run_id: str):
        try:store.get('run',run_id)
        except ValueError:raise HTTPException(404,'Запуск не найден') from None
        exchanges=store.json_requests(run_id)
        return dict(run_id=run_id,capture_available=bool(exchanges),exchanges=exchanges)
    @app.post('/api/plans/{plan_id}/confirm')
    async def confirm(plan_id: str):
        # An empty body confirms exactly the immutable persisted card, never model text.
        lock=confirmation_locks.setdefault(plan_id,asyncio.Lock())
        async with lock:
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                try:plan=store.get('plan',plan_id,db)
                except ValueError:raise HTTPException(404,'План не найден') from None
                if plan['status']=='created':return plan
                if plan['status'] in ('rejected','conflict'):raise HTTPException(409,'Создайте новый проект')
                if plan['status']=='draft':
                    plan['status']='approved';store.put('plan',plan,db)
                    store.put('operation',dict(id=plan_id,plan_id=plan_id,uid=plan['uid'],payload_hash=payload_hash(plan),approved_at=now(),status='pending'),db)
            started=time.monotonic()
            trace=dict(sequence=0,server_id='calendar',alias='calendar__create_trip_event',tool='create_trip_event',status='running',duration_ms=0,arguments={'plan_id':plan_id},result_summary='',error=None)
            trace=store.allocate_trace(plan['run_id'],trace)
            try:
                with capture_scope(plan['run_id'],trace['sequence']):
                    result=await registry.call('calendar__create_trip_event',dict(plan_id=plan_id),approval=True)
                trace.update(status='completed' if result['status']=='created' else 'error',result_summary='Событие создано или найдено по UID' if result['status']=='created' else 'Календарная операция требует внимания',error=result.get('error'))
                return result
            except (Exception,asyncio.CancelledError) as error:
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE');plan=store.get('plan',plan_id,db)
                    if plan['status']!='created':
                        plan.update(status='uncertain',error='Ответ календаря не получен; повтор подтверждения сверит UID');store.put('plan',plan,db)
                        operation=store.get('operation',plan_id,db);operation['status']='uncertain';store.put('operation',operation,db)
                trace.update(status='completed' if plan['status']=='created' else 'error',error=plan.get('error'),result_summary='Исход операции требует сверки' if plan['status']!='created' else 'Событие подтверждено')
                if isinstance(error,asyncio.CancelledError):raise
                return plan
            finally:
                trace['duration_ms']=round((time.monotonic()-started)*1000);store.trace(plan['run_id'],trace)
    @app.post('/api/plans/{plan_id}/reject')
    async def reject(plan_id: str):
        lock=confirmation_locks.setdefault(plan_id,asyncio.Lock())
        async with lock:
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                try:plan=store.get('plan',plan_id,db)
                except ValueError:raise HTTPException(404,'План не найден') from None
                if plan['status'] not in ('draft','rejected'):raise HTTPException(409,'Календарная операция уже начата')
                plan['status']='rejected';return store.put('plan',plan,db)
    return app

app=create_app()
