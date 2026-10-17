"""Bounded, repeatable inline publication; PR text is never executable."""
from __future__ import annotations

import base64
import hashlib
import json
import re

from .review import MARKER, CATEGORIES, GraphQLPermissionError, changed_lines, safe, validate_finding

BOT = 'github-actions[bot]'
MAX_ANCHORS = 30
LIFECYCLE = '<!-- day32-lifecycle-outdated -->'
INLINE = re.compile(r'^<!-- day32-inline:([a-f0-9]{64}):([A-Za-z0-9_=-]+) -->')
THREADS = '''query($owner:String!,$name:String!,$number:Int!,$cursor:String){
 repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$cursor){
 pageInfo{hasNextPage endCursor} nodes{id path line diffSide isOutdated isResolved viewerCanResolve viewerCanUnresolve
 resolvedBy{login __typename} comments(first:1){nodes{databaseId body url author{login __typename}}}}
 }}}}'''


def canonical_key(path, side, text):
    return hashlib.sha256(json.dumps([path, side, text], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def marker(key, text):
    return f'<!-- day32-inline:{key}:{base64.urlsafe_b64encode(text.encode()).decode()} -->'


def identity(body):
    match = INLINE.match(body or '')
    if not match:
        return None
    try:
        text = base64.b64decode(match[2], altchars=b'-_', validate=True).decode()
    except (ValueError, UnicodeError):
        return None
    return match[1], text


def bot(user, graphql=False):
    return isinstance(user, dict) and user.get('login') == ('github-actions' if graphql else BOT) and user.get('__typename' if graphql else 'type') == 'Bot'


async def current(api, report):
    pr = await api.request('GET', f"pulls/{report['number']}")
    if not pr['head']['repo'] or pr['head']['repo']['full_name'] != api.repo:
        raise ValueError('Fork PR пропущен')
    if pr['head']['sha'] != report['head_sha'] or pr['base']['sha'] != report['base_sha']:
        raise ValueError('Устаревший отчёт: PR изменился')
    return pr


async def threads(api, number):
    owner, name = api.repo.split('/')
    result, cursor, seen = [], None, set()
    for _ in range(100):
        data = await api.graphql(THREADS, dict(owner=owner, name=name, number=number, cursor=cursor))
        try:
            page = data['repository']['pullRequest']['reviewThreads']
            nodes, info = page['nodes'], page['pageInfo']
            if not isinstance(nodes, list) or type(info['hasNextPage']) is not bool:
                raise ValueError()
            for node in nodes:
                if not isinstance(node, dict) or not node['comments']['nodes'] or any(k not in node for k in ('id','path','line','diffSide','isOutdated','isResolved','viewerCanResolve','viewerCanUnresolve','resolvedBy')):
                    raise ValueError()
                root = node['comments']['nodes'][0]
                if (not isinstance(root, dict) or any(k not in root for k in ('databaseId','body','url','author'))
                        or any(type(node[k]) is not bool for k in ('isOutdated','isResolved','viewerCanResolve','viewerCanUnresolve'))):
                    raise ValueError()
            result.extend(nodes)
            if not info['hasNextPage']:
                return result
            cursor = info['endCursor']
            if not cursor or cursor in seen:
                raise ValueError()
            seen.add(cursor)
        except (KeyError, TypeError, ValueError):
            raise ValueError('Неполный список review threads GitHub') from None
    raise ValueError('Превышен предел review threads GitHub')


async def transition(api, report, thread, resolved):
    await current(api, report)
    operation = 'resolveReviewThread' if resolved else 'unresolveReviewThread'
    data = await api.graphql(f'mutation($id:ID!){{{operation}(input:{{threadId:$id}}){{thread{{id isResolved}}}}}}', {'id':thread['id']})
    result = data.get(operation, {}).get('thread', {})
    if result.get('id') != thread['id'] or result.get('isResolved') is not resolved:
        raise ValueError('GitHub не подтвердил изменение review thread')


def owned_thread(thread):
    root = thread['comments']['nodes'][0]
    ident = identity(root.get('body'))
    side = {'RIGHT': 'head', 'LEFT': 'base'}.get(thread['diffSide'])
    if not bot(root.get('author'), True) or not ident or side is None:
        return None
    if canonical_key(thread['path'], side, ident[1]) != ident[0]:
        return None
    return root, ident, side


def inline_body(group, report):
    first = group[0]
    out = [marker(first['key'], first['canonical']), f"**AI-ревью Day32** · Head: `{report['head_sha']}`"]
    for f in group:
        out.append(f"**{f['severity']} · {CATEGORIES[f['category']]}: {safe(f['title'])}**"
                   + (' (гипотеза)' if f['hypothesis'] else '') + '\n\n' + safe(f['explanation']))
    out.append('Точная привязка к diff подтверждена; смысловой вывод требует проверки разработчиком.')
    return '\n\n'.join(out)[:60000]


async def mark_outdated(api, report, thread, root, comments, stats, reason):
    # REST ownership is independently checked before editing a comment body.
    comment = next((c for c in comments if c['id'] == root['databaseId'] and not c.get('in_reply_to_id')
                    and bot(c.get('user')) and identity(c.get('body')) == identity(root['body'])), None)
    detail = {'thread_id': thread['id'], 'reason': reason,
              'viewer_can_resolve': thread['viewerCanResolve']}
    stats['lifecycle_skipped'].append(detail)
    if comment is None:
        detail['notice_updated'] = False
        detail['notice_reason'] = 'REST root ownership not verified'
        return
    original = comment['body'].split(LIFECYCLE, 1)[0].rstrip()
    body = original + '\n\n' + LIFECYCLE + f"\n\n**Привязка устарела после изменения кода.** Head: `{report['head_sha']}`. " \
           + 'Это не доказательство исправления. GitHub не разрешил боту закрыть тред автоматически; история сохранена.'
    detail['notice_updated'] = body != comment['body']
    if detail['notice_updated']:
        await current(api, report)
        await api.request('PATCH', f"pulls/comments/{comment['id']}", json={'body': body})
    stats['outdated_marked'] += 1


def summary(report, stats):
    out = [MARKER, '# AI-ревью Day32', f"Base: `{report['base_sha']}` · Head: `{report['head_sha']}`",
           f"Проверено файлов: {len(report['reviewed_files'])}/{report['total_files']}. " +
           ('Анализ частичный.' if report['partial'] else 'Все доступные файлы обработаны.')]
    for key, title in CATEGORIES.items():
        out.append(f"**{title}:** {sum(f['category'] == key for f in report['findings'])}")
    out.append(f"Замечания размещены у строк во вкладке **Files changed**. Привязок: {len(stats['inline'])}; "
               f"не опубликовано из-за лимита: {stats['omitted_anchors']}. Отклонено неподтверждённых: {report['rejected_findings']}.")
    if stats['inline']:
        out.append(' · '.join(f"[Комментарий {i+1}]({x['url']})" for i,x in enumerate(stats['inline']) if x.get('url')))
    out.append(f"RAG: {len(report['sources'])} фрагментов base. Подробный отчёт и источники — report.md/report.json в artifact запуска Action.")
    if run := report.get('run_url'):
        if re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+', run):
            out.append(f'[Запуск и artifacts]({run})')
    if report['skipped']:
        out.append('## Ограничения покрытия')
        out.extend('- ' + safe(x['path']) + ': ' + safe(x['reason']) for x in report['skipped'][:20])
        if len(report['skipped']) > 20:
            out.append('Остальные пропуски перечислены в artifact.')
    if stats['resolved']:
        out.append(f"У {stats['resolved']} устаревших привязок закрыты треды: привязка устарела после изменения кода. Это не доказательство исправления.")
    if stats['outdated_marked']:
        out.append(f"Устаревшие привязки отмечены в {stats['outdated_marked']} комментариях; GitHub не разрешил боту закрыть эти треды. Это не доказательство исправления.")
    if stats['lifecycle_skipped']:
        out.append(f"Ограничения lifecycle: {len(stats['lifecycle_skipped'])}; причины и доступные флаги — report.json.publication.lifecycle_skipped. Ручные решения человека сохраняются.")
    out.append('Отсутствие замечаний не доказывает корректность. Ревью не блокирует merge.')
    return '\n\n'.join(out)[:60000]


async def publish_inline(api, report):
    if report.get('mode') == 'fixture' and not getattr(api, 'fixture_mode', False):
        raise ValueError('Fixture нельзя публиковать в GitHub')
    if report['repo'] != api.repo:
        raise ValueError('Репозиторий отчёта не совпадает')
    if report['status'] != 'reviewed':
        return {'status': 'skipped'}
    pr = await current(api, report)
    rows = await api.pages(f"pulls/{report['number']}/files")
    complete_files = len(rows) == pr['changed_files']
    files = [{'path': r['filename'], 'patch': r.get('patch', '')} for r in rows]
    groups = {}
    # Validate the entire report before the first mutation, against newly fetched patches.
    for original in report['findings']:
        f = validate_finding(original, files)
        f['canonical'] = changed_lines(next(r['patch'] for r in files if r['path'] == f['file']))[f['side']][f['line']]
        f['key'] = canonical_key(f['file'], f['side'], f['canonical'])
        # Identical code on multiple lines shares identity but keeps separate current anchors.
        groups.setdefault((f['key'], f['line']), []).append(f)
    selected = list(groups.values())[:MAX_ANCHORS]
    all_keys = {g[0]['key'] for g in groups.values()}
    comments = await api.pages(f"pulls/{report['number']}/comments")
    thread_rows = await threads(api, report['number'])
    issues = await api.pages(f"issues/{report['number']}/comments")
    await current(api, report)
    stats = {'status': 'published', 'created': 0, 'updated': 0, 'resolved': 0, 'reopened': 0,
             'outdated_marked': 0, 'lifecycle_skipped': [], 'omitted_anchors': max(0,len(groups)-MAX_ANCHORS), 'inline': []}
    for group in selected:
        f = group[0]; side = 'RIGHT' if f['side'] == 'head' else 'LEFT'
        matches = []
        for t in thread_rows:
            owned = owned_thread(t)
            if not owned or t['isOutdated'] or t['path'] != f['file'] or t['diffSide'] != side or t['line'] != f['line'] or owned[1][0] != f['key']:
                continue
            root = owned[0]
            comment = next((c for c in comments if c['id'] == root['databaseId'] and not c.get('in_reply_to_id')
                            and bot(c.get('user')) and identity(c.get('body')) == owned[1]), None)
            if comment:
                matches.append((t, comment))
        body = inline_body(group, report)
        if matches:
            t, comment = matches[0]
            # Do not alter or reopen a human-resolved thread.
            if not t['isResolved'] or (bot(t.get('resolvedBy'), True) and t['viewerCanUnresolve']):
                await current(api, report)
                await api.request('PATCH', f"pulls/comments/{comment['id']}", json={'body':body})
                stats['updated'] += 1
                if t['isResolved'] and t['viewerCanUnresolve']:
                    try:
                        await transition(api, report, t, False)
                        stats['reopened'] += 1
                    except GraphQLPermissionError:
                        stats['lifecycle_skipped'].append({'thread_id': t['id'], 'reason': 'integration cannot unresolve', 'viewer_can_unresolve': t['viewerCanUnresolve']})
            elif t['isResolved'] and bot(t.get('resolvedBy'), True):
                stats['lifecycle_skipped'].append({'thread_id': t['id'], 'reason': 'viewer cannot unresolve', 'viewer_can_unresolve': t['viewerCanUnresolve']})
            stats['inline'].append({'id':comment['id'],'url':comment.get('html_url',root.get('url'))})
        else:
            await current(api, report)
            created = await api.request('POST', f"pulls/{report['number']}/comments", json={
                'body':body, 'commit_id':report['head_sha'], 'path':f['file'], 'side':side, 'line':f['line']})
            stats['created'] += 1
            stats['inline'].append({'id':created['id'],'url':created.get('html_url')})
    for t in thread_rows:
        owned = owned_thread(t)
        if not owned or not t['isOutdated'] or t['isResolved'] or owned[1][0] in all_keys or not complete_files:
            continue
        root, ident, side = owned
        row = next((r for r in rows if r['filename'] == t['path'] or r.get('previous_filename') == t['path']), None)
        absent = row is None
        # Base RAG budget does not prevent checking disappearance of an exact changed line.
        fully_analyzed = (not report['rejected_findings'] and t['path'] in report['reviewed_files']
                          and not any(x['path'] != '(base RAG)' for x in report['skipped']))
        disappeared = bool(row and row.get('patch') and fully_analyzed
                           and ident[1] not in changed_lines(row['patch'])[side].values())
        if not absent and not disappeared:
            continue
        if not t['viewerCanResolve']:
            await mark_outdated(api, report, t, root, comments, stats, 'viewer cannot resolve')
            continue
        try:
            await transition(api, report, t, True)
            stats['resolved'] += 1
        except GraphQLPermissionError:
            await mark_outdated(api, report, t, root, comments, stats, 'integration cannot resolve')
    body = summary(report, stats)
    own = next((x for x in issues if x.get('body','').startswith(MARKER) and bot(x.get('user'))), None)
    await current(api, report)
    if own:
        await api.request('PATCH', f"issues/comments/{own['id']}", json={'body':body})
        stats['status'] = 'updated'
    else:
        await api.request('POST', f"issues/{report['number']}/comments", json={'body':body})
        stats['status'] = 'created'
    return stats
