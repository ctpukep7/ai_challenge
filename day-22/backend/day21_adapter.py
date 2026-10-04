"""Use Day 21's exact index builder and search against Day 22-owned data."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
DAY21 = HERE.parent / "day-21"
CORPUS = HERE.parent
DB = HERE / "data" / "index.sqlite"
CACHE = HERE / ".cache" / "fastembed"

# Day 21 uses top-level local imports; keep its source as the single implementation.
if str(DAY21) not in sys.path:
    sys.path.insert(0, str(DAY21))

from corpus import CORPUS_KIND, sources  # noqa: E402
from embedding import FastEmbedder  # noqa: E402
from index_store import read_manifest, search  # noqa: E402

_spec = importlib.util.spec_from_file_location("day21_pipeline", DAY21 / "backend" / "pipeline.py")
assert _spec and _spec.loader
_pipeline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pipeline)
build_project_index = _pipeline.build_project_index


def corpus_sha256(root: Path | None = None) -> str:
    return _pipeline.corpus_sha256(root or CORPUS)


def index_state(db: Path = DB, corpus: Path | None = None) -> dict:
    corpus = corpus or CORPUS
    try:
        sources(corpus)
    except (FileNotFoundError, ValueError) as exc:
        return {"ready": False, "corpus_ready": False, "manifest": None,
                "reason": str(exc)}
    if not db.is_file():
        return {"ready": False, "corpus_ready": True,
                "reason": "Индекс ещё не построен", "manifest": None}
    try:
        manifest = read_manifest(db)
        if manifest.get("schema_version") != 1 or manifest.get("corpus_kind") != CORPUS_KIND or manifest.get("corpus_sha256") != corpus_sha256(corpus):
            raise ValueError("Корпус изменился: перестройте индекс")
        if not all(manifest.get("chunks", {}).get(name, 0) > 0 for name in ("fixed", "structure")):
            raise ValueError("В индексе отсутствует одна из стратегий")
        return {"ready": True, "corpus_ready": True, "reason": None, "manifest": manifest}
    except Exception as exc:
        return {"ready": False, "corpus_ready": True,
                "reason": str(exc)[:220], "manifest": None}


def retrieve(question: str, strategy: str, top_k: int, db: Path = DB,
             cache_dir: Path = CACHE, embedder_factory=FastEmbedder,
             corpus: Path | None = None) -> list[dict]:
    state = index_state(db, corpus)
    if not state["ready"]:
        raise ValueError(state["reason"])
    embedder = embedder_factory(cache_dir)
    return search(db, embedder, question, strategy, top_k)
