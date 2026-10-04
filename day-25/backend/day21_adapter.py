"""Repository README corpus using Day 21's chunking, embedding and SQLite code."""
from __future__ import annotations

import importlib.util
import re
import sys
from threading import Lock
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
DAY21 = HERE.parent / "day-21"
CORPUS = HERE.parent
DB = HERE / "data" / "index.sqlite"
CACHE = HERE / ".cache" / "fastembed"
if str(DAY21) not in sys.path:
    sys.path.insert(0, str(DAY21))
from corpus import CORPUS_KIND, Document, extract_markdown, sources  # noqa: E402
from embedding import FastEmbedder  # noqa: E402
from index_store import read_manifest, search  # noqa: E402

_MODEL_LOCK = Lock()
_EMBEDDERS: dict[tuple, object] = {}


def _snapshot(root: Path):
    root = root.resolve(strict=True)
    documents, details, empty = [], [], 0
    for path in sources(root):
        name = path.relative_to(root).as_posix()
        raw_bytes = path.read_bytes()
        raw = raw_bytes.decode("utf-8")
        sections = extract_markdown(raw)
        heading = re.search(r"(?m)^#\s+(.+)$", raw)
        title = heading.group(1).strip() if heading else name
        document = Document(name, title, sections)
        details.append({"source": name, "title": title, "words": len(document.text.split()),
                        "sections": len(sections)})
        if sections:
            documents.append(document)
        else:
            empty += 1
    return documents, details, empty, corpus_sha256(root)


_spec = importlib.util.spec_from_file_location("day21_pipeline", DAY21 / "backend" / "pipeline.py")
assert _spec and _spec.loader
_pipeline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pipeline)
_grounding_spec = importlib.util.spec_from_file_location("day25_boundary_context", HERE.parent / "day-24" / "rag_core.py")
assert _grounding_spec and _grounding_spec.loader
_grounding = importlib.util.module_from_spec(_grounding_spec)
_grounding_spec.loader.exec_module(_grounding)

corpus_sha256 = _pipeline.corpus_sha256
build_project_index = _pipeline.build_project_index


def cached_embedder(cache_dir: Path, factory=FastEmbedder):
    key = (str(cache_dir.resolve()), factory)
    if key not in _EMBEDDERS:
        _EMBEDDERS[key] = factory(cache_dir)
    return _EMBEDDERS[key]


def build_cached_index(db: Path, corpus: Path, cache_dir: Path, **kwargs):
    with _MODEL_LOCK:
        factory = kwargs.pop("embedder_factory", FastEmbedder)
        return build_project_index(db, corpus, cache_dir,
                                   embedder_factory=lambda path: cached_embedder(path, factory), **kwargs)


def index_state(db: Path = DB, corpus: Path | None = None) -> dict:
    try:
        docs, details, empty, digest = _snapshot(corpus or CORPUS)
        summary = {"kind": CORPUS_KIND, "files": len(docs), "sections": sum(item["sections"] for item in details),
                   "words": sum(item["words"] for item in details), "source_details": details}
        base = {"ready": False, "corpus_ready": bool(docs), "manifest": None,
                "sources": [item["source"] for item in details], "corpus": summary}
        if not docs:
            return {**base, "reason": "README не содержат текста для индексации"}
    except (OSError, ValueError, UnicodeError) as exc:
        return {"ready": False, "corpus_ready": False, "manifest": None,
                "sources": [], "corpus": None, "reason": str(exc)[:220]}
    if not db.is_file():
        return {**base, "reason": "Индекс README ещё не построен"}
    try:
        manifest = read_manifest(db)
        if manifest.get("corpus_kind") != CORPUS_KIND:
            raise ValueError("Индекс другого корпуса: перестройте индекс README проекта")
        if (manifest.get("schema_version") != 1 or manifest.get("corpus_sha256") != digest or
                manifest.get("sources") != base["sources"]):
            raise ValueError("README проекта изменились: перестройте индекс")
        if not all(manifest.get("chunks", {}).get(name, 0) > 0 for name in ("fixed", "structure")):
            raise ValueError("В индексе отсутствует одна из стратегий")
        return {**base, "ready": True, "reason": None, "manifest": manifest}
    except Exception as exc:
        return {**base, "reason": str(exc)[:220]}


def retrieve(question: str, strategy: str, top_k: int, db: Path = DB,
             cache_dir: Path = CACHE, embedder_factory=FastEmbedder,
             corpus: Path | None = None, *, retrieval_scope: dict | None = None) -> list[dict]:
    state = index_state(db, corpus)
    if not state["ready"]:
        raise ValueError(state["reason"])
    with _MODEL_LOCK:
        embedder = cached_embedder(cache_dir, embedder_factory)
        candidates = search(db, embedder, question, strategy, top_k, retrieval_scope=retrieval_scope)
        return _grounding.with_boundary_context(db, strategy, candidates)
