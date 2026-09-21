"""Минимальный MCP-клиент: соединение, initialize и discovery tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, Tool


# Public, read-only demo endpoint. It is deliberately configurable because a
# public service can become unavailable or change its tool catalogue.
DEFAULT_SERVER_URL = "https://mcp.deepwiki.com/mcp"


async def initialize_and_list_tools(session: ClientSession) -> list[Tool]:
    """Perform the MCP handshake and return only the advertised tools."""
    await session.initialize()
    result = await session.list_tools()
    return list(result.tools)


async def discover_tools(url: str, timeout_seconds: float) -> list[Tool]:
    """Open a Streamable HTTP connection and discover tools without executing any."""
    async with streamablehttp_client(url, timeout=timeout_seconds) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            return await initialize_and_list_tools(session)


async def discover_and_read_wiki_structure(
    url: str, timeout_seconds: float, repo_name: str
) -> tuple[list[Tool], CallToolResult]:
    """List tools and explicitly run DeepWiki's read-only documentation lookup."""
    async with streamablehttp_client(url, timeout=timeout_seconds) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            tools = await initialize_and_list_tools(session)
            result = await session.call_tool(
                "read_wiki_structure", {"repoName": repo_name}
            )
            return tools, result


def tool_as_dict(tool: Tool) -> dict[str, Any]:
    """Return JSON-safe, useful discovery metadata for one MCP tool."""
    return {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": tool.inputSchema,
    }


def tool_result_as_dict(result: CallToolResult) -> dict[str, Any]:
    """Make an MCP tool result safe to print as JSON."""
    return {
        "isError": bool(result.isError),
        "content": [item.model_dump(mode="json") for item in result.content],
    }


def format_text(url: str, tools: Sequence[Tool]) -> str:
    """Build a compact human-readable discovery report."""
    lines = ["Connection established", f"Server: {url}", f"Available tools: {len(tools)}"]
    for tool in tools:
        lines.extend(
            (
                f"\n- {tool.name}",
                f"  Description: {tool.description or 'No description'}",
                "  Input schema:",
                json.dumps(tool.inputSchema, ensure_ascii=False, indent=2, sort_keys=True),
            )
        )
    return "\n".join(lines)


def describe_error(exc: BaseException) -> str:
    """Flatten SDK task groups so a failed connection remains understandable."""
    children = getattr(exc, "exceptions", None)
    if isinstance(children, tuple):
        messages = [describe_error(child) for child in children]
        return "; ".join(message for message in messages if message)
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect to an MCP Streamable HTTP server and list its tools."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("MCP_SERVER_URL", DEFAULT_SERVER_URL),
        help="MCP Streamable HTTP URL (or MCP_SERVER_URL; default: %(default)s)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print a JSON document instead of readable text",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="connection/request timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--demo-repo",
        metavar="OWNER/REPO",
        help=(
            "explicitly call DeepWiki's read-only read_wiki_structure tool "
            "for this GitHub repository after tools/list"
        ),
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.demo_repo is not None and "/" not in args.demo_repo:
        parser.error("--demo-repo must use owner/repository format")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.demo_repo:
            tools, tool_result = asyncio.run(
                discover_and_read_wiki_structure(args.url, args.timeout, args.demo_repo)
            )
        else:
            tools = asyncio.run(discover_tools(args.url, args.timeout))
            tool_result = None
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Discovery cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:  # transport and protocol errors originate in the SDK
        print(f"MCP discovery failed for {args.url}: {describe_error(exc)}", file=sys.stderr)
        return 1

    if args.json:
        payload: dict[str, Any] = {
            "connected": True,
            "server": args.url,
            "tools": [tool_as_dict(tool) for tool in tools],
        }
        if tool_result is not None:
            payload["demoCall"] = {
                "tool": "read_wiki_structure",
                "arguments": {"repoName": args.demo_repo},
                "result": tool_result_as_dict(tool_result),
            }
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(format_text(args.url, tools))
        if tool_result is not None:
            print("\nDemo call: read_wiki_structure")
            print(json.dumps(tool_result_as_dict(tool_result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
