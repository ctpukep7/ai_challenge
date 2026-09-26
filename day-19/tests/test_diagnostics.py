import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
from uuid import uuid4
import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from backend.config import Config, ROOT
from backend.diagnostics import Diagnostics, MAX_EXCHANGES, MAX_RUN_BYTES, MAX_VALUE_BYTES
from backend.errors import PipelineError
from backend.mcp_service import create_server
from backend.providers import DeepSeek, Tavily
from backend.runner import Runner
from backend.service import PipelineService
from backend.store import Store, canonical
from tests.fakes import ScriptModel, memory_factory


def run(awaitable):
    return asyncio.run(awaitable)


def setup(tmp_path, prompt="MCP"):
    store = Store(tmp_path)
    record, _ = store.admit(prompt, str(uuid4()))
    return store, record["run_id"]


def http_transport(monkeypatch, handler):
    import backend.providers as providers
    original = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def real_adapter_session(store):
    @asynccontextmanager
    async def session(config, run_id):
        # Mimic separate API / subprocess Store objects sharing SQLite.
        child_store = Store(store.data_dir)
        diagnostics = Diagnostics(child_store, run_id, (config.deepseek_key, config.tavily_key), (ROOT, config.data_dir))
        service = PipelineService(child_store, run_id,
                                  Tavily(config.tavily_key, diagnostics),
                                  DeepSeek(config.deepseek_key, config.model, diagnostics))
        async with create_connected_server_and_client_session(create_server(service)) as client:
            yield client
    return session


def success_handler(captured, secret=""):
    def handler(request):
        body = json.loads(request.content)
        captured.append((str(request.url), body, dict(request.headers)))
        if request.url.host == "api.tavily.com":
            return httpx.Response(200, json={"results": [{"title": "Official MCP", "url": "https://modelcontextprotocol.io/", "content": f"Typed MCP tools. {secret}"}], "api_key": secret, "raw_content": "excluded provider metadata"})
        if "tools" not in body:
            message = {"role": "assistant", "content": f"Краткая сводка. {secret}", "reasoning_content": "hidden reasoning", "provider_extra": "excluded metadata"}
        else:
            previous = json.loads(body["messages"][-1]["content"]) if body["messages"][-1]["role"] == "tool" else {}
            if "summary_id" in previous:
                name, args = "saveToFile", {"summary_id": previous["summary_id"]}
            elif "search_result_id" in previous:
                name, args = "summarize", {"search_result_id": previous["search_result_id"]}
            else:
                name, args = "search", {"query": "MCP"}
            message = {"role": "assistant", "content": None, "reasoning_content": "hidden reasoning",
                       "tool_calls": [{"id": f"actual-{len(captured)}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}, "provider_extra": "excluded metadata"}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls" if message.get("tool_calls") else "stop", "message": message}], "usage": {"provider_extra": "excluded metadata"}})
    return handler


def test_real_http_and_mcp_exchange_records_match_outgoing_body(tmp_path, monkeypatch):
    config = Config(tmp_path, "deepseek-secret-value", "tavily-secret-value")
    sensitive = f"{config.deepseek_key} {config.tavily_key} {ROOT}"
    store, run_id = setup(tmp_path, f"Изучи MCP {sensitive}")
    captured = []
    http_transport(monkeypatch, success_handler(captured, sensitive))
    run(Runner(config, store, session_factory=real_adapter_session(store)).run(run_id))
    record = store.get(run_id)
    assert record["status"] == "completed"
    assert store.file_bytes(run_id) == record["summary_result"]["markdown"].encode()
    exchanges = record["exchanges"]
    assert len(exchanges) == 9  # discovery + 3 agent + Tavily + summary + 3 tools
    assert all(exchange["status"] == "completed" and exchange["completed_at"] and exchange["duration_ms"] >= 0 for exchange in exchanges)
    assert [entry["stage"] for entry in exchanges if entry["kind"] == "deepseek_agent"] == ["search", "summarize", "saveToFile"]
    diagnostics = Diagnostics(store, run_id, (config.deepseek_key, config.tavily_key), (ROOT, config.data_dir))
    http_exchanges = [entry for entry in exchanges if entry["kind"] != "mcp"]
    assert len(http_exchanges) == len(captured) == 5
    for recorded, (endpoint, actual_body, headers) in zip(http_exchanges, captured):
        assert recorded["request"] == {"method": "POST", "endpoint": endpoint, "body": diagnostics.sanitize(actual_body)}
        if endpoint == "https://api.deepseek.com/chat/completions":
            assert headers["authorization"] == f"Bearer {config.deepseek_key}"
            assert recorded["response"].keys() == {"role", "content", "tool_calls"}
            assert actual_body["thinking"] == {"type": "disabled"}
        else:
            assert actual_body["api_key"] == config.tavily_key
            assert "api_key" not in recorded["request"]["body"]
            assert recorded["response"]["sources"] == diagnostics.sanitize(record["search_result"]["sources"])
    agent = [entry for entry in http_exchanges if entry["kind"] == "deepseek_agent"]
    summary = next(entry for entry in http_exchanges if entry["kind"] == "deepseek_summary")
    assert all("tools" in entry["request"]["body"] for entry in agent)
    assert "tools" not in summary["request"]["body"]
    actual_mcp = [entry for entry in exchanges if entry["kind"] == "mcp" and entry["stage"] != "setup"]
    assert [entry["request"]["params"]["name"] for entry in actual_mcp] == ["search", "summarize", "saveToFile"]
    for entry, result_key in zip(actual_mcp, ("search_result", "summary_result", "file_result")):
        assert entry["request"]["method"] == "tools/call"
        assert set(entry["request"]) == {"method", "params"}
        assert entry["response"] == {"isError": False, "structuredContent": diagnostics.sanitize(record[result_key])}
    serialized = json.dumps(exchanges, ensure_ascii=False)
    assert all(value not in serialized for value in (config.deepseek_key, config.tavily_key, str(ROOT), "hidden reasoning", "excluded metadata", "provider_extra"))
    assert "[REDACTED]" in serialized


@pytest.mark.parametrize("status,content,expected", [(500, b"raw provider dump deepseek-secret", "search_provider"), (200, b'{"results":[]}', "empty_search"), (200, b"invalid JSON secret", "search_provider")])
def test_http_error_pairs_are_safe(tmp_path, monkeypatch, status, content, expected):
    store, run_id = setup(tmp_path)
    diagnostics = Diagnostics(store, run_id, ("deepseek-secret", "tavily-secret"))
    http_transport(monkeypatch, lambda request: httpx.Response(status, content=content))
    with pytest.raises(PipelineError):
        run(Tavily("tavily-secret", diagnostics).search("MCP"))
    exchange = store.get(run_id)["exchanges"][0]
    assert exchange["status"] == "failed" and exchange["error_code"] == expected
    assert exchange["response"] is None and exchange["completed_at"]
    assert "secret" not in json.dumps(exchange)
    assert "raw provider" not in json.dumps(exchange)


def test_mcp_failure_pairs_fixed_error_only(tmp_path):
    store, run_id = setup(tmp_path)
    class FailedSearch:
        async def search(self, query): raise PipelineError("search_provider")
    run(Runner(Config(tmp_path), store, ScriptModel(), memory_factory(store, search=FailedSearch())).run(run_id))
    exchange = next(entry for entry in store.get(run_id)["exchanges"] if entry["stage"] == "search")
    assert exchange["request"] == {"method": "tools/call", "params": {"name": "search", "arguments": {"query": "MCP"}}}
    assert exchange["status"] == "failed"
    assert exchange["response"] == {"isError": True, "error_code": "search_provider"}


def test_fake_models_do_not_create_http_history(tmp_path):
    store, run_id = setup(tmp_path)
    run(Runner(Config(tmp_path), store, ScriptModel("correct"), memory_factory(store)).run(run_id))
    exchanges = store.get(run_id)["exchanges"]
    assert len(exchanges) == 4
    assert all(entry["kind"] == "mcp" for entry in exchanges)
    # The rejected proposed call with a path never reached ClientSession.call_tool.
    assert all("path" not in entry["request"].get("params", {}).get("arguments", {}) for entry in exchanges)


def test_diagnostic_write_failures_do_not_change_success(tmp_path, monkeypatch):
    config = Config(tmp_path, "deepseek-secret", "tavily-secret")
    store, run_id = setup(tmp_path)
    captured = []
    http_transport(monkeypatch, success_handler(captured))
    def broken(*args, **kwargs): raise RuntimeError("journal secret failed")
    monkeypatch.setattr(Store, "begin_exchange", broken)
    monkeypatch.setattr(Store, "finish_exchange", broken)
    run(Runner(config, store, session_factory=real_adapter_session(store)).run(run_id))
    record = store.get(run_id)
    assert record["status"] == "completed" and len(captured) == 5
    assert store.file_bytes(run_id) == record["summary_result"]["markdown"].encode()
    assert record["exchanges"] == []


def test_legacy_runs_remain_without_fabricated_exchanges(tmp_path):
    store, run_id = setup(tmp_path)
    def remove(run): run.pop("exchanges")
    store.update(run_id, remove)
    store.recover()
    assert "exchanges" not in store.get(run_id)
    assert "exchanges" not in store.latest()[0]


def test_interruption_finalizes_unfinished_diagnostic(tmp_path):
    store, run_id = setup(tmp_path)
    diagnostics = Diagnostics(store, run_id)
    diagnostics.begin("search", "tavily", {"method": "POST", "body": {"query": "MCP"}})
    store.recover()
    record = store.get(run_id)
    exchange = record["exchanges"][0]
    assert record["status"] == "interrupted"
    assert exchange["status"] == "failed" and exchange["error_code"] == "interrupted"
    assert exchange["response"] is None and exchange["completed_at"]


def test_diagnostics_bound_values_and_run_budget(tmp_path):
    store, run_id = setup(tmp_path)
    diagnostics = Diagnostics(store, run_id)
    large = diagnostics.begin("search", "tavily", {"text": "я" * MAX_VALUE_BYTES})
    diagnostics.finish(large, {"text": "я" * MAX_VALUE_BYTES})
    entries = store.get(run_id)["exchanges"]
    assert entries[0]["request"]["_diagnostic_truncated"]
    assert entries[0]["response"]["_diagnostic_truncated"]
    for _ in range(MAX_EXCHANGES + 10):
        exchange = diagnostics.begin("search", "tavily", {"text": "x" * 120_000})
        diagnostics.finish(exchange, {"text": "x" * 120_000})
    entries = store.get(run_id)["exchanges"]
    assert len(entries) == MAX_EXCHANGES
    assert len(canonical(entries).encode()) < MAX_RUN_BYTES
    assert all(entry["status"] == "completed" for entry in entries)
    assert any(entry["request"].get("reason") == "run_budget" for entry in entries)


def test_shared_sqlite_diagnostic_writers_preserve_other_fields(tmp_path):
    store, run_id = setup(tmp_path)
    second = Store(tmp_path)
    def append(index):
        diag = Diagnostics(second if index % 2 else store, run_id)
        identifier = diag.begin("search", "tavily", {"query": f"query-{index}"})
        diag.finish(identifier, {"sources": []})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(8)))
    record = store.get(run_id)
    assert record["status"] == "running" and record["prompt"] == "MCP"
    assert len(record["exchanges"]) == 8
    assert all(entry["status"] == "completed" for entry in record["exchanges"])
    assert len({entry["exchange_id"] for entry in record["exchanges"]}) == 8


def test_recursive_redaction_of_embedded_json(tmp_path):
    store, run_id = setup(tmp_path)
    secret = 'complex-"secret"-value'
    diag = Diagnostics(store, run_id, (secret,))
    actual = {"body": {"api_key": secret, "messages": [{"content": json.dumps({"api_key": secret})}], "tool_calls": [{"function": {"arguments": json.dumps({"query": secret})}}]}, "authorization": secret}
    exchange = diag.begin("search", "deepseek_agent", actual)
    diag.finish(exchange, {"role": "assistant", "reasoning_content": "hidden", "content": secret})
    serialized = json.dumps(store.get(run_id)["exchanges"], ensure_ascii=False)
    assert secret not in serialized and "complex-" not in serialized and "hidden" not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.parametrize("mode,expected", [("transport", "timeout"), ("integrity", "artifact_integrity")])
def test_mcp_transport_and_local_validation_do_not_invent_sdk_error_response(tmp_path, mode, expected):
    from mcp import types
    store, run_id = setup(tmp_path)
    base_factory = memory_factory(store)
    @asynccontextmanager
    async def session(config, identifier):
        async with base_factory(config, identifier) as original:
            class Session:
                async def list_tools(self): return await original.list_tools()
                async def call_tool(self, name, arguments):
                    if mode == "transport":
                        raise PipelineError("timeout")
                    actual = await original.call_tool(name, arguments)
                    payload = dict(actual.structuredContent)
                    payload["query"] = "forged after actual SDK success"
                    return types.CallToolResult(content=actual.content, isError=False, structuredContent=payload)
            yield Session()
    run(Runner(Config(tmp_path), store, ScriptModel(), session).run(run_id))
    record = store.get(run_id)
    exchange = next(entry for entry in record["exchanges"] if entry["stage"] == "search")
    assert record["error_code"] == expected
    assert exchange["status"] == "failed" and exchange["error_code"] == expected
    assert exchange["response"] is None


def test_completed_receipt_closes_unfinished_diagnostic_without_changing_outcome(tmp_path):
    from tests.fakes import FakeSearch, FakeSummary
    store, run_id = setup(tmp_path)
    service = PipelineService(store, run_id, FakeSearch(), FakeSummary())
    async def stages():
        search = await service.search("MCP")
        summary = await service.summarize(search.search_result_id)
        return await service.save(summary.summary_id)
    file = run(stages())
    Diagnostics(store, run_id).begin("saveToFile", "mcp", {"method": "tools/call", "params": {"name": "saveToFile"}})
    store.recover()
    record = store.get(run_id)
    exchange = record["exchanges"][0]
    assert record["status"] == "completed" and record["file_result"] == file.model_dump()
    assert exchange["status"] == "failed" and exchange["error_code"] == "diagnostic_incomplete"
    assert exchange["response"] is None
