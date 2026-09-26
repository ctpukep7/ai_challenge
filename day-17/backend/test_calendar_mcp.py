from __future__ import annotations

import json
import importlib.util
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from . import calendar_agent
from .calendar_agent import CalendarMCPAgent, local_mcp_session
from .main import create_app
from .store import SQLiteMemoryStore


def completion(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=4, total_tokens=14)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def tool_call(name="list_upcoming_events", arguments='{"days": 3}'):
    return SimpleNamespace(id="call-1", function=SimpleNamespace(name=name, arguments=arguments))


class FakeCompletions:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def create(self, **options):
        self.calls.append(options)
        return self.answers.pop(0)


class FakeSession:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"ok": True, "days": 3, "events": [{"start": "2026-09-24T10:00:00", "end": "2026-09-24T11:00:00", "title": "Секретная встреча"}], "truncated": False}

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(name="list_upcoming_events", description="Ближайшие встречи", inputSchema={"type": "object", "properties": {"days": {"type": "integer"}}}),
            SimpleNamespace(name="list_calendars", description="Календари", inputSchema={"type": "object", "properties": {}}),
            SimpleNamespace(name="create_event", description="Создать событие", inputSchema={"type": "object", "properties": {}}),
        ])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(structuredContent=self.result, isError=False, content=[])


@asynccontextmanager
async def session_factory(session):
    yield session


def bundle(tmp_path, answers, session=None):
    store = SQLiteMemoryStore(tmp_path / "day17.sqlite3")
    fake = FakeCompletions(answers)
    client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    session = session or FakeSession()
    agent = CalendarMCPAgent(store, client=client, mcp_session_factory=lambda: session_factory(session))
    return store, fake, session, TestClient(create_app(store, agent))


def ask(client):
    return client.post("/api/ask", json={"prompt": "Какие у меня ближайшие встречи?", "controls": {"compression_mode": "full_session"}})


def test_model_selects_tool_then_uses_mcp_result(tmp_path):
    store, fake, session, client = bundle(tmp_path, [completion(tool_calls=[tool_call()]), completion("Завтра в 10:00 — секретная встреча.")])
    response = ask(client)
    assert response.status_code == 200
    body = response.json()
    assert len(fake.calls) == 2
    assert fake.calls[0]["tool_choice"] == "auto"
    assert fake.calls[0]["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {"days": {"type": "integer"}}}
    assert session.calls == [("list_upcoming_events", {"days": 3})]
    assert fake.calls[1]["messages"][-1]["role"] == "tool"
    assert "Секретная встреча" in fake.calls[1]["messages"][-1]["content"]
    assert fake.calls[1]["tools"] == fake.calls[0]["tools"]
    assert fake.calls[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"] == '{"days": 3}'
    assert fake.calls[1]["messages"][-1]["tool_call_id"] == "call-1"
    assert body["mcp_trace"]["called_tool"] == {"name": "list_upcoming_events", "arguments": {"days": 3}, "status": "success", "event_count": 1}
    assert "Секретная встреча" not in json.dumps(body["mcp_trace"], ensure_ascii=False)
    assert body["preview"]["mcp_trace"] == body["mcp_trace"]
    assert store.session_messages(store.active_session()["id"])[-1]["content"] == body["answer"]


def test_no_model_tool_call_means_no_calendar_call(tmp_path):
    _, fake, session, client = bundle(tmp_path, [completion("Объясню, как работает календарь.")])
    response = ask(client)
    assert response.status_code == 200
    assert len(fake.calls) == 1
    assert session.calls == []
    assert response.json()["mcp_trace"]["called_tool"] is None


def test_explicit_create_request_proposes_without_mcp_write(tmp_path):
    omsk = timezone(timedelta(hours=6))
    start = (datetime.now(omsk) + timedelta(days=7)).replace(microsecond=0)
    arguments = {"title": "Демо", "start": start.isoformat(),
                 "end": (start + timedelta(hours=2)).isoformat()}

    class ProposalSession(FakeSession):
        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            assert name == "list_calendars"
            return SimpleNamespace(structuredContent={"ok": True, "calendars": [
                {"id": "cal_" + "a" * 24, "name": "Личный"}]}, isError=False, content=[])

    store, fake, session, client = bundle(tmp_path, [completion(tool_calls=[tool_call("create_event", json.dumps(arguments))])], ProposalSession())
    response = client.post("/api/ask", json={"prompt": "Создай событие в пятницу с 15 до 17 по Омску, название Демо"})
    assert response.status_code == 200
    assert fake.calls[0]["tool_choice"] == "required"
    assert "Asia/Omsk" in json.dumps(fake.calls[0]["messages"], ensure_ascii=False)
    assert response.json()["pending_action"]["status"] == "pending"
    assert [name for name, _ in session.calls] == ["list_calendars"]
    assert store.session_messages(store.active_session()["id"])[-1]["content"] == response.json()["answer"]


def test_explicit_create_no_tool_is_error_not_false_promise(tmp_path):
    store, fake, session, client = bundle(tmp_path, [completion("Я предложу событие после подтверждения.")])
    response = client.post("/api/ask", json={"prompt": "Создай событие завтра по Омску в 15:00"})
    assert response.status_code == 502
    assert fake.calls[0]["tool_choice"] == "required"
    assert session.calls == []
    assert store.session_messages(store.active_session()["id"]) == []


def test_read_then_proposal_keeps_tools_and_matching_call_id(tmp_path):
    omsk = timezone(timedelta(hours=6))
    start = (datetime.now(omsk) + timedelta(days=7)).replace(microsecond=0)
    arguments = {"title": "Демо", "start": start.isoformat(),
                 "end": (start + timedelta(hours=1)).isoformat()}

    class ProposalSession(FakeSession):
        async def call_tool(self, name, arguments):
            if name == "list_calendars":
                self.calls.append((name, arguments))
                return SimpleNamespace(structuredContent={"ok": True, "calendars": [
                    {"id": "cal_" + "a" * 24, "name": "Личный"}]}, isError=False, content=[])
            return await super().call_tool(name, arguments)

    first_call = tool_call("list_upcoming_events", '{ "days" : 3 }')
    first_call.id = "read-42"
    second_call = tool_call("create_event", json.dumps(arguments))
    second_call.id = "proposal-99"
    _, fake, session, client = bundle(tmp_path, [completion(tool_calls=[first_call]), completion(tool_calls=[second_call])], ProposalSession())
    response = client.post("/api/ask", json={"prompt": "Создай встречу завтра по Омску с 15 до 16, проверь расписание"})
    assert response.status_code == 200
    assert fake.calls[1]["tools"] == fake.calls[0]["tools"]
    assert fake.calls[1]["messages"][-2]["tool_calls"][0]["id"] == "read-42"
    assert fake.calls[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"] == '{ "days" : 3 }'
    assert fake.calls[1]["messages"][-1]["tool_call_id"] == "read-42"
    assert [item["name"] for item in response.json()["mcp_trace"]["steps"]] == ["list_upcoming_events", "create_event"]
    assert [name for name, _ in session.calls] == ["list_upcoming_events", "list_calendars"]


def test_repeated_read_is_stopped_before_second_mcp_call(tmp_path):
    store, fake, session, client = bundle(tmp_path, [completion(tool_calls=[tool_call()]), completion(tool_calls=[tool_call()])])
    response = ask(client)
    assert response.status_code == 502
    assert len(fake.calls) == 2
    assert session.calls == [("list_upcoming_events", {"days": 3})]
    assert store.session_messages(store.active_session()["id"]) == []


def test_explicit_omsk_does_not_accept_timezone_clarification(tmp_path):
    store, _, session, client = bundle(tmp_path, [completion(tool_calls=[tool_call("clarify_event", '{"question": "Какой часовой пояс?"}')])])
    response = client.post("/api/ask", json={"prompt": "Создай встречу завтра с 15 до 17 по Омску"})
    assert response.status_code == 502
    assert session.calls == []
    assert store.session_messages(store.active_session()["id"]) == []


def test_explicit_omsk_rejects_wrong_proposed_offset(tmp_path):
    start = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0)
    arguments = {"title": "Демо", "start": start.isoformat(),
                 "end": (start + timedelta(hours=1)).isoformat()}
    store, _, session, client = bundle(tmp_path, [completion(tool_calls=[tool_call("create_event", json.dumps(arguments))])])
    response = client.post("/api/ask", json={"prompt": "Создай встречу завтра по Омску с 15 до 16"})
    assert response.status_code == 400
    assert session.calls == []
    assert store.session_messages(store.active_session()["id"]) == []


def test_conflicting_time_can_be_clarified_without_write(tmp_path):
    store, fake, session, client = bundle(tmp_path, [completion(tool_calls=[tool_call("clarify_event", '{"question": "Что важнее: два часа или интервал 13:00–14:00?"}')])])
    response = client.post("/api/ask", json={"prompt": "Создай встречу на два часа с 13 до 14 по Омску"})
    assert response.status_code == 200
    assert "Что важнее" in response.json()["answer"]
    assert response.json()["mcp_trace"]["steps"] == [{"name": "clarify_event", "status": "clarification"}]
    assert session.calls == []
    assert store.pending_action(store.active_session()["id"]) is None


def test_text_confirmation_without_pending_action_never_writes(tmp_path):
    _, fake, session, client = bundle(tmp_path, [completion("Что именно подтвердить?")])
    response = client.post("/api/ask", json={"prompt": "Все верно"})
    assert response.status_code == 200
    assert fake.calls[0]["tool_choice"] == "auto"
    assert session.calls == []


def test_distinct_reads_stop_at_model_step_limit(tmp_path):
    reads = [completion(tool_calls=[tool_call(arguments=json.dumps({"days": days}))]) for days in (1, 2, 3)]
    store, fake, session, client = bundle(tmp_path, reads)
    response = ask(client)
    assert response.status_code == 502
    assert len(fake.calls) == 3
    assert [arguments for name, arguments in session.calls if name == "list_upcoming_events"] == [{"days": 1}, {"days": 2}]
    assert store.session_messages(store.active_session()["id"]) == []


def test_model_http_error_in_exception_group_logs_only_safe_metadata(tmp_path, caplog):
    private_detail = "PRIVATE-CREDENTIAL and https://private.example/calendar"

    class FakeHttpError(Exception):
        status_code = 429

    class ErrorCompletions:
        def create(self, **_):
            raise ExceptionGroup("PRIVATE outer", [FakeHttpError(private_detail)])

    store = SQLiteMemoryStore(tmp_path / "safe-http.sqlite3")
    model_client = SimpleNamespace(chat=SimpleNamespace(completions=ErrorCompletions()))
    agent = CalendarMCPAgent(store, client=model_client, mcp_session_factory=lambda: session_factory(FakeSession()))
    client = TestClient(create_app(store, agent))
    with caplog.at_level("WARNING", logger=calendar_agent.__name__):
        response = ask(client)
    assert response.status_code == 502
    assert response.json()["detail"]["provider_detail"] == "Этап model_first_response, HTTP 429"
    assert "exception_type=FakeHttpError http_status=429" in caplog.text
    assert private_detail not in caplog.text + response.text
    assert "PRIVATE outer" not in caplog.text + response.text


def test_mcp_context_exception_group_preserves_classified_model_error(tmp_path, caplog):
    private_detail = "PRIVATE-CREDENTIAL and https://private.example/calendar"

    class FakeHttpError(Exception):
        status_code = 429

    class ErrorCompletions:
        def create(self, **_):
            raise FakeHttpError(private_detail)

    @asynccontextmanager
    async def wrapping_session():
        try:
            yield FakeSession()
        except Exception as error:
            raise ExceptionGroup("PRIVATE task group", [ExceptionGroup("PRIVATE nested", [error])]) from error

    store = SQLiteMemoryStore(tmp_path / "wrapped-error.sqlite3")
    model_client = SimpleNamespace(chat=SimpleNamespace(completions=ErrorCompletions()))
    agent = CalendarMCPAgent(store, client=model_client, mcp_session_factory=wrapping_session)
    client = TestClient(create_app(store, agent))
    with caplog.at_level("WARNING", logger=calendar_agent.__name__):
        response = ask(client)
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["provider_detail"] == "Этап model_first_response, HTTP 429"
    assert caplog.text.count("Calendar model request failed") == 1
    assert "Calendar request failed" not in caplog.text
    assert private_detail not in caplog.text + response.text
    assert "PRIVATE task group" not in caplog.text + response.text
    assert "PRIVATE nested" not in caplog.text + response.text
    assert store.session_messages(store.active_session()["id"]) == []


def test_first_request_gets_fresh_trusted_time_without_persisting_it(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "time-context.sqlite3")
    fake = FakeCompletions([completion("Первый ответ."), completion("Второй ответ.")])
    client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    session = FakeSession()
    clock = iter([
        datetime(2026, 9, 24, 15, 30, tzinfo=timezone(timedelta(hours=6))),
        datetime(2026, 9, 25, 9, 0, tzinfo=timezone(timedelta(hours=6))),
    ])
    agent = CalendarMCPAgent(store, client=client,
                             mcp_session_factory=lambda: session_factory(session),
                             now_provider=lambda: next(clock))
    app = TestClient(create_app(store, agent))

    first = ask(app)
    second = ask(app)
    assert first.status_code == second.status_code == 200
    first_messages = fake.calls[0]["messages"]
    second_messages = fake.calls[1]["messages"]
    first_time = [item for item in first_messages if "Доверенный контекст времени" in item["content"]]
    second_time = [item for item in second_messages if "Доверенный контекст времени" in item["content"]]
    assert len(first_time) == len(second_time) == 1
    assert first_time[0]["role"] == second_time[0]["role"] == "system"
    assert first_messages[-1]["role"] == second_messages[-1]["role"] == "user"
    assert "2026-09-24T09:30:00+00:00" in first_time[0]["content"]
    assert "2026-09-24T15:30:00+06:00" in first_time[0]["content"]
    assert "2026-09-25T03:00:00+00:00" in second_time[0]["content"]
    assert "часовым поясом пользователя" in first_time[0]["content"]
    assert "после подтверждения" in first_time[0]["content"]
    assert "Доверенный контекст времени" not in json.dumps(first.json()["preview"]["messages"], ensure_ascii=False)
    assert "Доверенный контекст времени" not in json.dumps(second.json()["preview"]["messages"], ensure_ascii=False)


@pytest.mark.parametrize("requested", [tool_call("delete_event"), tool_call(arguments='{"days": 50}'), tool_call(arguments='{"days": "7"}'), tool_call(arguments='{"other": 1}'), tool_call(arguments="broken")])
def test_invalid_model_tool_call_does_not_call_calendar_or_persist(tmp_path, requested):
    store, _, session, client = bundle(tmp_path, [completion(tool_calls=[requested])])
    response = ask(client)
    assert response.status_code == 400
    assert session.calls == []
    assert store.session_messages(store.active_session()["id"]) == []


def test_calendar_error_does_not_persist_or_send_fake_result(tmp_path):
    session = FakeSession({"ok": False, "error": {"code": "not_configured", "message": "Укажите локальные переменные."}})
    store, fake, session, client = bundle(tmp_path, [completion(tool_calls=[tool_call()])], session)
    response = ask(client)
    assert response.status_code == 502
    assert len(fake.calls) == 1
    assert store.session_messages(store.active_session()["id"]) == []


@pytest.mark.parametrize("failure_stage,expected_stage,public_message", [
    ("mcp_connect", "mcp_connect", "MCP-серверу"),
    ("mcp_list_tools", "mcp_list_tools", "MCP-серверу"),
    ("mcp_read_events", "mcp_read_events", "MCP-серверу"),
    ("model_first_response", "model_first_response", "модели"),
    ("model_second_response", "model_second_response", "модели"),
])
def test_unexpected_failure_reports_safe_stage_without_leaking_details(
    tmp_path, caplog, failure_stage, expected_stage, public_message,
):
    private_detail = "PRIVATE-CREDENTIAL and https://private.example/calendar"

    class FailingSession(FakeSession):
        async def list_tools(self):
            if failure_stage == "mcp_list_tools":
                raise RuntimeError(private_detail)
            return await super().list_tools()

        async def call_tool(self, name, arguments):
            if failure_stage == "mcp_read_events":
                raise RuntimeError(private_detail)
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def failing_session():
        if failure_stage == "mcp_connect":
            raise RuntimeError(private_detail)
        yield FailingSession()

    class FailingCompletions(FakeCompletions):
        def create(self, **options):
            if failure_stage == "model_first_response" or (
                failure_stage == "model_second_response" and self.calls
            ):
                raise RuntimeError(private_detail)
            return super().create(**options)

    store = SQLiteMemoryStore(tmp_path / "safe-error.sqlite3")
    model = FailingCompletions([completion(tool_calls=[tool_call()])])
    model_client = SimpleNamespace(chat=SimpleNamespace(completions=model))
    agent = CalendarMCPAgent(store, client=model_client, mcp_session_factory=failing_session)
    client = TestClient(create_app(store, agent))

    with caplog.at_level("WARNING", logger=calendar_agent.__name__):
        response = ask(client)

    assert response.status_code == 502
    body = response.json()
    assert public_message in body["detail"]["message"]
    assert body["detail"]["category"] == "provider_request"
    assert f"stage={expected_stage}" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert private_detail not in caplog.text
    assert private_detail not in response.text
    assert store.session_messages(store.active_session()["id"]) == []


def test_read_upcoming_events_uses_only_minimal_fields(monkeypatch):
    spec = importlib.util.spec_from_file_location("day17_mcp_server", Path(__file__).resolve().parents[1] / "mcp_server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    monkeypatch.setenv("YANDEX_CALDAV_USERNAME", "demo@example.com")
    monkeypatch.setenv("YANDEX_CALDAV_APP_PASSWORD", "local-password")

    class Component:
        def get(self, key, default=None):
            return {"summary": "Demo", "description": "PRIVATE NOTES", "attendee": "private@example.com"}.get(key, default)

        def decoded(self, key):
            return {"dtstart": datetime(2026, 9, 24, 10, tzinfo=timezone.utc)}[key]

    class Calendar:
        def search(self, **kwargs):
            assert kwargs["event"] is True and kwargs["expand"] is True
            return [SimpleNamespace(icalendar_component=Component())]

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["url"] == server.CALDAV_URL
            assert kwargs["username"] == "demo@example.com"
            assert kwargs["password"] == "local-password"

        def get_calendars(self):
            return [Calendar()]

        def close(self):
            pass

    result = server.read_upcoming_events(3, client_factory=Client, now=datetime(2026, 9, 23, tzinfo=timezone.utc))
    assert result == {"ok": True, "days": 3, "events": [{"start": "2026-09-24T10:00:00+00:00", "end": None, "title": "Demo"}], "truncated": False}
    assert "PRIVATE" not in json.dumps(result)


def test_real_stdio_server_lists_and_calls_tool_without_credentials(monkeypatch):
    monkeypatch.delenv("YANDEX_CALDAV_USERNAME", raising=False)
    monkeypatch.delenv("YANDEX_CALDAV_APP_PASSWORD", raising=False)

    async def inspect():
        async with local_mcp_session() as session:
            tools = (await session.list_tools()).tools
            assert [tool.name for tool in tools] == ["list_upcoming_events", "list_calendars", "create_event"]
            assert "days" in tools[0].inputSchema["properties"]
            assert {"title", "start", "end", "calendar_id", "operation_uid"}.issubset(tools[2].inputSchema["properties"])
            result = await session.call_tool("list_upcoming_events", {"days": 7})
            assert result.structuredContent["ok"] is False
            assert result.structuredContent["error"]["code"] == "not_configured"

    asyncio.run(inspect())


@pytest.mark.parametrize("configured", [True, False])
def test_stdio_child_receives_only_nonempty_calendar_credentials(tmp_path, monkeypatch, configured):
    probe = tmp_path / "environment_probe.py"
    probe.write_text('''
import os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Environment probe")

@mcp.tool()
def check_environment() -> dict[str, bool]:
    return {
        "username_matches": os.getenv("YANDEX_CALDAV_USERNAME") == "test-calendar-user",
        "password_matches": os.getenv("YANDEX_CALDAV_APP_PASSWORD") == "test-calendar-password",
        "model_key_present": bool(os.getenv("DEEPSEEK_API_KEY")),
        "other_model_key_present": bool(os.getenv("OPENROUTER_API_KEY")),
    }

if __name__ == "__main__":
    mcp.run(transport="stdio")
''')
    monkeypatch.setattr(calendar_agent, "SERVER_PATH", probe)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-model-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-other-model-key")
    if configured:
        monkeypatch.setenv("YANDEX_CALDAV_USERNAME", "test-calendar-user")
        monkeypatch.setenv("YANDEX_CALDAV_APP_PASSWORD", "test-calendar-password")
    else:
        monkeypatch.setenv("YANDEX_CALDAV_USERNAME", "")
        monkeypatch.delenv("YANDEX_CALDAV_APP_PASSWORD", raising=False)

    async def inspect():
        async with local_mcp_session() as session:
            result = await session.call_tool("check_environment", {})
            assert result.structuredContent == {
                "username_matches": configured,
                "password_matches": configured,
                "model_key_present": False,
                "other_model_key_present": False,
            }

    asyncio.run(inspect())
