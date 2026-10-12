import asyncio
import copy
import hashlib
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictStr, model_validator

from .errors import APIError
from .cloud_llm import load_config, comparison_state, generate as cloud_generate
from .local_llm import MODEL, PARAMETERS, URL, generate, require_model
from .logic import CloudBaseline, mark_citations, prepare_context
from .retrieval import HERE, Retriever

REQUEST_SECONDS = 120


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: StrictStr

    @model_validator(mode="after")
    def validate_question(self):
        self.question = self.question.strip()
        try:
            raw = self.question.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("invalid Unicode") from None
        if not self.question:
            raise ValueError("empty question")
        if len(self.question) > 500 or len(raw) > 1000:
            raise APIError(413, "question_too_long", "Вопрос слишком длинный: максимум 500 символов и 1000 байт UTF-8.")
        return self


def create_app(retriever=None, transport=None, baseline_path=None, dist=None, cloud_transport=None, cloud_config=None):
    retrieval = retriever if retriever is not None else Retriever()
    baseline = CloudBaseline(baseline_path or HERE / "data/cloud-baseline.json")
    config = cloud_config if cloud_config is not None else load_config()
    questions = json.loads((HERE / "questions.json").read_text())

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(base_url=URL, transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(REQUEST_SECONDS, connect=5)) as client, httpx.AsyncClient(
                transport=cloud_transport, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(REQUEST_SECONDS, connect=10)) as cloud_client:
            app.state.client = client
            app.state.cloud_client = cloud_client
            app.state.lock = asyncio.Lock()
            app.state.tasks = set()
            yield
            tasks = list(app.state.tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Day28 · Локальный RAG", lifespan=lifespan)

    @app.exception_handler(APIError)
    async def error(_request, exc):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _exc):
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": "Введите непустой вопрос без дополнительных полей."}})

    @app.get("/api/state")
    async def state(request: Request):
        index = await asyncio.to_thread(retrieval.index_state)
        result = {"status": "ready" if index["ready"] else "unavailable", "message": "Локальный RAG готов к вопросам." if index["ready"] else index.get("reason", "Локальный индекс недоступен."),
                  "model": MODEL, "parameters": PARAMETERS.copy(), "index": index,
                  "questions": [{key: row[key] for key in ("id", "question", "label")} for row in questions],
                  "cloud": baseline.state(), "comparison": comparison_state(config)}
        try:
            async with asyncio.timeout(5):
                await require_model(request.app.state.client)
        except APIError as exc:
            result.update(status="unavailable", message=exc.message)
        except TimeoutError:
            result.update(status="unavailable", message="Ollama не ответил на проверку состояния.")
        return result

    def classify(answer):
        return "unknown" if re.match(r"^не\s+знаю\b", answer, re.IGNORECASE) else "answered"

    async def pipeline(question, application, deadline, compare=False):
        started = time.perf_counter()
        worker = asyncio.create_task(asyncio.to_thread(retrieval.retrieve, question))
        try:
            try:
                found = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Keep the slot until a non-cancellable Python thread actually finishes.
                try:
                    await worker
                except Exception:
                    pass
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise APIError(504, "timeout", "Локальный RAG не успел ответить за 120 секунд.")
            metrics = {"retrieval_seconds": found["retrieval_seconds"], "embedding_load_seconds": found["embedding_load_seconds"],
                       "embedding_cold": found["embedding_cold"], "http_seconds": 0.0, "load_seconds": 0.0,
                       "generation_seconds": 0.0, "prompt_tokens": 0, "output_tokens": 0, "tokens_per_second": 0.0,
                       "total_seconds": 0.0}
            messages, sources = prepare_context(question, found["evidence"])
            base = {"question": question, "mode": "compare" if compare else "local", "status": "unknown",
                    "answer": "Не знаю. Подтверждённых источников недостаточно. Уточните день и вопрос.",
                    "model": MODEL, "truncated": False, "reason": "weak_context" if not found["evidence"] else "context_limit",
                    "citation_warning": None, "sources": [], "metrics": metrics, "scope": found["scope"],
                    "cloud": None, "archive": baseline.match(question), "error": None}
            if compare:
                base["comparison"] = {"identical_messages": True,
                    "messages_sha256": hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                    "chunks": len(sources), "total_seconds": 0.0, "temperature": .7, "max_tokens": 1024, "thinking": "disabled"}
                base["cloud"] = {"historical": False, "model": config.model, "status": "unknown",
                    "answer": "Сравнение не выполнено: подтверждённых источников недостаточно.", "truncated": False,
                    "reason": base["reason"], "citation_warning": None, "sources": [], "metrics": None, "error": None, "called": False}
            if messages:
                # Reserve a small slice of the shared deadline to assemble partial results.
                reserve = min(.05, REQUEST_SECONDS * .02) if compare else 0
                async def local_result():
                    http_start = time.perf_counter()
                    own_sources = copy.deepcopy(sources)
                    try:
                        async with asyncio.timeout(max(0, deadline - time.monotonic() - reserve)):
                            answer, generated, truncated = await generate(application.state.client, messages)
                        return {"answer": answer, "status": classify(answer), "reason": "missing_evidence" if classify(answer) == "unknown" else "",
                                "truncated": truncated, "sources": own_sources, "citation_warning": mark_citations(answer, own_sources),
                                "metrics": {**metrics, **generated, "http_seconds": time.perf_counter() - http_start,
                                            "total_seconds": time.perf_counter() - started}, "error": None}
                    except (APIError, TimeoutError) as exc:
                        if not compare:
                            if isinstance(exc, APIError):
                                raise
                            raise APIError(504, "timeout", "Локальный RAG не успел ответить за 120 секунд.") from None
                        code = exc.code if isinstance(exc, APIError) else "timeout"
                        message = exc.message if isinstance(exc, APIError) else "Локальная модель не ответила за отведённое время."
                        return {"status": "error", "answer": message, "reason": "provider_error", "sources": own_sources,
                                "metrics": {**metrics, "total_seconds": time.perf_counter() - started}, "error": {"code": code, "message": message}}

                async def cloud_result():
                    http_start = time.perf_counter()
                    own_sources = copy.deepcopy(sources)
                    try:
                        async with asyncio.timeout(max(0, deadline - time.monotonic() - reserve)):
                            generated = await cloud_generate(application.state.cloud_client, config, messages)
                        status = classify(generated["answer"])
                        return {"historical": False, "model": generated["model"], "answer": generated["answer"], "status": status,
                                "reason": "missing_evidence" if status == "unknown" else "", "truncated": generated["truncated"],
                                "citation_warning": mark_citations(generated["answer"], own_sources), "sources": own_sources,
                                "metrics": {"http_seconds": time.perf_counter() - http_start, "total_seconds": time.perf_counter() - started,
                                            "prompt_tokens": generated["prompt_tokens"], "output_tokens": generated["output_tokens"]},
                                "error": None, "called": True}
                    except (APIError, TimeoutError) as exc:
                        code = exc.code if isinstance(exc, APIError) else "cloud_timeout"
                        message = exc.message if isinstance(exc, APIError) else "DeepSeek не ответил за отведённое время. Автоматического повтора не было."
                        return {"historical": False, "model": config.model, "status": "error", "answer": message,
                                "reason": "provider_error", "truncated": False, "citation_warning": None, "sources": own_sources,
                                "metrics": None, "error": {"code": code, "message": message}, "called": True}

                if compare:
                    local, cloud = await asyncio.gather(local_result(), cloud_result())
                    base.update(local)
                    base["cloud"] = cloud
                else:
                    base.update(await local_result())
            else:
                metrics["total_seconds"] = time.perf_counter() - started
            if compare:
                base["comparison"]["total_seconds"] = time.perf_counter() - started
            return base
        except APIError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise APIError(503, "local_rag_error", "Локальный RAG не смог обработать вопрос. Проверьте индекс и кэш Day21.") from None
        finally:
            application.state.lock.release()

    async def execute(body, request, compare=False):
        application = request.app
        if compare and not config.api_key:
            raise APIError(503, "cloud_unconfigured", "DeepSeek не настроен. Локальный вопрос можно отправить обычной кнопкой.")
        lock = application.state.lock
        if lock.locked():
            raise APIError(409, "busy", "Модель обрабатывает другой вопрос. Дождитесь завершения и отправьте вопрос снова.")
        await lock.acquire()
        task = asyncio.create_task(pipeline(body.question, application, time.monotonic() + REQUEST_SECONDS, compare))
        application.state.tasks.add(task)
        def completed(done):
            application.state.tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(completed)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=REQUEST_SECONDS)
        except TimeoutError:
            raise APIError(504, "timeout", "Локальный RAG не успел ответить за 120 секунд. Дождитесь освобождения модели.") from None

    @app.post("/api/ask")
    async def ask(body: AskRequest, request: Request):
        return await execute(body, request)

    @app.post("/api/compare")
    async def compare(body: AskRequest, request: Request):
        return await execute(body, request, compare=True)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def missing(path):
        return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "Такого API-маршрута нет."}})

    built = Path(dist) if dist is not None else HERE / "frontend/dist"
    if built.is_dir():
        app.mount("/", StaticFiles(directory=built, html=True), name="frontend")
    return app


app = create_app()
