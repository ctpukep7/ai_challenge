"""One real stdio session per help request, with an explicit child environment."""
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

class ProjectSession:
    async def __aenter__(self):
        self.stack = AsyncExitStack()
        await self.stack.__aenter__()
        # mcp merges a small platform default set; only selected nonsecret values added.
        clean = {k: v for k, v in os.environ.items() if k in {'PATH', 'HOME', 'LANG', 'TMPDIR', 'SYSTEMROOT', 'ASSISTANT_EMBED_CACHE'}}
        clean.update({'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'DO_NOT_TRACK': '1'})
        try:
            read, write = await self.stack.enter_async_context(stdio_client(StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name('mcp_server.py'))], env=clean)))
            self.session = await self.stack.enter_async_context(ClientSession(read, write))
            await self.session.initialize()
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *args):
        return await self.stack.__aexit__(*args)

    async def discover(self):
        discovery = await self.session.list_tools()
        return [{'type': 'function', 'function': {'name': t.name, 'description': t.description or '', 'parameters': t.inputSchema}} for t in discovery.tools]

    async def call(self, name, arguments):
        result = await self.session.call_tool(name, arguments)
        if result.isError:
            return {'error': 'MCP не выполнил инструмент', 'evidence': []}
        try:
            value = json.loads('\n'.join(c.text for c in result.content if c.type == 'text'))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            return {'error': 'Некорректный результат MCP', 'evidence': []}
