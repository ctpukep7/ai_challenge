import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from dotenv import load_dotenv
from .config import Config, ROOT
from .errors import PipelineError
from .runner import Runner
from .store import Store

class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=2000)
    request_id: UUID

def create_app(config=None, runner_factory=None):
    if config is None:
        load_dotenv(ROOT / ".env")
        config = Config.from_env()
    store = Store(config.data_dir)
    runner = runner_factory(config, store) if runner_factory else Runner(config, store)
    tasks = set()

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        try:
            yield
        finally:
            active = list(tasks)
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            # A task can be cancelled before its coroutine gets its first turn.
            store.recover()

    app = FastAPI(title="Day 19 · MCP цепочка", lifespan=lifespan)
    app.state.store = store
    app.state.runner = runner

    def translate(error):
        status = {"configuration": 503, "busy": 409, "request_conflict": 409,
                  "not_found": 404, "no_file": 409, "artifact_integrity": 409}.get(error.code, 400)
        return HTTPException(status_code=status, detail={"error": str(error), "error_code": error.code})

    @app.post("/api/runs", status_code=202)
    async def start(request: RunRequest):
        if not request.prompt.strip():
            raise HTTPException(422, detail={"error": "Введите поручение.", "error_code": "invalid_arguments"})
        try:
            run, replayed = store.admit(request.prompt, str(request.request_id), bool(config.deepseek_key and config.tavily_key))
        except PipelineError as error:
            raise translate(error) from None
        if not replayed:
            task = asyncio.create_task(runner.run(run["run_id"]))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return {"run_id": run["run_id"], "status": run["status"], "replayed": replayed}

    @app.get("/api/state")
    async def state():
        runs = store.latest()
        return {"configuration": config.public(), "busy": any(run["status"] == "running" for run in runs), "runs": runs}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: UUID):
        try:
            return store.get(str(run_id))
        except PipelineError as error:
            raise translate(error) from None

    @app.get("/api/runs/{run_id}/download")
    async def download(run_id: UUID):
        try:
            data = store.file_bytes(str(run_id))
        except PipelineError as error:
            raise translate(error) from None
        return Response(data, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{run_id}.md"'})

    return app

app = create_app()
