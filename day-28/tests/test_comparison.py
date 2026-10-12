import asyncio
import hashlib
import json

import httpx
import pytest

from backend import app as module
from backend import cloud_llm
from backend.cloud_llm import CloudConfig
from test_app import make_retriever, client_for, transport, response


def cloud_response(**overrides):
    return {"choices": [{"message": {"content": "SQLite [2]."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 70, "completion_tokens": 30, "total_tokens": 100},
            "model": "deepseek-actual", **overrides}


def cloud_transport(chat=None, calls=None):
    async def handle(request):
        assert str(request.url) == cloud_llm.URL
        if calls is not None:
            calls.append(request)
        if chat:
            result = chat(request)
            return await result if hasattr(result, "__await__") else result
        return httpx.Response(200, json=cloud_response())
    return httpx.MockTransport(handle)


def compare(retriever, local=None, cloud=None, config=None):
    async def run():
        async with client_for(retriever, local, cloud_transport=cloud or cloud_transport(),
                              cloud_config=config if config is not None else CloudConfig(api_key="fake-test-key", model="deepseek-test")) as (client, _):
            return await client.post("/api/compare", json={"question": "Как в дне 7 сохраняется контекст?"})
    return asyncio.run(run())


def test_one_retrieval_identical_messages_independent_citations_and_usage(tmp_path):
    retriever = make_retriever(tmp_path)
    retrieve = retriever.retrieve
    invocations = []
    def counted(question):
        invocations.append(question)
        return retrieve(question)
    retriever.retrieve = counted
    local_calls, cloud_calls = [], []
    result = compare(retriever, transport(calls=local_calls), cloud_transport(calls=cloud_calls))
    assert result.status_code == 200
    data = result.json()
    local = json.loads(local_calls[-1].content)
    cloud = json.loads(cloud_calls[-1].content)
    assert local["messages"] == cloud["messages"]
    assert len(invocations) == 1 and len(cloud_calls) == 1
    assert cloud["temperature"] == local["options"]["temperature"] == .7
    assert cloud["max_tokens"] == local["options"]["num_predict"] == 1024
    assert cloud["thinking"] == {"type": "disabled"} and cloud["stream"] is False
    assert "response_format" not in cloud
    assert cloud_calls[0].headers["Authorization"] == "Bearer fake-test-key"
    assert "fake-test-key" not in result.text
    assert data["mode"] == "compare" and data["status"] == data["cloud"]["status"] == "answered"
    assert data["cloud"]["model"] == "deepseek-actual" and data["cloud"]["historical"] is False
    assert data["cloud"]["called"] is True and data["cloud"]["error"] is None
    assert data["comparison"]["messages_sha256"] == hashlib.sha256(json.dumps(local["messages"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert data["comparison"]["chunks"] == len(data["sources"]) == len(data["cloud"]["sources"])
    assert data["sources"][0]["cited"] is True and data["cloud"]["sources"][0]["cited"] is False
    assert data["sources"][1]["cited"] is False and data["cloud"]["sources"][1]["cited"] is True
    assert data["cloud"]["metrics"]["prompt_tokens"] == 70 and data["cloud"]["metrics"]["output_tokens"] == 30
    assert set(data["cloud"]["metrics"]) == {"http_seconds", "total_seconds", "prompt_tokens", "output_tokens"}


@pytest.mark.parametrize("configured", [False, True])
def test_local_ask_and_state_never_call_cloud_and_archive_is_separate(tmp_path, configured):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"source": "Day24", "historical": True, "rows": [
        {"question": "Как в дне 7 сохраняется контекст?", "answer": "Архивный ответ", "status": "answered"}]}))
    calls = []
    async def run():
        async with client_for(make_retriever(tmp_path), cloud_transport=cloud_transport(calls=calls),
                              cloud_config=CloudConfig(api_key="fake-test-key" if configured else ""), baseline_path=path) as (client, _):
            state = (await client.get("/api/state")).json()
            assert state["comparison"]["available"] is configured
            answer = (await client.post("/api/ask", json={"question": "Как в дне 7 сохраняется контекст?"})).json()
            assert answer["mode"] == "local" and answer["cloud"] is None
            assert answer["archive"]["historical"] is True
    asyncio.run(run())
    assert calls == []


def test_unconfigured_comparison_before_retrieval(tmp_path):
    retriever = make_retriever(tmp_path)
    result = compare(retriever, config=CloudConfig())
    assert result.status_code == 503 and result.json()["error"]["code"] == "cloud_unconfigured"
    assert retriever.embedder is None


def test_empty_context_calls_neither_provider(tmp_path):
    calls, cloud_calls = [], []
    data = compare(make_retriever(tmp_path, scores=(.1,)), transport(calls=calls), cloud_transport(calls=cloud_calls)).json()
    assert data["status"] == data["cloud"]["status"] == "unknown"
    assert data["cloud"]["called"] is False and data["cloud"]["metrics"] is None
    assert data["comparison"]["chunks"] == 0
    assert data["metrics"]["total_seconds"] >= data["metrics"]["retrieval_seconds"] > 0
    assert calls == cloud_calls == []


@pytest.mark.parametrize("local_fails", [True, False])
def test_partial_errors_preserve_other_answer_no_retry_or_archive_fallback(tmp_path, local_fails):
    cloud_calls, local_calls = [], []
    local = transport(chat=(lambda _: httpx.Response(500, text="private provider body")) if local_fails else None, calls=local_calls)
    cloud = cloud_transport(chat=None if local_fails else (lambda _: httpx.Response(429, text="private provider body")), calls=cloud_calls)
    result = compare(make_retriever(tmp_path), local, cloud)
    assert result.status_code == 200
    data = result.json()
    assert data["status"] == ("error" if local_fails else "answered")
    assert data["cloud"]["status"] == ("answered" if local_fails else "error")
    assert len(cloud_calls) == 1 and len(local_calls) == 2
    assert "private provider body" not in result.text
    if not local_fails:
        assert data["cloud"]["metrics"] is None


@pytest.mark.parametrize("bad", [[], {}, cloud_response(choices=[]), cloud_response(choices=[{"message": {"content": " "}, "finish_reason": "stop"}]),
    cloud_response(usage={"prompt_tokens": True, "completion_tokens": 5}), cloud_response(usage={"prompt_tokens": 5, "completion_tokens": 0}),
    cloud_response(model=5), cloud_response(usage={"prompt_tokens": 10**400, "completion_tokens": 1}),
    cloud_response(choices=[{"message": {"content": "Ответ"}, "finish_reason": "content_filter"}])])
def test_cloud_malformed_response_keeps_local_success(tmp_path, bad):
    data = compare(make_retriever(tmp_path), cloud=cloud_transport(chat=lambda _: httpx.Response(200, json=bad))).json()
    assert data["status"] == "answered" and data["cloud"]["status"] == "error"
    assert data["cloud"]["error"]["code"] == "cloud_invalid_response"


def test_cloud_redirect_not_followed_and_invalid_json(tmp_path):
    calls = []
    data = compare(make_retriever(tmp_path), cloud=cloud_transport(chat=lambda _: httpx.Response(302, headers={"Location": "https://other.example"}), calls=calls)).json()
    assert data["cloud"]["status"] == "error" and len(calls) == 1
    directory = tmp_path / "again"
    directory.mkdir()
    data = compare(make_retriever(directory), cloud=cloud_transport(chat=lambda _: httpx.Response(200, text="{"))).json()
    assert data["cloud"]["error"]["code"] == "cloud_invalid_response"


def test_parallel_calls_cloud_timeout_is_partial_and_lock_shared(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "REQUEST_SECONDS", .2)
    async def run():
        local_started, cloud_started = asyncio.Event(), asyncio.Event()
        async def local(_):
            local_started.set()
            await asyncio.wait_for(cloud_started.wait(), .1)
            return httpx.Response(200, json=response())
        async def cloud(_):
            cloud_started.set()
            await local_started.wait()
            await asyncio.sleep(1)
            return httpx.Response(200, json=cloud_response())
        async with client_for(make_retriever(tmp_path), transport(chat=local), cloud_transport=cloud_transport(chat=cloud),
                              cloud_config=CloudConfig(api_key="fake-test-key")) as (client, app):
            task = asyncio.create_task(client.post("/api/compare", json={"question": "Вопрос"}))
            await asyncio.wait_for(cloud_started.wait(), 1)
            assert (await client.post("/api/ask", json={"question": "Второй"})).status_code == 409
            assert (await client.get("/api/state")).status_code == 200
            result = await task
            assert result.status_code == 200
            data = result.json()
            assert data["status"] == "answered" and data["cloud"]["status"] == "error"
            assert data["cloud"]["error"]["code"] == "cloud_timeout"
            assert not app.state.lock.locked()
    asyncio.run(run())


def test_cloud_transport_timeout_unknown_and_length(tmp_path):
    def fail(_):
        raise httpx.ReadTimeout("private transport details")
    result = compare(make_retriever(tmp_path), cloud=cloud_transport(chat=fail))
    assert result.json()["cloud"]["error"]["code"] == "cloud_timeout"
    assert "private transport details" not in result.text
    directory = tmp_path / "again"
    directory.mkdir()
    data = compare(make_retriever(directory), cloud=cloud_transport(chat=lambda _: httpx.Response(200, json=cloud_response(
        choices=[{"message": {"content": "Не знаю. Уточните [1]."}, "finish_reason": "length"}])))).json()
    assert data["cloud"]["status"] == "unknown" and data["cloud"]["answer"] == "Не знаю. Уточните [1]."
    assert data["cloud"]["truncated"] is True and data["cloud"]["reason"] == "missing_evidence"


def test_config_precedence_model_independent_and_key_not_repr(tmp_path, monkeypatch):
    here = tmp_path / "day-28"
    previous = tmp_path / "day-24"
    here.mkdir()
    previous.mkdir()
    (previous / ".env").write_text("DEEPSEEK_API_KEY=fake-old-key\nDEEPSEEK_MODEL=old-model\n")
    (here / ".env").write_text("DEEPSEEK_MODEL=current-model\n")
    monkeypatch.setattr(cloud_llm, "HERE", here)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    config = cloud_llm.load_config()
    assert config.api_key == "fake-old-key" and config.model == "current-model"
    assert "fake-old-key" not in repr(config)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-env-key")
    assert cloud_llm.load_config().api_key == "fake-env-key"
    (previous / ".env").unlink()
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert cloud_llm.load_config().model == "current-model" and not cloud_llm.load_config().api_key
