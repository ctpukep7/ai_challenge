"""Independent flow contracts: actual service/schema checks, synthetic sources."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from backend.config import Settings
from backend.providers import instant
from backend.registry import Registry
from backend.services import Service
from backend.store import Store


ZONE = "Asia/Omsk"
START = "2026-10-01T10:00:00+06:00"
END = "2026-10-01T18:00:00+06:00"
TRIP_END = "2026-10-01T13:00:00+06:00"
TOOLS = {
    "weather": ["resolve_city", "get_hourly_forecast"],
    "calendar": ["list_calendars", "find_free_slots", "create_trip_event"],
    "plans": ["save_trip_draft", "get_trip_plan"],
}


class Weather:
    def __init__(self):
        self.hours = [
            {"time": (instant(START) + timedelta(hours=i)).isoformat(),
             "temperature_c": 10, "precipitation_probability": 20, "wind_ms": 4}
            for i in range(8)
        ]
    def resolve(self, name):
        return [{"name": "Омск", "latitude": 55, "longitude": 73, "timezone": ZONE}]
    def forecast(self, city, timezone):
        return {"hours": deepcopy(self.hours), "available_start": START, "available_end": END, "timezone": ZONE}


class Calendar:
    def __init__(self):
        self.intervals = []
        self.writes = []
        self.items = [{"id": "calendar-test", "name": "Тестовый календарь"}]
    def calendars(self): return deepcopy(self.items)
    def busy(self, calendar_id, start, end, timezone): return list(self.intervals)
    def lookup(self, calendar_id, uid): return None
    def create(self, plan, uid): self.writes.append((plan, uid))


@pytest.fixture
def context(tmp_path, monkeypatch):
    import backend.services as services
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 9, 26, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(services, "datetime", FixedDateTime)
    store = Store(tmp_path / "orchestration-contract.sqlite")
    weather, calendar = Weather(), Calendar()
    service = Service(store, weather=weather, calendar=calendar, timezone=ZONE)
    run, _ = store.new_run("request-flow", "fingerprint-flow", "Тестовый длинный поток", "calendar-test")
    return SimpleNamespace(store=store, service=service, weather=weather, calendar=calendar,
                           run=run, settings=Settings(db_path=str(store.path), api_key="synthetic"))


def evidence(context, run=None):
    run_id = (run or context.run)["id"]
    city = context.service.resolve_city("Омск", run_id)
    forecast = context.service.get_hourly_forecast(city["city_id"], 0, START, END, run_id)
    calendars = context.service.list_calendars(run_id)
    available = context.service.find_free_slots(calendars["calendars_id"], "calendar-test", START, END, 3, "10:00", "18:00", run_id)
    return forecast, available


def draft_arguments(forecast_id, availability_id):
    return dict(forecast_id=forecast_id, availability_id=availability_id, start=START, end=TRIP_END,
                title="Тестовая поездка", reason="Основано на двух настоящих тестовых источниках",
                min_temperature_c=5, max_precipitation_probability=30, max_wind_ms=8)


@pytest.mark.parametrize("bad_source", ["forecast", "availability"])
def test_forged_source_cannot_save_a_plan(context, bad_source):
    forecast, available = evidence(context)
    args = draft_arguments(forecast["forecast_id"], available["availability_id"])
    args[bad_source + "_id"] = "invented-source"
    with pytest.raises(ValueError):
        context.service.save_trip_draft(**args, run_id=context.run["id"])
    assert context.store.all("plan") == []


def test_premature_save_has_no_side_effect(context):
    with pytest.raises(ValueError):
        context.service.save_trip_draft(**draft_arguments("not-read", "not-read"), run_id=context.run["id"])
    assert context.store.all("plan") == []


@pytest.mark.parametrize("bad_source", ["forecast", "availability"])
def test_sources_from_another_run_cannot_authorize_the_current_plan(context, bad_source):
    own_forecast, own_available = evidence(context)
    other, _ = context.store.new_run("request-other", "fingerprint-other", "Другой запрос", "calendar-test")
    other_forecast, other_available = evidence(context, other)
    args = draft_arguments(own_forecast["forecast_id"], own_available["availability_id"])
    args[bad_source + "_id"] = (other_forecast if bad_source == "forecast" else other_available)[bad_source + "_id"]
    with pytest.raises(ValueError):
        context.service.save_trip_draft(**args, run_id=context.run["id"])
    assert context.store.all("plan") == []


@pytest.mark.parametrize("change", ["bad-middle-hour", "gap", "missing-value"])
def test_weather_must_cover_every_hour_of_the_trip(context, change):
    if change == "bad-middle-hour": context.weather.hours[1]["precipitation_probability"] = 80
    elif change == "gap": context.weather.hours.pop(1)
    else: context.weather.hours[1]["wind_ms"] = None
    forecast, available = evidence(context)
    with pytest.raises(ValueError):
        context.service.save_trip_draft(**draft_arguments(forecast["forecast_id"], available["availability_id"]), run_id=context.run["id"])
    assert context.store.all("plan") == []


def test_bad_weather_after_exclusive_trip_end_does_not_reject_valid_trip(context):
    context.weather.hours[3]["precipitation_probability"] = 100
    forecast, available = evidence(context)
    plan = context.service.save_trip_draft(**draft_arguments(forecast["forecast_id"], available["availability_id"]), run_id=context.run["id"])
    assert plan["status"] == "draft"
    assert plan["weather_summary"]["max_precipitation_probability"] == 20
    assert context.calendar.writes == []


def test_multiple_calendars_require_explicit_selection(context):
    context.calendar.items.append({"id": "other-calendar", "name": "Второй тестовый"})
    run, _ = context.store.new_run("request-unselected", "fingerprint-unselected", "Проверь календарь", None)
    found = context.service.list_calendars(run["id"])
    with pytest.raises(ValueError):
        context.service.find_free_slots(found["calendars_id"], "calendar-test", START, END, 3, "10:00", "18:00", run["id"])
    assert not [e for e in context.store.all("evidence") if e["run_id"] == run["id"] and e["kind"] == "availability"]


def test_unknown_alias_is_rejected_before_dispatch(context):
    registry = Registry(context.settings)
    with pytest.raises(ValueError):
        asyncio.run(registry.call("calendar__invented", {}, run_id=context.run["id"]))
    assert registry.queue.empty()


@pytest.mark.parametrize("arguments", [{"name": 15}, {"name": "Омск", "unexpected": "field"}, {"name": "Омск", "run_id": "other"}])
def test_discovered_schema_rejects_invalid_or_injected_arguments(context, arguments):
    registry = Registry(context.settings)
    registry.add("weather", SimpleNamespace(name="resolve_city", description="test", inputSchema={
        "type": "object", "properties": {"name": {"type": "string"}, "run_id": {"type": "string"}},
        "required": ["name", "run_id"]}), object())
    with pytest.raises(ValueError):
        asyncio.run(registry.call("weather__resolve_city", arguments, run_id=context.run["id"]))
    assert registry.queue.empty()


def install_inprocess_transports(monkeypatch, context):
    """Replace transport boundaries only; keep Registry discovery and dispatch intact."""
    from mcp.server.fastmcp import FastMCP
    from mcp.types import CallToolResult, TextContent
    import backend.registry as module
    sessions = {}
    for server_id, names in TOOLS.items():
        server = FastMCP("Contract fixture " + server_id)
        for name in names: server.add_tool(getattr(context.service, name), name=name)
        class Session:
            def __init__(self, server, server_id):
                self.server, self.server_id = server, server_id
                self.calls, self.initialized = [], False
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def initialize(self): self.initialized = True
            async def list_tools(self, cursor=None):
                assert self.initialized
                return SimpleNamespace(tools=await self.server.list_tools(), nextCursor=None)
            async def call_tool(self, name, arguments):
                self.calls.append((name, deepcopy(arguments)))
                try:
                    result = await self.server.call_tool(name, arguments)
                    if isinstance(result, tuple):
                        return CallToolResult(content=list(result[0]), structuredContent=result[1])
                    if isinstance(result, dict):
                        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result))], structuredContent=result)
                    return CallToolResult(content=list(result))
                except Exception as error:
                    return CallToolResult(isError=True, content=[TextContent(type="text", text=str(error))])
        sessions[server_id] = Session(server, server_id)
    @asynccontextmanager
    async def transport(parameters):
        yield (parameters.args[-1], None)
    monkeypatch.setattr(module, "stdio_client", transport)
    monkeypatch.setattr(module, "ClientSession", lambda server_id, _: sessions[server_id])
    return sessions


def call_message(name, arguments, call_id):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


class FlowModel:
    def __init__(self, order):
        self.order, self.inputs, self.results = order, [], {}
    async def complete(self, messages, tools):
        self.inputs.append((deepcopy(messages), deepcopy(tools)))
        for message in messages:
            if message.get("role") == "tool": self.results[message["tool_call_id"]] = json.loads(message["content"])
        index = len(self.inputs) - 1
        if index >= len(self.order): return {"role": "assistant", "content": "Тестовый план предложен, запись ожидает подтверждения"}
        step = self.order[index]
        if step == "resolve": name, args = "weather__resolve_city", {"name": "Омск"}
        elif step == "calendars": name, args = "calendar__list_calendars", {}
        elif step == "forecast":
            name, args = "weather__get_hourly_forecast", {"city_id": self.results["resolve"]["city_id"], "city_index": 0, "start": START, "end": END}
        elif step == "availability":
            name, args = "calendar__find_free_slots", {"calendars_id": self.results["calendars"]["calendars_id"], "calendar_id": "calendar-test", "start": START, "end": END, "duration_hours": 3, "day_start": "10:00", "day_end": "18:00"}
        elif step == "draft":
            name, args = "plans__save_trip_draft", draft_arguments(self.results["forecast"]["forecast_id"], self.results["availability"]["availability_id"])
        elif step == "read": name, args = "plans__get_trip_plan", {"plan_id": self.results["draft"]["id"]}
        else: raise AssertionError("Unexpected scripted step")
        return call_message(name, args, step)


def execute(context, monkeypatch, model):
    from backend.agent import Agent
    sessions = install_inprocess_transports(monkeypatch, context)
    async def scenario():
        registry = Registry(context.settings)
        await registry.start()
        try:
            await Agent(context.store, registry, model, context.settings).run(context.run["id"])
        finally:
            await registry.close()
    asyncio.run(scenario())
    return context.store.get("run", context.run["id"]), sessions


@pytest.mark.parametrize("order", [
    ["resolve", "forecast", "calendars", "availability", "draft", "read"],
    ["calendars", "availability", "resolve", "forecast", "draft", "read"],
])
def test_six_call_three_server_flow_preserves_model_choice_and_tool_ids(context, monkeypatch, order):
    model = FlowModel(order)
    run, sessions = execute(context, monkeypatch, model)
    assert run["status"] == "completed", run.get("error")
    assert run["plan"]["status"] == "draft"
    aliases = {name: server + "__" + name for server, names in TOOLS.items() for name in names}
    expected = {
        "resolve": aliases["resolve_city"], "forecast": aliases["get_hourly_forecast"],
        "calendars": aliases["list_calendars"], "availability": aliases["find_free_slots"],
        "draft": aliases["save_trip_draft"], "read": aliases["get_trip_plan"],
    }
    assert [t["alias"] for t in run["trace"]] == [expected[step] for step in order]
    assert [t["sequence"] for t in run["trace"]] == list(range(1, 7))
    assert all(t["status"] == "completed" for t in run["trace"])
    assert {t["server_id"] for t in run["trace"]} == {"weather", "calendar", "plans"}
    assert sum(len(session.calls) for session in sessions.values()) == 6
    assert context.calendar.writes == []
    exposed = set(aliases.values()) - {"calendar__create_trip_event"}
    for index, (messages, tools) in enumerate(model.inputs):
        assert {t["function"]["name"] for t in tools} == exposed
        assert all("run_id" not in t["function"]["parameters"].get("properties", {}) for t in tools)
        previous = order[:index]
        assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == previous
        assert [m["tool_calls"][0]["id"] for m in messages if m["role"] == "assistant" and m.get("tool_calls")] == previous
    read_result = json.loads(model.inputs[-1][0][-1]["content"])
    assert read_result["id"] == run["plan"]["id"]
    assert read_result["forecast_id"] == model.results["forecast"]["forecast_id"]
    assert read_result["availability_id"] == model.results["availability"]["availability_id"]


@pytest.mark.parametrize("order,expected_servers", [
    (["resolve", "forecast"], {"weather"}),
    (["calendars", "availability"], {"calendar"}),
])
def test_single_topic_request_calls_only_relevant_server(context, monkeypatch, order, expected_servers):
    run, sessions = execute(context, monkeypatch, FlowModel(order))
    assert run["status"] == "completed", run.get("error")
    assert {t["server_id"] for t in run["trace"]} == expected_servers
    assert {id for id, s in sessions.items() if s.calls} == expected_servers
    assert context.store.all("plan") == []
    assert context.calendar.writes == []


def test_saved_plan_survives_failure_of_final_model_response(context, monkeypatch):
    class FailingFinal(FlowModel):
        async def complete(self, messages, tools):
            if len(self.inputs) == len(self.order): raise RuntimeError("Synthetic provider failure")
            return await super().complete(messages, tools)
    order = ["resolve", "calendars", "forecast", "availability", "draft", "read"]
    run, _ = execute(context, monkeypatch, FailingFinal(order))
    assert run["status"] == "error"
    assert run["plan"]["status"] == "draft"
    reopened = Store(context.store.path)
    assert reopened.get("plan", run["plan"]["id"]) == run["plan"]
    assert context.calendar.writes == []


@pytest.mark.parametrize("field", ["temperature_c", "precipitation_probability", "wind_ms"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_weather_is_never_accepted_as_trip_evidence(context, field, value):
    context.weather.hours[1][field] = value
    # Reject either at ingestion or draft validation; never produce a plan.
    with pytest.raises(ValueError):
        forecast, available = evidence(context)
        context.service.save_trip_draft(**draft_arguments(forecast["forecast_id"], available["availability_id"]), run_id=context.run["id"])
    assert context.store.all("plan") == []


@pytest.mark.parametrize("field", ["min_temperature_c", "max_precipitation_probability", "max_wind_ms"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_user_thresholds_are_rejected(context, field, value):
    forecast, available = evidence(context)
    args = draft_arguments(forecast["forecast_id"], available["availability_id"])
    args[field] = value
    with pytest.raises(ValueError):
        context.service.save_trip_draft(**args, run_id=context.run["id"])
    assert context.store.all("plan") == []


def test_identical_raw_names_route_to_the_owning_session(context, monkeypatch):
    sessions = install_inprocess_transports(monkeypatch, context)
    def echo_for(server_id):
        def echo(value: str) -> dict: return {"source": server_id, "value": value}
        return echo
    for server_id in ("weather", "calendar"):
        sessions[server_id].server.add_tool(echo_for(server_id), name="echo")
    async def scenario():
        registry = Registry(context.settings)
        await registry.start()
        try:
            weather = await registry.call("weather__echo", {"value": "first"})
            calendar = await registry.call("calendar__echo", {"value": "second"})
            assert weather == {"source": "weather", "value": "first"}
            assert calendar == {"source": "calendar", "value": "second"}
        finally:
            await registry.close()
    asyncio.run(scenario())
    assert sessions["weather"].calls == [("echo", {"value": "first"})]
    assert sessions["calendar"].calls == [("echo", {"value": "second"})]
    assert sessions["plans"].calls == []


def test_model_cannot_invoke_discovered_calendar_write_alias(context, monkeypatch):
    install_inprocess_transports(monkeypatch, context)
    async def scenario():
        registry = Registry(context.settings)
        await registry.start()
        try:
            assert "calendar__create_trip_event" in registry.entries
            assert "calendar__create_trip_event" not in {t["function"]["name"] for t in registry.tools()}
            with pytest.raises(ValueError):
                await registry.call("calendar__create_trip_event", {"plan_id": "invented"}, run_id=context.run["id"])
        finally:
            await registry.close()
    asyncio.run(scenario())
    assert context.calendar.writes == []


def test_response_with_four_calls_is_rejected_before_any_dispatch(context, monkeypatch):
    class TooManyCalls:
        async def complete(self, messages, tools):
            return {"tool_calls": [call_message("weather__resolve_city", {"name": "Омск"}, f"call-{i}")["tool_calls"][0] for i in range(4)]}
    run, sessions = execute(context, monkeypatch, TooManyCalls())
    assert run["status"] == "incomplete"
    assert run["trace"] == []
    assert all(session.calls == [] for session in sessions.values())


@pytest.mark.parametrize("errors_before_success,expected_status", [(2, "completed"), (3, "incomplete")])
def test_argument_correction_budget_stops_after_two_repair_attempts(context, monkeypatch, errors_before_success, expected_status):
    class RepairModel:
        calls = 0
        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls <= errors_before_success:
                return call_message("unknown__missing", {}, f"invalid-{self.calls}")
            if self.calls == errors_before_success + 1:
                return call_message("weather__resolve_city", {"name": "Омск"}, "valid")
            return {"content": "Уточнение обработано"}
    model = RepairModel()
    run, _ = execute(context, monkeypatch, model)
    assert run["status"] == expected_status
    assert len([t for t in run["trace"] if t["status"] == "error"]) == errors_before_success
    if errors_before_success == 3:
        assert model.calls == 3
        assert context.store.all("evidence") == []


def test_http_request_uuid_replays_same_run_and_rejects_changed_payload(context, monkeypatch):
    import httpx
    from uuid import uuid4
    # Override the settings factory before importing module-level app.
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: context.settings))
    from backend.app import create_app
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        class Model:
            calls = 0
            async def complete(self, messages, tools):
                self.calls += 1
                started.set()
                await release.wait()
                return {"content": "Тестовый ответ"}
        class NoToolRegistry:
            def tools(self): return []
        model = Model()
        app = create_app(settings=context.settings, model=model, registry=NoToolRegistry())
        request = {"prompt": "Как спланировать поездку?", "request_id": str(uuid4()), "calendar_id": "calendar-test"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://contract") as client:
            first = await client.post("/api/chat", json=request)
            await asyncio.wait_for(started.wait(), timeout=2)
            replay = await client.post("/api/chat", json=request)
            assert first.status_code == replay.status_code == 202
            assert first.json() == replay.json()
            assert (await client.post("/api/chat", json={**request, "prompt": "Изменённый запрос"})).status_code == 409
            assert (await client.post("/api/chat", json={**request, "calendar_id": "other-calendar"})).status_code == 409
            release.set()
            async def completed():
                while True:
                    result = await client.get("/api/runs/" + first.json()["run_id"])
                    if result.json()["status"] == "completed": return result.json()
                    await asyncio.sleep(0)
            run = await asyncio.wait_for(completed(), timeout=2)
            assert run["answer"] == "Тестовый ответ"
            assert model.calls == 1
            messages = [m for m in context.store.all("message") if m["run_id"] == run["id"]]
            assert [m["role"] for m in messages] == ["user", "assistant"]
    asyncio.run(scenario())


def test_restart_marks_inflight_work_interrupted_and_keeps_the_saved_plan(context):
    forecast, available = evidence(context)
    plan = context.service.save_trip_draft(**draft_arguments(forecast["forecast_id"], available["availability_id"]), run_id=context.run["id"])
    context.store.update_run(context.run["id"], status="running")
    restarted = Store(context.store.path)
    restarted.recover()
    run = restarted.get("run", context.run["id"])
    assert run["status"] == "interrupted"
    assert run["plan"]["id"] == plan["id"]
    assert restarted.get("plan", plan["id"])["status"] == "draft"


def test_real_stdio_timeout_closes_all_three_transports_in_reverse_order(context, monkeypatch, tmp_path):
    """Real SDK subprocesses; delay only a client call, with all providers offline."""
    import backend.registry as module
    path = tmp_path / "stdio-source.json"
    path.write_text(json.dumps({"cities": context.weather.resolve("Омск"),
        "forecast": context.weather.forecast({}, ZONE), "calendars": context.calendar.calendars()}))
    settings = Settings(db_path=context.store.path, fixture_path=str(path), tool_timeout=15,
                        api_key="model-secret-must-stay-host-only", calendar_username="synthetic-user",
                        calendar_password="synthetic-calendar-password")
    original_transport, original_session = module.stdio_client, module.ClientSession
    entered, exited, environments = [], [], {}
    @asynccontextmanager
    async def tracked_transport(parameters):
        environments[parameters.args[-1]] = dict(parameters.env)
        async with original_transport(parameters) as streams:
            entered.append(parameters.args[-1])
            try: yield streams
            finally: exited.append(parameters.args[-1])
    class SlowSession(original_session):
        async def call_tool(self, name, arguments, **kwargs):
            await asyncio.sleep(1)
            return await super().call_tool(name, arguments, **kwargs)
    monkeypatch.setattr(module, "stdio_client", tracked_transport)
    monkeypatch.setattr(module, "ClientSession", SlowSession)
    async def scenario():
        registry = Registry(settings)
        await registry.start()
        assert len(registry.entries) == 7
        settings.tool_timeout = 0.05
        try:
            with pytest.raises(TimeoutError):
                await registry.call("weather__resolve_city", {"name": "Омск"}, run_id=context.run["id"])
        finally:
            await asyncio.wait_for(registry.close(), timeout=5)
        assert registry.task is None
    asyncio.run(scenario())
    assert entered == ["weather", "calendar", "plans"]
    assert exited == ["plans", "calendar", "weather"]
    assert all("DEEPSEEK_API_KEY" not in env for env in environments.values())
    assert environments["calendar"]["YANDEX_CALDAV_USERNAME"] == "synthetic-user"
    assert environments["calendar"]["YANDEX_CALDAV_APP_PASSWORD"] == "synthetic-calendar-password"
    for server_id in ("weather", "plans"):
        assert not any(key.startswith("YANDEX_CALDAV_") for key in environments[server_id])


def test_confirmation_between_draft_and_model_read_never_overwrites_agent_trace(context, monkeypatch):
    import httpx
    install_inprocess_transports(monkeypatch, context)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: context.settings))
    from backend.app import create_app
    async def scenario():
        draft_ready, continue_read = asyncio.Event(), asyncio.Event()
        class PausedBeforeRead(FlowModel):
            async def complete(self, messages, tools):
                if len(self.inputs) == 5:
                    draft_ready.set()
                    await continue_read.wait()
                return await super().complete(messages, tools)
        model = PausedBeforeRead(["resolve", "calendars", "forecast", "availability", "draft", "read"])
        registry = Registry(context.settings)
        await registry.start()
        app = create_app(settings=context.settings, model=model, registry=registry)
        run_task = asyncio.create_task(app.state.agent.run(context.run["id"]))
        try:
            await asyncio.wait_for(draft_ready.wait(), timeout=2)
            plan = context.store.plan_for_run(context.run["id"])
            assert plan and plan["status"] == "draft"
            assert len(context.store.get("run", context.run["id"])["trace"]) == 5
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://contract") as client:
                confirmation = await client.post(f"/api/plans/{plan['id']}/confirm")
                assert confirmation.status_code == 200
                assert confirmation.json()["status"] == "created"
            continue_read.set()
            await asyncio.wait_for(run_task, timeout=2)
        finally:
            if not run_task.done():
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
            await registry.close()
    asyncio.run(scenario())
    run = context.store.get("run", context.run["id"])
    assert run["status"] == "completed", run.get("error")
    assert [t["sequence"] for t in run["trace"]] == list(range(1, 8))
    assert [t["alias"] for t in run["trace"]][-3:] == [
        "plans__save_trip_draft", "calendar__create_trip_event", "plans__get_trip_plan"]
    assert all(t["status"] == "completed" for t in run["trace"])
    assert len(context.calendar.writes) == 1


@pytest.mark.parametrize("calls_per_response,expected_rounds,expected_calls", [(1, 12, 12), (3, 7, 18)])
def test_model_round_and_total_tool_budgets_stop_further_dispatch(context, monkeypatch, calls_per_response, expected_rounds, expected_calls):
    class RepeatingModel:
        rounds = 0
        async def complete(self, messages, tools):
            self.rounds += 1
            return {"tool_calls": [call_message("weather__resolve_city", {"name": "Омск"},
                f"round-{self.rounds}-call-{i}")["tool_calls"][0] for i in range(calls_per_response)]}
    model = RepeatingModel()
    run, sessions = execute(context, monkeypatch, model)
    assert run["status"] == "incomplete"
    assert model.rounds == expected_rounds
    assert len(run["trace"]) == expected_calls
    assert len(sessions["weather"].calls) == expected_calls
    assert sessions["calendar"].calls == sessions["plans"].calls == []
    assert all(t["status"] == "completed" for t in run["trace"])
    assert context.store.all("plan") == []


def test_overall_deadline_interrupts_model_and_keeps_run_terminal(context, monkeypatch):
    class SlowModel:
        cancelled = False
        async def complete(self, messages, tools):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
    context.settings.run_timeout = 0.02
    model = SlowModel()
    run, sessions = execute(context, monkeypatch, model)
    assert run["status"] == "incomplete" and run["error"]
    assert model.cancelled
    assert all(s.calls == [] for s in sessions.values())
