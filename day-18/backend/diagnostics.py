"""Bounded, detached JSON bodies; transport headers and local secrets stay private."""
from copy import deepcopy
import json

MAX_BODY_BYTES = 64_000
MAX_RECORDS = 48  # Eight model steps plus at most five MCP calls per step.
PRIVATE_FIELDS = {'api_key','authorization','headers','env','db_path','processing_token','lease_token','token'}


def completion_body(config,messages,tools):
    return {'model':config.model,'messages':messages,'tools':tools,'tool_choice':'auto',
            'max_tokens':1200,'thinking':{'type':'disabled'}}


class DiagnosticLog:
    def __init__(self,secrets=()):
        variants = set()
        for secret in secrets:
            if secret:
                value = str(secret)
                variants.update((value,json.dumps(value,ensure_ascii=False)[1:-1],json.dumps(value,ensure_ascii=True)[1:-1]))
        self.secrets = sorted(variants,key=len,reverse=True)
        self.records = []

    def _safe(self,value):
        if isinstance(value,str):
            for secret in self.secrets:
                value = value.replace(secret,'[скрыто]')
            return value
        if isinstance(value,dict):
            return {self._safe(str(key)):('[скрыто]' if str(key).lower() in PRIVATE_FIELDS else self._safe(item)) for key,item in value.items()}
        if isinstance(value,list):
            return [self._safe(item) for item in value]
        return value

    def snapshot(self,value):
        safe = self._safe(deepcopy(value))
        encoded = json.dumps(safe,ensure_ascii=False,allow_nan=False)
        size = len(encoded.encode('utf-8'))
        if size > MAX_BODY_BYTES:
            return {'_truncated':True,'original_bytes':size,'preview':encoded[:2000]}
        return safe

    def begin(self,protocol,label,request):
        record = {'protocol':protocol,'label':label,'request':self.snapshot(request),'ok':False}
        if len(self.records) < MAX_RECORDS:
            self.records.append(record)
        return record

    def success(self,record,response):
        record.update(response=self.snapshot(response),ok=True)

    def failure(self,record,message):
        record.update(error=self.snapshot(message),ok=False)
