"""Scoped MCP service. The scheduled model cannot choose another job or city."""
import json
import os
from mcp.server.fastmcp import FastMCP
from .config import settings, SKILL_PATH
from .store import Store
from . import weather


def make_server(store=None, scope=None, request_id=None, chat_token=None):
    store = store or Store(settings().db_path)
    server = FastMCP("Day 18 — periodic weather collection")
    loaded = False
    listed_jobs = None

    def require_skill():
        if not loaded:
            raise ValueError("Сначала вызовите load_skill")

    def require_listed_job(job_id):
        require_skill()
        if listed_jobs is None:
            raise ValueError("Сначала вызовите list_schedules для актуального списка расписаний")
        if job_id not in listed_jobs:
            raise ValueError("Задание не найдено в актуальном списке расписаний")

    if scope:
        @server.tool()
        async def get_current_weather() -> dict:
            """Получить реальную текущую погоду закреплённого города. Без аргументов."""
            with store.connection() as db:
                from .scheduling import now_utc
                store.assert_scope(db,scope,now_utc())
                job = dict(db.execute("SELECT * FROM jobs WHERE id=?",(scope["job_id"],)).fetchone())
            payload = await weather.current_weather(job)
            return store.observation(scope,payload)

        @server.tool()
        def save_weather_result(result_id: str) -> dict:
            """Сохранить наблюдение только по result_id из get_current_weather текущего запуска."""
            return store.save_result(scope,result_id)
    else:
        @server.tool()
        def load_skill(name: str) -> dict:
            """Загрузить инструкцию periodic-weather-collection перед изменением заданий."""
            nonlocal loaded
            if name != "periodic-weather-collection":
                raise ValueError("Доступен только periodic-weather-collection")
            loaded = True
            return {"name":name,"content":SKILL_PATH.read_text()}

        @server.tool()
        async def resolve_city(query: str) -> dict:
            """Найти город. Вернуть реальные city_id и координаты для выбора расписания."""
            return {"candidates":store.register_cities(await weather.resolve(query))}

        @server.tool()
        def create_weather_schedule(city_id: str, instruction: str, schedule_type: str,
                                    timezone: str, interval_minutes: int | None = None,
                                    cron_expression: str | None = None) -> dict:
            """Сохранить периодический сбор погоды: interval (1–1440 минут) или cron (5 полей)."""
            require_skill()
            job = store.create_job(city_id,instruction,schedule_type,interval_minutes,
                                   cron_expression,timezone,SKILL_PATH.read_text(),request_id,chat_token=chat_token)
            return public_job(job)

        @server.tool()
        def list_schedules() -> dict:
            """Актуальный список текущих и удалённых расписаний. Перед изменением выбери job_id из этого списка."""
            nonlocal listed_jobs
            jobs = [public_job(job) for job in store.list_jobs()]
            listed_jobs = {job["id"] for job in jobs}
            return {"jobs":jobs}

        @server.tool()
        def set_schedule_status(job_id: str, status: str) -> dict:
            """После load_skill и свежего list_schedules: paused — прекратить сбор, active — возобновить, cancelled — окончательно отменить."""
            nonlocal listed_jobs
            require_listed_job(job_id)
            job = public_job(store.set_status(job_id,status,request_id=request_id,chat_token=chat_token))
            listed_jobs = None
            return job

        @server.tool()
        def delete_weather_schedule(job_id: str) -> dict:
            """После load_skill и свежего list_schedules удалить указанное расписание сбора погоды. История сохранится, общий cron не изменится."""
            nonlocal listed_jobs
            require_listed_job(job_id)
            job = public_job(store.delete_job(job_id,request_id=request_id,chat_token=chat_token))
            listed_jobs = None
            return job
    return server


def public_job(job):
    return {key:value for key,value in job.items() if key not in ("request_id","instruction","skill_body","skill_hash")}


def run():
    scope = json.loads(os.getenv("DAY18_SCOPE","null"))
    make_server(scope=scope,request_id=os.getenv("DAY18_REQUEST_ID"),chat_token=os.getenv("DAY18_CHAT_TOKEN")).run(transport="stdio")
