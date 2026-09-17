"""Explicit SQLite persistence for the three Day 11 memory layers."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class MemoryLimitError(ValueError):
    """A deliberately small memory layer reached its learning limit."""


class SQLiteMemoryStore:
    """Single-user store. Tables deliberately mirror memory scopes, not UI forms."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions' LIMIT 1"
        ).fetchone():
            self._bootstrap_schema(connection)
            connection.commit()
        return connection

    @contextmanager
    def connection(self):
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        with self.connection() as connection:
            self._bootstrap_schema(connection)
        self.ensure_session()

    def _bootstrap_schema(self, connection):
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS long_term_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                goal TEXT NOT NULL DEFAULT '',
                hard_constraints TEXT NOT NULL DEFAULT '',
                task_data TEXT NOT NULL DEFAULT '',
                open_questions TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS session_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                label TEXT NOT NULL,
                parent_thread_id INTEGER REFERENCES session_threads(id),
                checkpoint_message_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                thread_id INTEGER REFERENCES session_threads(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
                request_preview_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS session_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                through_message_id INTEGER NOT NULL REFERENCES messages(id),
                content TEXT NOT NULL,
                request_preview_json TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS session_facts_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                through_user_message_id INTEGER NOT NULL REFERENCES messages(id),
                values_json TEXT NOT NULL,
                request_preview_json TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS session_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
                root_thread_id INTEGER NOT NULL REFERENCES session_threads(id),
                message_id INTEGER NOT NULL REFERENCES messages(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL CHECK (layer IN ('short_term', 'working', 'long_term')),
                action TEXT NOT NULL,
                actor TEXT NOT NULL CHECK (actor IN ('user', 'agent', 'system')),
                object_type TEXT NOT NULL,
                object_id INTEGER,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        # Earlier Day 11 builds split explicit long-term entries into
        # ``decision`` and ``knowledge``.  The lesson now treats long-term
        # memory as one user-owned collection, so rebuild only that table and
        # preserve the durable identity, text and timestamp of every entry.
        long_term_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(long_term_entries)")
        }
        if "entry_type" in long_term_columns:
            connection.executescript(
                """
                ALTER TABLE long_term_entries RENAME TO long_term_entries_legacy;
                CREATE TABLE long_term_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO long_term_entries(id, content, created_at)
                SELECT id, content, created_at FROM long_term_entries_legacy ORDER BY id;
                DROP TABLE long_term_entries_legacy;
                """
            )
        # Earlier Day 11 builds audited a personalization profile. That concept
        # now belongs exclusively to Day 12, so hide its stale technical rows
        # from migrated databases and the lesson UI.
        connection.execute("DELETE FROM memory_events WHERE object_type = 'profile'")
        connection.execute(
            """UPDATE memory_events SET object_type = 'entry'
               WHERE layer = 'long_term' AND object_type IN ('decision', 'knowledge')"""
        )
        # Day 11 originally selected the newest session implicitly.  Keep
        # databases created by that version usable, then make the selected
        # short-term scope an explicit, durable value.
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
        if "is_active" not in columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0")

        active_count = connection.execute("SELECT COUNT(*) FROM sessions WHERE is_active = 1").fetchone()[0]
        if active_count == 0:
            latest = connection.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
            if latest:
                connection.execute("UPDATE sessions SET is_active = 1 WHERE id = ?", (latest["id"],))
        elif active_count > 1:
            # Defensive repair for a manually edited / interrupted database
            # before the unique partial index below is installed.
            latest = connection.execute("SELECT id FROM sessions WHERE is_active = 1 ORDER BY id DESC LIMIT 1").fetchone()
            connection.execute("UPDATE sessions SET is_active = 0 WHERE id != ?", (latest["id"],))
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_session ON sessions(is_active) WHERE is_active = 1")

        session_columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
        if "active_branch_thread_id" not in session_columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN active_branch_thread_id INTEGER REFERENCES session_threads(id)")
        if "branched_from_id" not in session_columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN branched_from_id INTEGER REFERENCES sessions(id)")

        message_columns = {row["name"] for row in connection.execute("PRAGMA table_info(messages)")}
        if "archived" not in message_columns:
            connection.execute("ALTER TABLE messages ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        if "thread_id" not in message_columns:
            connection.execute("ALTER TABLE messages ADD COLUMN thread_id INTEGER REFERENCES session_threads(id)")

        for row in connection.execute("SELECT id FROM sessions ORDER BY id").fetchall():
            self.ensure_root_thread(row["id"], connection)
            connection.execute(
                "UPDATE messages SET thread_id = (SELECT id FROM session_threads WHERE session_id = messages.session_id AND parent_thread_id IS NULL LIMIT 1) WHERE session_id = ? AND thread_id IS NULL",
                (row["id"],),
            )

    @staticmethod
    def _entry(row):
        return dict(row) if row else None

    @staticmethod
    def _decode(value):
        return json.loads(value) if value else None

    def _event(self, connection, layer, action, actor, object_type, object_id=None, **details):
        connection.execute(
            """INSERT INTO memory_events(layer, action, actor, object_type, object_id, details_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (layer, action, actor, object_type, object_id, json.dumps(details, ensure_ascii=False)),
        )

    def ensure_session(self):
        session = self.active_session()
        return session if session else self.create_session()

    def long_term_entries(self):
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, content, created_at FROM long_term_entries ORDER BY id"
            ).fetchall()
        return [self._entry(row) for row in rows]

    def add_long_term(self, content):
        with self.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM long_term_entries").fetchone()[0]
            if count >= 40:
                raise MemoryLimitError("Можно сохранить до 40 записей долговременной памяти.")
            entry_id = connection.execute(
                "INSERT INTO long_term_entries(content) VALUES (?)", (content,)
            ).lastrowid
            self._event(connection, "long_term", "save", "user", "entry", entry_id)
        return next(item for item in self.long_term_entries() if item["id"] == entry_id)

    def delete_long_term(self, entry_id):
        with self.connection() as connection:
            row = connection.execute("SELECT id FROM long_term_entries WHERE id = ?", (entry_id,)).fetchone()
            if not row:
                return False
            connection.execute("DELETE FROM long_term_entries WHERE id = ?", (entry_id,))
            self._event(connection, "long_term", "delete", "user", "entry", entry_id)
        return True

    def task(self):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, goal, hard_constraints, task_data, open_questions, updated_at FROM tasks WHERE id = 1"
            ).fetchone()
        return self._entry(row)

    def save_task(self, values):
        """Replace every field: a new task cannot silently inherit working data."""
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO tasks(id, goal, hard_constraints, task_data, open_questions)
                   VALUES (1, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     goal = excluded.goal, hard_constraints = excluded.hard_constraints,
                     task_data = excluded.task_data, open_questions = excluded.open_questions,
                     updated_at = CURRENT_TIMESTAMP""",
                (values["goal"], values["hard_constraints"], values["task_data"], values["open_questions"]),
            )
            self._event(connection, "working", "replace", "user", "task", 1)
        return self.task()

    def sessions(self):
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT s.id, s.label, s.is_active, s.created_at, s.branched_from_id, COUNT(m.id) AS message_count
                   FROM sessions s LEFT JOIN messages m ON m.session_id = s.id
                   GROUP BY s.id ORDER BY s.id DESC"""
            ).fetchall()
        return [self._entry(row) for row in rows]

    def active_session(self):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, label, is_active, created_at, branched_from_id FROM sessions WHERE is_active = 1"
            ).fetchone()
        return self._entry(row)

    def session(self, session_id):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, label, is_active, created_at, branched_from_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._entry(row)

    def create_session(self):
        with self.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            if count >= 20:
                raise MemoryLimitError("Можно создать до 20 сессий. Сбросьте текущую для нового диалога.")
            connection.execute("UPDATE sessions SET is_active = 0 WHERE is_active = 1")
            session_id = connection.execute(
                "INSERT INTO sessions(label, is_active) VALUES (?, 1)", (f"Диалог {count + 1}",)
            ).lastrowid
            self.ensure_root_thread(session_id, connection)
            self._event(connection, "short_term", "create", "user", "session", session_id)
        return self.session(session_id)

    def branch_session(self, source_session_id):
        """Copy the current short-term session into a new active session (Day 10 branching as session fork)."""
        source = self.session(source_session_id)
        if not source:
            return None
        with self.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            if count >= 20:
                raise MemoryLimitError("Можно создать до 20 сессий. Сбросьте или удалите лишние ветки.")
            connection.execute("UPDATE sessions SET is_active = 0 WHERE is_active = 1")
            new_id = connection.execute(
                "INSERT INTO sessions(label, is_active, branched_from_id) VALUES (?, 1, ?)",
                (f"Ветка · {source['label']}", source_session_id),
            ).lastrowid
            new_root = self.ensure_root_thread(new_id, connection)
            old_messages = connection.execute(
                """SELECT id, role, content, archived, request_preview_json
                   FROM messages WHERE session_id = ? ORDER BY id""",
                (source_session_id,),
            ).fetchall()
            id_map = {}
            for row in old_messages:
                new_message_id = connection.execute(
                    """INSERT INTO messages(session_id, thread_id, role, content, archived, request_preview_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (new_id, new_root, row["role"], row["content"], row["archived"], row["request_preview_json"]),
                ).lastrowid
                id_map[row["id"]] = new_message_id
            for row in connection.execute(
                """SELECT through_message_id, content, request_preview_json, usage_json
                   FROM session_summaries WHERE session_id = ? ORDER BY id""",
                (source_session_id,),
            ).fetchall():
                connection.execute(
                    """INSERT INTO session_summaries(session_id, through_message_id, content, request_preview_json, usage_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (new_id, id_map[row["through_message_id"]], row["content"], row["request_preview_json"], row["usage_json"]),
                )
            for row in connection.execute(
                """SELECT through_user_message_id, values_json, request_preview_json, usage_json
                   FROM session_facts_snapshots WHERE session_id = ? ORDER BY id""",
                (source_session_id,),
            ).fetchall():
                connection.execute(
                    """INSERT INTO session_facts_snapshots(session_id, through_user_message_id, values_json, request_preview_json, usage_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (new_id, id_map[row["through_user_message_id"]], row["values_json"], row["request_preview_json"], row["usage_json"]),
                )
            self._event(connection, "short_term", "branch", "user", "session", new_id, source_session_id=source_session_id)
        return self.session(new_id)

    def activate_session(self, session_id):
        """Choose the single short-term scope without changing other layers."""
        with self.connection() as connection:
            if not connection.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
                return None
            current = connection.execute("SELECT id FROM sessions WHERE is_active = 1").fetchone()
            if not current or current["id"] != session_id:
                connection.execute("UPDATE sessions SET is_active = 0 WHERE is_active = 1")
                connection.execute("UPDATE sessions SET is_active = 1 WHERE id = ?", (session_id,))
                self._event(connection, "short_term", "activate", "user", "session", session_id)
        return self.session(session_id)

    def ensure_root_thread(self, session_id, connection=None):
        if connection is None:
            with self.connection() as managed:
                return self.ensure_root_thread(session_id, managed)
        row = connection.execute(
            "SELECT id FROM session_threads WHERE session_id = ? AND parent_thread_id IS NULL",
            (session_id,),
        ).fetchone()
        if row:
            return row["id"]
        return connection.execute(
            "INSERT INTO session_threads(session_id, label) VALUES (?, ?)",
            (session_id, "Корневая ветка"),
        ).lastrowid

    def root_thread_id(self, session_id):
        with self.connection() as connection:
            return self.ensure_root_thread(session_id, connection)

    def thread(self, thread_id):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, session_id, label, parent_thread_id, checkpoint_message_id, created_at FROM session_threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
        return self._entry(row)

    def session_threads(self, session_id):
        with self.connection() as connection:
            active_branch = connection.execute(
                "SELECT active_branch_thread_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            active_id = active_branch["active_branch_thread_id"] if active_branch else None
            rows = connection.execute(
                "SELECT id, session_id, label, parent_thread_id, checkpoint_message_id, created_at FROM session_threads WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [{**self._entry(row), "is_active": row["id"] == active_id} for row in rows]

    def active_thread_id(self, session_id):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT active_branch_thread_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row and row["active_branch_thread_id"]:
            return row["active_branch_thread_id"]
        return self.root_thread_id(session_id)

    def thread_messages(self, thread_id, include_archived=True, through_message_id=None):
        clause = "" if include_archived else "AND archived = 0"
        values = [thread_id]
        if through_message_id is not None:
            clause += " AND id <= ?"
            values.append(through_message_id)
        with self.connection() as connection:
            rows = connection.execute(
                f"""SELECT id, session_id, thread_id, role, content, archived, request_preview_json, created_at
                    FROM messages WHERE thread_id = ? {clause} ORDER BY id""",
                values,
            ).fetchall()
        return [
            {
                **self._entry(row),
                "archived": bool(row["archived"]),
                "preview": self._decode(row["request_preview_json"]),
            }
            for row in rows
        ]

    def _message_rows(self, session_id, include_archived=True):
        clause = "" if include_archived else "AND archived = 0"
        with self.connection() as connection:
            rows = connection.execute(
                f"""SELECT id, session_id, thread_id, role, content, archived, request_preview_json, created_at
                    FROM messages WHERE session_id = ? {clause} ORDER BY id""",
                (session_id,),
            ).fetchall()
        return [
            {
                **self._entry(row),
                "archived": bool(row["archived"]),
                "preview": self._decode(row["request_preview_json"]),
            }
            for row in rows
        ]

    def session_messages(self, session_id):
        return self._message_rows(session_id, include_archived=True)

    def active_session_messages(self, session_id, include_archived=False):
        return self._message_rows(session_id, include_archived=include_archived)

    def latest_summary(self, session_id):
        with self.connection() as connection:
            row = connection.execute(
                """SELECT id, session_id, through_message_id, content, request_preview_json, usage_json, created_at
                   FROM session_summaries WHERE session_id = ? ORDER BY id DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {
            **self._entry(row),
            "preview": self._decode(row["request_preview_json"]),
            "usage": self._decode(row["usage_json"]),
        }

    def summary_history(self, session_id):
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT id, session_id, through_message_id, content, request_preview_json, usage_json, created_at
                   FROM session_summaries WHERE session_id = ? ORDER BY id""",
                (session_id,),
            ).fetchall()
        return [
            {
                **self._entry(row),
                "preview": self._decode(row["request_preview_json"]),
                "usage": self._decode(row["usage_json"]),
            }
            for row in rows
        ]

    def compressible_messages(self, session_id, batch_size, keep_recent):
        active = self.active_session_messages(session_id, include_archived=False)
        return active[:batch_size] if len(active) >= batch_size + keep_recent else []

    def save_summary_and_archive(self, session_id, messages, content, preview, usage):
        if not messages:
            return None
        message_ids = [item["id"] for item in messages]
        with self.connection() as connection:
            summary_id = connection.execute(
                """INSERT INTO session_summaries(session_id, through_message_id, content, request_preview_json, usage_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    session_id,
                    message_ids[-1],
                    content,
                    json.dumps(preview, ensure_ascii=False),
                    json.dumps(usage, ensure_ascii=False),
                ),
            ).lastrowid
            placeholders = ", ".join("?" for _ in message_ids)
            connection.execute(f"UPDATE messages SET archived = 1 WHERE id IN ({placeholders})", message_ids)
            self._event(connection, "short_term", "save", "agent", "summary", summary_id)
        return summary_id

    def latest_facts(self, session_id):
        with self.connection() as connection:
            row = connection.execute(
                """SELECT id, session_id, through_user_message_id, values_json, request_preview_json, usage_json, created_at
                   FROM session_facts_snapshots WHERE session_id = ? ORDER BY id DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {
            **self._entry(row),
            "values": self._decode(row["values_json"]),
            "preview": self._decode(row["request_preview_json"]),
            "usage": self._decode(row["usage_json"]),
        }

    def facts_history(self, session_id):
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT id, session_id, through_user_message_id, values_json, request_preview_json, usage_json, created_at
                   FROM session_facts_snapshots WHERE session_id = ? ORDER BY id""",
                (session_id,),
            ).fetchall()
        return [
            {
                **self._entry(row),
                "values": self._decode(row["values_json"]),
                "preview": self._decode(row["request_preview_json"]),
                "usage": self._decode(row["usage_json"]),
            }
            for row in rows
        ]

    def save_facts(self, session_id, user_message_id, values, preview, usage):
        with self.connection() as connection:
            snapshot_id = connection.execute(
                """INSERT INTO session_facts_snapshots(session_id, through_user_message_id, values_json, request_preview_json, usage_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    session_id,
                    user_message_id,
                    json.dumps(values, ensure_ascii=False),
                    json.dumps(preview, ensure_ascii=False),
                    json.dumps(usage, ensure_ascii=False),
                ),
            ).lastrowid
            self._event(connection, "short_term", "save", "agent", "facts", snapshot_id)
        return snapshot_id

    def session_compression(self, session_id):
        return {
            "summary": self.latest_summary(session_id),
            "summaries": self.summary_history(session_id),
            "facts": self.latest_facts(session_id),
            "facts_history": self.facts_history(session_id),
            "archived_messages": sum(message["archived"] for message in self.active_session_messages(session_id, include_archived=True)),
            "active_messages": sum(not message["archived"] for message in self.active_session_messages(session_id, include_archived=True)),
        }

    def reset_session(self, session_id):
        with self.connection() as connection:
            if not connection.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
                return False
            connection.execute("DELETE FROM session_summaries WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM session_facts_snapshots WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM session_checkpoints WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM session_threads WHERE session_id = ?", (session_id,))
            connection.execute("UPDATE sessions SET active_branch_thread_id = NULL WHERE id = ?", (session_id,))
            deleted = connection.execute("DELETE FROM messages WHERE session_id = ?", (session_id,)).rowcount
            self.ensure_root_thread(session_id, connection)
            self._event(connection, "short_term", "reset", "user", "session", session_id, removed_messages=deleted)
        return True

    def append_turn(self, session_id, prompt, answer, preview, thread_id=None):
        """Persist a complete turn only after a non-empty provider answer exists."""
        if thread_id is None:
            thread_id = self.root_thread_id(session_id)
        with self.connection() as connection:
            if not connection.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
                raise ValueError("Сессия не найдена.")
            user_id = connection.execute(
                "INSERT INTO messages(session_id, thread_id, role, content) VALUES (?, ?, 'user', ?)",
                (session_id, thread_id, prompt),
            ).lastrowid
            assistant_id = connection.execute(
                """INSERT INTO messages(session_id, thread_id, role, content, request_preview_json)
                   VALUES (?, ?, 'assistant', ?, ?)""",
                (session_id, thread_id, answer, json.dumps(preview, ensure_ascii=False)),
            ).lastrowid
            self._event(connection, "short_term", "save", "user", "message", user_id)
            self._event(connection, "short_term", "save", "agent", "message", assistant_id)
        return {"user_id": user_id, "assistant_id": assistant_id}

    def events(self, limit=20):
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT id, layer, action, actor, object_type, object_id, details_json, created_at
                   FROM memory_events ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [{**self._entry(row), "details": self._decode(row["details_json"])} for row in rows]

    def clear_all_memory(self):
        """Wipe all persisted memory and start one empty session without audit events."""
        with self.connection() as connection:
            connection.executescript(
                """
                DELETE FROM memory_events;
                DELETE FROM session_checkpoints;
                DELETE FROM session_facts_snapshots;
                DELETE FROM session_summaries;
                DELETE FROM messages;
                DELETE FROM session_threads;
                DELETE FROM sessions;
                DELETE FROM long_term_entries;
                DELETE FROM tasks;
                """
            )
            session_id = connection.execute(
                "INSERT INTO sessions(label, is_active) VALUES ('Диалог 1', 1)"
            ).lastrowid
            self.ensure_root_thread(session_id, connection)
        return self.session(session_id)

    def state(self):
        active = self.ensure_session()
        messages = self.session_messages(active["id"])
        compression = self.session_compression(active["id"])
        entries = self.long_term_entries()
        task = self.task()
        return {
            "long_term": entries,
            "task": task,
            "sessions": self.sessions(),
            "active_session": {**active, "messages": messages, "compression": compression},
            "events": self.events(),
            "metrics": {
                "short_term_messages": len(messages),
                "sessions": len(self.sessions()),
                "long_term_entries": len(entries),
                "working_task": bool(task and task["goal"]),
                "summaries": len(compression["summaries"]),
                "facts_updates": len(compression["facts_history"]),
            },
        }
