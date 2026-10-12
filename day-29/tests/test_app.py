import asyncio
import hashlib
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from backend import app as module
from backend.local_llm import MODEL, URL, NATIVE_STOPS
from backend.profiles import BASELINE, OPTIMIZED, OPTIMIZED_SYSTEM


def response(**overrides):
    return {"done": True, "message": {"content": "История хранится в SQLite [1]."}, "load_duration": 1_000_000,
            "eval_duration": 2_000_000_000, "prompt_eval_count": 50, "eval_count": 60, "done_reason": "stop", **overrides}



def transport(chat=None, calls=None, models=None, ps=None, warm=None):
    active_context = None
    async def handle(request):
        nonlocal active_context
        assert str(request.url).startswith(URL + "/api/")
        if calls is not None:
            calls.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": MODEL}] if models is None else models})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"details": {"quantization_level": "Q4_K_M"}})
        if request.url.path == "/api/ps":
            if ps:
                result = ps(request)
                return await result if hasattr(result, "__await__") else result
            return httpx.Response(200, json={"models": [{"name": MODEL, "context_length": active_context, "size": 1000, "size_vram": 900, "details": {"quantization_level": "Q4_K_M"}}]})
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content)
        active_context = payload["options"]["num_ctx"]
        if not payload["messages"]:
            if warm:
                result = warm(request)
                return await result if hasattr(result, "__await__") else result
            return httpx.Response(200, json={"done": True, "load_duration": 2_000_000_000})
        if chat:
            result = chat(request)
            return await result if hasattr(result, "__await__") else result
        return httpx.Response(200, json=response())
    return httpx.MockTransport(handle)



@asynccontextmanager
async def client_for(provider=None, **kwargs):
    app = module.create_app(transport=provider or transport(), **kwargs)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, app



def post(body=None, provider=None, route="compare"):
    async def run():
        async with client_for(provider) as (client, _):
            return await client.post("/api/" + route, json=body if body is not None else {"question": "Расскажи о локальной модели двумя предложениями."})
    return asyncio.run(run())



def chats(calls):
    return [json.loads(call.content) for call in calls if call.url.path == "/api/chat"]



def test_alternating_and_explicit_order(tmp_path):
    async def run():
        async with client_for() as (client, _):
            bodies = [{"question": "Вопрос"}, {"question": "Вопрос"}, {"question": "Вопрос", "first_profile": "optimized"}]
            data = [(await client.post("/api/compare", json=body)).json() for body in bodies]
            assert [row["order"] for row in data] == [["baseline", "optimized"], ["optimized", "baseline"], ["optimized", "baseline"]]
            assert data[0]["comparison"]["input_sha256"] == data[1]["comparison"]["input_sha256"]
    asyncio.run(run())



@pytest.mark.parametrize("settings", [{"temperature": 0, "num_predict": 512, "num_ctx": 3072},
                                      {"temperature": 1, "num_predict": 2048, "num_ctx": 4096}, {"num_predict": 768}])
def test_allowed_settings_boundaries(tmp_path, settings):
    assert post({"question": "Вопрос", "settings": settings}).status_code == 200



def test_invalid_unicode(tmp_path):
    async def run():
        async with client_for() as (client, _):
            result = await client.post("/api/ask", content=b'{"question":"\\ud800"}', headers={"Content-Type": "application/json"})
            assert result.status_code == 422
    asyncio.run(run())



@pytest.mark.parametrize("bad", [response(done=False), response(message={"content": " "}), response(eval_count=0), response(eval_count=True),
                                    response(eval_duration=-1), response(load_duration=10**400), {"error": "private"}, []])
def test_malformed_generations_partial_no_retry(tmp_path, bad):
    calls = []
    result = post(provider=transport(chat=lambda _: httpx.Response(200, json=bad), calls=calls))
    assert result.status_code == 200
    for identifier in result.json()["order"]:
        assert result.json()[identifier]["status"] == "error"
        assert result.json()[identifier]["error"]["code"] == "invalid_response"
    assert len(chats(calls)) == 4
    assert "private" not in result.text



@pytest.mark.parametrize("failure", [500, 302, "json", "connect", "timeout"])
def test_failure_of_first_keeps_second_and_never_retries(tmp_path, failure):
    calls = []
    def chat(request):
        if json.loads(request.content)["options"]["temperature"] == .7:
            if failure == "connect": raise httpx.ConnectError("private")
            if failure == "timeout": raise httpx.ReadTimeout("private")
            if failure == "json": return httpx.Response(200, text="{")
            return httpx.Response(failure, text="private", headers={"Location": "https://cloud.example"})
        return httpx.Response(200, json=response())
    result = post(provider=transport(chat=chat, calls=calls))
    assert result.status_code == 200
    data = result.json()
    assert data["baseline"]["status"] == "error" and data["optimized"]["status"] == "answered"
    assert "private" not in result.text and len(chats(calls)) == 4



def test_preload_error_skips_own_generation_keeps_other(tmp_path):
    calls = []
    def warm(request):
        return httpx.Response(500) if json.loads(request.content)["options"]["temperature"] == .7 else httpx.Response(200, json={"done": True})
    data = post(provider=transport(warm=warm, calls=calls)).json()
    assert data["baseline"]["status"] == "error" and data["baseline"]["called"] is False
    assert data["baseline"]["metrics"]["http_seconds"] is None
    assert data["optimized"]["status"] == "answered"
    assert data["optimized"]["metrics"]["preload_load_seconds"] is None
    assert len(chats(calls)) == 3



def test_provider_timeout_is_partial_and_slot_released(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "STAGE_SECONDS", .07)
    async def chat(request):
        if json.loads(request.content)["options"]["temperature"] == .7:
            await asyncio.sleep(.3)
        return httpx.Response(200, json=response())
    async def run():
        async with client_for(transport(chat=chat)) as (client, app):
            result = (await client.post("/api/compare", json={"question": "Вопрос"})).json()
            assert result["baseline"]["status"] == "error" and result["baseline"]["error"]["code"] == "timeout"
            assert result["optimized"]["status"] == "answered"
            assert not app.state.lock.locked()
    asyncio.run(run())



@pytest.mark.parametrize("bad_ps", [{}, {"models": []}, {"models": [{"name": "other", "context_length": 3072}]},
    {"models": [{"name": MODEL, "context_length": 999}]}, {"models": [{"name": MODEL, "context_length": True}]}])
def test_ps_missing_wrong_model_context_does_not_invalidate_answer(tmp_path, bad_ps):
    data = post(provider=transport(ps=lambda _: httpx.Response(200, json=bad_ps))).json()
    for identifier in data["order"]:
        assert data[identifier]["status"] == "answered"
        assert data[identifier]["resources"]["samples"] == 0
        assert data[identifier]["resources"]["peak_size_bytes"] is None
        assert data[identifier]["resources"]["context_length"] is None



def test_ps_periodic_observations_capture_actual_peak(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "PS_INTERVAL", .01)
    counter = 0
    def ps(_):
        nonlocal counter
        counter += 1
        return httpx.Response(200, json={"models": [{"name": MODEL, "context_length": OPTIMIZED["num_ctx"], "size": counter * 100, "size_vram": 0}]})
    async def chat(_):
        await asyncio.sleep(.055)
        return httpx.Response(200, json=response())
    data = post(provider=transport(chat=chat, ps=ps), route="ask").json()
    resource = data["optimized"]["resources"]
    assert resource["samples"] >= 3 and resource["peak_size_bytes"] == counter * 100
    assert resource["peak_size_vram_bytes"] == 0



def test_observer_exception_does_not_break_generation(tmp_path):
    def ps(_): raise httpx.ReadTimeout("secret")
    data = post(provider=transport(ps=ps), route="ask").json()
    assert data["optimized"]["status"] == "answered"
    assert data["optimized"]["resources"]["samples"] == 0



def test_insufficient_context_warning_after_actual_prompt_count(tmp_path):
    data = post({"question": "Вопрос", "settings": {"num_ctx": 3072}}, provider=transport(chat=lambda _: httpx.Response(200, json=response(prompt_eval_count=2600)))).json()
    assert data["baseline"]["context_warning"] is None
    assert data["optimized"]["context_warning"] is not None
    assert data["optimized"]["called"] is True



def test_busy_generation_and_lifespan_cancel_owned_tasks(tmp_path):
    async def run():
        started = asyncio.Event()
        async def chat(_):
            started.set()
            await asyncio.sleep(20)
            return httpx.Response(200, json=response())
        app = module.create_app(transport=transport(chat=chat))
        pending = None
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                pending = asyncio.create_task(client.post("/api/compare", json={"question": "Вопрос"}))
                await asyncio.wait_for(started.wait(), 2)
                assert (await client.post("/api/ask", json={"question": "Второй"})).status_code == 409
                assert (await client.get("/api/state")).status_code == 200
        await asyncio.gather(pending, return_exceptions=True)
        assert not app.state.lock.locked() and not app.state.tasks
    asyncio.run(run())



def test_missing_model_result_error_no_generation(tmp_path):
    calls = []
    data = post(provider=transport(models=[], calls=calls)).json()
    assert data["baseline"]["error"]["code"] == "missing_model"
    assert data["optimized"]["error"]["code"] == "missing_model"
    assert chats(calls) == []



def test_static_files_cannot_mask_unknown_api(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>Day29</html>")
    async def run():
        async with client_for(dist=dist) as (client, _):
            assert "Day29" in (await client.get("/")).text
            unknown = await client.get("/api/unknown")
            assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"
    asyncio.run(run())



def test_slow_ps_observation_never_delays_successful_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "STAGE_SECONDS", .05)
    async def ps(_):
        await asyncio.sleep(.3)
        return httpx.Response(500)
    data = post(provider=transport(ps=ps), route="ask").json()
    assert data["optimized"]["status"] == "answered"
    assert data["optimized"]["resources"]["samples"] == 0
    assert data["optimized"]["metrics"]["total_seconds"] < .04



def test_same_input_exact_baseline_options_and_sequential_preload():
    calls = []
    question = "Напиши код и объясни его."
    data = post({"question": question}, provider=transport(calls=calls)).json()
    assert data["order"] == ["baseline", "optimized"]
    assert data["comparison"] == {"same_input": True, "input_sha256": hashlib.sha256(question.encode()).hexdigest()}
    payloads = chats(calls)
    assert len(payloads) == 4
    assert payloads[0]["messages"] == [] and payloads[2]["messages"] == []
    assert payloads[1]["messages"] == [{"role": "user", "content": question}]
    assert payloads[3]["messages"] == [{"role": "user", "content": question}]
    assert payloads[0]["options"] == payloads[1]["options"] == {**BASELINE, "presence_penalty": 0, "stop": list(NATIVE_STOPS)}
    assert payloads[2]["options"] == payloads[3]["options"] == {**OPTIMIZED, "presence_penalty": 0, "stop": list(NATIVE_STOPS)}
    assert all(p["model"] == MODEL and p["think"] is False and p["stream"] is False and p["keep_alive"] == "1m" for p in payloads)
    assert "shared_metrics" not in data and "scope" not in data
    assert data["baseline"]["system"] == ""
    for identifier in data["order"]:
        row = data[identifier]
        assert row["called"] is True and row["status"] == "answered"
        assert row["metrics"]["tokens_per_second"] == 30
        assert row["metrics"]["preload_load_seconds"] == 2
        assert row["metrics"]["load_seconds"] == .001
        assert row["resources"]["samples"] >= 1
        assert "sources" not in row and "citation_warning" not in row


def test_no_rag_imports_or_index_access_even_when_missing(monkeypatch):
    real_open = Path.open
    def guarded(path, *args, **kwargs):
        if any(part in str(path) for part in ("index.sqlite", "fastembed", "embedding", "/day-21/")):
            pytest.fail("Direct requests must not access RAG files")
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded)
    for name in ("embedding", "index_store", "fastembed", "onnxruntime"):
        monkeypatch.setitem(sys.modules, name, None)
    async def run():
        async with client_for() as (client, _):
            state = (await client.get("/api/state")).json()
            assert state["status"] == "ready" and "index" not in state
            result = await client.post("/api/ask", json={"question": "Сколько будет 2 + 2?"})
            assert result.status_code == 200 and result.json()["optimized"]["status"] == "answered"
    asyncio.run(run())
    backend = Path(__file__).resolve().parents[1] / "backend"
    assert not (backend / "retrieval.py").exists() and not (backend / "logic.py").exists()
    assert "Retriever" not in (backend / "app.py").read_text()


def test_independent_requests_do_not_include_history():
    calls = []
    async def run():
        async with client_for(transport(calls=calls)) as (client, _):
            await client.post("/api/ask", json={"question": "Первый вопрос"})
            await client.post("/api/compare", json={"question": "Второй вопрос"})
    asyncio.run(run())
    generations = [p for p in chats(calls) if p["messages"]]
    assert [p["messages"][-1]["content"] for p in generations] == ["Первый вопрос", "Второй вопрос", "Второй вопрос"]
    assert all(len(p["messages"]) in {1, 2} for p in generations)


def test_ask_only_optimized_settings_do_not_change_baseline():
    calls = []
    settings = {"temperature": 0, "num_predict": 2048, "num_ctx": 4096}
    result = post({"question": "Вопрос", "settings": settings}, transport(calls=calls), route="ask").json()
    assert result["mode"] == "optimized" and result["order"] == ["optimized"] and "baseline" not in result
    assert result["optimized"]["parameters"] == {**OPTIMIZED, "presence_penalty": 0, "format": None, **settings, "keep_alive": "1m"}
    assert len(chats(calls)) == 2
    again = post({"question": "Вопрос", "settings": settings}).json()
    assert again["baseline"]["parameters"] == {**BASELINE, "presence_penalty": 0, "format": None, "keep_alive": "1m"}
    assert again["optimized"]["parameters"] == {**OPTIMIZED, "presence_penalty": 0, "format": None, **settings, "keep_alive": "1m"}


def test_independent_latency_excludes_first_and_preload_from_http():
    async def slow_warm(_):
        await asyncio.sleep(.04)
        return httpx.Response(200, json={"done": True, "load_duration": 3_000_000})
    async def chat(request):
        if json.loads(request.content)["options"]["temperature"] == .7:
            await asyncio.sleep(.09)
        return httpx.Response(200, json=response())
    data = post(provider=transport(chat=chat, warm=slow_warm)).json()
    baseline, optimized = data["baseline"]["metrics"], data["optimized"]["metrics"]
    assert baseline["http_seconds"] > .08 and optimized["http_seconds"] < .03
    assert optimized["preload_seconds"] > .035
    assert optimized["preload_seconds"] <= optimized["total_seconds"] < .08


@pytest.mark.parametrize("settings", [{"temperature": -1}, {"temperature": 2.01}, {"temperature": True}, {"temperature": "0.2"},
    {"temperature": float("nan")}, {"temperature": float("inf")}, {"num_predict": 100}, {"num_predict": "768"},
    {"num_predict": 768.0}, {"num_predict": True}, {"num_ctx": 2048}, {"num_ctx": "3072"}, {"num_ctx": 3072.0}, {"unknown_setting": 20}])
def test_strict_invalid_settings_before_model(settings):
    calls = []
    async def run():
        async with client_for(transport(calls=calls)) as (client, _):
            result = await client.post("/api/compare", content=json.dumps({"question": "Вопрос", "settings": settings}).encode(), headers={"Content-Type": "application/json"})
            assert result.status_code == 422
    asyncio.run(run())
    assert calls == []


@pytest.mark.parametrize("body,status", [({}, 422), ({"question": " "}, 422), ({"question": 5}, 422),
    ({"question": "Вопрос", "history": []}, 422), ({"question": "Вопрос", "task_profile": "logic"}, 422), ({"question": "Вопрос", "first_profile": "cloud"}, 422),
    ({"question": "Вопрос", "settings": None}, 422), ({"question": "a" * 4001}, 413), ({"question": "€" * 3334}, 413)])
def test_bad_questions_before_model(body, status):
    calls = []
    assert post(body, provider=transport(calls=calls)).status_code == status
    assert calls == []


@pytest.mark.parametrize("question", ["a" * 4000, "я" * 4000, "€" * 3333])
def test_long_question_allowed_without_embedding_limit(question):
    calls = []
    data = post({"question": question}, provider=transport(calls=calls)).json()
    assert data["optimized"]["status"] == "answered"
    assert data["comparison"]["input_sha256"] == hashlib.sha256(question.encode()).hexdigest()
    assert chats(calls)[1]["messages"][0]["content"] == question


def test_state_profiles_quantization_no_presets_or_index():
    calls = []
    async def run():
        async with client_for(transport(calls=calls)) as (client, _):
            result = (await client.get("/api/state")).json()
            assert result["status"] == "ready" and result["quantization"] == "Q4_K_M"
            assert "index" not in result and "cloud" not in result
            assert "questions" not in result
            assert "task_profiles" not in result and "default_task_profile" not in result
            assert result["profiles"]["baseline"]["system"] == ""
            assert result["profiles"]["optimized"]["parameters"] == {**OPTIMIZED, "presence_penalty": 0, "format": None, "keep_alive": "1m"}
            assert result["tuning_limits"]["num_predict"] == [256, 512, 768, 1024, 1536, 2048]
    asyncio.run(run())
    assert [call.url.path for call in calls] == ["/api/tags", "/api/show"]


def test_state_missing_model_is_unavailable():
    async def run():
        async with client_for(transport(models=[])) as (client, _):
            state = (await client.get("/api/state")).json()
            assert state["status"] == "unavailable" and state["quantization"] is None
    asyncio.run(run())


def test_ps_failure_and_null_measurements():
    data = post(provider=transport(ps=lambda _: httpx.Response(500))).json()
    assert data["baseline"]["status"] == data["optimized"]["status"] == "answered"
    def ps(_):
        return httpx.Response(200, json={"models": [{"name": MODEL, "context_length": 4096, "size": None, "size_vram": None}]})
    data = post(provider=transport(ps=ps)).json()
    assert data["baseline"]["resources"]["samples"] >= 1
    assert data["baseline"]["resources"]["peak_size_bytes"] is None
    assert data["baseline"]["resources"]["peak_size_vram_bytes"] is None


def test_nonempty_dont_know_answer_not_rag_unknown_and_truncation_retained():
    data = post(provider=transport(chat=lambda _: httpx.Response(200, json=response(message={"content": "Не знаю исходной цены."}, done_reason="length")))).json()
    for identifier in data["order"]:
        assert data[identifier]["status"] == "answered" and data[identifier]["truncated"] is True
        assert data[identifier]["answer"] == "Не знаю исходной цены."
        assert data[identifier]["called"] is True and data[identifier]["reason"] == ""


def test_browser_cancellation_keeps_owned_slot_until_generation_finishes():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def chat(_):
            started.set()
            await release.wait()
            return httpx.Response(200, json=response())
        async with client_for(transport(chat=chat)) as (client, app):
            pending = asyncio.create_task(client.post("/api/ask", json={"question": "Первый"}))
            await asyncio.wait_for(started.wait(), 2)
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            assert app.state.lock.locked()
            assert (await client.post("/api/compare", json={"question": "Второй"})).status_code == 409
            release.set()
            for _ in range(100):
                if not app.state.lock.locked(): break
                await asyncio.sleep(.005)
            assert not app.state.lock.locked()
    asyncio.run(run())
