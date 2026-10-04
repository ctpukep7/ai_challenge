"""Shared CLI/API index construction with measurable stage callbacks."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from chunking import make_chunks
from corpus import CORPUS_KIND, load_corpus, sources
from embedding import FastEmbedder
from index_store import Progress, build_index


def corpus_sha256(corpus: Path) -> str:
    """Hash the explicit source names and bytes, exactly as the index manifest does."""
    digest = hashlib.sha256()
    for path in sources(corpus):
        name = path.relative_to(corpus.resolve()).as_posix().encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        raw = path.read_bytes()
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def build_project_index(db: Path, corpus: Path, cache_dir: Path, overlap: float = 0.18,
                        min_words: int = 10000, progress: Progress | None = None,
                        embedder_factory: Callable[[Path], object] = FastEmbedder) -> dict:
    """Build both strategies from the same tracked README files and return the manifest."""
    if not 0 <= overlap < 1:
        raise ValueError("Перекрытие должно быть от 0 до 1")

    def report(phase: str, completed: int | None, total: int | None, message: str) -> None:
        if progress:
            progress(phase, completed, total, message)

    initial_hash = corpus_sha256(corpus)
    paths = sources(corpus)
    report("reading", 0, len(paths), "Чтение отслеживаемых README репозитория")
    docs, empty = load_corpus(corpus)
    words = sum(len(document.text.split()) for document in docs)
    chapters = sum(len(document.sections) for document in docs)
    report("reading", len(paths), len(paths), f"Прочитано {len(docs)} документов, {chapters} разделов")
    if not docs or words < min_words:
        raise ValueError(f"Корпус содержит {words} слов; требуется минимум {min_words}")

    report("loading_model", None, None, "Загрузка локальной модели и токенизатора")
    embedder = embedder_factory(cache_dir)
    report("chunking_fixed", 0, 1, "Разбиение фиксированными окнами")
    fixed = make_chunks(docs, "fixed", embedder, overlap)
    report("chunking_fixed", 1, 1, f"Создано {len(fixed)} fixed-чанков")
    report("chunking_structure", 0, 1, "Разбиение по разделам")
    structured = make_chunks(docs, "structure", embedder, overlap)
    report("chunking_structure", 1, 1, f"Создано {len(structured)} structure-чанков")
    if not fixed or not structured:
        raise ValueError("Одна из стратегий не создала чанков")

    manifest = {
        "schema_version": 1,
        "corpus_kind": CORPUS_KIND,
        "sources": [path.relative_to(corpus.resolve()).as_posix() for path in paths],
        "sections": chapters,
        "model": embedder.model_name,
        "fastembed_version": getattr(embedder, "fastembed_version", None),
        "dimension": embedder.dimension,
        "model_input_limit": 128,
        "content_token_limit": embedder.max_tokens,
        "overlap_ratio": overlap,
        "files": len(docs),
        "chapters": chapters,
        "empty_files": empty,
        "words": words,
        "pages_at_500_words": round(words / 500, 2),
        "corpus_sha256": initial_hash,
        "chunks": {"fixed": len(fixed), "structure": len(structured)},
    }
    if corpus_sha256(corpus) != initial_hash:
        raise ValueError("README-корпус изменился во время сборки; повторите индексацию")
    build_index(db, fixed + structured, embedder, manifest, progress=progress)
    return manifest
