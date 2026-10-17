"""Read-only MCP server. stdout is reserved for protocol messages."""
from pathlib import Path
import os
from typing import Literal
from mcp.server.fastmcp import FastMCP
from project_tools import ROOT, PublicError, git_branch as branch, list_project_files as listing, read_project_file as reading, git_diff as diff, safe_text
from retrieval import search_sources

server = FastMCP('AI Challenge public project')
_index = None

def guarded(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (PublicError, OSError):
        return {'error': 'Публичные данные недоступны для этой операции', 'evidence': []}

@server.tool()
def git_branch() -> dict:
    """Current branch, HEAD SHA and detached status. Fresh Git, no documentation needed."""
    return guarded(branch)

@server.tool()
def list_project_files(prefix: str = '') -> dict:
    """List up to 300 public source/document/config paths, optionally starting with prefix."""
    return guarded(listing, prefix)

@server.tool()
def read_project_file(path: str, start_line: int = 1, end_line: int | None = None) -> dict:
    """Read public project text by exact relative path, max 200 lines and 16 KB. Private paths forbidden."""
    return guarded(reading, path, start_line, end_line)

@server.tool()
def git_diff(scope: str = 'all', path: str | None = None) -> dict:
    """Current changes: all versus HEAD, working versus index, staged versus HEAD. Optional exact public path. Includes safe untracked snapshots, max 32 KB."""
    return guarded(diff, scope, path)

@server.tool()
def search_documentation(query: str, day: int | None = None, scope: Literal['all', 'project'] = 'all') -> dict:
    """Semantic RAG with exact lines. Use scope=project for overall AI Challenge structure/architecture/RAG (root README + docs only). Default all searches all days. Optional day scopes that day's README + docs, incompatible with project. Requires fresh index."""
    global _index
    from assistant_core import Index, FastEmbedder, documentation_paths
    try:
        if not query.strip() or len(query) > 4000 or (day is not None and not 1 <= day <= 99):
            return {'error': 'Некорректный поисковый запрос', 'evidence': []}
        if scope not in {'all', 'project'} or (scope == 'project' and day is not None):
            return {'error': 'Область project нельзя сочетать с номером дня; выберите all с day', 'evidence': []}
        if _index is None:
            cache = Path(os.getenv('ASSISTANT_EMBED_CACHE', str(ROOT / 'day-21/.cache/fastembed')))
            _index = Index(ROOT/'day-31/data/index.sqlite', ROOT, lambda: documentation_paths(ROOT), FastEmbedder(cache))
        state = _index.status()
        if not state['ready'] or state['stale']:
            return {'error': 'Индекс отсутствует или устарел: обновите его кнопкой в интерфейсе', 'index': state, 'evidence': []}
        sources = search_sources(_index, query, documentation_paths(ROOT), day=day, scope=scope)
        result = []
        for source in sources:
            safe_text(source['text'].encode())
            result.append(dict(source, kind='documentation'))
        return {'evidence': result, 'index': state, 'scope': scope, 'day': day}
    except Exception:
        return {'error': 'Локальный поиск недоступен: проверьте актуальность индекса и существующий кэш модели', 'evidence': []}

if __name__ == '__main__':
    server.run(transport='stdio')
