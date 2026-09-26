"""UTC storage, timezone-aware cron and a future-only interval grid."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re
from croniter import croniter


def now_utc():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def parse(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def validate_schedule(kind, minutes, expression, tz):
    try:
        ZoneInfo(tz)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("Укажите существующий часовой пояс IANA, например Asia/Omsk") from None
    if kind == "interval":
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:
            raise ValueError("Интервал должен быть целым числом от 1 до 1440 минут")
        if expression:
            raise ValueError("Для интервала cron_expression должен быть пустым")
    elif kind == "cron":
        if minutes is not None or not isinstance(expression, str) or len(expression.split()) != 5:
            raise ValueError("Нужно ровно 5 полей cron и пустой interval_minutes")
        if len(expression) > 100 or not re.fullmatch(r"[0-9*/ ,\-]+",expression) or not croniter.is_valid(expression):
            raise ValueError("Некорректное расписание cron")
    else:
        raise ValueError("Поддерживаются interval и cron")


def next_run(job, after, anchor=None):
    if job["schedule_type"] == "cron":
        local = after.astimezone(ZoneInfo(job["timezone"]))
        return croniter(job["cron_expression"], local).get_next(datetime).astimezone(timezone.utc)
    step = timedelta(minutes=job["interval_minutes"])
    if anchor is None:
        return after + step
    elapsed = max(0, (after - anchor).total_seconds())
    return anchor + step * (int(elapsed // step.total_seconds()) + 1)
