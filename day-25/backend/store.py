"""Transactional dialogue storage. HTTP/model calls never occur in write transactions."""
from __future__ import annotations
import json
from contextlib import contextmanager
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

EMPTY_TASK = {"goal": "", "clarifications": [], "constraints": [], "terms": [], "source_days": []}

def now():
    return datetime.now(timezone.utc).isoformat()

class Conflict(ValueError):
    pass

class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript('''
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,title TEXT NOT NULL,task TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,request_id TEXT NOT NULL,question TEXT NOT NULL,status TEXT NOT NULL,result TEXT,created TEXT NOT NULL, UNIQUE(session_id,request_id));
            ''')
            if "scope_cutoff" not in {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}:
                conn.execute("ALTER TABLE sessions ADD COLUMN scope_cutoff INTEGER NOT NULL DEFAULT 0")
            if "request_payload" not in {row[1] for row in conn.execute("PRAGMA table_info(messages)")}:
                conn.execute("ALTER TABLE messages ADD COLUMN request_payload TEXT")

    def recover_pending(self):
        """Called only by the exclusive application lifespan, never on import/read."""
        with self.connect() as conn:
            conn.execute("UPDATE messages SET status='interrupted',result=? WHERE status='pending'", (json.dumps({"status":"error","answer":"Запрос прерван перезапуском. Отправьте его заново с новым идентификатором.","sources":[],"reason":"interrupted"}, ensure_ascii=False),))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create(self, title):
        identifier = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute("INSERT INTO sessions(id,title,task,revision,created) VALUES(?,?,?,?,?)", (identifier,title,json.dumps(EMPTY_TASK),0,now()))
        return self.get(identifier)

    def list(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT id,title,revision,created FROM sessions ORDER BY created DESC")]

    def get(self, identifier):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (identifier,)).fetchone()
            if row is None: raise KeyError(identifier)
            messages = []
            for item in conn.execute("SELECT * FROM messages WHERE session_id=? ORDER BY rowid", (identifier,)):
                message = dict(item)
                message.pop("request_payload",None)
                message["result"] = json.loads(message["result"]) if message["result"] else None
                messages.append(message)
            session = dict(row)
            task = json.loads(session.pop("task"))
            return {"session":session,"messages":messages,"task_state":task,"revision":row["revision"]}

    def delete(self, identifier):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM messages WHERE session_id=? AND status='pending'", (identifier,)).fetchone():
                raise Conflict("Дождитесь ответа перед удалением сессии")
            if conn.execute("DELETE FROM sessions WHERE id=?", (identifier,)).rowcount == 0: raise KeyError(identifier)

    def update_task(self, identifier, task, revision):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT revision,task,scope_cutoff FROM sessions WHERE id=?", (identifier,)).fetchone()
            if row is None: raise KeyError(identifier)
            if row[0] != revision: raise Conflict("Память изменилась: обновите сессию")
            if conn.execute("SELECT 1 FROM messages WHERE session_id=? AND status='pending'", (identifier,)).fetchone():
                raise Conflict("Дождитесь ответа перед изменением памяти")
            cutoff=row["scope_cutoff"]
            if json.loads(row["task"])["source_days"] != task["source_days"]:
                cutoff=conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (identifier,)).fetchone()[0]
            conn.execute("UPDATE sessions SET task=?,revision=revision+1,scope_cutoff=? WHERE id=?", (json.dumps(task,ensure_ascii=False),cutoff,identifier))
        return self.get(identifier)

    def reserve(self, identifier, question, request_id, settings=None):
        payload=json.dumps(settings or {},sort_keys=True,separators=(",",":"))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = conn.execute("SELECT * FROM sessions WHERE id=?", (identifier,)).fetchone()
            if session is None: raise KeyError(identifier)
            old = conn.execute("SELECT * FROM messages WHERE session_id=? AND request_id=?", (identifier,request_id)).fetchone()
            if old:
                if old["question"] != question or (old["request_payload"] is not None and old["request_payload"] != payload): raise Conflict("request_id уже использован для другого вопроса")
                if old["status"] == "pending": raise Conflict("Этот запрос ещё выполняется")
                return dict(old),session["revision"],True
            if conn.execute("SELECT 1 FROM messages WHERE session_id=? AND status='pending'", (identifier,)).fetchone():
                raise Conflict("В этой сессии уже выполняется запрос")
            message_id = uuid.uuid4().hex
            conn.execute("INSERT INTO messages(id,session_id,request_id,question,status,result,created,request_payload) VALUES(?,?,?,?,?,?,?,?)", (message_id,identifier,request_id,question,"pending",None,now(),payload))
            return {"id":message_id},session["revision"],False

    def finish(self, identifier, message_id, revision, result, task=None):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT revision,task,scope_cutoff FROM sessions WHERE id=?", (identifier,)).fetchone()
            pending = conn.execute("SELECT status FROM messages WHERE id=? AND session_id=?", (message_id,identifier)).fetchone()
            if row is None or row[0] != revision or pending is None or pending[0] != "pending":
                raise Conflict("Ответ устарел и не может изменить сессию")
            conn.execute("UPDATE messages SET status=?,result=? WHERE id=?", (result["status"],json.dumps(result,ensure_ascii=False),message_id))
            if task is None:
                conn.execute("UPDATE sessions SET revision=revision+1 WHERE id=?", (identifier,))
            else:
                cutoff=row["scope_cutoff"]
                if json.loads(row["task"])["source_days"] != task["source_days"]:
                    cutoff=conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (identifier,)).fetchone()[0]
                conn.execute("UPDATE sessions SET revision=revision+1,task=?,scope_cutoff=? WHERE id=?", (json.dumps(task,ensure_ascii=False),cutoff,identifier))
        return self.get(identifier)
