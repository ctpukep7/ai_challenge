from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from .calendar_agent import local_mcp_session


def load_server():
    spec = importlib.util.spec_from_file_location("day17_mcp_server_write", Path(__file__).resolve().parents[1] / "mcp_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
START = "2030-01-02T10:00:00+06:00"
END = "2030-01-02T11:00:00+06:00"


class FakeCalendar:
    canonical_url = "https://caldav.yandex.ru/example/calendar/"

    def __init__(self):
        self.writes = []

    def get_display_name(self):
        return "Личный"

    def add_event(self, **kwargs):
        self.writes.append(kwargs)
        return SimpleNamespace()


class FakeClient:
    calendar = FakeCalendar()
    closed = False

    def __init__(self, **kwargs):
        assert kwargs["url"] == "https://caldav.yandex.ru"
        assert kwargs["username"] == "demo@example.com"
        assert kwargs["password"] == "fake-password"

    def get_calendars(self):
        return [self.calendar]

    def close(self):
        type(self).closed = True


@pytest.fixture
def setup_server(monkeypatch):
    monkeypatch.setenv("YANDEX_CALDAV_USERNAME", "demo@example.com")
    monkeypatch.setenv("YANDEX_CALDAV_APP_PASSWORD", "fake-password")
    FakeClient.calendar = FakeCalendar()
    FakeClient.closed = False
    return load_server()


def test_calendar_discovery_returns_opaque_id_and_name(setup_server):
    result = setup_server.read_calendars(client_factory=FakeClient)
    assert result == {"ok": True, "calendars": [{"id": setup_server._calendar_id(FakeClient.calendar), "name": "Личный"}]}
    assert result["calendars"][0]["id"].startswith("cal_")
    assert "https://" not in json.dumps(result)
    assert FakeClient.closed


def test_create_uses_selected_discovered_calendar_and_stable_uid(setup_server):
    calendar_id = setup_server._calendar_id(FakeClient.calendar)
    uid = str(uuid4())
    result = setup_server.write_event(" Встреча ", START, END, calendar_id, uid, client_factory=FakeClient, now=NOW)
    assert result == {
        "ok": True, "uid": uid, "title": "Встреча", "start": START, "end": END,
        "calendar_id": calendar_id, "calendar_name": "Личный",
    }
    assert FakeClient.calendar.writes == [{
        "dtstart": datetime.fromisoformat(START), "dtend": datetime.fromisoformat(END),
        "summary": "Встреча", "uid": uid, "no_overwrite": True,
    }]


@pytest.mark.parametrize("changes", [
    {"title": ""}, {"title": "x" * 121}, {"title": "bad\nname"},
    {"start": "2030-01-02T10:00:00"}, {"start": "2029-01-02T10:00:00+06:00"},
    {"end": "2030-01-02T09:00:00+06:00"}, {"end": "2030-01-04T11:00:00+06:00"},
    {"calendar_id": "https://caldav.yandex.ru/evil"}, {"operation_uid": "model-provided-not-uuid"},
])
def test_invalid_input_never_writes(setup_server, changes):
    args = {"title": "Встреча", "start": START, "end": END,
            "calendar_id": setup_server._calendar_id(FakeClient.calendar), "operation_uid": str(uuid4())}
    args.update(changes)
    result = setup_server.write_event(**args, client_factory=FakeClient, now=NOW)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_event"
    assert FakeClient.calendar.writes == []


def test_unknown_calendar_never_writes(setup_server):
    result = setup_server.write_event("Встреча", START, END, "cal_" + "0" * 24, str(uuid4()), client_factory=FakeClient, now=NOW)
    assert result["error"]["code"] == "calendar_not_found"
    assert FakeClient.calendar.writes == []


def test_write_failure_has_safe_unknown_outcome(setup_server):
    class FailingCalendar(FakeCalendar):
        def add_event(self, **kwargs):
            raise RuntimeError("SECRET private URL and password")

    FakeClient.calendar = FailingCalendar()
    result = setup_server.write_event("Встреча", START, END, setup_server._calendar_id(FakeClient.calendar), str(uuid4()), client_factory=FakeClient, now=NOW)
    assert result["error"]["code"] == "write_outcome_unknown"
    assert "SECRET" not in json.dumps(result)


def test_stdio_lists_tools_and_schema_without_real_calendar_access(monkeypatch):
    monkeypatch.delenv("YANDEX_CALDAV_USERNAME", raising=False)
    monkeypatch.delenv("YANDEX_CALDAV_APP_PASSWORD", raising=False)

    async def inspect():
        async with local_mcp_session() as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert set(tools) == {"list_upcoming_events", "list_calendars", "create_event"}
            assert tools["list_upcoming_events"].annotations.readOnlyHint is True
            assert tools["list_calendars"].annotations.readOnlyHint is True
            assert tools["create_event"].annotations.readOnlyHint is False
            assert tools["create_event"].annotations.destructiveHint is False
            assert tools["create_event"].annotations.idempotentHint is False
            assert set(tools["create_event"].inputSchema["required"]) == {
                "title", "start", "end", "calendar_id", "operation_uid"
            }

    asyncio.run(inspect())
