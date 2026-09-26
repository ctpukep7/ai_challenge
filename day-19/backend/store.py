"""Small SQLite journal shared by the API and the per-run MCP process."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone
from contextlib import contextmanager
from uuid import UUID, uuid4
from .errors import PipelineError

TOOLS = ("search", "summarize", "saveToFile")

def now():
    return datetime.now(timezone.utc).isoformat()

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()

def valid_id(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise PipelineError("invalid_arguments") from None

class Store:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "pipeline.sqlite3"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                  prompt TEXT NOT NULL, status TEXT NOT NULL, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                  kind TEXT NOT NULL, args TEXT NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL,
                  UNIQUE(run_id, kind));
            """)

    @contextmanager
    def db(self, timeout=10):
        db = sqlite3.connect(self.db_path, timeout=timeout)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def admit(self, prompt, request_id, configured=True):
        request_id = valid_id(request_id)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM runs WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous["prompt"] != prompt:
                    raise PipelineError("request_conflict")
                return json.loads(previous["document"]), True
            if not configured:
                raise PipelineError("configuration")
            if db.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                raise PipelineError("busy")
            run_id, stamp = str(uuid4()), now()
            run = {"run_id": run_id, "request_id": request_id, "prompt": prompt,
                   "status": "running", "current_step": "search", "created_at": stamp,
                   "updated_at": stamp, "error": None, "error_code": None,
                   "steps": [{"name": name, "status": "pending", "input_id": None,
                              "output_id": None, "input_sha256": None, "output_sha256": None,
                              "started_at": None, "completed_at": None, "duration_ms": None,
                              "error": None} for name in TOOLS], "trace": [], "exchanges": [],
                   "search_result": None, "summary_result": None, "file_result": None,
                   "download_url": None}
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (run_id, request_id, prompt, "running", canonical(run)))
            return run, False

    def get(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT document FROM runs WHERE id=?", (str(run_id),)).fetchone()
        if not row:
            raise PipelineError("not_found")
        return json.loads(row[0])

    def latest(self):
        with self.db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT document FROM runs ORDER BY rowid DESC LIMIT 10")]

    def update(self, run_id, change):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise PipelineError("not_found")
            run = json.loads(row[0])
            change(run)
            run["updated_at"] = now()
            db.execute("UPDATE runs SET status=?, document=? WHERE id=?", (run["status"], canonical(run), run_id))
            return run

    def artifact(self, run_id, artifact_id, kind):
        artifact_id = valid_id(artifact_id)
        with self.db() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id=? AND run_id=? AND kind=?", (artifact_id, run_id, kind)).fetchone()
        if not row:
            raise PipelineError("artifact_missing")
        payload = json.loads(row["payload"])
        if digest(payload) != row["hash"]:
            raise PipelineError("artifact_integrity")
        # The exposed hashes bind content rather than the receipt itself.
        if kind == "search":
            content = {key: payload[key] for key in ("query", "sources", "retrieved_at")}
        elif kind == "summarize":
            content = payload["markdown"].encode("utf-8")
        else:
            content = None
        if content is not None and digest(content) != payload["sha256"]:
            raise PipelineError("artifact_integrity")
        return payload

    def existing(self, run_id, kind, args):
        with self.db() as db:
            row = db.execute("SELECT id,args FROM artifacts WHERE run_id=? AND kind=?", (run_id, kind)).fetchone()
        if row:
            if row["args"] != canonical(args):
                raise PipelineError("stage_order")
            return self.artifact(run_id, row["id"], kind)
        return None

    def check_stage(self, run_id, name):
        run = self.get(run_id)
        index = TOOLS.index(name)
        if run["status"] != "running" or any(step["status"] != "completed" for step in run["steps"][:index]):
            raise PipelineError("stage_order")
        if any(step["status"] == "completed" for step in run["steps"][index:]):
            raise PipelineError("stage_order")

    def start_step(self, run_id, name, input_id=None, input_hash=None):
        def change(run):
            step = run["steps"][TOOLS.index(name)]
            step.update(status="running", started_at=now(), input_id=input_id, input_sha256=input_hash)
            run["current_step"] = name
        self.update(run_id, change)

    def register(self, run_id, kind, args, payload):
        id_field = {"search": "search_result_id", "summarize": "summary_id", "saveToFile": "file_id"}[kind]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            run = json.loads(db.execute("SELECT document FROM runs WHERE id=?", (run_id,)).fetchone()[0])
            index = TOOLS.index(kind)
            if run["status"] != "running" or any(step["status"] != "completed" for step in run["steps"][:index]):
                raise PipelineError("stage_order")
            prior = db.execute("SELECT * FROM artifacts WHERE run_id=? AND kind=?", (run_id, kind)).fetchone()
            if prior:
                if prior["args"] != canonical(args):
                    raise PipelineError("stage_order")
                return json.loads(prior["payload"])
            db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?)", (payload[id_field], run_id, kind, canonical(args), canonical(payload), digest(payload)))
            step = run["steps"][TOOLS.index(kind)]
            stamp = now()
            duration = int((datetime.fromisoformat(stamp) - datetime.fromisoformat(step["started_at"] or stamp)).total_seconds() * 1000)
            step.update(status="completed", output_id=payload[id_field], output_sha256=payload["sha256"], completed_at=stamp, duration_ms=duration)
            key = {"search": "search_result", "summarize": "summary_result", "saveToFile": "file_result"}[kind]
            run[key] = payload
            if kind == "saveToFile":
                run["download_url"] = payload["download_url"]
            run["updated_at"] = stamp
            db.execute("UPDATE runs SET document=? WHERE id=?", (canonical(run), run_id))
        return payload

    def trace(self, run_id, event):
        def change(run):
            if len(run["trace"]) < 20:
                run["trace"].append({"at": now(), **event})
        self.update(run_id, change)

    def begin_exchange(self, run_id, stage, kind, request):
        from .diagnostics import MAX_EXCHANGES, MAX_RUN_BYTES
        if stage not in (*TOOLS, "setup") or kind not in ("mcp", "deepseek_agent", "deepseek_summary", "tavily"):
            return None
        # Logging waits at most 50ms for another SQLite writer, then is dropped.
        with self.db(timeout=.05) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                return None
            run = json.loads(row[0])
            exchanges = run.setdefault("exchanges", [])
            if len(exchanges) >= MAX_EXCHANGES:
                return None
            if len(canonical(exchanges).encode()) + len(canonical(request).encode()) > MAX_RUN_BYTES - 5000:
                request = {"_diagnostic_truncated": True, "reason": "run_budget"}
            exchange_id = str(uuid4())
            entry = {"exchange_id": exchange_id, "stage": stage, "kind": kind,
                     "started_at": now(), "completed_at": None, "duration_ms": None,
                     "status": "running", "request": request, "response": None, "error_code": None}
            # Leave enough space to close every admitted pair, even near the cap.
            if len(canonical([*exchanges, entry]).encode()) > MAX_RUN_BYTES - 2000:
                return None
            exchanges.append(entry)
            db.execute("UPDATE runs SET document=? WHERE id=?", (canonical(run), run_id))
            return exchange_id

    def finish_exchange(self, run_id, exchange_id, response, code=None):
        from .diagnostics import MAX_RUN_BYTES
        with self.db(timeout=.05) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                return
            run = json.loads(row[0])
            exchanges = run.get("exchanges", [])
            exchange = next((entry for entry in exchanges if entry["exchange_id"] == exchange_id), None)
            if exchange is None or exchange["status"] != "running":
                return
            if len(canonical(exchanges).encode()) + len(canonical(response).encode()) > MAX_RUN_BYTES - 5000:
                response = {"_diagnostic_truncated": True, "reason": "run_budget"}
            stamp = now()
            duration = int((datetime.fromisoformat(stamp) - datetime.fromisoformat(exchange["started_at"])).total_seconds() * 1000)
            exchange.update(completed_at=stamp, duration_ms=max(0, duration),
                            status="failed" if code else "completed", response=response, error_code=code)
            db.execute("UPDATE runs SET document=? WHERE id=?", (canonical(run), run_id))

    @staticmethod
    def close_open_exchanges(run, code):
        # Missing completion means the diagnostic record is incomplete, not a
        # fabricated upstream result. This also handles abrupt subprocess exits.
        stamp = now()
        for exchange in run.get("exchanges", []):
            if exchange["status"] == "running":
                duration = int((datetime.fromisoformat(stamp) - datetime.fromisoformat(exchange["started_at"])).total_seconds() * 1000)
                exchange.update(status="failed", completed_at=stamp, duration_ms=max(0, duration),
                                error_code=code, response=None)

    def fail(self, run_id, code, interrupted=False):
        def change(run):
            self.close_open_exchanges(run, code)
            run.update(status="interrupted" if interrupted else "failed", error=str(PipelineError(code)), error_code=code)
            for step in run["steps"]:
                if step["status"] == "running":
                    stamp = now()
                    step.update(status="failed", error=str(PipelineError(code)), completed_at=stamp)
                    step["duration_ms"] = int((datetime.fromisoformat(stamp) - datetime.fromisoformat(step["started_at"])).total_seconds() * 1000)
                    break
            else:
                for step in run["steps"]:
                    if step["status"] == "pending":
                        step.update(status="failed", error=str(PipelineError(code)), completed_at=now())
                        break
        return self.update(run_id, change)

    def file_bytes(self, run_id):
        run = self.get(run_id)
        receipt = run["file_result"]
        if not receipt:
            raise PipelineError("no_file")
        receipt = self.artifact(run_id, receipt["file_id"], "saveToFile")
        summary = self.artifact(run_id, receipt["summary_id"], "summarize")
        path = self.data_dir / "files" / f"{run_id}.md"
        try:
            if path.is_symlink():
                raise OSError()
            data = path.read_bytes()
        except OSError:
            raise PipelineError("artifact_integrity") from None
        if digest(data) != receipt["sha256"] or data != summary["markdown"].encode() or len(data) != receipt["bytes"]:
            raise PipelineError("artifact_integrity")
        return data

    def complete(self, run_id):
        self.file_bytes(run_id)
        def change(run):
            self.close_open_exchanges(run, "diagnostic_incomplete")
            run.update(status="completed", current_step="saveToFile", error=None, error_code=None)
        return self.update(run_id, change)

    def recover(self):
        with self.db() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM runs WHERE status='running'")]
        for run_id in ids:
            try:
                self.complete(run_id)
            except PipelineError:
                self.fail(run_id, "interrupted", interrupted=True)

    def atomic_write(self, run_id, data):
        run_id = valid_id(run_id)
        folder = self.data_dir / "files"
        try:
            folder.mkdir(exist_ok=True)
            if folder.is_symlink():
                raise OSError()
            destination = folder / f"{run_id}.md"
            if destination.is_symlink():
                raise OSError()
            if destination.exists():
                if destination.read_bytes() == data:
                    return
                raise OSError()
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=folder, prefix=".pending-", delete=False) as file:
                    temporary = file.name
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, destination)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError:
            raise PipelineError("write_failed") from None
