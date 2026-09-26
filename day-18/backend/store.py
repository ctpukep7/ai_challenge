"""Small SQLite transactions; network/model calls never hold a database lock."""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
from .scheduling import iso, now_utc, parse, next_run, validate_schedule


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS cities(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE, label TEXT, latitude REAL,
                longitude REAL,country TEXT,instruction TEXT,timezone TEXT,
                schedule_type TEXT,interval_minutes INTEGER,cron_expression TEXT,
                status TEXT,next_run_at TEXT,created_at TEXT,skill_body TEXT,skill_hash TEXT,
                last_run_at TEXT,last_error TEXT);
              CREATE TABLE IF NOT EXISTS occurrences(
                id TEXT PRIMARY KEY,job_id TEXT,scheduled_for TEXT,status TEXT,
                token TEXT,lease_until TEXT,started_at TEXT,finished_at TEXT,
                model TEXT,error TEXT,trace TEXT DEFAULT '[]',
                UNIQUE(job_id,scheduled_for));
              CREATE TABLE IF NOT EXISTS weather_results(
                id TEXT PRIMARY KEY,run_id TEXT,token TEXT,payload TEXT,
                persisted INTEGER DEFAULT 0,UNIQUE(run_id,token));
              CREATE UNIQUE INDEX IF NOT EXISTS one_sample_per_run
                ON weather_results(run_id) WHERE persisted=1;
              CREATE TABLE IF NOT EXISTS chat_requests(
                id TEXT PRIMARY KEY,prompt TEXT,status TEXT,answer TEXT,trace TEXT,
                created_at TEXT,processing_token TEXT);
              CREATE TABLE IF NOT EXISTS chat_actions(
                request_id TEXT,action_key TEXT,tool TEXT,payload TEXT,
                PRIMARY KEY(request_id,action_key));
              CREATE TABLE IF NOT EXISTS messages(
                id TEXT PRIMARY KEY,role TEXT,content TEXT,trace TEXT,created_at TEXT);
              CREATE TABLE IF NOT EXISTS json_requests(
                scope TEXT,entity_id TEXT,payload TEXT NOT NULL,
                PRIMARY KEY(scope,entity_id));
              CREATE TABLE IF NOT EXISTS dispatcher(
                id INTEGER PRIMARY KEY CHECK(id=1),last_tick TEXT,last_execution TEXT);
              INSERT OR IGNORE INTO dispatcher(id) VALUES(1);
            """)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def register_cities(self, candidates):
        with self.connection(True) as db:
            result = []
            for city in candidates:
                candidate = dict(city)
                candidate["city_id"] = str(uuid.uuid4())
                db.execute("INSERT INTO cities VALUES(?,?)", (candidate["city_id"], json.dumps(candidate)))
                result.append(candidate)
            return result

    def create_job(self, city_id, instruction, schedule_type, interval_minutes,
                   cron_expression, timezone, skill, request_id, now=None, chat_token=None):
        now = now or now_utc()
        validate_schedule(schedule_type, interval_minutes, cron_expression, timezone)
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2000:
            raise ValueError("Поручение должно содержать от 1 до 2000 символов")
        with self.connection(True) as db:
            self.assert_chat_owner(db,request_id,chat_token,now)
            existing = db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                return dict(existing)
            if db.execute("SELECT count(*) FROM jobs WHERE status!='cancelled'").fetchone()[0] >= 3:
                raise ValueError("Можно сохранить не более трёх активных или приостановленных заданий")
            city = db.execute("SELECT payload FROM cities WHERE id=?", (city_id,)).fetchone()
            if not city:
                raise ValueError("Сначала найдите город через resolve_city")
            city = json.loads(city[0])
            original = db.execute("SELECT prompt FROM chat_requests WHERE id=?",(request_id,)).fetchone()
            if original:
                instruction = original["prompt"]
            job = dict(id=str(uuid.uuid4()), request_id=request_id, label=city["label"],
                       latitude=city["latitude"], longitude=city["longitude"], country=city["country"],
                       instruction=instruction.strip(), timezone=timezone, schedule_type=schedule_type,
                       interval_minutes=interval_minutes, cron_expression=cron_expression,
                       status="active", created_at=iso(now), skill_body=skill,
                       skill_hash=hashlib.sha256(skill.encode()).hexdigest(),last_run_at=None,last_error=None)
            job["next_run_at"] = iso(next_run(job, now))
            db.execute(f"INSERT INTO jobs ({','.join(job)}) VALUES({','.join('?' for _ in job)})", tuple(job.values()))
            self.record_action(db,request_id,"create", "create_weather_schedule",job)
            return job

    def list_jobs(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY created_at DESC")]

    def set_status(self, job_id, status, now=None, request_id=None, chat_token=None, action_tool="set_schedule_status"):
        if status not in ("active", "paused", "cancelled"):
            raise ValueError("Статус: active, paused или cancelled")
        now = now or now_utc()
        with self.connection(True) as db:
            self.assert_chat_owner(db,request_id,chat_token,now)
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("Задание не найдено")
            job = dict(row)
            if job["status"] == "cancelled" and status != "cancelled":
                raise ValueError("Отменённое задание нельзя возобновить; создайте новое")
            if job["status"] == status:
                self.record_action(db,request_id,"status:"+job_id,action_tool,job)
                return job
            future = iso(next_run(job, now)) if status == "active" else None
            db.execute("UPDATE jobs SET status=?,next_run_at=? WHERE id=?", (status, future, job_id))
            db.execute("UPDATE occurrences SET status='abandoned',token=NULL,finished_at=?,error=? WHERE job_id=? AND status='running'",
                       (iso(now), "Задание приостановлено или отменено", job_id))
            job.update(status=status, next_run_at=future)
            self.record_action(db,request_id,"status:"+job_id,action_tool,job)
            return job

    def delete_job(self,job_id,now=None,request_id=None,chat_token=None):
        """Archive a schedule, revoke its lease, and retain all collected history."""
        return self.set_status(job_id,"cancelled",now=now,request_id=request_id,chat_token=chat_token,
                               action_tool="delete_weather_schedule")

    def tick(self, now=None):
        with self.connection(True) as db:
            db.execute("UPDATE dispatcher SET last_tick=? WHERE id=1", (iso(now or now_utc()),))

    def claim_due(self, model, now=None):
        """Claim one occurrence. An expired lease rotates its fencing token."""
        now = now or now_utc()
        with self.connection(True) as db:
            jobs = db.execute("SELECT * FROM jobs WHERE status='active' AND next_run_at<=? ORDER BY next_run_at", (iso(now),)).fetchall()
            for row in jobs:
                job = dict(row)
                run = db.execute("SELECT * FROM occurrences WHERE job_id=? AND scheduled_for=?", (job["id"], job["next_run_at"])).fetchone()
                if run and run["status"] == "running" and parse(run["lease_until"]) > now:
                    continue
                if run and run["status"] != "running":
                    # Advance a recovered completed row without creating historical observations.
                    db.execute("UPDATE jobs SET next_run_at=? WHERE id=?", (iso(next_run(job, now, parse(job["next_run_at"]))), job["id"]))
                    continue
                run_id = run["id"] if run else str(uuid.uuid4())
                token = str(uuid.uuid4())
                lease = iso(now + timedelta(seconds=180))
                if run:
                    saved = db.execute("SELECT 1 FROM weather_results WHERE run_id=? AND persisted=1", (run_id,)).fetchone()
                    if saved:
                        db.execute("UPDATE occurrences SET status='success',finished_at=?,token=NULL WHERE id=?", (iso(now),run_id))
                        db.execute("UPDATE jobs SET next_run_at=?,last_run_at=?,last_error=NULL WHERE id=?", (iso(next_run(job, now, parse(job["next_run_at"]))),iso(now),job["id"]))
                        db.execute("UPDATE dispatcher SET last_execution=? WHERE id=1", (iso(now),))
                        continue
                    db.execute("UPDATE occurrences SET token=?,lease_until=?,started_at=?,model=?,error=NULL,trace='[]' WHERE id=?",(token,lease,iso(now),model,run_id))
                else:
                    db.execute("INSERT INTO occurrences(id,job_id,scheduled_for,status,token,lease_until,started_at,model) VALUES(?,?,?,'running',?,?,?,?)", (run_id,job["id"],job["next_run_at"],token,lease,iso(now),model))
                return {"run_id":run_id,"job_id":job["id"],"token":token,"job":job}
        return None

    def assert_scope(self, db, scope, now):
        row = db.execute("SELECT o.*,j.status AS job_status FROM occurrences o JOIN jobs j ON j.id=o.job_id WHERE o.id=? AND o.job_id=?",(scope["run_id"],scope["job_id"])).fetchone()
        if not row or row["token"] != scope["token"] or row["status"] != "running" or row["job_status"] != "active" or parse(row["lease_until"]) <= now:
            raise ValueError("Запуск больше не владеет заданием: пауза, отмена или истёкшая аренда")

    def observation(self, scope, payload, now=None):
        with self.connection(True) as db:
            self.assert_scope(db, scope, now or now_utc())
            existing = db.execute("SELECT id,payload FROM weather_results WHERE run_id=? AND token=?", (scope["run_id"],scope["token"])).fetchone()
            if existing:
                return {"result_id":existing["id"],**json.loads(existing["payload"])}
            result_id = str(uuid.uuid4())
            db.execute("INSERT INTO weather_results(id,run_id,token,payload) VALUES(?,?,?,?)", (result_id,scope["run_id"],scope["token"],json.dumps(payload)))
            return {"result_id":result_id,**payload}

    def save_result(self, scope, result_id, now=None):
        with self.connection(True) as db:
            self.assert_scope(db, scope, now or now_utc())
            row = db.execute("SELECT * FROM weather_results WHERE id=? AND run_id=? AND token=?", (result_id,scope["run_id"],scope["token"])).fetchone()
            if not row:
                raise ValueError("result_id должен принадлежать текущему реальному наблюдению")
            db.execute("UPDATE weather_results SET persisted=1 WHERE id=?", (result_id,))
            return {"saved":True,"result_id":result_id}

    def finish(self, scope, error=None, trace=None, now=None, json_requests=None):
        now = now or now_utc()
        with self.connection(True) as db:
            try:
                self.assert_scope(db, scope, now)
            except ValueError:
                return False
            saved = db.execute("SELECT 1 FROM weather_results WHERE run_id=? AND persisted=1",(scope["run_id"],)).fetchone()
            if not saved and not error:
                error = "Агент завершился без сохранённого наблюдения"
            status = "success" if saved else "error"
            safe_error = None if saved else error
            db.execute("UPDATE occurrences SET status=?,error=?,trace=?,finished_at=?,token=NULL WHERE id=?",(status,safe_error,json.dumps(trace or []),iso(now),scope["run_id"]))
            self.save_requests(db,"run",scope["run_id"],json_requests)
            job = dict(db.execute("SELECT * FROM jobs WHERE id=?",(scope["job_id"],)).fetchone())
            db.execute("UPDATE jobs SET next_run_at=?,last_run_at=?,last_error=? WHERE id=?",(iso(next_run(job,now,parse(job["next_run_at"]))),iso(now),safe_error,job["id"]))
            db.execute("UPDATE dispatcher SET last_execution=? WHERE id=1",(iso(now),))
            return status == "success"

    def assert_chat_owner(self,db,request_id,token,now):
        if not request_id:
            return
        request = db.execute("SELECT * FROM chat_requests WHERE id=?",(request_id,)).fetchone()
        if request and (request["status"] != "processing" or request["processing_token"] != token or (now-parse(request["created_at"])).total_seconds() >= 120):
            raise ValueError("Запрос больше не владеет диалогом; устаревшее изменение отклонено")

    def record_action(self,db,request_id,action_key,tool,job):
        if request_id:
            public = {key:value for key,value in job.items() if key not in ("request_id","instruction","skill_body","skill_hash")}
            db.execute("INSERT INTO chat_actions VALUES(?,?,?,?) ON CONFLICT(request_id,action_key) DO UPDATE SET tool=excluded.tool,payload=excluded.payload",(request_id,action_key,tool,json.dumps(public)))

    def chat_actions(self,request_id):
        """Read only committed mutations for this request, without retrying them."""
        with self.connection() as db:
            actions = db.execute("SELECT tool,payload FROM chat_actions WHERE request_id=? ORDER BY rowid",(request_id,)).fetchall()
            return [{"tool":action["tool"],"ok":True,"result":json.loads(action["payload"])} for action in actions]

    def begin_chat(self, request_id, prompt, now=None):
        now = now or now_utc()
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM chat_requests WHERE id=?",(request_id,)).fetchone()
            if row:
                if row["prompt"] != prompt:
                    raise ValueError("request_id уже использован для другого сообщения")
                if row["status"] == "done":
                    return {"answer":row["answer"],"trace":json.loads(row["trace"])}
                if (now-parse(row["created_at"])).total_seconds() < 120:
                    raise ValueError("Это сообщение уже выполняется; дождитесь ответа")
                actions = db.execute("SELECT tool,payload FROM chat_actions WHERE request_id=?",(request_id,)).fetchall()
                if actions:
                    trace = [{"tool":action["tool"],"ok":True,"result":json.loads(action["payload"])} for action in actions]
                    statuses = {"active":"сбор включён","paused":"сбор приостановлен","cancelled":"расписание удалено"}
                    answer = "Предыдущий ответ был прерван. Изменения сохранены: " + "; ".join(f"{item['result']['label']} — {statuses[item['result']['status']]}" for item in trace)
                    db.execute("UPDATE chat_requests SET status='done',answer=?,trace=? WHERE id=?",(answer,json.dumps(trace),request_id))
                    db.execute("INSERT INTO messages VALUES(?,'assistant',?,?,?)",(str(uuid.uuid4()),answer,json.dumps(trace),iso(now)))
                    return {"answer":answer,"trace":trace}
                token = str(uuid.uuid4())
                db.execute("UPDATE chat_requests SET created_at=?,processing_token=? WHERE id=?",(iso(now),token,request_id))
                return {"processing_token":token}
            timestamp = iso(now)
            token = str(uuid.uuid4())
            db.execute("INSERT INTO chat_requests VALUES(?,?,'processing',NULL,NULL,?,?)",(request_id,prompt,timestamp,token))
            db.execute("INSERT INTO messages VALUES(?,'user',?,'[]',?)",(str(uuid.uuid4()),prompt,timestamp))
            return {"processing_token":token}

    def finish_chat(self, request_id, answer, trace, token, json_requests=None):
        with self.connection(True) as db:
            result = db.execute("UPDATE chat_requests SET status='done',answer=?,trace=? WHERE id=? AND processing_token=? AND status='processing'",(answer,json.dumps(trace),request_id,token))
            if result.rowcount:
                message_id = str(uuid.uuid4())
                db.execute("INSERT INTO messages VALUES(?,'assistant',?,?,?)",(message_id,answer,json.dumps(trace),iso(now_utc())))
                self.save_requests(db,"message",message_id,json_requests)
            return bool(result.rowcount)

    def save_requests(self,db,scope,entity_id,requests):
        if requests:
            db.execute("INSERT INTO json_requests VALUES(?,?,?)",(scope,entity_id,json.dumps(requests,ensure_ascii=False)))

    def requests(self,scope,entity_id):
        table = {"message":"messages","run":"occurrences"}[scope]
        with self.connection() as db:
            if not db.execute(f"SELECT 1 FROM {table} WHERE id=?",(entity_id,)).fetchone():
                return None
            row = db.execute("SELECT payload FROM json_requests WHERE scope=? AND entity_id=?",(scope,entity_id)).fetchone()
            return json.loads(row["payload"]) if row else []

    def history(self):
        with self.connection() as db:
            return [dict(row) for row in reversed(db.execute("SELECT role,content FROM messages ORDER BY rowid DESC LIMIT 20").fetchall())]

    def data(self, job_id=None):
        where = "AND j.id=?" if job_id else ""
        args = (job_id,) if job_id else ()
        with self.connection() as db:
            count = db.execute(f"SELECT count(*) FROM weather_results w JOIN occurrences o ON o.id=w.run_id JOIN jobs j ON j.id=o.job_id WHERE w.persisted=1 {where}",args).fetchone()[0]
            rows = db.execute(f"SELECT w.id,j.id AS job_id,j.label,w.payload FROM weather_results w JOIN occurrences o ON o.id=w.run_id JOIN jobs j ON j.id=o.job_id WHERE w.persisted=1 {where} ORDER BY w.rowid DESC LIMIT 100",args).fetchall()
            records = [{"id":row["id"],"job_id":row["job_id"],"label":row["label"],**json.loads(row["payload"])} for row in rows]
            return {"count":count,"latest":records[0] if records else None,"records":records}

    def state(self):
        jobs = self.list_jobs()
        for job in jobs:
            for key in ("request_id","instruction","skill_body","skill_hash","created_at"):
                job.pop(key,None)
        with self.connection() as db:
            runs = [dict(row) for row in db.execute("SELECT id,job_id,status,scheduled_for,started_at,finished_at,error,trace,EXISTS(SELECT 1 FROM json_requests q WHERE q.scope='run' AND q.entity_id=occurrences.id) AS has_json_requests FROM occurrences ORDER BY rowid DESC LIMIT 100")]
            messages = [dict(row) for row in db.execute("SELECT id,role,content,trace,EXISTS(SELECT 1 FROM json_requests q WHERE q.scope='message' AND q.entity_id=messages.id) AS has_json_requests FROM messages ORDER BY rowid DESC LIMIT 100")][::-1]
            for row in runs + messages:
                row["trace"] = json.loads(row["trace"] or "[]")
                row["has_json_requests"] = bool(row["has_json_requests"])
            scheduler = dict(db.execute("SELECT last_tick,last_execution FROM dispatcher WHERE id=1").fetchone())
        data = self.data()
        return {"jobs":jobs,"samples":data["records"],"runs":runs,"messages":messages,"scheduler":scheduler,"total_samples":data["count"]}
