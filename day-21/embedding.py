"""Local FastEmbed adapter with an explicit, checked tokenizer limit."""

from __future__ import annotations

import math
from importlib.metadata import version
from pathlib import Path

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIMENSION = 384
MODEL_LIMIT = 128


class FastEmbedder:
    max_tokens = MODEL_LIMIT - 2
    model_name = MODEL_NAME
    dimension = DIMENSION

    def __init__(self, cache_dir: Path | None = None):
        from fastembed import TextEmbedding

        self.fastembed_version = version("fastembed")
        kwargs = {"model_name": MODEL_NAME}
        if cache_dir is not None:
            kwargs["cache_dir"] = str(cache_dir)
        self.model = TextEmbedding(**kwargs)
        try:
            self.tokenizer = self.model.model.tokenizer
            truncation = self.tokenizer.truncation
            if not truncation or truncation.get("max_length") != MODEL_LIMIT:
                raise RuntimeError(f"Неожиданный лимит токенизатора: {truncation}")
            self.tokenizer.no_truncation()
        except AttributeError as exc:
            raise RuntimeError("FastEmbed не предоставил токенизатор; сборка остановлена") from exc

    def offsets(self, text: str) -> list[tuple[int, int]]:
        return list(self.tokenizer.encode(text, add_special_tokens=False).offsets)

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def embed(self, texts: list[str]) -> list[list[float]]:
        for text in texts:
            full = len(self.tokenizer.encode(text, add_special_tokens=True).ids)
            if full > MODEL_LIMIT:
                raise ValueError(f"Текст превышает лимит модели: {full} > {MODEL_LIMIT} токенов")
        vectors = [list(map(float, item)) for item in self.model.embed(texts, batch_size=32)]
        if len(vectors) != len(texts):
            raise RuntimeError("FastEmbed вернул неправильное число векторов")
        if any(len(vector) != DIMENSION or not all(map(math.isfinite, vector)) for vector in vectors):
            raise RuntimeError("FastEmbed вернул вектор неверной размерности или с NaN/Inf")
        return vectors
