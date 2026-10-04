import pytest

from backend.app import create_app
from backend.day21_adapter import retrieve
from conftest import FakeEmbedder, completed
from fastapi.testclient import TestClient


@pytest.mark.parametrize('original, rewritten, expected_sources', [
    ('Что делает Day16 MCP?', 'Что делает Day7 MCP?', ['day-16/README.md']),
    ('Что делает Day16 MCP?', 'Что делает MCP?', ['day-16/README.md']),
    ('Что делает MCP?', 'Что делает Day16 MCP?', None),
])
def test_original_scope_survives_llm_rewrite_added_removed_or_changed_day(
        db, corpus, tmp_path, original, rewritten, expected_sources):
    calls, searches = [], []
    def retriever(query, strategy, top_k, path, cache, **kwargs):
        searches.append((query, kwargs['retrieval_scope']))
        return retrieve(query, strategy, top_k, path, cache, FakeEmbedder, corpus, **kwargs)
    def llm(messages):
        calls.append(messages)
        if len(calls) == 1:
            import json
            return {'answer': json.dumps({'query': rewritten})}
        return {'answer': 'Ответ [1]'}
    client = TestClient(create_app(db=db, corpus=corpus, cache=tmp_path / 'cache',
                                  retriever=retriever, llm=llm))
    response = client.post('/api/compare', json={'question': original, 'generate_answers': True,
                                                'rewrite_method': 'llm', 'threshold': -1,
                                                'top_k_after': 10})
    result = completed(client, response.json()['id'])
    assert result['status'] == 'completed'
    row = result['results'][0]
    assert searches[0][0] == original and searches[1][0] == rewritten
    assert searches[0][1] == searches[1][1]
    for mode in row['modes'].values():
        assert mode['retrieval_scope']['sources'] == expected_sources
        sources = {chunk['source'] for chunk in mode['chunks']}
        assert sources == (set(expected_sources) if expected_sources else {'README.md', 'day-16/README.md'})
        if expected_sources:
            assert 'source: README.md' not in mode['messages'][1]['content']
    assert len(calls) == 4


def test_unknown_explicit_day_policy_abstains_in_all_modes_no_llm(db, corpus, tmp_path):
    def retriever(query, strategy, top_k, path, cache, **kwargs):
        return retrieve(query, strategy, top_k, path, cache, FakeEmbedder, corpus, **kwargs)
    client = TestClient(create_app(db=db, corpus=corpus, cache=tmp_path / 'cache',
                                  retriever=retriever,
                                  llm=lambda messages: pytest.fail('No generation for unknown source')))
    response = client.post('/api/compare', json={'question': 'Что делает Day99 MCP?',
                                                'generate_answers': True, 'threshold': -1})
    result = completed(client, response.json()['id'])
    assert result['status'] == 'completed'
    for mode in result['results'][0]['modes'].values():
        assert mode['retrieval_scope']['sources'] == ['day-99/README.md']
        assert mode['chunks'] == []
        assert mode['answer_status'] == 'abstained'
        assert mode['generated_by'] == 'policy'
    assert result['summary']['calls']['total'] == 0
