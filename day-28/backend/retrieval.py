"""Reuse only local index/search primitives; never rebuild or download."""
import importlib.util
import os
import sys
import time
from importlib.metadata import version
from pathlib import Path

from .errors import APIError

HERE = Path(__file__).resolve().parents[1]
PROJECT = HERE.parent
DAY21 = PROJECT / "day-21"
DB = DAY21 / "data/index.sqlite"
CACHE = DAY21 / ".cache/fastembed"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
if str(DAY21) not in sys.path:
    sys.path.insert(0, str(DAY21))
from embedding import FastEmbedder, MODEL_NAME, DIMENSION, MODEL_LIMIT
from index_store import read_manifest, search, source_scope
from corpus import CORPUS_KIND


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


corpus_sha256 = load_module("day28_shared_pipeline", DAY21 / "backend/pipeline.py").corpus_sha256
with_boundary_context = load_module("day28_boundary", PROJECT / "day-24/rag_core.py").with_boundary_context


class OfflineEmbedder(FastEmbedder):
    def __init__(self, cache_dir):
        if not cache_dir.is_dir() or not any(cache_dir.iterdir()):
            raise APIError(503, "embedding_unavailable", "Локальный embedding-кэш Day21 отсутствует. Автоматическая загрузка отключена.")
        import onnxruntime
        from fastembed import TextEmbedding
        onnxruntime.disable_telemetry_events()
        self.fastembed_version = version("fastembed")
        self.model = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(cache_dir),
                                   local_files_only=True, threads=2, providers=["CPUExecutionProvider"])
        self.tokenizer = self.model.model.tokenizer
        truncation = self.tokenizer.truncation
        if not truncation or truncation.get("max_length") != MODEL_LIMIT:
            raise APIError(503, "embedding_incompatible", "Не совпадает лимит локального embedding-токенизатора.")
        self.tokenizer.no_truncation()


class Retriever:
    def __init__(self, db=DB, cache=CACHE, corpus=PROJECT, embedder_factory=OfflineEmbedder, digest=corpus_sha256):
        self.db, self.cache, self.corpus = Path(db), Path(cache), Path(corpus)
        self.embedder_factory, self.digest = embedder_factory, digest
        self.embedder = None

    def index_state(self):
        base = {"ready": False, "files": 0, "sections": 0, "chunks": {}, "dimension": None,
                "model": MODEL_NAME, "corpus_sha256": None, "current_corpus_sha256": None, "snapshot_differs": False}
        try:
            manifest = read_manifest(self.db)
            if (manifest.get("schema_version") != 1 or manifest.get("corpus_kind") != CORPUS_KIND or
                    manifest.get("model") != MODEL_NAME or manifest.get("dimension") != DIMENSION or
                    manifest.get("fastembed_version") != version("fastembed") or
                    manifest.get("chunks", {}).get("structure", 0) < 1):
                raise ValueError("incompatible manifest")
            if not self.cache.is_dir() or not any(self.cache.iterdir()):
                base["reason"] = "Отсутствует локальный embedding-кэш Day21. Скачивание отключено."
                return base
            digest = self.digest(self.corpus)
            return {"ready": True, "files": manifest["files"], "sections": manifest.get("sections", manifest.get("chapters", 0)),
                    "chunks": manifest["chunks"], "dimension": manifest["dimension"], "model": manifest["model"],
                    "corpus_sha256": manifest["corpus_sha256"], "current_corpus_sha256": digest,
                    "snapshot_differs": manifest["corpus_sha256"] != digest}
        except Exception:
            base["reason"] = "Индекс Day21 отсутствует, повреждён или несовместим с локальной embedding-моделью."
            return base

    def retrieve(self, question):
        start = time.perf_counter()
        if not self.index_state()["ready"]:
            raise APIError(503, "index_unavailable", "Локальный индекс или embedding-кэш недоступен. Проверьте Day21.")
        cold, load = self.embedder is None, 0.0
        if cold:
            loaded = time.perf_counter()
            try:
                self.embedder = self.embedder_factory(self.cache)
            except APIError:
                raise
            except Exception:
                raise APIError(503, "embedding_unavailable", "Не удалось открыть локальный embedding-кэш. Загрузка из сети отключена.") from None
            load = time.perf_counter() - loaded
        if self.embedder.count(question) > self.embedder.max_tokens:
            raise APIError(422, "embedding_query_too_long", "Вопрос превышает лимит embedding-модели. Сократите формулировку.")
        scope = source_scope(question)
        try:
            candidates = search(self.db, self.embedder, question, "structure", 12, retrieval_scope=scope)
            anchors = [item for item in candidates if item["score"] >= .45][:5]
            enriched = with_boundary_context(self.db, "structure", anchors)
        except Exception:
            raise APIError(503, "index_unavailable", "Ошибка чтения локального индекса. Индекс не изменялся.") from None
        evidence, seen = [], set()
        for anchor in enriched:
            if anchor["chunk_id"] not in seen:
                evidence.append({**anchor, "role": "anchor"})
                seen.add(anchor["chunk_id"])
        for anchor in enriched:
            for neighbor in anchor.get("boundary_context", {}).values():
                if neighbor is None or neighbor["chunk_id"] in seen:
                    continue
                if (neighbor["source"] != anchor["source"] or neighbor["strategy"] != "structure" or
                        neighbor["section"] != anchor["section"] or abs(neighbor["ordinal"] - anchor["ordinal"]) != 1):
                    continue
                evidence.append({**neighbor, "title": anchor["title"], "score": None, "role": "neighbor"})
                seen.add(neighbor["chunk_id"])
        return {"evidence": evidence[:15], "scope": scope, "embedding_cold": cold,
                "embedding_load_seconds": load, "retrieval_seconds": time.perf_counter() - start}
