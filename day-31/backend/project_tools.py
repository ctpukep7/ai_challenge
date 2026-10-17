"""Fixed-root public project evidence. No shell, private paths or file writes."""
from pathlib import Path, PurePosixPath
import difflib
import hashlib
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MAX_FILE = 300_000
DENIED = {'wiki', 'data', 'fixtures', 'node_modules', 'dist', 'build', 'coverage', 'swarm-report', '__pycache__', 'weights', 'cache', 'certs', 'tls'}
EXTENSIONS = {'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.vue', '.css', '.html', '.json', '.toml', '.txt', '.md', '.yml', '.yaml', '.sh'}
CONFIGS = {'package.json', 'pyproject.toml', 'requirements.txt', 'requirements.lock.txt', 'vite.config.js', 'vite.config.ts', 'pytest.ini', 'tsconfig.json'}
# Match actual credential signatures or quoted high-entropy literal assignments,
# never expressions such as os.getenv(), re.compile() or configuration variable names.
SECRET = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})")
LITERAL_SECRET = re.compile(r"(?:api[_-]?key|password|secret|access[_-]?token|authorization)\s*[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_+/=-]{20,})[\"']", re.I)


class PublicError(ValueError):
    pass

def allowed(rel):
    if not isinstance(rel, str) or not rel or '\\' in rel or '\0' in rel:
        return False
    p = PurePosixPath(rel)
    if p.is_absolute() or '..' in p.parts or str(p) != rel:
        return False
    parts = p.parts
    if any(x in DENIED or (x.startswith('.') and not (i == 0 and x == '.github')) for i, x in enumerate(parts)):
        return False
    if any(re.search(r'(?:credential|private[_-]?key|secret|\.env)', x, re.I) for x in parts):
        return False
    if rel == 'README.md' or (parts[0] == 'docs' and p.suffix == '.md'):
        return True
    if parts[:2] == ('.github', 'workflows') and p.suffix in {'.yml', '.yaml'}:
        return True
    if re.fullmatch(r'day-\d{2}', parts[0]):
        return (len(parts) == 2 and (parts[1] == 'README.md' or parts[1] in CONFIGS)) or (len(parts) >= 3 and parts[1] in {'backend', 'frontend', 'scripts', 'tests'} and p.suffix in EXTENSIONS)
    if parts[0] == 'packages' and len(parts) >= 3:
        return p.suffix in EXTENSIONS and (parts[2] in {'src', 'tests'} or (len(parts) == 3 and (p.name == 'README.md' or p.name in CONFIGS)))
    return False

def public_path(rel, root=ROOT):
    if not allowed(rel):
        raise PublicError('Этот путь не входит в публичный контекст проекта')
    root = root.resolve()
    path = root / rel
    current = root
    for part in PurePosixPath(rel).parts:
        current /= part
        if current.is_symlink():
            raise PublicError('Символьные ссылки запрещены')
    if not path.resolve().is_relative_to(root):
        raise PublicError('Недопустимый путь')
    return path

def safe_text(raw):
    if len(raw) > MAX_FILE or b'\0' in raw:
        raise PublicError('Файл бинарный или превышает лимит')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise PublicError('Файл не является UTF-8 текстом') from None
    if SECRET.search(text) or any(not re.match(r'(?:test|example|dummy|your|placeholder)', m.group(1), re.I) for m in LITERAL_SECRET.finditer(text)):
        raise PublicError('Содержимое исключено фильтром секретов')
    return text

def read_public(rel, root=ROOT):
    path = public_path(rel, root)
    if not path.is_file() or path.stat().st_size > MAX_FILE:
        raise PublicError('Файл отсутствует или превышает лимит')
    return safe_text(path.read_bytes())

def git(*args, root=ROOT, binary=False):
    try:
        r = subprocess.run(['git', '-c', 'core.quotePath=false', '-c', 'core.pager=cat', '-c', 'diff.external=', '-C', str(root), *args], capture_output=True, timeout=10, check=True)
        if len(r.stdout) > 2_000_000:
            raise PublicError('Git-результат превышает лимит')
        return r.stdout if binary else r.stdout.decode('utf-8')
    except (subprocess.SubprocessError, UnicodeDecodeError):
        raise PublicError('Git недоступен для этой операции') from None

def evidence(kind, text, path=None, start_line=None, end_line=None, **extra):
    return dict(kind=kind, text=text, path=path, start_line=start_line, end_line=end_line,
                sha256=hashlib.sha256(text.encode()).hexdigest(), **extra)

def git_branch(root=ROOT):
    sha = git('rev-parse', 'HEAD', root=root).strip()
    branch = git('branch', '--show-current', root=root).strip()
    value = {'branch': branch or None, 'sha': sha, 'detached': not bool(branch)}
    return dict(value, evidence=[evidence('git', json.dumps(value, ensure_ascii=False))])

def project_paths(root=ROOT):
    return sorted(set(git('ls-files', '-z', '--cached', '--others', '--exclude-standard', root=root).split('\0')) - {''})

def list_project_files(prefix='', root=ROOT):
    if prefix and (PurePosixPath(prefix).is_absolute() or '..' in PurePosixPath(prefix).parts or '\\' in prefix):
        raise PublicError('Недопустимый префикс')
    paths = []
    for path in project_paths(root):
        if not allowed(path) or not path.startswith(prefix):
            continue
        try:
            if public_path(path, root).is_file():
                paths.append(path)
        except PublicError:
            pass
    selected = paths[:300]
    return {'files': selected, 'total': len(paths), 'truncated': len(paths) > len(selected),
            'evidence': [evidence('code', '\n'.join(selected), section='Публичные пути проекта') ] if selected else []}

def read_project_file(path, start_line=1, end_line=None, root=ROOT):
    if end_line is None:
        end_line = start_line + 199
    if start_line < 1 or end_line < start_line or end_line - start_line >= 200:
        raise PublicError('За один вызов доступно не более 200 строк')
    full = read_public(path, root)
    lines = full.splitlines(keepends=True)
    if start_line > max(1, len(lines)):
        raise PublicError('Начальная строка за пределами файла')
    selected = lines[start_line - 1:end_line]
    while selected and len(''.join(selected).encode()) > 16_000:
        selected.pop()
    if not selected and lines:
        raise PublicError('Строка превышает лимит чтения')
    text = ''.join(selected)
    last = start_line + len(selected) - 1
    # A caller-selected range is not a byte-limit truncation. Preserve the old
    # truncated coverage flag for API clients, while explaining its cause.
    byte_truncated = len(selected) < len(lines[start_line - 1:end_line])
    return {'path': path, 'total_lines': len(lines), 'truncated': start_line > 1 or last < len(lines),
            'byte_truncated': byte_truncated, 'requested_start_line': start_line,
            'requested_end_line': end_line, 'returned_start_line': start_line,
            'returned_end_line': max(start_line, last),
            'evidence': [evidence('documentation' if path.endswith('.md') else 'code', text, path, start_line, max(start_line, last))]}

def git_diff(scope='all', path=None, root=ROOT):
    if scope not in {'all', 'working', 'staged'}:
        raise PublicError('Неизвестная область diff')
    if path is not None:
        public_path(path, root)
    # Discover names first. Never request a whole-repository patch with secrets.
    opts = ['--cached'] if scope == 'staged' else ['HEAD'] if scope == 'all' else []
    raw = git('diff', '--name-only', '-z', '--no-renames', *opts, '--', root=root)
    tracked = [p for p in raw.split('\0') if p]
    untracked = [] if scope == 'staged' else [p for p in git('ls-files', '--others', '--exclude-standard', '-z', root=root).split('\0') if p]
    selected = sorted(set(tracked + untracked))
    if path:
        selected = [p for p in selected if p == path]
    public_inventory = []
    for rel in selected:
        if allowed(rel):
            try:
                public_path(rel, root)
                public_inventory.append({'path': rel, 'status': 'untracked snapshot' if rel in untracked else scope + ' tracked change'})
            except PublicError:
                pass
    entries = []; omitted = 0; used = 0
    for position, rel in enumerate(selected):
        if used >= 32_000 or len(entries) >= 30:
            omitted += len(selected) - position
            break
        if not allowed(rel):
            omitted += 1
            continue
        try:
            p = public_path(rel, root)
            if p.exists():
                read_public(rel, root)  # Reject symlinks, huge/binary/secrets before git reads.
            if rel in untracked:
                content = read_public(rel, root)
                patch = ''.join(difflib.unified_diff([], content.splitlines(True), fromfile='/dev/null', tofile=rel))
                label = 'untracked snapshot'
            else:
                patch = safe_text(git('diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--unified=3', *opts, '--', rel, root=root, binary=True))
                label = scope
            available = 32_000 - used
            if available <= 0 or len(entries) >= 30:
                omitted += 1
                continue
            truncated = len(patch.encode()) > available
            if truncated:
                patch = patch.encode()[:available].decode('utf-8', errors='ignore')
            used += len(patch.encode())
            if patch:
                entries.append(evidence('diff', patch, rel, section=label, truncated=truncated))
        except (PublicError, OSError):
            omitted += 1
    summary = {'scope': scope, 'public_changed_total': len(public_inventory), 'public_changed_files': public_inventory[:300], 'inventory_truncated': len(public_inventory) > 300, 'shown_patches': len(entries), 'omitted_files': omitted, 'partial': bool(omitted or len(public_inventory) > 300 or any(x['truncated'] for x in entries))}
    # Counts themselves are evidence even for an empty diff.
    return dict(summary, evidence=[evidence('git', json.dumps(summary, ensure_ascii=False))] + entries)
