"""Local API: build an index, retrieve source chunks, compare two LLM calls."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock, Thread
from time import perf_counter
from typing import Callable, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from backend.day21_adapter import CACHE, CORPUS, DB, build_project_index, index_state, retrieve
from backend.llm import MAX_TOKENS, MODEL, TEMPERATURE, THINKING, DeepSeekClient, LLMError
from backend.logic import context_for, evaluate, messages_for
from index_store import source_scope

LOGGER = logging.getLogger(__name__)
QUESTIONS = json.loads((Path(__file__).resolve().parents[1] / "questions.json").read_text(encoding="utf-8"))
BY_ID = {item["id"]: item for item in QUESTIONS}


class CompareRequest(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    strategy: Literal["fixed", "structure"] = "structure"
    top_k: int = Field(default=5, ge=1, le=5)
    question_id: str | None = None
    modes: list[Literal["without_rag", "with_rag"]] | None = None


class BuildController:
    def __init__(self, builder: Callable, db: Path, corpus: Path, cache: Path):
        self.builder, self.db, self.corpus, self.cache = builder, db, corpus, cache
        self.lock = Lock()
        self.job = {"status": "idle", "phase": None, "completed": None, "total": None,
                    "events": [], "error": None}

    def snapshot(self) -> dict:
        with self.lock:
            return {**self.job, "events": [*self.job["events"]]}

    def report(self, phase: str, completed: int | None, total: int | None, message: str) -> None:
        with self.lock:
            self.job.update(phase=phase, completed=completed, total=total)
            self.job["events"] = [*self.job["events"], {"phase": phase, "message": message,
                                                        "completed": completed, "total": total}][-40:]

    def start(self) -> None:
        corpus_state = index_state(self.db, self.corpus)
        if not corpus_state["corpus_ready"]:
            raise HTTPException(409, corpus_state["reason"])
        with self.lock:
            if self.job["status"] == "running":
                raise HTTPException(409, "Сборка уже выполняется")
            self.job = {"status": "running", "phase": "queued", "completed": None, "total": None,
                        "events": [], "error": None}
        try:
            Thread(target=self._run, daemon=True, name="day22-index-build").start()
        except RuntimeError as exc:
            with self.lock:
                self.job.update(status="failed", error="Не удалось запустить сборку")
            raise HTTPException(503, "Не удалось запустить сборку") from exc

    def _run(self) -> None:
        try:
            self.builder(self.db, self.corpus, self.cache, progress=self.report)
        except Exception as exc:
            LOGGER.exception("Index build failed")
            with self.lock:
                self.job.update(status="failed", error=(
                    str(exc)[:250] if isinstance(exc, (ValueError, FileNotFoundError))
                    else "Сборка не удалась; проверьте подключение для первой загрузки модели"
                ))
        else:
            with self.lock:
                self.job.update(status="completed", phase="complete", completed=1, total=1)
                self.job["events"] = [*self.job["events"], {"phase": "complete",
                                                            "message": "Индекс готов",
                                                            "completed": 1, "total": 1}]


def create_app(db: Path = DB, corpus: Path = CORPUS, cache: Path = CACHE,
               builder: Callable = build_project_index,
               retriever: Callable = retrieve,
               llm: Callable | None = None,
               clock: Callable[[], float] = perf_counter) -> FastAPI:
    owned_llm = DeepSeekClient() if llm is None else None
    llm = llm or owned_llm

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if owned_llm is not None:
                owned_llm.close()

    app = FastAPI(title="Day 22 — Первый RAG-запрос", version="1.0", lifespan=lifespan)
    build = BuildController(builder, db, corpus, cache)

    @app.get("/api/state")
    def state() -> dict:
        return {"index": index_state(db, corpus), "build": build.snapshot(),
                "model": MODEL, "temperature": TEMPERATURE, "max_tokens": MAX_TOKENS,
                "thinking": THINKING,
                "questions": QUESTIONS}

    @app.post("/api/build", status_code=202)
    def start_build() -> dict:
        build.start()
        return {"status": "running"}

    @app.get("/api/build")
    def build_status() -> dict:
        return build.snapshot()

    @app.post("/api/compare")
    def compare(request: CompareRequest) -> dict:
        question = request.question.strip()
        if not question:
            raise HTTPException(422, "Введите вопрос")
        if request.modes is not None and (not request.modes or len(set(request.modes)) != len(request.modes)):
            raise HTTPException(422, "Укажите один или два разных режима")
        state = index_state(db, corpus)
        if not state["ready"]:
            raise HTTPException(409, f"Сначала постройте индекс. {state['reason']}")
        scope = source_scope(question)
        retrieval_started = clock()
        try:
            chunks = retriever(question, request.strategy, request.top_k, db, cache)
        except ValueError as exc:
            raise HTTPException(422, str(exc)[:250]) from exc
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            LOGGER.exception("Retrieval failed")
            raise HTTPException(503, "Поиск недоступен; проверьте модель и индекс") from exc
        retrieval_ms = max(0.0, round((clock() - retrieval_started) * 1000, 1))
        if not chunks:
            if scope["sources"] is not None:
                raise HTTPException(422, "Для явно указанного дня нет индексированного README: "
                                    + ", ".join(scope["sources"]))
            raise HTTPException(503, "Поиск не вернул чанки; RAG-ответ невозможен")

        fixture = BY_ID.get(request.question_id) if request.question_id else None
        if fixture and fixture["question"] != question:
            fixture = None
        modes = {}
        for name, selected in (("without_rag", None), ("with_rag", chunks)):
            if request.modes is not None and name not in request.modes:
                modes[name] = None
                continue
            messages = messages_for(question, selected)
            prompt_chars = sum(len(message["content"]) for message in messages)
            call_started = clock()
            try:
                result = llm(messages)
                answer, usage, error = result["answer"], result.get("usage"), None
            except LLMError as exc:
                answer, usage, error = None, None, str(exc)
            except Exception:
                LOGGER.exception("LLM call failed")
                answer, usage, error = None, None, "Не удалось получить ответ модели"
            elapsed_ms = max(0.0, round((clock() - call_started) * 1000, 1))
            modes[name] = {"answer": answer, "usage": usage, "error": error,
                           "messages": messages,
                           "elapsed_ms": elapsed_ms, "prompt_chars": prompt_chars,
                           "evaluation": evaluate(answer, fixture, selected) if fixture else None}
        return {"question": question, "question_id": fixture["id"] if fixture else None,
                "strategy": request.strategy, "top_k": request.top_k,
                "model": MODEL, "temperature": TEMPERATURE, "max_tokens": MAX_TOKENS,
                "thinking": THINKING, "retrieval_ms": retrieval_ms, "retrieval_scope": scope,
                "chunks": [{**chunk, "marker": f"[{i}]"} for i, chunk in enumerate(chunks, 1)],
                "context": context_for(chunks), "without_rag": modes["without_rag"],
                "with_rag": modes["with_rag"]}

    return app


app = create_app()
