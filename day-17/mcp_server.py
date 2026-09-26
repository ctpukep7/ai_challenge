"""Personal Yandex Calendar MCP server with explicit, validated event creation."""

from __future__ import annotations

import os
import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import caldav
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


CALDAV_URL = "https://caldav.yandex.ru"
MAX_EVENTS = 50
MAX_EVENT_HOURS = 24
mcp = FastMCP("Day 17 Calendar", log_level="WARNING")


def _credentials() -> tuple[str, str] | None:
    username = os.getenv("YANDEX_CALDAV_USERNAME")
    password = os.getenv("YANDEX_CALDAV_APP_PASSWORD")
    return (username, password) if username and password else None


def _not_configured() -> dict[str, Any]:
    return {"ok": False, "error": {"code": "not_configured", "message": "Укажите YANDEX_CALDAV_USERNAME и YANDEX_CALDAV_APP_PASSWORD в локальном .env."}}


def _calendar_id(calendar: Any) -> str:
    # URLs are never returned to, or accepted from, the model/application.
    url = str(getattr(calendar, "canonical_url", None) or calendar.url)
    return "cal_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def _calendar_name(calendar: Any) -> str:
    name = calendar.get_display_name()
    return str(name).strip()[:120] if name else "Календарь"


def _safe_error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _parse_event_datetime(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError(f"{field} должно быть датой и временем ISO 8601 с часовым поясом.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} должно содержать часовой пояс.")
    return parsed


def validate_event_input(
    title: str,
    start: str,
    end: str,
    calendar_id: str,
    operation_uid: str,
    *,
    now: datetime | None = None,
) -> tuple[str, datetime, datetime]:
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120 or any(ord(char) < 32 for char in title):
        raise ValueError("Название должно содержать от 1 до 120 символов без управляющих символов.")
    event_start = _parse_event_datetime(start, "start")
    event_end = _parse_event_datetime(end, "end")
    if event_start <= (now or datetime.now(timezone.utc)):
        raise ValueError("Начало события должно быть в будущем.")
    if event_end <= event_start:
        raise ValueError("Конец события должен быть позже начала.")
    if event_end - event_start > timedelta(hours=MAX_EVENT_HOURS):
        raise ValueError("Событие не может длиться более 24 часов.")
    if not isinstance(calendar_id, str) or not re.fullmatch(r"cal_[a-f0-9]{24}", calendar_id):
        raise ValueError("Выберите календарь из списка.")
    try:
        if not isinstance(operation_uid, str) or str(UUID(operation_uid)) != operation_uid:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("Некорректный идентификатор операции.") from None
    return title.strip(), event_start, event_end


def read_calendars(*, client_factory=caldav.DAVClient) -> dict[str, Any]:
    credentials = _credentials()
    if credentials is None:
        return _not_configured()
    client = None
    try:
        client = client_factory(url=CALDAV_URL, username=credentials[0], password=credentials[1], timeout=12)
        calendars = [{"id": _calendar_id(calendar), "name": _calendar_name(calendar)} for calendar in client.get_calendars()]
        return {"ok": True, "calendars": calendars}
    except Exception:
        return _safe_error("calendar_unavailable", "Не удалось получить список календарей. Проверьте пароль приложения и соединение.")
    finally:
        if client is not None:
            client.close()


def write_event(
    title: str,
    start: str,
    end: str,
    calendar_id: str,
    operation_uid: str,
    *,
    client_factory=caldav.DAVClient,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        title, event_start, event_end = validate_event_input(title, start, end, calendar_id, operation_uid, now=now)
    except ValueError as exc:
        return _safe_error("invalid_event", str(exc))
    credentials = _credentials()
    if credentials is None:
        return _not_configured()
    client = None
    try:
        client = client_factory(url=CALDAV_URL, username=credentials[0], password=credentials[1], timeout=12)
        matches = [calendar for calendar in client.get_calendars() if _calendar_id(calendar) == calendar_id]
        if len(matches) != 1:
            return _safe_error("calendar_not_found", "Выбранный календарь не найден; обновите список календарей.")
        calendar = matches[0]
        calendar.add_event(
            dtstart=event_start,
            dtend=event_end,
            summary=title,
            uid=operation_uid,
            no_overwrite=True,
        )
        return {
            "ok": True,
            "uid": operation_uid,
            "title": title,
            "start": event_start.isoformat(),
            "end": event_end.isoformat(),
            "calendar_id": calendar_id,
            "calendar_name": _calendar_name(calendar),
        }
    except Exception:
        # The caller treats this as an unknown outcome; a network error can
        # happen after the remote server has already stored the event.
        return _safe_error("write_outcome_unknown", "Не удалось подтвердить создание события. Проверьте календарь перед повторной попыткой.")
    finally:
        if client is not None:
            client.close()


def validate_days(days: int) -> int:
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 14:
        raise ValueError("days должно быть целым числом от 1 до 14.")
    return days


def _event_value(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _event_sort_key(event: dict[str, Any]) -> str:
    return event["start"] or ""


def read_upcoming_events(
    days: int = 7,
    *,
    client_factory=caldav.DAVClient,
    now: datetime | None = None,
) -> dict[str, Any]:
    days = validate_days(days)
    credentials = _credentials()
    if credentials is None:
        return _not_configured()
    start = now or datetime.now(timezone.utc)
    end = start + timedelta(days=days)
    client = None
    try:
        client = client_factory(url=CALDAV_URL, username=credentials[0], password=credentials[1], timeout=12)
        calendars = client.get_calendars()
        events = []
        for calendar in calendars:
            for item in calendar.search(event=True, start=start, end=end, expand=True):
                component = item.icalendar_component
                if str(component.get("status", "")).upper() == "CANCELLED":
                    continue
                event_start = component.decoded("dtstart")
                event_end = component.decoded("dtend") if component.get("dtend") else None
                events.append({
                    "start": _event_value(event_start),
                    "end": _event_value(event_end),
                    "title": str(component.get("summary", "Без названия"))[:120],
                })
        events.sort(key=_event_sort_key)
        return {"ok": True, "days": days, "events": events[:MAX_EVENTS], "truncated": len(events) > MAX_EVENTS}
    except Exception:
        return {"ok": False, "error": {"code": "calendar_unavailable", "message": "Не удалось прочитать Яндекс Календарь. Проверьте пароль приложения и соединение."}}
    finally:
        if client is not None:
            client.close()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_upcoming_events(days: int = 7) -> dict[str, Any]:
    """Прочитать ближайшие события личного Яндекс Календаря за 1–14 дней; только время и название."""
    return read_upcoming_events(days)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_calendars() -> dict[str, Any]:
    """Вернуть названия и безопасные идентификаторы доступных личных календарей."""
    return read_calendars()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def create_event(title: str, start: str, end: str, calendar_id: str, operation_uid: str) -> dict[str, Any]:
    """Создать одно событие после подтверждения в приложении; operation_uid выдаёт только приложение."""
    return write_event(title, start, end, calendar_id, operation_uid)


if __name__ == "__main__":
    mcp.run(transport="stdio")
