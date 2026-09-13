"""FastAPI endpoints for Day 10 context strategies."""

from pathlib import Path

import bleach
import markdown
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .agent import AgentConfigurationError, AgentError, AgentInputError, AgentRequestError, ContextStrategiesAgent
from .store import SQLiteStrategyStore


BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")
app = FastAPI(title="Day 10 — стратегии контекста")
store = SQLiteStrategyStore(BASE_DIR / "data" / "context.sqlite3")
agent = ContextStrategiesAgent(store)
ALLOWED_TAGS = ["p", "br", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "h4", "hr", "table", "thead", "tbody", "tr", "th", "td"]


class AskPayload(BaseModel):
    experiment_id: int
    target: str = "all"
    prompt: str
    controls: dict = Field(default_factory=dict)
    window_size: int = 6


class RenamePayload(BaseModel):
    title: str


class ActiveBranchPayload(BaseModel):
    thread_id: int


def html(text):
    return bleach.clean(markdown.markdown(text, extensions=["extra", "sane_lists"]), tags=ALLOWED_TAGS, attributes={}, strip=True)


def serialize(experiment):
    if not experiment:
        return None
    for message in experiment["messages"].values():
        if message["role"] == "assistant":
            message["answer_html"] = html(message["content"])
    facts = experiment["strategies"]["facts"]
    for snapshot in facts["facts_history"]:
        snapshot["pretty_values"] = snapshot["values"]
    return experiment


def require(experiment_id):
    experiment = serialize(store.get_experiment(experiment_id))
    if not experiment:
        raise HTTPException(404, "Эксперимент не найден.")
    return experiment


def public_error(error, status=400):
    category = "validation"
    if isinstance(error, AgentConfigurationError):
        category = "configuration"
    if isinstance(error, AgentRequestError):
        category = "provider_request"
    detail = {"message": str(error), "category": category}
    if getattr(error, "provider_detail", None):
        detail["provider_detail"] = error.provider_detail
    raise HTTPException(status, detail) from error


@app.get("/api/experiments")
def experiments():
    return {"experiments": store.list_experiments()}


@app.post("/api/experiments", status_code=201)
def create_experiment():
    created = store.create_experiment()
    return {"experiment": require(created["id"])}


@app.get("/api/experiments/{experiment_id}")
def get_experiment(experiment_id: int):
    return {"experiment": require(experiment_id), "stats": store.experiment_stats(experiment_id)}


@app.put("/api/experiments/{experiment_id}")
def rename_experiment(experiment_id: int, payload: RenamePayload):
    title = payload.title.strip()
    if not title or len(title) > 80:
        raise HTTPException(400, "Введите название до 80 символов.")
    if not store.rename_experiment(experiment_id, title):
        raise HTTPException(404, "Эксперимент не найден.")
    return {"experiment": require(experiment_id)}


@app.delete("/api/experiments/{experiment_id}")
def delete_experiment(experiment_id: int):
    active = store.delete_experiment(experiment_id)
    if not active:
        raise HTTPException(404, "Эксперимент не найден.")
    return {"active": active, "experiments": store.list_experiments()}


@app.post("/api/experiments/{experiment_id}/checkpoint")
def create_checkpoint(experiment_id: int):
    try:
        experiment = store.create_checkpoint(experiment_id)
        if not experiment:
            raise HTTPException(404, "Эксперимент не найден.")
        return {"experiment": serialize(experiment), "stats": store.experiment_stats(experiment_id)}
    except ValueError as error:
        raise HTTPException(400, {"message": str(error), "category": "validation"}) from error


@app.post("/api/experiments/{experiment_id}/active-branch")
def set_active_branch(experiment_id: int, payload: ActiveBranchPayload):
    if not store.set_active_branch(experiment_id, payload.thread_id):
        raise HTTPException(404, "Ветка не найдена.")
    return {"experiment": require(experiment_id), "stats": store.experiment_stats(experiment_id)}


@app.post("/api/ask")
def ask(payload: AskPayload):
    try:
        result = agent.ask(payload.experiment_id, payload.target, payload.prompt, payload.controls, payload.window_size)
        result["experiment"] = serialize(result["experiment"])
        for item in result["results"].values():
            if item.get("answer"):
                item["answer_html"] = html(item["answer"])
        return result
    except AgentInputError as error:
        public_error(error, 400)
    except AgentConfigurationError as error:
        public_error(error, 503)
    except AgentError as error:
        public_error(error, 502)
