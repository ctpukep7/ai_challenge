"""SQLite persistence for Day 07 conversation context."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class SQLiteConversationStore:
    """Stores conversations and ordered LLM messages in one local SQLite file."""

    def __init__(self, database_path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def connection(self):
        """Commit a store operation and always release its SQLite connection."""
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    request_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        self.ensure_conversation()

    def ensure_conversation(self):
        conversations = self.list_conversations(create_if_empty=False)
        return conversations[0] if conversations else self.create_conversation()

    def list_conversations(self, create_if_empty=True):
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT conversations.id, conversations.title, COUNT(messages.id) / 2 AS answer_count
                FROM conversations LEFT JOIN messages ON messages.conversation_id = conversations.id
                GROUP BY conversations.id
                ORDER BY conversations.id DESC
                """
            ).fetchall()
        if not rows and create_if_empty:
            return [self.create_conversation()]
        return [dict(row) for row in rows]

    def create_conversation(self):
        with self.connection() as connection:
            cursor = connection.execute("INSERT INTO conversations(title) VALUES ('Новый диалог')")
            conversation_id = cursor.lastrowid
        return {"id": conversation_id, "title": "Новый диалог", "answer_count": 0}

    def get_conversation(self, conversation_id):
        with self.connection() as connection:
            conversation = connection.execute("SELECT id, title FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            messages = connection.execute(
                "SELECT id, role, content, request_json FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,)
            ).fetchall()
        if not conversation:
            return None
        result = []
        for message in messages:
            item = dict(message)
            item["request"] = json.loads(item.pop("request_json")) if item.get("request_json") else None
            result.append(item)
        return {"id": conversation["id"], "title": conversation["title"], "messages": result}

    def rename_conversation(self, conversation_id, title):
        """Persist a user-chosen title for a conversation."""
        with self.connection() as connection:
            updated = connection.execute(
                "UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id)
            ).rowcount
        return bool(updated)

    def delete_conversation(self, conversation_id):
        with self.connection() as connection:
            deleted = connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,)).rowcount
        if not deleted:
            return None
        return self.ensure_conversation()

    def append_turn(self, conversation_id, prompt, answer, request):
        title = prompt.replace("\n", " ").strip()[:34]
        with self.connection() as connection:
            conversation = connection.execute("SELECT title FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if not conversation:
                return False
            if conversation["title"] == "Новый диалог":
                connection.execute("UPDATE conversations SET title = ? WHERE id = ?", (title or "Новый диалог", conversation_id))
            connection.executemany(
                "INSERT INTO messages(conversation_id, role, content, request_json) VALUES (?, ?, ?, ?)",
                [(conversation_id, "user", prompt, None), (conversation_id, "assistant", answer, json.dumps(request, ensure_ascii=False))],
            )
        return True
