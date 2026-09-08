"""FastAPI boundary for the Day 07 persistent-context agent."""

from pathlib import Path

import bleach
import markdown
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .agent import AgentConfigurationError, AgentError, AgentInputError, AssistantAgent
from .store import SQLiteConversationStore


BASE_DIR = Path(__file__).resolve().parents[1]
ALLOWED_TAGS = [
    "p", "br", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol",
    "li", "h1", "h2", "h3", "h4", "hr", "table", "thead", "tbody", "tr", "th", "td",
]

load_dotenv(BASE_DIR / ".env")
app = FastAPI(title="Day 07 — Сохранение контекста")
store = SQLiteConversationStore(BASE_DIR / "data" / "context.sqlite3")
agent = AssistantAgent(store)


class AskPayload(BaseModel):
    conversation_id: int
    prompt: str
    controls: dict = Field(default_factory=dict)


class RenamePayload(BaseModel):
    title: str


def markdown_html(answer: str) -> str:
    """Render only a safe subset of Markdown supplied by the model."""
    return bleach.clean(
        markdown.markdown(answer, extensions=["extra", "sane_lists"]),
        tags=ALLOWED_TAGS,
        attributes={},
        strip=True,
    )


def serialize_conversation(conversation_id: int):
    conversation = store.get_conversation(conversation_id)
    if not conversation:
        return None
    for message in conversation["messages"]:
        if message["role"] == "assistant":
            message["answer_html"] = markdown_html(message["content"])
    return conversation


def require_conversation(conversation_id: int):
    conversation = serialize_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Диалог не найден.")
    return conversation


@app.get("/api/conversations")
def list_conversations():
    return {"conversations": store.list_conversations()}


@app.post("/api/conversations", status_code=201)
def create_conversation():
    return {"conversation": store.create_conversation()}


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: int):
    return {"conversation": require_conversation(conversation_id)}


@app.put("/api/conversations/{conversation_id}")
def rename_conversation(conversation_id: int, payload: RenamePayload):
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Введите название диалога.")
    if len(title) > 80:
        raise HTTPException(status_code=400, detail="Название должно быть не длиннее 80 символов.")
    if not store.rename_conversation(conversation_id, title):
        raise HTTPException(status_code=404, detail="Диалог не найден.")
    return {"conversation": require_conversation(conversation_id)}


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: int):
    active = store.delete_conversation(conversation_id)
    if not active:
        raise HTTPException(status_code=404, detail="Диалог не найден.")
    return {"active": active, "conversations": store.list_conversations()}


@app.post("/api/ask")
def ask(payload: AskPayload):
    if not payload.prompt.strip():
        raise HTTPException(status_code=400, detail="Введите непустой текст вопроса.")
    try:
        result = agent.reply(payload.conversation_id, payload.prompt, payload.controls).to_dict()
        result["answer_html"] = markdown_html(result["answer"])
        result["conversation"] = require_conversation(payload.conversation_id)
        return result
    except AgentInputError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except AgentConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except AgentError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
