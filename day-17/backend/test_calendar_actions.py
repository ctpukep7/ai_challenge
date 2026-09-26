"""Offline write lifecycle tests: no real model or Yandex connection."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from .calendar_agent import CalendarMCPAgent
from .main import create_app
from .store import SQLiteMemoryStore
from .test_calendar_mcp import completion, session_factory, tool_call


CAL_ID = "cal_" + "a" * 24
OTHER_ID = "cal_" + "b" * 24


def event_arguments(**overrides):
    start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    values = {"title": "Демо-встреча", "start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat()}
    values.update(overrides)
    return values


class FakeModel:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeMCP:
    def __init__(self, calendars=None, write_result=None):
        self.calendars = calendars or [{"id": CAL_ID, "name": "Личный"}]
        self.write_result = write_result
        self.calls = []

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=name, description=name, inputSchema={"type": "object", "properties": {"days": {"type": "integer"}}})
                                      for name in ("list_upcoming_events", "list_calendars", "create_event")])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "list_calendars":
            data = {"ok": True, "calendars": self.calendars}
        elif name == "create_event":
            data = self.write_result or {"ok": True, "uid": arguments["operation_uid"],
                                          "title": arguments["title"], "start": arguments["start"],
                                          "end": arguments["end"], "calendar_id": arguments["calendar_id"],
                                          "calendar_name": "Личный"}
        else:
            data = {"ok": True, "events": [], "days": arguments["days"]}
        return SimpleNamespace(structuredContent=data, isError=False, content=[])


def bundle(tmp_path, arguments=None, *, calendars=None, model_tail=None, write_result=None):
    store = SQLiteMemoryStore(tmp_path / "actions.sqlite3")
    proposed = completion(tool_calls=[tool_call("create_event", json.dumps(arguments or event_arguments()))])
    model = FakeModel([proposed, *(model_tail if model_tail is not None else [completion("Я создал демо-встречу.")])])
    mcp = FakeMCP(calendars, write_result)
    client = SimpleNamespace(chat=SimpleNamespace(completions=model))
    agent = CalendarMCPAgent(store, client=client, mcp_session_factory=lambda: session_factory(mcp))
    return store, model, mcp, TestClient(create_app(store, agent))


def propose(client):
    return client.post("/api/ask", json={"prompt": "Создай демо-встречу", "controls": {"compression_mode": "full_session"}})


def confirm(client, action, calendar_id=CAL_ID):
    return client.post(f"/api/calendar-actions/{action['id']}/confirm", json={"calendar_id": calendar_id})


def test_proposal_requires_confirmation_and_never_exposes_private_fields(tmp_path):
    _, model, mcp, client = bundle(tmp_path)
    response = propose(client)
    assert response.status_code == 200
    body = response.json()
    action = body["pending_action"]
    assert action["status"] == "pending"
    assert body["state"]["pending_action"]["id"] == action["id"]
    assert [name for name, _ in mcp.calls] == ["list_calendars"]
    create_schema = model.calls[0]["tools"][1]["function"]["parameters"]
    assert set(create_schema["properties"]) == {"title", "start", "end"}
    assert "operation_uid" not in json.dumps(body)
    assert "context_json" not in json.dumps(body)


def test_confirm_writes_once_and_returns_model_answer(tmp_path):
    store, model, mcp, client = bundle(tmp_path)
    action = propose(client).json()["pending_action"]
    first = confirm(client, action)
    assert first.status_code == 200
    assert first.json()["action"]["status"] == "created"
    assert first.json()["answer"] == "Я создал демо-встречу."
    assert first.json()["mcp_trace"]["called_tool"]["status"] == "created"
    assert model.calls[1]["messages"][-1]["role"] == "tool"
    assert json.loads(model.calls[1]["messages"][-1]["content"])["ok"] is True
    proposal_message = SQLiteMemoryStore(store.database_path).state()["active_session"]["messages"][-2]
    assert proposal_message["preview"]["mcp_trace"]["called_tool"]["status"] == "created"
    again = confirm(client, action)
    assert again.status_code == 200
    assert [name for name, _ in mcp.calls].count("create_event") == 1
    assert len(model.calls) == 2


def test_cancel_never_writes(tmp_path):
    store, _, mcp, client = bundle(tmp_path)
    action = propose(client).json()["pending_action"]
    cancelled = client.post(f"/api/calendar-actions/{action['id']}/cancel", json={})
    assert cancelled.status_code == 200
    assert cancelled.json()["action"]["status"] == "cancelled"
    assert cancelled.json()["state"]["pending_action"] is None
    reopened = SQLiteMemoryStore(store.database_path)
    message = reopened.state()["active_session"]["messages"][-1]
    assert message["preview"]["mcp_trace"]["called_tool"]["status"] == "cancelled"
    assert "отменено" in message["content"]
    assert confirm(client, action).json()["action"]["status"] == "cancelled"
    assert [name for name, _ in mcp.calls] == ["list_calendars"]


def test_multiple_calendars_require_exact_choice(tmp_path):
    _, _, mcp, client = bundle(tmp_path, calendars=[{"id": CAL_ID, "name": "Личный"}, {"id": OTHER_ID, "name": "Работа"}])
    action = propose(client).json()["pending_action"]
    assert action["selected_calendar_id"] is None
    assert confirm(client, action, "cal_invalid").status_code == 400
    assert [name for name, _ in mcp.calls] == ["list_calendars"]
    assert confirm(client, action, OTHER_ID).status_code == 200
    assert mcp.calls[-1][1]["calendar_id"] == OTHER_ID


@pytest.mark.parametrize("change", [
    {"title": ""}, {"end": "2026-01-01T00:00:00+00:00"},
    {"start": "2026-01-01T00:00:00"}, {"calendar_id": CAL_ID},
])
def test_bad_model_proposal_rejected_without_write_or_history(tmp_path, change):
    store, _, mcp, client = bundle(tmp_path, event_arguments(**change))
    assert propose(client).status_code == 400
    assert mcp.calls == []
    assert store.session_messages(store.active_session()["id"]) == []


def test_profile_or_session_switch_blocks_confirmation(tmp_path):
    store, _, mcp, client = bundle(tmp_path)
    action = propose(client).json()["pending_action"]
    store.create_session()
    assert confirm(client, action).status_code == 400
    assert [name for name, _ in mcp.calls] == ["list_calendars"]


def test_unknown_write_outcome_is_not_retried(tmp_path):
    store, _, mcp, client = bundle(tmp_path, write_result={"ok": False, "error": {"code": "write_outcome_unknown"}})
    action = propose(client).json()["pending_action"]
    assert confirm(client, action).json()["action"]["status"] == "unknown"
    assert confirm(client, action).json()["action"]["status"] == "unknown"
    assert [name for name, _ in mcp.calls].count("create_event") == 1
    message = SQLiteMemoryStore(store.database_path).state()["active_session"]["messages"][-1]
    assert message["preview"]["mcp_trace"]["called_tool"]["status"] == "unknown"
    assert "Проверьте календарь" in message["content"]


def test_success_persists_when_second_model_call_fails(tmp_path):
    store, _, mcp, client = bundle(tmp_path, model_tail=[RuntimeError("model unavailable")])
    action = propose(client).json()["pending_action"]
    body = confirm(client, action).json()
    assert body["action"]["status"] == "created"
    assert "создано" in body["answer"]
    assert store.calendar_action(action["id"])["result"]["ok"] is True
    assert [name for name, _ in mcp.calls].count("create_event") == 1


def test_bad_mcp_success_shape_is_unknown_and_never_retried(tmp_path):
    _, model, mcp, client = bundle(tmp_path, write_result={
        "ok": True, "uid": "wrong-uid", "title": "Демо-встреча", "start": event_arguments()["start"],
        "end": event_arguments()["end"], "calendar_id": CAL_ID, "private": "do-not-expose",
    })
    action = propose(client).json()["pending_action"]
    body = confirm(client, action).json()
    assert body["action"]["status"] == "unknown"
    assert "do-not-expose" not in json.dumps(body)
    assert len(model.calls) == 1
    assert confirm(client, action).json()["action"]["status"] == "unknown"
    assert [name for name, _ in mcp.calls].count("create_event") == 1


def test_expired_proposal_cannot_write(tmp_path):
    store, _, mcp, client = bundle(tmp_path)
    action = propose(client).json()["pending_action"]
    with store.connection() as connection:
        connection.execute("UPDATE calendar_actions SET expires_at=0 WHERE id=?", (action["id"],))
    reloaded_state = SQLiteMemoryStore(store.database_path).state()
    assert reloaded_state["pending_action"] is None
    expired_message = reloaded_state["active_session"]["messages"][-1]
    assert expired_message["preview"]["mcp_trace"]["called_tool"]["status"] == "cancelled"
    assert expired_message["preview"]["mcp_trace"]["called_tool"]["reason"] == "expired"
    assert "истёк" in expired_message["content"]
    body = confirm(client, action).json()
    assert body["action"]["status"] == "cancelled"
    assert body["state"]["pending_action"] is None
    assert [name for name, _ in mcp.calls] == ["list_calendars"]


def test_proposal_survives_store_reopen(tmp_path):
    store, _, _, client = bundle(tmp_path)
    action = propose(client).json()["pending_action"]
    reopened = SQLiteMemoryStore(store.database_path)
    assert reopened.state()["pending_action"]["id"] == action["id"]


def test_retry_after_persisted_success_recovers_answer_without_second_write(tmp_path):
    store, _, mcp, client = bundle(tmp_path)
    action = propose(client).json()["pending_action"]
    with store.connection() as connection:
        connection.execute("UPDATE calendar_actions SET status='created',selected_calendar_id=?,result_json=? WHERE id=?",
                           (CAL_ID, json.dumps({"ok": True, "uid": "saved"}), action["id"]))
    response = confirm(client, action)
    assert response.status_code == 200
    assert response.json()["action"]["status"] == "created"
    assert "создано" in response.json()["answer"]
    assert [name for name, _ in mcp.calls] == ["list_calendars"]


def test_restart_normalizes_orphaned_write_to_unknown_without_retry(tmp_path):
    store, _, mcp, client = bundle(tmp_path)
    action = propose(client).json()["pending_action"]
    with store.connection() as connection:
        connection.execute("UPDATE calendar_actions SET status='executing',selected_calendar_id=? WHERE id=?",
                           (CAL_ID, action["id"]))
    restarted = SQLiteMemoryStore(store.database_path)
    saved = restarted.calendar_action(action["id"])
    assert saved["status"] == "unknown"
    message = restarted.state()["active_session"]["messages"][-1]
    assert message["preview"]["mcp_trace"]["called_tool"]["status"] == "unknown"
    assert "Проверьте календарь" in message["content"]
    retry = confirm(client, action)
    assert retry.json()["action"]["status"] == "unknown"
    assert [name for name, _ in mcp.calls] == ["list_calendars"]


def test_new_store_does_not_normalize_same_process_in_flight_action(tmp_path):
    store, _, _, client = bundle(tmp_path)
    action = propose(client).json()["pending_action"]
    _, claimed = store.claim_calendar_action(action["id"], CAL_ID)
    assert claimed is True
    reopened_while_running = SQLiteMemoryStore(store.database_path)
    assert reopened_while_running.calendar_action_private(action["id"])["status"] == "executing"
    store.finish_calendar_action(action["id"], status="unknown")
