from typing import Annotated
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .store import digest, now
from .errors import PipelineError

Query = Annotated[str, Field(min_length=1, max_length=400)]
ArtifactID = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", json_schema_extra={"format": "uuid"})]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class SearchInput(StrictModel):
    query: Query

class SummaryInput(StrictModel):
    search_result_id: ArtifactID

class SaveInput(StrictModel):
    summary_id: ArtifactID

class Source(BaseModel):
    title: str
    url: str
    text: str

class SearchOutput(BaseModel):
    search_result_id: ArtifactID
    query: str
    sources: list[Source]
    retrieved_at: str
    sha256: str

class SummaryOutput(BaseModel):
    summary_id: ArtifactID
    search_result_id: ArtifactID
    input_sha256: str
    markdown: str
    sha256: str

class FileOutput(BaseModel):
    file_id: ArtifactID
    summary_id: ArtifactID
    filename: str
    bytes: int
    sha256: str
    download_url: str

INPUTS = {"search": SearchInput, "summarize": SummaryInput, "saveToFile": SaveInput}

def parse_input(model, **values):
    try:
        return model(**values).model_dump()
    except ValidationError:
        raise PipelineError("invalid_arguments") from None

class PipelineService:
    def __init__(self, store, run_id, search_adapter, summary_adapter):
        self.store, self.run_id = store, run_id
        self.search_adapter, self.summary_adapter = search_adapter, summary_adapter

    async def search(self, query):
        args = parse_input(SearchInput, query=query)
        if not query.strip():
            raise PipelineError("invalid_arguments")
        previous = self.store.existing(self.run_id, "search", args)
        if previous:
            return SearchOutput(**previous)
        self.store.check_stage(self.run_id, "search")
        self.store.start_step(self.run_id, "search")
        sources = await self.search_adapter.search(query)
        if not sources:
            raise PipelineError("empty_search")
        core = {"query": query, "sources": sources, "retrieved_at": now()}
        output = {"search_result_id": str(uuid4()), **core, "sha256": digest(core)}
        return SearchOutput(**self.store.register(self.run_id, "search", args, output))

    async def summarize(self, search_result_id):
        args = parse_input(SummaryInput, search_result_id=search_result_id)
        snapshot = self.store.artifact(self.run_id, search_result_id, "search")
        previous = self.store.existing(self.run_id, "summarize", args)
        if previous:
            return SummaryOutput(**previous)
        self.store.check_stage(self.run_id, "summarize")
        self.store.start_step(self.run_id, "summarize", search_result_id, snapshot["sha256"])
        text = await self.summary_adapter.summarize(snapshot)
        if not isinstance(text, str) or not text.strip():
            raise PipelineError("empty_summary")
        lines = [f"# {snapshot['query']}", "", f"Поиск выполнен: {snapshot['retrieved_at']}", "", text.strip(), "", "## Источники", ""]
        # Plain URL lines avoid constructing Markdown links from untrusted titles.
        for index, source in enumerate(snapshot["sources"], 1):
            lines.extend([f"{index}. {source['title']}", f"   {source['url']}"])
        markdown = "\n".join(lines) + "\n"
        if len(markdown) > 10_000:
            raise PipelineError("payload_limit")
        output = {"summary_id": str(uuid4()), "search_result_id": search_result_id,
                  "input_sha256": snapshot["sha256"], "markdown": markdown,
                  "sha256": digest(markdown.encode())}
        return SummaryOutput(**self.store.register(self.run_id, "summarize", args, output))

    async def save(self, summary_id):
        args = parse_input(SaveInput, summary_id=summary_id)
        summary = self.store.artifact(self.run_id, summary_id, "summarize")
        previous = self.store.existing(self.run_id, "saveToFile", args)
        if previous:
            self.store.file_bytes(self.run_id)
            return FileOutput(**previous)
        self.store.check_stage(self.run_id, "saveToFile")
        self.store.start_step(self.run_id, "saveToFile", summary_id, summary["sha256"])
        data = summary["markdown"].encode("utf-8")
        self.store.atomic_write(self.run_id, data)
        output = {"file_id": str(uuid4()), "summary_id": summary_id,
                  "filename": f"{self.run_id}.md", "bytes": len(data), "sha256": digest(data),
                  "download_url": f"/api/runs/{self.run_id}/download"}
        self.store.register(self.run_id, "saveToFile", args, output)
        self.store.file_bytes(self.run_id)
        return FileOutput(**output)
