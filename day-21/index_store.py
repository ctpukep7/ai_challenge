"""SQLite vector storage, atomic replacement, and exact cosine retrieval."""

from __future__ import annotations

import json
import math
import os
import re
import unicodedata
import sqlite3
import statistics
import struct
import tempfile
from pathlib import Path
from typing import Callable, Protocol

from chunking import Chunk


class Embedder(Protocol):
    model_name: str
    dimension: int
    max_tokens: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


Progress = Callable[[str, int | None, int | None, str], None]


SCHEMA = """
CREATE TABLE manifest (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL);
CREATE TABLE chunks (
    strategy TEXT NOT NULL CHECK (strategy IN ('fixed', 'structure')),
    chunk_id TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    section TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    overlap_tokens INTEGER NOT NULL,
    crosses_sections INTEGER NOT NULL CHECK (crosses_sections IN (0, 1)),
    vector BLOB NOT NULL,
    PRIMARY KEY (strategy, chunk_id),
    UNIQUE (strategy, source, ordinal)
);
CREATE INDEX chunks_strategy_source ON chunks (strategy, source);
"""


def _pack(vector: list[float], dimension: int) -> bytes:
    if len(vector) != dimension or not all(math.isfinite(value) for value in vector):
        raise ValueError("Неверная размерность вектора или NaN/Inf")
    return struct.pack(f"<{dimension}f", *vector)


def _unpack(blob: bytes, dimension: int) -> tuple[float, ...]:
    if len(blob) != dimension * 4:
        raise ValueError("Повреждён BLOB вектора")
    vector = struct.unpack(f"<{dimension}f", blob)
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("Вектор содержит NaN/Inf")
    return vector


def _check_manifest(connection: sqlite3.Connection, embedder: Embedder | None = None) -> dict:
    row = connection.execute("SELECT data FROM manifest WHERE id = 1").fetchone()
    if row is None:
        raise ValueError("Индекс не содержит манифеста")
    manifest = json.loads(row[0])
    if embedder is not None and (manifest["model"] != embedder.model_name
                                 or manifest["dimension"] != embedder.dimension
                                 or ("fastembed_version" in manifest and
                                     manifest["fastembed_version"] != getattr(embedder, "fastembed_version", None))):
        raise ValueError("Модель индекса и текущая модель несовместимы; перестройте индекс")
    return manifest


def build_index(path: Path, chunks: list[Chunk], embedder: Embedder, manifest: dict,
                progress: Progress | None = None) -> None:
    """Replace the old index only after a complete and verified temporary DB."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".index-", suffix=".sqlite", dir=path.parent)
    os.close(descriptor)
    try:
        with sqlite3.connect(temporary) as connection:
            connection.executescript(SCHEMA)
            connection.execute("INSERT INTO manifest (id, data) VALUES (1, ?)",
                               (json.dumps(manifest, ensure_ascii=False, sort_keys=True),))
            if progress:
                progress("embedding", 0, len(chunks), "Генерация эмбеддингов начата")
            for offset in range(0, len(chunks), 32):
                batch = chunks[offset : offset + 32]
                vectors = embedder.embed([chunk.text for chunk in batch])
                if len(vectors) != len(batch):
                    raise ValueError("Количество эмбеддингов не совпадает с количеством чанков")
                connection.executemany("""
                    INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    (chunk.strategy, chunk.chunk_id, chunk.source, chunk.title,
                     chunk.section, chunk.ordinal, chunk.text, chunk.token_count,
                     chunk.overlap_tokens, int(chunk.crosses_sections),
                     _pack(vector, embedder.dimension))
                    for chunk, vector in zip(batch, vectors, strict=True)
                ])
                if progress:
                    progress("embedding", offset + len(batch), len(chunks),
                             f"Сохранено векторов: {offset + len(batch)} из {len(chunks)}")
            if progress:
                progress("sqlite", 0, 1, "Проверка временного SQLite-индекса")
            count = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            if count != len(chunks):
                raise RuntimeError("Индекс содержит не все чанки")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite integrity_check не прошёл")
        with sqlite3.connect(temporary) as connection:
            _check_manifest(connection, embedder)
            for blob, in connection.execute("SELECT vector FROM chunks"):
                _unpack(blob, embedder.dimension)
        os.replace(temporary, path)
        if progress:
            progress("sqlite", 1, 1, "Индекс проверен и атомарно заменён")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_manifest(path: Path) -> dict:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return _check_manifest(connection)


def strategy_stats(path: Path, strategy: str) -> dict:
    if strategy not in {"fixed", "structure"}:
        raise ValueError("Неизвестная стратегия")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        rows = connection.execute("""
            SELECT token_count, overlap_tokens, crosses_sections
            FROM chunks WHERE strategy = ?
        """, (strategy,)).fetchall()
    if not rows:
        raise ValueError(f"В индексе нет чанков {strategy}")
    lengths = sorted(row[0] for row in rows)
    p95 = lengths[max(0, (95 * len(lengths) + 99) // 100 - 1)]
    return {"count": len(rows), "min": lengths[0], "median": statistics.median(lengths),
            "p95": p95, "max": lengths[-1], "overlap": sum(row[1] for row in rows),
            "crosses": sum(row[2] for row in rows)}


def list_chunks(path: Path, strategy: str, source: str | None,
                page: int, limit: int) -> dict:
    if strategy not in {"fixed", "structure"} or page < 1 or not 1 <= limit <= 50:
        raise ValueError("Неверные параметры списка чанков")
    manifest = read_manifest(path)
    condition = "strategy = ?"
    params: list[object] = [strategy]
    if source is not None:
        condition += " AND source = ?"
        params.append(source)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        total = connection.execute(f"SELECT COUNT(*) FROM chunks WHERE {condition}", params).fetchone()[0]
        rows = connection.execute(f"""
            SELECT source, title, section, chunk_id, strategy, ordinal, text,
                   token_count, overlap_tokens, crosses_sections, vector
            FROM chunks WHERE {condition}
            ORDER BY source, ordinal LIMIT ? OFFSET ?
        """, [*params, limit, (page - 1) * limit]).fetchall()
    items = []
    for row in rows:
        _unpack(row[10], manifest["dimension"])
        items.append({"source": row[0], "title": row[1], "section": row[2],
                      "chunk_id": row[3], "strategy": row[4], "ordinal": row[5],
                      "text": row[6], "token_count": row[7],
                      "overlap_tokens": row[8], "crosses_sections": bool(row[9]),
                      "vector_dimensions": manifest["dimension"]})
    return {"items": items, "total": total, "page": page, "limit": limit}


_DAY_REFERENCE = re.compile(
    r"(?<![\w-])(?:day\s*[-№]?\s*|(?:день|дня|дне)\s*[-№]?\s*)(\d+)(?![\w-])",
    re.IGNORECASE,
)


def source_scope(query: str) -> dict:
    """Conservative source constraints from the user's text, never benchmark data.

    Counts such as 'через 7 дней' are not references. Negated/alternative day
    references leave the search unconstrained rather than silently inverting intent.
    """
    text = unicodedata.normalize("NFKC", query)
    references = list(_DAY_REFERENCE.finditer(text))
    if not references:
        return {"kind": "all", "sources": None, "reason": "no_explicit_day"}
    for match in references:
        prefix = text[:match.start()]
        suffix = text[match.end():]
        negated = re.search(
            r"\b(?:не|not|кроме|без|исключая|except|excluding)\b"
            r"(?:\s+(?:про|в|о|об|из|на|about|in|for|only|just|только|используй|рассматривай|бери|include|use))*\s*$",
            prefix, re.IGNORECASE,
        ) or re.match(r"\s+(?:не\s+(?:рассматривай|используй|бери)|not\s+(?:include|use))\b",
                      suffix, re.IGNORECASE)
        alternative = re.search(r"\b(?:или|or)\s*$", prefix, re.IGNORECASE) or re.match(
            r"\s+(?:или|or|ли)\b", suffix, re.IGNORECASE,
        )
        if negated or alternative:
            return {"kind": "all", "sources": None, "reason": "ambiguous_day_reference"}
    days = sorted({int(match.group(1)) for match in references})
    return {"kind": "explicit_days", "sources": [f"day-{day:02d}/README.md" for day in days],
            "reason": "explicit_day_reference"}


def search(path: Path, embedder: Embedder, query: str, strategy: str, top_k: int = 5,
           *, retrieval_scope: dict | None = None) -> list[dict]:
    if strategy not in {"fixed", "structure"} or top_k < 1:
        raise ValueError("Нужны стратегия fixed/structure и top_k >= 1")
    if not query.strip():
        raise ValueError("Пустой поисковый запрос")
    if hasattr(embedder, "count") and embedder.count(query) > embedder.max_tokens:
        raise ValueError("Поисковый запрос длиннее лимита модели")
    scope = source_scope(query) if retrieval_scope is None else retrieval_scope
    condition = "strategy = ?"
    params: list[object] = [strategy]
    if scope["sources"] is not None:
        if not scope["sources"]:
            return []
        condition += " AND source IN (" + ", ".join("?" for _ in scope["sources"]) + ")"
        params.extend(scope["sources"])
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        manifest = _check_manifest(connection, embedder)
        vector = embedder.embed([query])[0]
        _pack(vector, manifest["dimension"])
        query_norm = math.sqrt(sum(value * value for value in vector))
        matches = []
        for row in connection.execute(f"""
            SELECT source, title, section, chunk_id, text, vector
            FROM chunks WHERE {condition}
        """, params):
            candidate = _unpack(row[5], manifest["dimension"])
            norm = math.sqrt(sum(value * value for value in candidate))
            score = sum(a * b for a, b in zip(vector, candidate, strict=True)) / (query_norm * norm) if query_norm and norm else 0.0
            matches.append({"source": row[0], "title": row[1], "section": row[2],
                            "chunk_id": row[3], "text": row[4], "score": score})
    return sorted(matches, key=lambda item: (-item["score"], item["source"], item["chunk_id"]))[:top_k]
