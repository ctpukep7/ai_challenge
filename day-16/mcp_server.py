"""Локальный MCP-сервер с read-only weather tools из Open-Meteo."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP(
    "Demo MCP server",
    instructions="get_weather возвращает текущую погоду, get_forecast — прогноз на 3 дня из Open-Meteo.",
    log_level="WARNING",
)
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_SECONDS = 8


def fetch_json(url: str) -> dict[str, Any]:
    """Fetch one Open-Meteo JSON document with a bounded request timeout."""
    response = httpx.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def weather_description(code: object) -> str:
    labels = {
        0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
        45: "туман", 48: "изморозевый туман", 51: "лёгкая морось", 53: "морось",
        55: "сильная морось", 56: "ледяная морось", 57: "сильная ледяная морось",
        61: "небольшой дождь", 63: "дождь", 65: "сильный дождь", 66: "ледяной дождь",
        67: "сильный ледяной дождь", 71: "небольшой снег", 73: "снег", 75: "сильный снег",
        77: "снежные зёрна", 80: "небольшие ливни", 81: "ливни", 82: "сильные ливни",
        85: "снегопад", 86: "сильный снегопад", 95: "гроза", 96: "гроза с градом",
        99: "сильная гроза с градом",
    }
    return labels.get(code, "неизвестное состояние") if isinstance(code, int) else "неизвестное состояние"


def geocode_city(city: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve a city once for either tool, with stable errors and encoded URL."""
    requested_city = city.strip()
    if not requested_city:
        return None, error("empty_city", "Укажите непустое название города.")
    query = urlencode({"name": requested_city, "count": 1, "language": "ru", "format": "json"})
    try:
        response = fetch_json(f"{GEOCODING_URL}?{query}")
        results = response.get("results")
        if not isinstance(results, list) or not results:
            return None, error("city_not_found", f"Город «{requested_city}» не найден Open-Meteo.")
        place = results[0]
        if not isinstance(place, dict):
            raise TypeError("invalid geocoding result")
        latitude, longitude = place["latitude"], place["longitude"]
        if (
            isinstance(latitude, bool)
            or isinstance(longitude, bool)
            or not isinstance(latitude, (int, float))
            or not isinstance(longitude, (int, float))
        ):
            raise TypeError("invalid coordinates")
    except (httpx.HTTPError, TimeoutError, OSError, json.JSONDecodeError, KeyError, TypeError):
        return None, error("external_service_unavailable", "Open-Meteo временно недоступен. Повторите позже.")
    return {
        "name": place.get("name", requested_city), "country": place.get("country"),
        "latitude": latitude, "longitude": longitude,
    }, None


def fetch_forecast(location: dict[str, Any], **parameters: str | int) -> dict[str, Any]:
    query = urlencode({"latitude": location["latitude"], "longitude": location["longitude"], **parameters})
    return fetch_json(f"{FORECAST_URL}?{query}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_weather(city: str) -> dict[str, Any]:
    """Получить текущую погоду Open-Meteo для города без API-ключа."""
    location, location_error = geocode_city(city)
    if location_error is not None:
        return location_error
    assert location is not None
    try:
        forecast = fetch_forecast(
            location,
            current="temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            timezone="auto",
        )
        current = forecast.get("current")
        if not isinstance(current, dict):
            return error("invalid_response", "Open-Meteo не вернул текущие погодные данные.")
    except (httpx.HTTPError, TimeoutError, OSError, json.JSONDecodeError, KeyError, TypeError):
        return error("external_service_unavailable", "Open-Meteo временно недоступен. Повторите позже.")
    weather_code = current.get("weather_code")
    return {
        "ok": True, "city": location["name"], "country": location["country"],
        "latitude": location["latitude"], "longitude": location["longitude"],
        "temperature_c": current.get("temperature_2m"),
        "apparent_temperature_c": current.get("apparent_temperature"),
        "wind_speed_km_h": current.get("wind_speed_10m"), "weather_code": weather_code,
        "condition": weather_description(weather_code), "observed_at": current.get("time"),
        "source": "open-meteo",
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_forecast(city: str) -> dict[str, Any]:
    """Получить компактный прогноз Open-Meteo на ближайшие 3 дня для города."""
    location, location_error = geocode_city(city)
    if location_error is not None:
        return location_error
    assert location is not None
    try:
        forecast = fetch_forecast(
            location,
            daily="temperature_2m_min,temperature_2m_max,precipitation_probability_max,weather_code",
            forecast_days=3,
            timezone="auto",
        )
        daily = forecast.get("daily")
        if not isinstance(daily, dict):
            return error("invalid_response", "Open-Meteo не вернул данные прогноза.")
        columns = tuple(daily.get(key) for key in (
            "time", "temperature_2m_min", "temperature_2m_max",
            "precipitation_probability_max", "weather_code",
        ))
        if not all(isinstance(column, list) for column in columns):
            return error("invalid_response", "Open-Meteo вернул неполный прогноз.")
        dates, minimums, maximums, precipitations, codes = columns
        length = min(len(column) for column in columns)
        if length == 0:
            return error("invalid_response", "Open-Meteo не вернул дни прогноза.")
    except (httpx.HTTPError, TimeoutError, OSError, json.JSONDecodeError, KeyError, TypeError):
        return error("external_service_unavailable", "Open-Meteo временно недоступен. Повторите позже.")
    return {
        "ok": True, "city": location["name"], "country": location["country"],
        "days": [
            {
                "date": dates[index], "temperature_min_c": minimums[index],
                "temperature_max_c": maximums[index],
                "precipitation_probability_max_percent": precipitations[index],
                "weather_code": codes[index], "condition": weather_description(codes[index]),
            }
            for index in range(min(length, 3))
        ],
        "source": "open-meteo",
    }


if __name__ == "__main__":
    # stdout is reserved for the stdio JSON-RPC protocol.
    mcp.run(transport="stdio")
