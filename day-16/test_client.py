"""Offline tests for the MCP discovery sequence."""

from __future__ import annotations

import unittest

import anyio
from mcp import ClientSession
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams

from client import format_text, initialize_and_list_tools, tool_as_dict, tool_result_as_dict
from mcp.types import CallToolResult, TextContent


class McpToolsListTests(unittest.IsolatedAsyncioTestCase):
    async def discover_from_memory_server(self, server: FastMCP):
        """Run a FastMCP server and client on paired in-memory streams."""
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            client_read, client_write = client_streams
            server_read, server_write = server_streams
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    server._mcp_server.run,
                    server_read,
                    server_write,
                    server._mcp_server.create_initialization_options(),
                )
                try:
                    async with ClientSession(client_read, client_write) as session:
                        return await initialize_and_list_tools(session)
                finally:
                    task_group.cancel_scope.cancel()

    async def test_initialize_then_returns_known_tool_from_memory_server(self) -> None:
        server = FastMCP("Offline test server", log_level="CRITICAL")

        @server.tool()
        def hello(name: str) -> str:
            """Say hello without contacting external services."""
            return f"Hello, {name}!"

        tools = await self.discover_from_memory_server(server)

        self.assertEqual([tool.name for tool in tools], ["hello"])
        self.assertEqual(tool_as_dict(tools[0])["inputSchema"]["required"], ["name"])

    async def test_helper_initializes_and_lists_tools(self) -> None:
        server = FastMCP("Offline helper test", log_level="CRITICAL")

        @server.tool()
        def hello() -> str:
            """Known tool used only for the offline test."""
            return "Hello!"

        tools = await self.discover_from_memory_server(server)

        report = format_text("memory://offline", tools)
        self.assertIn("Connection established", report)
        self.assertIn("hello", report)

    def test_tool_result_serializer_exposes_only_protocol_content(self) -> None:
        result = CallToolResult(
            content=[TextContent(type="text", text="Read-only response")], isError=False
        )

        self.assertEqual(
            tool_result_as_dict(result),
            {
                "isError": False,
                "content": [
                    {
                        "type": "text",
                        "text": "Read-only response",
                        "annotations": None,
                        "meta": None,
                    }
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
