"""Приватный шлюз: Ollama остаётся доступен только на loopback."""
from __future__ import annotations
import asyncio
import hmac
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, ConfigDict, ValidationError

MODEL = 'qwen3:4b-instruct-2507-q4_K_M'
SYSTEM = 'Ты локальный помощник. Отвечай на русском, точно и понятно. Не добавляй выдуманные факты.'
ROOT = Path(__file__).resolve().parents[1]
OPTIONS = {'temperature': .7, 'num_ctx': 4096, 'num_predict': 1024}

@dataclass(frozen=True)
class Config:
    token: str
    model: str = MODEL
    static_dir: Path = ROOT / 'frontend' / 'dist'

    @classmethod
    def from_file(cls, path=ROOT / 'data' / 'access-token'):
        try:
            token = Path(path).read_text().strip()
        except OSError:
            raise RuntimeError('Сначала выполните настройку: npm run setup -- --host <IP Mac>.') from None
        if len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
            raise RuntimeError('Файл токена должен содержать случайный 256-битный токен.')
        if Path(path).stat().st_mode & 0o077:
            raise RuntimeError('Файл токена должен иметь права 0600.')
        return cls(token=token)

class Pair(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    user: str
    assistant: str

class ChatInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    prompt: str
    history: list[Pair] = []

class WindowLimiter:
    def __init__(self, clock, bound=1024):
        self.clock, self.bound, self.entries = clock, bound, OrderedDict()
    def take(self, key, limit):
        now = self.clock()
        for expired in [k for k, (start, _) in self.entries.items() if now - start >= 60]:
            del self.entries[expired]
        start, count = self.entries.get(key, (now, 0))
        if count >= limit:
            return max(1, math.ceil(60 - (now - start)))
        self.entries[key] = (start, count + 1)
        self.entries.move_to_end(key)
        while len(self.entries) > self.bound:
            self.entries.popitem(last=False)
        return 0

class BodyCap:
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith('/api/'):
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            size += len(message.get('body', b''))
            if size > 32768:
                return await error('Тело запроса превышает 32768 байт.', 413)(scope, receive, send)
            chunks.append(message)
            if not message.get('more_body', False):
                break
        async def replay():
            if chunks:
                return chunks.pop(0)
            return await receive()
        await self.app(scope, replay, send)


def error(message, status=400, headers=None):
    codes = {401:'unauthorized', 409:'busy', 413:'context_limit', 422:'invalid_request', 429:'rate_limit', 502:'upstream_error', 503:'unavailable', 504:'timeout', 404:'not_found'}
    return JSONResponse({'error': {'code': codes.get(status, 'error'), 'message': message}}, status_code=status, headers=headers)


def create_app(config: Config | None = None, transport=None, clock: Callable = time.monotonic):
    config = config or Config.from_file()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyCap)
    limiter = WindowLimiter(clock)
    chat_limiter = WindowLimiter(clock, bound=1)
    slot = asyncio.Lock()
    app.state.limiter = limiter
    app.state.slot = slot
    async def upstream(method, path, payload=None):
        async with httpx.AsyncClient(base_url='http://127.0.0.1:11434', transport=transport,
                                     timeout=120, trust_env=False, follow_redirects=False) as client:
            response = await client.request(method, path, json=payload)
            response.raise_for_status()
            return response.json()

    @app.middleware('http')
    async def security(request: Request, call_next):
        if request.url.path.startswith('/api/'):
            supplied = request.headers.get('authorization', '')
            # Compare UTF-8 bytes: unexpected unicode headers must not raise TypeError.
            expected = ('Bearer ' + config.token).encode()
            if not hmac.compare_digest(supplied.encode(), expected):
                ip = request.client.host if request.client else 'unknown'
                retry = limiter.take(('auth', ip), 30)
                response = error('Слишком много попыток входа.', 429, {'Retry-After': str(retry)}) if retry else error('Требуется токен доступа.', 401)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get('/api/limits')
    async def limits():
        return {'model': config.model, 'max_history_pairs': 4, 'max_input_bytes': 6000,
                'max_body_bytes': 32768, 'requests_per_minute': 6, 'concurrency': 1,
                'queue_size': 0, 'timeout_seconds': 120, 'options': OPTIONS,
                'max_context_bytes': 6000, 'context_window': 4096, 'max_output_tokens': 1024,
                'rate_limit_per_minute': 6,
                'context_limit_kind': 'UTF-8 bytes including system; not exact tokenization'}

    @app.get('/api/health')
    async def health():
        try:
            tags = await upstream('GET', '/api/tags')
            version = await upstream('GET', '/api/version')
            if not isinstance(tags, dict) or not isinstance(tags.get('models'), list) or not isinstance(version, dict):
                return error('Ollama вернула некорректный ответ.', 503)
            present = any(isinstance(m, dict) and (m.get('name') == config.model or m.get('model') == config.model) for m in tags['models'])
            if not present:
                return error('Выбранная модель не установлена в Ollama.', 503)
            return {'status': 'ok', 'model': config.model, 'ollama_version': version.get('version')}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return error('Ollama недоступна или вернула некорректный ответ.', 503)

    @app.post('/api/chat')
    async def chat(request: Request):
        try:
            data = ChatInput.model_validate(await request.json())
        except (ValueError, ValidationError, UnicodeError, RecursionError):
            return error('Некорректный JSON: нужны prompt и history без дополнительных полей.', 422)
        if not data.prompt.strip():
            return error('Введите непустой вопрос.', 422)
        if len(data.history) > 4:
            return error('История ограничена четырьмя парами сообщений.', 413)
        texts = [SYSTEM, data.prompt] + [t for p in data.history for t in (p.user, p.assistant)]
        try:
            input_size = sum(len(t.encode('utf-8')) for t in texts)
        except UnicodeError:
            return error('Текст содержит некорректные Unicode-символы.', 422)
        if input_size > 6000:
            return error('Контекст превышает 6000 байт UTF-8. Начните новый чат или сократите текст.', 413)
        if slot.locked():
            return error('Модель занята. Дождитесь завершения текущего запроса.', 409)
        retry = chat_limiter.take(('chat', 'shared'), 6)
        if retry:
            return error('Лимит: 6 запросов в минуту. Повторите позже.', 429, {'Retry-After': str(retry)})
        # No await occurs between locked() and acquire() for the single event loop.
        async with slot:
            started = clock()
            messages = [{'role': 'system', 'content': SYSTEM}]
            for pair in data.history:
                messages.extend([{'role': 'user', 'content': pair.user}, {'role': 'assistant', 'content': pair.assistant}])
            messages.append({'role': 'user', 'content': data.prompt})
            try:
                result = await upstream('POST', '/api/chat', {'model': config.model, 'messages': messages,
                        'stream': False, 'think': False, 'keep_alive': '1m', 'options': OPTIONS})
                if not isinstance(result, dict) or result.get('done') is not True:
                    return error('Модель вернула незавершённый или некорректный ответ.', 502)
                if not isinstance(result.get('message'), dict):
                    return error('Модель вернула некорректный ответ.', 502)
                answer = result['message']['content']
                if not isinstance(answer, str) or not answer.strip():
                    return error('Модель вернула пустой ответ.', 502)
                def number(key):
                    value = result.get(key)
                    try:
                        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None
                    except (OverflowError, ValueError):
                        return None
                def seconds(key):
                    value = number(key)
                    return value / 1e9 if value is not None else None
                def finite_calculation(value):
                    try:
                        return value if math.isfinite(value) else None
                    except (OverflowError, ValueError):
                        return None
                output, prompt = number('eval_count'), number('prompt_eval_count')
                generation = seconds('eval_duration')
                return {'answer': answer, 'model': config.model, 'truncated': result.get('done_reason') == 'length',
                        'metrics': {'total_seconds': max(0, clock() - started), 'load_seconds': seconds('load_duration'),
                        'generation_seconds': generation, 'prompt_tokens': prompt, 'output_tokens': output,
                        'total_tokens': finite_calculation(prompt + output) if prompt is not None and output is not None else None,
                        'tokens_per_second': finite_calculation(output / generation) if output is not None and generation else None}}
            except httpx.TimeoutException:
                return error('Модель не ответила за 120 секунд.', 504)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                return error('Ollama недоступна или вернула некорректный ответ.', 502)

    @app.api_route('/api/{path:path}', methods=['GET','POST','PUT','DELETE','PATCH','OPTIONS','HEAD'])
    async def unknown_api(path):
        return error('Метод API не найден.', 404)

    @app.get('/{path:path}')
    async def static(path):
        base = config.static_dir.resolve()
        candidate = (base / path).resolve()
        if candidate.is_relative_to(base) and candidate.is_file():
            return FileResponse(candidate)
        index = base / 'index.html'
        if index.is_file():
            return FileResponse(index)
        return error('Сначала соберите интерфейс: npm run build.', 503)
    return app
