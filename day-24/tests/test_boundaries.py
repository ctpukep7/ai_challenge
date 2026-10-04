import json
import sqlite3
from rag_core import with_boundary_context,GroundedRAG
from test_grounding import gen

def database(tmp_path):
    db=tmp_path/'index.sqlite'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE chunks(strategy,source,section,chunk_id,ordinal,text,vector)')
        rows=[('structure','day-07/README.md','Хранение','prev',0,'История не хранится в ',b'original-prev-vector'),
              ('structure','day-07/README.md','Хранение','c1',1,'localStorage. Она в SQLite.',b'original-selected-vector'),
              ('structure','day-07/README.md','Другая','different-section',2,'Другой раздел',b'other'),
              ('structure','day-07/README.md','Хранение','next',3,'История загружается после перезапуска.',b'next'),
              ('structure','day-08/README.md','Хранение','other-source',0,'unrelated',b'other'),
              ('fixed','day-07/README.md','Хранение','other-strategy',0,'unrelated',b'other')]
        c.executemany('INSERT INTO chunks VALUES(?,?,?,?,?,?,?)',rows)
    return db

def candidate():
    return {'source':'day-07/README.md','section':'Хранение','chunk_id':'c1','text':'localStorage. Она в SQLite.','score':.8765}

def test_boundaries_preserve_ranking_text_topk_and_vectors(tmp_path):
    db=database(tmp_path); selected=[candidate()]
    enriched=with_boundary_context(db,'structure',selected)
    assert len(enriched)==len(selected)==1
    assert all(enriched[0][k]==v for k,v in selected[0].items())
    assert enriched[0]['ordinal']==1 and enriched[0]['strategy']=='structure'
    assert 'boundary_context' not in selected[0]
    assert enriched[0]['boundary_context']['previous']['chunk_id']=='prev'
    assert enriched[0]['boundary_context']['next']['chunk_id']=='next'
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT vector FROM chunks WHERE chunk_id='c1'").fetchone()[0]==b'original-selected-vector'
    assert all(n['source']=='day-07/README.md' and n['strategy']=='structure' and n['section']=='Хранение'
               for n in enriched[0]['boundary_context'].values())

def test_partial_positive_quote_rejected_when_neighbor_negates(tmp_path):
    chunks=with_boundary_context(database(tmp_path),'structure',[candidate()])
    def verifier(messages):
        claim=json.loads(messages[-1]['content'])['claims'][0]
        assert claim['quote']=='localStorage'
        assert claim['selected_chunk_text']=='localStorage. Она в SQLite.'
        assert claim['boundary_context']['previous']['text']=='История не хранится в '
        return {'answer':json.dumps({'checks':[{'index':0,'status':'unsupported','reason':'Обрезана отрицательная конструкция'}]})}
    def generation(messages):
        assert 'prev' in {c['chunk_id'] for c in json.loads(messages[-1]['content'])['evidence_chunks']}
        return gen('История хранится в localStorage.','localStorage')
    result=GroundedRAG(lambda *a,**kw:chunks,generation,verifier).answer('День 7 история')
    assert result['status']=='unknown' and result['reason']=='semantic_rejected' and not result['sources']

def test_neighbor_quote_with_wrong_anchor_id_is_rejected(tmp_path):
    chunks=with_boundary_context(database(tmp_path),'structure',[candidate()])
    result=GroundedRAG(lambda *a,**kw:chunks,lambda _:gen('История не хранится в localStorage.','История не хранится в '),
        lambda _:(_ for _ in ()).throw(AssertionError('do not verify invalid quote'))).answer('День 7')
    assert result['reason']=='invalid_grounding'


def test_neighbor_quote_legal_only_under_disclosed_own_id(tmp_path):
    chunks=with_boundary_context(database(tmp_path),'structure',[candidate()])
    def generation(messages):
        prompt=json.loads(messages[-1]['content'])
        assert prompt['anchor_chunk_ids']==['c1']
        assert len(prompt['evidence_chunks'])==3
        prev=next(c for c in prompt['evidence_chunks'] if c['chunk_id']=='prev')
        assert prev['retrieval_role']=='neighbor' and prev['score'] is None and prev['anchor_chunk_id']=='c1'
        return gen('История не хранится в localStorage.','История не хранится в ','prev')
    def verifier(messages):
        claim=json.loads(messages[-1]['content'])['claims'][0]
        assert claim['selected_chunk_id']=='prev'
        assert claim['boundary_context']['next']['chunk_id']=='c1'
        return {'answer':json.dumps({'checks':[{'index':0,'status':'supported','reason':'Отрицание с продолжением'}]})}
    result=GroundedRAG(lambda *a,**kw:chunks,generation,verifier).answer('День 7 история')
    assert result['status']=='answered' and len(result['chunks'])==1 and len(result['evidence_chunks'])==3
    assert result['chunks'][0]['score']==.8765
    assert result['sources'][0]['retrieval_role']=='neighbor' and result['sources'][0]['chunk_id']=='prev'


def test_weak_anchor_never_expands_context_or_calls_provider(tmp_path):
    chunks=with_boundary_context(database(tmp_path),'structure',[{**candidate(),'score':.01}])
    result=GroundedRAG(lambda *a,**kw:chunks,lambda _:(_ for _ in ()).throw(AssertionError())).answer('День 7')
    assert result['reason']=='weak_context' and result['evidence_chunks']==[] and not result['messages']


def test_foreign_source_neighbor_rejected_before_generation():
    c={**candidate(),'boundary_context':{'previous':{'chunk_id':'foreign','source':'day-08/README.md',
        'strategy':'structure','section':'Хранение','text':'foreign'}}}
    result=GroundedRAG(lambda *a,**kw:[c],lambda _:(_ for _ in ()).throw(AssertionError())).answer('День 7')
    assert result['status']=='error' and result['evidence_chunks']==[]
