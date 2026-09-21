"""Offline protocol tests for the local demo MCP server."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import anyio
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from mcp_client import initialize_list_and_call_tool
from mcp_server import FORECAST_URL, GEOCODING_URL, get_forecast, get_weather, mcp


class WeatherToolUnitTests(unittest.TestCase):
    def test_weather_uses_geocoding_then_forecast_and_returns_real_source(self) -> None:
        responses = [
            {"results": [{"name": "Омск", "country": "Россия", "latitude": 54.99, "longitude": 73.37}]},
            {
                "current": {
                    "time": "2026-09-21T12:00",
                    "temperature_2m": 17.2,
                    "apparent_temperature": 15.8,
                    "weather_code": 2,
                    "wind_speed_10m": 12.4,
                }
            },
        ]
        with patch("mcp_server.fetch_json", side_effect=responses) as fetch:
            forecast = get_weather(" Омск ")

        self.assertEqual(forecast["city"], "Омск")
        self.assertEqual(forecast["source"], "open-meteo")
        self.assertTrue(forecast["ok"])
        self.assertEqual(forecast["temperature_c"], 17.2)
        self.assertEqual(forecast["condition"], "переменная облачность")
        self.assertIn(GEOCODING_URL, fetch.call_args_list[0].args[0])
        self.assertIn("name=%D0%9E%D0%BC%D1%81%D0%BA", fetch.call_args_list[0].args[0])
        self.assertIn(FORECAST_URL, fetch.call_args_list[1].args[0])

    def test_empty_city_returns_structured_validation_error(self) -> None:
        response = get_weather("   ")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "empty_city")

    def test_unknown_city_returns_structured_error_without_forecast_request(self) -> None:
        with patch("mcp_server.fetch_json", return_value={"results": []}) as fetch:
            response = get_weather("Несуществующий город")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "city_not_found")
        self.assertEqual(fetch.call_count, 1)

    def test_network_error_is_not_replaced_by_demo_weather(self) -> None:
        with patch("mcp_server.fetch_json", side_effect=OSError("offline")):
            response = get_weather("Омск")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "external_service_unavailable")
        self.assertNotIn("source", response)

    def test_forecast_returns_three_compact_days_from_open_meteo(self) -> None:
        responses = [
            {"results": [{"name": "Омск", "country": "Россия", "latitude": 54.99, "longitude": 73.37}]},
            {"daily": {
                "time": ["2026-09-21", "2026-09-22", "2026-09-23"],
                "temperature_2m_min": [5.1, 6.2, 7.3],
                "temperature_2m_max": [13.4, 14.5, 15.6],
                "precipitation_probability_max": [20, 40, 10],
                "weather_code": [2, 61, 0],
            }},
        ]
        with patch("mcp_server.fetch_json", side_effect=responses) as fetch:
            result = get_forecast("Омск")

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "open-meteo")
        self.assertEqual(len(result["days"]), 3)
        self.assertEqual(result["days"][1]["condition"], "небольшой дождь")
        self.assertEqual(result["days"][2]["temperature_max_c"], 15.6)
        self.assertIn("forecast_days=3", fetch.call_args_list[1].args[0])
        self.assertIn("daily=", fetch.call_args_list[1].args[0])


class WeatherMcpProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_server_lists_both_tools_and_calls_them_on_choice(self) -> None:
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            client_read, client_write = client_streams
            server_read, server_write = server_streams
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    mcp._mcp_server.run,
                    server_read,
                    server_write,
                    mcp._mcp_server.create_initialization_options(),
                )
                try:
                    with patch(
                        "mcp_server.fetch_json",
                        side_effect=[
                            {"results": [{"name": "Омск", "latitude": 55, "longitude": 73}]},
                            {"current": {"temperature_2m": 18, "weather_code": 0}},
                            {"results": [{"name": "Омск", "latitude": 55, "longitude": 73}]},
                            {"daily": {
                                "time": ["2026-09-21", "2026-09-22", "2026-09-23"],
                                "temperature_2m_min": [1, 2, 3],
                                "temperature_2m_max": [10, 11, 12],
                                "precipitation_probability_max": [0, 10, 20],
                                "weather_code": [0, 2, 61],
                            }},
                        ],
                    ):
                        async with ClientSession(client_read, client_write) as session:
                            tools, weather_result = await initialize_list_and_call_tool(
                                session, "Омск", "weather"
                            )
                            forecast_result = await session.call_tool("get_forecast", {"city": "Омск"})
                finally:
                    task_group.cancel_scope.cancel()

        self.assertEqual({tool.name for tool in tools}, {"get_forecast", "get_weather"})
        self.assertTrue(all(tool.annotations.readOnlyHint for tool in tools))
        self.assertFalse(weather_result.isError)
        self.assertFalse(forecast_result.isError)
        text = weather_result.content[0].text
        self.assertEqual(json.loads(text)["source"], "open-meteo")
        self.assertEqual(json.loads(text)["city"], "Омск")
        self.assertEqual(json.loads(forecast_result.content[0].text)["source"], "open-meteo")


if __name__ == "__main__":
    unittest.main()
