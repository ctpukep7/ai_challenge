import asyncio
import hashlib
import json
import sqlite3
import struct
import threading
from contextlib import asynccontextmanager
from importlib.metadata import version

import httpx
import pytest

from backend import app as module
from backend.errors import APIError
from backend.local_llm import MODEL, URL
from backend.logic import CloudBaseline, prepare_context
from backend.retrieval import Retriever, OfflineEmbedder, MODEL_NAME, DIMENSION
from index_store import SCHEMA


class FakeEmbedder:
    model_name, dimension, max_tokens = MODEL_NAME, DIMENSION, 126
    fastembed_version = version("fastembed")
    def __init__(self, cache):
        self.calls = 0
    def count(self, text):
        return len(text.split())
    def embed(self, texts):
        self.calls += 1
        return [[1.0] + [0.0] * (DIMENSION - 1) for _ in texts]


def make_retriever(tmp_path, scores=(1.0, .8, .6), texts=None):
    db, cache = tmp_path / "index.sqlite", tmp_path / "cache"
    cache.mkdir()
    (cache / "cached").write_text("present")
    manifest = {"schema_version": 1, "corpus_kind": "repository_readmes", "model": MODEL_NAME,
                "dimension": DIMENSION, "fastembed_version": version("fastembed"), "files": 1,
                "sections": 1, "chunks": {"fixed": 1, "structure": len(scores)}, "corpus_sha256": "snapshot"}
    with sqlite3.connect(db) as con:
        con.executescript(SCHEMA)
        con.execute("INSERT INTO manifest VALUES(1,?)", (json.dumps(manifest),))
        for index, score in enumerate(scores):
            vector = [score, (1 - score**2)**.5] + [0.0] * (DIMENSION - 2)
            text = texts[index] if texts else f"Фрагмент {index} о SQLite"
            con.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("structure", f"chunk-{index}", "day-07/README.md", "День 7", "История", index, text, 10, 0, 0, struct.pack(f"<{DIMENSION}f", *vector)))
    return Retriever(db, cache, tmp_path, embedder_factory=FakeEmbedder, digest=lambda _: "current")


def response(**overrides):
    return {"done": True, "message": {"content": "История хранится в SQLite [1]."}, "load_duration": 1_000_000,
            "eval_duration": 2_000_000_000, "prompt_eval_count": 50, "eval_count": 60, "done_reason": "stop", **overrides}


def transport(chat=None, calls=None, models=None):
    async def handle(request):
        assert str(request.url).startswith(URL + "/api/")
        if calls is not None:
            calls.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": MODEL}] if models is None else models})
        if chat:
            result = chat(request)
            return await result if hasattr(result, "__await__") else result
        return httpx.Response(200, json=response())
    return httpx.MockTransport(handle)


@asynccontextmanager
async def client_for(retriever, provider=None, **kwargs):
    app = module.create_app(retriever=retriever, transport=provider or transport(), **kwargs)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, app


def post(retriever, body=None, provider=None):
    async def run():
        async with client_for(retriever, provider) as (client, _):
            return await client.post("/api/ask", json=body if body is not None else {"question": "Как в дне 7 сохраняется контекст?"})
    return asyncio.run(run())


def test_read_only_retrieval_scope_actual_context_metrics_and_options(tmp_path):
    retriever = make_retriever(tmp_path)
    before = hashlib.sha256(retriever.db.read_bytes()).hexdigest()
    calls = []
    result = post(retriever, provider=transport(calls=calls))
    assert result.status_code == 200
    data = result.json()
    assert data["scope"]["sources"] == ["day-07/README.md"]
    assert data["status"] == "answered" and data["sources"][0]["cited"]
    assert data["metrics"]["embedding_cold"] is True
    assert data["metrics"]["embedding_load_seconds"] >= 0
    assert data["metrics"]["tokens_per_second"] == 30
    assert data["metrics"]["retrieval_seconds"] > 0
    assert hashlib.sha256(retriever.db.read_bytes()).hexdigest() == before
    payload = json.loads(calls[-1].content)
    assert payload["options"] == {"temperature": .7, "num_predict": 1024, "num_ctx": 4096}
    assert payload["think"] is False and payload["stream"] is False and payload["keep_alive"] == "1m"
    assert payload["model"] == MODEL
    assert len("".join(item["content"] for item in payload["messages"]).encode()) <= 8000
    assert all(source["text"] in payload["messages"][1]["content"] for source in data["sources"])
    assert len({source["chunk_id"] for source in data["sources"]}) == len(data["sources"])
    again = post(retriever).json()
    assert again["metrics"]["embedding_cold"] is False


def test_threshold_and_neighbor_provenance(tmp_path):
    retriever = make_retriever(tmp_path, scores=(1.0, .3, .1))
    found = retriever.retrieve("Как в дне 7 сохраняется история?")
    assert [source["role"] for source in found["evidence"]] == ["anchor", "neighbor"]
    assert found["evidence"][1]["score"] is None
    assert all(source["source"] == "day-07/README.md" for source in found["evidence"])


def test_weak_or_outside_context_never_generates(tmp_path):
    retriever = make_retriever(tmp_path, scores=(.1,))
    calls = []
    result = post(retriever, provider=transport(calls=calls)).json()
    assert result["status"] == "unknown" and result["reason"] == "weak_context"
    assert result["sources"] == [] and result["metrics"]["generation_seconds"] == 0
    assert result["metrics"]["total_seconds"] >= result["metrics"]["retrieval_seconds"] > 0
    assert calls == []
    assert post(retriever, {"question": "Как в дне 99 запускается корабль?"}).json()["status"] == "unknown"


def test_whole_chunk_budget_omits_large_and_keeps_actual_metadata(tmp_path):
    retriever = make_retriever(tmp_path, scores=(1.0, .8), texts=["я" * 3100, "Полезные данные SQLite"])
    result = post(retriever).json()
    assert [source["chunk_id"] for source in result["sources"]] == ["chunk-1"]
    assert result["sources"][0]["text"] == "Полезные данные SQLite"
    assert result["sources"][0]["marker"] == "[1]"


def test_context_that_cannot_fit_does_not_call_model(tmp_path):
    calls = []
    result = post(make_retriever(tmp_path, scores=(1.0,), texts=["я" * 4000]), provider=transport(calls=calls)).json()
    assert result["reason"] == "context_limit" and result["status"] == "unknown"
    assert calls == []


def test_full_messages_budget_is_enforced():
    evidence = [{"text": "a", "source": "x" * 10000, "section": "y", "chunk_id": "id", "role": "anchor", "score": .8}]
    assert prepare_context("Вопрос", evidence) == ([], [])


@pytest.mark.parametrize("body,status", [({}, 422), ({"question": " "}, 422), ({"question": 5}, 422),
    ({"question": "Вопрос", "history": []}, 422), ({"question": "a" * 501}, 413), ({"question": "€" * 400}, 413)])
def test_bad_questions_rejected_before_retrieval(tmp_path, body, status):
    retriever = make_retriever(tmp_path)
    result = post(retriever, body)
    assert result.status_code == status
    assert retriever.embedder is None


def test_invalid_unicode(tmp_path):
    async def run():
        async with client_for(make_retriever(tmp_path)) as (client, _):
            result = await client.post("/api/ask", content=b'{"question":"\\ud800"}', headers={"Content-Type": "application/json"})
            assert result.status_code == 422
    asyncio.run(run())


def test_embedding_query_too_long_before_llm(tmp_path):
    calls = []
    result = post(make_retriever(tmp_path), {"question": "x " * 127}, transport(calls=calls))
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "embedding_query_too_long"
    assert calls == []


def test_state_lazy_snapshot_warning_and_no_expected_facts(tmp_path):
    retriever = make_retriever(tmp_path)
    calls = []
    async def run():
        async with client_for(retriever, transport(calls=calls)) as (client, _):
            result = (await client.get("/api/state")).json()
            assert result["status"] == "ready"
            assert result["index"]["snapshot_differs"] is True
            assert result["index"]["corpus_sha256"] == "snapshot"
            assert all(set(question) == {"id", "question", "label"} for question in result["questions"])
    asyncio.run(run())
    assert retriever.embedder is None
    assert [call.url.path for call in calls] == ["/api/tags"]


def test_incompatible_index_and_missing_cache(tmp_path):
    retriever = make_retriever(tmp_path)
    (retriever.cache / "cached").unlink()
    assert retriever.index_state()["ready"] is False
    result = post(retriever)
    assert result.status_code == 503
    assert retriever.embedder is None


def test_offline_constructor_flags_and_telemetry(tmp_path, monkeypatch):
    import fastembed
    import onnxruntime
    from types import SimpleNamespace
    flags = {}
    class Tokenizer:
        truncation = {"max_length": 128}
        def no_truncation(self):
            flags["no_truncation"] = True
    def model(**kwargs):
        flags.update(kwargs)
        return SimpleNamespace(model=SimpleNamespace(tokenizer=Tokenizer()))
    monkeypatch.setattr(fastembed, "TextEmbedding", model)
    monkeypatch.setattr(onnxruntime, "disable_telemetry_events", lambda: flags.update(telemetry_disabled=True))
    (tmp_path / "cached").write_text("present")
    OfflineEmbedder(tmp_path)
    assert flags["local_files_only"] is True
    assert flags["threads"] == 2 and flags["providers"] == ["CPUExecutionProvider"]
    assert flags["telemetry_disabled"] is True and flags["no_truncation"] is True
    monkeypatch.setattr(fastembed, "TextEmbedding", lambda **_: pytest.fail("must not download"))
    with pytest.raises(APIError):
        OfflineEmbedder(tmp_path / "missing")


@pytest.mark.parametrize("bad", [response(done=False), response(message={"content": " "}), response(eval_count=0), response(eval_count=True),
                                    response(eval_duration=-1), response(load_duration=10**400), {"error": "private"}, []])
def test_malformed_model_response(tmp_path, bad):
    result = post(make_retriever(tmp_path), provider=transport(chat=lambda _: httpx.Response(200, json=bad)))
    assert result.status_code == 502
    assert result.json()["error"]["code"] == "invalid_response"


@pytest.mark.parametrize("status", [500, 302])
def test_http_error_redirect_and_no_retry(tmp_path, status):
    calls = []
    result = post(make_retriever(tmp_path), provider=transport(chat=lambda _: httpx.Response(status, text="private", headers={"Location": "https://cloud.example"}), calls=calls))
    assert result.status_code == 502
    assert "private" not in result.text and len(calls) == 2


def test_invalid_json(tmp_path):
    result = post(make_retriever(tmp_path), provider=transport(chat=lambda _: httpx.Response(200, text="{")))
    assert result.status_code == 502


@pytest.mark.parametrize("failure,status", [(httpx.ConnectError("secret"), 503), (httpx.ReadTimeout("secret"), 504)])
def test_transport_error(tmp_path, failure, status):
    def fail(_):
        raise failure
    result = post(make_retriever(tmp_path), provider=transport(chat=fail))
    assert result.status_code == status and "secret" not in result.text


def test_missing_model(tmp_path):
    result = post(make_retriever(tmp_path), provider=transport(models=[]))
    assert result.status_code == 503 and result.json()["error"]["code"] == "missing_model"


@pytest.mark.parametrize("answer,warning,status", [("Не знаю. Уточните вопрос [1].", False, "unknown"),
    ("Ответ без ссылки", True, "answered"), ("Ответ [99]", True, "answered")])
def test_citations_and_unknown_are_honest(tmp_path, answer, warning, status):
    result = post(make_retriever(tmp_path), provider=transport(chat=lambda _: httpx.Response(200, json=response(message={"content": answer}, done_reason="length")))).json()
    assert bool(result["citation_warning"]) is warning
    assert result["status"] == status and result["truncated"] is True
    assert result["reason"] == ("missing_evidence" if status == "unknown" else "")


def test_archive_exact_match_no_metrics_and_corrupt_file(tmp_path):
    path = tmp_path / "cloud.json"
    path.write_text(json.dumps({"source": "Day24", "historical": True, "note": "Архив", "rows": [
        {"question": "Вопрос", "answer": "Ответ", "status": "answered", "model": "DeepSeek (архив)", "metrics": {"fake": 1}}]}))
    baseline = CloudBaseline(path)
    assert baseline.available and baseline.match(" Вопрос ")["metrics"] is None
    assert baseline.match("Новый вопрос") is None
    path.write_text("{")
    assert not CloudBaseline(path).available


def test_timeout_holds_slot_until_thread_finishes_and_state_is_responsive(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "REQUEST_SECONDS", .05)
    retriever = make_retriever(tmp_path)
    real_retrieve = retriever.retrieve
    started, release = threading.Event(), threading.Event()
    def waiting(question):
        started.set()
        release.wait(2)
        return real_retrieve(question)
    retriever.retrieve = waiting
    calls = []
    async def run():
        async with client_for(retriever, transport(calls=calls)) as (client, app):
            first = asyncio.create_task(client.post("/api/ask", json={"question": "Вопрос"}))
            await asyncio.to_thread(started.wait, 1)
            assert (await client.get("/api/state")).json()["status"] == "ready"
            assert (await first).status_code == 504
            assert app.state.lock.locked()
            assert (await client.post("/api/ask", json={"question": "Второй"})).status_code == 409
            release.set()
            for _ in range(100):
                if not app.state.lock.locked():
                    break
                await asyncio.sleep(.01)
            assert not app.state.lock.locked()
    asyncio.run(run())
    assert [call.url.path for call in calls] == ["/api/tags"]


def test_concurrent_generation_and_error_release(tmp_path):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def wait(_):
            started.set()
            await release.wait()
            return httpx.Response(500)
        async with client_for(make_retriever(tmp_path), transport(chat=wait)) as (client, app):
            task = asyncio.create_task(client.post("/api/ask", json={"question": "Вопрос"}))
            await asyncio.wait_for(started.wait(), 2)
            assert (await client.post("/api/ask", json={"question": "Второй"})).status_code == 409
            assert (await client.get("/api/state")).status_code == 200
            release.set()
            assert (await task).status_code == 502
            assert not app.state.lock.locked()
    asyncio.run(run())


def test_static_files_cannot_mask_unknown_api(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>RAG</html>")
    async def run():
        async with client_for(make_retriever(tmp_path), dist=dist) as (client, _):
            assert "RAG" in (await client.get("/")).text
            unknown = await client.get("/api/unknown")
            assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"
    asyncio.run(run())
