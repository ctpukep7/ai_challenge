"""Day 23: asynchronous, observable retrieve-filter-rewrite comparisons."""
from __future__ import annotations

import copy
import json
import logging
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from time import perf_counter
from typing import Callable, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator

from backend.day21_adapter import CACHE, CORPUS, DB, build_cached_index, index_state, retrieve
from backend.llm import MAX_TOKENS, MODEL, TEMPERATURE, THINKING, DeepSeekClient, LLMError, config_value
from backend.logic import (ABSTENTION, MODES, context_for, evaluate, local_rewrite, messages_for,
                           parse_rewrite, retrieval_evaluation, rewrite_messages,
                           select_candidates, summarize)

from index_store import source_scope

LOGGER = logging.getLogger(__name__)
QUESTIONS = json.loads((Path(__file__).resolve().parents[1] / "questions.json").read_text(encoding="utf-8"))
BY_ID = {item["id"]: item for item in QUESTIONS}
DEFAULTS = {"strategy": "structure", "top_k_before": 12, "top_k_after": 3,
            "threshold": 0.5, "rewrite_method": "local"}


class Settings(BaseModel):
    strategy: Literal["fixed", "structure"] = "structure"
    top_k_before: int = Field(default=12, ge=1, le=30, strict=True)
    top_k_after: int = Field(default=3, ge=1, le=10, strict=True)
    threshold: float = Field(default=0.5, ge=-1, le=1, allow_inf_nan=False)
    rewrite_method: Literal["local", "llm"] = "local"
    generate_answers: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def coherent(self):
        if self.top_k_after > self.top_k_before:
            raise ValueError("Итоговый top-K не может быть больше top-K кандидатов")
        if not self.generate_answers and self.rewrite_method == "llm":
            raise ValueError("Поиск без внешних вызовов требует local rewrite")
        return self


class CompareRequest(Settings):
    question: str = Field(min_length=1, max_length=600)
    question_id: str | None = None

    @model_validator(mode="after")
    def nonblank(self):
        self.question = self.question.strip()
        if not self.question:
            raise ValueError("Введите вопрос")
        if self.question_id is not None and self.question_id not in BY_ID:
            raise ValueError("Неизвестный контрольный вопрос")
        return self


class Cancelled(Exception):
    pass


class Controller:
    """One build or run at a time; published dictionaries are copied under lock."""
    def __init__(self, builder, db, corpus, cache, retriever, llm, clock):
        self.builder, self.db, self.corpus, self.cache = builder, db, corpus, cache
        self.retriever, self.llm, self.clock = retriever, llm, clock
        self.lock = Lock()
        self.busy = False
        self.jobs = OrderedDict()
        self.build = {"status": "idle", "phase": None, "completed": None, "total": None,
                      "events": [], "error": None}

    def acquire(self):
        # Must be called with self.lock held to serialize POSTs and starts.
        if self.busy:
            raise HTTPException(409, "Уже выполняется сборка или сравнение. Дождитесь завершения или отмены.")
        self.busy = True

    def build_snapshot(self):
        with self.lock:
            return copy.deepcopy(self.build)

    def active_run(self):
        with self.lock:
            for job in reversed(self.jobs.values()):
                if job["status"] in {"queued", "running"}:
                    return {"id": job["id"], "status": job["status"]}
            return None

    def start_build(self):
        state = index_state(self.db, self.corpus)
        if not state["corpus_ready"]:
            raise HTTPException(409, state["reason"])
        with self.lock:
            self.acquire()
            self.build = {"status": "running", "phase": "queued", "completed": 0,
                          "total": None, "events": [], "error": None}
        try:
            Thread(target=self._build_worker, daemon=True, name="day23-build").start()
        except RuntimeError as exc:
            with self.lock:
                self.busy = False
                self.build.update(status="failed", error="Не удалось запустить сборку")
            raise HTTPException(503, "Не удалось запустить сборку") from exc

    def _build_worker(self):
        def progress(phase, completed, total, message):
            with self.lock:
                self.build.update(phase=phase, completed=completed, total=total)
                self.build["events"] = [*self.build["events"],
                    {"phase": phase, "message": message, "completed": completed, "total": total}][-60:]
        try:
            self.builder(self.db, self.corpus, self.cache, progress=progress)
            progress("complete", 1, 1, "Индекс Day 23 готов")
            with self.lock:
                self.build["status"] = "completed"
        except Exception as exc:
            LOGGER.exception("Index build failed")
            with self.lock:
                self.build.update(status="failed", error=(str(exc)[:250] if isinstance(exc, (ValueError, FileNotFoundError))
                                  else "Не удалось построить индекс; проверьте модель и корпус"))
        finally:
            with self.lock:
                self.busy = False

    def start_run(self, kind, settings, questions):
        state = index_state(self.db, self.corpus)
        if not state["ready"]:
            raise HTTPException(409, f"Сначала постройте индекс. {state['reason']}")
        identifier = uuid.uuid4().hex
        with self.lock:
            self.acquire()
            self.jobs[identifier] = {"id": identifier, "kind": kind, "status": "queued", "phase": "queued",
                "current": 0, "total": len(questions), "settings": settings, "error": None,
                "events": [], "results": [], "summary": summarize([], {"answer": 0, "rewrite": 0}),
                "cancel_requested": False, "model": MODEL, "temperature": TEMPERATURE,
                "max_tokens": MAX_TOKENS, "thinking": THINKING}
            while len(self.jobs) > 20:
                self.jobs.popitem(last=False)
        try:
            Thread(target=self._run_worker, args=(identifier, questions), daemon=True,
                   name=f"day23-{kind}").start()
        except RuntimeError as exc:
            with self.lock:
                self.busy = False
                self.jobs[identifier].update(status="failed", error="Не удалось запустить сравнение")
            raise HTTPException(503, "Не удалось запустить сравнение") from exc
        return {"id": identifier}

    def run_snapshot(self, identifier):
        with self.lock:
            if identifier not in self.jobs:
                raise HTTPException(404, "Прогон не найден или удалён из ограниченной истории")
            return copy.deepcopy(self.jobs[identifier])

    def cancel(self, identifier):
        with self.lock:
            if identifier not in self.jobs:
                raise HTTPException(404, "Прогон не найден")
            job = self.jobs[identifier]
            if job["status"] in {"queued", "running"}:
                job["cancel_requested"] = True
            return {"id": identifier, "status": job["status"], "cancel_requested": job["cancel_requested"]}

    def check_cancel(self, identifier):
        with self.lock:
            if self.jobs[identifier]["cancel_requested"]:
                raise Cancelled()

    def event(self, identifier, phase, message):
        with self.lock:
            job = self.jobs[identifier]
            job["phase"] = phase
            job["events"] = [*job["events"], {"phase": phase, "message": message,
                "at": datetime.now(timezone.utc).isoformat()}][-100:]

    def publish(self, identifier, row, position, calls):
        with self.lock:
            job = self.jobs[identifier]
            if len(job["results"]) <= position:
                job["results"].append(copy.deepcopy(row))
            else:
                job["results"][position] = copy.deepcopy(row)
            job["summary"] = summarize(job["results"], calls)

    def _search(self, query, settings, scope):
        started = self.clock()
        chunks = self.retriever(query, settings["strategy"], settings["top_k_before"], self.db, self.cache,
                                retrieval_scope=scope)
        elapsed = max(0, round((self.clock() - started) * 1000, 1))
        return chunks, elapsed

    def _mode(self, original, query, candidates, elapsed, settings, fixture, filtering, scope):
        annotated, selected, counts = select_candidates(candidates, settings["threshold"],
                                                       settings["top_k_after"], filtering)
        messages = messages_for(original, selected)
        return {"query": query, "retrieval_scope": copy.deepcopy(scope), "candidates": annotated, "chunks": selected, "counts": counts,
                "retrieval_ms": elapsed, "context_chars": len(context_for(selected)), "messages": messages,
                "prompt_chars": sum(len(item["content"]) for item in messages),
                "answer": None, "answer_status": "not_requested", "error": None, "elapsed_ms": None,
                "usage": None, "evaluation": None, "retrieval_evaluation": retrieval_evaluation(fixture, selected),
                "generated_by": None}

    @staticmethod
    def _error_mode(query, error, scope):
        return {"query": query, "retrieval_scope": copy.deepcopy(scope), "candidates": [], "chunks": [], "counts": None,
                "retrieval_ms": None, "context_chars": 0, "messages": [], "prompt_chars": 0,
                "answer": None, "answer_status": "failed", "error": error, "elapsed_ms": None,
                "usage": None, "evaluation": None, "retrieval_evaluation": None, "generated_by": None}

    @staticmethod
    def safe_error(exc, message):
        return str(exc)[:250] if isinstance(exc, (ValueError, LLMError)) else message

    def _rewrite(self, original, settings, calls):
        if settings["rewrite_method"] == "local":
            started = self.clock()
            result = local_rewrite(original)
            result["elapsed_ms"] = max(0, round((self.clock() - started) * 1000, 1))
            return result
        started = self.clock()
        messages = rewrite_messages(original)
        calls["rewrite"] += 1
        result = {"original": original, "query": None, "method": "llm", "changed": None,
                  "applied_rules": [], "status": "failed", "error": None, "elapsed_ms": None,
                  "messages": messages, "usage": None}
        try:
            response = self.llm(messages)
            result["usage"] = response.get("usage")
            result["query"] = parse_rewrite(response["answer"])
            result.update(status="completed", changed=result["query"] != original)
        except Exception as exc:
            result["error"] = self.safe_error(exc, "Не удалось переписать поисковый запрос")
        result["elapsed_ms"] = max(0, round((self.clock() - started) * 1000, 1))
        return result

    def _run_worker(self, identifier, questions):
        calls = {"answer": 0, "rewrite": 0}
        with self.lock:
            job = self.jobs[identifier]
            job["status"] = "running"
            settings = copy.deepcopy(job["settings"])
        try:
            for position, question in enumerate(questions):
                self.check_cancel(identifier)
                fixture = question.get("fixture")
                original = question["question"]
                scope = source_scope(original)
                row = {"question_id": fixture["id"] if fixture else None, "question": original,
                       "rewrite": None, "modes": {name: None for name in MODES}}
                self.publish(identifier, row, position, calls)
                self.event(identifier, "retrieval", f"Вопрос {position + 1}/{len(questions)}: поиск исходного запроса")
                try:
                    candidates, elapsed = self._search(original, settings, scope)
                    for name, filtering in (("baseline", False), ("filtered", True)):
                        row["modes"][name] = self._mode(original, original, candidates, elapsed,
                                                       settings, fixture, filtering, scope)
                except Exception as exc:
                    error = self.safe_error(exc, "Локальный поиск не удался; проверьте индекс и модель")
                    for name in ("baseline", "filtered"):
                        row["modes"][name] = self._error_mode(original, error, scope)
                self.publish(identifier, row, position, calls)
                self.check_cancel(identifier)
                self.event(identifier, "rewrite", "Переписывание только поискового запроса")
                row["rewrite"] = self._rewrite(original, settings, calls)
                self.publish(identifier, row, position, calls)
                self.check_cancel(identifier)
                rewritten = row["rewrite"]["query"]
                if row["rewrite"]["status"] == "failed":
                    row["modes"]["rewritten"] = self._error_mode(None, row["rewrite"]["error"], scope)
                else:
                    self.event(identifier, "rewritten_retrieval", "Поиск переписанного запроса и фильтрация")
                    try:
                        new_candidates, elapsed = self._search(rewritten, settings, scope)
                        row["modes"]["rewritten"] = self._mode(original, rewritten, new_candidates,
                                                              elapsed, settings, fixture, True, scope)
                    except Exception as exc:
                        error = self.safe_error(exc, "Поиск переписанного запроса не удался")
                        row["modes"]["rewritten"] = self._error_mode(rewritten, error, scope)
                self.publish(identifier, row, position, calls)
                if settings["generate_answers"]:
                    for name in MODES:
                        self.check_cancel(identifier)
                        mode = row["modes"][name]
                        if mode["error"]:
                            continue
                        if not mode["chunks"]:
                            mode.update(answer=ABSTENTION, answer_status="abstained", generated_by="policy", elapsed_ms=0)
                            self.event(identifier, "abstention", f"{name}: пустой контекст, отказ приложения без вызова LLM")
                        else:
                            self.event(identifier, "generation", f"{name}: запрос к DeepSeek с выбранными чанками")
                            started = self.clock()
                            calls["answer"] += 1
                            # Publish attempted call count immediately, while the HTTP call is active.
                            self.publish(identifier, row, position, calls)
                            try:
                                response = self.llm(mode["messages"])
                                answer = response["answer"]
                                if not isinstance(answer, str) or not answer.strip():
                                    raise LLMError("Модель вернула пустой ответ")
                                mode.update(answer=answer, usage=response.get("usage"), answer_status="completed", generated_by="llm",
                                            evaluation=evaluate(answer, fixture, mode["chunks"]))
                            except Exception as exc:
                                mode.update(answer_status="failed", error=self.safe_error(exc, "Не удалось получить ответ DeepSeek"))
                            mode["elapsed_ms"] = max(0, round((self.clock() - started) * 1000, 1))
                        self.publish(identifier, row, position, calls)
                self.check_cancel(identifier)
                with self.lock:
                    self.jobs[identifier]["current"] = position + 1
            self.event(identifier, "complete", "Сравнение завершено")
            with self.lock:
                self.jobs[identifier]["status"] = "completed"
        except Cancelled:
            self.event(identifier, "cancelled", "Прогон отменён; завершённые этапы сохранены")
            with self.lock:
                self.jobs[identifier]["status"] = "cancelled"
        except Exception:
            LOGGER.exception("Comparison worker failed")
            with self.lock:
                self.jobs[identifier].update(status="failed", error="Не удалось завершить сравнение")
        finally:
            with self.lock:
                self.jobs[identifier]["summary"] = summarize(self.jobs[identifier]["results"], calls)
                self.busy = False


def create_app(db: Path = DB, corpus: Path = CORPUS, cache: Path = CACHE,
               builder: Callable = build_cached_index, retriever: Callable = retrieve,
               llm: Callable | None = None, clock: Callable = perf_counter) -> FastAPI:
    owned_llm = DeepSeekClient() if llm is None else None
    service = llm if llm is not None else owned_llm
    controller = Controller(builder, db, corpus, cache, retriever, service, clock)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if owned_llm is not None:
                owned_llm.close()

    app = FastAPI(title="Day 23 — Реранкинг и фильтрация", version="1.0", lifespan=lifespan)
    app.state.controller = controller

    @app.get("/api/state")
    def state():
        return {"index": index_state(db, corpus), "build": controller.build_snapshot(), "defaults": DEFAULTS,
                "active_run": controller.active_run(),
                "model": MODEL, "temperature": TEMPERATURE, "max_tokens": MAX_TOKENS, "thinking": THINKING,
                "llm_configured": bool(config_value("DEEPSEEK_API_KEY")), "questions": QUESTIONS}

    @app.post("/api/build", status_code=202)
    def build():
        controller.start_build()
        return {"status": "running"}

    @app.get("/api/build")
    def build_status():
        return controller.build_snapshot()

    @app.post("/api/compare", status_code=202)
    def compare(request: CompareRequest):
        fixture = BY_ID.get(request.question_id)
        if fixture and fixture["question"] != request.question:
            fixture = None
        return controller.start_run("compare", request.model_dump(exclude={"question", "question_id"}),
                                    [{"question": request.question, "fixture": fixture}])

    @app.post("/api/benchmark", status_code=202)
    def benchmark(request: Settings):
        return controller.start_run("benchmark", request.model_dump(),
                                    [{"question": item["question"], "fixture": item} for item in QUESTIONS])

    @app.get("/api/runs/{identifier}")
    def run(identifier: str):
        return controller.run_snapshot(identifier)

    @app.post("/api/runs/{identifier}/cancel")
    def cancel(identifier: str):
        return controller.cancel(identifier)

    return app


app = create_app()
