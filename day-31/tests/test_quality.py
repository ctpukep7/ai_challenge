"""Regressions for the observed stdio masking and demonstration UX."""
import asyncio
import importlib
import json
import sys
from pathlib import Path

import httpx
import pytest
from assistant_core import FastEmbedder, Index, ProviderError

DAY = Path(__file__).resolve().parents[1]
ROOT = DAY.parent
sys.path.insert(0, str(DAY))
from backend.agent import Agent, OVERVIEW
from backend.errors import error_result, public_errors
from backend.mcp_session import ProjectSession
from backend.provider import DeepSeek
from backend.retrieval import search_sources
from backend import project_tools


class BrokenProvider:
    async def complete(self, messages, tools):
        raise ProviderError('DeepSeek недоступен: не удалось установить соединение')


def test_real_stdio_preserves_provider_error_and_metrics():
    # Actual AnyIO stdio managers produce nested groups on body failure.
    result = asyncio.run(Agent(BrokenProvider(), ProjectSession).run('Вопрос', []))
    assert result['status'] == 'error'
    assert result['answer'] == 'DeepSeek недоступен: не удалось установить соединение'
    assert result['error_codes'] == ['provider']
    assert result['trace'][0]['method'] == 'tools/list'
    assert result['metrics']['llm_calls'] == 1
    assert result['metrics']['llm'][0]['error'] is True
    assert result['metrics']['total_tokens'] is None


def test_nested_and_mixed_groups_preserve_every_independent_cause():
    grouped = ExceptionGroup('outer raw text', [
        ExceptionGroup('inner raw text', [ProviderError('Ответ содержит неподтверждённую ссылку или цитату')]),
        TimeoutError('private raw timeout'), ValueError('never disclose internal payload'),
    ])
    result = error_result(grouped)
    assert result['error_codes'] == ['model_output', 'timeout', 'internal']
    assert 'цитату' in result['answer'] and 'Истекло' in result['answer'] and 'журнал' in result['answer']
    assert all(secret not in result['answer'] for secret in ['private', 'payload', 'raw text'])


@pytest.mark.parametrize('exc', [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()])
def test_control_flow_is_not_converted_to_answer(exc):
    group = BaseExceptionGroup('control flow', [ProviderError('safe diagnosis'), exc])
    with pytest.raises(type(exc)):
        public_errors(group)


def test_cancelled_agent_request_propagates():
    class Cancelled:
        async def complete(self, *args): raise asyncio.CancelledError()
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def discover(self): return [{'type': 'function', 'function': {'name': 'git_branch', 'parameters': {}}}]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Agent(Cancelled(), Session).run('Вопрос', []))


@pytest.mark.parametrize('status,part,code', [(401, 'доступ', 'configuration'), (402, 'средств', 'provider'), (429, 'лимит', 'provider'), (503, 'HTTP 503', 'provider')])
def test_provider_http_failure_is_safe_and_specific(monkeypatch, status, part, code):
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            return httpx.Response(status, request=httpx.Request('POST', url), text='private provider body')
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    with pytest.raises(ProviderError) as raised:
        asyncio.run(DeepSeek(api_key='test-only').complete([], []))
    result = error_result(raised.value)
    assert part in result['answer'] and result['error_codes'] == [code]
    assert 'private provider body' not in result['answer']


def test_overview_is_model_answer_with_separate_host_suggestions():
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def discover(self): return [{'type': 'function', 'function': {'name': 'git_branch', 'parameters': {}}}]
    class Provider:
        async def complete(self, messages, tools):
            assert OVERVIEW in messages[0]['content']
            assert 'scope="project"' in messages[0]['content']
            return {'message': {'role': 'assistant', 'content': json.dumps({'status': 'unknown', 'clarification': 'Нет источников'})}}
    result = asyncio.run(Agent(Provider(), Session).run('Обзор', [], overview=True))
    assert result['status'] == 'unknown' and result['sources'] == []
    assert len(result['suggestions']) == 3
    assert result['suggestions'][0] not in result['answer']


def test_range_is_distinct_from_byte_limit(tmp_path):
    folder = tmp_path / 'day-31/backend'
    folder.mkdir(parents=True)
    (folder / 'a.py').write_text('line\n' * 10)
    selected = project_tools.read_project_file('day-31/backend/a.py', 2, 3, root=tmp_path)
    assert selected['truncated'] is True and selected['byte_truncated'] is False
    assert selected['returned_start_line'] == 2 and selected['returned_end_line'] == 3
    assert selected['total_lines'] == 10
    (folder / 'a.py').write_text('a' * 9000 + '\n' + 'b' * 9000 + '\n')
    limited = project_tools.read_project_file('day-31/backend/a.py', 1, 2, root=tmp_path)
    assert limited['byte_truncated'] is True and limited['returned_end_line'] == 1


def test_scope_does_not_invent_diversity_below_threshold():
    class Fake:
        def search(self, query, top_k=5, threshold=.35, source_paths=None):
            if source_paths == ['README.md']: return []
            return [{'id': 'docs', 'path': 'docs/a.md', 'score': .4}]
        def expand_neighbors(self, sources): return sources
    result = search_sources(Fake(), 'query', ['README.md', 'docs/a.md'], scope='project')
    assert [source['id'] for source in result] == ['docs']
    with pytest.raises(ValueError):
        search_sources(Fake(), 'query', [], day=31, scope='project')


def test_real_embeddings_project_scope_and_staleness(tmp_path):
    # Existing cached weights only. Real texts catch the reported Day12 drift.
    paths = ['README.md', 'docs/architecture.md', 'docs/project-map.md', 'day-12/README.md']
    for path in paths:
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text((ROOT / path).read_text())
    embedder = FastEmbedder(ROOT / 'day-21/.cache/fastembed')
    index = Index(tmp_path / 'index.sqlite', tmp_path, paths, embedder)
    index.build()
    result = search_sources(index, 'структура проекта запуск архитектура RAG', paths, scope='project')
    assert result and all(source['path'] == 'README.md' or source['path'].startswith('docs/') for source in result)
    assert any(source['path'] == 'README.md' for source in result)
    assert any(source['path'].startswith('docs/') for source in result)
    assert len([source for source in result if source['retrieval_role'] == 'anchor']) <= 5
    assert len([source for source in result if source['retrieval_role'] == 'neighbor']) <= 2
    day_sources = search_sources(index, 'Персонализация ассистента', paths, day=12)
    assert any(source['path'] == 'day-12/README.md' for source in day_sources)
    (tmp_path / 'README.md').write_text('changed')
    with pytest.raises(ValueError, match='устарел'):
        search_sources(index, 'query', paths, scope='project')


def test_mcp_scope_validates_before_index_and_keeps_stale_explicit(monkeypatch):
    monkeypatch.syspath_prepend(str(DAY / 'backend'))
    server = importlib.import_module('mcp_server')
    class Stale:
        def status(self): return {'ready': True, 'stale': True}
        def search(self, *args, **kwargs): raise AssertionError('stale index must not be searched')
    monkeypatch.setattr(server, '_index', Stale())
    conflict = server.search_documentation('query', day=31, scope='project')
    assert conflict.get('error') and not conflict['evidence']
    stale = server.search_documentation('query', scope='project')
    assert stale['index']['stale'] and not stale['evidence']
    assert 'устарел' in stale['error']


@pytest.mark.parametrize('state', [{'ready': False, 'stale': False}, {'ready': True, 'stale': True}])
@pytest.mark.parametrize('kind,expected', [('documentation', 'unknown'), ('code', 'answered'), ('git', 'answered')])
def test_failed_rag_cannot_be_bypassed_with_readme_but_git_and_code_remain_available(state, kind, expected):
    def request(name, cid):
        return {'role': 'assistant', 'tool_calls': [{'id': cid, 'type': 'function', 'function': {'name': name, 'arguments': '{}'}}]}
    responses = iter([request('search_documentation', 'search1'), request('read_project_file', 'read1'),
        {'role': 'assistant', 'content': json.dumps({'status': 'answered', 'claims': [{'text': 'Факт', 'source_id': 'e1', 'quote': 'fact'}]})}])
    class Provider:
        async def complete(self, *args): return {'message': next(responses)}
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def discover(self):
            return [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object', 'properties': {}}}} for name in ['search_documentation', 'read_project_file']]
        async def call(self, name, args):
            if name == 'search_documentation': return {'error': 'Обновите индекс', 'index': state, 'evidence': []}
            return {'evidence': [{'kind': kind, 'text': 'fact', 'path': 'README.md' if kind == 'documentation' else 'a.py'}]}
    result = asyncio.run(Agent(Provider(), Session).run('Что в проекте?', []))
    assert result['status'] == expected
    assert len(result['trace']) == 3 and result['metrics']['tool_calls'] == 2
    if kind == 'documentation':
        assert result['sources'] == [] and 'Обновите индекс' in result['answer']


@pytest.mark.parametrize('question,overview', [('/help', True), ('/help Где код?', False), ('Где код?', False)])
def test_api_overview_flag_does_not_change_question_tool_routing(question, overview):
    from fastapi.testclient import TestClient
    from backend.app import create_app
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def discover(self): return [{'type': 'function', 'function': {'name': 'git_branch', 'parameters': {}}}]
    class Provider:
        async def complete(self, messages, tools):
            assert (OVERVIEW in messages[0]['content']) is overview
            if not overview: assert messages[-1]['content'] == 'Где код?'
            return {'message': {'role': 'assistant', 'content': '{"status":"unknown","clarification":"Уточните"}'}}
    with TestClient(create_app(provider=Provider(), session_factory=Session)) as client:
        result = client.post('/api/help', json={'question': question}).json()
    assert len(result['suggestions']) == (3 if overview else 0)
    assert result['metrics']['tool_calls'] == 0
