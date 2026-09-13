"""SQLite persistence for the Day 10 context-strategy laboratory."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class SQLiteStrategyStore:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
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
                    active_branch_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS threads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
                    strategy TEXT NOT NULL CHECK(strategy IN ('sliding', 'facts', 'branching')),
                    label TEXT NOT NULL,
                    parent_thread_id INTEGER REFERENCES threads(id) ON DELETE CASCADE,
                    checkpoint_message_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    request_json TEXT,
                    usage_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS facts_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                    through_user_message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    values_json TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    usage_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_id INTEGER NOT NULL UNIQUE REFERENCES experiments(id) ON DELETE CASCADE,
                    root_thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
                    prompt TEXT NOT NULL,
                    sliding_assistant_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                    facts_assistant_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                    branching_assistant_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                    branching_thread_id INTEGER REFERENCES threads(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        self.ensure_experiment()

    @staticmethod
    def _decode(value):
        return json.loads(value) if value else None

    @staticmethod
    def _message(row):
        return {
            **dict(row),
            "request": SQLiteStrategyStore._decode(row["request_json"]),
            "usage": SQLiteStrategyStore._decode(row["usage_json"]),
        }

    def ensure_experiment(self):
        experiments = self.list_experiments(create_if_empty=False)
        return experiments[0] if experiments else self.create_experiment()

    def list_experiments(self, create_if_empty=True):
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT e.id, e.title, COUNT(t.id) AS turn_count
                FROM experiments e LEFT JOIN turns t ON t.experiment_id = e.id
                GROUP BY e.id ORDER BY e.id DESC
                """
            ).fetchall()
        if not rows and create_if_empty:
            return [self.create_experiment()]
        return [dict(row) for row in rows]

    def create_experiment(self):
        with self.connection() as connection:
            cursor = connection.execute("INSERT INTO experiments(title) VALUES ('Новый эксперимент')")
            experiment_id = cursor.lastrowid
            thread_ids = {}
            for strategy, label in (("sliding", "Sliding Window"), ("facts", "Sticky Facts"), ("branching", "Корневая ветка")):
                thread_ids[strategy] = connection.execute(
                    "INSERT INTO threads(experiment_id, strategy, label) VALUES (?, ?, ?)",
                    (experiment_id, strategy, label),
                ).lastrowid
            connection.execute("UPDATE experiments SET active_branch_id = ? WHERE id = ?", (thread_ids["branching"], experiment_id))
        return {"id": experiment_id, "title": "Новый эксперимент", "turn_count": 0}

    def rename_experiment(self, experiment_id, title):
        with self.connection() as connection:
            return bool(connection.execute("UPDATE experiments SET title = ? WHERE id = ?", (title, experiment_id)).rowcount)

    def delete_experiment(self, experiment_id):
        with self.connection() as connection:
            deleted = connection.execute("DELETE FROM experiments WHERE id = ?", (experiment_id,)).rowcount
        return self.ensure_experiment() if deleted else None

    def strategy_root(self, experiment_id, strategy):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id FROM threads WHERE experiment_id = ? AND strategy = ? AND parent_thread_id IS NULL",
                (experiment_id, strategy),
            ).fetchone()
        return row["id"] if row else None

    def thread_messages(self, thread_id, through_message_id=None):
        where = "thread_id = ?"
        values = [thread_id]
        if through_message_id is not None:
            where += " AND id <= ?"
            values.append(through_message_id)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT id, thread_id, role, content, request_json, usage_json, created_at FROM messages WHERE {where} ORDER BY id",
                values,
            ).fetchall()
        return [self._message(row) for row in rows]

    def thread(self, thread_id):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, experiment_id, strategy, label, parent_thread_id, checkpoint_message_id, created_at FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
        return dict(row) if row else None

    def active_branch(self, experiment_id):
        with self.connection() as connection:
            row = connection.execute("SELECT active_branch_id FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
        return row["active_branch_id"] if row and row["active_branch_id"] else self.strategy_root(experiment_id, "branching")

    def branch_lineage(self, thread_id):
        thread = self.thread(thread_id)
        if not thread:
            return []
        if not thread["parent_thread_id"]:
            return self.thread_messages(thread_id)
        parent = self.branch_lineage(thread["parent_thread_id"])
        checkpoint = thread["checkpoint_message_id"]
        inherited = [message for message in parent if message["id"] <= checkpoint]
        return inherited + self.thread_messages(thread_id)

    def append_turn(self, thread_id, prompt, answer, request, usage):
        with self.connection() as connection:
            user = connection.execute("INSERT INTO messages(thread_id, role, content) VALUES (?, 'user', ?)", (thread_id, prompt))
            assistant = connection.execute(
                "INSERT INTO messages(thread_id, role, content, request_json, usage_json) VALUES (?, 'assistant', ?, ?, ?)",
                (thread_id, answer, json.dumps(request, ensure_ascii=False), json.dumps(usage, ensure_ascii=False)),
            )
        return {"user_id": user.lastrowid, "assistant_id": assistant.lastrowid}

    def latest_facts(self, thread_id):
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT id, through_user_message_id, values_json, request_json, usage_json, created_at
                FROM facts_snapshots WHERE thread_id = ? ORDER BY id DESC LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
        if not row:
            return None
        return {
            **dict(row),
            "values": self._decode(row["values_json"]),
            "request": self._decode(row["request_json"]),
            "usage": self._decode(row["usage_json"]),
        }

    def facts_history(self, thread_id):
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, through_user_message_id, values_json, request_json, usage_json, created_at FROM facts_snapshots WHERE thread_id = ? ORDER BY id",
                (thread_id,),
            ).fetchall()
        return [
            {**dict(row), "values": self._decode(row["values_json"]), "request": self._decode(row["request_json"]), "usage": self._decode(row["usage_json"])}
            for row in rows
        ]

    def save_facts(self, thread_id, user_id, values, request, usage):
        with self.connection() as connection:
            return connection.execute(
                "INSERT INTO facts_snapshots(thread_id, through_user_message_id, values_json, request_json, usage_json) VALUES (?, ?, ?, ?, ?)",
                (thread_id, user_id, json.dumps(values, ensure_ascii=False), json.dumps(request, ensure_ascii=False), json.dumps(usage, ensure_ascii=False)),
            ).lastrowid

    def checkpoint(self, experiment_id):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, root_thread_id, message_id, created_at FROM checkpoints WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_checkpoint(self, experiment_id):
        existing = self.checkpoint(experiment_id)
        if existing:
            return self.get_experiment(experiment_id)
        root = self.strategy_root(experiment_id, "branching")
        messages = self.thread_messages(root)
        assistant_messages = [message for message in messages if message["role"] == "assistant"]
        if not assistant_messages:
            raise ValueError("Сначала получите хотя бы один ответ в корневой ветке.")
        message_id = assistant_messages[-1]["id"]
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO checkpoints(experiment_id, root_thread_id, message_id) VALUES (?, ?, ?)",
                (experiment_id, root, message_id),
            )
            branch_a = connection.execute(
                "INSERT INTO threads(experiment_id, strategy, label, parent_thread_id, checkpoint_message_id) VALUES (?, 'branching', 'Ветка A', ?, ?)",
                (experiment_id, root, message_id),
            ).lastrowid
            connection.execute(
                "INSERT INTO threads(experiment_id, strategy, label, parent_thread_id, checkpoint_message_id) VALUES (?, 'branching', 'Ветка B', ?, ?)",
                (experiment_id, root, message_id),
            )
            connection.execute("UPDATE experiments SET active_branch_id = ? WHERE id = ?", (branch_a, experiment_id))
        return self.get_experiment(experiment_id)

    def set_active_branch(self, experiment_id, thread_id):
        thread = self.thread(thread_id)
        if not thread or thread["experiment_id"] != experiment_id or thread["strategy"] != "branching":
            return False
        with self.connection() as connection:
            connection.execute("UPDATE experiments SET active_branch_id = ? WHERE id = ?", (thread_id, experiment_id))
        return True

    def add_turn(self, experiment_id, prompt, results):
        ids = {strategy: results.get(strategy, {}).get("assistant_id") for strategy in ("sliding", "facts", "branching")}
        branch_id = results.get("branching", {}).get("branch_thread_id")
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO turns(experiment_id, prompt, sliding_assistant_id, facts_assistant_id, branching_assistant_id, branching_thread_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (experiment_id, prompt, ids["sliding"], ids["facts"], ids["branching"], branch_id),
            )

    def _threads_for(self, experiment_id, strategy):
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, experiment_id, strategy, label, parent_thread_id, checkpoint_message_id, created_at FROM threads WHERE experiment_id = ? AND strategy = ? ORDER BY id",
                (experiment_id, strategy),
            ).fetchall()
        return [dict(row) for row in rows]

    def _message_index(self, experiment_id):
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT m.id, m.thread_id, m.role, m.content, m.request_json, m.usage_json, m.created_at
                FROM messages m JOIN threads t ON t.id = m.thread_id
                WHERE t.experiment_id = ? ORDER BY m.id
                """,
                (experiment_id,),
            ).fetchall()
        return {str(row["id"]): self._message(row) for row in rows}

    def get_experiment(self, experiment_id):
        with self.connection() as connection:
            experiment = connection.execute(
                "SELECT id, title, active_branch_id, created_at FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
            turns = connection.execute(
                "SELECT id, prompt, sliding_assistant_id, facts_assistant_id, branching_assistant_id, branching_thread_id, created_at FROM turns WHERE experiment_id = ? ORDER BY id",
                (experiment_id,),
            ).fetchall()
        if not experiment:
            return None
        sliding_id = self.strategy_root(experiment_id, "sliding")
        facts_id = self.strategy_root(experiment_id, "facts")
        branching_threads = self._threads_for(experiment_id, "branching")
        active_id = experiment["active_branch_id"]
        return {
            **dict(experiment),
            "turns": [dict(row) for row in turns],
            "messages": self._message_index(experiment_id),
            "strategies": {
                "sliding": {"thread_id": sliding_id, "messages": self.thread_messages(sliding_id)},
                "facts": {
                    "thread_id": facts_id,
                    "messages": self.thread_messages(facts_id),
                    "facts": self.latest_facts(facts_id),
                    "facts_history": self.facts_history(facts_id),
                },
                "branching": {
                    "active_thread_id": active_id,
                    "messages": self.branch_lineage(active_id),
                    "threads": [{**thread, "is_active": thread["id"] == active_id} for thread in branching_threads],
                    "checkpoint": self.checkpoint(experiment_id),
                },
            },
        }

    @staticmethod
    def _usage_total(usages):
        result = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "unknown_cost": False}
        for usage in usages:
            if not usage:
                continue
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if isinstance(usage.get(key), int):
                    result[key] += usage[key]
            if isinstance(usage.get("cost_usd"), (int, float)):
                result["cost_usd"] += usage["cost_usd"]
            else:
                result["unknown_cost"] = True
        result["cost_usd"] = round(result["cost_usd"], 10)
        return result

    def experiment_stats(self, experiment_id):
        experiment = self.get_experiment(experiment_id)
        if not experiment:
            return None
        stats = {}
        for strategy in ("sliding", "facts", "branching"):
            if strategy == "branching":
                messages = [message for message in experiment["messages"].values() if self.thread(message["thread_id"])["strategy"] == "branching"]
                facts = []
                latest_messages = experiment["strategies"]["branching"]["messages"]
            else:
                branch = experiment["strategies"][strategy]
                messages = branch["messages"]
                facts = branch.get("facts_history", [])
                latest_messages = messages
            replies = [message["usage"] for message in messages if message["role"] == "assistant" and message["usage"]]
            fact_usages = [item["usage"] for item in facts]
            answer_total = self._usage_total(replies)
            facts_total = self._usage_total(fact_usages)
            stats[strategy] = {
                "answers": answer_total,
                "facts": facts_total,
                "overall": {
                    **{key: answer_total[key] + facts_total[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd")},
                    "unknown_cost": answer_total["unknown_cost"] or facts_total["unknown_cost"],
                },
                "latest": next((message["usage"] for message in reversed(latest_messages) if message["role"] == "assistant" and message["usage"]), None),
            }
        return stats
