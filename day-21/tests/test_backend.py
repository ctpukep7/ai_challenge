from __future__ import annotations

import re
import time
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.pipeline import build_project_index, corpus_sha256
from chunking import Chunk, make_chunks
from corpus import load_corpus, sources
from index_store import build_index, read_manifest


class FakeEmbedder:
    model_name = "fake-model"
    fastembed_version = "test"
    dimension = 3
    max_tokens = 8

    def __init__(self, cache_dir=None):
        pass

    def offsets(self, text):
        return [match.span() for match in re.finditer(r"\S+", text)]

    def count(self, text):
        return len(self.offsets(text))

    def embed(self, texts):
        return [[float(len(text) % 17), float(text.count("Mars")), 1.0] for text in texts]


from repo_fixture import make_tracked_corpus as make_corpus


def seed_index(path: Path, corpus: Path, marker="old"):
    docs, _ = load_corpus(corpus)
    model = FakeEmbedder()
    chunks = make_chunks(docs, "fixed", model) + make_chunks(docs, "structure", model)
    manifest = {"model": model.model_name, "dimension": model.dimension,
                "fastembed_version": model.fastembed_version, "marker": marker,
                "schema_version": 1, "corpus_kind": "repository_readmes", "corpus_sha256": corpus_sha256(corpus),
                "chunks": {"fixed": len([c for c in chunks if c.strategy == "fixed"]),
                           "structure": len([c for c in chunks if c.strategy == "structure"])}}
    build_index(path, chunks, model, manifest)


def await_status(client: TestClient, target: str, workspace="main"):
    for _ in range(100):
        result = client.get("/api/build", params={"workspace": workspace}).json()
        if result["status"] == target:
            return result
        time.sleep(0.01)
    raise AssertionError(f"Job did not reach {target}: {result}")


def test_state_without_index_does_not_load_model(tmp_path):
    corpus = tmp_path / "corpus"
    make_corpus(corpus)
    app = create_app(db=tmp_path / "missing.sqlite", corpus=corpus,
                     embedder_factory=lambda _: (_ for _ in ()).throw(AssertionError("model loaded")))
    with TestClient(app) as client:
        state = client.get("/api/state").json()
        assert state["corpus"]["files"] == 4
        assert state["corpus"]["available"] is True
        assert state["corpus"]["chapters"] == 8
        assert state["corpus"]["words"] > 0
        assert len(state["corpus"]["documents"]) == 4
        assert len({item["title"] for item in state["corpus"]["documents"]}) == 4
        assert state["index"] == {"ready": False, "exists": False,
                                  "file": "data/missing.sqlite", "manifest": None,
                                  "strategies": {"fixed": None, "structure": None}}
        assert state["build"]["status"] == "idle"
        assert client.get("/api/chunks").json() == {"items": [], "total": 0, "page": 1, "limit": 20}
        assert client.post("/api/search", json={"query": "Mars"}).status_code == 409


def test_missing_private_corpus_explains_preparation_without_server_error(tmp_path):
    app = create_app(db=tmp_path / "index.sqlite", corpus=tmp_path / "corpus")
    with TestClient(app) as client:
        response = client.get("/api/state")
        assert response.status_code == 200
        state = response.json()
        assert state["corpus"]["available"] is False
        assert state["corpus"]["files"] == 0
        assert "репозиторий" in state["corpus"]["reason"]
        assert state["index"]["ready"] is False
        assert client.post("/api/build", json={}).status_code == 409
        assert not (tmp_path / "index.sqlite").exists()


def test_changed_corpus_invalidates_main_and_demo_indexes(tmp_path):
    corpus = tmp_path / "corpus"
    make_corpus(corpus)
    main_db = tmp_path / "index.sqlite"
    demo_db = tmp_path / "demo-index.sqlite"
    seed_index(main_db, corpus)
    seed_index(demo_db, corpus)
    first = sources(corpus)[0]
    first.write_text(first.read_text(encoding="utf-8") + "\nНовая редакция корпуса.\n", encoding="utf-8")
    app = create_app(db=main_db, corpus=corpus,
                     embedder_factory=lambda _: (_ for _ in ()).throw(AssertionError("model loaded")))
    with TestClient(app) as client:
        for workspace in ("main", "demo"):
            state = client.get("/api/state", params={"workspace": workspace}).json()["index"]
            assert state["exists"] is True and state["ready"] is False
            assert "Корпус изменился" in state["reason"]
            assert state["manifest"] is None
            assert client.get("/api/chunks", params={"workspace": workspace}).status_code == 409
            assert client.post("/api/search", params={"workspace": workspace},
                               json={"query": "Mars"}).status_code == 409


def test_ready_index_chunks_pagination_validation_and_search(tmp_path):
    corpus = tmp_path / "corpus"
    make_corpus(corpus)
    db = tmp_path / "index.sqlite"
    seed_index(db, corpus)
    app = create_app(db=db, corpus=corpus, embedder_factory=FakeEmbedder)
    with TestClient(app) as client:
        state = client.get("/api/state").json()
        assert state["index"]["ready"] is True
        assert state["index"]["exists"] is True
        assert state["index"]["file"] == "data/index.sqlite"
        assert state["index"]["manifest"]["marker"] == "old"
        assert state["index"]["strategies"]["fixed"]["count"] > 0
        assert state["index"]["strategies"]["structure"]["crosses"] == 0
        first = client.get("/api/chunks", params={"strategy": "fixed", "page": 1, "limit": 2}).json()
        second = client.get("/api/chunks", params={"strategy": "fixed", "page": 2, "limit": 2}).json()
        assert first["total"] > 2 and len(first["items"]) == len(second["items"]) == 2
        assert first["items"][0]["chunk_id"] != second["items"][0]["chunk_id"]
        assert first["items"][0]["vector_dimensions"] == 3
        assert "vector" not in first["items"][0]
        source = sources(corpus)[0].name
        filtered = client.get("/api/chunks", params={"strategy": "structure", "source": source}).json()
        assert filtered["total"] > 0 and all(item["source"] == source for item in filtered["items"])
        for params in ({"strategy": "unknown"}, {"source": "../.env"},
                       {"page": 0}, {"limit": 51}):
            assert client.get("/api/chunks", params=params).status_code == 422
        result = client.post("/api/search", json={"query": "Mars", "top_k": 2}).json()
        assert result["query"] == "Mars"
        assert set(result["results"]) == {"fixed", "structure"}
        assert all(len(result["results"][strategy]) == 2 for strategy in ("fixed", "structure"))
        assert all("score" in item and "source" in item and "text" in item
                   for strategy in result["results"] for item in result["results"][strategy])
        assert client.post("/api/search", json={"query": "x" * 301}).status_code == 422
        assert client.post("/api/search", json={"query": "   "}).status_code == 422
        assert client.post("/api/search", json={"query": "Mars", "top_k": 6}).status_code == 422


def test_build_conflict_progress_and_success(tmp_path):
    corpus = tmp_path / "corpus"
    make_corpus(corpus)
    db = tmp_path / "index.sqlite"
    seed_index(db, corpus)
    started, release = Event(), Event()

    def builder(db_path, corpus_path, cache_dir, overlap, progress):
        progress("reading", 4, 4, "Прочитано четыре документа")
        progress("embedding", 32, 64, "Сохранено векторов: 32 из 64")
        started.set()
        assert release.wait(5)
        seed_index(db_path, corpus_path, marker="new")
        progress("sqlite", 1, 1, "Индекс проверен и атомарно заменён")

    app = create_app(db=db, corpus=corpus, builder=builder)
    with TestClient(app) as client:
        response = client.post("/api/build", json={"overlap_ratio": 0.22})
        assert response.status_code == 202 and response.json()["id"]
        assert started.wait(2)
        assert client.post("/api/build", json={"overlap_ratio": 0.22}).status_code == 409
        running = client.get("/api/build").json()
        assert running["status"] == "running" and running["phase"] == "embedding"
        assert (running["completed"], running["total"]) == (32, 64)
        assert all(set(event) == {"phase", "message", "completed", "total", "at"}
                   for event in running["events"])
        assert client.get("/api/state").json()["index"]["manifest"]["marker"] == "old"
        release.set()
        complete = await_status(client, "completed")
        assert complete["phase"] == "complete"
        assert client.get("/api/state").json()["index"]["manifest"]["marker"] == "new"
        assert client.post("/api/build", json={"overlap_ratio": 0.5}).status_code == 422


def test_build_failure_keeps_previous_index(tmp_path):
    corpus = tmp_path / "corpus"
    make_corpus(corpus)
    db = tmp_path / "index.sqlite"
    seed_index(db, corpus)
    before = db.read_bytes()

    def builder(db_path, corpus_path, cache_dir, overlap, progress):
        progress("embedding", 1, 3, "Сохранено векторов: 1 из 3")
        raise RuntimeError("synthetic failure")

    app = create_app(db=db, corpus=corpus, builder=builder)
    with TestClient(app) as client:
        assert client.post("/api/build", json={"overlap_ratio": 0.18}).status_code == 202
        failed = await_status(client, "failed")
        assert failed["error"] and failed["phase"] == "failed"
        assert db.read_bytes() == before
        assert client.get("/api/state").json()["index"]["manifest"]["marker"] == "old"
        assert not list(tmp_path.glob(".index-*.sqlite"))


def test_demo_first_run_build_reset_and_main_isolation(tmp_path):
    corpus = tmp_path / "corpus"
    make_corpus(corpus)
    main_db = tmp_path / "index.sqlite"
    demo_db = tmp_path / "demo-index.sqlite"
    seed_index(main_db, corpus, marker="main")
    main_bytes = main_db.read_bytes()

    def builder(db_path, corpus_path, cache_dir, overlap, progress):
        build_project_index(db_path, corpus_path, cache_dir, overlap=overlap,
                            min_words=0, progress=progress,
                            embedder_factory=FakeEmbedder)

    app = create_app(db=main_db, corpus=corpus, builder=builder,
                     embedder_factory=FakeEmbedder)
    with TestClient(app) as client:
        empty = client.get("/api/state", params={"workspace": "demo"}).json()
        assert empty["corpus"] == client.get("/api/state").json()["corpus"]
        assert empty["index"] == {"ready": False, "exists": False,
                                  "file": "data/demo-index.sqlite", "manifest": None,
                                  "strategies": {"fixed": None, "structure": None}}
        assert not demo_db.exists()
        assert client.get("/api/chunks", params={"workspace": "demo"}).json()["total"] == 0
        assert client.post("/api/search?workspace=demo", json={"query": "Mars"}).status_code == 409

        started = client.post("/api/build?workspace=demo", json={"overlap_ratio": 0.18})
        assert started.status_code == 202 and started.json()["id"]
        done = await_status(client, "completed", "demo")
        assert any(event["phase"] == "embedding" for event in done["events"])
        ready = client.get("/api/state?workspace=demo").json()
        assert demo_db.is_file()
        assert ready["index"]["ready"] is True
        assert ready["index"]["exists"] is True
        assert ready["index"]["file"] == "data/demo-index.sqlite"
        assert ready["index"]["manifest"]["chunks"]["fixed"] > 0
        assert ready["index"]["manifest"]["chunks"]["structure"] > 0
        assert client.get("/api/chunks?workspace=demo").json()["total"] > 0
        assert client.post("/api/search?workspace=demo", json={"query": "Mars"}).status_code == 200
        assert client.get("/api/build").json()["status"] == "idle"
        assert main_db.read_bytes() == main_bytes
        assert client.get("/api/state").json()["index"]["manifest"]["marker"] == "main"

        assert client.post("/api/demo/reset").json() == {"status": "idle"}
        assert not demo_db.exists()
        assert client.get("/api/build?workspace=demo").json()["status"] == "idle"
        assert client.get("/api/state?workspace=demo").json()["index"]["exists"] is False
        assert client.post("/api/demo/reset").status_code == 200
        assert main_db.read_bytes() == main_bytes


def test_invalid_workspace_is_rejected_on_every_scoped_route(tmp_path):
    corpus = tmp_path / "corpus"
    make_corpus(corpus)
    app = create_app(db=tmp_path / "index.sqlite", corpus=corpus)
    with TestClient(app) as client:
        assert client.get("/api/state?workspace=other").status_code == 422
        assert client.get("/api/build?workspace=other").status_code == 422
        assert client.post("/api/build?workspace=other", json={}).status_code == 422
        assert client.get("/api/chunks?workspace=other").status_code == 422
        assert client.post("/api/search?workspace=other", json={"query": "Mars"}).status_code == 422
    assert list(tmp_path.glob("*.sqlite")) == []


def test_demo_reset_conflicts_with_running_demo_build(tmp_path):
    corpus = tmp_path / "corpus"
    make_corpus(corpus)
    main_db = tmp_path / "index.sqlite"
    demo_db = tmp_path / "demo-index.sqlite"
    seed_index(main_db, corpus, marker="main")
    main_bytes = main_db.read_bytes()
    started, release = Event(), Event()

    def builder(db_path, corpus_path, cache_dir, overlap, progress):
        started.set()
        assert release.wait(5)
        seed_index(db_path, corpus_path, marker="demo")

    app = create_app(db=main_db, corpus=corpus, builder=builder)
    with TestClient(app) as client:
        try:
            assert client.post("/api/build?workspace=demo", json={}).status_code == 202
            assert started.wait(2)
            assert client.post("/api/demo/reset").status_code == 409
            assert client.get("/api/build?workspace=demo").json()["status"] == "running"
            assert main_db.read_bytes() == main_bytes
        finally:
            release.set()
        assert await_status(client, "completed", "demo")["status"] == "completed"
        assert demo_db.is_file()
        assert client.post("/api/demo/reset").status_code == 200
        assert main_db.read_bytes() == main_bytes


def test_storage_callback_reports_every_embedding_batch(tmp_path):
    chunks = [Chunk("fixed", "inner-planets.md", "Planets", "Mercury", f"id-{n}", n,
                    "Mars text", 2, 0, False) for n in range(65)]
    model = FakeEmbedder()
    manifest = {"model": model.model_name, "dimension": model.dimension,
                "fastembed_version": model.fastembed_version}
    events = []
    build_index(tmp_path / "index.sqlite", chunks, model, manifest,
                progress=lambda phase, completed, total, message:
                events.append((phase, completed, total)))
    assert [event[1:] for event in events if event[0] == "embedding"] == [
        (0, 65), (32, 65), (64, 65), (65, 65)
    ]
    assert events[-1] == ("sqlite", 1, 1)
    assert read_manifest(tmp_path / "index.sqlite") == manifest


def test_shared_pipeline_reports_real_stages(tmp_path):
    corpus = tmp_path / "corpus"
    make_corpus(corpus)
    events = []
    db = tmp_path / "index.sqlite"
    manifest = build_project_index(
        db, corpus, tmp_path / "cache", min_words=0,
        embedder_factory=FakeEmbedder,
        progress=lambda phase, completed, total, message:
        events.append((phase, completed, total)),
    )
    assert manifest["files"] == 4 and manifest["chapters"] == 8
    assert manifest["chunks"]["fixed"] > 0 and manifest["chunks"]["structure"] > 0
    assert [phase for phase, _, _ in events[:2]] == ["reading", "reading"]
    assert ("loading_model", None, None) in events
    assert ("chunking_fixed", 1, 1) in events
    assert ("chunking_structure", 1, 1) in events
    embedding = [event for event in events if event[0] == "embedding"]
    assert embedding[0][1] == 0 and embedding[-1][1] == embedding[-1][2]
    assert events[-1] == ("sqlite", 1, 1)
    assert read_manifest(db) == manifest


def test_old_book_index_is_rejected_even_with_current_hash(tmp_path):
    import sqlite3, json
    corpus = tmp_path / "repo"
    make_corpus(corpus)
    db = tmp_path / "index.sqlite"
    seed_index(db, corpus)
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT data FROM manifest").fetchone()
        manifest = json.loads(row[0]); manifest.pop("corpus_kind")
        conn.execute("UPDATE manifest SET data=?", (json.dumps(manifest),))
    with TestClient(create_app(db=db, corpus=corpus)) as client:
        assert client.get("/api/state").json()["index"]["ready"] is False
        assert client.post("/api/search", json={"query": "Mars"}).status_code == 409


def test_source_filter_uses_full_relative_path(tmp_path):
    corpus = tmp_path / "repo"
    make_corpus(corpus)
    db = tmp_path / "index.sqlite"
    seed_index(db, corpus)
    with TestClient(create_app(db=db, corpus=corpus)) as client:
        data = client.get("/api/chunks", params={"source": "day-01/README.md"}).json()
        assert data["items"] and all(c["source"] == "day-01/README.md" for c in data["items"])
        assert client.get("/api/chunks", params={"source": "notes.md"}).status_code == 422


def test_search_api_reports_source_scope_and_unknown_day_empty(tmp_path):
    corpus = tmp_path / 'corpus'
    make_corpus(corpus)
    db = tmp_path / 'index.sqlite'
    seed_index(db, corpus)
    with TestClient(create_app(db=db, corpus=corpus, embedder_factory=FakeEmbedder)) as client:
        result = client.post('/api/search', json={'query': 'Day7', 'top_k': 5}).json()
        assert result['retrieval_scope']['sources'] == ['day-07/README.md']
        assert all(hit['source'] == 'day-07/README.md' for hits in result['results'].values() for hit in hits)
        unknown = client.post('/api/search', json={'query': 'Day99'})
        assert unknown.status_code == 200
        assert unknown.json()['retrieval_scope']['sources'] == ['day-99/README.md']
        assert unknown.json()['results'] == {'fixed': [], 'structure': []}
