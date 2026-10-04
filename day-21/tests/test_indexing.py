from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from chunking import make_chunks
from corpus import Document, Section, load_corpus, sources
from index_store import build_index, read_manifest, search


class FakeEmbedder:
    model_name = "fake-model"
    dimension = 3
    max_tokens = 8

    def offsets(self, text):
        return [match.span() for match in re.finditer(r"\S+", text)]

    def count(self, text):
        return len(self.offsets(text))

    def embed(self, texts):
        return [[float(len(text) % 17), float(text.count("Mars")), 1.0] for text in texts]


def fixture_docs():
    return [Document("solar-system.md", "Solar System", (
        Section("Solar System / Mars", "Mars one two three four five six seven eight nine ten eleven"),
        Section("Solar System / Jupiter", "Jupiter alpha beta gamma delta epsilon zeta eta theta iota kappa"),
    ))]


def test_extraction_and_explicit_corpus(tmp_path):
    from repo_fixture import make_tracked_corpus
    root = make_tracked_corpus(tmp_path / "repo")
    (root / "notes.md").write_text("DO NOT INDEX", encoding="utf-8")
    docs, empty = load_corpus(root)
    assert empty == 0 and len(docs) == 4
    assert [doc.source for doc in docs] == [p.relative_to(root).as_posix() for p in sources(root)]
    assert all(len(doc.sections) == 2 for doc in docs)
    fixed = make_chunks(docs, "fixed", FakeEmbedder())
    structured = make_chunks(docs, "structure", FakeEmbedder())
    assert all(not chunk.crosses_sections for chunk in structured)
    assert any(chunk.crosses_sections for chunk in fixed)




def test_pdf_preparation_rejects_wrong_edition_pages(tmp_path, monkeypatch):
    pytest.importorskip("pypdf")
    from scripts import prepare_corpus as script

    class FakePage:
        def extract_text(self):
            return "другой раздел " * 100

    class FakeReader:
        is_encrypted = False
        pages = [FakePage()] * 316

        def __init__(self, path):
            pass

    monkeypatch.setattr(script, "PdfReader", FakeReader)
    pdfs = {book: tmp_path / f"{book}.pdf" for book in ("clean_code", "clean_architecture")}
    for path in pdfs.values():
        path.write_bytes(b"wrong edition fixture")
    with pytest.raises(ValueError, match="проверьте издание"):
        script.prepare_corpus(pdfs, tmp_path / "corpus")
    assert not (tmp_path / "corpus").exists()


def test_chunking_preserves_words_and_structure():
    docs = fixture_docs()
    model = FakeEmbedder()
    fixed = make_chunks(docs, "fixed", model)
    structured = make_chunks(docs, "structure", model)
    assert any(chunk.crosses_sections for chunk in fixed)
    assert all(not chunk.crosses_sections for chunk in structured)
    assert all(chunk.token_count <= model.max_tokens for chunk in fixed + structured)
    assert all(chunk.section and chunk.source and chunk.title and chunk.chunk_id for chunk in fixed + structured)
    for strategy in (fixed, structured):
        words = set(" ".join(chunk.text for chunk in strategy).split())
        assert set(docs[0].text.split()) <= words
    assert [chunk.chunk_id for chunk in fixed] == [chunk.chunk_id for chunk in make_chunks(docs, "fixed", model)]
    assert {chunk.chunk_id for chunk in fixed}.isdisjoint(chunk.chunk_id for chunk in structured)


def test_sqlite_roundtrip_model_guard_and_atomic_replacement(tmp_path):
    path = tmp_path / "index.sqlite"
    model = FakeEmbedder()
    docs = fixture_docs()
    chunks = make_chunks(docs, "fixed", model) + make_chunks(docs, "structure", model)
    manifest = {"model": model.model_name, "dimension": model.dimension, "files": 1}
    build_index(path, chunks, model, manifest)
    assert read_manifest(path) == manifest
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == len(chunks)
        assert all(len(row[0]) == 12 for row in connection.execute("SELECT vector FROM chunks"))
    results = search(path, model, "Mars", "structure", 2)
    assert results and len(results) == 2 and results[0]["source"] == "solar-system.md"
    incompatible = FakeEmbedder()
    incompatible.model_name = "another-model"
    with pytest.raises(ValueError, match="несовместимы"):
        search(path, incompatible, "Mars", "fixed")
    before = path.read_bytes()

    class BrokenEmbedder(FakeEmbedder):
        def embed(self, texts):
            raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        build_index(path, chunks, BrokenEmbedder(), manifest)
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".index-*.sqlite")) == []


def test_missing_corpus_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_corpus(tmp_path)


def test_chunk_text_starts_at_word_boundary():
    model = FakeEmbedder()
    chunks = make_chunks(fixture_docs(), "fixed", model)
    assert all(chunk.text[0].isalpha() for chunk in chunks)
    assert all(chunk.text[-1].isalpha() for chunk in chunks)


def test_tracked_selector_privacy_missing_and_nested_root(tmp_path):
    import subprocess
    from repo_fixture import make_tracked_corpus
    root = make_tracked_corpus(tmp_path / "repo")
    for name in ("day-21/README.md", "day-22/README.md", "day-23/README.md", ".env", "wiki/README.md", "day-01/report.md"):
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("PRIVATE", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    assert len(sources(root)) == 4
    assert all("PRIVATE" not in doc.text for doc in load_corpus(root)[0])
    with pytest.raises(FileNotFoundError):
        sources(root / "day-01")
    (root / "day-01/README.md").unlink()
    with pytest.raises(FileNotFoundError, match="отслеживаемый"):
        sources(root)


def test_hash_tracks_paths_file_list_and_content(tmp_path):
    import subprocess
    from backend.pipeline import corpus_sha256
    from repo_fixture import make_tracked_corpus
    root = make_tracked_corpus(tmp_path / "repo")
    initial = corpus_sha256(root)
    target = root / "day-01/README.md"
    target.write_text(target.read_text() + "\nChanged")
    assert corpus_sha256(root) != initial
    changed = corpus_sha256(root)
    subprocess.run(["git", "-C", str(root), "rm", "--cached", "-f", "day-01/README.md"], check=True, capture_output=True)
    assert corpus_sha256(root) != changed


def test_symlink_file_and_directory_are_rejected(tmp_path):
    from repo_fixture import make_tracked_corpus
    root = make_tracked_corpus(tmp_path / "repo")
    target = root / "day-01/README.md"
    target.unlink()
    target.symlink_to(root / "README.md")
    with pytest.raises(ValueError, match="Символическая"):
        sources(root)
    target.unlink()
    target.parent.rmdir()
    target.parent.symlink_to(root / "day-07", target_is_directory=True)
    with pytest.raises(ValueError, match="Символическая"):
        sources(root)
