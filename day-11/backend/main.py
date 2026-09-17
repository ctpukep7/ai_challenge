"""HTTP API for Day 11 explicit memory layers."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import bleach
import markdown
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .agent import AgentConfigurationError, AgentError, AgentInputError, AgentRequestError, StatefulMemoryAgent, public_constants
from .store import MemoryLimitError, SQLiteMemoryStore


BASE_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = BASE_DIR / "data" / "memory-lab.sqlite3"
load_dotenv(BASE_DIR / ".env")
ALLOWED_TAGS = ["p", "br", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "h4", "hr", "table", "thead", "tbody", "tr", "th", "td"]
SECRET_PATTERN = re.compile(
    r"(?i)(?:bearer\s+|(?:api[_ -]?key|authorization)\s*[:=]\s*)[A-Za-z0-9._-]{12,}|\b(?:sk|gho)_[A-Za-z0-9_-]{12,}"
)


class LongTermPayload(BaseModel):
    content: object
    # Accepted and ignored so clients from the earlier typed-memory build can
    # move to the unified collection without a coordinated deployment.
    entry_type: object | None = None


class TaskPayload(BaseModel):
    goal: object = ""
    hard_constraints: object = ""
    task_data: object = ""
    open_questions: object = ""


class AskPayload(BaseModel):
    prompt: object
    controls: object = Field(default_factory=dict)


def clean_html(text):
    return bleach.clean(markdown.markdown(text, extensions=["extra", "sane_lists"]), tags=ALLOWED_TAGS, attributes={}, strip=True)


def validate_text(value, field, maximum=500, required=False):
    if not isinstance(value, str):
        raise ValueError(f"Поле «{field}» должно быть текстом.")
    value = value.strip()
    if required and not value:
        raise ValueError(f"Заполните поле «{field}».")
    if len(value) > maximum:
        raise ValueError(f"Поле «{field}» может содержать до {maximum} символов.")
    if SECRET_PATTERN.search(value):
        raise ValueError("Секреты и ключи нельзя сохранять или отправлять модели.")
    return value


def safe_state(store):
    state = store.state()
    for message in state["active_session"]["messages"]:
        if message["role"] == "assistant":
            message["answer_html"] = clean_html(message["content"])
    return {"constants": public_constants(), **state}


def public_error(error, status):
    detail = {"message": str(error), "category": "validation"}
    if isinstance(error, AgentConfigurationError):
        detail["category"] = "configuration"
    elif isinstance(error, AgentRequestError):
        detail["category"] = "provider_request"
        if error.provider_detail:
            detail["provider_detail"] = error.provider_detail
    raise HTTPException(status, detail) from error


def create_app(store_instance=None, agent_instance=None):
    app = FastAPI(title="Day 11 — явные слои памяти")
    app.state.store = store_instance or SQLiteMemoryStore(DATABASE_PATH)
    app.state.agent = agent_instance or StatefulMemoryAgent(app.state.store)

    @app.exception_handler(RequestValidationError)
    async def invalid_payload(_, __):
        return JSONResponse(status_code=400, content={"detail": {"message": "Некорректное тело запроса.", "category": "validation"}})

    @app.get("/api/state")
    def get_state(request: Request):
        return safe_state(request.app.state.store)

    @app.post("/api/long-term", status_code=201)
    def add_long_term(payload: LongTermPayload, request: Request):
        try:
            content = validate_text(payload.content, "Запись", required=True)
            request.app.state.store.add_long_term(content)
        except (ValueError, MemoryLimitError) as error:
            public_error(error, 400)
        return safe_state(request.app.state.store)

    @app.delete("/api/long-term/{entry_id}")
    def delete_long_term(entry_id: int, request: Request):
        if not request.app.state.store.delete_long_term(entry_id):
            raise HTTPException(404, {"message": "Долгосрочная запись не найдена.", "category": "not_found"})
        return safe_state(request.app.state.store)

    @app.put("/api/task")
    def save_task(payload: TaskPayload, request: Request):
        try:
            values = {
                "goal": validate_text(payload.goal, "Цель", required=True),
                "hard_constraints": validate_text(payload.hard_constraints, "Жёсткие ограничения"),
                "task_data": validate_text(payload.task_data, "Данные"),
                "open_questions": validate_text(payload.open_questions, "Открытые вопросы"),
            }
        except ValueError as error:
            public_error(error, 400)
        request.app.state.store.save_task(values)
        return safe_state(request.app.state.store)

    @app.post("/api/sessions", status_code=201)
    def create_session(request: Request):
        try:
            request.app.state.store.create_session()
        except MemoryLimitError as error:
            public_error(error, 400)
        return safe_state(request.app.state.store)

    @app.post("/api/sessions/{session_id}/activate")
    def activate_session(session_id: int, request: Request):
        if not request.app.state.store.activate_session(session_id):
            raise HTTPException(404, {"message": "Сессия не найдена.", "category": "not_found"})
        return safe_state(request.app.state.store)

    @app.post("/api/sessions/{session_id}/reset")
    def reset_session(session_id: int, request: Request):
        if not request.app.state.store.reset_session(session_id):
            raise HTTPException(404, {"message": "Сессия не найдена.", "category": "not_found"})
        return safe_state(request.app.state.store)

    @app.post("/api/sessions/{session_id}/branch", status_code=201)
    def branch_session(session_id: int, request: Request):
        try:
            branched = request.app.state.store.branch_session(session_id)
        except MemoryLimitError as error:
            public_error(error, 400)
        if not branched:
            raise HTTPException(404, {"message": "Сессия не найдена.", "category": "not_found"})
        return safe_state(request.app.state.store)

    @app.post("/api/memory/clear")
    def clear_memory(request: Request):
        request.app.state.store.clear_all_memory()
        return safe_state(request.app.state.store)

    @app.post("/api/ask")
    async def ask(payload: AskPayload, request: Request):
        try:
            prompt = validate_text(payload.prompt, "Вопрос", maximum=4000, required=True)
            if not isinstance(payload.controls, dict):
                raise AgentInputError("Настройки модели должны быть объектом.")
            result = await asyncio.to_thread(request.app.state.agent.ask, prompt, payload.controls)
            response = {**result, "answer_html": clean_html(result["answer"]), "state": safe_state(request.app.state.store)}
            return response
        except (ValueError, AgentInputError, MemoryLimitError) as error:
            public_error(error, 400)
        except AgentConfigurationError as error:
            public_error(error, 503)
        except AgentError as error:
            public_error(error, 502)

    return app


app = create_app()
