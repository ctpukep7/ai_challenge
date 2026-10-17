from pathlib import Path
import asyncio, json, os, threading
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict, model_validator
from dotenv import load_dotenv
from assistant_core import Index, FastEmbedder, DeepSeek, ProviderError
from assistant_core.mcp_client import call_tools
from backend.grounded import grounded_answer
DAY=Path(__file__).resolve().parents[1]; ROOT=DAY.parent
SUPPORT_PATHS=['docs/support/faq.md','docs/support/troubleshooting.md','docs/support/privacy.md']
class Turn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ticket_id:str=Field(pattern=r'^[A-Z]+-\d{3}$')
    role:str=Field(pattern='^(user|assistant)$')
    content:str=Field(max_length=6000)
class AskRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ticket_id:str=Field(pattern=r'^[A-Z]+-\d{3}$')
    question:str=Field(min_length=1,max_length=4000)
    history:list[Turn]=Field(default_factory=list,max_length=8)
    @model_validator(mode='after')
    def scoped_pairs(self):
        if any(t.ticket_id!=self.ticket_id for t in self.history): raise ValueError('История относится к другому тикету')
        if len(self.history)%2 or any(t.role!=('user' if i%2==0 else 'assistant') for i,t in enumerate(self.history)):
            raise ValueError('История должна содержать до четырёх полных пар')
        return self

def create_app(index=None,provider=None,mcp_call=None):
    load_dotenv(DAY/'.env')
    app=FastAPI(title='Day33 — Поддержка пользователей')
    lock=asyncio.Lock(); init_lock=threading.Lock(); state={'index':index}
    provider=provider or DeepSeek(); mcp_call=mcp_call or call_tools
    def get_index():
        with init_lock:
            if state['index'] is None:
                state['index']=Index(DAY/'data/index.sqlite',ROOT,SUPPORT_PATHS,FastEmbedder(Path(os.getenv('ASSISTANT_EMBED_CACHE',str(ROOT/'day-21/.cache/fastembed')))))
        return state['index']
    async def selected(ticket_id):
        data,trace=await mcp_call(DAY/'backend/mcp_server.py',[('get_ticket',{'ticket_id':ticket_id}),('get_ticket_user',{'ticket_id':ticket_id})])
        ticket=data.get('get_ticket',{}); user=data.get('get_ticket_user',{})
        if ticket.get('id')!=ticket_id or user.get('id')!=ticket.get('user_id'): raise RuntimeError('Invalid MCP relationship')
        return {'ticket':ticket,'user':user},trace
    @app.get('/api/tickets')
    async def tickets():
        try:
            data,trace=await mcp_call(DAY/'backend/mcp_server.py',[('list_tickets',{})])
            return dict(data['list_tickets'],trace=trace)
        except Exception: raise HTTPException(503,'MCP недоступен: не удалось получить тикеты.')
    @app.get('/api/index')
    async def status():
        try: return await asyncio.to_thread(lambda:get_index().status())
        except Exception: raise HTTPException(503,'Не удалось открыть индекс или локальную embedding-модель. Проверьте кэш.')
    @app.post('/api/index/build')
    async def build():
        if lock.locked(): raise HTTPException(409,'Запрос уже выполняется')
        async with lock:
            try: return await asyncio.to_thread(lambda:get_index().build())
            except Exception: raise HTTPException(503,'Не удалось построить индекс поддержки. Проверьте кэш и документы.')
    @app.post('/api/support/ask')
    async def ask(req:AskRequest):
        if not req.question.strip(): raise HTTPException(422,'Введите вопрос')
        if lock.locked(): raise HTTPException(409,'Запрос уже выполняется')
        async with lock:
            try:
                listed,list_trace=await mcp_call(DAY/'backend/mcp_server.py',[('list_tickets',{})])
                if req.ticket_id not in {t['id'] for t in listed['list_tickets']['tickets']}: raise HTTPException(404,'Неизвестный тикет')
                context,trace=await selected(req.ticket_id)
                trace=list_trace+trace
            except HTTPException: raise
            except Exception: raise HTTPException(503,'MCP недоступен: сведения выбранного тикета неизвестны.')
            try:
                idx=get_index(); index_status=await asyncio.to_thread(idx.status)
                if not index_status['ready']: raise HTTPException(409,'Сначала постройте индекс')
                if index_status['stale']: raise HTTPException(409,'Документы изменились: обновите индекс')
                question=req.question.strip()
                # Ticket facts refine retrieval, but remain distinct from cited documentation.
                query=question+' '+context['ticket']['title']+' '+' '.join(context['ticket']['facts'])
                sources=await asyncio.to_thread(idx.search,query)
                sources=await asyncio.to_thread(idx.expand_neighbors,sources)
                if context['ticket'].get('needs_details') is True and not req.history:
                    return {'status':'unknown','reason':'missing_ticket_details',
                            'answer':'Не знаю причину по имеющимся данным тикета. Уточните номер дня, адрес сервиса без токена, точный текст ошибки и шаг воспроизведения. Секреты присылать не нужно.',
                            'sources':[],'ticket_id':req.ticket_id,'context':context,'trace':trace,'index':index_status}
                history=[{'role':t.role,'content':t.content} for t in req.history]
                tailored=question+'\nПодбери рекомендации для выбранного тикета по его наблюдениям. Не объявляй непроверенную причину установленной. При нехватке сведений уточни код ошибки и шаг воспроизведения. Не повторяй данные тикета в документальных утверждениях.'
                answer=await grounded_answer(tailored,sources,context,history,provider)
                return dict(answer,ticket_id=req.ticket_id,context=context,trace=trace,index=index_status)
            except HTTPException: raise
            except ProviderError as exc: raise HTTPException(502,str(exc))
            except Exception: raise HTTPException(503,'Ошибка локального поиска. Проверьте индекс и кэш модели.')
    if (DAY/'frontend/dist').exists(): app.mount('/',StaticFiles(directory=DAY/'frontend/dist',html=True),name='ui')
    return app
app=create_app()
