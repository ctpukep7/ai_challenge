"""Fixed-root, public-source-only tooling. Never executes project contents."""
import difflib
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile


class PolicyError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


SECRET = re.compile(r'''(?im)(?:api[_-]?key|access[_-]?token|password|secret|authorization)\s*[:=]\s*["'](?:Bearer\s+)?[A-Za-z0-9_./+\-=]{12,}["']''')


class Files:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.sources = []
        self.hashes = {}
        self.changes = []

    @property
    def change(self):
        """Compatibility for clients that display only the first proposal."""
        return self.changes[0] if self.changes else None

    def path(self, name, write=False):
        if not isinstance(name, str) or not name or '\\' in name:
            raise PolicyError('Недопустимый путь')
        p = PurePosixPath(name)
        if p.is_absolute() or any(x in {'.', '..'} or x.startswith('.') for x in name.split('/')):
            raise PolicyError('Путь вне разрешённой области')
        if not self.allowed(name, write):
            raise PolicyError('Файл вне публичной области проекта')
        target = self.root.joinpath(*p.parts)
        cursor = self.root
        for part in p.parts:
            cursor /= part
            if cursor.is_symlink():
                raise PolicyError('Символьные ссылки запрещены')
        if not target.resolve().is_relative_to(self.root):
            raise PolicyError('Путь вне проекта')
        return target

    @staticmethod
    def allowed(name, write=False):
        if re.fullmatch(r'day-34/generated/[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\.md', name):
            return True
        if write:
            return False
        parts = name.split('/')
        if any(p.startswith('.') or p in {'node_modules', 'data', '__pycache__', 'dist'} for p in parts):
            return False
        if re.search(r'(?i)(?:credential|secret|token|password|lock)', parts[-1]):
            return False
        if name == 'README.md' or re.fullmatch(r'docs/(?:[^/]+/)*[^/]+\.md', name):
            return True
        if re.fullmatch(r'packages/assistant_core/src/(?:[^/]+/)*[^/]+\.py', name):
            return True
        if not re.match(r'day-\d{2}/', name):
            return False
        relative = '/'.join(parts[1:])
        return bool(relative == 'README.md' or re.fullmatch(r'backend/(?:[^/]+/)*[^/]+\.(?:py|json|sql)', relative)
                    or re.fullmatch(r'frontend/src/(?:[^/]+/)*[^/]+\.(?:js|ts|vue|css)', relative)
                    or re.fullmatch(r'tests/(?:[^/]+/)*[^/]+\.(?:py|js|ts|json)', relative)
                    or re.fullmatch(r'fixtures/(?:[^/]+/)*[^/]+\.json', relative))

    def content(self, name):
        path = self.path(name)
        try:
            if path.stat().st_size > 200_000:
                raise PolicyError('Файл превышает лимит 200 KB')
            data = path.read_bytes()
            if len(data) > 200_000:
                raise PolicyError('Файл превышает лимит 200 KB')
            text = data.decode('utf-8')
            if '\x00' in text:
                raise ValueError()
        except (OSError, UnicodeError, ValueError) as exc:
            if isinstance(exc, PolicyError):
                raise
            raise PolicyError('Файл недоступен или не является UTF-8 текстом') from None
        if SECRET.search(text):
            raise PolicyError('Файл содержит возможное присвоение секрета: чтение запрещено')
        return text, digest(data)

    def candidates(self, prefix=''):
        if prefix and (prefix.startswith('/') or '\\' in prefix or any(x.startswith('.') for x in prefix.split('/'))):
            raise PolicyError('Недопустимый префикс')
        result = []
        # os.walk does not follow directory links; prune private and irrelevant trees early.
        for base, dirs, names in os.walk(self.root, followlinks=False):
            relative = Path(base).relative_to(self.root)
            if relative == Path('.'):
                dirs[:] = [d for d in dirs if re.fullmatch(r'day-\d{2}', d) or d in {'docs', 'packages'}]
            else:
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in {'node_modules', 'data', 'dist', '__pycache__', 'wiki', 'venv'} and not (Path(base)/d).is_symlink()]
            for filename in names:
                name = (relative/filename).as_posix()
                if name.startswith(prefix) and self.allowed(name):
                    try:
                        self.path(name)
                        result.append(name)
                    except PolicyError:
                        pass
        return sorted(result)

    def list_files(self, prefix=''):
        paths = self.candidates(prefix)
        return {'files': paths[:500], 'truncated': len(paths) > 500, 'total': len(paths)}

    def search_text(self, query, prefix=''):
        if not query.strip():
            raise PolicyError('Пустая строка поиска')
        hits = []
        skipped = 0
        for name in self.candidates(prefix):
            try:
                content, _ = self.content(name)
            except PolicyError:
                skipped += 1
                continue
            for line, text in enumerate(content.splitlines(), 1):
                if query.casefold() in text.casefold():
                    if len(hits) >= 100:
                        return {'hits': hits, 'truncated': True, 'skipped_files': skipped}
                    hits.append({'path': name, 'line': line, 'text': text[:600], 'text_truncated': len(text) > 600})
        return {'hits': hits, 'truncated': False, 'skipped_files': skipped}

    def read_file(self, path, start_line=1, end_line=None):
        content, sha = self.content(path)
        if path in self.hashes and self.hashes[path] != sha:
            raise PolicyError('Файл изменился во время исследования: запустите задачу заново')
        lines = content.splitlines(keepends=True)
        if start_line < 1 or start_line > max(len(lines), 1) or (end_line is not None and end_line < start_line):
            raise PolicyError('Некорректный диапазон строк')
        stop = min(end_line or len(lines), start_line + 199, len(lines))
        selected = []
        size = 0
        for line in lines[start_line-1:stop]:
            size += len(line.encode())
            if size > 20_000:
                break
            selected.append(line)
        if not selected and lines:
            raise PolicyError('Одна строка превышает лимит чтения 20 KB')
        source = {'id': f'S{len(self.sources)+1}', 'path': path, 'start_line': start_line,
                  'end_line': start_line+len(selected)-1, 'text': ''.join(selected), 'sha256': sha}
        self.sources.append(source)
        self.hashes[path] = sha
        return dict(source, total_lines=len(lines), truncated=source['end_line'] < len(lines),
                    next_start_line=source['end_line']+1 if source['end_line'] < len(lines) else None)

    def stage_file(self, path, content):
        if len(self.hashes) < 3:
            raise PolicyError('Перед подготовкой diff прочитайте минимум три разных файла')
        target = self.path(path, write=True)
        if len(content.encode()) > 20_000 or '\x00' in content or SECRET.search(content):
            raise PolicyError('Содержимое превышает 20 KB или содержит недопустимые данные')
        before, before_sha = self.content(path) if target.exists() else ('', None)
        previous = next((change for change in self.changes if change['path'] == path), None)
        if previous and previous['before_sha256'] != before_sha:
            raise PolicyError('Целевой файл изменился: запустите задачу заново')
        if before == content:
            self.changes[:] = [change for change in self.changes if change['path'] != path]
            return {'unchanged': True, 'path': path, 'diff': ''}
        if not previous and len(self.changes) >= 5:
            raise PolicyError('Одна задача может изменять максимум пять Markdown-файлов')
        patch = ''.join(line if line.endswith('\n') else line+'\n\\ No newline at end of file\n' for line in
                        difflib.unified_diff(before.splitlines(keepends=True), content.splitlines(keepends=True),
                        fromfile=f'a/{path}' if before_sha else '/dev/null', tofile=f'b/{path}'))
        change = {'path': path, 'before': before, 'after': content, 'diff': patch, 'before_sha256': before_sha}
        if previous:
            self.changes[self.changes.index(previous)] = change
        else:
            self.changes.append(change)
        return {'prepared': True, 'path': path, 'diff': patch, 'saved': False}

    def result(self):
        return {'applied': True, 'changes': self.changes.copy(),
                'path': self.change['path'], 'diff': ''.join(change['diff'] for change in self.changes)}

    def apply(self):
        if not self.changes or len(self.hashes) < 3:
            raise PolicyError('Нет подготовленных изменений с тремя источниками')
        # Validate EVERY source and target before preparing or replacing any file.
        for name, expected in self.hashes.items():
            if self.content(name)[1] != expected:
                raise PolicyError('Исходные файлы изменились: запустите задачу заново')
        originals = {}
        targets = {}
        for change in self.changes:
            name = change['path']
            target = self.path(name, write=True)
            current = self.content(name)[1] if target.exists() else None
            if current != change['before_sha256']:
                raise PolicyError(f'Целевой файл {name} изменился: запустите задачу заново')
            try:
                original = target.read_bytes() if current is not None else None
                mode = stat.S_IMODE(target.stat().st_mode) if current is not None else None
            except OSError:
                raise PolicyError(f'Целевой файл {name} недоступен') from None
            if original is not None and digest(original) != current:
                raise PolicyError(f'Целевой файл {name} изменился во время проверки')
            originals[name] = (original, mode)
            targets[name] = target
        temporary_files = []
        prepared = {}
        backups = {}
        replaced = []
        preserved_backups = set()

        def prepare(target, content, mode):
            descriptor, temporary = tempfile.mkstemp(prefix='.day34-', dir=target.parent)
            temporary_files.append(temporary)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(content)
                if mode is not None:
                    os.fchmod(stream.fileno(), mode)
                stream.flush()
                os.fsync(stream.fileno())
            return temporary

        try:
            # Prepare all new files and rollback copies before the first replace.
            for change in self.changes:
                name = change['path']
                target = targets[name]
                target.parent.mkdir(parents=True, exist_ok=True)
                targets[name] = target = self.path(name, write=True)
                original, mode = originals[name]
                prepared[name] = prepare(target, change['after'].encode('utf-8'), mode)
                if original is not None:
                    backups[name] = prepare(target, original, mode)
            # Recheck paths/hashes after preparation. This is not a cross-file crash transaction.
            for name in targets:
                target = self.path(name, write=True)
                current = self.content(name)[1] if target.exists() else None
                expected = next(change['before_sha256'] for change in self.changes if change['path'] == name)
                if current != expected:
                    raise PolicyError(f'Целевой файл {name} изменился во время подготовки')
            for name, target in targets.items():
                os.replace(prepared[name], target)
                replaced.append(name)
        except (OSError, PolicyError) as exc:
            rollback_errors = []
            for name in reversed(replaced):
                try:
                    if originals[name][0] is None:
                        targets[name].unlink()
                    else:
                        os.replace(backups[name], targets[name])
                except OSError:
                    backup = backups.get(name)
                    if backup:
                        preserved_backups.add(backup)
                    rollback_errors.append(name + (f' (резервная копия: {backup})' if backup else ''))
            if rollback_errors:
                raise PolicyError('Ошибка сохранения; откат не завершён для: ' + ', '.join(rollback_errors)
                                  + '. Проверьте файлы вручную перед повторным запуском.') from exc
            if isinstance(exc, PolicyError):
                raise
            raise PolicyError('Ошибка сохранения; уже записанные файлы восстановлены. Повторите задачу.') from exc
        finally:
            for temporary in temporary_files:
                if temporary in preserved_backups:
                    continue
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        return self.result()
