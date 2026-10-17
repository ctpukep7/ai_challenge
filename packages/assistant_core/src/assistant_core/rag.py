from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
import hashlib, json, math, os, sqlite3, tempfile, threading

MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
@dataclass
class Document:
    path: str
    text: str
    sha256: str
@dataclass
class Chunk:
    id: str
    path: str
    section: str
    start_line: int
    end_line: int
    text: str
    sha256: str

DENIED = {'.env', '.git', '.memory-bank', 'wiki', 'node_modules', 'data', '.venv', '.assistant-venv', '__pycache__'}
def documentation_paths(root: Path) -> list[str]:
    return sorted(set(['README.md'] + [str(p.relative_to(root)) for p in root.glob('day-[0-9][0-9]/README.md')] + [str(p.relative_to(root)) for p in (root/'docs').rglob('*.md')]))
def load_documents(root: Path, paths: list[str]) -> list[Document]:
    root = root.resolve(); docs=[]
    for rel in sorted(set(paths)):
        p = root / rel
        if Path(rel).is_absolute() or '..' in Path(rel).parts or any(x in DENIED or x.startswith('.env') for x in Path(rel).parts):
            raise ValueError('Недопустимый источник')
        if p.is_symlink() or any(parent.is_symlink() for parent in p.parents if parent != root.parent) or not p.resolve().is_relative_to(root):
            raise ValueError('Ссылки и выход за корень запрещены')
        raw=p.read_bytes()
        if len(raw)>2_000_000 or b'\0' in raw: raise ValueError('Источник слишком большой или бинарный')
        docs.append(Document(rel,raw.decode('utf-8'),hashlib.sha256(raw).hexdigest()))
    return docs

class FastEmbedder:
    max_tokens=126
    def __init__(self, cache_dir: Path):
        from fastembed import TextEmbedding
        self.model=TextEmbedding(model_name=MODEL,cache_dir=str(cache_dir),local_files_only=True,threads=2)
        self.tokenizer=self.model.model.tokenizer
        self.tokenizer.no_truncation()
        self.lock=threading.Lock()
    def offsets(self,text): return self.tokenizer.encode(text,add_special_tokens=False).offsets
    def embed(self,texts):
        if any(len(self.offsets(t))>126 for t in texts): raise ValueError('Слишком длинный embedding input')
        with self.lock: vectors=[list(map(float,v)) for v in self.model.embed(texts,batch_size=32)]
        if any(len(v)!=384 or not all(map(math.isfinite,v)) for v in vectors): raise ValueError('Некорректный embedding')
        return vectors

def chunk_document(doc: Document, embedder) -> list[Chunk]:
    offsets=embedder.offsets(doc.text); chunks=[]
    for i in range(0,len(offsets),106):
        group=offsets[i:i+126]
        start,end=group[0][0],group[-1][1]
        text=doc.text[start:end]
        # Tokenization at a new boundary can change token count; shrink safely.
        while len(embedder.offsets(text))>126:
            end=group[-2][1]; group=group[:-1]; text=doc.text[start:end]
        first=doc.text.count('\n',0,start)+1; last=doc.text.count('\n',0,end)+1
        section=next((s.lstrip('# ').strip() for s in reversed(doc.text[:start].splitlines()) if s.startswith('#')), 'Документ')
        cid=hashlib.sha256(f'{doc.path}:{doc.sha256}:{start}:{end}'.encode()).hexdigest()[:20]
        chunks.append(Chunk(cid,doc.path,section,first,last,text,doc.sha256))
    return chunks

class Index:
    def __init__(self,path: Path,root: Path,paths,embedder):
        self.path=Path(path); self.root=Path(root); self.paths=paths; self.embedder=embedder
        self.lock=threading.RLock()
    def documents(self): return load_documents(self.root,self.paths() if callable(self.paths) else self.paths)
    def status(self):
        with self.lock:
            current={d.path:d.sha256 for d in self.documents()}
            if not self.path.exists(): return {'ready':False,'stale':True,'documents':len(current),'chunks':0}
            with sqlite3.connect(self.path) as db:
                stored=json.loads(db.execute('SELECT value FROM meta WHERE key="sources"').fetchone()[0])
                count=db.execute('SELECT count(*) FROM chunks').fetchone()[0]
            return {'ready':True,'stale':stored!=current,'documents':len(stored),'chunks':count}
    def build(self):
        with self.lock:
            docs=self.documents(); chunks=[c for d in docs for c in chunk_document(d,self.embedder)]
            vectors=self.embedder.embed([c.text for c in chunks]) if chunks else []
            self.path.parent.mkdir(parents=True,exist_ok=True)
            fd,name=tempfile.mkstemp(dir=self.path.parent,suffix='.sqlite'); os.close(fd)
            try:
                with sqlite3.connect(name) as db:
                    db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
                    db.execute('CREATE TABLE chunks(id TEXT PRIMARY KEY,payload TEXT,vector TEXT)')
                    db.execute('INSERT INTO meta VALUES (?,?)',('sources',json.dumps({d.path:d.sha256 for d in docs})))
                    db.executemany('INSERT INTO chunks VALUES (?,?,?)',[(c.id,json.dumps(asdict(c),ensure_ascii=False),json.dumps(v)) for c,v in zip(chunks,vectors,strict=True)])
                os.replace(name,self.path)
            finally:
                if os.path.exists(name): os.unlink(name)
            return self.status()
    def search(self,query: str,top_k=5,threshold=.35,source_paths=None):
        with self.lock:
            status=self.status()
            if not status['ready']: raise ValueError('Сначала постройте индекс')
            if status['stale']: raise ValueError('Индекс устарел: обновите его перед поиском')
            # Long questions are bounded by tokenizer offsets, not silently truncated by model.
            offsets=self.embedder.offsets(query)
            if len(offsets)>126: query=query[:offsets[125][1]]
            q=self.embedder.embed([query])[0]
            def cosine(v):
                den=math.sqrt(sum(x*x for x in q)*sum(x*x for x in v))
                return sum(a*b for a,b in zip(q,v))/den if den else 0
            with sqlite3.connect(self.path) as db:
                rows=[dict(json.loads(p),score=cosine(json.loads(v))) for p,v in db.execute('SELECT payload,vector FROM chunks')]
            if source_paths is not None: rows=[r for r in rows if r['path'] in source_paths]
            candidates=sorted(rows,key=lambda x:x['score'],reverse=True)[:12]
            return [x for x in candidates if x['score']>=threshold][:top_k]

    def expand_neighbors(self,sources,limit=2):
        """Recover clipped sentences with bounded same-document context, never new scores."""
        with self.lock:
            status=self.status()
            if not status['ready'] or status['stale']: raise ValueError('Обновите индекс перед поиском')
            with sqlite3.connect(self.path) as db:
                rows=[json.loads(p) for (p,) in db.execute('SELECT payload FROM chunks ORDER BY rowid')]
            by_path={}
            for row in rows: by_path.setdefault(row['path'],[]).append(row)
            result=[dict(s,retrieval_role='anchor') for s in sources]
            seen={s['id'] for s in result}; remaining=min(2,max(0,limit))
            for anchor in sources:
                siblings=by_path.get(anchor['path'],[])
                position=next((i for i,s in enumerate(siblings) if s['id']==anchor['id']),None)
                if position is None: raise ValueError('Источник исчез из индекса')
                for index in (position-1,position+1):
                    if remaining and 0<=index<len(siblings) and siblings[index]['id'] not in seen:
                        row=siblings[index]
                        result.append(dict(row,score=None,retrieval_role='neighbor',anchor_id=anchor['id']))
                        seen.add(row['id']);remaining-=1
            return result
