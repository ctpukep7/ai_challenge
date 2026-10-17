import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
import uuid
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from backend.agent import run_task
from backend.files import Files, PolicyError
from backend.provider import DeepSeek

DAY = Path(__file__).resolve().parents[1]
ROOT = DAY.parent
EXAMPLES = [
    'Найди, где используется API поддержки и как вопрос проходит от интерфейса до ответа. Проверь реализацию и тесты.',
    'Подготовь памятку для пользователя поддержки Day33: как разбирать проблемы входа в Day30 по данным обращения. Изучи реализацию, интерфейс и тесты и создай Markdown-документ на русском.'
]


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    goal: str = Field(min_length=1, max_length=4000)


def create_app(root=None, provider=None):
    root = Path(root or ROOT).resolve()
    load_dotenv(DAY/'.env')
    provider = provider or DeepSeek()
    tasks = {}
    contexts = {}
    running = set()
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        for job in running:
            job.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    app = FastAPI(title='Pipeline · Day34 · Работа с файлами', lifespan=lifespan)
    app.state.tasks = tasks

    def find(task_id):
        if task_id not in tasks:
            raise HTTPException(404, 'Задача не найдена')
        return tasks[task_id]

    @app.get('/api/project')
    async def project():
        try:
            branch = subprocess.run(['git', '-C', str(root), 'branch', '--show-current'], capture_output=True, text=True, timeout=3, check=True).stdout.strip()
            if not branch:
                sha = subprocess.run(['git', '-C', str(root), 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True, timeout=3, check=True).stdout.strip()
                branch = f'detached HEAD · {sha}'
        except (subprocess.SubprocessError, OSError):
            branch = 'Git недоступен'
        return {'name': 'AI Challenge', 'branch': branch, 'root_label': 'AI_Challenge',
                'write_scope': 'day-34/generated/*.md', 'examples': EXAMPLES}

    @app.post('/api/tasks', status_code=202)
    async def start(req: TaskRequest):
        if not req.goal.strip():
            raise HTTPException(422, 'Введите цель')
        async with lock:
            if running:
                raise HTTPException(409, 'Другая задача ещё выполняется')
            if len(tasks) >= 30:
                oldest = next(iter(tasks))
                tasks.pop(oldest)
                contexts.pop(oldest)
            task_id = uuid.uuid4().hex
            task = {'id': task_id, 'goal': req.goal.strip(), 'status': 'running', 'summary': '', 'error': None,
                    'trace': [], 'sources': [], 'change': None, 'changes': [], 'metrics': {'llm_calls': 0, 'tool_calls': 0, 'elapsed_seconds': 0}, 'applied': False, 'format_repair_attempted': False}
            files = Files(root)
            tasks[task_id], contexts[task_id] = task, files
            job = asyncio.create_task(run_task(task, files, provider))
            running.add(job)
            job.add_done_callback(running.discard)
            return {'id': task_id, 'status': 'running'}

    @app.get('/api/tasks/{task_id}')
    async def get(task_id: str):
        return find(task_id)

    @app.post('/api/tasks/{task_id}/apply')
    async def apply(task_id: str):
        async with lock:
            task = find(task_id)
            if running:
                raise HTTPException(409, 'Дождитесь завершения текущей задачи')
            if task['applied']:
                return contexts[task_id].result()
            if task['status'] != 'completed' or not task['changes']:
                raise HTTPException(409, 'Нет завершённого изменения для сохранения')
            try:
                result = contexts[task_id].apply()
            except PolicyError as exc:
                raise HTTPException(409, str(exc)) from None
            task['applied'] = True
            return result

    @app.get('/api/tasks/{task_id}/patch', response_class=PlainTextResponse)
    async def patch(task_id: str):
        task = find(task_id)
        if task['status'] != 'completed' or not task['changes']:
            raise HTTPException(409, 'Нет подготовленного diff')
        return PlainTextResponse(''.join(change['diff'] for change in task['changes']), headers={'Content-Disposition': 'attachment; filename="day34.patch"'})

    if (DAY/'frontend/dist').exists():
        app.mount('/', StaticFiles(directory=DAY/'frontend/dist', html=True), name='ui')
    return app


app = create_app()
