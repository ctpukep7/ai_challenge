import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import QUESTIONS, create_app
from backend.llm import LLMError


class FakeEmbedder:
    model_name = "test-model"
    dimension = 3
    max_tokens = 126
    fastembed_version = None

    def __init__(self, cache):
        self.cache = cache

    def offsets(self, text):
        import re
        return [(match.start(), match.end()) for match in re.finditer(r"\S+", text)]

    def count(self, text):
        return len(self.offsets(text))

    def embed(self, texts):
        return [[float(len(text) % 11 + 1), float(text.lower().count("архитектура") + 1), 1.0]
                for text in texts]


@pytest.fixture
def corpus(tmp_path):
    from repo_fixture import make_tracked_corpus
    return make_tracked_corpus(tmp_path / "repo")


def prepared_db(tmp_path, corpus):
    from backend.day21_adapter import build_project_index
    db = tmp_path / "index.sqlite"
    build_project_index(db, corpus, tmp_path / "cache", min_words=0,
                        embedder_factory=FakeEmbedder)
    return db


def test_control_questions_match_tracked_readmes():
    from backend.day21_adapter import CORPUS
    from corpus import load_corpus
    docs, empty = load_corpus(CORPUS)
    sections = {(d.source, s.title): s.text.casefold() for d in docs for s in d.sections}
    assert len(QUESTIONS) == len({q["id"] for q in QUESTIONS}) == 10
    for item in QUESTIONS:
        matches = [body for (source, section), body in sections.items()
                   if source == item["source"] and section.endswith(item["section"])]
        assert matches, item["id"]
        assert all(any(alias.casefold() in body for body in matches for alias in expected["aliases"])
                   for expected in item["expected"]), item["id"]


def test_real_day21_build_and_read_only_search(tmp_path, corpus):
    from backend.day21_adapter import retrieve, index_state
    db = prepared_db(tmp_path, corpus)
    before = db.read_bytes()
    state = index_state(db, corpus)
    assert state["ready"] is True
    assert state["manifest"]["files"] == 4
    assert state["manifest"]["chunks"]["fixed"] > 0
    assert state["manifest"]["chunks"]["structure"] > 0
    hits = retrieve("Что такое чистая архитектура?", "structure", 5, db,
                    tmp_path / "cache", FakeEmbedder, corpus)
    assert len(hits) == 5
    assert all({"source", "section", "chunk_id", "score", "text"} <= set(hit) for hit in hits)
    assert db.read_bytes() == before


def test_compare_two_independent_calls_and_exact_context(tmp_path, corpus):
    db = prepared_db(tmp_path, corpus)
    calls = []
    chunks = [{"source": QUESTIONS[6]["source"],
               "title": "Чистая архитектура: принципы SOLID",
               "section": QUESTIONS[6]["section"],
               "chunk_id": "id1",
               "text": "Сущности открыты для расширения и закрыты для изменения.", "score": 0.7}]

    def retrieve_fake(question, strategy, top_k, db, cache):
        assert (strategy, top_k) == ("structure", 5)
        return chunks

    def llm_fake(messages):
        calls.append(messages)
        answer = "Открыты для расширения и закрыты для изменения"
        return {"answer": f"{answer} [1]" if len(calls) == 2 else answer,
                "usage": {"total_tokens": 20}}

    clock = iter([0, 0.125, 1, 1.75, 2, 3.5]).__next__
    app = create_app(db=db, corpus=corpus, retriever=retrieve_fake, llm=llm_fake, clock=clock)
    client = TestClient(app)
    response = client.post("/api/compare", json={
        "question": QUESTIONS[6]["question"], "question_id": "q07",
        "strategy": "structure", "top_k": 5,
    })
    assert response.status_code == 200
    result = response.json()
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0]
    assert calls[0][1]["content"] == result["question"]
    assert "chunk_id: id1" in calls[1][1]["content"]
    assert result["chunks"][0]["marker"] == "[1]"
    assert result["with_rag"]["evaluation"]["expected_source_retrieved"] is True
    assert result["with_rag"]["evaluation"]["citation_markers_valid"] is True
    assert result["retrieval_ms"] == 125
    assert result["without_rag"]["elapsed_ms"] == 750
    assert result["with_rag"]["elapsed_ms"] == 1500
    assert result["with_rag"]["prompt_chars"] > result["without_rag"]["prompt_chars"]
    assert result["thinking"] == "disabled"
    assert result["max_tokens"] == 400


def test_missing_corpus_and_index_are_distinguished(tmp_path, corpus):
    missing_corpus = tmp_path / "missing-corpus"
    app = create_app(db=tmp_path / "missing.sqlite", corpus=missing_corpus)
    client = TestClient(app)
    state = client.get("/api/state").json()["index"]
    assert state["ready"] is False and state["corpus_ready"] is False
    assert "репозиторий" in state["reason"].lower()
    assert client.post("/api/build").status_code == 409
    assert client.post("/api/compare", json={"question": "Что такое база данных?"}).status_code == 409

    prepared = create_app(db=tmp_path / "still-missing.sqlite", corpus=corpus)
    state = TestClient(prepared).get("/api/state").json()["index"]
    assert state["ready"] is False and state["corpus_ready"] is True
    assert "Индекс ещё не построен" in state["reason"]


def test_partial_llm_failure(tmp_path, corpus):
    db = prepared_db(tmp_path, corpus)
    calls = 0

    def llm(messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LLMError("DeepSeek timeout")
        return {"answer": "База данных — деталь [1]", "usage": {"total_tokens": 10}}

    clock = iter([0, 0.05, 1, 76, 77, 78]).__next__
    app = create_app(db=db, corpus=corpus, llm=llm, clock=clock,
                     retriever=lambda *args: [{"source": "clean-architecture-boundaries.md",
                                                "title": "Чистая архитектура: границы и детали",
                                                "section": "Глава 30 — База данных — это деталь",
                                                "chunk_id": "x", "text": "База данных — деталь",
                                                "score": 0.5}])
    result = TestClient(app).post("/api/compare", json={"question": "База данных?"}).json()
    assert result["without_rag"]["answer"] is None
    assert result["without_rag"]["error"] == "DeepSeek timeout"
    assert result["with_rag"]["answer"] == "База данных — деталь [1]"
    assert result["retrieval_ms"] == 50
    assert result["without_rag"]["elapsed_ms"] == 75000
    assert result["with_rag"]["elapsed_ms"] == 1000
    assert calls == 2


def test_retry_only_failed_mode_with_same_retrieval_parameters(tmp_path, corpus):
    db = prepared_db(tmp_path, corpus)
    calls, searches = [], []

    def retrieve_fake(question, strategy, top_k, db, cache):
        searches.append((strategy, top_k))
        return [{"source": "clean-architecture-boundaries.md",
                 "title": "Чистая архитектура: границы и детали",
                 "section": "Глава 30 — База данных — это деталь",
                 "chunk_id": "db-1", "text": "База данных — деталь.", "score": 0.8}]

    def llm_fake(messages):
        calls.append(messages)
        if len(calls) == 2:
            raise LLMError("Превышено время ожидания ответа DeepSeek")
        return {"answer": "База данных — деталь", "usage": {"total_tokens": 11}}

    clock = iter([0, 0.02, 1, 2, 3, 78, 79, 79.01, 80, 80.5]).__next__
    app = create_app(db=db, corpus=corpus, retriever=retrieve_fake, llm=llm_fake, clock=clock)
    payload = {"question": QUESTIONS[9]["question"], "question_id": "q10",
               "strategy": "fixed", "top_k": 3}
    with TestClient(app) as client:
        partial = client.post("/api/compare", json=payload).json()
        resumed = client.post("/api/compare", json={**payload, "modes": ["with_rag"]}).json()
    assert partial["without_rag"]["answer"] == "База данных — деталь"
    assert partial["with_rag"]["answer"] is None
    assert resumed["without_rag"] is None
    assert resumed["with_rag"]["answer"] == "База данных — деталь"
    assert searches == [("fixed", 3), ("fixed", 3)]
    assert len(calls) == 3
    assert resumed["chunks"][0]["chunk_id"] == "db-1"
    assert partial["without_rag"]["elapsed_ms"] == 1000
    assert partial["with_rag"]["elapsed_ms"] == 75000
    assert resumed["retrieval_ms"] == 10
    assert resumed["with_rag"]["elapsed_ms"] == 500
    assert resumed["with_rag"]["prompt_chars"] == partial["with_rag"]["prompt_chars"]


def test_app_closes_owned_llm_client(tmp_path, monkeypatch):
    from backend import app as app_module
    closed = []

    class FakeService:
        def __call__(self, messages):
            return {"answer": "ok"}

        def close(self):
            closed.append(True)

    monkeypatch.setattr(app_module, "DeepSeekClient", FakeService)
    app = create_app(db=tmp_path / "missing.sqlite")
    with TestClient(app) as client:
        assert client.get("/api/state").status_code == 200
    assert closed == [True]


def test_old_corpus_kind_rejected_with_valid_hash(tmp_path, corpus):
    import sqlite3
    from backend.day21_adapter import index_state
    db = prepared_db(tmp_path, corpus)
    with sqlite3.connect(db) as conn:
        manifest = json.loads(conn.execute("SELECT data FROM manifest").fetchone()[0])
        manifest["corpus_kind"] = "private_books"
        conn.execute("UPDATE manifest SET data=?", (json.dumps(manifest),))
    assert index_state(db, corpus)["ready"] is False


def test_explicit_source_scope_real_retrieval_and_unknown_day_has_no_generation(tmp_path, corpus):
    from backend.day21_adapter import retrieve
    db = prepared_db(tmp_path, corpus)
    calls = []
    def retriever(query, strategy, top_k, path, cache):
        return retrieve(query, strategy, top_k, path, cache, FakeEmbedder, corpus)
    client = TestClient(create_app(db=db, corpus=corpus, retriever=retriever,
                                  llm=lambda messages: calls.append(messages) or {'answer': 'Ответ [1]'}))
    result = client.post('/api/compare', json={'question': 'Как сохраняет память Day7?'}).json()
    assert result['retrieval_scope']['sources'] == ['day-07/README.md']
    assert result['chunks'] and all(chunk['source'] == 'day-07/README.md' for chunk in result['chunks'])
    assert 'source: day-07/README.md' in calls[1][1]['content']
    assert 'source: day-01/README.md' not in calls[1][1]['content']
    assert 'source: README.md' not in calls[1][1]['content']
    calls.clear()
    response = client.post('/api/compare', json={'question': 'Day99 память?'})
    assert response.status_code == 422
    assert 'day-99/README.md' in response.json()['detail']
    assert calls == []
