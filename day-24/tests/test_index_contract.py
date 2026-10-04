import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import day21_adapter as adapter

def test_manifest_hash_mismatch_prevents_retrieval(tmp_path,monkeypatch):
    database=tmp_path/'index.sqlite';database.touch()
    monkeypatch.setattr(adapter,'_snapshot',lambda _root:([object()],[{'source':'README.md','title':'root','words':10001,'sections':1}],0,'new-hash'))
    monkeypatch.setattr(adapter,'read_manifest',lambda _:{'corpus_kind':adapter.CORPUS_KIND,'schema_version':1,
       'corpus_sha256':'old-hash','sources':['README.md'],'chunks':{'fixed':2,'structure':3}})
    state=adapter.index_state(database,tmp_path)
    assert state['ready'] is False and 'изменились' in state['reason']
    import pytest
    with pytest.raises(ValueError,match='изменились'):
        adapter.retrieve('question','structure',5,database,tmp_path,corpus=tmp_path,
            embedder_factory=lambda _:(_ for _ in ()).throw(AssertionError('stale index must not load model')))
