"""Explicit stateful offline replay. No network or GitHub mutations."""
import copy


class FixtureGitHub:
    fixture_mode = True

    def __init__(self, data):
        self.data = copy.deepcopy(data)
        self.repo = data.get('repo', 'owner/repo')
        self.mutations = []
        self.inline = self.data.get('inline_comments', [])
        self.threads = self.data.get('threads', [])
        self.comments = self.data.get('comments', [])
        self.next_id = max([c['id'] for c in self.inline + self.comments] + [0]) + 1

    async def close(self):
        pass

    async def request(self, method, path, **kwargs):
        if method == 'GET':
            return self.data['pr']
        self.mutations.append({'method': method, 'path': path, 'body': kwargs.get('json')})
        payload = kwargs['json']
        if method == 'PATCH':
            pool = self.inline if path.startswith('pulls/') else self.comments
            item = next(c for c in pool if c['id'] == int(path.rsplit('/',1)[1]))
            item['body'] = payload['body']
            for t in self.threads:
                root = t['comments']['nodes'][0]
                if root['databaseId'] == item['id']:
                    root['body'] = item['body']
            return item
        item = dict(payload, id=self.next_id, html_url=f'https://github.com/{self.repo}/pull/1#discussion_r{self.next_id}',
                    user={'login':'github-actions[bot]', 'type':'Bot'})
        self.next_id += 1
        if path.startswith('pulls/'):
            self.inline.append(item)
            self.threads.append({'id':f"thread{item['id']}", 'path':item['path'], 'line':item['line'],
                'diffSide':item['side'], 'isOutdated':False, 'isResolved':False, 'viewerCanResolve':True, 'viewerCanUnresolve':True,
                'resolvedBy':None, 'comments':{'nodes':[{'databaseId':item['id'],'body':item['body'],
                    'url':item['html_url'],'author':{'login':'github-actions','__typename':'Bot'}}]}})
        else:
            self.comments.append(item)
        return item

    async def graphql(self, query, variables):
        if query.startswith('mutation'):
            self.mutations.append({'method':'GRAPHQL', 'query':query, 'variables':variables})
            thread = next(t for t in self.threads if t['id'] == variables['id'])
            thread['isResolved'] = 'unresolveReviewThread' not in query
            thread['resolvedBy'] = {'login':'github-actions','__typename':'Bot'} if thread['isResolved'] else None
            operation = 'resolveReviewThread' if thread['isResolved'] else 'unresolveReviewThread'
            return {operation:{'thread':thread}}
        return {'repository':{'pullRequest':{'reviewThreads':{'nodes':self.threads,'pageInfo':{'hasNextPage':False,'endCursor':None}}}}}

    async def pages(self, path):
        if path.endswith('/files'):
            return self.data['files']
        return self.inline if path.startswith('pulls/') else self.comments

    async def content(self, path, sha):
        key = sha + ':' + path
        if key not in self.data.get('contents', {}):
            raise ValueError('Fixture: content отсутствует')
        return self.data['contents'][key]


class FixtureProvider:
    def __init__(self, data):
        self.data = data

    async def complete(self, messages):
        return {'findings': self.data.get('findings', [])}
