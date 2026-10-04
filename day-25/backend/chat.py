"""Automatic user-grounded task memory and fresh retrieval on every question."""
from __future__ import annotations
import copy
import json
import re
from backend.store import EMPTY_TASK
from backend.memory import TaskMemory
from index_store import source_scope

COMMANDS = {"цель":"goal", "уточнение":"clarifications", "ограничение":"constraints", "термин":"terms", "источники":"source_days"}

def command_task(question, task):
    match = re.fullmatch(r"\s*(Цель|Уточнение|Ограничение|Термин|Источники)\s*:\s*(.*)", question, re.I | re.S)
    if not match: return None
    updated = copy.deepcopy(task)
    field, value = COMMANDS[match[1].lower()], match[2].strip()
    if field == "goal": updated[field] = value
    elif field == "source_days":
        if not value: updated[field] = []
        else:
            parts = re.split(r"\s*,\s*", value)
            if any(not p.isdigit() for p in parts): raise ValueError("Источники: номера дней через запятую, например 7,9")
            updated[field] = sorted(set(map(int, parts)))
    else:
        if not value: raise ValueError("Значение памяти не должно быть пустым")
        if value not in updated[field]: updated[field].append(value)
    return updated


def retrieval_plan(question, task, fit, messages=None):
    scope = source_scope(question)
    query = fit(question, "")
    if scope["reason"] == "no_explicit_day":
        recent = None
        if len(question.split()) <= 18:
            for message in reversed(messages or []):
                previous = message["question"]
                if re.match(r"\s*Источники\s*:", previous, re.I): break
                if re.match(r"\s*(Цель|Уточнение|Ограничение|Термин)\s*:", previous, re.I): continue
                previous_scope = source_scope(previous)
                if previous_scope["kind"] == "explicit_days":
                    recent = (previous, previous_scope)
                    break
        if recent:
            scope = {**recent[1],"kind":"conversation_days","reason":"last_explicit_user_reference"}
            query = fit(question, recent[0])
        else:
            if task["source_days"]:
                scope = {"kind":"task_days","sources":[f"day-{day:02d}/README.md" for day in task["source_days"]],"reason":"confirmed_task_memory"}
            if len(question.split()) <= 18 and task["goal"]:
                # The user's question is never shortened. Only optional goal words may be dropped.
                query = fit(question, task["goal"])
    return query, scope


def history_for(messages):
    result = []
    for message in messages:
        if message["status"] == "pending": continue
        result.append({"role":"user","content":message["question"]})
        if message["result"]:
            result.append({"role":"assistant","content":message["result"].get("answer", "")})
    return result[-12:]


class ChatService:
    def __init__(self, store, rag, validate_task, query_fitter, memory_llm=None):
        self.store, self.rag = store, rag
        self.validate_task, self.query_fitter = validate_task, query_fitter
        self.memory = TaskMemory(memory_llm, validate_task)

    def send(self, identifier, question, request_id, settings):
        reserved, revision, cached = self.store.reserve(identifier, question, request_id, settings)
        if cached:
            view = self.store.get(identifier)
            message = next(m for m in view["messages"] if m["id"] == reserved["id"])
            return {"message":message,"result":message["result"],"task_state":view["task_state"],"revision":view["revision"],"replayed":True}
        view = self.store.get(identifier)
        task = view["task_state"]
        updated = None
        memory_update = {'status':'unchanged','changes':[],'warning':None}
        try:
            command = command_task(question, task)
            if command is not None:
                updated = self.validate_task(command)
                memory_update = {'status':'manual','changes':[{'field':field,'action':'set','value':updated[field],'evidence':question} for field in updated if updated[field]!=task[field]],'warning':None}
                result = {"status":"memory_updated","answer":"Память задачи обновлена по вашей команде.","sources":[],"claims":[],"reason":"confirmed_user_command","task_state":updated}
            else:
                updated, memory_update = self.memory.extract(question, task, view["messages"])
                changed_sources = updated["source_days"] != task["source_days"]
                previous = [] if changed_sources else view["messages"][view["session"].get("scope_cutoff",0):-1]
                query, scope = retrieval_plan(question, updated, self.query_fitter, previous)
                settings = {**settings,"retrieval_scope":scope}
                result = self.rag.answer(question, settings=settings, history=history_for(view["messages"]), task_state=updated, retrieval_query=query)
                result["retrieval_query"] = query
                result["task_revision"] = revision
        except Exception as exc:
            # External/provider details may contain credentials; only validation errors are disclosed.
            result = {"status":"error","answer":"Запрос не выполнен. Можно отправить его повторно.","sources":[],"reason":"request_failed","error":str(exc)[:250] if isinstance(exc, ValueError) else "Ошибка обработки запроса"}
        result["memory_update"] = memory_update
        final = self.store.finish(identifier,reserved["id"],revision,result,updated)
        message = next(m for m in final["messages"] if m["id"] == reserved["id"])
        return {"message":message,"result":result,"task_state":final["task_state"],"revision":final["revision"],"replayed":False}
