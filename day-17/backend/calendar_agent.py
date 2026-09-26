"""MCP-backed calendar agent with explicit, durable write confirmation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .agent import (AgentConfigurationError, AgentError, AgentInputError,
                    AgentRequestError, COMPRESSION_MODES, PROVIDERS,
                    PersonalizedAgent, _attr)

SERVER_PATH = Path(__file__).resolve().parents[1] / "mcp_server.py"
READ_TOOL, CREATE_TOOL, CALENDARS_TOOL = "list_upcoming_events", "create_event", "list_calendars"
CLARIFY_TOOL = "clarify_event"
MAX_MODEL_STEPS = 3
CREATE_INTENT = re.compile(r"\b(?:создай|создать|создаю|добавь|добавить|поставь|поставить|запланируй|запланировать|предложи)\b", re.I)
EVENT_INTENT = re.compile(r"\b(?:событи\w*|встреч\w*|запис\w*|календар\w*)\b", re.I)
OMSK_CONTEXT = re.compile(r"\bомск\w*\b", re.I)
DATETIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})\Z")
LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def local_mcp_session():
    calendar_env = {
        name: value
        for name in ("YANDEX_CALDAV_USERNAME", "YANDEX_CALDAV_APP_PASSWORD")
        if (value := os.environ.get(name))
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PATH)],
        cwd=str(SERVER_PATH.parent),
        env=calendar_env,
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            yield session


def _tool_payload(tool):
    return {"type": "function", "function": {"name": tool.name, "description": tool.description or "", "parameters": tool.inputSchema}}


def _create_payload():
    # The model can propose fields, never a target calendar or operation UID.
    return {"type": "function", "function": {"name": CREATE_TOOL,
        "description": "Предложить будущее событие; запись только после подтверждения пользователя.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"}, "start": {"type": "string", "description": "ISO 8601 с часовым поясом"},
            "end": {"type": "string", "description": "ISO 8601 с часовым поясом"}},
            "required": ["title", "start", "end"], "additionalProperties": False}}}


def _clarify_payload():
    return {"type": "function", "function": {"name": CLARIFY_TOOL,
        "description": "Уточнить только действительно отсутствующую или противоречивую информацию о событии; не записывает событие.",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}},
                       "required": ["question"], "additionalProperties": False}}}


def _create_intent(prompt):
    return bool(CREATE_INTENT.search(prompt) and EVENT_INTENT.search(prompt))


def _validate_explicit_zone(prompt, args):
    if not OMSK_CONTEXT.search(prompt):
        return
    zone = ZoneInfo("Asia/Omsk")
    for key in ("start", "end"):
        value = datetime.fromisoformat(args[key].replace("Z", "+00:00"))
        if value.utcoffset() != value.astimezone(zone).utcoffset():
            raise AgentInputError("Предложенное время не соответствует часовому поясу Омска. Повторите запрос.")


def _safe_model_error(error, stage):
    """Classify at the model call, before MCP's task group can wrap the error."""
    leaves = list(_exception_leaves(error))
    leaf = next((item for item in leaves if type(getattr(item, "status_code", None)) is int), leaves[0])
    kind = type(leaf).__name__
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", kind):
        kind = "UnknownError"
    status = getattr(leaf, "status_code", None)
    status = status if type(status) is int and 400 <= status <= 599 else None
    LOGGER.warning("Calendar model request failed: stage=%s exception_type=%s http_status=%s",
                   stage, kind, status if status is not None else "none")
    detail = f"Этап {stage}" + (f", HTTP {status}" if status is not None else "")
    return AgentRequestError("Не удалось получить ответ модели. Проверьте настройки модели и повторите попытку.", detail)


def _exception_leaves(error):
    if isinstance(error, BaseExceptionGroup):
        for item in error.exceptions:
            yield from _exception_leaves(item)
    else:
        yield error


def _assistant_tool_message(message):
    """Preserve the model's actual tool-call IDs and arguments in the transcript."""
    if isinstance(message, dict):
        raw = dict(message)
    elif callable(getattr(message, "model_dump", None)):
        raw = message.model_dump(exclude_none=True)
    else:
        raw = {"content": _attr(message, "content"), "tool_calls": _attr(message, "tool_calls")}
    calls = []
    for call in raw.get("tool_calls") or []:
        value = call if isinstance(call, dict) else call.model_dump(exclude_none=True) if callable(getattr(call, "model_dump", None)) else {
            "id": _attr(call, "id"), "type": "function",
            "function": {"name": _attr(_attr(call, "function"), "name"),
                         "arguments": _attr(_attr(call, "function"), "arguments")}}
        calls.append(value)
    result = {"role": "assistant", "content": raw.get("content"), "tool_calls": calls}
    if "reasoning_content" in raw:
        result["reasoning_content"] = raw["reasoning_content"]
    return result


def _result_data(result, key):
    data = result.structuredContent
    if data is None:
        texts = [part.text for part in result.content if getattr(part, "type", None) == "text"]
        if len(texts) != 1:
            raise AgentRequestError("MCP вернул неожиданный результат.")
        try:
            data = json.loads(texts[0])
        except json.JSONDecodeError as error:
            raise AgentRequestError("MCP вернул некорректный JSON.") from error
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        raise AgentRequestError("MCP вернул неожиданный результат.")
    if result.isError or not data["ok"]:
        reason = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else None
        raise AgentRequestError(reason or "MCP-инструмент вернул ошибку.")
    if not isinstance(data.get(key), list):
        raise AgentRequestError("MCP вернул неожиданный список.")
    return data


def _parse_call(call):
    function = _attr(call, "function")
    name, call_id = _attr(function, "name"), _attr(call, "id")
    if name not in (READ_TOOL, CREATE_TOOL, CLARIFY_TOOL) or not isinstance(call_id, str) or not call_id:
        raise AgentInputError("Модель запросила недоступный инструмент.")
    try:
        args = json.loads(_attr(function, "arguments") or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise AgentInputError("Модель передала некорректные аргументы инструмента.") from error
    if not isinstance(args, dict):
        raise AgentInputError("Аргументы инструмента должны быть объектом.")
    if name == CLARIFY_TOOL:
        question = args.get("question")
        if set(args) != {"question"} or not isinstance(question, str) or not question.strip() or len(question) > 500:
            raise AgentInputError("Уточнение должно содержать один короткий вопрос.")
        return name, call_id, {"question": question.strip()}
    if name == READ_TOOL:
        if set(args) - {"days"}:
            raise AgentInputError("Модель передала недопустимые аргументы календаря.")
        days = args.get("days", 7)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 14:
            raise AgentInputError("days должно быть целым числом от 1 до 14.")
        return name, call_id, {"days": days}
    if set(args) != {"title", "start", "end"}:
        raise AgentInputError("Для события нужны только title, start и end.")
    title, start, end = (args[key] for key in ("title", "start", "end"))
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120 or any(ord(c) < 32 for c in title):
        raise AgentInputError("Название события должно содержать от 1 до 120 символов.")
    if not isinstance(start, str) or not isinstance(end, str) or not DATETIME_PATTERN.fullmatch(start) or not DATETIME_PATTERN.fullmatch(end):
        raise AgentInputError("Укажите начало и конец в ISO 8601 с часовым поясом.")
    try:
        a, b = datetime.fromisoformat(start.replace("Z", "+00:00")), datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as error:
        raise AgentInputError("Некорректная дата или время события.") from error
    if a <= datetime.now(timezone.utc) or b <= a or b - a > timedelta(hours=24):
        raise AgentInputError("Событие должно начаться в будущем и длиться не более 24 часов.")
    return name, call_id, {"title": title.strip(), "start": start, "end": end}


def _preview(controls, messages, snapshot, trace):
    preview = {"kind": "answer", "provider": controls.provider, "model": controls.model,
               "temperature": controls.temperature, "compression_mode": controls.compression_mode,
               "compression_label": COMPRESSION_MODES[controls.compression_mode],
               "profile_snapshot": snapshot, "messages": messages, "mcp_trace": trace}
    if controls.max_tokens is not None:
        preview["max_tokens"] = controls.max_tokens
    return preview


def _usage(items, started):
    value = {key: sum(_attr(_attr(item, "usage"), key) or 0 for item in items)
             for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    value["elapsed_ms"] = round((perf_counter() - started) * 1000)
    return value


def _time_context(now):
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now_provider must return a timezone-aware datetime")
    local_time = now.isoformat(timespec="seconds")
    utc_time = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    return {"role": "system", "content": (
        f"Доверенный контекст времени для этого запроса: UTC {utc_time}; "
        f"локальное время сервера {local_time}. "
        "Используй текущую дату для относительных выражений вроде «завтра». "
        "Локальный часовой пояс сервера не обязательно совпадает с часовым поясом пользователя. "
        "Омск — Asia/Omsk; используй смещение этой зоны на дату события, не спрашивай повторно часовой пояс, если назван Омск. "
        "Если пользователь явно назвал иной часовой пояс или город, используй его часовой пояс "
        "и смещение на дату события; если пояс нельзя определить однозначно, уточни его. "
        "Если длительность противоречит указанному интервалу, уточни, что важнее, до предложения. "
        "Если название, начало и конец встречи известны, вызови create_event с ISO 8601 "
        "и явным смещением, чтобы предложить событие. Не заявляй о создании: "
        "запись возможна только после подтверждения пользователем в интерфейсе."
    )}


class CalendarMCPAgent(PersonalizedAgent):
    def __init__(self, store, client=None, client_factory=None, mcp_session_factory=local_mcp_session,
                 now_provider=None):
        if client_factory is None:
            super().__init__(store, client=client)
        else:
            super().__init__(store, client=client, client_factory=client_factory)
        self.mcp_session_factory = mcp_session_factory
        self.now_provider = now_provider or (lambda: datetime.now().astimezone())

    def ask(self, prompt, raw_controls=None):
        if not isinstance(prompt, str) or not (prompt := prompt.strip()):
            raise AgentInputError("Введите непустой вопрос.")
        if len(prompt) > 4000:
            raise AgentInputError("Вопрос может содержать до 4 000 символов.")
        controls, state = self.controls_from(raw_controls), self.store.state()
        session_id = state["active_session"]["id"]
        if self.store.pending_action(session_id):
            raise AgentInputError("Сначала подтвердите или отмените предложенное событие.")
        snapshot = self.profile_snapshot(state["active_profile"])
        outcome = asyncio.run(self._first_with_mcp(controls, self.build_messages(prompt, controls, state), snapshot))
        answer, preview, usage = outcome["answer"], outcome["preview"], outcome["usage"]
        if outcome.get("proposal"):
            ids = self.store.create_calendar_action(session_id, prompt, answer, preview, **outcome["proposal"])
        else:
            ids = self.store.append_turn(session_id, prompt, answer, preview)
        result = {"answer": answer, "preview": preview, "usage": usage,
                  "session_id": session_id, "mcp_trace": preview["mcp_trace"], **ids}
        if outcome.get("proposal"):
            return result
        if controls.compression_mode == "rolling_summary":
            try:
                result["summary_generated"] = self._compress(self.store.state(), controls, snapshot)
            except AgentError as error:
                result["summary_error"] = {"message": str(error)}
        if controls.compression_mode == "sticky_facts":
            try:
                result["facts_update"] = self._update_facts(self.store.state(), controls, ids["user_id"], prompt, snapshot)
            except (AgentError, json.JSONDecodeError) as error:
                result["facts_error"] = {"message": str(error)}
        return result

    async def _first_with_mcp(self, controls, messages, snapshot):
        started = perf_counter()
        stage = "prepare"
        try:
            # Keep clock context in this model request, not in persisted chat history.
            request_messages = [*messages[:-1], _time_context(self.now_provider()), messages[-1]]
            wants_create = _create_intent(messages[-1]["content"])
            stage = "mcp_connect"
            async with self.mcp_session_factory() as session:
                stage = "mcp_list_tools"
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                if not {READ_TOOL, CREATE_TOOL, CALENDARS_TOOL}.issubset(tools):
                    raise AgentRequestError("MCP-инструменты календаря недоступны.")
                trace = {"available_tools": [READ_TOOL, CREATE_TOOL], "called_tool": None, "steps": []}
                stage = "model_setup"
                model = self._client(PROVIDERS[controls.provider]).chat.completions
                tool_schemas = [_tool_payload(tools[READ_TOOL]), _create_payload(), _clarify_payload()]
                completions = []
                read_days = set()
                for step in range(MAX_MODEL_STEPS):
                    stage = "model_first_response" if step == 0 else "model_second_response"
                    try:
                        response = await asyncio.to_thread(
                            model.create, **self._options(controls, request_messages),
                            tools=tool_schemas,
                            tool_choice="required" if wants_create else "auto")
                    except Exception as error:
                        raise _safe_model_error(error, stage) from error
                    completions.append(response)
                    try:
                        model_message = response.choices[0].message
                    except (IndexError, AttributeError, TypeError) as error:
                        raise AgentRequestError("Модель вернула ответ в неожиданном формате.") from error
                    calls = _attr(model_message, "tool_calls") or []
                    if not calls:
                        answer = _attr(model_message, "content")
                        if wants_create:
                            raise AgentRequestError("Модель не предложила событие и не указала, что требуется уточнить. Повторите запрос с конкретной датой и временем.")
                        if not isinstance(answer, str) or not answer.strip():
                            raise AgentRequestError("Модель вернула пустой ответ.")
                        return {"answer": answer.strip(), "preview": _preview(controls, messages, snapshot, trace),
                                "usage": _usage(completions, started)}
                    if len(calls) != 1:
                        raise AgentInputError("За один ответ разрешён только один вызов календаря.")
                    name, call_id, args = _parse_call(calls[0])
                    if name == CLARIFY_TOOL:
                        question = args["question"]
                        if OMSK_CONTEXT.search(messages[-1]["content"]) and re.search(r"часов\w*\s+пояс|временн\w*\s+зон", question, re.I):
                            raise AgentRequestError("Омское время уже указано. Модель запросила лишнее уточнение; повторите запрос.")
                        trace["steps"].append({"name": CLARIFY_TOOL, "status": "clarification"})
                        return {"answer": question, "preview": _preview(controls, messages, snapshot, trace),
                                "usage": _usage(completions, started)}
                    if name == CREATE_TOOL:
                        _validate_explicit_zone(messages[-1]["content"], args)
                        stage = "mcp_list_calendars"
                        calendar_data = _result_data(await session.call_tool(CALENDARS_TOOL, {}), "calendars")
                        calendars = calendar_data["calendars"]
                        if not calendars or not all(isinstance(x, dict) and isinstance(x.get("id"), str) and isinstance(x.get("name"), str) for x in calendars):
                            raise AgentRequestError("Не найден доступный календарь для записи.")
                        if len({x["id"] for x in calendars}) != len(calendars):
                            raise AgentRequestError("Список календарей содержит повторяющиеся идентификаторы.")
                        trace["called_tool"] = {"name": CREATE_TOOL, "arguments": args, "status": "proposed"}
                        trace["steps"].append(trace["called_tool"])
                        answer = f"Проверьте событие «{args['title']}» с {args['start']} до {args['end']} и подтвердите создание. До подтверждения календарь не изменён."
                        context = {"messages": messages, "call_id": call_id,
                                   "controls": {"provider": controls.provider, "model": controls.model,
                                                "temperature": controls.temperature, "max_tokens": controls.max_tokens,
                                                "compression_mode": controls.compression_mode}}
                        return {"answer": answer, "preview": _preview(controls, messages, snapshot, trace),
                                "usage": _usage(completions, started),
                                "proposal": {**args, "calendars": calendars, "context": context}}
                    if args["days"] in read_days:
                        raise AgentRequestError("Модель повторно запросила те же события календаря. Уточните запрос и попробуйте снова.")
                    read_days.add(args["days"])
                    if step == MAX_MODEL_STEPS - 1:
                        raise AgentRequestError("Агент превысил лимит шагов календаря. Уточните запрос и попробуйте снова.")
                    stage = "mcp_read_events"
                    data = _result_data(await session.call_tool(READ_TOOL, args), "events")
                    trace["called_tool"] = {"name": READ_TOOL, "arguments": args,
                                            "status": "success", "event_count": len(data["events"])}
                    trace["steps"].append(trace["called_tool"])
                    request_messages = [*request_messages, _assistant_tool_message(model_message),
                                        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(data, ensure_ascii=False)}]
                raise AgentRequestError("Агент превысил лимит шагов календаря.")
        except (AgentConfigurationError, AgentInputError, AgentRequestError):
            raise
        except Exception as error:
            # AnyIO can wrap an already classified model error while closing
            # the stdio task group. Preserve that safe error and avoid logging twice.
            original = next((item for item in _exception_leaves(error) if isinstance(item, AgentError)), None)
            if original is not None:
                raise original from None
            # Never log exception text or traceback: SDK errors can include URLs and credentials.
            leaf = next(_exception_leaves(error))
            if stage.startswith("model_"):
                raise _safe_model_error(error, stage) from error
            LOGGER.warning("Calendar request failed: stage=%s exception_type=%s", stage, type(leaf).__name__)
            if stage.startswith("mcp_"):
                raise AgentRequestError("Не удалось обратиться к MCP-серверу календаря. Проверьте подключение и повторите попытку.") from error
            raise AgentRequestError("Не удалось обработать запрос. Повторите попытку.") from error

    async def _write_event(self, arguments):
        async with self.mcp_session_factory() as session:
            tools = {tool.name for tool in (await session.list_tools()).tools}
            if CREATE_TOOL not in tools:
                raise AgentRequestError("MCP-инструмент создания события недоступен.")
            result = await session.call_tool(CREATE_TOOL, arguments)
            data = result.structuredContent
            if data is None:
                texts = [part.text for part in result.content if getattr(part, "type", None) == "text"]
                data = json.loads(texts[0]) if len(texts) == 1 else None
            if result.isError:
                return None
            return data

    def confirm(self, action_id, calendar_id):
        row, claimed = self.store.claim_calendar_action(action_id, calendar_id)
        if not claimed:
            if row["status"] == "created" and row["answer"] is None:
                fallback = f"Событие «{row['title']}» создано с {row['start']} до {row['end']}."
                trace = {"available_tools": [READ_TOOL, CREATE_TOOL], "called_tool": {
                    "name": CREATE_TOOL, "arguments": {k: row[k] for k in ("title", "start", "end")},
                    "status": "created", "calendar_id": row["selected_calendar_id"]}}
                return self.store.save_calendar_answer(action_id, fallback, {"kind": "answer", "mcp_trace": trace})
            return self.store.calendar_action(action_id)
        arguments = {"title": row["title"], "start": row["start"], "end": row["end"],
                     "calendar_id": row["selected_calendar_id"], "operation_uid": row["operation_uid"]}
        try:
            data = asyncio.run(self._write_event(arguments))
        except Exception:
            return self.store.finish_calendar_action(action_id, status="unknown")
        valid = isinstance(data, dict) and data.get("ok") is True and data.get("uid") == row["operation_uid"]
        valid = valid and data.get("title") == row["title"] and data.get("calendar_id") == row["selected_calendar_id"]
        try:
            valid = valid and datetime.fromisoformat(data["start"].replace("Z", "+00:00")) == datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
            valid = valid and datetime.fromisoformat(data["end"].replace("Z", "+00:00")) == datetime.fromisoformat(row["end"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, TypeError, ValueError):
            valid = False
        if not valid:
            return self.store.finish_calendar_action(action_id, status="unknown")
        public_result = {key: data[key] for key in ("ok", "uid", "title", "start", "end", "calendar_id")}
        public_result["calendar_name"] = next((item["name"] for item in json.loads(row["calendars_json"])
                                               if item["id"] == row["selected_calendar_id"]), "Календарь")
        self.store.finish_calendar_action(action_id, status="created", result=public_result)
        context = json.loads(row["context_json"])
        answer = f"Событие «{row['title']}» создано с {row['start']} до {row['end']}."
        try:
            controls = self.controls_from(context["controls"])
            followup = [*context["messages"],
                        {"role": "system", "content": "Подтверждённый MCP-инструмент создаёт одно событие. После результата достоверно сообщи о записи; не предлагай создать его повторно."},
                        {"role": "assistant", "content": None, "tool_calls": [{"id": context["call_id"], "type": "function",
                         "function": {"name": CREATE_TOOL, "arguments": json.dumps({k: row[k] for k in ("title", "start", "end")}, ensure_ascii=False)}}]},
                        {"role": "tool", "tool_call_id": context["call_id"], "content": json.dumps(public_result, ensure_ascii=False)}]
            second = self._client(PROVIDERS[controls.provider]).chat.completions.create(**self._options(controls, followup))
            candidate = second.choices[0].message.content
            if isinstance(candidate, str) and candidate.strip():
                answer = candidate.strip()
        except Exception:
            pass  # MCP success is already persisted; never hide it behind a model error.
        trace = {"available_tools": [READ_TOOL, CREATE_TOOL], "called_tool": {
            "name": CREATE_TOOL, "arguments": {k: row[k] for k in ("title", "start", "end")},
            "status": "created", "calendar_id": row["selected_calendar_id"]}}
        return self.store.save_calendar_answer(action_id, answer, {"kind": "answer", "mcp_trace": trace})
