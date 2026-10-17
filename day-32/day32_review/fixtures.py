"""Explicit offline replay. Never presented as a live GitHub/LLM result."""
from .review import GitHub


class FixtureGitHub:
    fixture_mode = True

    def __init__(self, data):
        self.data = data
        self.repo = data.get('repo', 'owner/repo')
        self.mutations = []

    async def close(self):
        pass

    async def request(self, method, path, **kwargs):
        if method != 'GET':
            self.mutations.append({'method': method, 'path': path, 'body': kwargs.get('json')})
            return {'id': 1}
        return self.data['pr']

    async def pages(self, path):
        return self.data['files'] if path.endswith('/files') else self.data.get('comments', [])

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
