import asyncio
import json
from pathlib import Path
import pytest
import httpx
from fastapi.testclient import TestClient
from assistant_core import ProviderError
from backend.app import create_app
from backend.agent import run_task
from backend.files import Files, PolicyError
from backend.provider import DeepSeek


@pytest.fixture
def root(tmp_path):
    for name in ['README.md', 'day-33/backend/app.py', 'day-33/frontend/src/App.vue', 'docs/support/faq.md']:
        path = tmp_path/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Первая строка\nAPI support\nПоследняя строка\n')
    return tmp_path


def reads(files):
    for name in ['README.md', 'day-33/backend/app.py', 'docs/support/faq.md']:
        files.read_file(name)


def test_real_search_read_list(root):
    files = Files(root)
    assert len(files.list_files()['files']) == 4
    assert len(files.search_text('api SUPPORT')['hits']) == 4
    assert len(files.search_text('API', prefix='docs/')['hits']) == 1
    source = files.read_file('README.md', 2, 2)
    assert source['id'] == 'S1' and source['text'] == 'API support\n'
    assert source['start_line'] == source['end_line'] == 2
    assert source['next_start_line'] == 3


@pytest.mark.parametrize('name', ['../README.md', '/README.md', 'docs/../README.md', 'docs//faq.md',
    '.env', '.git/config', 'wiki/topic.md', '.memory-bank/index.md', 'final week.md',
    'day-33/data/tickets.json', 'day-33/frontend/node_modules/x.js', 'day-33/backend/credentials.py',
    'day-33/backend/.hidden.py', 'day-33/backend/secret.py', 'day-33/backend/token.json'])
def test_private_scope(root, name):
    with pytest.raises(PolicyError):
        Files(root).read_file(name)


def test_symlink(root):
    (root/'docs/link.md').symlink_to(root/'README.md')
    with pytest.raises(PolicyError):
        Files(root).read_file('docs/link.md')
    assert 'docs/link.md' not in Files(root).list_files()['files']
    (root/'day-34').symlink_to(root/'docs', target_is_directory=True)
    with pytest.raises(PolicyError):
        Files(root).path('day-34/generated/new.md', write=True)


def test_content_constraints(root):
    files = Files(root)
    (root/'README.md').write_bytes(b'\xff')
    with pytest.raises(PolicyError): files.read_file('README.md')
    (root/'README.md').write_text('x'*200001)
    with pytest.raises(PolicyError): files.read_file('README.md')
    (root/'README.md').write_text('api_key = "abcdefgh1234567890"')
    with pytest.raises(PolicyError): files.read_file('README.md')
    assert files.search_text('api_key')['skipped_files'] == 1
    (root/'README.md').write_text('api_key = os.getenv("DEEPSEEK_API_KEY")')
    assert files.read_file('README.md')['text']


def test_read_limit_and_search_limit(root):
    (root/'README.md').write_text('API\n'*300)
    files = Files(root)
    source = files.read_file('README.md')
    assert source['end_line'] == 200 and source['truncated']
    assert files.search_text('API')['truncated']
    assert len(files.search_text('API')['hits']) == 100
    (root/'README.md').write_text('x'*21000)
    with pytest.raises(PolicyError): Files(root).read_file('README.md')


def test_stage_apply_and_update(root):
    files = Files(root)
    with pytest.raises(PolicyError): files.stage_file('day-34/generated/test.md', '# Test\n')
    reads(files)
    result = files.stage_file('day-34/generated/test.md', '# Test\n')
    assert result['saved'] is False and '/dev/null' in result['diff']
    assert not (root/'day-34/generated/test.md').exists()
    files.apply()
    assert (root/'day-34/generated/test.md').read_text() == '# Test\n'
    next_files = Files(root)
    reads(next_files)
    next_files.stage_file('day-34/generated/test.md', '# Updated')
    assert '\\ No newline at end of file' in next_files.change['diff']
    next_files.apply()
    assert (root/'day-34/generated/test.md').read_text() == '# Updated'


@pytest.mark.parametrize('path', ['README.md', 'day-34/generated/../README.md', 'day-34/generated/.hidden.md',
    'day-34/generated/sub/test.md', 'day-33/generated/test.md', 'day-34/generated/test.py'])
def test_write_scope(root, path):
    files = Files(root)
    reads(files)
    with pytest.raises(PolicyError): files.stage_file(path, 'Test')


@pytest.mark.parametrize('stale', ['source', 'target'])
def test_stale_guard(root, stale):
    files = Files(root)
    reads(files)
    files.stage_file('day-34/generated/test.md', 'new')
    if stale == 'source':
        (root/'README.md').write_text('changed')
    else:
        (root/'day-34/generated').mkdir(parents=True)
        (root/'day-34/generated/test.md').write_text('someone else')
    with pytest.raises(PolicyError): files.apply()


def test_noop_and_multiple_targets(root):
    files = Files(root)
    reads(files)
    path = root/'day-34/generated/test.md'
    path.parent.mkdir(parents=True)
    path.write_text('unchanged')
    assert files.stage_file('day-34/generated/test.md', 'unchanged')['unchanged']
    assert files.change is None
    files.stage_file('day-34/generated/test.md', 'change')
    files.stage_file('day-34/generated/other.md', 'other')
    assert len(files.changes) == 2
    files.stage_file('day-34/generated/test.md', 'unchanged')
    assert [change['path'] for change in files.changes] == ['day-34/generated/other.md']


def call(name, args, cid):
    return {'id': cid, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


class Scripted:
    def __init__(self, sequence):
        self.sequence = iter(sequence)
        self.messages = []
        self.calls = []

    async def complete(self, messages, tools):
        self.messages = list(messages)
        self.calls.append(tools)
        value = next(self.sequence)
        if isinstance(value, Exception): raise value
        return {'message': dict(role='assistant', **value)}


def sequence():
    return [
        {'tool_calls': [call('list_files', {'prefix': 'day-33/'}, 'list')]},
        {'tool_calls': [call('read_file', {'path': 'README.md'}, 'read1'),
                        call('read_file', {'path': 'day-33/backend/app.py'}, 'read2'),
                        call('read_file', {'path': 'docs/support/faq.md'}, 'read3')]},
        {'tool_calls': [call('stage_file', {'path': 'day-34/generated/test.md', 'content': '# Real proposal\n'}, 'stage')]},
        {'content': json.dumps({'status': 'completed', 'summary': 'Изучены файлы; подготовлен diff.', 'evidence_ids': ['S1', 'S2', 'S3']})}]


def task():
    return {'goal': 'Исследуй и подготовь документ', 'trace': [], 'sources': [], 'change': None, 'status': 'running',
            'metrics': {'llm_calls': 0, 'tool_calls': 0, 'elapsed_seconds': 0}}


def test_model_selects_real_tools(root):
    provider = Scripted(sequence())
    state = task()
    asyncio.run(run_task(state, Files(root), provider))
    assert state['status'] == 'completed'
    assert len(state['sources']) == 3 and len(state['trace']) == 5
    assert state['change']['path'] == 'day-34/generated/test.md'
    assert not (root/'day-34/generated/test.md').exists()
    tool_messages = [m for m in provider.messages if m['role'] == 'tool']
    assert [m['tool_call_id'] for m in tool_messages] == ['list', 'read1', 'read2', 'read3', 'stage']
    assert state['metrics']['llm_calls'] == 4


def test_schema_error_and_unknown(root):
    provider = Scripted([{'tool_calls': [call('read_file', {'path': 'README.md', 'shell': 'bad'}, 'bad')]},
        {'content': '{"status":"unknown","summary":"Нужны данные","evidence_ids":[]}'}])
    state = task()
    asyncio.run(run_task(state, Files(root), provider))
    assert state['status'] == 'unknown' and state['trace'][0]['status'] == 'error'
    assert not state['sources']


@pytest.mark.parametrize('reply', [ProviderError('Provider unavailable'), {'content': 'not JSON'},
    {'content': '{"status":"completed","summary":"Invented","evidence_ids":["S999"]}'},
    {'tool_calls': [call('list_files', {}, 'same'), call('list_files', {}, 'same')]}])
def test_provider_errors(root, reply):
    state = task()
    asyncio.run(run_task(state, Files(root), Scripted([reply])))
    assert state['status'] == 'error' and state['change'] is None


def test_budget(root):
    state = task()
    replies = [{'tool_calls': [call('list_files', {}, f'call{i}')]} for i in range(10)]
    asyncio.run(run_task(state, Files(root), Scripted(replies)))
    assert state['status'] == 'unknown' and state['metrics']['llm_calls'] == 10


def test_api_flow_idempotency(root):
    with TestClient(create_app(root, Scripted(sequence()))) as client:
        assert client.get('/api/project').json()['write_scope'] == 'day-34/generated/*.md'
        assert client.post('/api/tasks', json={'goal': ' '}).status_code == 422
        response = client.post('/api/tasks', json={'goal': 'Создай документ'})
        assert response.status_code == 202
        task_id = response.json()['id']
        for _ in range(50):
            state = client.get(f'/api/tasks/{task_id}').json()
            if state['status'] != 'running': break
        assert state['status'] == 'completed'
        assert client.get(f'/api/tasks/{task_id}/patch').status_code == 200
        first = client.post(f'/api/tasks/{task_id}/apply')
        assert first.status_code == 200 and first.json()['applied']
        assert client.post(f'/api/tasks/{task_id}/apply').status_code == 200
        assert client.get('/api/tasks/missing').status_code == 404


def test_api_conflict(root):
    class Slow:
        async def complete(self, messages, tools):
            await asyncio.sleep(10)
    with TestClient(create_app(root, Slow())) as client:
        first = client.post('/api/tasks', json={'goal': 'Start'})
        assert client.post('/api/tasks', json={'goal': 'Second'}).status_code == 409
        assert client.post(f"/api/tasks/{first.json()['id']}/apply").status_code == 409


def test_format_repair_preserves_real_evidence_and_disables_tools(root):
    replies = sequence()
    final = replies.pop()
    provider = Scripted(replies + [{'content': 'Готово, изучил файлы.'}, final])
    state = task()
    asyncio.run(run_task(state, Files(root), provider))
    assert state['status'] == 'completed' and state['format_repair_attempted']
    assert state['metrics']['llm_calls'] == 5
    assert provider.calls[-1] is None
    assert state['evidence_ids'] == ['S1', 'S2', 'S3']
    assert len(state['trace']) == 5
    assert state['change']
    assert 'Доступные реальные ID' in provider.messages[-1]['content']


@pytest.mark.parametrize('repair', [{'content': 'Still invalid'},
    {'content': '{"status":"completed","summary":"Invented","evidence_ids":["S999"]}'},
    {'tool_calls': [call('read_file', {'path': 'README.md'}, 'forbidden')]},
    ProviderError('Provider unavailable')])
def test_bad_repair_not_retried(root, repair):
    provider = Scripted([{'content': 'Invalid'}, repair])
    state = task()
    asyncio.run(run_task(state, Files(root), provider))
    assert state['status'] == 'error' and state['format_repair_attempted']
    assert state['metrics']['llm_calls'] == 2
    assert not state['trace'] and state['change'] is None


def test_network_error_not_repaired(root):
    provider = Scripted([ProviderError('No connection')])
    state = task()
    asyncio.run(run_task(state, Files(root), provider))
    assert state['status'] == 'error' and not state['format_repair_attempted']
    assert len(provider.calls) == 1


@pytest.mark.parametrize('with_tools', [True, False])
def test_provider_always_requests_json(monkeypatch, with_tools):
    from backend import provider as module
    observed = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            observed.append(kwargs['json'])
            return httpx.Response(200, request=httpx.Request('POST', url), json={
                'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{}'}}]})
    monkeypatch.setattr(module.httpx, 'AsyncClient', Client)
    asyncio.run(DeepSeek(api_key='test-key').complete([{'role': 'user', 'content': 'JSON'}], [{'type': 'function'}] if with_tools else None))
    assert observed[0]['max_tokens'] == (16000 if with_tools else 4000)
    assert observed[0]['response_format'] == {'type': 'json_object'}
    assert observed[0]['tool_choice'] == ('auto' if with_tools else 'none')


def test_multifile_create_upsert_update_and_modes(root):
    files = Files(root)
    reads(files)
    for i in range(3):
        files.stage_file(f'day-34/generated/doc{i}.md', f'# Document {i}\n')
    files.stage_file('day-34/generated/doc1.md', '# Revised\n')
    assert len(files.changes) == 3 and files.change is files.changes[0]
    assert files.changes[1]['after'] == '# Revised\n'
    files.apply()
    for change in files.changes:
        assert (root/change['path']).read_text() == change['after']
    paths = [root/f'day-34/generated/doc{i}.md' for i in range(2)]
    paths[0].chmod(0o640)
    paths[1].chmod(0o644)
    update = Files(root)
    reads(update)
    for i in range(5):
        update.stage_file(f'day-34/generated/doc{i}.md', 'new content')
    with pytest.raises(PolicyError, match='пять'):
        update.stage_file('day-34/generated/doc5.md', 'sixth')
    with pytest.raises(PolicyError, match='20 KB'):
        update.stage_file('day-34/generated/doc0.md', 'ж'*10001)
    update.apply()
    assert [path.stat().st_mode & 0o777 for path in paths] == [0o640, 0o644]


def multi_sequence():
    replies = sequence()
    replies[2]['tool_calls'].append(call('stage_file', {'path': 'day-34/generated/second.md', 'content': '# Second\n'}, 'stage2'))
    return replies


def wait_task(client, task_id):
    for _ in range(50):
        state = client.get(f'/api/tasks/{task_id}').json()
        if state['status'] != 'running': return state
    raise AssertionError('task did not finish')


def test_api_multiple_patch_apply_idempotent(root):
    with TestClient(create_app(root, Scripted(multi_sequence()))) as client:
        task_id = client.post('/api/tasks', json={'goal': 'Создай два документа'}).json()['id']
        state = wait_task(client, task_id)
        assert state['status'] == 'completed'
        assert len(state['changes']) == 2 and state['change'] == state['changes'][0]
        patch = client.get(f'/api/tasks/{task_id}/patch').text
        assert patch == ''.join(change['diff'] for change in state['changes'])
        assert 'b/day-34/generated/test.md' in patch and 'b/day-34/generated/second.md' in patch
        first = client.post(f'/api/tasks/{task_id}/apply')
        assert first.status_code == 200 and len(first.json()['changes']) == 2
        assert client.post(f'/api/tasks/{task_id}/apply').json() == first.json()
        assert (root/'day-34/generated/second.md').read_text() == '# Second\n'


def test_api_conflict_on_second_file_writes_nothing(root):
    with TestClient(create_app(root, Scripted(multi_sequence()))) as client:
        task_id = client.post('/api/tasks', json={'goal': 'Создай два документа'}).json()['id']
        wait_task(client, task_id)
        second = root/'day-34/generated/second.md'
        second.parent.mkdir(parents=True)
        second.write_text('User edits')
        response = client.post(f'/api/tasks/{task_id}/apply')
        assert response.status_code == 409 and 'second.md' in response.text
        assert not (second.parent/'test.md').exists()
        assert second.read_text() == 'User edits'
        assert not list(second.parent.glob('.day34-*'))


@pytest.mark.parametrize('existing', [True, False])
def test_replace_failure_rolls_back_bytes_and_modes(root, monkeypatch, existing):
    from backend import files as module
    directory = root/'day-34/generated'
    directory.mkdir(parents=True)
    first = directory/'first.md'
    if existing:
        first.write_bytes(b'old\r\nbytes\r\n')
        first.chmod(0o640)
    files = Files(root)
    reads(files)
    files.stage_file('day-34/generated/first.md', 'new first')
    files.stage_file('day-34/generated/second.md', 'new second')
    replace = module.os.replace
    def fail_second(source, target):
        if Path(target).name == 'second.md': raise OSError('simulated disk failure')
        return replace(source, target)
    monkeypatch.setattr(module.os, 'replace', fail_second)
    with pytest.raises(PolicyError, match='восстановлены'): files.apply()
    if existing:
        assert first.read_bytes() == b'old\r\nbytes\r\n'
        assert first.stat().st_mode & 0o777 == 0o640
    else:
        assert not first.exists()
    assert not (directory/'second.md').exists()
    assert not list(directory.glob('.day34-*'))


def test_rollback_failure_keeps_backup_for_recovery(root, monkeypatch):
    from backend import files as module
    directory = root/'day-34/generated'
    directory.mkdir(parents=True)
    (directory/'first.md').write_text('old')
    files = Files(root)
    reads(files)
    files.stage_file('day-34/generated/first.md', 'new')
    files.stage_file('day-34/generated/second.md', 'second')
    replace = module.os.replace
    calls = 0
    def fail_replace(source, target):
        nonlocal calls
        calls += 1
        if calls >= 2: raise OSError('disk offline')
        return replace(source, target)
    monkeypatch.setattr(module.os, 'replace', fail_replace)
    with pytest.raises(PolicyError, match='откат не завершён для: day-34/generated/first.md') as error:
        files.apply()
    backups = list(directory.glob('.day34-*'))
    assert len(backups) == 1 and backups[0].read_text() == 'old'
    assert str(backups[0]) in str(error.value)


@pytest.mark.parametrize('failure', ['unknown', 'provider', 'budget', 'stage'])
def test_staged_changes_clear_on_failed_completion(root, failure):
    replies = multi_sequence()[:-1]
    if failure == 'unknown':
        replies.append({'content': '{"status":"unknown","summary":"Partial","evidence_ids":["S1"]}'})
    elif failure == 'provider': replies.append(ProviderError('offline'))
    elif failure == 'budget':
        replies.extend({'tool_calls': [call('list_files', {}, f'extra{i}')]} for i in range(7))
    else:
        replies.append({'tool_calls': [call('stage_file', {'path': 'README.md', 'content': 'forbidden'}, 'badstage')]})
        replies.append(sequence()[-1])
    state = task()
    asyncio.run(run_task(state, Files(root), Scripted(replies)))
    assert state['status'] in {'unknown', 'error'}
    assert state['change'] is None and state['changes'] == []
    assert not (root/'day-34/generated/test.md').exists()


def test_all_temporary_files_prepared_before_first_replace(root, monkeypatch):
    from backend import files as module
    files = Files(root)
    reads(files)
    files.stage_file('day-34/generated/first.md', 'first')
    files.stage_file('day-34/generated/second.md', 'second')
    original = module.tempfile.mkstemp
    calls = 0
    def fail_preparation(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2: raise OSError('disk full')
        return original(*args, **kwargs)
    monkeypatch.setattr(module.tempfile, 'mkstemp', fail_preparation)
    with pytest.raises(PolicyError): files.apply()
    directory = root/'day-34/generated'
    assert not (directory/'first.md').exists() and not (directory/'second.md').exists()
    assert not list(directory.glob('.day34-*'))


def test_corrected_stage_error_can_finish(root):
    replies = sequence()
    replies.insert(2, {'tool_calls': [call('stage_file', {'path': 'day-34/generated/test.md', 'content': 'ж'*10001}, 'badstage')]})
    state = task()
    asyncio.run(run_task(state, Files(root), Scripted(replies)))
    assert state['status'] == 'completed' and len(state['changes']) == 1
    assert state['trace'][4]['status'] == 'error'
