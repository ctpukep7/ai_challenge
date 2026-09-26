"""Calendar safety contracts; use synthetic ICS and isolated databases only."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.providers import busy_intervals, instant


ZONE = "Asia/Omsk"
START = "2026-10-01T10:00:00+06:00"
END = "2026-10-01T13:00:00+06:00"


def ics(*events):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(
        "BEGIN:VEVENT\r\nUID:synthetic-%s\r\n%s\r\nEND:VEVENT\r\n" % (i, fields)
        for i, fields in enumerate(events)
    ) + "END:VCALENDAR\r\n"


def test_all_day_without_end_blocks_the_complete_local_date():
    document = ics("DTSTART;VALUE=DATE:20261001")
    intervals = busy_intervals([document], START, END, ZONE)
    assert intervals == [(instant(START), instant(END))]


@pytest.mark.parametrize("extra", ["TRANSP:TRANSPARENT", "STATUS:CANCELLED"])
def test_nonblocking_events_do_not_consume_free_time(extra):
    document = ics("DTSTART:20261001T040000Z\r\nDTEND:20261001T070000Z\r\n" + extra)
    assert busy_intervals([document], START, END, ZONE) == []


def test_utc_omsk_overlap_and_exclusive_end_boundaries():
    document = ics(
        "DTSTART:20261001T030000Z\r\nDTEND:20261001T040000Z",
        "DTSTART:20261001T050000Z\r\nDTEND:20261001T060000Z",
        "DTSTART:20261001T070000Z\r\nDTEND:20261001T080000Z",
    )
    assert busy_intervals([document], START, END, ZONE) == [
        (instant("2026-10-01T11:00:00+06:00"), instant("2026-10-01T12:00:00+06:00"))
    ]


def test_expanded_recurring_occurrences_keep_distinct_busy_windows():
    document = ics(
        "DTSTART:20261001T040000Z\r\nDTEND:20261001T043000Z\r\nRECURRENCE-ID:20261001T040000Z",
        "DTSTART:20261001T060000Z\r\nDTEND:20261001T063000Z\r\nRECURRENCE-ID:20261001T060000Z",
    )
    assert busy_intervals([document], START, END, ZONE) == [
        (instant(START), instant("2026-10-01T10:30:00+06:00")),
        (instant("2026-10-01T12:00:00+06:00"), instant("2026-10-01T12:30:00+06:00")),
    ]


@pytest.mark.parametrize("rule", ["RRULE:FREQ=DAILY;COUNT=3", "RDATE:20261002T040000Z", "EXDATE:20261002T040000Z"])
def test_unexpanded_recurrence_is_unknown_availability_not_free(rule):
    document = ics("DTSTART:20261001T040000Z\r\nDTEND:20261001T050000Z\r\n" + rule)
    with pytest.raises(ValueError):
        busy_intervals([document], START, END, ZONE)


def test_floating_event_time_uses_the_requested_calendar_zone():
    document = ics("DTSTART:20261001T110000\r\nDURATION:PT1H")
    assert busy_intervals([document], START, END, ZONE) == [
        (instant("2026-10-01T11:00:00+06:00"), instant("2026-10-01T12:00:00+06:00"))
    ]


def test_zero_duration_timed_event_does_not_reserve_an_interval():
    document = ics("DTSTART:20261001T050000Z")
    assert busy_intervals([document], START, END, ZONE) == []


class FakeCalendar:
    """Observable external boundary, including a write accepted before timeout."""
    def __init__(self):
        self.events = {}
        self.writes = []
        self.lookups = []
        self.busy_calls = []
        self.intervals = []
        self.lose_response = False
        self.lookup_failure = None

    def calendars(self):
        return [{"id": "calendar-test", "name": "Тестовый календарь"}]

    def busy(self, calendar_id, start, end, timezone):
        self.busy_calls.append((calendar_id, start, end, timezone))
        return list(self.intervals)

    def lookup(self, calendar_id, uid):
        self.lookups.append((calendar_id, uid))
        if self.lookup_failure:
            raise self.lookup_failure
        return self.events.get(uid)

    def create(self, plan, uid):
        self.writes.append((plan.copy(), uid))
        self.events[uid] = {key: plan[key] for key in ("title", "start", "end")}
        self.events[uid]["uid"] = uid
        if self.lose_response:
            raise TimeoutError("Synthetic accepted-write response lost")


def test_caldav_requests_server_side_recurrence_expansion(monkeypatch):
    from backend.providers import CalendarProvider
    from types import SimpleNamespace
    calls = []
    class RemoteCalendar:
        url = "calendar-test"
        def search(self, **kwargs):
            calls.append(kwargs)
            return [SimpleNamespace(data=ics("DTSTART:20261001T050000Z\r\nDTEND:20261001T060000Z"))]
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def principal(self): return SimpleNamespace(get_calendars=lambda: [RemoteCalendar()])
    provider = CalendarProvider("synthetic", "synthetic", "https://invalid.example")
    monkeypatch.setattr(provider, "_client", lambda: Client())
    from hashlib import sha256
    result = provider.busy(sha256(b"calendar-test").hexdigest()[:24], START, END, ZONE)
    assert result == [(instant("2026-10-01T11:00:00+06:00"), instant("2026-10-01T12:00:00+06:00"))]
    assert calls == [{"start": instant(START), "end": instant(END), "event": True, "expand": True}]


def test_uid_lookup_does_not_misclassify_transport_error_as_absence(monkeypatch):
    from backend.providers import CalendarProvider
    from types import SimpleNamespace
    class RemoteCalendar:
        url = "calendar-test"
        def get_event_by_uid(self, uid):
            raise TimeoutError("Synthetic lookup network failure")
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def principal(self): return SimpleNamespace(get_calendars=lambda: [RemoteCalendar()])
    provider = CalendarProvider("synthetic", "synthetic", "https://invalid.example")
    monkeypatch.setattr(provider, "_client", lambda: Client())
    from hashlib import sha256
    with pytest.raises(TimeoutError):
        provider.lookup(sha256(b"calendar-test").hexdigest()[:24], "synthetic-uid")


@pytest.fixture
def trip(tmp_path, monkeypatch):
    from backend.store import Store
    from backend.services import Service
    import backend.services as services
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 9, 26, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(services, "datetime", FixedDateTime)
    class Weather:
        def resolve(self, name):
            return [{"name": "Омск", "latitude": 55, "longitude": 73, "timezone": ZONE}]
        def forecast(self, city, timezone):
            return {
                "available_start": START, "available_end": END, "timezone": ZONE,
                "hours": [
                    {"time": (instant(START) + timedelta(hours=i)).isoformat(),
                     "temperature_c": 9, "precipitation_probability": 20, "wind_ms": 3}
                    for i in range(3)
                ],
            }
    store = Store(tmp_path / "calendar-contract.sqlite")
    run, _ = store.new_run("request-calendar", "calendar-fingerprint", "Тестовая поездка", "calendar-test")
    calendar = FakeCalendar()
    service = Service(store, weather=Weather(), calendar=calendar, timezone=ZONE)
    city = service.resolve_city("Омск", run["id"])
    forecast = service.get_hourly_forecast(city["city_id"], 0, START, END, run["id"])
    calendars = service.list_calendars(run["id"])
    availability = service.find_free_slots(calendars["calendars_id"], "calendar-test", START, END, 3, "10:00", "18:00", run["id"])
    plan = service.save_trip_draft(forecast["forecast_id"], availability["availability_id"], START, END,
                                   "Тестовая поездка", "Синтетические источники", 5, 30, 8, run["id"])
    return store, service, calendar, plan


def approve(store, plan):
    """Seed the host-owned persisted consent; no model tool can manufacture it."""
    from backend.services import payload_hash
    approved = {**plan, "status": "approved"}
    store.put("plan", approved)
    store.put("operation", {"id": plan["id"], "plan_id": plan["id"], "uid": plan["uid"], "status": "approved",
                            "approved_at": "2026-09-26T00:00:00Z", "payload_hash": payload_hash(approved)})


def test_no_calendar_write_or_lookup_before_persisted_ui_approval(trip):
    store, service, calendar, plan = trip
    with pytest.raises(ValueError):
        service.create_trip_event(plan["id"])
    assert calendar.writes == [] and calendar.lookups == []
    assert store.get("plan", plan["id"])["status"] == "draft"


def test_rejected_plan_cannot_be_written(trip):
    store, service, calendar, plan = trip
    store.put("plan", {**plan, "status": "rejected"})
    with pytest.raises(ValueError):
        service.create_trip_event(plan["id"])
    assert calendar.writes == []


def test_double_confirmation_keeps_same_uid_and_one_write(trip):
    store, service, calendar, plan = trip
    approve(store, plan)
    first = service.create_trip_event(plan["id"])
    second = service.create_trip_event(plan["id"])
    assert first["status"] == second["status"] == "created"
    assert len(calendar.writes) == 1
    assert calendar.writes[0][1] == plan["uid"]
    written = calendar.writes[0][0]
    assert {k: written[k] for k in ("calendar_id", "title", "start", "end", "timezone")} == {
        k: plan[k] for k in ("calendar_id", "title", "start", "end", "timezone")
    }


def test_busy_calendar_recheck_revokes_stale_offer(trip):
    store, service, calendar, plan = trip
    approve(store, plan)
    calendar.intervals = [(instant(START) + timedelta(hours=1), instant(END))]
    result = service.create_trip_event(plan["id"])
    assert result["status"] == "conflict"
    assert calendar.writes == []
    with pytest.raises(ValueError):
        service.create_trip_event(plan["id"])
    assert store.get("plan", plan["id"])["status"] == "conflict"


def test_changed_fields_invalidate_previously_approved_payload(trip):
    store, service, calendar, plan = trip
    approve(store, plan)
    current = store.get("plan", plan["id"])
    current["title"] = "Изменённое название"
    store.put("plan", current)
    with pytest.raises(ValueError):
        service.create_trip_event(plan["id"])
    assert calendar.writes == []


@pytest.mark.parametrize("changed_document", ["operation", "plan"])
def test_uid_tampering_invalidates_persisted_confirmation_before_network(trip, changed_document):
    store, service, calendar, plan = trip
    approve(store, plan)
    document = store.get(changed_document, plan["id"])
    document["uid"] = "another-operation@synthetic.local"
    store.put(changed_document, document)
    with pytest.raises(ValueError):
        service.create_trip_event(plan["id"])
    assert calendar.lookups == [] and calendar.writes == []


def test_lost_write_response_is_reconciled_using_uid_without_second_write(trip):
    store, service, calendar, plan = trip
    approve(store, plan)
    calendar.lose_response = True
    result = service.create_trip_event(plan["id"])
    assert result["status"] == "created"
    assert len(calendar.writes) == 1
    assert len(calendar.lookups) >= 2
    assert {uid for _, uid in calendar.lookups} == {plan["uid"]}
    assert service.create_trip_event(plan["id"])["status"] == "created"
    assert len(calendar.writes) == 1


def test_uncertain_lookup_failure_never_triggers_a_blind_write(trip):
    store, service, calendar, plan = trip
    approve(store, plan)
    calendar.lookup_failure = TimeoutError("Synthetic private transport detail")
    first = service.create_trip_event(plan["id"])
    second = service.create_trip_event(plan["id"])
    assert first["status"] == second["status"] == "uncertain"
    assert calendar.writes == []
    assert "private transport" not in (second.get("error") or "")
    calendar.lookup_failure = None
    assert service.create_trip_event(plan["id"])["status"] == "uncertain"
    assert calendar.writes == []


def test_existing_uid_with_different_payload_requires_manual_reconciliation(trip):
    store, service, calendar, plan = trip
    approve(store, plan)
    calendar.events[plan["uid"]] = {"uid": plan["uid"], "title": "Другая запись", "start": START, "end": END}
    assert service.create_trip_event(plan["id"])["status"] == "uncertain"
    assert calendar.writes == []


def test_concurrent_http_confirm_and_reject_preserve_approved_operation(trip, monkeypatch):
    import asyncio
    import httpx
    from backend.config import Settings
    store, service, calendar, plan = trip
    settings = Settings(db_path=store.path, api_key="synthetic")
    # Prevent the module-level application from loading local .env/default data.
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    from backend.app import create_app
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        class ApprovalRegistry:
            async def call(self, alias, arguments, run_id=None, approval=False):
                assert alias == "calendar__create_trip_event" and approval
                assert store.get("operation", plan["id"])["approved_at"]
                started.set()
                await release.wait()
                return service.create_trip_event(arguments["plan_id"])
        app = create_app(settings=settings, model=object(), registry=ApprovalRegistry())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://contract") as client:
            confirm = asyncio.create_task(client.post(f"/api/plans/{plan['id']}/confirm"))
            await asyncio.wait_for(started.wait(), timeout=2)
            reject = asyncio.create_task(client.post(f"/api/plans/{plan['id']}/reject"))
            await asyncio.sleep(0)
            assert not reject.done()
            release.set()
            confirmed, rejected = await asyncio.wait_for(asyncio.gather(confirm, reject), timeout=2)
            assert confirmed.status_code == 200 and confirmed.json()["status"] == "created"
            assert rejected.status_code == 409
    asyncio.run(scenario())
    assert len(calendar.writes) == 1
    assert store.get("plan", plan["id"])["status"] == "created"


def test_http_rejection_before_confirmation_never_dispatches_write(trip, monkeypatch):
    import asyncio
    import httpx
    from backend.config import Settings
    store, service, calendar, plan = trip
    settings = Settings(db_path=store.path, api_key="synthetic")
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    from backend.app import create_app
    class NoWriteRegistry:
        async def call(self, *args, **kwargs):
            pytest.fail("Rejected project must never reach the MCP write tool")
    async def scenario():
        app = create_app(settings=settings, model=object(), registry=NoWriteRegistry())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://contract") as client:
            assert (await client.post(f"/api/plans/{plan['id']}/reject")).status_code == 200
            assert (await client.post(f"/api/plans/{plan['id']}/confirm")).status_code == 409
    asyncio.run(scenario())
    assert calendar.writes == []
    assert store.get("plan", plan["id"])["status"] == "rejected"


def test_concurrent_http_confirmations_dispatch_one_calendar_write(trip, monkeypatch):
    import asyncio
    import httpx
    from backend.config import Settings
    store, service, calendar, plan = trip
    settings = Settings(db_path=store.path, api_key="synthetic")
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    from backend.app import create_app
    class ApprovalRegistry:
        calls = 0
        async def call(self, alias, arguments, run_id=None, approval=False):
            self.calls += 1
            assert approval
            await asyncio.sleep(0)
            return service.create_trip_event(arguments["plan_id"])
    registry = ApprovalRegistry()
    async def scenario():
        app = create_app(settings=settings, model=object(), registry=registry)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://contract") as client:
            replies = await asyncio.gather(*[client.post(f"/api/plans/{plan['id']}/confirm") for _ in range(2)])
            assert all(r.status_code == 200 and r.json()["status"] == "created" for r in replies)
    asyncio.run(scenario())
    assert registry.calls == 1 and len(calendar.writes) == 1


def test_cancelled_confirmation_preserves_uncertainty_and_terminal_trace(trip, monkeypatch):
    import asyncio
    import httpx
    from backend.config import Settings
    store, service, calendar, plan = trip
    settings = Settings(db_path=store.path, api_key="synthetic")
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    from backend.app import create_app
    async def scenario():
        started = asyncio.Event()
        class CancelledRegistry:
            async def call(self, alias, arguments, run_id=None, approval=False):
                assert approval
                # Simulate an externally accepted write whose result never arrives.
                current = store.get("plan", plan["id"])
                calendar.create(current, current["uid"])
                started.set()
                await asyncio.Event().wait()
        app = create_app(settings=settings, model=object(), registry=CancelledRegistry())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://contract") as client:
            request = asyncio.create_task(client.post(f"/api/plans/{plan['id']}/confirm"))
            await asyncio.wait_for(started.wait(), timeout=2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
    asyncio.run(scenario())
    assert store.get("plan", plan["id"])["status"] == "uncertain"
    assert store.get("operation", plan["id"])["status"] == "uncertain"
    traces = store.get("run", plan["run_id"])["trace"]
    assert len(traces) == 1
    assert traces[0]["alias"] == "calendar__create_trip_event"
    assert traces[0]["status"] == "error" and traces[0]["error"]
    assert len(calendar.writes) == 1
    # The next attempt reconciles the stable UID instead of writing again.
    assert service.create_trip_event(plan["id"])["status"] == "created"
    assert len(calendar.writes) == 1
