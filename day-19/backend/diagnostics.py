"""Best-effort, bounded, credential-free records of actual exchanges.

This journal is educational evidence, not a control plane: errors here never
change provider/tool results or artifact bytes. Only normalized responses enter it.
"""
import asyncio
import json
from .errors import PipelineError, MESSAGES

MAX_VALUE_BYTES = 140_000
MAX_EXCHANGES = 24
MAX_RUN_BYTES = 1_000_000
DROP_KEYS = {"api_key", "apikey", "authorization", "proxy-authorization", "headers",
             "reasoning", "reasoning_content", "reasoning_details"}
REDACTED = "[REDACTED]"


def error_code(error):
    if isinstance(error, PipelineError):
        return error.code if error.code in MESSAGES else "internal"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(error, asyncio.CancelledError):
        return "interrupted"
    return "internal"


class Diagnostics:
    def __init__(self, store, run_id, secrets=(), private_paths=()):
        self.store, self.run_id = store, run_id
        values = {str(value) for value in (*secrets, *private_paths) if value}
        # Embedded JSON (messages / function arguments) may contain escaped keys.
        self.redactions = sorted(values | {json.dumps(value, ensure_ascii=False)[1:-1] for value in values}, key=len, reverse=True)

    def sanitize(self, value, depth=0):
        if depth > 20:
            return {"_diagnostic_truncated": True, "reason": "depth"}
        if isinstance(value, str):
            for secret in self.redactions:
                value = value.replace(secret, REDACTED)
            return value
        if isinstance(value, dict):
            return {self.sanitize(str(key), depth + 1): self.sanitize(item, depth + 1)
                    for key, item in value.items() if str(key).lower() not in DROP_KEYS}
        if isinstance(value, (list, tuple)):
            return [self.sanitize(item, depth + 1) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        # Never repr() arbitrary SDK objects or exceptions.
        return None

    def bounded(self, value):
        result = self.sanitize(value)
        size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
        if size > MAX_VALUE_BYTES:
            return {"_diagnostic_truncated": True, "original_bytes": size, "limit_bytes": MAX_VALUE_BYTES}
        return result

    def begin(self, stage, kind, request):
        try:
            return self.store.begin_exchange(self.run_id, stage, kind, self.bounded(request))
        except Exception:
            return None

    def finish(self, exchange_id, response=None, code=None):
        if exchange_id is None:
            return
        try:
            self.store.finish_exchange(self.run_id, exchange_id,
                                       self.bounded(response) if response is not None else None, code)
        except Exception:
            pass
