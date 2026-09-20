"""SQLite persistence with profile-scoped conversations and shared explicit memory."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

class MemoryLimitError(ValueError):
    pass


DEFAULT_PROFILES = (
    {
        "name": "Новичок",
        "role_context": "Я изучаю тему впервые. Используй простые термины и поясняй новые понятия.",
        "language": "Русский",
        "tone": "friendly",
        "detail_level": "balanced",
        "response_format": "steps",
        "constraints": "Дай понятный пример и не пропускай важные промежуточные шаги.",
    },
    {
        "name": "Обычный",
        "role_context": "Мне нужен универсальный ответ без предположения о специальной подготовке.",
        "language": "Русский",
        "tone": "neutral",
        "detail_level": "balanced",
        "response_format": "paragraphs",
        "constraints": "Сначала дай прямой ответ, затем необходимые пояснения и практические шаги.",
    },
    {
        "name": "Эксперт",
        "role_context": "Я технический специалист и знаком с архитектурой AI-систем.",
        "language": "Русский",
        "tone": "neutral",
        "detail_level": "deep",
        "response_format": "paragraphs",
        "constraints": "Явно называй допущения, компромиссы, ограничения и пограничные случаи.",
    },
    {
        "name": "Руководитель",
        "role_context": "Я принимаю продуктовые и технические решения.",
        "language": "Русский",
        "tone": "business",
        "detail_level": "brief",
        "response_format": "bullets",
        "constraints": "Начни с вывода; выдели риски, решение и следующие шаги.",
    },
)

PROFILE_FIELDS = ("name", "role_context", "language", "tone", "detail_level", "response_format", "constraints")
BUILTIN_PROFILES_MIGRATION = "day14_builtin_profiles_v1"
AUTOINCREMENT_TABLES = (
    "user_profiles", "long_term_entries", "invariants", "invariant_check_events", "sessions", "messages",
    "session_summaries", "session_facts_snapshots", "memory_events",
)


class SQLiteMemoryStore:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
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
                CREATE TABLE IF NOT EXISTS user_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    role_context TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT '',
                    tone TEXT NOT NULL CHECK (tone IN ('friendly','neutral','business')),
                    detail_level TEXT NOT NULL CHECK (detail_level IN ('brief','balanced','deep')),
                    response_format TEXT NOT NULL CHECK (response_format IN ('paragraphs','bullets','steps','table')),
                    constraints TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0,1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_profile ON user_profiles(is_active) WHERE is_active=1;
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS long_term_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_type TEXT NOT NULL CHECK (entry_type IN ('decision','knowledge')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS invariants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL CHECK (category IN ('architecture','stack','technical_decision','business_rule')),
                    field TEXT NOT NULL,
                    operator TEXT NOT NULL CHECK (operator IN ('equals','not_equals','one_of','not_one_of')),
                    expected_value TEXT NOT NULL,
                    description TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    compatible_alternative TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS invariant_check_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_field TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN ('allowed','rejected')),
                    checked_invariant_ids_json TEXT NOT NULL DEFAULT '[]',
                    violated_invariant_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    goal TEXT NOT NULL DEFAULT '', hard_constraints TEXT NOT NULL DEFAULT '',
                    task_data TEXT NOT NULL DEFAULT '', open_questions TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0,1)),
                    branched_from_id INTEGER REFERENCES sessions(id),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_per_profile ON sessions(profile_id) WHERE is_active=1;
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1)),
                    request_preview_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS session_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    through_message_id INTEGER NOT NULL REFERENCES messages(id),
                    content TEXT NOT NULL, request_preview_json TEXT NOT NULL, usage_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS session_facts_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    through_user_message_id INTEGER NOT NULL REFERENCES messages(id),
                    values_json TEXT NOT NULL, request_preview_json TEXT NOT NULL, usage_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS memory_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    layer TEXT NOT NULL CHECK (layer IN ('personalization','short_term','working','long_term')),
                    action TEXT NOT NULL, actor TEXT NOT NULL,
                    object_type TEXT NOT NULL, object_id INTEGER,
                    details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            connection.execute("INSERT OR IGNORE INTO tasks(id) VALUES (1)")
            if connection.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0] == 0:
                self._seed_defaults(connection)
            else:
                migration_applied = bool(connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?", (BUILTIN_PROFILES_MIGRATION,)
                ).fetchone())
                self._ensure_builtin_profiles(connection)
                if not migration_applied:
                    self._activate_profile_row(connection, "Обычный")
                    connection.execute(
                        "INSERT INTO schema_migrations(version) VALUES (?)", (BUILTIN_PROFILES_MIGRATION,)
                    )
                elif not connection.execute("SELECT 1 FROM user_profiles WHERE is_active=1").fetchone():
                    self._activate_profile_row(connection, "Обычный")

    @staticmethod
    def _entry(row):
        return dict(row) if row else None

    @staticmethod
    def _decode(value):
        return json.loads(value) if value else None

    def _event(self, connection, layer, action, actor, object_type, object_id=None, **details):
        connection.execute(
            "INSERT INTO memory_events(layer,action,actor,object_type,object_id,details_json) VALUES (?,?,?,?,?,?)",
            (layer, action, actor, object_type, object_id, json.dumps(details, ensure_ascii=False)),
        )

    def _seed_defaults(self, connection):
        for values in DEFAULT_PROFILES:
            profile_id = connection.execute(
                """INSERT INTO user_profiles(name,role_context,language,tone,detail_level,response_format,constraints,is_active)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (*(values[key] for key in PROFILE_FIELDS), 1 if values["name"] == "Обычный" else 0),
            ).lastrowid
            connection.execute(
                "INSERT INTO sessions(profile_id,label,is_active) VALUES (?, 'Диалог 1', 1)", (profile_id,)
            )
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (BUILTIN_PROFILES_MIGRATION,)
        )

    @staticmethod
    def _activate_profile_row(connection, name):
        connection.execute("UPDATE user_profiles SET is_active=0 WHERE is_active=1")
        connection.execute("UPDATE user_profiles SET is_active=1 WHERE name=?", (name,))

    def _ensure_builtin_profiles(self, connection):
        for values in DEFAULT_PROFILES:
            row = connection.execute("SELECT id FROM user_profiles WHERE name=?", (values["name"],)).fetchone()
            if row:
                profile_id = row["id"]
            else:
                profile_id = connection.execute(
                    """INSERT INTO user_profiles(name,role_context,language,tone,detail_level,response_format,constraints,is_active)
                       VALUES (?,?,?,?,?,?,?,0)""",
                    tuple(values[key] for key in PROFILE_FIELDS),
                ).lastrowid
            if not connection.execute(
                "SELECT 1 FROM sessions WHERE profile_id=? AND is_active=1", (profile_id,)
            ).fetchone():
                latest = connection.execute(
                    "SELECT id FROM sessions WHERE profile_id=? ORDER BY id DESC LIMIT 1", (profile_id,)
                ).fetchone()
                if latest:
                    connection.execute("UPDATE sessions SET is_active=1 WHERE id=?", (latest["id"],))
                else:
                    connection.execute(
                        "INSERT INTO sessions(profile_id,label,is_active) VALUES (?, 'Диалог 1', 1)", (profile_id,)
                    )

    def profiles(self):
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM user_profiles ORDER BY id").fetchall()
        return [self._entry(row) for row in rows]

    def profile(self, profile_id):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM user_profiles WHERE id=?", (profile_id,)).fetchone()
        return self._entry(row)

    def active_profile(self):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM user_profiles WHERE is_active=1").fetchone()
        return self._entry(row)

    def create_profile(self, values):
        with self.connection() as connection:
            if connection.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0] >= 10:
                raise MemoryLimitError("Можно создать до 10 профилей.")
            connection.execute("UPDATE user_profiles SET is_active=0 WHERE is_active=1")
            profile_id = connection.execute(
                """INSERT INTO user_profiles(name,role_context,language,tone,detail_level,response_format,constraints,is_active)
                   VALUES (?,?,?,?,?,?,?,1)""",
                tuple(values[key] for key in ("name","role_context","language","tone","detail_level","response_format","constraints")),
            ).lastrowid
            connection.execute("INSERT INTO sessions(profile_id,label,is_active) VALUES (?, 'Диалог 1', 1)", (profile_id,))
            self._event(connection, "personalization", "create", "user", "profile", profile_id)
            self._event(connection, "personalization", "activate", "user", "profile", profile_id)
        return self.profile(profile_id)

    def update_profile(self, profile_id, values):
        with self.connection() as connection:
            if not connection.execute("SELECT 1 FROM user_profiles WHERE id=?", (profile_id,)).fetchone():
                return None
            connection.execute(
                """UPDATE user_profiles SET name=?,role_context=?,language=?,tone=?,detail_level=?,response_format=?,
                   constraints=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (*tuple(values[key] for key in ("name","role_context","language","tone","detail_level","response_format","constraints")), profile_id),
            )
            self._event(connection, "personalization", "update", "user", "profile", profile_id)
        return self.profile(profile_id)

    def activate_profile(self, profile_id):
        with self.connection() as connection:
            if not connection.execute("SELECT 1 FROM user_profiles WHERE id=?", (profile_id,)).fetchone():
                return None
            connection.execute("UPDATE user_profiles SET is_active=0 WHERE is_active=1")
            connection.execute("UPDATE user_profiles SET is_active=1 WHERE id=?", (profile_id,))
            if not connection.execute("SELECT 1 FROM sessions WHERE profile_id=? AND is_active=1", (profile_id,)).fetchone():
                latest = connection.execute("SELECT id FROM sessions WHERE profile_id=? ORDER BY id DESC LIMIT 1", (profile_id,)).fetchone()
                if latest:
                    connection.execute("UPDATE sessions SET is_active=1 WHERE id=?", (latest["id"],))
                else:
                    connection.execute("INSERT INTO sessions(profile_id,label,is_active) VALUES (?, 'Диалог 1', 1)", (profile_id,))
            self._event(connection, "personalization", "activate", "user", "profile", profile_id)
        return self.profile(profile_id)

    def long_term_entries(self):
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM long_term_entries ORDER BY id").fetchall()
        return [self._entry(row) for row in rows]

    def add_long_term(self, entry_type, content):
        with self.connection() as connection:
            if connection.execute("SELECT COUNT(*) FROM long_term_entries WHERE entry_type=?", (entry_type,)).fetchone()[0] >= 20:
                raise MemoryLimitError("Можно сохранить до 20 записей каждого типа.")
            entry_id = connection.execute("INSERT INTO long_term_entries(entry_type,content) VALUES (?,?)", (entry_type,content)).lastrowid
            self._event(connection,"long_term","save","user",entry_type,entry_id)
        return entry_id

    def delete_long_term(self, entry_id):
        with self.connection() as connection:
            row = connection.execute("SELECT entry_type FROM long_term_entries WHERE id=?", (entry_id,)).fetchone()
            if not row:
                return False
            connection.execute("DELETE FROM long_term_entries WHERE id=?", (entry_id,))
            self._event(connection,"long_term","delete","user",row["entry_type"],entry_id)
        return True

    def invariants(self, active_only=False):
        clause = "WHERE active=1" if active_only else ""
        with self.connection() as connection:
            rows = connection.execute(f"SELECT * FROM invariants {clause} ORDER BY id").fetchall()
        return [{**self._entry(row), "active": bool(row["active"])} for row in rows]

    def invariant(self, invariant_id):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM invariants WHERE id=?", (invariant_id,)).fetchone()
        return None if not row else {**self._entry(row), "active": bool(row["active"])}

    def create_invariant(self, values):
        with self.connection() as connection:
            if connection.execute("SELECT COUNT(*) FROM invariants").fetchone()[0] >= 20:
                raise MemoryLimitError("Можно сохранить до 20 инвариантов.")
            invariant_id = connection.execute(
                """INSERT INTO invariants(category,field,operator,expected_value,description,reason,compatible_alternative,active)
                   VALUES (?,?,?,?,?,?,?,?)""",
                tuple(values[key] for key in ("category","field","operator","expected_value","description","reason","compatible_alternative","active")),
            ).lastrowid
        return self.invariant(invariant_id)

    def update_invariant(self, invariant_id, values):
        with self.connection() as connection:
            if not connection.execute("SELECT 1 FROM invariants WHERE id=?", (invariant_id,)).fetchone():
                return None
            connection.execute(
                """UPDATE invariants SET category=?,field=?,operator=?,expected_value=?,description=?,reason=?,
                   compatible_alternative=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (*tuple(values[key] for key in ("category","field","operator","expected_value","description","reason","compatible_alternative","active")), invariant_id),
            )
        return self.invariant(invariant_id)

    def set_invariant_active(self, invariant_id, active):
        with self.connection() as connection:
            updated = connection.execute(
                "UPDATE invariants SET active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (int(active), invariant_id),
            ).rowcount
        return self.invariant(invariant_id) if updated else None

    def delete_invariant(self, invariant_id):
        with self.connection() as connection:
            return bool(connection.execute("DELETE FROM invariants WHERE id=?", (invariant_id,)).rowcount)

    def record_invariant_check(self, result):
        checked = result["checked_invariant_ids"]
        violated = [item["invariant_id"] for item in result["violations"]]
        with self.connection() as connection:
            return connection.execute(
                """INSERT INTO invariant_check_events(candidate_field,outcome,checked_invariant_ids_json,violated_invariant_ids_json)
                   VALUES (?,?,?,?)""",
                (
                    result["candidate"]["field"],
                    "allowed" if result["allowed"] else "rejected",
                    json.dumps(checked),
                    json.dumps(violated),
                ),
            ).lastrowid

    def invariant_check_events(self, limit=30):
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM invariant_check_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [
            {
                **self._entry(row),
                "checked_invariant_ids": self._decode(row["checked_invariant_ids_json"]),
                "violated_invariant_ids": self._decode(row["violated_invariant_ids_json"]),
            }
            for row in rows
        ]

    def task(self):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT goal,hard_constraints,task_data,open_questions,updated_at FROM tasks WHERE id=1"
            ).fetchone()
        return self._entry(row)

    def save_task(self, values):
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO tasks(id,goal,hard_constraints,task_data,open_questions) VALUES (1,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET goal=excluded.goal,hard_constraints=excluded.hard_constraints,
                   task_data=excluded.task_data,open_questions=excluded.open_questions,updated_at=CURRENT_TIMESTAMP""",
                tuple(values.get(key,"") for key in ("goal","hard_constraints","task_data","open_questions")),
            )
            self._event(connection,"working","replace","user","task",1)
        return self.task()

    def sessions(self, profile_id=None):
        profile_id = profile_id or self.active_profile()["id"]
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT s.*,COUNT(m.id) message_count FROM sessions s LEFT JOIN messages m ON m.session_id=s.id
                   WHERE s.profile_id=? GROUP BY s.id ORDER BY s.id DESC""", (profile_id,)
            ).fetchall()
        return [self._entry(row) for row in rows]

    def session(self, session_id):
        with self.connection() as connection:
            return self._entry(connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())

    def active_session(self, profile_id=None):
        profile_id = profile_id or self.active_profile()["id"]
        with self.connection() as connection:
            return self._entry(connection.execute("SELECT * FROM sessions WHERE profile_id=? AND is_active=1", (profile_id,)).fetchone())

    def _is_active_profile_session(self, connection, session_id):
        profile = connection.execute("SELECT id FROM user_profiles WHERE is_active=1").fetchone()
        return bool(profile and connection.execute("SELECT 1 FROM sessions WHERE id=? AND profile_id=?", (session_id,profile["id"])).fetchone())

    def create_session(self):
        profile_id = self.active_profile()["id"]
        with self.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM sessions WHERE profile_id=?", (profile_id,)).fetchone()[0]
            if count >= 20:
                raise MemoryLimitError("Можно создать до 20 сессий для профиля.")
            connection.execute("UPDATE sessions SET is_active=0 WHERE profile_id=?", (profile_id,))
            session_id = connection.execute("INSERT INTO sessions(profile_id,label,is_active) VALUES (?,?,1)",(profile_id,f"Диалог {count+1}")).lastrowid
            self._event(connection,"short_term","create","user","session",session_id,profile_id=profile_id)
        return self.session(session_id)

    def activate_session(self, session_id):
        with self.connection() as connection:
            if not self._is_active_profile_session(connection,session_id):
                return None
            profile_id = connection.execute("SELECT profile_id FROM sessions WHERE id=?",(session_id,)).fetchone()["profile_id"]
            connection.execute("UPDATE sessions SET is_active=0 WHERE profile_id=?",(profile_id,))
            connection.execute("UPDATE sessions SET is_active=1 WHERE id=?",(session_id,))
            self._event(connection,"short_term","activate","user","session",session_id,profile_id=profile_id)
        return self.session(session_id)

    def branch_session(self, source_id):
        with self.connection() as connection:
            if not self._is_active_profile_session(connection,source_id):
                return None
            source = connection.execute("SELECT * FROM sessions WHERE id=?",(source_id,)).fetchone()
            count = connection.execute("SELECT COUNT(*) FROM sessions WHERE profile_id=?",(source["profile_id"],)).fetchone()[0]
            if count >= 20:
                raise MemoryLimitError("Можно создать до 20 сессий для профиля.")
            connection.execute("UPDATE sessions SET is_active=0 WHERE profile_id=?",(source["profile_id"],))
            new_id = connection.execute("INSERT INTO sessions(profile_id,label,is_active,branched_from_id) VALUES (?,?,1,?)",(source["profile_id"],f"Ветка · {source['label']}",source_id)).lastrowid
            id_map = {}
            for row in connection.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id",(source_id,)).fetchall():
                new_mid = connection.execute("INSERT INTO messages(session_id,role,content,archived,request_preview_json) VALUES (?,?,?,?,?)",(new_id,row["role"],row["content"],row["archived"],row["request_preview_json"])).lastrowid
                id_map[row["id"]] = new_mid
            for row in connection.execute("SELECT * FROM session_summaries WHERE session_id=?",(source_id,)).fetchall():
                connection.execute("INSERT INTO session_summaries(session_id,through_message_id,content,request_preview_json,usage_json) VALUES (?,?,?,?,?)",(new_id,id_map[row["through_message_id"]],row["content"],row["request_preview_json"],row["usage_json"]))
            for row in connection.execute("SELECT * FROM session_facts_snapshots WHERE session_id=?",(source_id,)).fetchall():
                connection.execute("INSERT INTO session_facts_snapshots(session_id,through_user_message_id,values_json,request_preview_json,usage_json) VALUES (?,?,?,?,?)",(new_id,id_map[row["through_user_message_id"]],row["values_json"],row["request_preview_json"],row["usage_json"]))
            self._event(connection,"short_term","branch","user","session",new_id,profile_id=source["profile_id"],source_session_id=source_id)
        return self.session(new_id)

    def session_messages(self, session_id, include_archived=True):
        clause = "" if include_archived else "AND archived=0"
        with self.connection() as connection:
            rows = connection.execute(f"SELECT * FROM messages WHERE session_id=? {clause} ORDER BY id",(session_id,)).fetchall()
        return [{**self._entry(row),"archived":bool(row["archived"]),"preview":self._decode(row["request_preview_json"])} for row in rows]

    def active_session_messages(self, session_id, include_archived=False):
        return self.session_messages(session_id,include_archived)

    def append_turn(self, session_id, prompt, answer, preview):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._is_active_profile_session(connection,session_id):
                raise ValueError("Сессия не принадлежит активному профилю.")
            user_id = connection.execute("INSERT INTO messages(session_id,role,content) VALUES (?,'user',?)",(session_id,prompt)).lastrowid
            assistant_id = connection.execute("INSERT INTO messages(session_id,role,content,request_preview_json) VALUES (?,'assistant',?,?)",(session_id,answer,json.dumps(preview,ensure_ascii=False))).lastrowid
            self._event(connection,"short_term","save","user","message",user_id)
            self._event(connection,"short_term","save","agent","message",assistant_id)
        return {"user_id":user_id,"assistant_id":assistant_id}

    def reset_session(self, session_id):
        with self.connection() as connection:
            if not self._is_active_profile_session(connection,session_id):
                return False
            connection.execute("DELETE FROM session_summaries WHERE session_id=?",(session_id,))
            connection.execute("DELETE FROM session_facts_snapshots WHERE session_id=?",(session_id,))
            removed = connection.execute("DELETE FROM messages WHERE session_id=?",(session_id,)).rowcount
            self._event(connection,"short_term","reset","user","session",session_id,removed_messages=removed)
        return True

    def delete_session(self, session_id):
        with self.connection() as connection:
            if not self._is_active_profile_session(connection, session_id):
                return False
            session = connection.execute(
                "SELECT profile_id,is_active FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            profile_id = session["profile_id"]
            connection.execute(
                "UPDATE sessions SET branched_from_id=NULL WHERE branched_from_id=?", (session_id,)
            )
            connection.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            if session["is_active"]:
                latest = connection.execute(
                    "SELECT id FROM sessions WHERE profile_id=? ORDER BY id DESC LIMIT 1", (profile_id,)
                ).fetchone()
                if latest:
                    connection.execute("UPDATE sessions SET is_active=1 WHERE id=?", (latest["id"],))
                else:
                    connection.execute(
                        "INSERT INTO sessions(profile_id,label,is_active) VALUES (?, 'Диалог 1', 1)",
                        (profile_id,),
                    )
            self._event(connection, "short_term", "delete", "user", "session", session_id)
        return True

    def latest_summary(self, session_id):
        with self.connection() as connection:
            row=connection.execute("SELECT * FROM session_summaries WHERE session_id=? ORDER BY id DESC LIMIT 1",(session_id,)).fetchone()
        return None if not row else {**self._entry(row),"preview":self._decode(row["request_preview_json"]),"usage":self._decode(row["usage_json"])}

    def latest_facts(self, session_id):
        with self.connection() as connection:
            row=connection.execute("SELECT * FROM session_facts_snapshots WHERE session_id=? ORDER BY id DESC LIMIT 1",(session_id,)).fetchone()
        return None if not row else {**self._entry(row),"values":self._decode(row["values_json"]),"preview":self._decode(row["request_preview_json"]),"usage":self._decode(row["usage_json"])}

    def compressible_messages(self,session_id,batch_size,keep_recent):
        active=self.active_session_messages(session_id,False)
        return active[:batch_size] if len(active)>=batch_size+keep_recent else []

    def save_summary_and_archive(self,session_id,messages,content,preview,usage):
        if not messages:return None
        ids=[item["id"] for item in messages]
        with self.connection() as connection:
            sid=connection.execute("INSERT INTO session_summaries(session_id,through_message_id,content,request_preview_json,usage_json) VALUES (?,?,?,?,?)",(session_id,ids[-1],content,json.dumps(preview,ensure_ascii=False),json.dumps(usage))).lastrowid
            connection.execute(f"UPDATE messages SET archived=1 WHERE id IN ({','.join('?'*len(ids))})",ids)
        return sid

    def save_facts(self,session_id,user_message_id,values,preview,usage):
        with self.connection() as connection:
            return connection.execute("INSERT INTO session_facts_snapshots(session_id,through_user_message_id,values_json,request_preview_json,usage_json) VALUES (?,?,?,?,?)",(session_id,user_message_id,json.dumps(values,ensure_ascii=False),json.dumps(preview,ensure_ascii=False),json.dumps(usage))).lastrowid

    def session_compression(self,session_id):
        messages=self.session_messages(session_id,True)
        with self.connection() as connection:
            summaries=connection.execute("SELECT COUNT(*) FROM session_summaries WHERE session_id=?",(session_id,)).fetchone()[0]
            facts=connection.execute("SELECT COUNT(*) FROM session_facts_snapshots WHERE session_id=?",(session_id,)).fetchone()[0]
        return {"summary":self.latest_summary(session_id),"facts":self.latest_facts(session_id),"summaries_count":summaries,"facts_updates":facts,"archived_messages":sum(x["archived"] for x in messages),"active_messages":sum(not x["archived"] for x in messages)}

    def events(self,limit=30):
        with self.connection() as connection:
            rows=connection.execute("SELECT * FROM memory_events ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
        return [{**self._entry(row),"details":self._decode(row["details_json"])} for row in rows]

    def clear_all_memory(self):
        with self.connection() as connection:
            for table in ("session_facts_snapshots","session_summaries","messages","sessions","long_term_entries","invariant_check_events","invariants","tasks","memory_events","user_profiles"):
                connection.execute(f"DELETE FROM {table}")
            connection.execute("DROP TABLE IF EXISTS task_transition_events")
            connection.execute(
                "DELETE FROM sqlite_sequence WHERE name IN (?,?,?,?,?,?,?,?,?)", AUTOINCREMENT_TABLES
            )
            self._seed_defaults(connection)
            connection.execute("INSERT INTO tasks(id) VALUES (1)")

    def state(self):
        profile=self.active_profile()
        active=self.active_session(profile["id"])
        messages=self.session_messages(active["id"],True)
        compression=self.session_compression(active["id"])
        profiles=self.profiles()
        entries=self.long_term_entries()
        invariants=self.invariants()
        task=self.task()
        return {
            "profiles":profiles,"active_profile":profile,"long_term":entries,"invariants":invariants,
            "invariant_check_events":self.invariant_check_events(),"task":task,
            "sessions":self.sessions(profile["id"]),
            "active_session":{**active,"messages":messages,"compression":compression},
            "events":self.events(),
            "metrics":{
                "profiles":len(profiles),"profile_sessions":len(self.sessions(profile["id"])),
                "profile_messages":len(messages),"decisions":sum(x["entry_type"]=="decision" for x in entries),
                "knowledge":sum(x["entry_type"]=="knowledge" for x in entries),"working_task":bool(task and task["goal"]),
                "invariants":len(invariants),"active_invariants":sum(item["active"] for item in invariants),
                "summaries":compression["summaries_count"],"facts_updates":compression["facts_updates"],
            },
        }
