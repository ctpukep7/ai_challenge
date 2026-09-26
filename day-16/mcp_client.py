"""End-to-end MCP client for the local demo server over stdio."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, Tool


SERVER_PATH = Path(__file__).with_name("mcp_server.py")


async def initialize_list_and_call_tool(
    session: ClientSession, city: str, selected_tool: str
) -> tuple[list[Tool], CallToolResult]:
    """Discover one local server, then explicitly call one of its tools."""
    await session.initialize()
    tools = list((await session.list_tools()).tools)
    tool_name = "get_weather" if selected_tool == "weather" else "get_forecast"
    result = await session.call_tool(tool_name, {"city": city})
    return tools, result


async def request_weather(city: str, selected_tool: str) -> tuple[list[Tool], CallToolResult]:
    """Launch this directory's server as a subprocess and speak MCP over stdio."""
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PATH)],
        cwd=str(SERVER_PATH.parent),
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            return await initialize_list_and_call_tool(session, city, selected_tool)


def tool_as_dict(tool: Tool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": tool.inputSchema,
    }


def result_as_dict(result: CallToolResult) -> dict[str, Any]:
    """Print one useful representation, not duplicate text and structured data."""
    if result.structuredContent is not None:
        data: Any = result.structuredContent
    else:
        data = [item.model_dump(mode="json") for item in result.content]
    return {"isError": bool(result.isError), "data": data}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Запустить локальный MCP server, найти tool и вызвать его."
    )
    parser.add_argument("--city", default="Омск", help="город из demo-каталога (по умолчанию: Омск)")
    parser.add_argument(
        "--tool",
        choices=("weather", "forecast"),
        default="weather",
        help="какой tool одного локального MCP server вызвать (по умолчанию: weather)",
    )
    parser.add_argument("--json", action="store_true", help="оставлено для явности команды; вывод JSON")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        tools, result = asyncio.run(request_weather(args.city, args.tool))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("MCP weather demo cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"MCP weather demo failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    payload = {
        "connected": True,
        "transport": "stdio",
        "server": SERVER_PATH.name,
        "tools": [tool_as_dict(tool) for tool in tools],
        "call": {
            "tool": "get_weather" if args.tool == "weather" else "get_forecast",
            "arguments": {"city": args.city},
            "result": result_as_dict(result),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
