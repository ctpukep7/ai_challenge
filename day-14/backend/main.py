"""FastAPI surface for Day 14 deterministic invariants."""

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

from .agent import AgentConfigurationError, AgentError, AgentInputError, AgentRequestError, InvariantConflictError, PersonalizedAgent, public_constants
from .store import MemoryLimitError, SQLiteMemoryStore

BASE_DIR=Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR/".env")
ALLOWED_TAGS=["p","br","strong","em","del","code","pre","blockquote","ul","ol","li","h1","h2","h3","h4","hr","table","thead","tbody","tr","th","td"]
SECRET_PATTERN=re.compile(r"(?i)(?:bearer\s+|(?:api[_ -]?key|authorization)\s*[:=]\s*)[A-Za-z0-9._-]{12,}|\b(?:sk[-_]|gho_)[A-Za-z0-9_-]{12,}")
TONES={"friendly","neutral","business"}; DETAILS={"brief","balanced","deep"}; FORMATS={"paragraphs","bullets","steps","table"}
INVARIANT_CATEGORIES={"architecture","stack","technical_decision","business_rule"}
INVARIANT_OPERATORS={"equals","not_equals","one_of","not_one_of"}
INVARIANT_FIELD_PATTERN=re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")


class ProfilePayload(BaseModel):
    name:object; role_context:object=""; language:object=""; tone:object="neutral"
    detail_level:object="balanced"; response_format:object="paragraphs"; constraints:object=""
class LongTermPayload(BaseModel): entry_type:object; content:object
class TaskPayload(BaseModel):
    goal:object=""; hard_constraints:object=""; task_data:object=""; open_questions:object=""
class InvariantPayload(BaseModel):
    category:object; field:object; operator:object; expected_value:object
    description:object; reason:object; compatible_alternative:object; active:object=True
class InvariantActivePayload(BaseModel): active:object
class CandidatePayload(BaseModel): field:object; value:object; proposal:object
class AskPayload(BaseModel): prompt:object; candidate:CandidatePayload; controls:object=Field(default_factory=dict)


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


def invariant_values(payload):
    values={
        "category":validate_text(payload.category,"Категория",40,True),
        "field":validate_text(payload.field,"Поле",80,True).lower(),
        "operator":validate_text(payload.operator,"Оператор",20,True),
        "expected_value":validate_text(payload.expected_value,"Ожидаемое значение",200,True),
        "description":validate_text(payload.description,"Описание",500,True),
        "reason":validate_text(payload.reason,"Причина",500,True),
        "compatible_alternative":validate_text(payload.compatible_alternative,"Совместимая альтернатива",200,True),
        "active":payload.active,
    }
    if values["category"] not in INVARIANT_CATEGORIES:raise ValueError("Неизвестная категория инварианта.")
    if values["operator"] not in INVARIANT_OPERATORS:raise ValueError("Неизвестный оператор инварианта.")
    if not INVARIANT_FIELD_PATTERN.fullmatch(values["field"]):raise ValueError("Поле: латиница, цифры, точка, дефис или подчёркивание.")
    if not isinstance(values["active"],bool):raise ValueError("Признак активности должен быть true или false.")
    if values["operator"] in ("one_of","not_one_of"):
        options=[item.strip() for item in values["expected_value"].split("|") if item.strip()]
        if not 2<=len(options)<=10:raise ValueError("Для one_of/not_one_of укажите 2–10 значений через |.")
    return values


def candidate_values(payload):
    field=validate_text(payload.field,"Поле candidate",80,True).lower()
    if not INVARIANT_FIELD_PATTERN.fullmatch(field):raise ValueError("Поле candidate имеет неверный формат.")
    return {
        "field":field,
        "value":validate_text(payload.value,"Значение candidate",200,True),
        "proposal":validate_text(payload.proposal,"Описание candidate",1000,True),
    }


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
    app=FastAPI(title="Day 14 — детерминированные инварианты")
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

    @app.post("/api/invariants",status_code=201)
    def create_invariant(payload:InvariantPayload,request:Request):
        try:request.app.state.store.create_invariant(invariant_values(payload))
        except (ValueError,MemoryLimitError) as error:public_error(error)
        return safe_state(request.app.state.store)

    @app.put("/api/invariants/{invariant_id}")
    def update_invariant(invariant_id:int,payload:InvariantPayload,request:Request):
        try:updated=request.app.state.store.update_invariant(invariant_id,invariant_values(payload))
        except ValueError as error:public_error(error)
        if not updated:raise HTTPException(404,{"message":"Инвариант не найден.","category":"not_found"})
        return safe_state(request.app.state.store)

    @app.patch("/api/invariants/{invariant_id}/active")
    def set_invariant_active(invariant_id:int,payload:InvariantActivePayload,request:Request):
        if not isinstance(payload.active,bool):public_error(ValueError("Признак активности должен быть true или false."))
        if not request.app.state.store.set_invariant_active(invariant_id,payload.active):
            raise HTTPException(404,{"message":"Инвариант не найден.","category":"not_found"})
        return safe_state(request.app.state.store)

    @app.delete("/api/invariants/{invariant_id}")
    def delete_invariant(invariant_id:int,request:Request):
        if not request.app.state.store.delete_invariant(invariant_id):
            raise HTTPException(404,{"message":"Инвариант не найден.","category":"not_found"})
        return safe_state(request.app.state.store)

    @app.put("/api/task")
    def save_task(payload:TaskPayload,request:Request):
        try:
            values={"goal":validate_text(payload.goal,"Цель",500,True),"hard_constraints":validate_text(payload.hard_constraints,"Жёсткие ограничения"),"task_data":validate_text(payload.task_data,"Данные"),"open_questions":validate_text(payload.open_questions,"Открытые вопросы")}
            request.app.state.store.save_task(values)
        except ValueError as error:public_error(error)
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
            prompt=validate_text(payload.prompt,"Вопрос",4000,True)
            candidate=candidate_values(payload.candidate)
            if not isinstance(payload.controls,dict):raise AgentInputError("Настройки модели должны быть объектом.")
            result=await asyncio.to_thread(request.app.state.agent.ask,prompt,candidate,payload.controls)
            return {**result,"answer_html":clean_html(result["answer"]),"state":safe_state(request.app.state.store)}
        except InvariantConflictError as error:
            raise HTTPException(409,{
                "message":"Предложение отклонено до вызова модели: нарушен активный инвариант.",
                "category":"invariant_conflict",
                "invariant_check":error.check_result,
            }) from error
        except (ValueError,AgentInputError,MemoryLimitError) as error:public_error(error)
        except AgentConfigurationError as error:public_error(error,503)
        except AgentError as error:public_error(error,502)

    return app


app=create_app()
