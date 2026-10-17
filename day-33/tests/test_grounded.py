import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from backend.grounded import checked_answer, grounded_answer, quote_catalog
from assistant_core import ProviderError
from test_support import appmod, fake_tools, index

SOURCES = [{'id': 's1', 'text': 'Точная справка.\nВторая строка.', 'path': 'faq.md'}]
CATALOG = quote_catalog(SOURCES)
VALID = {'status': 'answered', 'claims': [{'source_id': 's1', 'quote_id': 'q1', 'text': 'Проверьте справку.'}]}

class SequenceProvider:
    def __init__(self, *results):
        self.results = iter(results)
        self.payloads = []
    async def complete(self, messages):
        self.payloads.append(json.loads(messages[-1]['content']))
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result

def test_host_quote_expanded_and_legacy_supported():
    assert checked_answer(VALID, SOURCES, CATALOG)['sources'][0]['quote'] == SOURCES[0]['text']
    legacy = {'status': 'answered', 'claims': [{'source_id': 's1', 'quote': 'Вторая строка.', 'text': 'Проверьте справку.'}]}
    assert checked_answer(legacy, SOURCES, CATALOG)['status'] == 'answered'

@pytest.mark.parametrize('change', [
    {'quote_id': 'missing'}, {'quote_id': []}, {'source_id': 'other'},
    {'quote': 'Точная справка'}, {'quote': None}, {'source_id': None},
])
def test_no_guessing_or_conflicting_citations(change):
    with pytest.raises(ProviderError):
        checked_answer(dict(VALID, claims=[dict(VALID['claims'][0], **change)]), SOURCES, CATALOG)

@pytest.mark.parametrize('bad', [None, {'status': 'broken'}, {'status': 'answered', 'claims': {}},
                                dict(VALID, claims=[{'source_id': 's1', 'quote_id': 'missing', 'text': 'x'}])])
def test_validation_only_repair_same_context(bad):
    p = SequenceProvider(bad, VALID)
    answer = asyncio.run(grounded_answer('q', SOURCES, {'ticket': 'selected'}, list(range(12)), p))
    assert answer['validation'] == {'repair_attempted': True, 'repaired': True}
    assert len(p.payloads) == 2
    first, repair = p.payloads
    assert first['history'] == list(range(4, 12))
    for key in ('sources', 'quote_catalog', 'history', 'tool_context', 'question'):
        assert first[key] == repair[key]
    assert repair['failed_answer'] == bad and repair['repair'] is True


def test_normal_response_one_call():
    p = SequenceProvider(VALID)
    answer = asyncio.run(grounded_answer('q', SOURCES, {}, [], p))
    assert len(p.payloads) == 1
    assert answer['validation'] == {'repair_attempted': False, 'repaired': False}


def test_second_invalid_fails_without_third_call():
    p = SequenceProvider(None, None)
    with pytest.raises(ProviderError):
        asyncio.run(grounded_answer('q', SOURCES, {}, [], p))
    assert len(p.payloads) == 2


def test_provider_error_never_retried():
    p = SequenceProvider(ProviderError('network error'))
    with pytest.raises(ProviderError, match='network error'):
        asyncio.run(grounded_answer('q', SOURCES, {}, [], p))
    assert len(p.payloads) == 1


def test_empty_sources_without_generation():
    p = SequenceProvider()
    assert asyncio.run(grounded_answer('q', [], {}, [], p))['status'] == 'unknown'
    assert not p.payloads


def test_api_second_invalid_502(index):
    p = SequenceProvider(None, None)
    with TestClient(appmod.create_app(index, p, fake_tools)) as client:
        response = client.post('/api/support/ask', json={'ticket_id': 'AUTH-101', 'question': 'q'})
    assert response.status_code == 502
    assert len(p.payloads) == 2


def test_api_repair_preserves_ticket_and_mcp_trace(index):
    class CatalogProvider(SequenceProvider):
        async def complete(self, messages):
            payload = json.loads(messages[-1]['content'])
            self.payloads.append(payload)
            if len(self.payloads) == 1:
                return None
            entry = payload['quote_catalog'][0]
            return {'status': 'answered', 'claims': [{'source_id': entry['source_id'], 'quote_id': entry['quote_id'], 'text': 'Проверьте токен.'}]}
    p = CatalogProvider()
    with TestClient(appmod.create_app(index, p, fake_tools)) as client:
        response = client.post('/api/support/ask', json={'ticket_id': 'AUTH-101', 'question': 'q'})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['validation']['repaired']
    assert [t['tool'] for t in data['trace']] == ['list_tickets', 'get_ticket', 'get_ticket_user']
    for payload in p.payloads:
        assert payload['tool_context']['ticket']['id'] == 'AUTH-101'
        assert payload['tool_context']['user']['id'] == 'demo-01'


def test_repair_unknown_does_not_claim_confirmed_answer():
    p = SequenceProvider(None, {'status': 'unknown', 'clarification': 'Уточните шаги.'})
    answer = asyncio.run(grounded_answer('q', SOURCES, {}, [], p))
    assert answer['status'] == 'unknown' and answer['sources'] == []
    assert answer['validation'] == {'repair_attempted': True, 'repaired': False}
    assert len(p.payloads) == 2
