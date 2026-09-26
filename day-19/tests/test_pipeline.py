import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import sqlite3
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from backend.config import Config
from backend.errors import PipelineError
from backend.main import create_app
from backend.mcp_service import create_server
from backend.providers import DeepSeek, Tavily
from backend.runner import Runner, subprocess_session
from backend.service import PipelineService
from backend.store import Store, digest
from tests.fakes import FakeSearch, FakeSummary, ScriptModel, memory_factory, fake_runner
from mcp.shared.memory import create_connected_server_and_client_session

@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path)
    run, _ = store.admit("Изучи MCP", str(uuid4()))
    service = PipelineService(store, run["run_id"], FakeSearch(), FakeSummary())
    return store, run["run_id"], service

def execute(awaitable):
    return asyncio.run(awaitable)

async def all_stages(service):
    search = await service.search("MCP")
    summary = await service.summarize(search.search_result_id)
    file = await service.save(summary.summary_id)
    return search, summary, file


def test_exact_handoff_and_duplicates(setup):
    store, run_id, service = setup
    search, summary, file = execute(all_stages(service))
    assert execute(service.search("MCP")) == search
    assert execute(service.summarize(search.search_result_id)) == summary
    assert execute(service.save(summary.summary_id)) == file
    assert summary.input_sha256 == search.sha256
    assert file.sha256 == summary.sha256
    assert store.file_bytes(run_id) == summary.markdown.encode("utf-8")
    assert digest(store.file_bytes(run_id)) == file.sha256
    assert store.complete(run_id)["status"] == "completed"

@pytest.mark.parametrize("method", ["summarize", "save"])
def test_unknown_ids(setup, method):
    with pytest.raises(PipelineError, match="Артефакт"):
        execute(getattr(setup[2], method)(str(uuid4())))

@pytest.mark.parametrize("identifier", ["../../file", "", "not-a-uuid"])
def test_invalid_uuid(setup, identifier):
    with pytest.raises(PipelineError) as error:
        execute(setup[2].summarize(identifier))
    assert error.value.code == "invalid_arguments"


def test_foreign_run_ids(setup):
    store, run_id, service = setup
    search = execute(service.search("MCP"))
    store.fail(run_id, "internal")
    run, _ = store.admit("Другой запуск", str(uuid4()))
    other = PipelineService(store, run["run_id"], FakeSearch(), FakeSummary())
    with pytest.raises(PipelineError) as error:
        execute(other.summarize(search.search_result_id))
    assert error.value.code == "artifact_missing"


def test_conflicting_successful_args(setup):
    execute(setup[2].search("MCP"))
    with pytest.raises(PipelineError) as error:
        execute(setup[2].search("changed"))
    assert error.value.code == "stage_order"


def test_wrong_order(setup):
    store, run_id, service = setup
    with pytest.raises(PipelineError) as error:
        store.check_stage(run_id, "saveToFile")
    assert error.value.code == "stage_order"


def test_artifact_database_tamper(setup):
    store, run_id, service = setup
    search = execute(service.search("MCP"))
    with store.db() as db:
        db.execute("UPDATE artifacts SET payload=? WHERE id=?", ('{}', search.search_result_id))
    with pytest.raises(PipelineError) as error:
        execute(service.summarize(search.search_result_id))
    assert error.value.code == "artifact_integrity"


def test_file_tamper_download(setup):
    store, run_id, service = setup
    execute(all_stages(service))
    (store.data_dir / "files" / f"{run_id}.md").write_text("tampered")
    with pytest.raises(PipelineError) as error:
        store.complete(run_id)
    assert error.value.code == "artifact_integrity"


def test_admission_idempotency_conflict_busy(setup):
    store, run_id, _ = setup
    previous = store.get(run_id)
    run, replayed = store.admit(previous["prompt"], previous["request_id"], False)
    assert replayed and run["run_id"] == run_id
    with pytest.raises(PipelineError) as error:
        store.admit("different", previous["request_id"])
    assert error.value.code == "request_conflict"
    with pytest.raises(PipelineError) as error:
        store.admit("new", str(uuid4()))
    assert error.value.code == "busy"


def test_startup_interruption(setup):
    store, run_id, _ = setup
    store.recover()
    assert store.get(run_id)["status"] == "interrupted"
    assert store.get(run_id)["steps"][0]["status"] == "failed"


def test_startup_saved_receipt_reconciliation(setup):
    store, run_id, service = setup
    execute(all_stages(service))
    store.recover()
    assert store.get(run_id)["status"] == "completed"


def test_late_provider_result_rejected(setup):
    store, run_id, service = setup
    class CancelDuringSearch:
        async def search(self, query):
            store.fail(run_id, "interrupted", interrupted=True)
            return await FakeSearch().search(query)
    service.search_adapter = CancelDuringSearch()
    with pytest.raises(PipelineError) as error:
        execute(service.search("MCP"))
    assert error.value.code == "stage_order"
    assert store.get(run_id)["search_result"] is None


def test_atomic_write_preserves_existing(setup):
    store, run_id, _ = setup
    store.atomic_write(run_id, b"original")
    store.atomic_write(run_id, b"original")
    with pytest.raises(PipelineError) as error:
        store.atomic_write(run_id, b"different")
    assert error.value.code == "write_failed"
    assert (store.data_dir / "files" / f"{run_id}.md").read_bytes() == b"original"

@pytest.mark.parametrize("code", ["empty_search", "search_provider", "timeout", "payload_limit"])
def test_search_failures_retained(setup, code):
    store, run_id, service = setup
    class BadSearch:
        async def search(self, query):
            raise PipelineError(code)
    runner = Runner(Config(store.data_dir), store, ScriptModel(), memory_factory(store, search=BadSearch()))
    execute(runner.run(run_id))
    run = store.get(run_id)
    assert run["status"] == "failed" and run["error_code"] == code
    assert run["steps"][0]["status"] == "failed"
    assert "secret" not in json.dumps(run)

@pytest.mark.parametrize("code", ["empty_summary", "model_provider", "payload_limit"])
def test_summary_failure_preserves_search(setup, code):
    store, run_id, _ = setup
    class BadSummary:
        async def summarize(self, snapshot):
            raise PipelineError(code)
    execute(Runner(Config(store.data_dir), store, ScriptModel(), memory_factory(store, summary=BadSummary())).run(run_id))
    run = store.get(run_id)
    assert run["error_code"] == code and run["search_result"]
    assert run["steps"][1]["status"] == "failed"


def test_write_error_preserves_summary(setup, monkeypatch):
    store, run_id, _ = setup
    def bad_write(*args):
        raise PipelineError("write_failed")
    monkeypatch.setattr(store, "atomic_write", bad_write)
    execute(Runner(Config(store.data_dir), store, ScriptModel(), memory_factory(store)).run(run_id))
    run = store.get(run_id)
    assert run["summary_result"] and run["error_code"] == "write_failed"
    assert run["steps"][2]["status"] == "failed"

@pytest.mark.parametrize("mode,expected,count", [("success", "completed", 3), ("correct", "completed", 4), ("early", "failed", 1), ("bad_twice", "failed", 2)])
def test_real_mcp_orchestration(setup, mode, expected, count):
    store, run_id, _ = setup
    model = ScriptModel(mode)
    execute(Runner(Config(store.data_dir), store, model, memory_factory(store)).run(run_id))
    run = store.get(run_id)
    assert run["status"] == expected
    assert model.calls == count
    if expected == "completed":
        assert [event["tool"] for event in run["trace"] if event["status"] == "completed"] == ["search", "summarize", "saveToFile"]
        assert store.file_bytes(run_id) == run["summary_result"]["markdown"].encode()


def test_in_memory_tool_schemas_and_extra_args(setup):
    async def check():
        async with create_connected_server_and_client_session(create_server(setup[2])) as session:
            tools = (await session.list_tools()).tools
            assert len(tools) == 3
            assert {tool.name for tool in tools} == {"search", "summarize", "saveToFile"}
            assert all(tool.outputSchema for tool in tools)
            schema = next(tool.inputSchema for tool in tools if tool.name == "search")
            assert schema["properties"]["query"]["maxLength"] == 400
            result = await session.call_tool("search", {"query": "MCP"})
            assert result.structuredContent["search_result_id"]
            result = await session.call_tool("saveToFile", {"summary_id": str(uuid4()), "content": "injected"})
            assert result.isError
    execute(check())


def test_real_stdio_discovery(tmp_path):
    config = Config(tmp_path)
    async def check():
        async with subprocess_session(config, str(uuid4())) as session:
            tools = (await session.list_tools()).tools
            assert {tool.name for tool in tools} == {"search", "summarize", "saveToFile"}
            assert all(tool.outputSchema for tool in tools)
    execute(check())


def test_api_full_chain_download_and_replay(tmp_path):
    app = create_app(Config(tmp_path, "test-key", "test-key"), fake_runner)
    request = {"prompt": "Изучи MCP", "request_id": str(uuid4())}
    with TestClient(app) as client:
        response = client.post("/api/runs", json=request)
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        # TestClient owns a separate event loop; a bounded async wait advances it.
        for _ in range(100):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] != "running":
                break
            import time
            time.sleep(.01)
        assert run["status"] == "completed", run
        data = client.get(f"/api/runs/{run_id}/download")
        assert data.status_code == 200
        assert data.content == run["summary_result"]["markdown"].encode()
        assert hashlib.sha256(data.content).hexdigest() == run["file_result"]["sha256"]
        assert client.post("/api/runs", json=request).json()["replayed"]
        conflict = client.post("/api/runs", json={**request, "prompt": "different"})
        assert conflict.status_code == 409
        state = client.get("/api/state").json()
        assert not state["busy"] and state["runs"][0]["run_id"] == run_id


def test_api_missing_config_and_unknown_download(tmp_path):
    with TestClient(create_app(Config(tmp_path))) as client:
        state = client.get("/api/state").json()
        assert not state["configuration"]["tavily"]
        response = client.post("/api/runs", json={"prompt": "MCP", "request_id": str(uuid4())})
        assert response.status_code == 503
        assert response.json()["detail"]["error_code"] == "configuration"
        assert client.get(f"/api/runs/{uuid4()}/download").status_code == 404
        assert client.get("/api/state").json()["runs"] == []


def test_api_busy_and_shutdown_cancel(tmp_path):
    class Waiting:
        def __init__(self, config, store): self.store = store
        async def run(self, run_id):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.store.fail(run_id, "interrupted", interrupted=True)
                raise
    app = create_app(Config(tmp_path, "test", "test"), Waiting)
    with TestClient(app) as client:
        response = client.post("/api/runs", json={"prompt": "MCP", "request_id": str(uuid4())})
        run_id = response.json()["run_id"]
        assert client.post("/api/runs", json={"prompt": "new", "request_id": str(uuid4())}).status_code == 409
        assert client.get(f"/api/runs/{run_id}/download").status_code == 409
    assert app.state.store.get(run_id)["status"] == "interrupted"


def test_provider_summary_has_no_tools(monkeypatch):
    import backend.providers as providers
    calls = []
    async def fake_post(url, body, headers, timeout, error_code):
        calls.append(body)
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Сводка"}}]}
    monkeypatch.setattr(providers, "post_json", fake_post)
    assert execute(DeepSeek("test", "deepseek-flash").summarize({"sources": []})) == "Сводка"
    assert "tools" not in calls[0]
    assert calls[0]["thinking"] == {"type": "disabled"}


def test_tavily_request_contract_and_empty(monkeypatch):
    import backend.providers as providers
    async def fake_post(url, body, headers, timeout, error_code):
        assert body["max_results"] == 3 and not body["include_answer"] and not body["include_raw_content"]
        assert timeout == 15
        return {"results": []}
    monkeypatch.setattr(providers, "post_json", fake_post)
    with pytest.raises(PipelineError) as error:
        execute(Tavily("test").search("MCP"))
    assert error.value.code == "empty_search"


def test_run_deadline(setup, monkeypatch):
    import backend.runner as module
    store, run_id, _ = setup
    class SlowModel:
        async def completion(self, *args, **kwargs):
            await asyncio.sleep(1)
    monkeypatch.setattr(module, "RUN_TIMEOUT", .02)
    execute(Runner(Config(store.data_dir), store, SlowModel(), memory_factory(store)).run(run_id))
    assert store.get(run_id)["error_code"] == "timeout"


def test_success_survives_mcp_cleanup_error(setup):
    store, run_id, _ = setup
    factory = memory_factory(store)
    @asynccontextmanager
    async def broken_cleanup(config, identifier):
        async with factory(config, identifier) as session:
            yield session
        raise RuntimeError("raw secret provider dump")
    execute(Runner(Config(store.data_dir), store, ScriptModel(), broken_cleanup).run(run_id))
    run = store.get(run_id)
    assert run["status"] == "completed" and run["error"] is None
    assert "raw secret" not in json.dumps(run)


def test_actual_mcp_save_rejects_content_path(setup):
    store, run_id, service = setup
    search = execute(service.search("MCP"))
    summary = execute(service.summarize(search.search_result_id))
    async def check():
        async with create_connected_server_and_client_session(create_server(service)) as session:
            for extra in ({"content": "forged"}, {"path": "../evil"}, {"filename": "evil.md"}):
                result = await session.call_tool("saveToFile", {"summary_id": summary.summary_id, **extra})
                assert result.isError
            schema = next(tool.inputSchema for tool in (await session.list_tools()).tools if tool.name == "saveToFile")
            assert not schema["additionalProperties"]
            assert schema["properties"]["summary_id"]["format"] == "uuid"
    execute(check())
    assert store.get(run_id)["file_result"] is None
    assert not (store.data_dir / "files").exists()


def test_six_decision_limit(setup):
    store, run_id, _ = setup
    class Repeating(ScriptModel):
        async def completion(self, messages, tools=None):
            return await super().completion(messages[:2], tools)
    model = Repeating()
    execute(Runner(Config(store.data_dir), store, model, memory_factory(store)).run(run_id))
    assert model.calls == 6
    assert store.get(run_id)["error_code"] == "decision_limit"

@pytest.mark.parametrize("bad_calls", [[], [{"id": "a", "type": "function", "function": {"name": "search", "arguments": "{}"}}, {"id": "b", "type": "function", "function": {"name": "search", "arguments": "{}"}}]])
def test_early_or_parallel_calls_rejected(setup, bad_calls):
    store, run_id, _ = setup
    class BadModel:
        async def completion(self, *args, **kwargs):
            return {"role": "assistant", "tool_calls": bad_calls}
    execute(Runner(Config(store.data_dir), store, BadModel(), memory_factory(store)).run(run_id))
    run = store.get(run_id)
    assert run["status"] == "failed" and run["file_result"] is None
    assert run["error_code"] == ("early_final" if not bad_calls else "tool_protocol")


def test_summary_length_limit(setup):
    store, run_id, service = setup
    class LongSummary:
        async def summarize(self, snapshot): return "я" * 10_001
    service.summary_adapter = LongSummary()
    search = execute(service.search("MCP"))
    with pytest.raises(PipelineError) as error:
        execute(service.summarize(search.search_result_id))
    assert error.value.code == "payload_limit"
    assert store.get(run_id)["summary_result"] is None


def test_atomic_admission_concurrency(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = Store(tmp_path)
    request_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.admit("MCP", request_id), range(4)))
    assert len({run["run_id"] for run, replayed in results}) == 1
    assert sum(not replayed for run, replayed in results) == 1


def mock_http(monkeypatch, handler):
    import httpx
    import backend.providers as providers
    constructor = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: constructor(transport=httpx.MockTransport(handler), **kwargs))
    return providers


def test_http_size_limit(monkeypatch):
    import httpx
    providers = mock_http(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 200_001))
    with pytest.raises(PipelineError) as error:
        execute(providers.post_json("https://test.invalid", {}, {}, 1, "search_provider"))
    assert error.value.code == "payload_limit"


def test_http_overall_deadline(monkeypatch):
    import httpx
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})
    providers = mock_http(monkeypatch, slow)
    with pytest.raises(PipelineError) as error:
        execute(providers.post_json("https://test.invalid", {}, {}, .01, "search_provider"))
    assert error.value.code == "timeout"

@pytest.mark.parametrize("status,body", [(401, b"raw key secret"), (500, b"raw upstream dump"), (200, b"not-json secret"), (200, b"[]")])
def test_http_errors_sanitized(monkeypatch, status, body):
    import httpx
    providers = mock_http(monkeypatch, lambda request: httpx.Response(status, content=body))
    with pytest.raises(PipelineError) as error:
        execute(providers.post_json("https://test.invalid", {}, {}, 1, "search_provider"))
    assert error.value.code == "search_provider"
    assert "secret" not in str(error.value) and "upstream" not in str(error.value)


def test_file_symlink_rejected(setup, tmp_path):
    store, run_id, _ = setup
    target = tmp_path / "elsewhere"
    target.write_bytes(b"unchanged")
    folder = store.data_dir / "files"
    folder.mkdir()
    (folder / f"{run_id}.md").symlink_to(target)
    with pytest.raises(PipelineError) as error:
        store.atomic_write(run_id, b"override")
    assert error.value.code == "write_failed"
    assert target.read_bytes() == b"unchanged"
