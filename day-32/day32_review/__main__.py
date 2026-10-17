import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import httpx
from assistant_core.provider import ProviderError
from .review import GitHub, collect, generate, publish, markdown
from .fixtures import FixtureGitHub, FixtureProvider


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


async def run(args):
    fixture = json.loads(args.fixture.read_text()) if args.fixture else None
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    if args.command == 'generate':
        snapshot = json.loads((work / 'snapshot.json').read_text())
        try:
            result = await generate(snapshot, args.root.resolve(), work, provider=FixtureProvider(fixture) if fixture else None)
        except (ProviderError, ValueError, OSError) as exc:
            save(work / 'error.json', {'status': 'error', 'message': str(exc)})
            raise
        save(work / 'report.json', result)
        (work / 'report.md').write_text(markdown(result), encoding='utf-8')
        if summary := os.getenv('GITHUB_STEP_SUMMARY'):
            with open(summary, 'a', encoding='utf-8') as output:
                output.write(markdown(result))
        print(f"Результат: {result['status']}; файлов {len(result['reviewed_files'])}")
        return
    api = FixtureGitHub(fixture) if fixture else GitHub(args.repo or os.getenv('GITHUB_REPOSITORY', ''), os.getenv('GITHUB_TOKEN', ''))
    try:
        if args.command == 'collect':
            number = args.pr
            if not number and os.getenv('GITHUB_EVENT_PATH'):
                event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
                number = event.get('pull_request', {}).get('number') or int(event.get('inputs', {}).get('pr_number', 0))
            if not number or number < 1:
                raise ValueError('Укажите положительный --pr')
            result = await collect(api, number)
            save(work / 'snapshot.json', result)
            print(result['status'])
        else:
            result = json.loads((work / 'report.json').read_text())
            if os.getenv('GITHUB_RUN_ID', '').isdigit() and os.getenv('GITHUB_REPOSITORY') == api.repo:
                result['run_url'] = f"https://github.com/{api.repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
            result['publication'] = await publish(api, result)
            save(work / 'report.json', result)
            print(json.dumps(result['publication'], ensure_ascii=False))
    finally:
        if fixture is not None:
            save(work / 'fixture-mutations.json', api.mutations)
            print('OFFLINE FIXTURE: внешние API не вызывались')
        await api.close()


async def bounded_run(args):
    async with asyncio.timeout(900):
        await run(args)


def main():
    parser = argparse.ArgumentParser(description='Day32: collect → generate → publish; head не исполняется')
    parser.add_argument('command', choices=['collect', 'generate', 'publish'])
    parser.add_argument('--repo', help='owner/repository; иначе GITHUB_REPOSITORY')
    parser.add_argument('--pr', type=int)
    parser.add_argument('--fixture', type=Path, help='Явный offline replay JSON: GitHub/LLM не вызываются; embeddings локальные')
    parser.add_argument('--root', type=Path, default=Path.cwd(), help='Корень доверенного base checkout; по умолчанию текущий каталог')
    parser.add_argument('--work', type=Path, default=Path('data/review'))
    args = parser.parse_args()
    try:
        asyncio.run(bounded_run(args))
    except (ProviderError, ValueError, OSError, httpx.HTTPError, KeyError, TimeoutError) as exc:
        # HTTP exceptions can contain request URLs, never dump response bodies or headers.
        message = str(exc) if isinstance(exc, (ProviderError, ValueError)) else type(exc).__name__
        print('Ошибка: ' + message, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
