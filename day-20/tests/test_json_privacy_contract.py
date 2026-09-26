"""Independent inspector contracts; synthetic secrets and temporary SQLite only."""
import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.config import Settings
from backend.store import Store


SECRET = 'synthetic-ключ-"/\\\n-only-for-test'
ICS_PRIVATE = "PRIVATE_EVENT_TITLE_ONLY_FOR_TEST"


@pytest.fixture
def inspector_context(tmp_path):
    store = Store(tmp_path / "json-contract.sqlite")
    run, _ = store.new_run("json-request", "json-fingerprint", "Тест JSON-инспектора", None)
    settings = Settings(db_path=store.path, api_key="synthetic-model-key",
                        calendar_username="synthetic-calendar-user",
                        calendar_password="synthetic-calendar-password")
    return SimpleNamespace(store=store, run=run, settings=settings)


def persisted_json(store):
    return json.dumps(store.all("json_request"), ensure_ascii=False, sort_keys=True)


def assert_no_secret(value):
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    variants = [SECRET, json.dumps(SECRET, ensure_ascii=False)[1:-1],
                json.dumps(SECRET, ensure_ascii=True)[1:-1]]
    def strings(obj):
        if isinstance(obj, str): yield obj
        elif isinstance(obj, dict):
            for key, child in obj.items():
                yield str(key)
                yield from strings(child)
        elif isinstance(obj, list):
            for child in obj: yield from strings(child)
    assert all(variant not in text for text in strings(value) for variant in variants)
    assert ICS_PRIVATE not in serialized


def provider_reply(content="Ответ тестового транспорта", tool_calls=None):
    return {"id": "provider-response-real-id", "object": "chat.completion", "created": 1,
            "model": "synthetic-model", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": content,
                            "tool_calls": tool_calls or [], "reasoning_content": ICS_PRIVATE},
                "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}


def observer(context, **kwargs):
    from backend.json_requests import JsonObserver
    return JsonObserver(context.store,
        secrets=(SECRET, context.settings.api_key, context.settings.calendar_username, context.settings.calendar_password),
        paths=(context.store.path,), **kwargs)


def test_nested_json_arguments_and_escaped_credentials_are_redacted_before_persistence(inspector_context):
    scope = inspector_context
    capture = observer(scope)
    arguments = json.dumps({"name": "Омск", "nested": {"password": SECRET, "api_key": "unknown-api-value"},
                            "authorization": "Bearer unknown-token", "env": {"ARBITRARY": "unknown-env-value"}}, ensure_ascii=True)
    request = {"messages": [{"role": "assistant", "tool_calls": [{"id": "original-id", "function": {
        "name": "weather__resolve_city", "arguments": arguments}}]}],
        "free_text": SECRET + " " + json.dumps(SECRET, ensure_ascii=True)[1:-1],
        "service_path": scope.store.path, "headers": {"x-private": "unknown-header-value"},
        "reasoning_content": ICS_PRIVATE, "ics": "BEGIN:VCALENDAR\n" + ICS_PRIVATE}
    original = deepcopy(request)
    exchange_id = capture.begin("model", request, {"run_id": scope.run["id"]})
    assert exchange_id
    capture.finish(exchange_id, "completed", {"content": "Публичный ответ", "reasoning_content": ICS_PRIVATE})
    record = scope.store.get("json_request", exchange_id)
    assert_no_secret(record)
    dumped = json.dumps(record, ensure_ascii=False)
    for unknown in ("unknown-api-value", "unknown-token", "unknown-env-value", "unknown-header-value", scope.store.path):
        assert unknown not in dumped
    assert record["request"]["messages"][0]["tool_calls"][0]["id"] == "original-id"
    assert request == original, "Observing must not redact the request actually sent to the provider"
    request["messages"][0]["tool_calls"][0]["id"] = "mutated-after-observation"
    assert scope.store.get("json_request", exchange_id)["request"]["messages"][0]["tool_calls"][0]["id"] == "original-id"


def test_large_encoded_arguments_cannot_bypass_sensitive_container_redaction(inspector_context):
    capture = observer(inspector_context)
    encoded = json.dumps({"headers": {"x-private": "UNKNOWN_HEADER_SENTINEL"},
                          "env": {"PRIVATE_VALUE": "UNKNOWN_ENV_SENTINEL"}, "padding": "x" * 200000})
    exchange_id = capture.begin("model", {"arguments": encoded}, {"run_id": inspector_context.run["id"]})
    record = inspector_context.store.get("json_request", exchange_id)
    dumped = json.dumps(record, ensure_ascii=False)
    assert "UNKNOWN_HEADER_SENTINEL" not in dumped
    assert "UNKNOWN_ENV_SENTINEL" not in dumped
    assert record["truncated"]


def test_restart_interrupts_running_exchange_without_inventing_response(inspector_context):
    capture = observer(inspector_context)
    exchange_id = capture.begin("model", {"body": {"messages": []}}, {"run_id": inspector_context.run["id"]})
    reopened = Store(inspector_context.store.path)
    reopened.recover()
    record = reopened.get("json_request", exchange_id)
    assert record["status"] == "interrupted"
    assert record["response"] is None
    capture.finish(exchange_id, "completed", {"content": "Late response must not revive interrupted exchange"})
    assert reopened.get("json_request", exchange_id) == record


@pytest.mark.parametrize("ascii_escaped", [False, True])
def test_credentials_with_json_escaped_slashes_are_hidden_inside_non_json_debug_text(inspector_context, ascii_escaped):
    capture = observer(inspector_context)
    debug = "debug " + json.dumps({"value": SECRET}, ensure_ascii=ascii_escaped).replace("/", "\\/")
    exchange_id = capture.begin("model", {"debug": debug}, {"run_id": inspector_context.run["id"]})
    record = inspector_context.store.get("json_request", exchange_id)
    assert "synthetic-" not in record["request"]["debug"]


def test_real_sdk_body_matches_observed_body_without_changing_tool_ids(inspector_context):
    from backend.agent import Model
    from backend.json_requests import capture_scope
    scope = inspector_context
    capture = observer(scope)
    original_calls = [{"id": "provider-original-tool-id", "type": "function", "function": {
        "name": "weather__resolve_city", "arguments": json.dumps({"name": "Омск", "password": SECRET})}}]
    messages = [{"role": "user", "content": "Проверь погоду"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "previous-original-id", "type": "function",
            "function": {"name": "weather__resolve_city", "arguments": json.dumps({"name": "Омск", "api_key": SECRET})}}]},
        {"role": "tool", "tool_call_id": "previous-original-id", "content": '{"city_id":"real-source-id"}'}]
    tools = [{"type": "function", "function": {"name": "weather__resolve_city", "description": "Тест",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}}}}]
    before = deepcopy(messages)
    observed = []
    async def transport(request):
        observed.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=provider_reply(None, original_calls))
    async def scenario():
        model = Model(scope.settings, observer=capture,
                      http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        try:
            with capture_scope(scope.run["id"], trace_sequence=8, tool_call_id="context-id"):
                result = await model.complete(messages, tools)
            assert result["tool_calls"] == original_calls
        finally:
            await model.close()
    asyncio.run(scenario())
    assert len(observed) == 1
    record, = scope.store.json_requests(scope.run["id"])
    method, path, actual_body = observed[0]
    assert record["kind"] == "model" and record["status"] == "completed"
    assert record["request"] == capture.sanitize({"method": method, "path": path, "body": actual_body})
    assert actual_body["thinking"] == {"type": "disabled"}
    assert "extra_body" not in actual_body
    assert actual_body["messages"] == before == messages
    assert record["trace_sequence"] == 8 and record["tool_call_id"] == "context-id"
    assert record["response"]["id"] == "provider-response-real-id"
    assert "reasoning_content" not in record["response"]["choices"][0]["message"]
    assert record["response"]["choices"][0]["message"]["tool_calls"][0]["id"] == "provider-original-tool-id"
    assert_no_secret(record)


def test_concurrent_model_cancellation_does_not_finish_another_run(inspector_context):
    from backend.agent import Model
    from backend.json_requests import capture_scope
    scope = inspector_context
    second, _ = scope.store.new_run("second-http", "second-fingerprint", "Второй запуск", None)
    capture = observer(scope)
    async def scenario():
        first_started, second_started, release_second = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def transport(request):
            tag = json.loads(request.content)["messages"][-1]["content"]
            if tag == "FIRST":
                first_started.set()
                await asyncio.Event().wait()
            second_started.set()
            await release_second.wait()
            return httpx.Response(200, json=provider_reply("SECOND response"))
        model = Model(scope.settings, observer=capture,
                      http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        async def invoke(run_id, tag):
            with capture_scope(run_id, trace_sequence=1, tool_call_id=tag + "-metadata"):
                return await model.complete([{"role": "user", "content": tag}], [])
        try:
            first = asyncio.create_task(invoke(scope.run["id"], "FIRST"))
            second_task = asyncio.create_task(invoke(second["id"], "SECOND"))
            await asyncio.wait_for(asyncio.gather(first_started.wait(), second_started.wait()), timeout=2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError): await first
            release_second.set()
            assert (await second_task)["content"] == "SECOND response"
        finally:
            await model.close()
    asyncio.run(scenario())
    first_record, = scope.store.json_requests(scope.run["id"])
    second_record, = scope.store.json_requests(second["id"])
    assert first_record["status"] == "interrupted" and first_record["response"] is None
    assert second_record["status"] == "completed"
    assert second_record["response"]["choices"][0]["message"]["content"] == "SECOND response"
    for record, tag in ((first_record, "FIRST"), (second_record, "SECOND")):
        assert record["sequence"] == 1 and record["tool_call_id"] == tag + "-metadata"
        assert record["request"]["body"]["messages"][-1]["content"] == tag


def test_injected_model_is_not_claimed_as_an_observed_http_exchange(inspector_context):
    from backend.agent import Agent
    scope = inspector_context
    class FakeModel:
        async def complete(self, messages, tools): return {"content": "Ответ заглушки"}
    class FakeRegistry:
        def tools(self): return []
    asyncio.run(Agent(scope.store, FakeRegistry(), FakeModel(), scope.settings).run(scope.run["id"]))
    assert scope.store.get("run", scope.run["id"])["status"] == "completed"
    assert scope.store.json_requests(scope.run["id"]) == []


def test_legacy_run_endpoint_reports_absence_without_backfilling_or_mutating_run(inspector_context, monkeypatch):
    scope = inspector_context
    scope.store.update_run(scope.run["id"], status="completed", answer="Старый ответ",
        trace=[{"sequence": 1, "alias": "weather__resolve_city", "status": "completed",
                "arguments": {"name": "Омск"}, "result_summary": "Старая краткая трасса"}])
    before = deepcopy(scope.store.get("run", scope.run["id"]))
    messages_before = deepcopy(scope.store.all("message"))
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: scope.settings))
    from backend.app import create_app
    async def scenario():
        app = create_app(settings=scope.settings, model=object(), registry=object())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://contract") as client:
            for _ in range(2):
                result = await client.get(f"/api/runs/{scope.run['id']}/json-requests")
                assert result.status_code == 200
                assert result.json() == {"run_id": scope.run["id"], "capture_available": False, "exchanges": []}
            assert (await client.get("/api/runs/missing/json-requests")).status_code == 404
    asyncio.run(scenario())
    assert scope.store.get("run", scope.run["id"]) == before
    assert scope.store.all("message") == messages_before
    assert scope.store.all("json_request") == []


def install_registry_sessions(monkeypatch, handler):
    """Keep the Registry owner/discovery/guards; replace just MCP transport/session."""
    from mcp.types import CallToolResult, TextContent
    import backend.registry as module
    schemas = {
        "weather": {"resolve_city": {"name": {"type": "string"}, "run_id": {"type": "string"}}},
        "calendar": {"create_trip_event": {"plan_id": {"type": "string"}}},
        "plans": {"get_trip_plan": {"plan_id": {"type": "string"}, "run_id": {"type": "string"}}},
    }
    sessions = {}
    class Session:
        def __init__(self, server_id): self.server_id, self.calls = server_id, []
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def initialize(self): pass
        async def list_tools(self, cursor=None):
            return SimpleNamespace(nextCursor=None, tools=[SimpleNamespace(name=name, description="Synthetic contract tool",
                inputSchema={"type": "object", "properties": props, "required": list(props)})
                for name, props in schemas[self.server_id].items()])
        async def call_tool(self, name, arguments):
            self.calls.append((name, deepcopy(arguments)))
            result = await handler(self.server_id, name, arguments)
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))], structuredContent=result)
    for server_id in schemas: sessions[server_id] = Session(server_id)
    @asynccontextmanager
    async def transport(parameters): yield (parameters.args[-1], None)
    monkeypatch.setattr(module, "stdio_client", transport)
    monkeypatch.setattr(module, "ClientSession", lambda server_id, _: sessions[server_id])
    return sessions


def test_mcp_capture_uses_actual_params_and_preserves_concurrent_run_metadata(inspector_context, monkeypatch):
    from backend.registry import Registry
    from backend.json_requests import capture_scope
    scope = inspector_context
    second, _ = scope.store.new_run("second-mcp", "second-mcp-fingerprint", "Второй MCP", None)
    capture = observer(scope)
    async def handler(server_id, name, arguments):
        return {"source": server_id, "name": arguments["name"], "run_id": arguments["run_id"]}
    sessions = install_registry_sessions(monkeypatch, handler)
    async def scenario():
        registry = Registry(scope.settings, observer=capture)
        await registry.start()
        async def invoke(run_id, marker, sequence, tool_id):
            with capture_scope(run_id, sequence, tool_id):
                return await registry.call("weather__resolve_city", {"name": marker}, run_id=run_id)
        try:
            results = await asyncio.gather(
                invoke(scope.run["id"], "FIRST " + SECRET, 2, "first-original-id"),
                invoke(second["id"], "SECOND", 9, "second-original-id"))
            assert results[0]["name"] == "FIRST " + SECRET
            assert results[1]["name"] == "SECOND"
        finally:
            await registry.close()
    asyncio.run(scenario())
    for run_id, marker, seq, call_id in [(scope.run["id"], "FIRST " + SECRET, 2, "first-original-id"),
                                         (second["id"], "SECOND", 9, "second-original-id")]:
        record, = scope.store.json_requests(run_id)
        assert record["kind"] == "mcp" and record["status"] == "completed"
        assert record["tool_call_id"] == call_id and record["trace_sequence"] == seq and record["sequence"] == 1
        assert record["request"] == capture.sanitize({"method": "tools/call", "params": {
            "name": "resolve_city", "arguments": {"name": marker, "run_id": run_id}}})
        assert "id" not in record["request"], "The observer cannot invent the SDK JSON-RPC transport ID"
        assert_no_secret(record)
    assert [args["name"] for _, args in sessions["weather"].calls] == ["FIRST " + SECRET, "SECOND"]


def test_cancelled_mcp_exchange_does_not_gain_a_late_response(inspector_context, monkeypatch):
    from backend.registry import Registry
    from backend.json_requests import capture_scope
    scope = inspector_context
    capture = observer(scope)
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def handler(server_id, name, arguments):
            started.set()
            await release.wait()
            return {"city_id": "late-real-id"}
        install_registry_sessions(monkeypatch, handler)
        registry = Registry(scope.settings, observer=capture)
        await registry.start()
        async def invoke():
            with capture_scope(scope.run["id"], 4, "cancelled-original-id"):
                return await registry.call("weather__resolve_city", {"name": "Омск"}, run_id=scope.run["id"])
        caller = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError): await caller
            release.set()
        finally:
            await registry.close()
    asyncio.run(scenario())
    record, = scope.store.json_requests(scope.run["id"])
    assert record["status"] == "interrupted"
    assert record["response"] is None
    assert record["tool_call_id"] == "cancelled-original-id"


def test_rejected_before_dispatch_tool_is_not_fabricated_as_an_mcp_exchange(inspector_context, monkeypatch):
    from backend.registry import Registry
    from backend.json_requests import capture_scope
    scope = inspector_context
    async def handler(*args): pytest.fail("Rejected tool must never reach a session")
    sessions = install_registry_sessions(monkeypatch, handler)
    async def scenario():
        registry = Registry(scope.settings, observer=observer(scope))
        await registry.start()
        try:
            with capture_scope(scope.run["id"], 1, "rejected-original-id"):
                with pytest.raises(ValueError):
                    await registry.call("weather__resolve_city", {"name": "Омск", "env": {"private": "value"}}, run_id=scope.run["id"])
                with pytest.raises(ValueError):
                    await registry.call("calendar__create_trip_event", {"plan_id": "invented"}, run_id=scope.run["id"])
        finally:
            await registry.close()
    asyncio.run(scenario())
    assert scope.store.json_requests(scope.run["id"]) == []
    assert all(s.calls == [] for s in sessions.values())


def seed_calendar_plan(scope):
    plan = {"id": "synthetic-plan", "run_id": scope.run["id"], "title": "Тестовая поездка",
        "calendar_id": "calendar-test", "start": "2099-10-01T10:00:00+06:00", "end": "2099-10-01T13:00:00+06:00",
        "timezone": "Asia/Omsk", "uid": "immutable-confirmed-uid@day20.local", "status": "draft", "error": None}
    scope.store.put("plan", plan)
    class Calendar:
        def __init__(self): self.writes, self.events = [], {}
        def lookup(self, calendar_id, uid): return self.events.get(uid)
        def busy(self, *args): return []
        def create(self, current, uid):
            self.writes.append((deepcopy(current), uid))
            self.events[uid] = {"uid": uid, **{k: current[k] for k in ("title", "start", "end")}}
    return plan, Calendar()


def test_confirm_and_simultaneous_model_run_keep_exchange_ownership_and_immutable_uid(inspector_context, monkeypatch):
    from backend.agent import Model
    from backend.registry import Registry
    from backend.services import Service
    scope = inspector_context
    plan, calendar = seed_calendar_plan(scope)
    service = Service(scope.store, calendar=calendar)
    second, _ = scope.store.new_run("model-during-confirm", "model-fingerprint", "Новый независимый вопрос", None)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: scope.settings))
    from backend.app import create_app
    async def scenario():
        model_started, release_model = asyncio.Event(), asyncio.Event()
        async def mcp_handler(server_id, name, arguments):
            assert server_id == "calendar" and name == "create_trip_event"
            return service.create_trip_event(arguments["plan_id"])
        sessions = install_registry_sessions(monkeypatch, mcp_handler)
        async def transport(request):
            model_started.set()
            await release_model.wait()
            return httpx.Response(200, json=provider_reply("Ответ нового запуска"))
        model = Model(scope.settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        registry = Registry(scope.settings)
        await registry.start()
        app = create_app(settings=scope.settings, model=model, registry=registry)
        model_task = asyncio.create_task(app.state.agent.run(second["id"]))
        try:
            await asyncio.wait_for(model_started.wait(), timeout=2)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://contract") as client:
                response = await client.post(f"/api/plans/{plan['id']}/confirm")
                assert response.status_code == 200 and response.json()["status"] == "created"
            release_model.set()
            await asyncio.wait_for(model_task, timeout=2)
        finally:
            if not model_task.done():
                model_task.cancel()
                await asyncio.gather(model_task, return_exceptions=True)
            await registry.close()
            await model.close()
        assert sessions["calendar"].calls == [("create_trip_event", {"plan_id": plan["id"]})]
    asyncio.run(scenario())
    confirmed_exchange, = scope.store.json_requests(scope.run["id"])
    model_exchange, = scope.store.json_requests(second["id"])
    assert confirmed_exchange["kind"] == "mcp" and confirmed_exchange["status"] == "completed"
    assert confirmed_exchange["alias"] == "calendar__create_trip_event"
    assert confirmed_exchange["trace_sequence"] == 1 and confirmed_exchange["tool_call_id"] is None
    assert confirmed_exchange["request"] == {"method": "tools/call", "params": {
        "name": "create_trip_event", "arguments": {"plan_id": plan["id"]}}}
    assert model_exchange["kind"] == "model" and model_exchange["status"] == "completed"
    assert model_exchange["request"]["body"]["messages"][-1]["content"] == second["prompt"]
    assert scope.store.get("plan", plan["id"])["uid"] == plan["uid"]
    assert len(calendar.writes) == 1 and calendar.writes[0][1] == plan["uid"]


@pytest.mark.parametrize("failing_phase", ["begin", "finish"])
def test_observer_storage_failure_never_replaces_confirmed_calendar_result(inspector_context, monkeypatch, failing_phase):
    from backend.registry import Registry
    from backend.services import Service
    scope = inspector_context
    plan, calendar = seed_calendar_plan(scope)
    service = Service(scope.store, calendar=calendar)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: scope.settings))
    from backend.app import create_app
    async def scenario():
        finish_started = False
        async def handler(server_id, name, arguments):
            nonlocal finish_started
            result = service.create_trip_event(arguments["plan_id"])
            finish_started = True
            return result
        install_registry_sessions(monkeypatch, handler)
        registry = Registry(scope.settings)
        await registry.start()
        app = create_app(settings=scope.settings, model=object(), registry=registry)
        capture_store = registry.observer.store
        if failing_phase == "begin":
            def broken_allocate(*args): raise RuntimeError("Synthetic observer database failure")
            monkeypatch.setattr(capture_store, "allocate_json_request", broken_allocate)
        else:
            original_put = capture_store.put
            def failing_put(kind, value, db=None):
                if kind == "json_request" and finish_started:
                    raise RuntimeError("Synthetic observer persistence failure after accepted write")
                return original_put(kind, value, db)
            monkeypatch.setattr(capture_store, "put", failing_put)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://contract") as client:
                response = await client.post(f"/api/plans/{plan['id']}/confirm")
                assert response.status_code == 200 and response.json()["status"] == "created"
        finally:
            await registry.close()
    asyncio.run(scenario())
    assert scope.store.get("plan", plan["id"])["status"] == "created"
    assert len(calendar.writes) == 1 and calendar.writes[0][1] == plan["uid"]


def test_private_calendar_fields_are_hidden_without_erasing_tool_descriptions(inspector_context):
    capture = observer(inspector_context)
    request = {"tools": [{"type": "function", "function": {"name": "weather__resolve_city",
        "description": "Публичное описание инструмента", "parameters": {"type": "object"}}}]}
    response = {"events": [{"summary": ICS_PRIVATE, "description": ICS_PRIVATE + " description",
        "location": ICS_PRIVATE + " location", "DTSTART": "2099-10-01T10:00:00Z",
        "DTEND": "2099-10-01T11:00:00Z", "attendees": [ICS_PRIVATE], "organizer": ICS_PRIVATE}]}
    exchange_id = capture.begin("model", request, {"run_id": inspector_context.run["id"]})
    capture.finish(exchange_id, "completed", response)
    record = inspector_context.store.get("json_request", exchange_id)
    assert ICS_PRIVATE not in json.dumps(record, ensure_ascii=False)
    assert record["request"]["tools"][0]["function"]["description"] == "Публичное описание инструмента"


def test_small_snapshot_budget_keeps_record_identity_and_explicit_truncation(inspector_context):
    capture = observer(inspector_context, max_bytes=512)
    exchange_id = capture.begin("model", {"messages": [{"content": "x" * 5000}]},
        {"run_id": inspector_context.run["id"], "trace_sequence": 5, "tool_call_id": "original-call-id"})
    assert exchange_id
    capture.finish(exchange_id, "completed", {"content": "y" * 5000})
    record = inspector_context.store.get("json_request", exchange_id)
    assert record["id"] == exchange_id and record["run_id"] == inspector_context.run["id"]
    assert record["truncated"] and record["status"] == "completed"
    assert record["trace_sequence"] == 5 and record["tool_call_id"] == "original-call-id"
    for body in (record["request"], record["response"]):
        assert len(json.dumps(body, ensure_ascii=False).encode()) <= 512
