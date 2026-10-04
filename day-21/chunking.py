"""Token-bounded fixed and Markdown-section chunking."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol

from corpus import Document


class TokenCounter(Protocol):
    max_tokens: int

    def offsets(self, text: str) -> list[tuple[int, int]]: ...

    def count(self, text: str) -> int: ...


@dataclass(frozen=True)
class Chunk:
    strategy: str
    source: str
    title: str
    section: str
    chunk_id: str
    ordinal: int
    text: str
    token_count: int
    overlap_tokens: int
    crosses_sections: bool


def _windows(text: str, counter: TokenCounter, overlap_ratio: float):
    # Prefer whitespace boundaries, so ordinary chunks do not begin mid-word.
    # An exceptional word longer than the model window is split on token spans.
    spans = []
    for match in re.finditer(r"\S+", text):
        a, b = match.span()
        if counter.count(match.group()) <= counter.max_tokens:
            spans.append((a, b))
        else:
            subspans = [(a + left, a + right) for left, right in counter.offsets(match.group()) if right > left]
            spans.extend(subspans)
    if not spans:
        return
    size = counter.max_tokens
    if size < 2:
        raise ValueError("Лимит токенов должен быть не меньше двух")
    overlap = min(size - 1, round(size * overlap_ratio))
    start = 0
    previous_end = 0
    while start < len(spans):
        end = min(start + size, len(spans))
        while end > start:
            fragment = text[spans[start][0] : spans[end - 1][1]]
            actual = counter.count(fragment)
            if actual <= size:
                break
            end -= 1
        if end == start:
            raise ValueError("Токенизатор не позволяет вместить один токен в чанк")
        repeated = counter.count(text[spans[start][0] : spans[previous_end - 1][1]]) if previous_end > start else 0
        yield spans[start][0], spans[end - 1][1], fragment, actual, repeated
        if end == len(spans):
            break
        previous_end = end
        next_start = end - 1
        while next_start > start and counter.count(text[spans[next_start][0] : spans[end - 1][1]]) < overlap:
            next_start -= 1
        start = max(start + 1, next_start)


def make_chunks(documents: list[Document], strategy: str, counter: TokenCounter,
                overlap_ratio: float = 0.18) -> list[Chunk]:
    if strategy not in {"fixed", "structure"}:
        raise ValueError(f"Неизвестная стратегия: {strategy}")
    if not 0 <= overlap_ratio < 1:
        raise ValueError("Перекрытие должно быть от 0 до 1")
    output: list[Chunk] = []
    for document in documents:
        boundaries = []
        cursor = 0
        for section in document.sections:
            boundaries.append((cursor, cursor + len(section.text), section.title))
            cursor += len(section.text) + 2
        ranges = [(0, document.text, None)] if strategy == "fixed" else [
            (start, section.text, section.title)
            for (start, _, _), section in zip(boundaries, document.sections, strict=True)
        ]
        ordinal = 0
        for base, text, section_name in ranges:
            for start, end, fragment, token_count, overlap in _windows(text, counter, overlap_ratio):
                absolute_start, absolute_end = base + start, base + end
                sections = [item for item in boundaries if item[0] < absolute_end and item[1] > absolute_start]
                name = section_name or (sections[0][2] if sections else "(без заголовка)")
                crosses = len(sections) > 1
                identity = json.dumps([strategy, document.source, ordinal, fragment,
                                       counter.max_tokens, overlap_ratio], ensure_ascii=False)
                chunk_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                output.append(Chunk(strategy, document.source, document.title, name,
                                    chunk_id, ordinal, fragment, token_count, overlap, crosses))
                ordinal += 1
    return output
