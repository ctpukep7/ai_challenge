"""Only fixed Open-Meteo endpoints; tool output is validated before storage."""
import asyncio
import math
from datetime import datetime, timezone
import httpx
from .scheduling import iso, now_utc


def number(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("Источник погоды вернул некорректные данные")
    return value


async def get_with_retry(client,url,params):
    """At most one retry of a read-only GET, within the caller's 15s deadline."""
    for attempt in range(2):
        try:
            response = await client.get(url,params=params)
            response.raise_for_status()
            return response
        except (httpx.NetworkError,httpx.TimeoutException):
            if attempt:
                raise
        except httpx.HTTPStatusError as exc:
            if attempt or not 500 <= exc.response.status_code < 600:
                raise


async def resolve(query):
    if not query.strip() or len(query) > 120:
        raise ValueError("Название города должно содержать от 1 до 120 символов")
    try:
        async with asyncio.timeout(15), httpx.AsyncClient(timeout=httpx.Timeout(8,connect=5)) as client:
            response = await get_with_retry(client,"https://geocoding-api.open-meteo.com/v1/search",params={"name":query,"count":5,"language":"ru","format":"json"})
            items = response.json().get("results",[])
            results = []
            for item in items:
                label = ", ".join(dict.fromkeys(str(item[key]) for key in ("name","admin1","country") if item.get(key)))
                results.append({"label":label,"latitude":number(item["latitude"],-90,90),"longitude":number(item["longitude"],-180,180),"country":str(item.get("country","")),"timezone":str(item.get("timezone","UTC"))})
            return results
    except (httpx.HTTPError,TimeoutError,KeyError,TypeError,ValueError,AttributeError):
        raise ValueError("Не удалось найти город: источник недоступен или вернул некорректный ответ") from None


def validate_weather(body, fetched_at=None):
    try:
        current = body["current"]
        units = body["current_units"]
        if units["temperature_2m"] != "°C" or units["relative_humidity_2m"] != "%" or units["wind_speed_10m"] != "km/h":
            raise ValueError("Неверные единицы")
        if body.get("utc_offset_seconds") != 0:
            raise ValueError("Погода должна быть в UTC")
        observed = datetime.fromisoformat(current["time"])
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        fetched_at = fetched_at or now_utc()
        if abs((observed-fetched_at).total_seconds()) > 7200:
            raise ValueError("Устаревшие данные")
        code = number(current["weather_code"],0,99)
        if int(code) != code:
            raise ValueError("Неверный погодный код")
        return {"temperature":number(current["temperature_2m"],-100,70),
                "humidity":number(current["relative_humidity_2m"],0,100),
                "wind_speed":number(current["wind_speed_10m"],0,500),
                "weather_code":int(code),"observed_at":iso(observed),"fetched_at":iso(fetched_at)}
    except (KeyError,TypeError,ValueError,OverflowError):
        raise ValueError("Источник погоды вернул некорректные или устаревшие данные") from None


async def current_weather(job):
    try:
        async with asyncio.timeout(15), httpx.AsyncClient(timeout=httpx.Timeout(8,connect=5)) as client:
            response = await get_with_retry(client,"https://api.open-meteo.com/v1/forecast",params={"latitude":job["latitude"],"longitude":job["longitude"],"current":"temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code","timezone":"UTC","temperature_unit":"celsius","wind_speed_unit":"kmh"})
            return validate_weather(response.json())
    except (httpx.HTTPError,TimeoutError):
        raise ValueError("Источник погоды недоступен; наблюдение не сохранено") from None
    except ValueError:
        raise ValueError("Источник погоды вернул некорректные или устаревшие данные") from None
