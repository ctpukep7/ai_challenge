import asyncio
import base64
import copy
import json
from pathlib import Path
import subprocess

import httpx
import pytest
from assistant_core.provider import DeepSeek, ProviderError
from day32_review.review import (GitHub, MARKER, allowed, batches, collect, generate,
                                 markdown, publish, validate_finding)


def run(value):
    return asyncio.run(value)


def pr(sha='a' * 40, fork=False, count=1):
    return {'base': {'sha': 'b' * 40}, 'head': {'sha': sha, 'repo': {'full_name': 'other/repo' if fork else 'owner/repo'}}, 'changed_files': count}


def file(path='app.py', status='modified'):
    return {'filename': path, 'status': status, 'patch': '@@ -1 +1 @@\n-old\n+new'}


class Fixture:
    def __init__(self, rows=None, fork=False, comments=None):
        self.rows = rows or [file()]
        self.current = pr(fork=fork, count=len(self.rows))
        self.calls = []
        self.comments = comments or []

    def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        if request.method in ('POST', 'PATCH'):
            return httpx.Response(200, json={'id': 99})
        if path.endswith('/files'):
            page = int(request.url.params.get('page', 1))
            return httpx.Response(200, json=self.rows[(page - 1) * 100:page * 100])
        if path.endswith('/comments'):
            return httpx.Response(200, json=self.comments)
        if '/contents/' in path:
            text = 'old' if request.url.params['ref'] == 'b' * 40 else 'new'
            return httpx.Response(200, json={'type': 'file', 'size': len(text), 'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()})
        return httpx.Response(200, json=self.current)


def api(fixture):
    return GitHub('owner/repo', 'fake-token', transport=httpx.MockTransport(fixture))


def snapshot():
    return {'mode': 'fixture', 'status': 'collected', 'repo': 'owner/repo', 'number': 1,
            'head_sha': 'a' * 40, 'base_sha': 'b' * 40, 'total_files': 1, 'skipped': [],
            'files': [{'path': 'app.py', 'previous_path': 'app.py', 'status': 'modified',
                       'patch': '@@ -1 +1 @@\n-old\n+new', 'base_text': 'old', 'head_text': 'new', 'truncated': False}]}


def finding(**kwargs):
    return dict({'category': 'bug', 'severity': 'P2', 'title': 'Ошибка', 'explanation': 'Проверить значение',
                 'file': 'app.py', 'side': 'head', 'line': 1, 'evidence': 'new', 'hypothesis': True}, **kwargs)


class Embedder:
    def offsets(self, text):
        return [(i, i + 1) for i in range(len(text))]

    def embed(self, texts):
        return [[1., 0.] for text in texts]


class Provider:
    def __init__(self):
        self.messages = []

    async def complete(self, messages):
        self.messages.append(messages)
        return {'findings': [finding(), finding(line=200)]}


def test_paginated_collection_and_bounded_large_pr():
    fixture = Fixture([file(f'file{i}.py') for i in range(101)])
    result = run(collect(api(fixture), 1))
    assert any(r.url.params.get('page') == '2' for r in fixture.calls)
    assert len(result['files']) == 60
    assert len(result['skipped']) == 41
    assert sum(sum(len(x[k]) for k in ('patch', 'base_text', 'head_text')) for x in result['files']) <= 100000
    assert all(r.method == 'GET' for r in fixture.calls)


def test_fork_no_contents_requests():
    fixture = Fixture(fork=True)
    assert run(collect(api(fixture), 1))['status'] == 'skipped'
    assert len(fixture.calls) == 1


def test_rename_and_delete_read_correct_paths():
    renamed = dict(file('new.py', 'renamed'), previous_filename='old.py')
    fixture = Fixture([renamed, file('deleted.py', 'removed')])
    result = run(collect(api(fixture), 1))
    requests = [str(r.url) for r in fixture.calls if '/contents/' in str(r.url)]
    assert any('/old.py?' in x and 'b' * 40 in x for x in requests)
    assert not any('/deleted.py?' in x and 'a' * 40 in x for x in requests)
    assert result['files'][0]['previous_path'] == 'old.py'
    assert validate_finding(finding(file='deleted.py', side='base', evidence='old'), result['files'])['side'] == 'base'


@pytest.mark.parametrize('change', [{'line': 99}, {'evidence': 'invented'}, {'file': '../.env'}, {'side': 'other'}, {'line': True}, {'hypothesis': 'yes'}, {'category': []}])
def test_bad_findings_rejected(change):
    with pytest.raises(ValueError):
        validate_finding(finding(**change), snapshot()['files'])


def test_allowlist_and_no_execution(tmp_path):
    for name in ['.env', 'wiki/key.md', 'day-31/node_modules/code.js', '../file.py', 'docs/credentials.json', 'day-31/package-lock.json']:
        assert not allowed(name)
    assert allowed('day-31/backend/app.py')
    assert allowed('docs/architecture.md')
    source = Path(__file__).parents[1].joinpath('day32_review/review.py').read_text()
    assert "['git', '-C', str(root), 'ls-files', '-z']" in source
    assert 'exec(' not in source


def test_generate_real_shared_rag_with_fixture_provider(tmp_path):
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs' / 'api.md').write_text('API response must contain status.')
    (tmp_path / 'README.md').write_text('Project API documentation')
    provider = Provider()
    result = run(generate(snapshot(), tmp_path, tmp_path / 'work', provider, Embedder()))
    assert len(result['findings']) == 1
    assert result['rejected_findings'] == 1
    assert result['sources']
    payload = json.loads(provider.messages[0][1]['content'])
    assert payload['base_rag_documents_and_code']
    assert payload['untrusted_pr_content'][0]['head_text'] == 'new'


def test_secret_missing_before_embedding(tmp_path, monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    with pytest.raises(ProviderError, match='DEEPSEEK_API_KEY'):
        run(generate(snapshot(), tmp_path, tmp_path, DeepSeek()))


def report():
    return dict(snapshot(), mode='live', status='reviewed', findings=[finding()], reviewed_files=['app.py'], sources=[], rejected_findings=0, partial=False)


def test_stale_report_does_not_mutate():
    fixture = Fixture(); fixture.current = pr(sha='c' * 40)
    with pytest.raises(ValueError, match='Устаревший'):
        run(publish(api(fixture), report()))
    assert all(r.method == 'GET' for r in fixture.calls)


def test_upsert_author_validated_and_escaped():
    spoof = {'id': 1, 'body': MARKER, 'user': {'login': 'attacker', 'type': 'User'}}
    own = {'id': 2, 'body': MARKER, 'user': {'login': 'github-actions[bot]', 'type': 'Bot'}}
    fixture = Fixture(comments=[spoof, own])
    result = report(); result['findings'][0]['title'] = '@everyone <img> [link](https://bad.example)'
    assert run(publish(api(fixture), result)) == 'updated'
    mutations = [r for r in fixture.calls if r.method != 'GET']
    assert len(mutations) == 1 and mutations[0].url.path.endswith('/comments/2')
    body = json.loads(mutations[0].content)['body']
    assert '@everyone' not in body and '<img>' not in body
    assert '[link](https://bad.example)' not in body
    assert 'Потенциальные баги' in body and 'Архитектурные проблемы' in body and 'Рекомендации' in body


def test_spoof_only_creates_bot_comment():
    fixture = Fixture(comments=[{'id': 1, 'body': MARKER, 'user': {'login': 'someone', 'type': 'User'}}])
    assert run(publish(api(fixture), report())) == 'created'


def test_batch_limits():
    rows = [dict(snapshot()['files'][0], head_text='x' * 6000, base_text='y' * 6000, patch='z' * 6000) for _ in range(8)]
    assert len(list(batches(rows))) == 8


def test_distant_changed_lines_validate_from_patch():
    item = snapshot()['files'][0]
    item['patch'] = '@@ -1200 +1200 @@\n-old\n+new'
    assert validate_finding(finding(line=1200), [item])['line'] == 1200


def test_fixture_report_cannot_publish_live():
    item = report(); item['mode'] = 'fixture'
    fixture = Fixture()
    with pytest.raises(ValueError, match='Fixture'):
        run(publish(api(fixture), item))
    assert not fixture.calls


def test_workflow_trusted_only_and_secret_separation():
    workflow = Path(__file__).parents[2].joinpath('.github/workflows/day32-review.yml').read_text()
    assert 'pull_request_target:' in workflow
    assert 'persist-credentials: false' in workflow
    assert 'head.sha' not in workflow
    assert workflow.count('secrets.DEEPSEEK_API_KEY') == 1
    assert 'run: python -m day32_review generate --root .' in workflow
    assert all(len(line.split('@')[1].split()[0]) == 40 for line in workflow.splitlines() if 'uses: actions/' in line)


def test_trusted_base_race_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('TRUSTED_BASE_SHA', 'c' * 40)
    with pytest.raises(ValueError, match='Base SHA'):
        run(generate(snapshot(), tmp_path, tmp_path, Provider(), Embedder()))


def test_binary_base_source_is_skipped(tmp_path):
    from day32_review.review import base_paths
    (tmp_path / 'README.md').write_text('Docs')
    (tmp_path / 'day-31').mkdir()
    (tmp_path / 'day-31' / 'bad.py').write_bytes(b'\0binary')
    (tmp_path / 'day-31' / 'good.py').write_text('print(1)')
    paths, skipped = base_paths(tmp_path, allow_fixture=True)
    assert 'day-31/good.py' in paths
    assert 'day-31/bad.py' not in paths
    assert skipped[0]['path'] == 'day-31/bad.py'


def test_collect_snapshot_race_rejected():
    fixture = Fixture()
    original = fixture.__call__
    def changing(request):
        if any('/contents/' in str(r.url) for r in fixture.calls):
            fixture.current = pr(sha='c' * 40)
        return original(request)
    client = GitHub('owner/repo', 'fake', transport=httpx.MockTransport(changing))
    with pytest.raises(ValueError, match='изменился'):
        run(collect(client, 1))


def test_missing_patch_is_partial_coverage():
    fixture = Fixture([{'filename': 'app.py', 'status': 'modified'}])
    result = run(collect(api(fixture), 1))
    assert result['skipped'][0]['reason'] == 'GitHub не предоставил patch'


def test_real_corpus_excludes_ignored_untracked_sources(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from day32_review import review
    (tmp_path / 'README.md').write_text('Public')
    (tmp_path / 'day-21' / 'corpus').mkdir(parents=True)
    (tmp_path / 'day-21' / 'corpus' / 'book.md').write_text('Private book')
    (tmp_path / 'day-21' / 'app.py').write_text('Public code')
    (tmp_path / 'day-21' / 'private.py').write_text('Private experiment')
    def git(args, **kwargs):
        if 'rev-parse' in args:
            return SimpleNamespace(stdout=str(tmp_path).encode())
        assert args == ['git', '-C', str(tmp_path), 'ls-files', '-z']
        return SimpleNamespace(stdout=b'README.md\0day-21/app.py\0')
    monkeypatch.setattr(review.subprocess, 'run', git)
    paths, skipped = review.base_paths(tmp_path)
    assert paths == ['README.md', 'day-21/app.py']


def init_checkout(path):
    """Real throwaway Git repository; never mutates the project's Git state."""
    def git(*args):
        return subprocess.run(['git', '-C', str(path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    (path / 'README.md').write_text('Public base documentation')
    git('add', 'README.md')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'base')
    return git, git('rev-parse', 'HEAD')


@pytest.mark.parametrize('dirty', ['worktree', 'index', 'wrong_head', 'subdirectory', 'not_git'])
def test_live_checkout_rejected_before_provider_or_embeddings(tmp_path, monkeypatch, dirty):
    from day32_review import review
    git, sha = init_checkout(tmp_path)
    value = snapshot()
    value.update(mode='live', base_sha=sha)
    root = tmp_path
    if dirty in {'worktree', 'index'}:
        (tmp_path / 'README.md').write_text('Changed docs')
        if dirty == 'index':
            git('add', 'README.md')
    elif dirty == 'wrong_head':
        value['base_sha'] = 'c' * 40
    else:
        root = tmp_path / 'nested'
        root.mkdir()
        if dirty == 'not_git':
            root = tmp_path.parent / (tmp_path.name + '-outside')
            root.mkdir()
    def unexpected(*args, **kwargs):
        pytest.fail('Guard must run before provider or embedding construction')
    monkeypatch.setattr(review, 'DeepSeek', unexpected)
    monkeypatch.setattr(review, 'FastEmbedder', unexpected)
    monkeypatch.delenv('TRUSTED_BASE_SHA', raising=False)
    with pytest.raises(ValueError):
        run(generate(value, root, tmp_path / 'work'))


def test_live_clean_checkout_accepts_untracked_but_never_indexes_it(tmp_path, monkeypatch):
    git, sha = init_checkout(tmp_path)
    (tmp_path / 'day-32').mkdir()
    (tmp_path / 'day-32' / 'untracked.py').write_text('Private local experiment')
    value = snapshot()
    value.update(mode='live', base_sha=sha)
    monkeypatch.delenv('TRUSTED_BASE_SHA', raising=False)
    provider = Provider()
    result = run(generate(value, tmp_path, tmp_path / 'work', provider, Embedder()))
    assert result['status'] == 'reviewed'
    assert result['base_corpus']['files'] == 1
    assert result['sources']
    assert all(s['path'] == 'README.md' for s in result['sources'])
    assert provider.messages


def test_findings_over_limit_are_explicitly_partial(tmp_path):
    (tmp_path / 'README.md').write_text('Project docs')
    class ManyFindings:
        async def complete(self, messages):
            return {'findings': [finding(title=f'Issue {i}') for i in range(32)]}
    result = run(generate(snapshot(), tmp_path, tmp_path / 'work', ManyFindings(), Embedder()))
    assert len(result['findings']) == 30
    assert result['rejected_findings'] == 0
    assert result['partial'] is True
    assert result['skipped'] == [{'path': '(findings batch 1)', 'reason': 'Лимит 30 замечаний на пакет: не проверено 2'}]
    assert 'Лимит 30' in markdown(result)


def test_cli_default_root_uses_callers_directory(tmp_path, monkeypatch):
    from day32_review import __main__ as cli
    seen = []
    async def capture(args):
        seen.append(args.root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['day32_review', 'generate'])
    monkeypatch.setattr(cli, 'bounded_run', capture)
    cli.main()
    assert seen == [tmp_path]
