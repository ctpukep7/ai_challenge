"""Test-only adapters: never selected by production configuration."""
from contextlib import asynccontextmanager
import json
from backend.errors import PipelineError
from backend.mcp_service import create_server
from backend.runner import Runner
from backend.service import PipelineService
from mcp.shared.memory import create_connected_server_and_client_session

class FakeSearch:
    async def search(self, query):
        return [{"title": "MCP documentation", "url": "https://modelcontextprotocol.io/", "text": "MCP exposes typed tools through client-server sessions."}]

class FakeSummary:
    async def summarize(self, snapshot):
        return "MCP позволяет агенту последовательно использовать типизированные инструменты."

class ScriptModel:
    def __init__(self, mode="success"):
        self.calls = 0
        self.mode = mode
        self.messages = []

    async def completion(self, messages, tools=None):
        self.calls += 1
        self.messages = messages.copy()
        if self.mode == "early":
            return {"role": "assistant", "content": "Готово."}
        if self.mode == "bad_twice":
            name, args = "search", {"query": "MCP", "filename": "bad"}
        elif self.mode == "correct" and self.calls == 1:
            name, args = "search", {"query": "MCP", "path": "bad"}
        elif messages[-1]["role"] == "tool" and "search_result_id" in json.loads(messages[-1]["content"]) and "summary_id" not in json.loads(messages[-1]["content"]):
            name, args = "summarize", {"search_result_id": json.loads(messages[-1]["content"])["search_result_id"]}
        elif messages[-1]["role"] == "tool" and "summary_id" in json.loads(messages[-1]["content"]):
            name, args = "saveToFile", {"summary_id": json.loads(messages[-1]["content"])["summary_id"]}
        else:
            name, args = "search", {"query": "MCP"}
        call = {"id": f"call-{self.calls}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        return {"role": "assistant", "content": None, "tool_calls": [call]}

def memory_factory(store, search=None, summary=None):
    @asynccontextmanager
    async def session(config, run_id):
        service = PipelineService(store, run_id, search or FakeSearch(), summary or FakeSummary())
        async with create_connected_server_and_client_session(create_server(service)) as client:
            yield client
    return session

def fake_runner(config, store):
    return Runner(config, store, ScriptModel(), memory_factory(store))
