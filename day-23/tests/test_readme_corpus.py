"""Corpus selection and invalidation use real temporary Git repositories."""
import json
import sqlite3
import subprocess

import pytest

from backend.day21_adapter import (CORPUS_KIND, _snapshot, build_project_index,
                                   corpus_sha256, index_state, sources)
from conftest import FakeEmbedder


def track(root, *names):
    subprocess.run(["git", "-C", str(root), "add", "--", *names], check=True)


def put(root, name, text="# Источник\n\nСодержимое раздела."):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_selector_only_tracked_root_and_day_readmes_excludes_self_other_and_secret(corpus):
    for name in ["day-01/README.md", "day-21/README.md", "day-22/README.md", "day-23/README.md",
                 "wiki/README.md", "AGENTS.md", ".env", "day-19/report.md", "day-123/README.md"]:
        put(corpus, name)
        track(corpus, name)
    put(corpus, "day-02/README.md")  # untracked is not a source
    assert [path.relative_to(corpus).as_posix() for path in sources(corpus)] == [
        "README.md", "day-01/README.md", "day-16/README.md"]
    docs, details, _, _ = _snapshot(corpus)
    assert [doc.source for doc in docs] == [item["source"] for item in details]
    assert details[0]["title"] == "Project"
    assert all(item["words"] > 0 and item["sections"] > 0 for item in details)
    with pytest.raises((ValueError, FileNotFoundError)):
        sources(corpus / "day-16")


def test_metadata_and_state_available_before_model_or_database(corpus, tmp_path):
    state = index_state(tmp_path / "absent.sqlite", corpus)
    assert not state["ready"] and state["corpus_ready"]
    assert state["sources"] == ["README.md", "day-16/README.md"]
    assert state["corpus"]["kind"] == CORPUS_KIND
    assert state["corpus"]["words"] > 0 and state["corpus"]["sections"] > 0
    assert state["manifest"] is None


def test_hash_tracks_full_relative_names_bytes_and_tracked_set(corpus):
    first = corpus_sha256(corpus)
    original = (corpus / "day-16/README.md").read_bytes()
    put(corpus, "day-02/README.md", original.decode())
    assert corpus_sha256(corpus) == first
    track(corpus, "day-02/README.md")
    added = corpus_sha256(corpus)
    assert added != first
    (corpus / "day-02/README.md").write_bytes(original + b"\nchange")
    assert corpus_sha256(corpus) != added
    before_rename = corpus_sha256(corpus)
    (corpus / "day-03").mkdir(exist_ok=True)
    subprocess.run(["git", "-C", str(corpus), "mv", "day-02/README.md", "day-03/README.md"], check=True)
    assert corpus_sha256(corpus) != before_rename


@pytest.mark.parametrize("directory", [False, True])
def test_symlink_never_reads_outside_repo(corpus, tmp_path, directory):
    outside = put(tmp_path, "outside/README.md", "# Private\nsecret")
    if directory:
        (corpus / "day-03").symlink_to(outside.parent, target_is_directory=True)
        # Index entry can be planted while directory was previously real.
        subprocess.run(["git", "-C", str(corpus), "update-index", "--add", "--cacheinfo",
                        "100644", subprocess.check_output(["git", "-C", str(corpus), "hash-object", "README.md"], text=True).strip(),
                        "day-03/README.md"], check=True)
    else:
        (corpus / "day-03").mkdir()
        (corpus / "day-03/README.md").symlink_to(outside)
        track(corpus, "day-03/README.md")
    with pytest.raises(ValueError, match="Символическая ссылка"):
        sources(corpus)
    state = index_state(tmp_path / "missing.sqlite", corpus)
    assert not state["corpus_ready"] and state["sources"] == []


def test_missing_tracked_source_fails_clearly(corpus):
    (corpus / "README.md").unlink()
    with pytest.raises(FileNotFoundError, match="Отсутствует"):
        sources(corpus)


def test_old_book_index_rejected_even_with_current_hash(corpus, db):
    with sqlite3.connect(db) as connection:
        manifest = json.loads(connection.execute("SELECT data FROM manifest").fetchone()[0])
        manifest.pop("corpus_kind")
        connection.execute("UPDATE manifest SET data=?", (json.dumps(manifest),))
    state = index_state(db, corpus)
    assert not state["ready"] and "другого корпуса" in state["reason"]


def test_index_has_only_selected_readme_metadata_and_rebuild_atomic(corpus, db, tmp_path):
    manifest = index_state(db, corpus)["manifest"]
    assert manifest["corpus_kind"] == CORPUS_KIND
    assert manifest["sources"] == ["README.md", "day-16/README.md"]
    assert manifest["sections"] == manifest["chapters"] > 0
    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT DISTINCT source,title,section FROM chunks").fetchall()
    assert {row[0] for row in rows} == set(manifest["sources"])
    assert all(row[1] and row[2] for row in rows)
    before = db.read_bytes()
    class Broken(FakeEmbedder):
        def embed(self, texts):
            raise RuntimeError("model failure")
    with pytest.raises(RuntimeError, match="model failure"):
        build_project_index(db, corpus, tmp_path / "cache", embedder_factory=Broken, min_words=0)
    assert db.read_bytes() == before
