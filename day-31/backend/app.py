from pathlib import Path
import asyncio, os, threading
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict, model_validator
from dotenv import load_dotenv
from assistant_core import Index, FastEmbedder, documentation_paths
from .provider import DeepSeek
from .agent import Agent
from .mcp_session import ProjectSession
DAY=Path(__file__).resolve().parents[1]; ROOT=DAY.parent
class Turn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    role: str=Field(pattern='^(user|assistant)$')
    content: str=Field(max_length=6000)
class HelpRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    question: str=Field(min_length=1,max_length=4000)
    history: list[Turn]=Field(default_factory=list,max_length=8)
    @model_validator(mode='after')
    def complete_pairs(self):
        if len(self.history)%2 or any(t.role != ('user' if i%2==0 else 'assistant') for i,t in enumerate(self.history)):
            raise ValueError('История должна содержать до четырёх полных пар user/assistant')
        return self
def create_app(index=None,provider=None,session_factory=None):
    load_dotenv(DAY/'.env')
    app=FastAPI(title='Day31 — Ассистент разработчика'); lock=asyncio.Lock()
    init_lock=threading.Lock()
    state={'index':index}; provider=provider or DeepSeek(); agent=Agent(provider, session_factory or ProjectSession)
    def get_index():
        with init_lock:
            if state['index'] is None:
                cache=Path(os.getenv('ASSISTANT_EMBED_CACHE',str(ROOT/'day-21/.cache/fastembed')))
                state['index']=Index(DAY/'data/index.sqlite',ROOT,lambda:documentation_paths(ROOT),FastEmbedder(cache))
        return state['index']
    @app.get('/api/index')
    async def status():
        try: return await asyncio.to_thread(lambda:get_index().status())
        except Exception: raise HTTPException(503,'Не удалось открыть локальную embedding-модель или индекс. Проверьте кэш и README.')
    @app.post('/api/index/build')
    async def build():
        if lock.locked(): raise HTTPException(409,'Запрос уже выполняется')
        async with lock:
            try: return await asyncio.to_thread(lambda:get_index().build())
            except Exception: raise HTTPException(503,'Не удалось построить индекс. Проверьте локальный кэш модели и источники.')
    @app.post('/api/help')
    async def help(req:HelpRequest):
        if lock.locked(): raise HTTPException(409,'Запрос уже выполняется')
        async with lock:
            question=req.question.strip()
            overview=question=='/help'
            if overview: question='Кратко объясни структуру проекта AI Challenge по корневому README и docs.'
            elif question.startswith('/help '): question=question[6:].strip()
            if not question: raise HTTPException(422,'Введите вопрос')
            return await agent.run(question,[t.model_dump() for t in req.history],overview=overview)
    if (DAY/'frontend/dist').exists(): app.mount('/',StaticFiles(directory=DAY/'frontend/dist',html=True),name='ui')
    return app
app=create_app()
