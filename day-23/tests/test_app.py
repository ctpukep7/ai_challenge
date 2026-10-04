import json
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from backend.app import QUESTIONS, create_app
from backend.llm import LLMError
from conftest import FakeEmbedder, completed
from test_logic import candidate


def service(db, corpus, **kwargs):
    return TestClient(create_app(db=db, corpus=corpus,
                                llm=kwargs.pop("llm", lambda messages: {"answer": "пример [1]"}), **kwargs))


def post(client, payload):
    response = client.post("/api/compare", json={"question": "Что такое код?", **payload})
    assert response.status_code == 202, response.text
    return completed(client, response.json()["id"])


def test_control_sources_valid_and_negative_questions_distinct():
    from backend.day21_adapter import _snapshot, CORPUS
    docs, _, _, _ = _snapshot(CORPUS)
    sections = {(document.source, section.title.split(" / ")[-1]) for document in docs for section in document.sections}
    positives = [item for item in QUESTIONS if not item["unanswerable"]]
    negatives = [item for item in QUESTIONS if item["unanswerable"]]
    assert len(positives) == 10 and len(negatives) == 3 and len({item["id"] for item in QUESTIONS}) == 13
    assert {(item["source"], item["section"]) for item in positives} <= sections
    assert all(item["expected"] for item in positives)
    assert all(not item["expected"] and item["source"] is None for item in negatives)


def test_missing_corpus_stale_and_broken_index_report_without_loading_model(tmp_path, corpus, db):
    client = service(tmp_path / "absent.sqlite", tmp_path / "missing")
    assert client.get("/api/state").json()["index"]["corpus_ready"] is False
    assert client.post("/api/build").status_code == 409
    assert client.post("/api/compare", json={"question": "test"}).status_code == 409
    empty = service(tmp_path / "absent.sqlite", corpus)
    assert empty.get("/api/state").json()["index"]["corpus_ready"] is True
    assert empty.get("/api/state").json()["index"]["ready"] is False
    source = next(corpus.glob("*.md"))
    source.write_text(source.read_text() + "\nИзменение корпуса.")
    stale = service(db, corpus)
    assert stale.get("/api/state").json()["index"]["ready"] is False
    assert stale.post("/api/compare", json={"question": "test"}).status_code == 409
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not sqlite")
    assert service(bad, corpus).get("/api/state").json()["index"]["ready"] is False


@pytest.mark.parametrize("payload", [{"question": "   "}, {"question": "x" * 601}, {"top_k_before": 0},
    {"top_k_before": 31}, {"top_k_after": 11}, {"top_k_after": 4, "top_k_before": 3},
    {"threshold": "nan"}, {"threshold": "inf"}, {"threshold": 1.1}, {"threshold": -1.1},
    {"top_k_before": True}, {"rewrite_method": "unknown"}, {"strategy": "unknown"},
    {"question_id": "q99"}, {"rewrite_method": "llm", "generate_answers": False}])
def test_validation_precedes_any_work(db, corpus, payload):
    calls = []
    client = service(db, corpus, retriever=lambda *args, **kwargs: calls.append(args))
    response = client.post("/api/compare", json={"question": "test", **payload})
    assert response.status_code == 422
    assert calls == []


def test_three_modes_real_filter_query_rewrite_exact_original_prompts(db, corpus):
    searches, calls = [], []
    def retriever(query, strategy, before, *_, **kwargs):
        searches.append((query, strategy, before))
        return [candidate(.8, "KEEP"), candidate(.55, "REJECT SECRET"), candidate(.4, "LOW")]
    def llm(messages):
        calls.append(messages)
        return {"answer": "пример [1]", "usage": {"total_tokens": 10}}
    client = service(db, corpus, retriever=retriever, llm=llm)
    result = post(client, {"question": "Как работает cron?", "generate_answers": True, "top_k_before": 12,
                           "top_k_after": 2, "threshold": .6})
    assert result["status"] == "completed"
    assert searches == [("Как работает cron?", "structure", 12),
                        ("Как работает cron планировщик фоновых задач?", "structure", 12)]
    row = result["results"][0]
    assert row["rewrite"]["method"] == "local"
    assert len(calls) == 3
    assert all("Исходный вопрос: Как работает cron?" in message[1]["content"] for message in calls)
    assert all(calls[0][0] == messages[0] for messages in calls)
    assert "REJECT SECRET" in calls[0][1]["content"]
    assert all("REJECT SECRET" not in messages[1]["content"] for messages in calls[1:])
    assert row["modes"]["baseline"]["counts"]["kept"] == 2
    assert row["modes"]["filtered"]["counts"]["kept"] == 1
    assert row["modes"]["rewritten"]["counts"]["kept"] == 1
    assert result["summary"]["calls"] == {"answer": 3, "rewrite": 0, "total": 3}


def test_no_results_and_filtered_empty_are_policy_no_llm_calls(db, corpus):
    calls = []
    client = service(db, corpus, retriever=lambda *args, **kwargs: [candidate(.2)],
                     llm=lambda messages: calls.append(messages) or {"answer": "[1]"})
    result = post(client, {"generate_answers": True})
    assert len(calls) == 1
    modes = result["results"][0]["modes"]
    assert modes["baseline"]["generated_by"] == "llm"
    for name in ("filtered", "rewritten"):
        assert modes[name]["answer_status"] == "abstained"
        assert modes[name]["generated_by"] == "policy"
        assert modes[name]["evaluation"] is None
        assert modes[name]["chunks"] == []
    client2 = service(db, corpus, retriever=lambda *args, **kwargs: [], llm=lambda messages: pytest.fail("No-call policy"))
    result2 = post(client2, {"generate_answers": True})
    assert result2["summary"]["calls"]["total"] == 0
    assert all(mode["answer_status"] == "abstained" for mode in result2["results"][0]["modes"].values())


def test_retrieval_only_benchmark_is_zero_external_calls_with_honest_negative_metrics(db, corpus):
    def retriever(query, *_, **kwargs):
        return [candidate(.2 if "борщ" in query or "погода" in query or "авиабилет" in query else .8)]
    client = service(db, corpus, retriever=retriever, llm=lambda messages: pytest.fail("Search-only called LLM"))
    response = client.post("/api/benchmark", json={"generate_answers": False})
    result = completed(client, response.json()["id"])
    assert result["status"] == "completed" and result["current"] == result["total"] == 13
    assert result["summary"]["retrieval"]["common_count"] == 13
    assert result["summary"]["generation"]["common_count"] == 0
    assert result["summary"]["calls"]["total"] == 0
    metrics = result["summary"]["retrieval"]["modes"]
    assert metrics["baseline"]["negative_empty_rate"] == 0
    assert metrics["filtered"]["negative_empty_rate"] == 1
    assert metrics["filtered"]["negative_count"] == 3
    assert all(mode["answer_status"] == "not_requested" for row in result["results"] for mode in row["modes"].values())


@pytest.mark.parametrize("response", [None, {"answer": "bad json"}, {"answer": '{"query":""}'}])
def test_llm_rewrite_error_no_hidden_fallback_baseline_preserved(db, corpus, response):
    searches, calls = [], []
    def llm(messages):
        calls.append(messages)
        if len(calls) == 1:
            if response is None:
                raise LLMError("rewrite timeout")
            return response
        return {"answer": "пример [1]"}
    def retriever(query, *_, **kwargs):
        searches.append(query)
        return [candidate(.8)]
    result = post(service(db, corpus, retriever=retriever, llm=llm),
                  {"generate_answers": True, "rewrite_method": "llm"})
    row = result["results"][0]
    assert searches == ["Что такое код?"]
    assert row["rewrite"]["status"] == "failed"
    assert row["modes"]["rewritten"]["answer_status"] == "failed"
    assert row["modes"]["rewritten"]["chunks"] == []
    assert row["modes"]["baseline"]["answer_status"] == "completed"
    assert result["summary"]["calls"] == {"answer": 2, "rewrite": 1, "total": 3}
    assert result["summary"]["retrieval"]["common_count"] == 0


def test_llm_rewrite_is_actual_second_query_and_separate_short_prompt(db, corpus):
    calls, searches = [], []
    def llm(messages):
        calls.append(messages)
        return {"answer": '{"query":"архитектура баз данных"}' if len(calls) == 1 else "пример [1]"}
    def retriever(query, *_, **kwargs):
        searches.append(query)
        return [candidate(.8, "source content")]
    result = post(service(db, corpus, retriever=retriever, llm=llm), {"generate_answers": True, "rewrite_method": "llm"})
    assert searches == ["Что такое код?", "архитектура баз данных"]
    assert calls[0][1]["content"] == "Что такое код?"
    assert "source content" not in str(calls[0])
    assert all("Исходный вопрос: Что такое код?" in messages[1]["content"] for messages in calls[1:])
    assert result["summary"]["calls"]["total"] == 4


def test_partial_answer_failure_does_not_zero_average(db, corpus):
    calls = []
    def llm(messages):
        calls.append(messages)
        if len(calls) == 2:
            raise LLMError("timeout")
        return {"answer": "очень маленькими [1]"}
    question = QUESTIONS[1]
    result = post(service(db, corpus, retriever=lambda *args, **kwargs: [candidate(.8)], llm=llm),
                  {"question": question["question"], "question_id": question["id"], "generate_answers": True})
    assert result["results"][0]["modes"]["filtered"]["error"] == "timeout"
    assert result["results"][0]["modes"]["rewritten"]["answer_status"] == "completed"
    assert result["summary"]["generation"]["common_count"] == 0
    assert result["summary"]["retrieval"]["common_count"] == 1
    assert result["summary"]["generation"]["modes"]["baseline"]["fact_ratio"] is None


def test_fixture_does_not_evaluate_edited_control_text(db, corpus):
    result = post(service(db, corpus, retriever=lambda *args, **kwargs: [candidate(.8)]), {"question_id": "q01"})
    assert result["results"][0]["question_id"] is None
    assert result["summary"]["retrieval"]["common_count"] == 0


def test_cancel_current_http_call_finishes_next_mode_never_called_and_busy_guard(db, corpus):
    entered, finish = Event(), Event()
    calls = []
    def llm(messages):
        calls.append(messages)
        entered.set()
        assert finish.wait(3)
        return {"answer": "пример [1]"}
    client = service(db, corpus, retriever=lambda *args, **kwargs: [candidate(.8)], llm=llm)
    started = client.post("/api/compare", json={"question": "код?", "generate_answers": True})
    identifier = started.json()["id"]
    assert entered.wait(3)
    assert client.get(f"/api/runs/{identifier}").json()["status"] == "running"
    assert client.post("/api/compare", json={"question": "другой?"}).status_code == 409
    assert client.post("/api/build").status_code == 409
    assert client.post(f"/api/runs/{identifier}/cancel").json()["cancel_requested"] is True
    finish.set()
    result = completed(client, identifier)
    assert result["status"] == "cancelled" and len(calls) == 1
    assert result["results"][0]["modes"]["baseline"]["answer_status"] == "completed"
    assert result["results"][0]["modes"]["filtered"]["answer_status"] == "not_requested"
    assert client.post("/api/compare", json={"question": "другой?"}).status_code == 202


def test_state_recovers_active_run_after_refresh_and_can_cancel(db, corpus):
    entered, finish = Event(), Event()
    calls = []
    def llm(messages):
        calls.append(messages)
        entered.set()
        assert finish.wait(3)
        return {"answer": "пример [1]"}
    app = create_app(db=db, corpus=corpus, retriever=lambda *args, **kwargs: [candidate(.8)], llm=llm)
    first_client = TestClient(app)
    assert first_client.get("/api/state").json()["active_run"] is None
    response = first_client.post("/api/compare", json={"question": "код?", "generate_answers": True})
    identifier = response.json()["id"]
    assert entered.wait(3)
    # A fresh client has no remembered job id; GET state must recover it without starting work.
    refreshed = TestClient(app)
    active = refreshed.get("/api/state").json()["active_run"]
    assert active == {"id": identifier, "status": "running"}
    assert refreshed.get(f"/api/runs/{active['id']}").json()["kind"] == "compare"
    assert refreshed.post(f"/api/runs/{active['id']}/cancel").json()["cancel_requested"] is True
    finish.set()
    assert completed(refreshed, identifier)["status"] == "cancelled"
    assert refreshed.get("/api/state").json()["active_run"] is None
    assert len(calls) == 1


def test_history_bounded_and_unknown_job_404(db, corpus):
    client = service(db, corpus, retriever=lambda *args, **kwargs: [])
    identifiers = []
    for _ in range(22):
        response = client.post("/api/compare", json={"question": "код?"})
        identifiers.append(response.json()["id"])
        assert completed(client, identifiers[-1])["status"] == "completed"
    assert client.get(f"/api/runs/{identifiers[0]}").status_code == 404
    assert client.get(f"/api/runs/{identifiers[2]}").status_code == 200
    assert client.post("/api/runs/unknown/cancel").status_code == 404


def test_real_day21_readonly_search_own_index_lazy_cached_embedder(db, corpus, tmp_path):
    from backend.day21_adapter import retrieve, _EMBEDDERS
    instances = []
    class CountEmbedder(FakeEmbedder):
        def __init__(self, cache):
            instances.append(cache)
            super().__init__(cache)
    before = db.read_bytes()
    cache = tmp_path / "unique-cache"
    hits = retrieve("архитектура", "structure", 12, db, cache, CountEmbedder, corpus)
    assert len(hits) > 0
    assert all({"source", "title", "section", "chunk_id", "score", "text"} <= hit.keys() for hit in hits)
    retrieve("архитектура", "fixed", 12, db, cache, CountEmbedder, corpus)
    assert len(instances) == 1 and db.read_bytes() == before
    with pytest.raises(ValueError, match="лимита модели"):
        retrieve("слово " * 127, "fixed", 12, db, cache, CountEmbedder, corpus)


def test_full_job_integrates_real_sqlite_retrieval_and_fake_llm(db, corpus, tmp_path):
    from backend.day21_adapter import retrieve
    before = db.read_bytes()
    calls = []
    def retriever(query, strategy, top_k, path, cache, **kwargs):
        return retrieve(query, strategy, top_k, path, cache, FakeEmbedder, corpus, **kwargs)
    def llm(messages):
        calls.append(messages)
        return {"answer": "Авторский пример [1]"}
    client = service(db, corpus, cache=tmp_path / "cache", retriever=retriever, llm=llm)
    result = post(client, {"question": "Как работает MCP?", "threshold": -1, "generate_answers": True})
    assert result["status"] == "completed" and len(calls) == 3
    assert all(mode["counts"]["kept"] == 3 for mode in result["results"][0]["modes"].values())
    assert all("Авторский тестовый пример" in messages[1]["content"] for messages in calls)
    assert result["results"][0]["rewrite"]["changed"] is True
    assert db.read_bytes() == before


def test_build_reports_progress_excludes_concurrent_run_and_preserves_error(tmp_path, corpus):
    started, finish = Event(), Event()
    def builder(db, corpus, cache, progress):
        started.set()
        progress("reading", 1, 4, "reading")
        assert finish.wait(3)
        raise ValueError("invalid corpus")
    client = service(tmp_path / "new.sqlite", corpus, builder=builder)
    assert client.post("/api/build").status_code == 202
    assert started.wait(3)
    assert client.get("/api/build").json()["phase"] == "reading"
    assert client.post("/api/build").status_code == 409
    finish.set()
    import time
    for _ in range(100):
        result = client.get("/api/build").json()
        if result["status"] != "running":break
        time.sleep(.005)
    assert result["status"] == "failed" and result["error"] == "invalid corpus"
