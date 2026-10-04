"""Create a local, untracked Markdown corpus from user-provided book PDFs."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

from pypdf import PdfReader


DAY_DIR = Path(__file__).resolve().parents[1]
MANIFEST = DAY_DIR / "corpus_manifest.json"


def _page_text(reader: PdfReader, page_number: int) -> str:
    raw = reader.pages[page_number - 1].extract_text() or ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _heading_key(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def prepare_corpus(pdf_paths: dict[str, Path], output_dir: Path = DAY_DIR / "corpus",
                   manifest_path: Path = MANIFEST) -> dict[str, int]:
    """Extract exactly the configured pages; print no copyrighted text."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if set(pdf_paths) != set(manifest["books"]):
        raise ValueError("Нужны PDF обеих книг, указанных в манифесте")
    readers = {}
    for key, details in manifest["books"].items():
        pdf_path = pdf_paths[key]
        if not pdf_path.is_file():
            raise FileNotFoundError(f"Не найден PDF для книги «{details['title']}»")
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            raise ValueError(f"PDF книги «{details['title']}» защищён паролем")
        if len(reader.pages) < details["minimum_pages"]:
            raise ValueError(f"PDF книги «{details['title']}» короче ожидаемого диапазона страниц")
        readers[key] = reader

    generated: dict[str, str] = {}
    chapter_count = 0
    for document in manifest["documents"]:
        name = document["file"]
        if Path(name).name != name or not name.endswith(".md") or name in generated:
            raise ValueError("Некорректное имя документа в манифесте")
        reader = readers[document["book"]]
        lines = [f"# {document['title']}"]
        for chapter in document["chapters"]:
            first, last = chapter["first_page"], chapter["last_page"]
            if not (1 <= first <= last <= len(reader.pages)):
                raise ValueError("Некорректный диапазон страниц в манифесте")
            pages = [_page_text(reader, number) for number in range(first, last + 1)]
            expected_heading = _heading_key(chapter["title"].split(" — ", 1)[1])
            if expected_heading not in _heading_key(pages[0][:500]):
                raise ValueError(f"На PDF-странице {first} не найдена глава «{chapter['title']}»: "
                                 "проверьте издание и диапазоны в манифесте")
            chapter_text = " ".join(pages)
            if len(chapter_text.split()) < 500:
                raise ValueError(f"Глава «{chapter['title']}» извлечена не полностью")
            lines.extend(["", f"## {chapter['title']}"])
            for number, page_text in enumerate(pages, first):
                if not page_text:
                    continue
                lines.extend(["", f"[PDF-страница {number}]", "", page_text])
            chapter_count += 1
        generated[name] = "\n".join(lines).strip() + "\n"

    words = sum(len(text.split()) for text in generated.values())
    if len(generated) != 4 or chapter_count != 9 or words < 10000:
        raise ValueError("Извлечённый корпус не соответствует ожидаемому объёму")
    if output_dir.is_symlink():
        raise ValueError("Каталог корпуса не должен быть символической ссылкой")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in generated.items():
        target = output_dir / name
        if target.is_symlink():
            raise ValueError("Файл корпуса не должен быть символической ссылкой")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_dir,
                                         prefix=".corpus-", suffix=".tmp", delete=False) as file:
            staged = Path(file.name)
            try:
                file.write(content)
            except Exception:
                staged.unlink(missing_ok=True)
                raise
        os.replace(staged, target)
    return {"documents": len(generated), "chapters": chapter_count, "words": words,
            "pages_at_500_words": round(words / 500, 2)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Локально извлечь главы двух книг для Дня 21")
    parser.add_argument("--clean-code", type=Path, required=True, help="путь к пользовательскому PDF «Чистый код»")
    parser.add_argument("--clean-architecture", type=Path, required=True,
                        help="путь к пользовательскому PDF «Чистая архитектура»")
    parser.add_argument("--output", type=Path, default=DAY_DIR / "corpus",
                        help="каталог для четырёх локальных Markdown-файлов")
    args = parser.parse_args()
    result = prepare_corpus({"clean_code": args.clean_code,
                             "clean_architecture": args.clean_architecture}, args.output)
    print(f"Подготовлено {result['documents']} документа, {result['chapters']} глав, "
          f"{result['words']} слов (≈ {result['pages_at_500_words']} страниц по 500 слов).")
    print("Тексты сохранены локально и исключены из Git.")


if __name__ == "__main__":
    main()
