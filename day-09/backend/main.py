"""FastAPI endpoints for the Day 09 compression comparison."""

from pathlib import Path

import bleach
import markdown
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .agent import AgentConfigurationError, AgentError, AgentInputError, AgentRequestError, ContextComparisonAgent
from .store import SQLiteExperimentStore


BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")
app = FastAPI(title="Day 09 — Управление контекстом")
store = SQLiteExperimentStore(BASE_DIR / "data" / "context.sqlite3")
agent = ContextComparisonAgent(store)
ALLOWED_TAGS = ["p", "br", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "h4", "hr", "table", "thead", "tbody", "tr", "th", "td"]


class AskPayload(BaseModel):
    experiment_id: int
    prompt: str
    controls: dict = Field(default_factory=dict)
    batch_size: int = 10
    keep_recent: int = 6


class RenamePayload(BaseModel): title: str


def html(text):
    return bleach.clean(markdown.markdown(text, extensions=["extra", "sane_lists"]), tags=ALLOWED_TAGS, attributes={}, strip=True)


def serialize(experiment):
    if not experiment: return None
    for branch in experiment["branches"].values():
        for message in branch["messages"]:
            if message["role"] == "assistant": message["answer_html"] = html(message["content"])
        for summary in branch.get("summaries", []):
            summary["summary_html"] = html(summary["content"])
    return experiment


def require(experiment_id):
    experiment = serialize(store.get_experiment(experiment_id))
    if not experiment: raise HTTPException(404, "Эксперимент не найден.")
    return experiment


def public_error(error, status=400):
    category = "validation"
    if isinstance(error, AgentConfigurationError): category = "configuration"
    if isinstance(error, AgentRequestError): category = "provider_request"
    detail = {"message": str(error), "category": category}
    if getattr(error, "provider_detail", None): detail["provider_detail"] = error.provider_detail
    raise HTTPException(status, detail) from error


@app.get("/api/experiments")
def experiments(): return {"experiments": store.list_experiments()}


@app.post("/api/experiments", status_code=201)
def create_experiment():
    created = store.create_experiment()
    return {"experiment": require(created["id"])}


@app.get("/api/experiments/{experiment_id}")
def get_experiment(experiment_id: int): return {"experiment": require(experiment_id), "stats": store.experiment_stats(experiment_id)}


@app.put("/api/experiments/{experiment_id}")
def rename_experiment(experiment_id: int, payload: RenamePayload):
    title = payload.title.strip()
    if not title or len(title) > 80: raise HTTPException(400, "Введите название до 80 символов.")
    if not store.rename_experiment(experiment_id, title): raise HTTPException(404, "Эксперимент не найден.")
    return {"experiment": require(experiment_id)}


@app.delete("/api/experiments/{experiment_id}")
def delete_experiment(experiment_id: int):
    active = store.delete_experiment(experiment_id)
    if not active: raise HTTPException(404, "Эксперимент не найден.")
    return {"active": active, "experiments": store.list_experiments()}


@app.post("/api/ask")
def ask(payload: AskPayload):
    try:
        result = agent.reply_pair(payload.experiment_id, payload.prompt, payload.controls, payload.batch_size, payload.keep_recent)
        result["experiment"] = serialize(result["experiment"])
        for item in result["results"].values():
            if item.get("answer"): item["answer_html"] = html(item["answer"])
        return result
    except AgentInputError as error: public_error(error, 400)
    except AgentConfigurationError as error: public_error(error, 503)
    except AgentError as error: public_error(error, 502)
