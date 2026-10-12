import asyncio
import copy
import hashlib
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from .errors import APIError
from .local_llm import MODEL, URL, generate, metadata, preload, require_model, resource_sample
from .profiles import LIMITS, profile
from .settings import Settings

ASK_SECONDS = 120
COMPARE_SECONDS = 240
STAGE_SECONDS = 120
PS_INTERVAL = .25
HERE = Path(__file__).resolve().parents[1]


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: StrictStr
    settings: Settings = Field(default_factory=Settings)
    first_profile: Literal["baseline", "optimized"] | None = None

    @model_validator(mode="after")
    def validate_question(self):
        self.question = self.question.strip()
        try:
            raw = self.question.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("invalid Unicode") from None
        if not self.question:
            raise ValueError("empty question")
        if len(self.question) > 4000 or len(raw) > 10000:
            raise APIError(413, "question_too_long", "Вопрос слишком длинный: максимум 4000 символов и 10000 байт UTF-8.")
        return self


def empty_resources():
    return {"samples": 0, "peak_size_bytes": None, "peak_size_vram_bytes": None,
            "context_length": None, "quantization": None,
            "note": "Размер размещения Ollama по полученным /api/ps; это не полный пик RAM системы."}


def accumulate(resources, sample):
    if sample is None:
        return
    resources["samples"] += 1
    resources["context_length"] = sample["context_length"]
    if sample["quantization"]:
        resources["quantization"] = sample["quantization"]
    for target, source in (("peak_size_bytes", "size_bytes"), ("peak_size_vram_bytes", "size_vram_bytes")):
        value = sample[source]
        if value is not None:
            resources[target] = value if resources[target] is None else max(value, resources[target])


def result_base(identifier, settings):
    chosen = profile(identifier, settings)
    return {"profile": identifier, "label": chosen["label"], "model": MODEL,
            "parameters": chosen["parameters"], "system": chosen["system"], "status": "error",
            "answer": "",
            "called": False, "truncated": False, "reason": "", "error": None,
            "context_warning": None,
            "metrics": {"total_seconds": 0, "preload_seconds": None, "preload_load_seconds": None,
                        "http_seconds": None, "load_seconds": None, "generation_seconds": None,
                        "prompt_tokens": None, "output_tokens": None, "tokens_per_second": None},
            "resources": empty_resources()}


def create_app(transport=None, dist=None):
    @asynccontextmanager
    async def lifespan(application):
        async with httpx.AsyncClient(base_url=URL, transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(STAGE_SECONDS, connect=5)) as client, httpx.AsyncClient(
                base_url=URL, transport=transport, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(1, connect=.5)) as observer:
            application.state.client, application.state.observer = client, observer
            application.state.lock, application.state.tasks = asyncio.Lock(), set()
            application.state.next_first = "baseline"
            yield
            tasks = list(application.state.tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Day29 · Настройка локальной LLM", lifespan=lifespan)

    @app.exception_handler(APIError)
    async def error(_request, exc):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _exc):
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": "Проверьте вопрос и параметры: temperature 0–2, токены 256/512/768/1024/1536/2048, контекст 3072/4096/6144/8192. Также проверьте расширенные параметры, системную инструкцию, стоп-строки и формат ответа: обычный текст (null), JSON (json) или непустой объект JSON Schema. Дополнительные поля запрещены."}})

    @app.get("/api/state")
    async def state(request: Request):
        result = {"status": "ready", "message": "Локальное сравнение готово.",
                  "model": MODEL, "quantization": None,
                  "profiles": {identifier: profile(identifier) for identifier in ("baseline", "optimized")},
                  "tuning_limits": copy.deepcopy(LIMITS)}
        try:
            async with asyncio.timeout(5):
                await require_model(request.app.state.client)
                result["quantization"] = await metadata(request.app.state.client)
        except APIError as exc:
            result.update(status="unavailable", message=exc.message)
        except TimeoutError:
            result.update(status="unavailable", message="Ollama не ответил на проверку состояния.")
        return result

    async def run_stage(identifier, settings, question, application, deadline):
        began = time.perf_counter()
        result = result_base(identifier, settings)
        parameters = result["parameters"]
        metrics, resources = result["metrics"], result["resources"]
        if parameters["num_ctx"] >= 6144:
            result["context_warning"] = "Увеличенное окно контекста повышает потребление RAM модели."
        stopping = asyncio.Event()
        watcher = None

        async def sample():
            try:
                async with asyncio.timeout(1):
                    accumulate(resources, await resource_sample(application.state.observer, parameters["num_ctx"]))
            except Exception:
                # Optional observation must never invalidate a generation.
                pass

        async def observe():
            while not stopping.is_set():
                await sample()
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=PS_INTERVAL)
                except TimeoutError:
                    pass

        try:
            available = min(STAGE_SECONDS, max(0, deadline - time.monotonic() - .02))
            async with asyncio.timeout(available):
                await require_model(application.state.client)
                preload_start = time.perf_counter()
                try:
                    metrics["preload_load_seconds"] = await preload(application.state.client, parameters)
                finally:
                    metrics["preload_seconds"] = time.perf_counter() - preload_start
                watcher = asyncio.create_task(observe())
                # Start a post-load sample concurrently; an unresponsive /ps cannot delay generation.
                await asyncio.sleep(0)
                actual_messages = ([{"role": "system", "content": result["system"]}] if result["system"] else [])
                actual_messages.append({"role": "user", "content": question})
                result["called"] = True
                http_start = time.perf_counter()
                try:
                    answer, generated, truncated = await generate(application.state.client, actual_messages, parameters)
                finally:
                    metrics["http_seconds"] = time.perf_counter() - http_start
                metrics.update(generated)
                result.update(answer=answer, status="answered", truncated=truncated, reason="")
                if generated["prompt_tokens"] + parameters["num_predict"] > parameters["num_ctx"]:
                    result["context_warning"] = (result["context_warning"] + " " if result["context_warning"] else "") + "Фактический вход и заданный запас ответа превышают окно контекста. Ответ может быть неполным; точная предварительная токенизация не выполнялась."
        except (APIError, TimeoutError) as exc:
            code = exc.code if isinstance(exc, APIError) else "timeout"
            message = exc.message if isinstance(exc, APIError) else "Профиль не ответил за отведённое время. Автоматического повтора не было."
            result.update(status="error", answer=message, reason="provider_error", error={"code": code, "message": message})
        except asyncio.CancelledError:
            raise
        except Exception:
            result.update(status="error", answer="Не удалось получить ответ локальной модели.", reason="provider_error",
                          error={"code": "local_error", "message": "Не удалось получить ответ локальной модели."})
        finally:
            # Fix own latency before stopping the observer: never include waiting for another profile.
            metrics["total_seconds"] = time.perf_counter() - began
            stopping.set()
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
        return result

    async def pipeline(body, application, deadline, compare, order):
        try:
            overrides = body.settings.model_dump(exclude_unset=True)
            chosen = profile("optimized", overrides)
            settings = {key: copy.deepcopy(value) for key, value in chosen["parameters"].items() if key != "keep_alive"}
            settings["system"] = chosen["system"]
            base = {"question": body.question, "mode": "compare" if compare else "optimized", "model": MODEL,
                    "settings": settings, "order": order,
                    "comparison": {"same_input": True, "input_sha256": hashlib.sha256(body.question.encode("utf-8")).hexdigest()}}
            for identifier in order:
                base[identifier] = await run_stage(identifier, settings, body.question, application, deadline)
            return base
        finally:
            application.state.lock.release()

    async def execute(body, request, compare):
        application = request.app
        if application.state.lock.locked():
            raise APIError(409, "busy", "Приложение обрабатывает другой вопрос. Дождитесь завершения.")
        await application.state.lock.acquire()
        if compare:
            first = body.first_profile or application.state.next_first
            application.state.next_first = "optimized" if first == "baseline" else "baseline"
            order = [first, "optimized" if first == "baseline" else "baseline"]
        else:
            order = ["optimized"]
        seconds = COMPARE_SECONDS if compare else ASK_SECONDS
        task = asyncio.create_task(pipeline(body, application, time.monotonic() + seconds, compare, order))
        application.state.tasks.add(task)
        def completed(done):
            application.state.tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(completed)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=seconds)
        except TimeoutError:
            raise APIError(504, "timeout", "Приложение не успело завершить запрос. Дождитесь освобождения модели.") from None

    @app.post("/api/ask")
    async def ask(body: AskRequest, request: Request):
        return await execute(body, request, False)

    @app.post("/api/compare")
    async def compare(body: AskRequest, request: Request):
        return await execute(body, request, True)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def missing(path):
        return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "Такого API-маршрута нет."}})

    built = Path(dist) if dist is not None else HERE / "frontend/dist"
    if built.is_dir():
        app.mount("/", StaticFiles(directory=built, html=True), name="frontend")
    return app


app = create_app()
