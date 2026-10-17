"""Exact citation recovery without paid calls or fictional MCP activity."""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from assistant_core import ProviderError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.answer_validation import inspect_answer
from backend.agent import Agent
from backend.mcp_session import ProjectSession
from test_agent import Provider, Session, tool, final


def draft(*claims):
    return {'status': 'answered', 'claims': list(claims)}


def claim(quote='main', sid='e1', text='Ветка main'):
    return {'text': text, 'source_id': sid, 'quote': quote}


def message(*claims):
    return {'role': 'assistant', 'content': json.dumps(draft(*claims))}


def run(messages, session=Session):
    provider = Provider(messages, dict(prompt_tokens=10, completion_tokens=5, total_tokens=15))
    return asyncio.run(Agent(provider, session).run('Вопрос', [])), provider


def test_bad_then_fixed_one_extra_final_turn_with_real_usage_and_safe_feedback():
    bad = message(claim(), claim('unconfirmed secret text'))
    result, provider = run([tool(), bad, final()])
    assert result['status'] == 'answered' and not result.get('partial')
    assert result['validation'] == {'repair_attempted': True, 'repaired': True, 'omitted_claims': 0, 'issues': []}
    assert result['metrics']['llm_calls'] == 3 and result['metrics']['total_tokens'] == 45
    assert result['metrics']['tool_calls'] == 1
    assert provider.messages[2][-2] == bad  # Actual assistant draft preserved.
    feedback = json.loads(provider.messages[2][-1]['content'])
    assert feedback['available_source_ids'] == ['e1']
    assert feedback['issues'] == [{'claim_index': 1, 'code': 'invalid_quote', 'source_id': 'e1'}]
    assert 'unconfirmed secret text' not in json.dumps(result)
    assert [t['method'] for t in result['trace']].count('tools/call') == 1
    assert result['trace'][-1]['status'] == 'repaired'


def test_repeated_bad_uses_only_latest_draft_valid_claims():
    result, _ = run([tool(), message(claim(text='OLD'), claim('bad')),
                    message(claim(text='LATEST'), claim('wrong'))])
    assert result['status'] == 'answered' and result['partial'] is True
    assert result['answer'] == 'LATEST' and result['sources'][0]['quote'] == 'main'
    assert result['validation']['omitted_claims'] == 1
    assert not result['validation']['repaired']
    assert any('неподтверждённые части не показаны' in w for w in result['warnings'])


def test_all_bad_unknown_and_only_one_repair():
    result, provider = run([tool(), message(claim('bad')), message(claim('still bad'))])
    assert len(provider.messages) == 3
    assert result['status'] == 'unknown' and result['sources'] == [] and result['partial']


def test_last_round_falls_back_without_seventh_call():
    result, _ = run([tool(cid=f'c{i}') for i in range(5)] + [message(claim(), claim('bad'))])
    assert result['status'] == 'answered' and result['partial']
    assert result['metrics']['llm_calls'] == 6
    assert not result['validation']['repair_attempted']


def test_repair_provider_failure_is_error_not_partial_and_no_transport_retry():
    result, provider = run([tool(), message(claim(), claim('bad')), ProviderError('DeepSeek недоступен')])
    assert result['status'] == 'error' and result['sources'] == []
    assert result['metrics']['llm_calls'] == 3 and result['metrics']['total_tokens'] is None
    assert len(provider.messages) == 3 and result['validation']['repair_attempted']


def test_repair_cannot_add_tools_or_model_turns():
    class NoToolsProvider(Provider):
        async def complete(self, messages, tools):
            if len(self.messages) == 2: assert tools is None
            return await super().complete(messages, tools)
    provider = NoToolsProvider([tool(), message(claim(), claim('bad')), tool(cid='never-run')])
    result = asyncio.run(Agent(provider, Session).run('Вопрос', []))
    assert result['partial'] and result['answer'] == 'Ветка main'
    assert result['metrics']['tool_calls'] == 1 and result['metrics']['llm_calls'] == 3
    assert any(t['status'] == 'repair_tool_request_rejected' for t in result['trace'] if t['method'] == 'answer/validation')


@pytest.mark.parametrize('bad', [[], {'status': 'invalid'}, {'status': 'answered', 'claims': {}},
                                {'status': 'answered', 'claims': [claim()] * 8},
                                {'status': 'unknown', 'clarification': ''}])
def test_malformed_envelope_never_becomes_partial(bad):
    with pytest.raises(ProviderError): inspect_answer(bad, [{'id': 'e1', 'text': 'main'}])


@pytest.mark.parametrize('bad,code', [
    (claim('main', 'e2'), 'invalid_quote'), (claim('other', 'e1'), 'invalid_quote'),
    (claim('main', 'invented'), 'unknown_source'), (claim(''), 'missing_quote'),
    (claim('main', text=' '), 'empty_text'), ('not a claim', 'malformed_claim'),
    (claim('npm run start'), 'invalid_quote'), (claim('    return 1'), 'invalid_quote'),
    (claim('**main**'), 'invalid_quote'), (claim('port 5031'), 'invalid_quote'),
])
def test_never_accept_wrong_source_whitespace_command_number_or_markdown(bad, code):
    sources = [{'id': 'e1', 'text': 'main\nnpm start\n  return 1\nport 5030'}, {'id': 'e2', 'text': 'other'}]
    result, issues = inspect_answer(draft(bad), sources)
    assert result['status'] == 'unknown' and result['sources'] == []
    expected = {'claim_index': 0, 'code': code}
    if isinstance(bad, dict) and bad.get('source_id') in {'e1','e2'}: expected['source_id'] = bad['source_id']
    assert issues == [expected]


def test_exact_quotes_unchanged_and_single_shared_source_for_multiple_claims():
    source = {'id': 'e1', 'text': 'main\nnpm start\n  return 1'}
    result, issues = inspect_answer(draft(claim(), claim('  return 1', text='код')), [source])
    assert not issues and result['status'] == 'answered'
    assert [s['quote'] for s in result['sources']] == ['main', '  return 1']


@pytest.mark.parametrize('fixed', [True, False])
@pytest.mark.parametrize('kind,expected', [('documentation', 'unknown'), ('code', 'answered'), ('git', 'answered')])
def test_stale_guard_after_repair_and_partial(fixed, kind, expected):
    class Stale(Session):
        async def call(self, *args):
            return {'index': {'ready': True, 'stale': True}, 'evidence': [{'kind': kind, 'text': 'main'}]}
        async def discover(self):
            return [{'type':'function','function':{'name':'search_documentation','parameters':{'type':'object','properties':{}}}}]
    second = final() if fixed else message(claim(), claim('bad'))
    result, _ = run([tool('search_documentation'), message(claim(), claim('bad')), second], Stale)
    assert result['status'] == expected
    if kind == 'documentation': assert not result['sources'] and 'Обновите индекс' in result['answer']


def test_real_stdio_discovery_read_evidence_repair_and_cleanup():
    class Scripted:
        def __init__(self): self.step = 0; self.evidence = None
        async def complete(self, messages, tools):
            self.step += 1
            if self.step == 1:
                return {'message': tool('read_project_file', json.dumps({'path':'day-31/backend/agent.py','start_line':1,'end_line':5}))}
            if self.step == 2:
                self.evidence = json.loads(messages[-1]['content'])['evidence'][0]
                return {'message': message(claim('made up quote', self.evidence['id']))}
            assert self.step == 3 and tools is None
            return {'message': message(claim(self.evidence['text'].splitlines()[0], self.evidence['id']))}
    provider = Scripted()
    result = asyncio.run(Agent(provider, ProjectSession).run('Прочитай реализацию', []))
    assert result['status'] == 'answered' and result['validation']['repaired']
    assert result['sources'][0]['quote'] in provider.evidence['text']
    assert result['metrics']['llm_calls'] == 3 and result['metrics']['tool_calls'] == 1
    assert result['trace'][0]['method'] == 'tools/list'


def test_malformed_repair_json_is_visible_error():
    result, _ = run([tool(), message(claim('bad')), {'role': 'assistant', 'content': 'not json'}])
    assert result['status'] == 'error' and result['sources'] == [] and result['metrics']['llm_calls'] == 3


def test_provider_disables_tools_for_single_repair_turn(monkeypatch):
    import httpx
    from backend.provider import DeepSeek
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            payload = kwargs['json']
            assert payload['tool_choice'] == 'none' and 'tools' not in payload
            return httpx.Response(200, request=httpx.Request('POST', url), json={
                'choices': [{'finish_reason': 'stop', 'message': final()}]})
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    response = asyncio.run(DeepSeek(api_key='test-only').complete([], None))
    assert response['message'] == final()


def test_bad_then_unknown_is_not_successful_citation_repair():
    result, _ = run([tool(), message(claim('bad')), {
        'role': 'assistant', 'content': json.dumps({'status': 'unknown', 'clarification': 'Не знаю: нужны дополнительные сведения'})}])
    assert result['status'] == 'unknown' and not result['sources']
    assert result['validation']['repair_attempted'] is True
    assert result['validation']['repaired'] is False
    assert result['trace'][-1] == {'method': 'answer/validation', 'status': 'unknown', 'issues': []}
    assert 'bad' not in json.dumps(result)
    assert result['metrics']['llm_calls'] == 3


from backend.answer_validation import repair_feedback

INVENTORY = '\n'.join([
    'day-31/README.md', 'day-31/backend/__init__.py', 'day-31/backend/agent.py',
    'day-31/backend/answer_validation.py', 'day-31/backend/app.py', 'day-31/backend/errors.py',
    'day-31/backend/mcp_server.py', 'day-31/backend/mcp_session.py', 'day-31/backend/project_tools.py',
    'day-31/backend/provider.py', 'day-31/backend/retrieval.py', 'day-31/frontend/index.html',
    'day-31/frontend/package-lock.json', 'day-31/frontend/package.json', 'day-31/frontend/src/app.js',
    'day-31/frontend/src/main.js', 'day-31/frontend/src/session.js', 'day-31/frontend/src/session.test.js',
    'day-31/frontend/src/style.css', 'day-31/frontend/vite.config.js', 'day-31/package.json',
    'day-31/scripts/dev.mjs', 'day-31/tests/test_agent.py', 'day-31/tests/test_answer_validation.py',
    'day-31/tests/test_core.py', 'day-31/tests/test_evaluation.py', 'day-31/tests/test_quality.py'])


def inventory_sources():
    return [{'id':'e2','kind':'documentation','path':'day-31/README.md','text':'Из day-31: npm run dev запускает API на 5030.'},
            {'id':'e8','kind':'code','path':None,'text':INVENTORY}]


def test_repair_catalog_exact_inventory_diversity_and_bounded_utf8():
    sources = inventory_sources()
    bad = draft(claim('day-31/backend/agent.py\nday-31/frontend/src/app.js','e8', 'backend и frontend'))
    _, issues = inspect_answer(bad, sources)
    feedback, catalog = repair_feedback(issues, sources, bad, 'Где backend и frontend Day31 и как запустить?')
    payload = json.loads(feedback)
    assert len(feedback.encode()) <= 2000 and len(INVENTORY.encode()) == 765
    assert any('/backend/' in item['quote'] for item in payload['quote_options'])
    assert any('/frontend/' in item['quote'] for item in payload['quote_options'])
    for option in payload['quote_options']:
        source = next(source for source in sources if source['id'] == option['source_id'])
        assert option['quote'] in source['text'] and len(option['quote'].strip()) >= 3
        assert catalog[option['quote_id']]['quote'] == option['quote']


@pytest.mark.parametrize('mutation,code', [
    ({'quote_id':'missing'}, 'unknown_quote_id'), ({'source_id':'e2'}, 'quote_source_mismatch'),
    ({'quote':'changed command 5031'}, 'quote_conflict'), ({'quote_id':[]}, 'unknown_quote_id'),
    ({'source_id':'unknown'}, 'unknown_source'),
])
def test_quote_reference_must_be_explicit_known_and_match_source_and_optional_quote(mutation, code):
    sources = inventory_sources()
    catalog = {'q1': {'source_id':'e8','quote':'day-31/backend/agent.py'}}
    selected = {'text':'Backend в day-31/backend', 'source_id':'e8', 'quote_id':'q1', **mutation}
    result, issues = inspect_answer(draft(selected), sources, catalog)
    assert result['status'] == 'unknown' and issues[0]['code'] == code


def test_quote_selection_materializes_unchanged_and_still_passes_shared_validator():
    sources = [{'id':'e1','text':'  return 1\nmain'}]
    catalog = {'q1':{'source_id':'e1','quote':'  return 1'}}
    selected = {'text':'Код', 'source_id':'e1', 'quote_id':'q1'}
    result, issues = inspect_answer(draft(selected), sources, catalog)
    assert not issues and result['sources'][0]['quote'] == '  return 1'
    assert 'quote' not in selected  # No mutation of original model draft.
    catalog['q1']['quote'] = '    return 1'
    result, issues = inspect_answer(draft(selected), sources, catalog)
    assert result['status'] == 'unknown' and issues[0]['code'] == 'invalid_quote'
    # A quote ID without the actual feedback catalog cannot validate itself.
    result, issues = inspect_answer(draft(selected), sources)
    assert result['status'] == 'unknown' and issues[0]['code'] == 'unknown_quote_id'


def test_inventory_failure_can_be_repaired_by_two_explicit_separate_quote_choices():
    class InventorySession(Session):
        async def call(self, *args): return {'evidence':[dict(s, id='ignored') for s in inventory_sources()]}
    class CatalogProvider:
        def __init__(self): self.step = 0
        async def complete(self, messages, tools):
            self.step += 1
            if self.step == 1: return {'message':tool()}
            if self.step == 2:
                return {'message':message(claim('day-31/backend/agent.py\nday-31/frontend/src/app.js', 'e2', 'Backend и frontend'))}
            assert self.step == 3 and tools is None
            options = json.loads(messages[-1]['content'])['quote_options']
            selected = [next(option for option in options if '/backend/' in option['quote']),
                        next(option for option in options if '/frontend/' in option['quote'])]
            claims = [{'text':text, 'source_id':option['source_id'], 'quote_id':option['quote_id']}
                      for text,option in zip(['Backend в day-31/backend', 'Frontend в day-31/frontend'],selected)]
            claims.append(claim('npm run dev', 'e1', 'Запуск: npm run dev'))
            return {'message':message(*claims)}
    provider = CatalogProvider()
    result = asyncio.run(Agent(provider, InventorySession).run('Где backend и frontend Day31 и как запустить?', []))
    assert result['status'] == 'answered' and not result.get('partial') and result['validation']['repaired']
    assert result['metrics']['llm_calls'] == 3 and result['metrics']['tool_calls'] == 1
    assert len(result['sources']) == 3
    assert all(source['quote'] in source['text'] for source in result['sources'])


def test_catalog_feedback_stays_bounded_for_large_ledger_and_preserves_whitespace():
    sources = [{'id':f'e{i}', 'kind':'code','path':'a.py','text':'  return 1\n' + ('Публичная строка ' * 100)} for i in range(300)]
    bad = draft(claim('wrong', 'e299'))
    feedback, catalog = repair_feedback([{'claim_index':0,'code':'invalid_quote'}], sources, bad)
    assert len(feedback.encode()) <= 2000
    assert catalog and any(option['source_id'] == 'e299' for option in catalog.values())
    assert all(option['quote'] in sources[int(option['source_id'][1:])]['text'] for option in catalog.values())
