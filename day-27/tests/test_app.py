"""HTTP-level tests; the provider transport is fake and no model is loaded."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from backend import app as module


def answer(**overrides):
    result = {
        "done": True, "message": {"role": "assistant", "content": "Ответ"},
        "total_duration": 3_000_000_000, "load_duration": 1_000_000_000,
        "eval_duration": 2_000_000_000, "prompt_eval_count": 20, "eval_count": 60,
        "done_reason": "stop",
    }
    return result | overrides


def provider(chat=None, tags=None, calls=None):
    async def handler(request):
        if calls is not None:
            calls.append(request)
        assert str(request.url).startswith(module.OLLAMA_URL + "/api/")
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.test"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=tags if tags is not None else {"models": [{"name": module.MODEL}]})
        if chat is not None:
            result = chat(request)
            return await result if hasattr(result, "__await__") else result
        return httpx.Response(200, json=answer())
    return httpx.MockTransport(handler)


@asynccontextmanager
async def client_for(transport, **kwargs):
    app = module.create_app(transport, **kwargs)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, app


def post(body, transport=None):
    async def run():
        async with client_for(transport or provider()) as (client, _):
            return await client.post("/api/chat", json=body)
    return asyncio.run(run())


def test_success_payload_and_metrics():
    calls = []
    history = [{"role": "user", "content": "Проект Север"}, {"role": "assistant", "content": "Запомнил"}]
    response = post({"prompt": "Как называется проект?", "history": history}, provider(calls=calls))
    assert response.status_code == 200
    assert response.json() == {
        "answer": "Ответ", "model": module.MODEL, "dropped_pairs": 0, "truncated": False,
        "metrics": {"total_seconds": 3.0, "load_seconds": 1.0, "generation_seconds": 2.0,
                    "prompt_tokens": 20, "output_tokens": 60, "tokens_per_second": 30.0},
    }
    assert [call.url.path for call in calls] == ["/api/tags", "/api/chat"]
    assert json.loads(calls[-1].content) == {
        "model": module.MODEL, "messages": history + [{"role": "user", "content": "Как называется проект?"}],
        "stream": False, "think": False, "keep_alive": "1m",
        "options": {"temperature": 0.7, "num_predict": 1024, "num_ctx": 4096},
    }


def test_drop_whole_pairs_for_utf8_budget():
    calls = []
    history = [{"role": "user", "content": "я" * 800}, {"role": "assistant", "content": "ответ"},
               {"role": "user", "content": "новое"}, {"role": "assistant", "content": "да"}]
    response = post({"prompt": "ю" * 750, "history": history}, provider(calls=calls))
    assert response.json()["dropped_pairs"] == 1
    sent = json.loads(calls[-1].content)["messages"]
    assert sent == history[2:] + [{"role": "user", "content": "ю" * 750}]
    assert sum(len(message["content"].encode()) for message in sent) <= 3000


@pytest.mark.parametrize("prompt", ["a" * 3001, "я" * 1501])
def test_oversized_prompt_rejected_without_provider(prompt):
    calls = []
    response = post({"prompt": prompt}, provider(calls=calls))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "prompt_too_long"
    assert calls == []


@pytest.mark.parametrize("body", [
    {}, {"prompt": " "}, {"prompt": 123}, {"prompt": "Привет", "model": "cloud"},
    {"prompt": "Привет", "history": [{"role": "user", "content": "вопрос"}]},
    {"prompt": "Привет", "history": [{"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}]},
    {"prompt": "Привет", "history": [{"role": "user", "content": "x"}, {"role": "assistant", "content": " "}]},
    {"prompt": "Привет", "history": [{"role": "system", "content": "x"}, {"role": "assistant", "content": "y"}]},
    {"prompt": "Привет", "history": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}] * 5},
])
def test_invalid_requests_do_not_contact_model(body):
    calls = []
    response = post(body, provider(calls=calls))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert calls == []


@pytest.mark.parametrize("raw", [
    b'{"prompt":"\\ud800"}',
    b'{"prompt":"hello","history":[{"role":"user","content":"\\udfff"},{"role":"assistant","content":"yes"}]}',
])
def test_lone_unicode_surrogates_are_rejected(raw):
    calls = []
    async def run():
        async with client_for(provider(calls=calls)) as (client, _):
            response = await client.post("/api/chat", content=raw, headers={"Content-Type": "application/json"})
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_request"
    asyncio.run(run())
    assert calls == []


@pytest.mark.parametrize("status", [404, 500, 302])
def test_provider_http_errors_and_redirects_have_no_retry(status):
    calls = []
    response = post({"prompt": "Привет"}, provider(chat=lambda _: httpx.Response(status, headers={"Location": "https://example.com"}, text="private provider body"), calls=calls))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_error"
    assert "private provider body" not in response.text
    assert len(calls) == 2


@pytest.mark.parametrize("data", [
    [], {"error": "private"}, answer(done=False), answer(message={"content": " "}),
    answer(message={"content": 5}), answer(eval_count=-1), answer(eval_count=True),
    answer(eval_duration=1.5), answer(total_duration=None), answer(eval_count=10**400),
])
def test_bad_response(data):
    response = post({"prompt": "Привет"}, provider(chat=lambda _: httpx.Response(200, json=data)))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_response"


def test_invalid_json():
    response = post({"prompt": "Привет"}, provider(chat=lambda _: httpx.Response(200, text="{bad json")))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_response"


@pytest.mark.parametrize("exception,status,code", [
    (httpx.ConnectError("private error"), 503, "unavailable"),
    (httpx.ReadTimeout("private error"), 504, "timeout"),
])
def test_transport_errors(exception, status, code):
    def fail(_):
        raise exception
    response = post({"prompt": "Привет"}, provider(chat=fail))
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "private error" not in response.text


def test_missing_model_has_no_generation():
    calls = []
    response = post({"prompt": "Привет"}, provider(tags={"models": []}, calls=calls))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "missing_model"
    assert len(calls) == 1


def test_zero_duration_and_truncation():
    response = post({"prompt": "Привет"}, provider(chat=lambda _: httpx.Response(200, json=answer(eval_duration=0, done_reason="length"))))
    assert response.status_code == 200
    assert response.json()["metrics"]["tokens_per_second"] == 0
    assert response.json()["truncated"] is True


def test_health_does_not_load_model():
    calls = []
    async def run():
        async with client_for(provider(calls=calls)) as (client, _):
            response = await client.get("/api/health")
            assert response.json()["status"] == "ready"
            assert response.json()["version"] == "0.test"
            assert response.json()["parameters"] == module.PARAMETERS
    asyncio.run(run())
    assert [call.url.path for call in calls] == ["/api/version", "/api/tags"]


def test_health_missing_model():
    async def run():
        async with client_for(provider(tags={"models": []})) as (client, _):
            result = (await client.get("/api/health")).json()
            assert result["status"] == "missing_model"
    asyncio.run(run())


def test_concurrent_request_is_busy_and_health_remains_available():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def wait(_):
            started.set()
            await release.wait()
            return httpx.Response(200, json=answer())
        async with client_for(provider(chat=wait)) as (client, app):
            first = asyncio.create_task(client.post("/api/chat", json={"prompt": "Первый"}))
            await asyncio.wait_for(started.wait(), 1)
            second = await client.post("/api/chat", json={"prompt": "Второй"})
            assert second.status_code == 409
            assert (await client.get("/api/health")).json()["status"] == "ready"
            release.set()
            assert (await first).status_code == 200
            assert not app.state.generation_lock.locked()
            assert (await client.post("/api/chat", json={"prompt": "Третий"})).status_code == 200
    asyncio.run(run())


def test_overall_timeout_releases_lock(monkeypatch):
    monkeypatch.setattr(module, "REQUEST_SECONDS", 0.01)
    async def wait(_):
        await asyncio.sleep(1)
        return httpx.Response(200, json=answer())
    async def run():
        async with client_for(provider(chat=wait)) as (client, app):
            response = await client.post("/api/chat", json={"prompt": "Привет"})
            assert response.status_code == 504
            assert not app.state.generation_lock.locked()
    asyncio.run(run())


def test_cancellation_releases_lock():
    async def run():
        started = asyncio.Event()
        async def wait(_):
            started.set()
            await asyncio.sleep(60)
        async with client_for(provider(chat=wait)) as (client, app):
            task = asyncio.create_task(client.post("/api/chat", json={"prompt": "Привет"}))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not app.state.generation_lock.locked()
    asyncio.run(run())


def test_error_releases_lock():
    attempts = 0
    def sometimes_fail(_):
        nonlocal attempts
        attempts += 1
        return httpx.Response(500) if attempts == 1 else httpx.Response(200, json=answer())
    async def run():
        async with client_for(provider(chat=sometimes_fail)) as (client, app):
            assert (await client.post("/api/chat", json={"prompt": "Привет"})).status_code == 502
            assert not app.state.generation_lock.locked()
            assert (await client.post("/api/chat", json={"prompt": "Привет"})).status_code == 200
    asyncio.run(run())


def test_production_static_files_do_not_mask_api(tmp_path):
    (tmp_path / "index.html").write_text("<html>Локальный чат</html>")
    async def run():
        async with client_for(provider(), dist=tmp_path) as (client, _):
            assert "Локальный чат" in (await client.get("/")).text
            response = await client.get("/api/unknown")
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"
            assert (await client.get("/api/health")).json()["status"] == "ready"
    asyncio.run(run())
