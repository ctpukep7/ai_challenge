"""Exactly three typed MCP tools; run ownership is pinned by server construction."""
from mcp.server.fastmcp import FastMCP
from .service import Query, ArtifactID, SearchOutput, SummaryOutput, FileOutput, INPUTS
from pydantic import ValidationError
from .errors import PipelineError

class StrictMCP(FastMCP):
    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            tool.inputSchema = INPUTS[tool.name].model_json_schema()
        return tools

    async def call_tool(self, name, arguments):
        if name not in INPUTS:
            raise ValueError("invalid_arguments: Некорректные аргументы инструмента.")
        try:
            INPUTS[name].model_validate(arguments)
        except ValidationError:
            raise ValueError("invalid_arguments: Некорректные аргументы инструмента.") from None
        return await super().call_tool(name, arguments)

def create_server(service):
    server = StrictMCP("Day19 research pipeline", log_level="ERROR")

    @server.tool(name="search", description="Первый этап: найти до трёх источников по запросу. Возвращает immutable search_result_id.")
    async def search(query: Query) -> SearchOutput:
        try:
            return await service.search(query)
        except PipelineError as error:
            raise ValueError(f"{error.code}: {error}") from None
        except Exception:
            raise ValueError("internal: Цепочка остановлена из-за внутренней ошибки.") from None

    @server.tool(name="summarize", description="Второй этап: составить сводку по search_result_id этого запуска. Передайте ID из search.")
    async def summarize(search_result_id: ArtifactID) -> SummaryOutput:
        try:
            return await service.summarize(search_result_id)
        except PipelineError as error:
            raise ValueError(f"{error.code}: {error}") from None
        except Exception:
            raise ValueError("internal: Цепочка остановлена из-за внутренней ошибки.") from None

    @server.tool(name="saveToFile", description="Третий этап: сохранить точный Markdown по summary_id этого запуска. Только ID, никаких content/path.")
    async def save_to_file(summary_id: ArtifactID) -> FileOutput:
        try:
            return await service.save(summary_id)
        except PipelineError as error:
            raise ValueError(f"{error.code}: {error}") from None
        except Exception:
            raise ValueError("internal: Цепочка остановлена из-за внутренней ошибки.") from None

    return server
