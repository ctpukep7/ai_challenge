"""Local FastAPI interface for the real Day 21 corpus and SQLite index."""

from __future__ import annotations

import copy
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from typing import Callable, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from backend.pipeline import build_project_index, corpus_sha256
from corpus import CORPUS_KIND, load_corpus, sources
from embedding import FastEmbedder
from index_store import list_chunks, read_manifest, search, source_scope, strategy_stats

LOGGER = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parents[1]
CORPUS_SETUP = "Корпус — отслеживаемые README.md репозитория и дней 01–20; проверьте файлы Git."


class BuildRequest(BaseModel):
    overlap_ratio: float = Field(default=0.18, ge=0, le=0.4, allow_inf_nan=False)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    top_k: int = Field(default=3, ge=1, le=5)


class BuildController:
    """One background build at a time; snapshots are safe for concurrent reads."""

    def __init__(self, builder: Callable, db: Path, corpus: Path, cache_dir: Path):
        self._builder = builder
        self._db = db
        self._corpus = corpus
        self._cache_dir = cache_dir
        self._lock = Lock()
        self._job = self._empty_job()

    @staticmethod
    def _empty_job() -> dict:
        return {"id": None, "status": "idle", "phase": None, "completed": None,
                "total": None, "error": None, "events": []}

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._job)

    def reset(self) -> None:
        """Remove this controller's completed index without racing its build."""
        with self._lock:
            if self._job["status"] == "running":
                raise HTTPException(status_code=409, detail="Дождитесь завершения сборки демо-индекса")
            try:
                self._db.unlink(missing_ok=True)
            except OSError as exc:
                LOGGER.exception("Could not reset Day 21 demo index")
                raise HTTPException(status_code=500, detail="Не удалось очистить демо-индекс") from exc
            self._job = self._empty_job()

    def _event(self, phase: str, completed: int | None, total: int | None,
               message: str) -> None:
        event = {"phase": phase, "message": message, "completed": completed,
                 "total": total, "at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            self._job.update(phase=phase, completed=completed, total=total)
            self._job["events"] = [*self._job["events"], event][-32:]

    def start(self, overlap_ratio: float) -> str:
        with self._lock:
            if self._job["status"] == "running":
                raise HTTPException(status_code=409, detail="Сборка уже выполняется")
            job_id = str(uuid4())
            self._job = {"id": job_id, "status": "running", "phase": "queued",
                         "completed": None, "total": None, "error": None,
                         "events": [{"phase": "queued", "message": "Сборка поставлена в очередь",
                                     "completed": None, "total": None,
                                     "at": datetime.now(timezone.utc).isoformat()}]}
        thread = Thread(target=self._run, args=(overlap_ratio,), daemon=True,
                        name=f"day21-build-{job_id[:8]}")
        try:
            thread.start()
        except RuntimeError:
            with self._lock:
                self._job.update(status="failed", phase="failed",
                                 error="Не удалось запустить фоновую сборку")
            raise HTTPException(status_code=503, detail="Не удалось запустить сборку") from None
        return job_id

    def _run(self, overlap_ratio: float) -> None:
        try:
            self._builder(self._db, self._corpus, self._cache_dir,
                          overlap=overlap_ratio, progress=self._event)
        except Exception as exc:
            LOGGER.exception("Day 21 index build failed")
            if isinstance(exc, FileNotFoundError):
                error = "Отсутствует файл корпуса"
            elif isinstance(exc, ValueError):
                error = str(exc)[:300]
            else:
                error = "Не удалось построить индекс; проверьте модель и файлы корпуса"
            self._event("failed", None, None, "Сборка завершилась ошибкой")
            with self._lock:
                self._job.update(status="failed", error=error)
        else:
            self._event("complete", 1, 1, "Индекс готов")
            with self._lock:
                self._job["status"] = "completed"


def create_app(db: Path | None = None, corpus: Path | None = None,
               cache_dir: Path | None = None,
               builder: Callable = build_project_index,
               embedder_factory: Callable = FastEmbedder) -> FastAPI:
    """Create a local-only API; no path is accepted from HTTP clients."""
    db = db or HERE / "data" / "index.sqlite"
    corpus = corpus or HERE.parent
    cache_dir = cache_dir or HERE / ".cache" / "fastembed"
    demo_db = db.with_name("demo-index.sqlite")
    if demo_db == db:
        raise ValueError("Основной индекс должен отличаться от демо-индекса")
    databases = {"main": db, "demo": demo_db}
    controllers = {workspace: BuildController(builder, path, corpus, cache_dir)
                   for workspace, path in databases.items()}
    app = FastAPI(title="Day 21 — Индексация документов", version="1.0")

    def current_manifest(selected_db: Path) -> dict:
        try:
            sources(corpus)
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        manifest = read_manifest(selected_db)
        if (manifest.get("schema_version") != 1
                or manifest.get("corpus_kind") != CORPUS_KIND
                or manifest.get("corpus_sha256") != corpus_sha256(corpus)):
            raise ValueError("Корпус изменился: перестройте индекс")
        if not all(manifest.get("chunks", {}).get(name, 0) > 0
                   for name in ("fixed", "structure")):
            raise ValueError("В индексе отсутствует одна из стратегий: перестройте индекс")
        return manifest

    @app.get("/api/state")
    def state(workspace: Literal["main", "demo"] = Query(default="main")) -> dict:
        selected_db = databases[workspace]
        try:
            try:
                documents, _ = load_corpus(corpus)
            except (FileNotFoundError, ValueError) as exc:
                documents = []
                corpus_error = str(exc)
            else:
                corpus_error = None
            document_items = []
            for document in documents:
                document_items.append({
                    "source": document.source,
                    "title": document.title,
                    "sections": [section.title for section in document.sections],
                    "words": len(document.text.split()),
                    "excerpt": " ".join(document.text.split())[:240],
                })
            words = sum(item["words"] for item in document_items)
            corpus_info = {"available": bool(document_items) and corpus_error is None,
                           "documents": document_items, "files": len(document_items),
                           "kind": CORPUS_KIND, "chapters": sum(len(item["sections"]) for item in document_items),
                           "words": words, "pages": round(words / 500, 2)}
            if not corpus_info["available"]:
                corpus_info["reason"] = corpus_error or CORPUS_SETUP
            index = {"ready": False, "exists": False,
                     "file": f"data/{selected_db.name}", "manifest": None,
                     "strategies": {"fixed": None, "structure": None}}
            if selected_db.is_file():
                try:
                    manifest = current_manifest(selected_db)
                except ValueError as exc:
                    index.update(exists=True, reason=str(exc))
                else:
                    index = {"ready": True, "exists": True,
                             "file": f"data/{selected_db.name}", "manifest": manifest,
                             "strategies": {strategy: strategy_stats(selected_db, strategy)
                                            for strategy in ("fixed", "structure")}}
            return {"corpus": corpus_info, "index": index,
                    "build": controllers[workspace].snapshot()}
        except (OSError, ValueError, sqlite3.Error) as exc:
            LOGGER.exception("Could not read Day 21 state")
            raise HTTPException(status_code=500, detail="Не удалось прочитать корпус или индекс") from exc

    @app.post("/api/build", status_code=202)
    def start_build(request: BuildRequest,
                    workspace: Literal["main", "demo"] = Query(default="main")) -> dict:
        try:
            sources(corpus)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": controllers[workspace].start(request.overlap_ratio)}

    @app.get("/api/build")
    def build_status(workspace: Literal["main", "demo"] = Query(default="main")) -> dict:
        return controllers[workspace].snapshot()

    @app.post("/api/demo/reset")
    def reset_demo() -> dict:
        controllers["demo"].reset()
        return {"status": "idle"}

    @app.get("/api/chunks")
    def chunks(strategy: Literal["fixed", "structure"] = "fixed",
               source: str | None = None,
               page: int = Query(default=1, ge=1),
               limit: int = Query(default=20, ge=1, le=50),
               workspace: Literal["main", "demo"] = Query(default="main")) -> dict:
        selected_db = databases[workspace]
        try:
            allowed = {path.relative_to(corpus.resolve()).as_posix() for path in sources(corpus)}
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if source is not None and source not in allowed:
            raise HTTPException(status_code=422, detail="Неизвестный файл корпуса")
        if not selected_db.is_file():
            return {"items": [], "total": 0, "page": page, "limit": limit}
        try:
            current_manifest(selected_db)
            return list_chunks(selected_db, strategy, source, page, limit)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (OSError, sqlite3.Error) as exc:
            LOGGER.exception("Could not read Day 21 chunks")
            raise HTTPException(status_code=500, detail="Не удалось прочитать чанки") from exc

    @app.post("/api/search")
    def search_index(request: SearchRequest,
                     workspace: Literal["main", "demo"] = Query(default="main")) -> dict:
        selected_db = databases[workspace]
        query = request.query.strip()
        if not query:
            raise HTTPException(status_code=422, detail="Введите поисковый запрос")
        if not selected_db.is_file():
            raise HTTPException(status_code=409, detail="Сначала постройте индекс")
        try:
            current_manifest(selected_db)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            embedder = embedder_factory(cache_dir)
            results = {strategy: search(selected_db, embedder, query, strategy, request.top_k)
                       for strategy in ("fixed", "structure")}
            return {"query": query, "retrieval_scope": source_scope(query), "results": results}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            LOGGER.exception("Day 21 search failed")
            raise HTTPException(status_code=503, detail="Поиск недоступен; проверьте модель и индекс") from exc

    return app


app = create_app()
