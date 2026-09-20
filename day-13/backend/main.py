"""FastAPI surface for Day 13 task state machine."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path

import bleach
import markdown
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .agent import AgentConfigurationError, AgentError, AgentInputError, AgentRequestError, PersonalizedAgent, public_constants
from .store import MemoryLimitError, SQLiteMemoryStore, StaleTaskStateError, TaskLockedError
from .task_machine import InvalidTransition, TaskEvent

BASE_DIR=Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR/".env")
ALLOWED_TAGS=["p","br","strong","em","del","code","pre","blockquote","ul","ol","li","h1","h2","h3","h4","hr","table","thead","tbody","tr","th","td"]
SECRET_PATTERN=re.compile(r"(?i)(?:bearer\s+|(?:api[_ -]?key|authorization)\s*[:=]\s*)[A-Za-z0-9._-]{12,}|\b(?:sk[-_]|gho_)[A-Za-z0-9_-]{12,}")
TONES={"friendly","neutral","business"}; DETAILS={"brief","balanced","deep"}; FORMATS={"paragraphs","bullets","steps","table"}


class ProfilePayload(BaseModel):
    name:object; role_context:object=""; language:object=""; tone:object="neutral"
    detail_level:object="balanced"; response_format:object="paragraphs"; constraints:object=""
class LongTermPayload(BaseModel): entry_type:object; content:object
class TaskPayload(BaseModel):
    goal:object=""; hard_constraints:object=""; task_data:object=""; open_questions:object=""
    current_step:object=""; expected_action:object=""
class TransitionPayload(BaseModel):
    event:object
    next_current_step:object|None=None
    next_expected_action:object|None=None
class AskPayload(BaseModel): prompt:object; controls:object=Field(default_factory=dict)


def clean_html(text):
    return bleach.clean(markdown.markdown(text,extensions=["extra","sane_lists"]),tags=ALLOWED_TAGS,attributes={},strip=True)


def validate_text(value,field,maximum=500,required=False):
    if not isinstance(value,str):raise ValueError(f"Поле «{field}» должно быть текстом.")
    value=value.strip()
    if required and not value:raise ValueError(f"Заполните поле «{field}».")
    if len(value)>maximum:raise ValueError(f"Поле «{field}» может содержать до {maximum} символов.")
    if SECRET_PATTERN.search(value):raise ValueError("Секреты и API-ключи нельзя сохранять или отправлять модели.")
    return value


def profile_values(payload):
    values={
        "name":validate_text(payload.name,"Имя",80,True),
        "role_context":validate_text(payload.role_context,"Роль и контекст"),
        "language":validate_text(payload.language,"Язык"),
        "tone":validate_text(payload.tone,"Тон",20,True),
        "detail_level":validate_text(payload.detail_level,"Детализация",20,True),
        "response_format":validate_text(payload.response_format,"Формат",20,True),
        "constraints":validate_text(payload.constraints,"Ограничения"),
    }
    if values["tone"] not in TONES:raise ValueError("Тон: friendly, neutral или business.")
    if values["detail_level"] not in DETAILS:raise ValueError("Детализация: brief, balanced или deep.")
    if values["response_format"] not in FORMATS:raise ValueError("Формат: paragraphs, bullets, steps или table.")
    return values


def safe_state(store):
    state=store.state()
    for message in state["active_session"]["messages"]:
        if message["role"]=="assistant":message["answer_html"]=clean_html(message["content"])
    return {"constants":public_constants(),**state}


def public_error(error,status=400):
    detail={"message":str(error),"category":"validation"}
    if isinstance(error,AgentConfigurationError):detail["category"]="configuration"
    elif isinstance(error,AgentRequestError):
        detail["category"]="provider_request"
        if error.provider_detail:detail["provider_detail"]=error.provider_detail
    raise HTTPException(status,detail) from error


def create_app(store_instance=None,agent_instance=None):
    app=FastAPI(title="Day 13 — конечный автомат задачи")
    app.state.store=store_instance or SQLiteMemoryStore(BASE_DIR/"data"/"memory.sqlite3")
    app.state.agent=agent_instance or PersonalizedAgent(app.state.store)

    @app.exception_handler(RequestValidationError)
    async def invalid_payload(_,__):
        return JSONResponse(status_code=400,content={"detail":{"message":"Некорректное тело запроса.","category":"validation"}})

    @app.get("/api/state")
    def get_state(request:Request):return safe_state(request.app.state.store)

    @app.post("/api/profiles",status_code=201)
    def create_profile(payload:ProfilePayload,request:Request):
        try:request.app.state.store.create_profile(profile_values(payload))
        except (ValueError,MemoryLimitError,sqlite3.IntegrityError) as error:
            public_error(ValueError("Имя профиля уже используется.") if isinstance(error,sqlite3.IntegrityError) else error)
        return safe_state(request.app.state.store)

    @app.put("/api/profiles/{profile_id}")
    def update_profile(profile_id:int,payload:ProfilePayload,request:Request):
        try:updated=request.app.state.store.update_profile(profile_id,profile_values(payload))
        except (ValueError,sqlite3.IntegrityError) as error:
            public_error(ValueError("Имя профиля уже используется.") if isinstance(error,sqlite3.IntegrityError) else error)
        if not updated:raise HTTPException(404,{"message":"Профиль не найден.","category":"not_found"})
        return safe_state(request.app.state.store)

    @app.post("/api/profiles/{profile_id}/activate")
    def activate_profile(profile_id:int,request:Request):
        if not request.app.state.store.activate_profile(profile_id):raise HTTPException(404,{"message":"Профиль не найден.","category":"not_found"})
        return safe_state(request.app.state.store)

    @app.post("/api/long-term",status_code=201)
    def add_long_term(payload:LongTermPayload,request:Request):
        try:
            kind=validate_text(payload.entry_type,"Тип",20,True)
            if kind not in ("decision","knowledge"):raise ValueError("Тип: decision или knowledge.")
            request.app.state.store.add_long_term(kind,validate_text(payload.content,"Запись",500,True))
        except (ValueError,MemoryLimitError) as error:public_error(error)
        return safe_state(request.app.state.store)

    @app.delete("/api/long-term/{entry_id}")
    def delete_long_term(entry_id:int,request:Request):
        if not request.app.state.store.delete_long_term(entry_id):raise HTTPException(404,{"message":"Запись не найдена.","category":"not_found"})
        return safe_state(request.app.state.store)

    @app.put("/api/task")
    def save_task(payload:TaskPayload,request:Request):
        try:
            values={"goal":validate_text(payload.goal,"Цель",500,True),"hard_constraints":validate_text(payload.hard_constraints,"Жёсткие ограничения"),"task_data":validate_text(payload.task_data,"Данные"),"open_questions":validate_text(payload.open_questions,"Открытые вопросы"),"current_step":validate_text(payload.current_step,"Текущий шаг"),"expected_action":validate_text(payload.expected_action,"Ожидаемое действие")}
            request.app.state.store.save_task(values)
        except TaskLockedError as error:
            raise HTTPException(409,{"message":str(error),"category":"task_locked"}) from error
        except ValueError as error:public_error(error)
        return safe_state(request.app.state.store)

    @app.post("/api/task/transition")
    def transition_task(payload:TransitionPayload,request:Request):
        try:
            event=TaskEvent(validate_text(payload.event,"Событие",40,True))
            checkpoint=None
            if event in (TaskEvent.ADVANCE,TaskEvent.APPROVE,TaskEvent.REQUEST_CHANGES):
                checkpoint={
                    "current_step":validate_text(payload.next_current_step,"Следующий шаг",500,True),
                    "expected_action":validate_text(payload.next_expected_action,"Следующее ожидаемое действие",500,True),
                }
            request.app.state.store.transition_task(event,checkpoint)
        except (ValueError,InvalidTransition) as error:
            detail={"message":str(error),"category":"invalid_transition"}
            raise HTTPException(409,detail) from error
        return safe_state(request.app.state.store)

    @app.post("/api/sessions",status_code=201)
    def create_session(request:Request):
        try:request.app.state.store.create_session()
        except MemoryLimitError as error:public_error(error)
        return safe_state(request.app.state.store)

    @app.post("/api/sessions/{session_id}/activate")
    def activate_session(session_id:int,request:Request):
        if not request.app.state.store.activate_session(session_id):raise HTTPException(404,{"message":"Сессия активного профиля не найдена.","category":"not_found"})
        return safe_state(request.app.state.store)

    @app.post("/api/sessions/{session_id}/branch",status_code=201)
    def branch_session(session_id:int,request:Request):
        try:result=request.app.state.store.branch_session(session_id)
        except MemoryLimitError as error:public_error(error)
        if not result:raise HTTPException(404,{"message":"Сессия активного профиля не найдена.","category":"not_found"})
        return safe_state(request.app.state.store)

    @app.post("/api/sessions/{session_id}/reset")
    def reset_session(session_id:int,request:Request):
        if not request.app.state.store.reset_session(session_id):raise HTTPException(404,{"message":"Сессия активного профиля не найдена.","category":"not_found"})
        return safe_state(request.app.state.store)

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id:int,request:Request):
        if not request.app.state.store.delete_session(session_id):raise HTTPException(404,{"message":"Сессия активного профиля не найдена.","category":"not_found"})
        return safe_state(request.app.state.store)

    @app.post("/api/memory/clear")
    def clear_memory(request:Request):
        request.app.state.store.clear_all_memory();return safe_state(request.app.state.store)

    @app.post("/api/ask")
    async def ask(payload:AskPayload,request:Request):
        try:
            task=request.app.state.store.task()
            if task["run_status"]=="paused":
                raise HTTPException(409,{"message":"Задача приостановлена. Сначала продолжите её.","category":"task_paused"})
            if task["phase"]=="done":
                raise HTTPException(409,{"message":"Задача завершена. Начните новый цикл полным сбросом.","category":"task_done"})
            prompt=validate_text(payload.prompt,"Вопрос",4000,True)
            if not isinstance(payload.controls,dict):raise AgentInputError("Настройки модели должны быть объектом.")
            result=await asyncio.to_thread(request.app.state.agent.ask,prompt,payload.controls)
            return {**result,"answer_html":clean_html(result["answer"]),"state":safe_state(request.app.state.store)}
        except StaleTaskStateError as error:
            raise HTTPException(409,{"message":str(error),"category":"stale_task_state"}) from error
        except (ValueError,AgentInputError,MemoryLimitError) as error:public_error(error)
        except AgentConfigurationError as error:public_error(error,503)
        except AgentError as error:public_error(error,502)

    return app


app=create_app()
