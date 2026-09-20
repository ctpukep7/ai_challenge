"""SQLite persistence with profile-scoped conversations and shared explicit memory."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .agent_actions import consent_decision, explicit_transition_target, transition_intent_decision
from .lifecycle_guards import unmet_requirements
from .task_machine import InvalidTransition, MachineState, RunStatus, TaskEvent, TaskPhase, allowed_events, transition


class MemoryLimitError(ValueError):
    pass


class TaskLockedError(ValueError):
    pass


class StaleTaskStateError(ValueError):
    pass


class TransitionRejectedError(ValueError):
    def __init__(self,message,reason_code,unmet=None):
        super().__init__(message)
        self.reason_code=reason_code
        self.unmet=unmet or []


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
BUILTIN_PROFILES_MIGRATION = "day15_builtin_profiles_v2"
PLANNING_READY_MIGRATION = "day15_planning_ready_v1"
AUTOINCREMENT_TABLES = (
    "user_profiles",
    "long_term_entries",
    "invariants",
    "invariant_check_events",
    "task_transition_events",
    "task_transition_attempts",
    "sessions",
    "messages",
    "session_summaries",
    "session_facts_snapshots",
    "memory_events",
    "agent_turns",
    "agent_actions",
    "pending_agent_approvals",
)


class SQLiteMemoryStore:
    PHASE_CHECKPOINTS = {
        "execution": {
            "current_step":"Выполнить утверждённый план подготовки.",
            "expected_action":"Сообщить, что выполнено и какие возникли препятствия.",
        },
        "validation": {
            "current_step":"Проверить фактический результат выполнения.",
            "expected_action":"Сообщить о найденных проблемах или подтвердить их отсутствие.",
        },
        "done": {
            "current_step":"Подготовка завершена.",
            "expected_action":"Можно подвести итог выполненной задачи.",
        },
    }

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
                    current_step TEXT NOT NULL DEFAULT '', expected_action TEXT NOT NULL DEFAULT '',
                    phase TEXT NOT NULL DEFAULT 'planning' CHECK (phase IN ('planning','execution','validation','done')),
                    run_status TEXT NOT NULL DEFAULT 'active' CHECK (run_status IN ('active','paused')),
                    revision INTEGER NOT NULL DEFAULT 0,
                    plan_artifact TEXT NOT NULL DEFAULT '',
                    planning_ready INTEGER NOT NULL DEFAULT 0 CHECK (planning_ready IN (0,1)),
                    plan_approved INTEGER NOT NULL DEFAULT 0 CHECK (plan_approved IN (0,1)),
                    execution_result TEXT NOT NULL DEFAULT '',
                    implementation_ready INTEGER NOT NULL DEFAULT 0 CHECK (implementation_ready IN (0,1)),
                    validation_passed INTEGER NOT NULL DEFAULT 0 CHECK (validation_passed IN (0,1)),
                    blocking_issues TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS task_transition_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    from_phase TEXT NOT NULL, from_run_status TEXT NOT NULL,
                    to_phase TEXT NOT NULL, to_run_status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS task_transition_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    requested_target TEXT NOT NULL,
                    from_phase TEXT NOT NULL,
                    from_run_status TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN ('accepted','rejected')),
                    reason_code TEXT NOT NULL,
                    unmet_requirements_json TEXT NOT NULL DEFAULT '[]',
                    expected_revision INTEGER NOT NULL,
                    actual_revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                CREATE TABLE IF NOT EXISTS agent_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK (status IN ('processing','completed','failed')),
                    user_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                    assistant_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                    action_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS agent_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    tool_call_id TEXT NOT NULL,
                    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN ('accepted','rejected')),
                    reason_code TEXT NOT NULL,
                    phase_before TEXT NOT NULL,
                    phase_after TEXT NOT NULL,
                    revision_before INTEGER NOT NULL,
                    revision_after INTEGER NOT NULL,
                    safe_details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS pending_agent_approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    action_id INTEGER NOT NULL REFERENCES agent_actions(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK (kind IN ('plan_approved','implementation_ready','validation_passed')),
                    expected_phase TEXT NOT NULL,
                    expected_revision INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','rejected','expired')),
                    display_summary TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TEXT
                );
                """
            )
            task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            migrations={
                "revision":"INTEGER NOT NULL DEFAULT 0",
                "plan_artifact":"TEXT NOT NULL DEFAULT ''",
                "planning_ready":"INTEGER NOT NULL DEFAULT 0",
                "plan_approved":"INTEGER NOT NULL DEFAULT 0",
                "execution_result":"TEXT NOT NULL DEFAULT ''",
                "implementation_ready":"INTEGER NOT NULL DEFAULT 0",
                "validation_passed":"INTEGER NOT NULL DEFAULT 0",
                "blocking_issues":"TEXT NOT NULL DEFAULT ''",
            }
            for column,declaration in migrations.items():
                if column not in task_columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} {declaration}")
            connection.execute("INSERT OR IGNORE INTO tasks(id) VALUES (1)")
            if not connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?",(PLANNING_READY_MIGRATION,)
            ).fetchone():
                connection.execute(
                    """UPDATE tasks SET planning_ready=1 WHERE trim(plan_artifact)<>''
                       AND (phase<>'planning' OR trim(open_questions)='')"""
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)",(PLANNING_READY_MIGRATION,)
                )
                connection.execute(
                    """UPDATE pending_agent_approvals SET status='expired',resolved_at=CURRENT_TIMESTAMP
                       WHERE status='pending' AND kind='plan_approved'
                       AND EXISTS (SELECT 1 FROM tasks WHERE id=1 AND planning_ready=0)"""
                )
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

    @staticmethod
    def _expire_all_pending(connection):
        connection.execute(
            "UPDATE pending_agent_approvals SET status='expired',resolved_at=CURRENT_TIMESTAMP WHERE status='pending'"
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
            row=self._entry(connection.execute("SELECT * FROM tasks WHERE id=1").fetchone())
        for key in ("planning_ready","plan_approved","implementation_ready","validation_passed"):
            row[key]=bool(row[key])
        return row

    def save_task(self, values):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT phase,run_status FROM tasks WHERE id=1").fetchone()
            if current["run_status"] == RunStatus.PAUSED.value or current["phase"] == TaskPhase.DONE.value:
                raise TaskLockedError("Checkpoint нельзя редактировать, пока задача на паузе или завершена.")
            connection.execute(
                """INSERT INTO tasks(id,goal,hard_constraints,task_data,open_questions,current_step,expected_action) VALUES (1,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET goal=excluded.goal,hard_constraints=excluded.hard_constraints,
                   task_data=excluded.task_data,open_questions=excluded.open_questions,current_step=excluded.current_step,
                   expected_action=excluded.expected_action,revision=tasks.revision+1,updated_at=CURRENT_TIMESTAMP""",
                tuple(values.get(key,"") for key in ("goal","hard_constraints","task_data","open_questions","current_step","expected_action")),
            )
            if current["phase"]==TaskPhase.PLANNING.value:
                connection.execute("UPDATE tasks SET planning_ready=0,plan_approved=0 WHERE id=1")
            self._expire_all_pending(connection)
            self._event(connection,"working","replace","user","task",1)
        return self.task()

    def save_evidence(self, values, expected_revision):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current=connection.execute("SELECT * FROM tasks WHERE id=1").fetchone()
            if current["revision"] != expected_revision:
                raise StaleTaskStateError("Evidence не сохранён: revision задачи уже изменился.")
            if current["run_status"] == RunStatus.PAUSED.value or current["phase"] == TaskPhase.DONE.value:
                raise TaskLockedError("Evidence нельзя редактировать, пока задача на паузе или завершена.")
            plan_changed=values["plan_artifact"] != current["plan_artifact"]
            execution_changed=values["execution_result"] != current["execution_result"]
            issues_changed=values["blocking_issues"] != current["blocking_issues"]
            if current["phase"] != TaskPhase.PLANNING.value and plan_changed:
                raise ValueError("Артефакт плана можно менять только на этапе planning.")
            if current["phase"] != TaskPhase.EXECUTION.value and execution_changed:
                raise ValueError("Результат выполнения можно менять только на этапе execution.")
            if current["phase"] != TaskPhase.VALIDATION.value and issues_changed:
                raise ValueError("Блокирующие замечания можно менять только на этапе validation.")
            next_execution="" if plan_changed else values["execution_result"]
            next_issues="" if plan_changed or execution_changed else values["blocking_issues"]
            connection.execute(
                """UPDATE tasks SET plan_artifact=?,execution_result=?,blocking_issues=?,
                   planning_ready=CASE WHEN ? THEN ? ELSE planning_ready END,
                   plan_approved=CASE WHEN ? THEN 0 ELSE plan_approved END,
                   implementation_ready=CASE WHEN ? OR ? THEN 0 ELSE implementation_ready END,
                   validation_passed=CASE WHEN ? OR ? OR ? THEN 0 ELSE validation_passed END,
                   revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=1""",
                (
                    values["plan_artifact"],next_execution,next_issues,
                    plan_changed,int(bool(values["plan_artifact"].strip() and not current["open_questions"].strip())),
                    plan_changed,plan_changed,execution_changed,
                    plan_changed,execution_changed,issues_changed,
                ),
            )
            self._expire_all_pending(connection)
            self._event(connection,"working","save_evidence","user","task",1)
        return self.task()

    def set_evidence_flag(self, kind, value, expected_revision):
        rules={
            "plan_approved":("planning","plan_artifact","Сначала сохраните непустой план."),
            "implementation_ready":("execution","execution_result","Сначала сохраните непустой результат выполнения."),
            "validation_passed":("validation",None,"Сначала устраните блокирующие замечания."),
        }
        phase,required_field,message=rules[kind]
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current=connection.execute("SELECT * FROM tasks WHERE id=1").fetchone()
            if current["revision"] != expected_revision:
                raise StaleTaskStateError("Решение не сохранено: revision задачи уже изменился.")
            if current["run_status"] != RunStatus.ACTIVE.value or current["phase"] != phase:
                raise ValueError("Это решение недоступно на текущем этапе или во время паузы.")
            if value and required_field and not current[required_field].strip():
                raise ValueError(message)
            if value and kind=="plan_approved" and not current["planning_ready"]:
                raise ValueError("Сначала подготовьте исполнимый план без открытых критичных вопросов.")
            if value and kind == "validation_passed" and current["blocking_issues"].strip():
                raise ValueError(message)
            connection.execute(
                f"UPDATE tasks SET {kind}=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=1",
                (int(value),),
            )
            self._expire_all_pending(connection)
            self._event(connection,"working",kind,"user","task",1,value=bool(value))
        return self.task()

    @staticmethod
    def _attempt(connection,event,target,row,outcome,reason_code,unmet,expected_revision):
        connection.execute(
            """INSERT INTO task_transition_attempts(event,requested_target,from_phase,from_run_status,outcome,
               reason_code,unmet_requirements_json,expected_revision,actual_revision) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                event,target,row["phase"],row["run_status"],outcome,reason_code,
                json.dumps(unmet,ensure_ascii=False),expected_revision,row["revision"],
            ),
        )

    def _transition_tx(self, connection, event, target, expected_revision, next_checkpoint=None):
        event_value=str(event) if str(event) in {item.value for item in TaskEvent} else "invalid"
        target_value=target if target in {item.value for item in TaskPhase} else "invalid"
        rejected=None
        row=connection.execute("SELECT * FROM tasks WHERE id=1").fetchone()
        if row["revision"] != expected_revision:
            rejected=TransitionRejectedError("Переход отклонён: revision устарел.","stale_revision")
            self._attempt(connection,event_value,target_value,row,"rejected",rejected.reason_code,[],expected_revision)
        else:
            before=MachineState(TaskPhase(row["phase"]),RunStatus(row["run_status"]))
            try:
                parsed_event=TaskEvent(event)
                after=transition(before,parsed_event)
            except (ValueError,InvalidTransition):
                rejected=TransitionRejectedError("Переход не является допустимым соседним событием.","invalid_transition")
                self._attempt(connection,event_value,target_value,row,"rejected",rejected.reason_code,[],expected_revision)
            if rejected is None and after.phase.value != target:
                rejected=TransitionRejectedError("Запрошенная цель не соответствует соседнему переходу автомата.","invalid_target")
                self._attempt(connection,event_value,target_value,row,"rejected",rejected.reason_code,[],expected_revision)
            if rejected is None:
                task_values=dict(row)
                unmet=unmet_requirements(task_values,target) if before.phase is not after.phase else []
                if unmet:
                    rejected=TransitionRejectedError("Переход заблокирован невыполненными prerequisites.","unmet_prerequisites",unmet)
                    self._attempt(connection,event_value,target_value,row,"rejected",rejected.reason_code,unmet,expected_revision)
            if rejected is None:
                phase_changed=before.phase is not after.phase
                if phase_changed and (not next_checkpoint or not all(next_checkpoint.get(key) for key in ("current_step","expected_action"))):
                    rejected=TransitionRejectedError("Для перехода укажите checkpoint следующего этапа.","checkpoint_missing")
                    self._attempt(connection,event_value,target_value,row,"rejected",rejected.reason_code,[],expected_revision)
            if rejected is None:
                connection.execute(
                    """UPDATE tasks SET phase=?,run_status=?,
                       current_step=CASE WHEN ? THEN ? ELSE current_step END,
                       expected_action=CASE WHEN ? THEN ? ELSE expected_action END,
                       execution_result=CASE WHEN ? THEN '' ELSE execution_result END,
                       implementation_ready=CASE WHEN ? THEN 0 ELSE implementation_ready END,
                       validation_passed=CASE WHEN ? THEN 0 ELSE validation_passed END,
                       revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=1""",
                    (
                        after.phase.value,after.run_status.value,phase_changed,
                        next_checkpoint["current_step"] if phase_changed else "",phase_changed,
                        next_checkpoint["expected_action"] if phase_changed else "",
                        False,False,False,
                    ),
                )
                self._expire_all_pending(connection)
                connection.execute(
                    """INSERT INTO task_transition_events(event,from_phase,from_run_status,to_phase,to_run_status)
                       VALUES (?,?,?,?,?)""",
                    (parsed_event.value,before.phase.value,before.run_status.value,after.phase.value,after.run_status.value),
                )
                self._attempt(connection,event_value,target_value,row,"accepted","accepted",[],expected_revision)
        return row,rejected

    def transition_task(self, event, target, expected_revision, next_checkpoint=None):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _,rejected=self._transition_tx(connection,event,target,expected_revision,next_checkpoint)
        if rejected is not None:
            raise rejected
        return self.task()

    def task_transition_events(self, limit=20):
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM task_transition_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._entry(row) for row in rows]

    def task_transition_attempts(self, limit=30):
        with self.connection() as connection:
            rows=connection.execute("SELECT * FROM task_transition_attempts ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
        return [{**self._entry(row),"unmet_requirements":self._decode(row["unmet_requirements_json"])} for row in rows]

    def task_transition_options(self):
        task=self.task()
        machine=MachineState(TaskPhase(task["phase"]),RunStatus(task["run_status"]))
        options=[]
        for event in allowed_events(machine):
            after=transition(machine,event)
            unmet=unmet_requirements(task,after.phase.value) if after.phase is not machine.phase else []
            options.append({"event":event.value,"target":after.phase.value,"enabled":not unmet,"unmet_requirements":unmet})
        return options

    def task_allowed_events(self):
        return [option["event"] for option in self.task_transition_options() if option["enabled"]]

    @staticmethod
    def _agent_action_row(connection, request_id, tool_call_id, session_id, action, outcome, reason_code,
                          before, after, safe_details=None):
        return connection.execute(
            """INSERT INTO agent_actions(request_id,tool_call_id,session_id,action,outcome,reason_code,
               phase_before,phase_after,revision_before,revision_after,safe_details_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                request_id,tool_call_id,session_id,action,outcome,reason_code,
                before["phase"],after["phase"],before["revision"],after["revision"],
                json.dumps(safe_details or {},ensure_ascii=False),
            ),
        ).lastrowid

    @staticmethod
    def _replace_pending_approval(connection, session_id, action_id, kind, phase, revision, summary):
        connection.execute(
            "UPDATE pending_agent_approvals SET status='expired',resolved_at=CURRENT_TIMESTAMP WHERE status='pending'"
        )
        pending_id=connection.execute(
            """INSERT INTO pending_agent_approvals(session_id,action_id,kind,expected_phase,expected_revision,display_summary)
               VALUES (?,?,?,?,?,?)""",
            (session_id,action_id,kind,phase,revision,summary),
        ).lastrowid
        return connection.execute("SELECT * FROM pending_agent_approvals WHERE id=?",(pending_id,)).fetchone()

    def claim_agent_turn(self, request_id, session_id):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing=connection.execute("SELECT * FROM agent_turns WHERE request_id=?",(request_id,)).fetchone()
            if existing:
                action_exists=connection.execute(
                    "SELECT 1 FROM agent_actions WHERE request_id=?",(request_id,)
                ).fetchone()
                if existing["status"]=="failed" and existing["session_id"]==session_id and not action_exists:
                    connection.execute(
                        """UPDATE agent_turns SET status='processing',user_message_id=NULL,assistant_message_id=NULL,
                           action_id=NULL,completed_at=NULL WHERE id=?""",(existing["id"],)
                    )
                    reclaimed=connection.execute("SELECT * FROM agent_turns WHERE id=?",(existing["id"],)).fetchone()
                    return {**self._entry(reclaimed),"created":True,"reclaimed":True}
                return {**self._entry(existing),"created":False}
            turn_id=connection.execute(
                "INSERT INTO agent_turns(request_id,session_id,status) VALUES (?,?,'processing')",
                (request_id,session_id),
            ).lastrowid
            return {**self._entry(connection.execute("SELECT * FROM agent_turns WHERE id=?",(turn_id,)).fetchone()),"created":True}

    def complete_agent_turn(self, request_id, user_message_id, assistant_message_id, action_id=None):
        with self.connection() as connection:
            connection.execute(
                """UPDATE agent_turns SET status='completed',user_message_id=?,assistant_message_id=?,action_id=?,
                   completed_at=CURRENT_TIMESTAMP WHERE request_id=?""",
                (user_message_id,assistant_message_id,action_id,request_id),
            )

    def fail_agent_turn(self, request_id, user_message_id=None, assistant_message_id=None, action_id=None):
        with self.connection() as connection:
            connection.execute(
                """UPDATE agent_turns SET status='failed',
                   user_message_id=COALESCE(?,user_message_id),assistant_message_id=COALESCE(?,assistant_message_id),
                   action_id=COALESCE(?,action_id),completed_at=CURRENT_TIMESTAMP WHERE request_id=?""",
                (user_message_id,assistant_message_id,action_id,request_id),
            )

    def claim_recoverable_agent_turn(self, request_id, session_id):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            turn=connection.execute("SELECT * FROM agent_turns WHERE request_id=?",(request_id,)).fetchone()
            if not turn:return None
            action=connection.execute("SELECT * FROM agent_actions WHERE request_id=?",(request_id,)).fetchone()
            if not action:return None
            if turn["session_id"] != session_id:
                return {"claimed":False,"reason":"session_changed","turn":self._entry(turn)}
            if turn["status"] != "failed":
                return {"claimed":False,"reason":turn["status"],"turn":self._entry(turn)}
            updated=connection.execute(
                """UPDATE agent_turns SET status='processing',completed_at=NULL
                   WHERE id=? AND status='failed'""",(turn["id"],)
            ).rowcount
            if updated != 1:
                return {"claimed":False,"reason":"processing","turn":self._entry(turn)}
            turn=connection.execute("SELECT * FROM agent_turns WHERE id=?",(turn["id"],)).fetchone()
            pending=connection.execute(
                "SELECT * FROM pending_agent_approvals WHERE action_id=? ORDER BY id DESC LIMIT 1",(action["id"],)
            ).fetchone()
            assistant=connection.execute(
                "SELECT * FROM messages WHERE id=?",(turn["assistant_message_id"],)
            ).fetchone() if turn["assistant_message_id"] else None
        return {
            "claimed":True,
            "turn":self._entry(turn),"action":self._public_agent_action(action),
            "pending_approval":self._public_pending(pending) if pending else None,
            "assistant":self._entry(assistant) if assistant else None,
        }

    def cached_agent_turn(self, request_id):
        with self.connection() as connection:
            turn=connection.execute("SELECT * FROM agent_turns WHERE request_id=?",(request_id,)).fetchone()
            if not turn or turn["status"] != "completed":
                return None
            assistant=connection.execute("SELECT * FROM messages WHERE id=?",(turn["assistant_message_id"],)).fetchone()
            action=connection.execute("SELECT * FROM agent_actions WHERE id=?",(turn["action_id"],)).fetchone() if turn["action_id"] else None
            pending=connection.execute(
                "SELECT * FROM pending_agent_approvals WHERE action_id=? ORDER BY id DESC LIMIT 1",
                (turn["action_id"],),
            ).fetchone() if turn["action_id"] else None
        if not assistant:
            return None
        preview=self._decode(assistant["request_preview_json"]) or {}
        return {
            "answer":assistant["content"],"preview":preview,"usage":preview.get("usage",{}),
            "session_id":turn["session_id"],"user_id":turn["user_message_id"],
            "assistant_id":turn["assistant_message_id"],"request_id":request_id,
            "action":self._public_agent_action(action) if action else None,
            "pending_approval":self._public_pending(pending) if pending else None,
            "idempotent_replay":True,
        }

    @classmethod
    def _public_agent_action(cls, row):
        if not row:return None
        item=cls._entry(row)
        item["safe_details"]=cls._decode(row["safe_details_json"]) or {}
        item.pop("safe_details_json",None)
        return item

    @classmethod
    def _public_pending(cls, row):
        if not row:return None
        item=cls._entry(row)
        item["value"]=True
        return item

    def agent_actions(self, limit=30):
        with self.connection() as connection:
            rows=connection.execute("SELECT * FROM agent_actions ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
        return [self._public_agent_action(row) for row in rows]

    def pending_agent_approvals(self, session_id=None, pending_only=False):
        session_id=session_id or self.active_session()["id"]
        clause="AND status='pending'" if pending_only else ""
        with self.connection() as connection:
            rows=connection.execute(
                f"SELECT * FROM pending_agent_approvals WHERE session_id=? {clause} ORDER BY id DESC",(session_id,)
            ).fetchall()
        return [self._public_pending(row) for row in rows]

    def reject_agent_action(self, request_id, tool_call_id, session_id, action, reason_code, message,
                            safe_details=None, unmet=None):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing=connection.execute("SELECT * FROM agent_actions WHERE request_id=?",(request_id,)).fetchone()
            if existing:return {**self._public_agent_action(existing),"message":message,"unmet_requirements":unmet or []}
            before=connection.execute("SELECT * FROM tasks WHERE id=1").fetchone()
            action_id=self._agent_action_row(
                connection,request_id,tool_call_id,session_id,action,"rejected",reason_code,before,before,safe_details
            )
            row=connection.execute("SELECT * FROM agent_actions WHERE id=?",(action_id,)).fetchone()
        return {**self._public_agent_action(row),"message":message,"unmet_requirements":unmet or []}

    def execute_agent_action(self, request_id, tool_call_id, session_id, action, arguments, expected_revision, user_prompt=""):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing=connection.execute("SELECT * FROM agent_actions WHERE request_id=?",(request_id,)).fetchone()
            if existing:
                pending=connection.execute(
                    "SELECT * FROM pending_agent_approvals WHERE action_id=? ORDER BY id DESC LIMIT 1",(existing["id"],)
                ).fetchone()
                return {**self._public_agent_action(existing),"message":"Повторный запрос не выполнил действие заново.",
                        "unmet_requirements":[],"pending_approval":self._public_pending(pending)}
            before=connection.execute("SELECT * FROM tasks WHERE id=1").fetchone()
            if before["revision"] != expected_revision:
                action_id=self._agent_action_row(connection,request_id,tool_call_id,session_id,action,"rejected","stale_revision",before,before)
                result={"message":"Действие отклонено: состояние задачи изменилось во время ответа модели.","unmet_requirements":[]}
            elif not self._is_active_profile_session(connection,session_id):
                action_id=self._agent_action_row(connection,request_id,tool_call_id,session_id,action,"rejected","session_changed",before,before)
                result={"message":"Активная сессия изменилась.","unmet_requirements":[]}
            elif before["phase"] == TaskPhase.DONE.value:
                action_id=self._agent_action_row(connection,request_id,tool_call_id,session_id,action,"rejected","task_done",before,before)
                result={"message":"Завершённую задачу можно только просматривать.","unmet_requirements":[]}
            elif before["run_status"] == RunStatus.PAUSED.value and action != "resume_task":
                action_id=self._agent_action_row(connection,request_id,tool_call_id,session_id,action,"rejected","task_paused",before,before)
                result={"message":"На паузе разрешено только продолжить задачу или объяснить состояние.","unmet_requirements":[]}
            elif action == "define_task":
                if before["phase"] != TaskPhase.PLANNING.value or before["run_status"] != RunStatus.ACTIVE.value:
                    action_id=self._agent_action_row(connection,request_id,tool_call_id,session_id,action,"rejected","wrong_phase",before,before)
                    result={"message":"Определить задачу можно только на активном этапе планирования.","unmet_requirements":[]}
                else:
                    planning_ready=arguments.get("planning_ready")
                    open_questions=arguments.get("open_questions","").strip()
                    invalid_ready_pair=(planning_ready and bool(open_questions)) or (
                        planning_ready is False and not open_questions
                    )
                    if not isinstance(planning_ready,bool) or invalid_ready_pair:
                        reason="planning_questions_open" if planning_ready else "planning_questions_missing"
                        action_id=self._agent_action_row(
                            connection,request_id,tool_call_id,session_id,action,"rejected",reason,
                            before,before,{"planning_ready":bool(planning_ready)},
                        )
                        result={"message":"planning_ready должен точно соответствовать наличию блокирующих вопросов.",
                                "unmet_requirements":[]}
                    else:
                        connection.execute(
                            """UPDATE tasks SET goal=?,hard_constraints=?,task_data=?,open_questions=?,current_step=?,expected_action=?,
                               plan_artifact=?,planning_ready=?,plan_approved=0,execution_result='',implementation_ready=0,
                               validation_passed=0,blocking_issues='',
                               revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=1""",
                            (*tuple(arguments.get(key,"") for key in (
                                "goal","hard_constraints","task_data","open_questions","current_step","expected_action"
                            )),arguments["proposed_plan"],int(planning_ready)),
                        )
                        self._expire_all_pending(connection)
                        self._event(connection,"working","replace","agent","task",1,planning_ready=planning_ready)
                        after=connection.execute("SELECT * FROM tasks WHERE id=1").fetchone()
                        action_id=self._agent_action_row(
                            connection,request_id,tool_call_id,session_id,action,"accepted","accepted",before,after,
                            {"evidence_kind":"plan_artifact","planning_ready":planning_ready},
                        )
                        pending=None
                        if planning_ready:
                            pending_id=connection.execute(
                                """INSERT INTO pending_agent_approvals(session_id,action_id,kind,expected_phase,expected_revision,display_summary)
                                   VALUES (?,?,?,?,?,?)""",
                                (session_id,action_id,"plan_approved","planning",after["revision"],"Подтвердить предложенный план подготовки"),
                            ).lastrowid
                            pending=connection.execute("SELECT * FROM pending_agent_approvals WHERE id=?",(pending_id,)).fetchone()
                        result={
                            "message":"Готовый план сохранён и ожидает подтверждения."
                                      if planning_ready else "Черновик сохранён; сначала ответьте на критичные вопросы.",
                            "unmet_requirements":[],"pending_approval":self._public_pending(pending),
                        }
            elif action == "save_current_evidence" and before["phase"]=="planning":
                action_id=self._agent_action_row(
                    connection,request_id,tool_call_id,session_id,action,"rejected","planning_requires_define_task",
                    before,before,{"evidence_kind":"plan_artifact"},
                )
                result={"message":"На этапе планирования создавайте или уточняйте план только через define_task.",
                        "unmet_requirements":[]}
            elif action == "save_current_evidence" and before["phase"]=="validation":
                action_id=self._agent_action_row(
                    connection,request_id,tool_call_id,session_id,action,"rejected","validation_requires_typed_result",
                    before,before,{"evidence_kind":"blocking_issues"},
                )
                result={"message":"На этапе проверки используйте типизированный результат с явным признаком наличия проблем.",
                        "unmet_requirements":[]}
            elif action == "save_current_evidence":
                phase=before["phase"]
                field_by_phase={"planning":"plan_artifact","execution":"execution_result"}
                kind_by_phase={"planning":"plan_approved","execution":"implementation_ready"}
                summary_by_phase={
                    "planning":"Подтвердить сохранённый план подготовки",
                    "execution":"Подтвердить готовность собранного чемодана",
                }
                field=field_by_phase[phase]; content=arguments["content"]
                plan_changed=field=="plan_artifact" and content!=before["plan_artifact"]
                execution_changed=field=="execution_result" and content!=before["execution_result"]
                issues_changed=field=="blocking_issues" and content!=before["blocking_issues"]
                next_plan=content if field=="plan_artifact" else before["plan_artifact"]
                next_execution=content if field=="execution_result" else before["execution_result"]
                next_issues=content if field=="blocking_issues" else before["blocking_issues"]
                if plan_changed:
                    next_execution="";next_issues=""
                elif execution_changed:
                    next_issues=""
                connection.execute(
                    """UPDATE tasks SET plan_artifact=?,execution_result=?,blocking_issues=?,
                       planning_ready=CASE WHEN ? THEN ? ELSE planning_ready END,
                       plan_approved=CASE WHEN ? THEN 0 ELSE plan_approved END,
                       implementation_ready=CASE WHEN ? OR ? THEN 0 ELSE implementation_ready END,
                       validation_passed=CASE WHEN ? OR ? OR ? THEN 0 ELSE validation_passed END,
                       revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=1""",
                    (next_plan,next_execution,next_issues,plan_changed,
                     int(bool(next_plan.strip() and not before["open_questions"].strip())),
                     plan_changed,plan_changed,execution_changed,
                     plan_changed,execution_changed,issues_changed),
                )
                self._expire_all_pending(connection)
                self._event(connection,"working","save_evidence","agent","task",1,evidence_kind=field)
                after=connection.execute("SELECT * FROM tasks WHERE id=1").fetchone()
                action_id=self._agent_action_row(
                    connection,request_id,tool_call_id,session_id,action,"accepted","accepted",before,after,
                    {"evidence_kind":field},
                )
                pending=None
                eligible=bool(content.strip()) and (phase!="planning" or bool(after["planning_ready"]))
                if eligible:
                    pending_id=connection.execute(
                        """INSERT INTO pending_agent_approvals(session_id,action_id,kind,expected_phase,expected_revision,display_summary)
                           VALUES (?,?,?,?,?,?)""",
                        (session_id,action_id,kind_by_phase[phase],phase,after["revision"],summary_by_phase[phase]),
                    ).lastrowid
                    pending=connection.execute("SELECT * FROM pending_agent_approvals WHERE id=?",(pending_id,)).fetchone()
                result={"message":"Evidence сохранён; approval остаётся за пользователем.","unmet_requirements":[],
                        "pending_approval":self._public_pending(pending)}
            elif action == "record_validation_result":
                has_blockers=arguments.get("has_blockers")
                raw_blocking_issues=arguments.get("blocking_issues")
                valid_types=isinstance(has_blockers,bool) and isinstance(raw_blocking_issues,str)
                blocking_issues=raw_blocking_issues.strip() if isinstance(raw_blocking_issues,str) else ""
                if before["phase"] != TaskPhase.VALIDATION.value:
                    action_id=self._agent_action_row(
                        connection,request_id,tool_call_id,session_id,action,"rejected","wrong_phase",before,before
                    )
                    result={"message":"Результат проверки можно сохранить только на активном этапе validation.",
                            "unmet_requirements":[]}
                elif not valid_types or has_blockers != bool(blocking_issues):
                    action_id=self._agent_action_row(
                        connection,request_id,tool_call_id,session_id,action,"rejected","invalid_validation_result",before,before,
                        {"has_blockers":bool(has_blockers)},
                    )
                    result={"message":"Признак наличия проблем не соответствует описанию результата проверки.",
                            "unmet_requirements":[]}
                else:
                    connection.execute(
                        "UPDATE pending_agent_approvals SET status='expired',resolved_at=CURRENT_TIMESTAMP WHERE status='pending'"
                    )
                    connection.execute(
                        """UPDATE tasks SET blocking_issues=?,validation_passed=0,
                           revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=1""",
                        (blocking_issues,),
                    )
                    self._event(connection,"working","save_validation_result","agent","task",1,
                                has_blockers=bool(has_blockers))
                    after=connection.execute("SELECT * FROM tasks WHERE id=1").fetchone()
                    action_id=self._agent_action_row(
                        connection,request_id,tool_call_id,session_id,action,"accepted","accepted",before,after,
                        {"evidence_kind":"blocking_issues","has_blockers":bool(has_blockers)},
                    )
                    pending=None
                    if not has_blockers:
                        pending=self._replace_pending_approval(
                            connection,session_id,action_id,"validation_passed","validation",after["revision"],
                            "Подтвердить успешную финальную проверку",
                        )
                    result={
                        "message":"Результат проверки сохранён; подтверждение остаётся за пользователем."
                                  if not has_blockers else "Блокирующие проблемы сохранены без подтверждения завершения.",
                        "unmet_requirements":[],"pending_approval":self._public_pending(pending),
                    }
            elif action == "accept_current_evidence_and_advance":
                pending_rows=connection.execute(
                    """SELECT * FROM pending_agent_approvals WHERE session_id=? AND status='pending'
                       AND expected_phase=? AND expected_revision=? ORDER BY id""",
                    (session_id,before["phase"],before["revision"]),
                ).fetchall()
                expected_kind={
                    "planning":"plan_approved",
                    "execution":"implementation_ready",
                    "validation":"validation_passed",
                }.get(before["phase"])
                consent_kind=pending_rows[0]["kind"] if len(pending_rows)==1 else expected_kind
                consent,consent_reason=consent_decision(user_prompt,consent_kind)
                if not consent:
                    action_id=self._agent_action_row(
                        connection,request_id,tool_call_id,session_id,action,"rejected",consent_reason,before,before
                    )
                    result={"message":"Не найдено однозначное согласие принять текущий результат и продолжить.","unmet_requirements":[]}
                elif len(pending_rows)>1:
                    action_id=self._agent_action_row(
                        connection,request_id,tool_call_id,session_id,action,"rejected","pending_approval_ambiguous",before,before
                    )
                    result={"message":"Нельзя однозначно определить актуальный результат для подтверждения.","unmet_requirements":[]}
                elif not pending_rows:
                    if not expected_kind or not before[expected_kind]:
                        action_id=self._agent_action_row(
                            connection,request_id,tool_call_id,session_id,action,"rejected","pending_approval_missing",before,before
                        )
                        result={"message":"Нет актуального подтверждения для текущего шага.","unmet_requirements":[]}
                    else:
                        event_by_phase={"planning":"advance","execution":"advance","validation":"approve"}
                        event=event_by_phase[before["phase"]]
                        machine=MachineState(TaskPhase(before["phase"]),RunStatus(before["run_status"]))
                        target=transition(machine,TaskEvent(event)).phase.value
                        checkpoint=self.PHASE_CHECKPOINTS[target]
                        _,rejected=self._transition_tx(connection,event,target,before["revision"],checkpoint)
                        after=connection.execute("SELECT * FROM tasks WHERE id=1").fetchone()
                        outcome="rejected" if rejected else "accepted"
                        reason=rejected.reason_code if rejected else "accepted"
                        action_id=self._agent_action_row(
                            connection,request_id,tool_call_id,session_id,action,outcome,reason,before,after,
                            {"event":event,"approval_kind":expected_kind,"consent":"explicit","approval_source":"existing"},
                        )
                        result={
                            "message":str(rejected) if rejected else "Ранее подтверждённый результат использован; переход выполнен.",
                            "unmet_requirements":rejected.unmet if rejected else [],"pending_approval":None,
                        }
                else:
                    pending=pending_rows[0]
                    event_by_phase={"planning":"advance","execution":"advance","validation":"approve"}
                    event=event_by_phase[before["phase"]]
                    machine=MachineState(TaskPhase(before["phase"]),RunStatus(before["run_status"]))
                    after_machine=transition(machine,TaskEvent(event));target=after_machine.phase.value
                    projected=dict(before);projected[pending["kind"]]=1
                    unmet=unmet_requirements(projected,target)
                    if unmet:
                        self._attempt(connection,event,target,before,"rejected","unmet_prerequisites",unmet,before["revision"])
                        action_id=self._agent_action_row(
                            connection,request_id,tool_call_id,session_id,action,"rejected","unmet_prerequisites",before,before,
                            {"event":event,"approval_kind":pending["kind"]},
                        )
                        result={"message":"Переход заблокирован невыполненными prerequisites.","unmet_requirements":unmet}
                    else:
                        connection.execute(
                            f"UPDATE tasks SET {pending['kind']}=1,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=1"
                        )
                        approved=connection.execute("SELECT * FROM tasks WHERE id=1").fetchone()
                        checkpoint=self.PHASE_CHECKPOINTS[target]
                        _,rejected=self._transition_tx(connection,event,target,approved["revision"],checkpoint)
                        if rejected:raise rejected
                        connection.execute(
                            "UPDATE pending_agent_approvals SET status='confirmed',resolved_at=CURRENT_TIMESTAMP WHERE id=?",
                            (pending["id"],),
                        )
                        self._event(connection,"working",pending["kind"],"user","task",1,value=True,source="chat")
                        after=connection.execute("SELECT * FROM tasks WHERE id=1").fetchone()
                        action_id=self._agent_action_row(
                            connection,request_id,tool_call_id,session_id,action,"accepted","accepted",before,after,
                            {"event":event,"approval_kind":pending["kind"],"consent":"explicit"},
                        )
                        result={"message":"Явное согласие принято; подтверждение и переход выполнены атомарно.",
                                "unmet_requirements":[],"pending_approval":self._public_pending(
                                    connection.execute("SELECT * FROM pending_agent_approvals WHERE id=?",(pending["id"],)).fetchone()
                                )}
            elif action in ("pause_task","resume_task","request_transition"):
                if action=="pause_task": event="pause"; checkpoint=None
                elif action=="resume_task": event="resume"; checkpoint=None
                else:
                    event=arguments["event"]
                    checkpoint=None
                explicit_target=explicit_transition_target(user_prompt) if action=="request_transition" else None
                # A specifically named target is itself an unambiguous request.
                # It must be attempted even when it is not the next neighbour,
                # otherwise the audit would hide the user's attempted jump.
                intent=(True,"transition_intent_explicit_target") if explicit_target else (
                    (True,"transition_intent_not_required") if action!="request_transition" else transition_intent_decision(
                        user_prompt,before["phase"],event
                    )
                )
                if not intent[0]:
                    action_id=self._agent_action_row(
                        connection,request_id,tool_call_id,session_id,action,"rejected",intent[1],before,before,{"event":event}
                    )
                    result={"message":"Текущая реплика не содержит однозначной просьбы выполнить этот переход.",
                            "unmet_requirements":[]}
                else:
                    machine=MachineState(TaskPhase(before["phase"]),RunStatus(before["run_status"]))
                    try:derived_target=transition(machine,TaskEvent(event)).phase.value
                    except (ValueError,InvalidTransition):derived_target="invalid"
                    target=explicit_target or derived_target
                    if target in self.PHASE_CHECKPOINTS:
                        checkpoint=self.PHASE_CHECKPOINTS[target]
                    _,rejected=self._transition_tx(connection,event,target,before["revision"],checkpoint)
                    after=connection.execute("SELECT * FROM tasks WHERE id=1").fetchone()
                    outcome="rejected" if rejected else "accepted"
                    reason=rejected.reason_code if rejected else "accepted"
                    action_id=self._agent_action_row(
                        connection,request_id,tool_call_id,session_id,action,outcome,reason,before,after,
                        {"event":event,"requested_target":target,"derived_target":derived_target}
                    )
                    result={"message":str(rejected) if rejected else "Переход выполнен.",
                            "unmet_requirements":rejected.unmet if rejected else []}
            else:
                action_id=self._agent_action_row(connection,request_id,tool_call_id,session_id,action,"rejected","unknown_action",before,before)
                result={"message":"Модель запросила неизвестное действие.","unmet_requirements":[]}
            row=connection.execute("SELECT * FROM agent_actions WHERE id=?",(action_id,)).fetchone()
        return {**self._public_agent_action(row),**result}

    def resolve_pending_approval(self, approval_id, session_id, confirm):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pending=connection.execute("SELECT * FROM pending_agent_approvals WHERE id=?",(approval_id,)).fetchone()
            if not pending or pending["session_id"] != session_id:
                return None,"not_found"
            if pending["status"] != "pending":
                return self._public_pending(pending),"already_resolved"
            if not confirm:
                connection.execute(
                    "UPDATE pending_agent_approvals SET status='rejected',resolved_at=CURRENT_TIMESTAMP WHERE id=?",(approval_id,)
                )
                row=connection.execute("SELECT * FROM pending_agent_approvals WHERE id=?",(approval_id,)).fetchone()
                return self._public_pending(row),"rejected"
            current=connection.execute("SELECT * FROM tasks WHERE id=1").fetchone()
            if current["revision"] != pending["expected_revision"] or current["phase"] != pending["expected_phase"]:
                connection.execute(
                    "UPDATE pending_agent_approvals SET status='expired',resolved_at=CURRENT_TIMESTAMP WHERE id=?",(approval_id,)
                )
                row=connection.execute("SELECT * FROM pending_agent_approvals WHERE id=?",(approval_id,)).fetchone()
                return self._public_pending(row),"stale_revision"
            kind=pending["kind"]
            if kind=="plan_approved" and (
                not current["planning_ready"] or current["open_questions"].strip()
            ):
                connection.execute(
                    "UPDATE pending_agent_approvals SET status='expired',resolved_at=CURRENT_TIMESTAMP WHERE id=?",(approval_id,)
                )
                row=connection.execute("SELECT * FROM pending_agent_approvals WHERE id=?",(approval_id,)).fetchone()
                return self._public_pending(row),"planning_not_ready"
            required={"plan_approved":"plan_artifact","implementation_ready":"execution_result","validation_passed":None}[kind]
            if required and not current[required].strip():return self._public_pending(pending),"evidence_missing"
            if kind=="validation_passed" and current["blocking_issues"].strip():return self._public_pending(pending),"blocking_issues_present"
            connection.execute(
                f"UPDATE tasks SET {kind}=1,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=1"
            )
            self._expire_all_pending(connection)
            connection.execute(
                "UPDATE pending_agent_approvals SET status='confirmed',resolved_at=CURRENT_TIMESTAMP WHERE id=?",(approval_id,)
            )
            self._event(connection,"working",kind,"user","task",1,value=True,source="agent_confirmation")
            row=connection.execute("SELECT * FROM pending_agent_approvals WHERE id=?",(approval_id,)).fetchone()
            return self._public_pending(row),"confirmed"

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

    def append_turn(self, session_id, prompt, answer, preview, expected_task_state=None):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._is_active_profile_session(connection,session_id):
                raise ValueError("Сессия не принадлежит активному профилю.")
            if expected_task_state is not None:
                current = connection.execute("SELECT phase,run_status,revision FROM tasks WHERE id=1").fetchone()
                actual = {key: current[key] for key in ("phase", "run_status", "revision")}
                expected = {key: expected_task_state[key] for key in ("phase", "run_status", "revision")}
                if actual != expected:
                    raise StaleTaskStateError(
                        "Состояние задачи изменилось во время ответа модели. Ответ отброшен; повторите запрос из актуального checkpoint."
                    )
            user_id = connection.execute("INSERT INTO messages(session_id,role,content) VALUES (?,'user',?)",(session_id,prompt)).lastrowid
            assistant_id = connection.execute("INSERT INTO messages(session_id,role,content,request_preview_json) VALUES (?,'assistant',?,?)",(session_id,answer,json.dumps(preview,ensure_ascii=False))).lastrowid
            self._event(connection,"short_term","save","user","message",user_id)
            self._event(connection,"short_term","save","agent","message",assistant_id)
        return {"user_id":user_id,"assistant_id":assistant_id}

    def reset_session(self, session_id):
        with self.connection() as connection:
            if not self._is_active_profile_session(connection,session_id):
                return False
            connection.execute("DELETE FROM agent_turns WHERE session_id=?",(session_id,))
            connection.execute("DELETE FROM pending_agent_approvals WHERE session_id=?",(session_id,))
            connection.execute("DELETE FROM agent_actions WHERE session_id=?",(session_id,))
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
            for table in ("agent_turns","pending_agent_approvals","agent_actions","session_facts_snapshots","session_summaries","messages","sessions","long_term_entries","invariant_check_events","invariants","task_transition_attempts","task_transition_events","tasks","memory_events","user_profiles"):
                connection.execute(f"DELETE FROM {table}")
            placeholders = ",".join("?" for _ in AUTOINCREMENT_TABLES)
            connection.execute(
                f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", AUTOINCREMENT_TABLES
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
            "allowed_events":self.task_allowed_events(),"transition_options":self.task_transition_options(),
            "task_transition_events":self.task_transition_events(),"task_transition_attempts":self.task_transition_attempts(),
            "agent_actions":self.agent_actions(),"pending_agent_approvals":self.pending_agent_approvals(active["id"]),
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
