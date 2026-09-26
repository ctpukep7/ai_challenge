"""Detached observations of real SDK boundaries, redacted before persistence."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import re
from backend.store import now, uid

REDACTED='[скрыто]'
_context=ContextVar('day20_exchange_context',default=None)
_UNSET=object()

@contextmanager
def capture_scope(run_id,trace_sequence=None,tool_call_id=None):
    token=_context.set(dict(run_id=run_id,trace_sequence=trace_sequence,tool_call_id=tool_call_id))
    try:yield
    finally:_context.reset(token)

def capture_metadata():
    value=_context.get()
    return dict(value) if value else None

class JsonObserver:
    def __init__(self,store,secrets=(),paths=(),max_bytes=65536):
        self.store=store;self.max_bytes=max(256,max_bytes)
        self.secrets=set()
        for value in (*secrets,*paths):
            if value:
                self.secrets.update((str(value),json.dumps(str(value),ensure_ascii=True)[1:-1],json.dumps(str(value),ensure_ascii=False)[1:-1]))
        for _ in range(2):
            current=list(self.secrets)
            for value in current:
                self.secrets.update((json.dumps(value,ensure_ascii=True)[1:-1],json.dumps(value,ensure_ascii=False)[1:-1],value.replace('/','\\/')))
        self.secrets=sorted(self.secrets,key=len,reverse=True)

    def _text(self,value):
        for secret in self.secrets:value=value.replace(secret,REDACTED)
        # Exclude actual local service paths, preserve protocol paths such as /chat/completions.
        value=re.sub(r'(?:/(?:Users|private|tmp|home|var|opt)/[^\s"\'<>]*|[A-Za-z]:\\[^\s"\'<>]*)',REDACTED,value)
        if 'BEGIN:VCALENDAR' in value.upper() or 'BEGIN:VEVENT' in value.upper():return REDACTED
        if not value.lstrip().startswith(('{','[')) and re.search(r'(?i)\\*"(?:headers|env|environment|reasoning_content|ics)\\*"\s*:',value):return REDACTED
        value=re.sub(r'(?i)("(?:api[_-]?key|password|authorization|reasoning_content)"\s*:\s*)"(?:\\.|[^"\\])*"',lambda m:m[1]+json.dumps(REDACTED),value)
        return value

    def snapshot(self,value):
        truncated=False;count=0
        sensitive={'apikey','password','apppassword','authorization','headers','env','environment','reasoningcontent','ics','ical','icalendar','attendees','organizer','summary','eventdata','calendardata'}
        def walk(item,depth=0):
            nonlocal truncated,count
            count+=1
            if depth>24 or count>5000:truncated=True;return '[сокращено]'
            if isinstance(item,dict):
                result={}
                event_fields={re.sub(r'[^a-z0-9]','',str(k).lower()) for k in item}
                private_event=bool(event_fields & {'summary','dtstart','dtend','attendees','organizer'})
                for key,child in item.items():
                    safe_key=self._text(str(key));normalized=re.sub(r'[^a-z0-9]','',str(key).lower())
                    if any(normalized.endswith(n) for n in ('apikey','password','authorization','reasoningcontent')) or normalized in sensitive or (private_event and normalized in {'description','location'}):
                        result[safe_key]=REDACTED
                    elif count>5000:truncated=True;result['[сокращено]']=True;break
                    else:result[safe_key]=walk(child,depth+1)
                return result
            if isinstance(item,(list,tuple)):
                result=[]
                for child in item:
                    if count>5000:truncated=True;result.append('[сокращено]');break
                    result.append(walk(child,depth+1))
                return result
            if isinstance(item,str):
                text=self._text(item)
                if text.lstrip().startswith(('{','[')):
                    if len(text)>=self.max_bytes*2:truncated=True;return '[JSON сокращён до безопасного представления]'
                    try:
                        parsed=json.loads(text)
                        if isinstance(parsed,(dict,list)):text=json.dumps(walk(parsed,depth+1),ensure_ascii=False)
                    except (ValueError,RecursionError):
                        if re.search(r'(?i)(headers|env|password|authorization|api[_-]?key|reasoning_content)',text):return REDACTED
                if len(text)>16000:truncated=True;text=text[:16000]+'…[сокращено]'
                return text
            if item is None or isinstance(item,(bool,int,float)):return item
            return self._text(str(item))
        try:
            result=walk(value)
            encoded=json.dumps(result,ensure_ascii=False,allow_nan=False)
        except (ValueError,TypeError,RecursionError):return {'unavailable':'Не удалось безопасно представить JSON'},True
        if len(encoded.encode())>self.max_bytes:
            truncated=True
            preview=encoded.encode()[:max(0,(self.max_bytes-160)//2)].decode('utf-8','ignore')
            result=dict(truncated=True,preview=preview)
        # The JSON roundtrip guarantees the record shares no mutable references with SDK payloads.
        return json.loads(json.dumps(result,ensure_ascii=False)),truncated

    def sanitize(self,value):return self.snapshot(value)[0]

    def begin(self,kind,request,metadata=_UNSET,**details):
        if metadata is _UNSET:metadata=capture_metadata()
        if not metadata or not metadata.get('run_id'):return None
        try:
            safe,truncated=self.snapshot(request)
            record=dict(id=uid(),run_id=metadata['run_id'],sequence=0,kind=kind,status='running',duration_ms=0,tool_call_id=metadata.get('tool_call_id'),trace_sequence=metadata.get('trace_sequence'),request=safe,response=None,error=None,truncated=truncated,started_at=now(),server_id=details.get('server_id'),alias=details.get('alias'))
            for name in ('tool_call_id','alias','server_id'):
                if record.get(name) is not None:
                    safe_value=self._text(str(record[name]))
                    if len(safe_value)>512:record['truncated']=True;safe_value=safe_value[:512]+'…[сокращено]'
                    record[name]=safe_value
            return self.store.allocate_json_request(record)['id']
        except Exception:return None

    def finish(self,id,status,response=None,error=None):
        if not id:return
        try:
            record=self.store.get('json_request',id)
            # Restart/cancellation is final; a late server response must not invent a completed exchange.
            if record['status']!='running':return
            safe,truncated=self.snapshot(response)
            from datetime import datetime,timezone
            record.update(status=status,response=safe if response is not None else None,error=self._text(str(error))[:500] if error else None,
                          duration_ms=max(0,round((datetime.now(timezone.utc)-datetime.fromisoformat(record['started_at'])).total_seconds()*1000)),truncated=record['truncated'] or truncated)
            self.store.put('json_request',record)
        except Exception:pass
