"""SQLite source of truth for Day 08 chats and exact token metadata."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class SQLiteConversationStore:
    def __init__(self, database_path: str | Path):
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
                    usage_json TEXT,
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
                """SELECT conversations.id, conversations.title, COUNT(messages.id) / 2 AS answer_count
                FROM conversations LEFT JOIN messages ON messages.conversation_id = conversations.id
                GROUP BY conversations.id ORDER BY conversations.id DESC"""
            ).fetchall()
        if not rows and create_if_empty:
            return [self.create_conversation()]
        return [dict(row) for row in rows]

    def create_conversation(self):
        with self.connection() as connection:
            cursor = connection.execute("INSERT INTO conversations(title) VALUES ('Новый диалог')")
        return {"id": cursor.lastrowid, "title": "Новый диалог", "answer_count": 0}

    def get_conversation(self, conversation_id: int):
        with self.connection() as connection:
            conversation = connection.execute("SELECT id, title FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            messages = connection.execute(
                "SELECT id, role, content, request_json, usage_json FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,)
            ).fetchall()
        if not conversation:
            return None
        decoded = []
        for message in messages:
            item = dict(message)
            item["request"] = self._decode(item.pop("request_json"))
            item["usage"] = self._decode(item.pop("usage_json"))
            decoded.append(item)
        return {"id": conversation["id"], "title": conversation["title"], "messages": decoded}

    @staticmethod
    def _decode(value):
        return json.loads(value) if value else None

    def rename_conversation(self, conversation_id: int, title: str):
        with self.connection() as connection:
            return bool(connection.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id)).rowcount)

    def delete_conversation(self, conversation_id: int):
        with self.connection() as connection:
            deleted = connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,)).rowcount
        return self.ensure_conversation() if deleted else None

    def append_turn(self, conversation_id: int, prompt: str, answer: str, request: dict, usage: dict):
        title = prompt.replace("\n", " ").strip()[:34] or "Новый диалог"
        with self.connection() as connection:
            conversation = connection.execute("SELECT title FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if not conversation:
                return False
            if conversation["title"] == "Новый диалог":
                connection.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id))
            connection.executemany(
                "INSERT INTO messages(conversation_id, role, content, request_json, usage_json) VALUES (?, ?, ?, ?, ?)",
                [
                    (conversation_id, "user", prompt, None, None),
                    (conversation_id, "assistant", answer, json.dumps(request, ensure_ascii=False), json.dumps(usage, ensure_ascii=False)),
                ],
            )
        return True

    def token_stats(self, conversation_id: int):
        conversation = self.get_conversation(conversation_id)
        if not conversation:
            return None
        turns = [item["usage"] for item in conversation["messages"] if item["role"] == "assistant" and item["usage"]]
        sums = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
        unknown_cost = False
        points = []
        for index, usage in enumerate(turns, start=1):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if isinstance(usage.get(key), int):
                    sums[key] += usage[key]
            if isinstance(usage.get("cost_usd"), (int, float)):
                sums["cost_usd"] += usage["cost_usd"]
            else:
                unknown_cost = True
            context = usage.get("context_length")
            prompt = usage.get("prompt_tokens")
            percent = round(prompt / context * 100, 4) if isinstance(context, int) and context > 0 and isinstance(prompt, int) else None
            points.append({
                "turn": index,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "cumulative_total_tokens": sums["total_tokens"],
                "cumulative_cost_usd": round(sums["cost_usd"], 10),
                "context_percent": percent,
            })
        return {"turns": len(turns), "totals": {**sums, "cost_usd": round(sums["cost_usd"], 10), "has_unknown_cost": unknown_cost}, "points": points, "latest": turns[-1] if turns else None}
