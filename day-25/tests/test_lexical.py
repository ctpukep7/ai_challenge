"""Lexical context stays bounded to real cosine-anchored sources."""
import json
import sqlite3
from pathlib import Path
import pytest
from backend.app import core, create_app
from backend.lexical import expand_context, tokens
from backend.day21_adapter import DB


def database(path):
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE chunks(strategy TEXT,chunk_id TEXT,source TEXT,title TEXT,section TEXT,ordinal INTEGER,text TEXT,token_count INTEGER,overlap_tokens INTEGER,crosses_sections INTEGER)')
        conn.executemany('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?)',[
            ('structure','anchor','day-10/README.md','Day 10','Настройки',2,'Параметры интерфейса и конфигурация.',5,0,0),
            ('structure','table','day-10/README.md','Day 10','Обзор',0,'Sliding Window отправляет последние N сохранённых сообщений и текущий вопрос.',12,0,0),
            ('structure','extra','day-10/README.md','Day 10','Другое',3,'Sliding Window не создаёт summary.',6,0,0),
            ('structure','noise','day-10/README.md','Day 10','Другое',4,'Настройки и интерфейс.',3,0,0),
            ('structure','otherfile','day-09/README.md','Day 9','Обзор',0,'Sliding Window отправляет последние сообщения.',7,0,0),
            ('fixed','otherstrategy','day-10/README.md','Day 10','Обзор',0,'Sliding Window отправляет последние сообщения.',7,0,0),
        ])
    return {'strategy':'structure','chunk_id':'anchor','source':'day-10/README.md','title':'Day 10','section':'Настройки','ordinal':2,'text':'Параметры интерфейса и конфигурация.','score':.8}


def test_real_day10_readonly_table_recall():
    if not DB.is_file(): pytest.skip('Локальный демонстрационный индекс отсутствует')
    with sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True) as conn:
        conn.row_factory=sqlite3.Row
        row=conn.execute("SELECT strategy,chunk_id,source,title,section,ordinal,text FROM chunks WHERE source='day-10/README.md' AND strategy='structure' AND section LIKE '%Настрой%' ORDER BY ordinal LIMIT 1").fetchone()
    anchor=dict(row)
    result=expand_context('В Day 10 можно обойтись без summary. Какие сообщения отправляет Sliding Window?','structure',[anchor],[anchor],db=DB)
    assert len(result)<=2
    assert any('| Sliding Window | Последние N сохранённых сообщений + текущий вопрос |' in chunk['text'] for chunk in result)
    assert all(c['source']=='day-10/README.md' and c['strategy']=='structure' and c['score'] is None and c['retrieval_role']=='lexical_context' and c['anchor_chunk_id']==anchor['chunk_id'] for c in result)


def test_bound_scope_strategy_dedup_noise_and_technical_guard(tmp_path):
    path=tmp_path/'index.sqlite';anchor=database(path)
    result=expand_context('Какие сообщения отправляет Sliding Window?','structure',[anchor],[anchor],db=path)
    assert {r['chunk_id'] for r in result}=={'table','extra'}
    assert all(r['source']==anchor['source'] and r['strategy']=='structure' for r in result)
    dedup=expand_context('Sliding Window','structure',[anchor],[anchor,*result],db=path)
    assert dedup==[]
    assert expand_context('Как в Day 10 это работает?','structure',[anchor],[anchor],db=path)==[]
    assert expand_context('Неизвестный технический термин','structure',[anchor],[anchor],db=path)==[]
    assert expand_context('Sliding','structure',[anchor],[anchor],db=path)==[] # common within this tiny corpus
    assert expand_context('Sliding Window','structure',[],[],db=path)==[]
    assert tokens('Сообщения сообщений')==['сообщен','сообщен']


def test_expander_no_weak_context_bypass_and_default_unchanged():
    calls=[]
    anchor={'chunk_id':'a','source':'README.md','section':'One','text':'Proof','score':.1}
    rag=core.GroundedRAG(lambda *a,**kw:[anchor],lambda m:(_ for _ in ()).throw(AssertionError('LLM called')),
        context_expander=lambda *a:calls.append(a))
    assert rag.answer('Question')['reason']=='weak_context'
    assert calls==[]
    assert core.GroundedRAG(lambda *a:[],lambda *a:None).context_expander is None


@pytest.mark.parametrize('invalid',[{'source':'other.md'},{'strategy':'fixed'},{'score':.7},{'anchor_chunk_id':'fake'},{'retrieval_role':'anchor'},{'text':''},{'ordinal':True}])
def test_sharedcore_rejects_scope_or_provenance_escape(invalid):
    anchor={'chunk_id':'a','source':'README.md','section':'One','text':'Proof','score':.8}
    extra={'chunk_id':'extra','source':'README.md','title':'Readme','section':'Two','text':'More proof','ordinal':1,'strategy':'structure','score':None,'retrieval_role':'lexical_context','anchor_chunk_id':'a',**invalid}
    rag=core.GroundedRAG(lambda *a,**kw:[anchor],lambda m:(_ for _ in ()).throw(AssertionError('Should not generate')),
        context_expander=lambda *a:[extra])
    result=rag.answer('Question')
    assert result['status']=='error' and result['sources']==[] and result['messages']==[]


def test_expander_limit_and_duplicate_are_enforced():
    anchor={'chunk_id':'a','source':'README.md','section':'One','text':'Proof','score':.8}
    extra={'chunk_id':'extra','source':'README.md','title':'Readme','section':'Two','text':'More proof','ordinal':1,'strategy':'structure','score':None,'retrieval_role':'lexical_context','anchor_chunk_id':'a'}
    def generation(messages):
        evidence=json.loads(messages[-1]['content'])['evidence_chunks']
        assert [c['chunk_id'] for c in evidence]==['a','extra']
        return {'answer':json.dumps({'status':'unknown','claims':[],'clarification':'Уточните'})}
    rag=core.GroundedRAG(lambda *a,**kw:[anchor],generation,context_expander=lambda *a:[extra,dict(extra)])
    assert rag.answer('Question')['reason']=='model_unknown'
    rag.context_expander=lambda *a:[extra]*3
    assert rag.answer('Question')['status']=='error'


def test_day25_publishes_supported_partial_claims_with_own_quotes(tmp_path):
    def retrieve(*a,**kw):
        return [{'chunk_id':'c','source':'README.md','section':'Proof','text':'SQLite хранит сообщения. Перезапуск восстанавливает историю.','score':.9}]
    def generation(messages):
        assert 'OWN exact quote' in messages[0]['content']
        return {'answer':json.dumps({'status':'answered','claims':[{'text':'SQLite хранит сообщения.','chunk_id':'c','quote':'SQLite хранит сообщения.'},{'text':'Любые настройки сохраняются.','chunk_id':'c','quote':'Перезапуск восстанавливает историю.'}],'clarification':''})}
    verifier=lambda messages:{'answer':json.dumps({'checks':[{'index':0,'status':'supported','reason':'Exact'},{'index':1,'status':'unsupported','reason':'Does not follow'}]})}
    app=create_app(chat_db=tmp_path/'chat.sqlite',db=tmp_path/'missing.sqlite',retriever=retrieve,llm=generation,verifier=verifier)
    result=app.state.service.rag.answer('Что сохраняется?')
    assert result['status']=='answered' and result['partial']
    assert len(result['sources'])==len(result['claims'])==1
    assert result['answer']=='SQLite хранит сообщения.'
    assert len(result['rejected_claims'])==1
