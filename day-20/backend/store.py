from backend.errors import DomainError
import json
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone


def uid(): return str(uuid.uuid4())
def now(): return datetime.now(timezone.utc).isoformat()

class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents(kind TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS requests(request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,run_id TEXT NOT NULL);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory=sqlite3.Row
        return db

    def put(self, kind, value, db=None):
        if db is None:
            with self.connect() as connection: return self.put(kind,value,connection)
        db.execute('INSERT INTO documents VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body',
                   (kind,value['id'],json.dumps(value,ensure_ascii=False)))
        return value

    def get(self, kind, id, db=None):
        if db is None:
            with self.connect() as connection: return self.get(kind,id,connection)
        row=db.execute('SELECT body FROM documents WHERE kind=? AND id=?',(kind,id)).fetchone()
        if not row: raise DomainError('Объект не найден')
        return json.loads(row['body'])

    def all(self, kind):
        with self.connect() as db:
            return [json.loads(row['body']) for row in db.execute('SELECT body FROM documents WHERE kind=? ORDER BY rowid',(kind,))]

    def evidence(self, kind, run_id, data):
        return self.put('evidence',dict(id=uid(),kind=kind,run_id=run_id,data=data))

    def require(self, evidence_id, kind, run_id):
        evidence=self.get('evidence',evidence_id)
        if evidence['kind'] != kind or evidence['run_id'] != run_id: raise DomainError('Источник не относится к этому запуску')
        return evidence['data']

    def new_run(self, request_id, fingerprint, prompt, calendar_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM requests WHERE request_id=?',(request_id,)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint: raise DomainError('request_id уже используется для другого запроса')
                return self.get('run',row['run_id'],db),False
            run=dict(id=uid(),status='queued',prompt=prompt,calendar_id=calendar_id,answer='',error=None,trace=[],plan=None,created_at=now())
            db.execute('INSERT INTO requests VALUES(?,?,?)',(request_id,fingerprint,run['id']))
            self.put('run',run,db)
            self.put('message',dict(id=uid(),role='user',content=prompt,run_id=run['id']),db)
            return run,True

    def update_run(self, id, **changes):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run=self.get('run',id,db);run.update(changes);return self.put('run',run,db)

    def allocate_json_request(self, value):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=[json.loads(row['body']) for row in db.execute("SELECT body FROM documents WHERE kind='json_request'")]
            sequence=max((r['sequence'] for r in existing if r['run_id']==value['run_id']),default=0)+1
            value=dict(value,sequence=sequence)
            return self.put('json_request',value,db)

    def json_requests(self, run_id):
        return sorted((r for r in self.all('json_request') if r['run_id']==run_id),key=lambda r:r['sequence'])

    def allocate_trace(self, id, value):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run=self.get('run',id,db)
            value=dict(value,sequence=max((t['sequence'] for t in run['trace']),default=0)+1)
            run['trace'].append(value);self.put('run',run,db)
            return value

    def trace(self, id, value):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run=self.get('run',id,db)
            existing=next((i for i,t in enumerate(run['trace']) if t['sequence']==value['sequence']),None)
            if existing is None:run['trace'].append(value)
            else:run['trace'][existing]=value
            self.put('run',run,db)

    def plan_for_run(self, run_id):
        plans=[p for p in self.all('plan') if p['run_id']==run_id]
        return plans[-1] if plans else None

    def recover(self):
        for exchange in self.all('json_request'):
            if exchange['status']=='running':
                exchange.update(status='interrupted',error='Обмен прерван перезапуском приложения',response=None)
                self.put('json_request',exchange)
        for run in self.all('run'):
            changed=False
            for trace in run['trace']:
                if trace['status']=='running':
                    trace.update(status='error',error='Вызов прерван перезапуском приложения',result_summary='Исход вызова не подтверждён');changed=True
            if changed:self.put('run',run)
            if run['status'] in ('queued','running'):
                self.update_run(run['id'],status='interrupted',error='Запуск прерван перезапуском приложения',plan=self.plan_for_run(run['id']))
        for op in self.all('operation'):
            if op['status'] in ('pending','approved','writing'):
                op['status']='uncertain';self.put('operation',op)
                plan=self.get('plan',op['plan_id'])
                if plan['status']!='created':
                    plan['status']='uncertain';plan['error']='Подтверждение прервано перезапуском; требуется сверка UID';self.put('plan',plan)
