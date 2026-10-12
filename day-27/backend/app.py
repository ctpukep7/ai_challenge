"""Stateless HTTP bridge to the fixed local Ollama instance."""

import asyncio
import math
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

MODEL = "qwen3:4b-instruct-2507-q4_K_M"
OLLAMA_URL = "http://127.0.0.1:11434"
TEXT_BUDGET = 3000
REQUEST_SECONDS = 120
PARAMETERS = {"temperature": 0.7, "num_predict": 1024, "num_ctx": 4096, "keep_alive": "1m"}


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: StrictStr


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: StrictStr
    history: list[Message] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def complete_pairs(self):
        # JSON permits escaped lone surrogates; they cannot enter a UTF-8 model request.
        try:
            self.prompt.encode("utf-8")
            for message in self.history:
                message.content.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("invalid Unicode") from None
        if not self.prompt.strip():
            raise ValueError("empty prompt")
        if len(self.history) % 2:
            raise ValueError("incomplete history")
        for index, message in enumerate(self.history):
            if message.role != ("user" if index % 2 == 0 else "assistant") or not message.content.strip():
                raise ValueError("invalid history")
        return self


def bounded_messages(body: ChatRequest) -> tuple[list[dict], int]:
    prompt_size = len(body.prompt.encode("utf-8"))
    if prompt_size > TEXT_BUDGET:
        raise APIError(413, "prompt_too_long", "Вопрос слишком длинный: максимум 3000 байт UTF-8. Сократите текст.")
    history = [message.model_dump() for message in body.history]
    total = prompt_size + sum(len(message["content"].encode("utf-8")) for message in history)
    dropped = 0
    while total > TEXT_BUDGET and history:
        total -= sum(len(message["content"].encode("utf-8")) for message in history[:2])
        del history[:2]
        dropped += 1
    return history + [{"role": "user", "content": body.prompt}], dropped


async def provider_json(client: httpx.AsyncClient, path: str, *, payload=None) -> dict:
    try:
        response = await client.request("GET" if payload is None else "POST", path, json=payload)
    except httpx.TimeoutException:
        raise APIError(504, "timeout", "Модель не ответила за отведённое время. Попробуйте отправить вопрос ещё раз.") from None
    except httpx.RequestError:
        raise APIError(503, "unavailable", "Ollama недоступен. Запустите локальный сервер и обновите состояние.") from None
    if response.is_error or response.is_redirect:
        raise APIError(502, "provider_error", "Локальный сервер модели вернул ошибку. Проверьте Ollama.")
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        raise APIError(502, "invalid_response", "Модель вернула некорректный JSON. Отправьте вопрос ещё раз.") from None
    if not isinstance(data, dict) or "error" in data:
        raise APIError(502, "invalid_response", "Получен некорректный ответ локальной модели.")
    return data


async def require_model(client: httpx.AsyncClient):
    data = await provider_json(client, "/api/tags")
    models = data.get("models")
    if not isinstance(models, list) or any(not isinstance(model, dict) for model in models):
        raise APIError(502, "invalid_response", "Ollama вернул некорректный список моделей.")
    if not any(model.get("name") == MODEL or model.get("model") == MODEL for model in models):
        raise APIError(503, "missing_model", "Выбранная модель не установлена. Используйте установку из Day26.")


def validate_answer(data: dict) -> tuple[str, dict, bool]:
    message = data.get("message")
    if data.get("done") is not True or not isinstance(message, dict) or not isinstance(message.get("content"), str) or not message["content"].strip():
        raise APIError(502, "invalid_response", "Модель вернула пустой или незавершённый ответ. Отправьте вопрос ещё раз.")
    values = {}
    for key in ("total_duration", "load_duration", "eval_duration", "prompt_eval_count", "eval_count"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise APIError(502, "invalid_response", "Модель вернула некорректные метрики ответа.")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise APIError(502, "invalid_response", "Модель вернула некорректные метрики ответа.")
        values[key] = value
    seconds = values["eval_duration"] / 1_000_000_000
    metrics = {
        "total_seconds": values["total_duration"] / 1_000_000_000,
        "load_seconds": values["load_duration"] / 1_000_000_000,
        "generation_seconds": seconds,
        "prompt_tokens": values["prompt_eval_count"],
        "output_tokens": values["eval_count"],
        "tokens_per_second": values["eval_count"] / seconds if seconds else 0.0,
    }
    if not all(math.isfinite(value) for value in metrics.values()):
        raise APIError(502, "invalid_response", "Модель вернула некорректные метрики ответа.")
    return message["content"].strip(), metrics, data.get("done_reason") == "length"


def create_app(transport: httpx.AsyncBaseTransport | None = None, dist: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(base_url=OLLAMA_URL, transport=transport, trust_env=False,
                                     follow_redirects=False, timeout=httpx.Timeout(REQUEST_SECONDS, connect=5)) as client:
            app.state.client = client
            app.state.generation_lock = asyncio.Lock()
            yield

    app = FastAPI(title="Day27 · Локальный чат", lifespan=lifespan)

    @app.exception_handler(APIError)
    async def api_error(_request: Request, exc: APIError):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": "Проверьте вопрос и историю: нужны непустые сообщения и максимум четыре полные пары вопрос–ответ."}})

    @app.get("/api/health")
    async def health(request: Request):
        result = {"status": "ready", "model": MODEL, "version": None, "parameters": PARAMETERS.copy(), "message": "Локальная модель готова к общению."}
        try:
            async with asyncio.timeout(5):
                version = await provider_json(request.app.state.client, "/api/version")
                if not isinstance(version.get("version"), str) or not version["version"].strip():
                    raise APIError(502, "invalid_response", "Ollama вернул некорректную версию сервера.")
                result["version"] = version["version"]
                await require_model(request.app.state.client)
        except APIError as exc:
            result.update(status="missing_model" if exc.code == "missing_model" else "unavailable", message=exc.message)
        except TimeoutError:
            result.update(status="unavailable", message="Ollama не ответил на проверку состояния. Обновите состояние.")
        return result

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request):
        messages, dropped = bounded_messages(body)
        lock = request.app.state.generation_lock
        if lock.locked():
            raise APIError(409, "busy", "Модель сейчас отвечает на другой вопрос. Дождитесь завершения и отправьте вопрос снова.")
        # No await between checking and acquiring: one event loop cannot queue a second generation here.
        await lock.acquire()
        try:
            async with asyncio.timeout(REQUEST_SECONDS):
                await require_model(request.app.state.client)
                data = await provider_json(request.app.state.client, "/api/chat", payload={
                    "model": MODEL, "messages": messages, "stream": False, "think": False,
                    "keep_alive": PARAMETERS["keep_alive"],
                    "options": {key: value for key, value in PARAMETERS.items() if key != "keep_alive"},
                })
                answer, metrics, truncated = validate_answer(data)
                return {"answer": answer, "model": MODEL, "dropped_pairs": dropped, "metrics": metrics, "truncated": truncated}
        except TimeoutError:
            raise APIError(504, "timeout", "Модель не ответила за 120 секунд. Попробуйте отправить вопрос ещё раз.") from None
        finally:
            lock.release()

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def unknown_api(path: str):
        return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "Такого API-маршрута нет."}})

    frontend_dist = dist if dist is not None else Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    return app


app = create_app()
