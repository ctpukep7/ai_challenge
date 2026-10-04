"""Explicit, read-only Markdown corpus for Day 21."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Section:
    title: str
    text: str


@dataclass(frozen=True)
class Document:
    source: str
    title: str
    sections: tuple[Section, ...]

    @property
    def text(self) -> str:
        return "\n\n".join(section.text for section in self.sections)


CORPUS_KIND = "repository_readmes"


def sources(root: Path) -> list[Path]:
    """Read only tracked README files, excluding the RAG days and private files."""
    root = root.resolve()
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             check=True, capture_output=True, text=True)
        if Path(top.stdout.strip()).resolve() != root:
            raise FileNotFoundError("Корнем корпуса должен быть корень Git-репозитория")
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--"],
            check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FileNotFoundError("Не найден Git-репозиторий с README-корпусом") from exc
    names = sorted({name.decode("utf-8") for name in result.stdout.split(b"\0") if name
                    and re.fullmatch(rb"(?:README\.md|day-(?:0[1-9]|1[0-9]|20)/README\.md)", name)})
    if not names:
        raise FileNotFoundError("В Git-репозитории отсутствуют отслеживаемые README-корпуса")
    paths = []
    for name in names:
        path = root / name
        if any(part.is_symlink() for part in (path, *path.parents) if part != root and root in part.parents):
            raise ValueError(f"Символическая ссылка запрещена в корпусе: {name}")
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"Источник выходит за границы репозитория: {name}")
        if not path.is_file():
            raise FileNotFoundError(f"Отсутствует отслеживаемый README: {name}")
        paths.append(path)
    return paths


def _clean_line(line: str) -> str:
    line = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", line)
    line = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", line)
    line = re.sub(r"<https?://[^>]+>", "", line)
    line = re.sub(r"(?<!\w)[*_]{1,2}(?=\S)|(?<=\S)[*_]{1,2}(?!\w)", "", line)
    return line.rstrip()


def extract_markdown(raw: str) -> tuple[Section, ...]:
    """Keep readable prose and fenced code; headings create section boundaries."""
    result: list[Section] = []
    headings: list[str] = []
    lines: list[str] = []
    in_fence = False

    def flush() -> None:
        text = "\n".join(lines).strip()
        if text:
            result.append(Section(" / ".join(headings) if headings else "(без заголовка)", text))
        lines.clear()

    for line in raw.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            continue
        heading = None if in_fence else re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush()
            level = len(heading.group(1))
            headings = headings[: level - 1] + [_clean_line(heading.group(2))]
            if level > 1:
                lines.append(headings[-1])
        else:
            lines.append(line.rstrip() if in_fence else _clean_line(line))
    flush()
    return tuple(result)


def load_corpus(root: Path) -> tuple[list[Document], int]:
    root = root.resolve()
    documents: list[Document] = []
    empty = 0
    for path in sources(root):
        if path.is_symlink():
            raise ValueError(f"Символическая ссылка запрещена в корпусе: {path}")
        if not path.is_file():
            raise FileNotFoundError(f"Отсутствует файл корпуса: {path}")
        relative = path.relative_to(root).as_posix()
        raw = path.read_text(encoding="utf-8")
        sections = extract_markdown(raw)
        if not sections:
            empty += 1
            continue
        title_match = re.search(r"(?m)^#\s+(.+)$", raw)
        title = title_match.group(1).strip() if title_match else path.stem
        documents.append(Document(relative, title, sections))
    return documents, empty
