"""SQLite persistence for paired full and compressed context experiments."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class SQLiteExperimentStore:
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
                CREATE TABLE IF NOT EXISTS experiments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL DEFAULT 'Новый эксперимент',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS branches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
                    mode TEXT NOT NULL CHECK(mode IN ('full', 'compressed')),
                    UNIQUE(experiment_id, mode)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    request_json TEXT,
                    usage_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                    through_message_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    usage_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS comparison_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
                    prompt TEXT NOT NULL,
                    full_assistant_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                    compressed_assistant_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        self.ensure_experiment()

    @staticmethod
    def _decode(value):
        return json.loads(value) if value else None

    def ensure_experiment(self):
        experiments = self.list_experiments(create_if_empty=False)
        return experiments[0] if experiments else self.create_experiment()

    def list_experiments(self, create_if_empty=True):
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT e.id, e.title, COUNT(t.id) AS turn_count
                FROM experiments e LEFT JOIN comparison_turns t ON t.experiment_id = e.id
                GROUP BY e.id ORDER BY e.id DESC"""
            ).fetchall()
        if not rows and create_if_empty:
            return [self.create_experiment()]
        return [dict(row) for row in rows]

    def create_experiment(self):
        with self.connection() as connection:
            cursor = connection.execute("INSERT INTO experiments(title) VALUES ('Новый эксперимент')")
            experiment_id = cursor.lastrowid
            connection.executemany(
                "INSERT INTO branches(experiment_id, mode) VALUES (?, ?)",
                [(experiment_id, "full"), (experiment_id, "compressed")],
            )
        return {"id": experiment_id, "title": "Новый эксперимент", "turn_count": 0}

    def rename_experiment(self, experiment_id: int, title: str):
        with self.connection() as connection:
            return bool(connection.execute("UPDATE experiments SET title = ? WHERE id = ?", (title, experiment_id)).rowcount)

    def delete_experiment(self, experiment_id: int):
        with self.connection() as connection:
            deleted = connection.execute("DELETE FROM experiments WHERE id = ?", (experiment_id,)).rowcount
        return self.ensure_experiment() if deleted else None

    def branch_id(self, experiment_id: int, mode: str):
        with self.connection() as connection:
            row = connection.execute("SELECT id FROM branches WHERE experiment_id = ? AND mode = ?", (experiment_id, mode)).fetchone()
        return row["id"] if row else None

    def branch_messages(self, branch_id: int, include_archived=True):
        clause = "" if include_archived else "AND archived = 0"
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT id, role, content, archived, request_json, usage_json FROM messages WHERE branch_id = ? {clause} ORDER BY id",
                (branch_id,),
            ).fetchall()
        return [
            {**dict(row), "archived": bool(row["archived"]), "request": self._decode(row["request_json"]), "usage": self._decode(row["usage_json"])}
            for row in rows
        ]

    def latest_summary(self, branch_id: int):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, through_message_id, content, request_json, usage_json FROM summaries WHERE branch_id = ? ORDER BY id DESC LIMIT 1",
                (branch_id,),
            ).fetchone()
        if not row:
            return None
        return {**dict(row), "request": self._decode(row["request_json"]), "usage": self._decode(row["usage_json"])}

    def summary_history(self, branch_id: int):
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, through_message_id, content, request_json, usage_json FROM summaries WHERE branch_id = ? ORDER BY id",
                (branch_id,),
            ).fetchall()
        return [{**dict(row), "request": self._decode(row["request_json"]), "usage": self._decode(row["usage_json"])} for row in rows]

    def append_turn(self, branch_id: int, prompt: str, answer: str, request: dict, usage: dict):
        with self.connection() as connection:
            user = connection.execute("INSERT INTO messages(branch_id, role, content) VALUES (?, 'user', ?)", (branch_id, prompt))
            assistant = connection.execute(
                "INSERT INTO messages(branch_id, role, content, request_json, usage_json) VALUES (?, 'assistant', ?, ?, ?)",
                (branch_id, answer, json.dumps(request, ensure_ascii=False), json.dumps(usage, ensure_ascii=False)),
            )
        return {"user_id": user.lastrowid, "assistant_id": assistant.lastrowid}

    def add_turn_pair(self, experiment_id: int, prompt: str, full_assistant_id: int | None, compressed_assistant_id: int | None):
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO comparison_turns(experiment_id, prompt, full_assistant_id, compressed_assistant_id) VALUES (?, ?, ?, ?)",
                (experiment_id, prompt, full_assistant_id, compressed_assistant_id),
            )
        return cursor.lastrowid

    def compressible_messages(self, branch_id: int, batch_size: int, keep_recent: int):
        active = self.branch_messages(branch_id, include_archived=False)
        return active[:batch_size] if len(active) >= batch_size + keep_recent else []

    def save_summary_and_archive(self, branch_id: int, messages: list[dict], content: str, request: dict, usage: dict):
        if not messages:
            return None
        message_ids = [item["id"] for item in messages]
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO summaries(branch_id, through_message_id, content, request_json, usage_json) VALUES (?, ?, ?, ?, ?)",
                (branch_id, message_ids[-1], content, json.dumps(request, ensure_ascii=False), json.dumps(usage, ensure_ascii=False)),
            )
            placeholders = ", ".join("?" for _ in message_ids)
            connection.execute(f"UPDATE messages SET archived = 1 WHERE id IN ({placeholders})", message_ids)
        return cursor.lastrowid

    def get_experiment(self, experiment_id: int):
        with self.connection() as connection:
            row = connection.execute("SELECT id, title FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
            turns = connection.execute(
                "SELECT id, prompt, full_assistant_id, compressed_assistant_id FROM comparison_turns WHERE experiment_id = ? ORDER BY id",
                (experiment_id,),
            ).fetchall()
        if not row:
            return None
        branches = {}
        for mode in ("full", "compressed"):
            branch_id = self.branch_id(experiment_id, mode)
            summaries = self.summary_history(branch_id) if mode == "compressed" else []
            branches[mode] = {
                "id": branch_id,
                "messages": self.branch_messages(branch_id),
                "summary": summaries[-1] if summaries else None,
                "summaries": summaries,
            }
        return {"id": row["id"], "title": row["title"], "branches": branches, "turns": [dict(item) for item in turns]}

    @staticmethod
    def stats(messages: list[dict], summaries: list[dict]):
        usage_rows = [item["usage"] for item in messages if item["role"] == "assistant" and item["usage"]]
        summary_rows = [item["usage"] for item in summaries if item.get("usage")]
        def totals(rows):
            result = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "unknown_cost": False}
            for usage in rows:
                for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if isinstance(usage.get(field), int): result[field] += usage[field]
                if isinstance(usage.get("cost_usd"), (int, float)): result["cost_usd"] += usage["cost_usd"]
                else: result["unknown_cost"] = True
            result["cost_usd"] = round(result["cost_usd"], 10)
            return result
        answer_total, summary_total = totals(usage_rows), totals(summary_rows)
        return {
            "answers": answer_total,
            "summary": summary_total,
            "overall": {**{key: answer_total[key] + summary_total[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd")}, "unknown_cost": answer_total["unknown_cost"] or summary_total["unknown_cost"]},
            "latest": usage_rows[-1] if usage_rows else None,
            "archived_messages": sum(item["archived"] for item in messages),
            "active_messages": sum(not item["archived"] for item in messages),
        }

    def experiment_stats(self, experiment_id: int):
        experiment = self.get_experiment(experiment_id)
        if not experiment: return None
        full = self.stats(experiment["branches"]["full"]["messages"], [])
        compressed_branch = experiment["branches"]["compressed"]
        compressed = self.stats(compressed_branch["messages"], self.summary_history(compressed_branch["id"]))
        full_prompt = full["latest"].get("prompt_tokens") if full["latest"] else None
        compressed_prompt = compressed["latest"].get("prompt_tokens") if compressed["latest"] else None
        saved = full_prompt - compressed_prompt if isinstance(full_prompt, int) and isinstance(compressed_prompt, int) else None
        return {"full": full, "compressed": compressed, "latest_saved_prompt_tokens": saved, "latest_saved_percent": round(saved / full_prompt * 100, 1) if saved is not None and full_prompt else None}
