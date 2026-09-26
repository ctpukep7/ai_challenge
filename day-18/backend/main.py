"""Read-only polling and interactive MCP chat; never dispatches scheduled jobs."""
import asyncio
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from .config import settings, SKILL_PATH
from .store import Store
from .agent import AgentRuntime, exception_leaves
from .mcp_client import connect,call
from scripts.cron import status as cron_status

app = FastAPI(title="День 18 — периодический сбор погоды")
app.add_middleware(CORSMiddleware,allow_origins=["http://127.0.0.1:5184","http://localhost:5184"],allow_methods=["GET","POST"],allow_headers=["Content-Type"])


def get_store():
    return Store(settings().db_path)


async def state(store=None):
    config = settings()
    store = store or get_store()
    result = store.state()
    result["config"] = {"model":config.model,"configured":bool(config.api_key),"timezone":config.timezone}
    result["scheduler"].update(await asyncio.to_thread(cron_status))
    return result


@app.get("/api/state")
async def read_state():
    return await state()


@app.get("/api/skill")
def read_skill():
    return {"content":SKILL_PATH.read_text()}


@app.get("/api/samples")
def samples(job_id: str | None = None):
    return get_store().data(job_id)


def read_requests(scope,entity_id):
    requests = get_store().requests(scope,entity_id)
    if requests is None:
        raise HTTPException(404,"Запись не найдена")
    return {"requests":requests}


@app.get("/api/messages/{message_id}/requests")
def message_requests(message_id: str):
    return read_requests("message",message_id)


@app.get("/api/runs/{run_id}/requests")
def run_requests(run_id: str):
    return read_requests("run",run_id)


class ChatBody(BaseModel):
    prompt: str = Field(min_length=1,max_length=2000)
    request_id: uuid.UUID


@app.post("/api/chat")
async def chat(body: ChatBody):
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(422,"Введите поручение")
    store = get_store()
    request_id = str(body.request_id)
    history = store.history()
    try:
        existing = store.begin_chat(request_id,prompt)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from None
    if existing and "answer" in existing:
        return {**existing,"state":await state(store)}
    token = existing["processing_token"]
    runtime = AgentRuntime(store)
    try:
        response = await runtime.run(prompt,request_id=request_id,history=history,chat_token=token)
    except asyncio.CancelledError as exc:
        store.finish_chat(request_id,"Запрос прерван; повторите сообщение",getattr(exc,"trace",[]),token,
                          json_requests=getattr(exc,"json_requests",[]))
        raise
    store.finish_chat(request_id,response["answer"],response["trace"],token,json_requests=response.get("json_requests"))
    return {"answer":response["answer"],"trace":response["trace"],"state":await state(store)}


class StatusBody(BaseModel):
    status: str


def schedule_error(exc):
    # Stdio cleanup may wrap a safe MCP validation error in a TaskGroup.
    prefixes = ("Сначала вызовите", "Задание", "Отменённое", "Статус:", "Запрос больше")
    for error in exception_leaves(exc):
        if isinstance(error,ValueError) and str(error).startswith(prefixes):
            return HTTPException(400,str(error)[:300])
    return HTTPException(503,"MCP-инструмент недоступен")


@app.post("/api/jobs/{job_id}/status")
async def change_status(job_id: str,body: StatusBody):
    store = get_store()
    try:
        async with asyncio.timeout(25):
            async with connect(store.path) as session:
                await call(session,"load_skill",{"name":"periodic-weather-collection"})
                await call(session,"list_schedules",{})
                await call(session,"set_schedule_status",{"job_id":job_id,"status":body.status})
    except Exception as exc:
        raise schedule_error(exc) from None
    return {"state":await state(store)}


@app.post("/api/jobs/{job_id}/delete")
async def delete_schedule(job_id: str):
    store = get_store()
    try:
        async with asyncio.timeout(25):
            async with connect(store.path) as session:
                await call(session,"load_skill",{"name":"periodic-weather-collection"})
                await call(session,"list_schedules",{})
                await call(session,"delete_weather_schedule",{"job_id":job_id})
    except Exception as exc:
        raise schedule_error(exc) from None
    return {"state":await state(store)}
