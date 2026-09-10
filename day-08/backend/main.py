"""FastAPI API for the Day 08 token laboratory."""

from pathlib import Path

import bleach
import markdown
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .agent import AgentConfigurationError, AgentError, AgentInputError, AgentRequestError, AssistantAgent, PROVIDERS

from .catalog import ModelMetadataService
from .store import SQLiteConversationStore


BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")
app = FastAPI(title="Day 08 — Лаборатория токенов")
store = SQLiteConversationStore(BASE_DIR / "data" / "context.sqlite3")
metadata_service = ModelMetadataService()
agent = AssistantAgent(store, metadata_service=metadata_service)
ALLOWED_TAGS = ["p", "br", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "h4", "hr", "table", "thead", "tbody", "tr", "th", "td"]


class AskPayload(BaseModel):
    conversation_id: int
    prompt: str
    controls: dict = Field(default_factory=dict)
    synthetic_copies: int = Field(default=1, ge=1, le=64)


class RenamePayload(BaseModel):
    title: str


def markdown_html(answer: str):
    return bleach.clean(markdown.markdown(answer, extensions=["extra", "sane_lists"]), tags=ALLOWED_TAGS, attributes={}, strip=True)


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


def public_error(error: AgentError, status=400):
    category = "validation"
    if isinstance(error, AgentConfigurationError):
        category = "configuration"
    elif isinstance(error, AgentRequestError):
        category = "provider_request"
    detail = {"message": str(error), "category": category}
    if isinstance(error, AgentRequestError) and error.provider_detail:
        detail["provider_detail"] = error.provider_detail
    raise HTTPException(status_code=status, detail=detail) from error


@app.get("/api/conversations")
def list_conversations():
    return {"conversations": store.list_conversations()}


@app.post("/api/conversations", status_code=201)
def create_conversation():
    return {"conversation": store.create_conversation()}


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: int):
    return {"conversation": require_conversation(conversation_id), "stats": store.token_stats(conversation_id)}


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


@app.get("/api/conversations/{conversation_id}/token-stats")
def token_stats(conversation_id: int):
    result = store.token_stats(conversation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Диалог не найден.")
    return result


@app.get("/api/model-metadata")
def model_metadata(provider: str = "deepseek", model: str = "deepseek-v4-flash"):
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="Неизвестный провайдер.")
    import os
    key = os.getenv(PROVIDERS[provider].key_name)
    return metadata_service.for_model(provider, model, key)


@app.get("/api/model-options")
def model_options(provider: str = "deepseek"):
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="Неизвестный провайдер.")
    import os
    key = os.getenv(PROVIDERS[provider].key_name)
    return {"models": metadata_service.model_options(provider, key)}


@app.post("/api/ask")
def ask(payload: AskPayload):
    try:
        result = agent.reply(
            payload.conversation_id,
            payload.prompt,
            payload.controls,
            synthetic_copies=payload.synthetic_copies,
        ).to_dict()
        result["answer_html"] = markdown_html(result["answer"])
        result["conversation"] = require_conversation(payload.conversation_id)
        result["stats"] = store.token_stats(payload.conversation_id)
        return result
    except AgentInputError as error:
        public_error(error, 400)
    except AgentConfigurationError as error:
        public_error(error, 503)
    except AgentError as error:
        public_error(error, 502)
