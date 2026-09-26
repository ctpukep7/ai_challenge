"""Real MCP initialize → tools/list → tools/call over an isolated stdio session."""
from contextlib import asynccontextmanager
import json
import os
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from .config import ROOT


@asynccontextmanager
async def connect(db_path, scope=None, request_id=None, chat_token=None):
    # Do not pass the model key into the MCP weather subprocess.
    env = {key:value for key,value in os.environ.items() if key not in ("DEEPSEEK_API_KEY", "DAY18_SCOPE", "DAY18_REQUEST_ID", "DAY18_CHAT_TOKEN")}
    env["DAY18_DB_PATH"] = str(db_path)
    if scope:
        env["DAY18_SCOPE"] = json.dumps({key:scope[key] for key in ("run_id","job_id","token")})
    if request_id:
        env["DAY18_REQUEST_ID"] = request_id
    if chat_token:
        env["DAY18_CHAT_TOKEN"] = chat_token
    params = StdioServerParameters(command=sys.executable,args=[str(ROOT / "mcp_server.py")],env=env,cwd=str(ROOT))
    with open(os.devnull,"w") as quiet:
        async with stdio_client(params,errlog=quiet) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=__import__('datetime').timedelta(seconds=20)) as session:
                await session.initialize()
                yield session


def decode(result):
    if result.isError:
        # MCP errors may include raw upstream exceptions. Keep public errors key-safe.
        texts = [item.text for item in result.content if item.type == "text"]
        message = " ".join(texts)
        allowed = ("Сначала вызовите", "Статус:", "Задание", "Отменённое", "Запуск больше", "Запрос больше", "result_id", "Источник погоды", "Не удалось найти", "Интервал", "Нужно ровно", "Некорректное расписание", "Можно сохранить", "Сначала найдите", "Укажите существующий", "Поручение", "Для интервала")
        safe = next((message[message.index(prefix):][:300] for prefix in allowed if prefix in message), "MCP-инструмент не смог выполнить запрос")
        raise ValueError(safe)
    structured = getattr(result,"structuredContent",None)
    if structured is not None:
        return structured
    text = "\n".join(item.text for item in result.content if item.type == "text")
    return json.loads(text)


async def call(session,name,arguments):
    return decode(await session.call_tool(name,arguments=arguments))
