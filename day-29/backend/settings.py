"""Settings validation shared by the local API and its benchmark. No provider calls."""
import copy
import math
import json

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

from .profiles import LIMITS, OPTIMIZED, OPTIMIZED_SYSTEM


class Settings(BaseModel):
    """Validate the editable settings; apply only explicitly supplied overrides."""
    model_config = ConfigDict(extra="forbid")
    temperature: float = OPTIMIZED["temperature"]
    num_predict: StrictInt = OPTIMIZED["num_predict"]
    num_ctx: StrictInt = OPTIMIZED["num_ctx"]
    top_k: StrictInt = OPTIMIZED["top_k"]
    top_p: float = OPTIMIZED["top_p"]
    min_p: float = OPTIMIZED["min_p"]
    repeat_penalty: float = OPTIMIZED["repeat_penalty"]
    presence_penalty: float = OPTIMIZED.get("presence_penalty", 0)
    format: None | StrictStr | dict = None
    repeat_last_n: StrictInt = OPTIMIZED["repeat_last_n"]
    seed: StrictInt = OPTIMIZED["seed"]
    stop: list[StrictStr] = Field(default_factory=lambda: copy.deepcopy(OPTIMIZED["stop"]))
    system: StrictStr = OPTIMIZED_SYSTEM

    @field_validator("format", mode="before")
    @classmethod
    def allowed_format(cls, value):
        if value is None or value == "json":
            return value
        if not isinstance(value, dict) or not value:
            raise ValueError("format must be null, json or a nonempty JSON Schema object")
        nodes = 0
        def visit(item, depth=1):
            nonlocal nodes
            nodes += 1
            if depth > 8 or nodes > 256:
                raise ValueError("JSON Schema exceeds depth 8 or 256 nodes")
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item) or "$ref" in item:
                    raise ValueError("JSON Schema keys must be strings; $ref is unsupported")
                for child in item.values(): visit(child, depth + 1)
            elif isinstance(item, list):
                for child in item: visit(child, depth + 1)
            elif not (item is None or isinstance(item, (str, bool, int, float))):
                raise ValueError("JSON Schema must contain JSON values")
        visit(value)
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if len(text) > 5000 or len(text.encode("utf-8")) > 12000:
                raise ValueError("JSON Schema exceeds 5000 characters or 12000 UTF-8 bytes")
        except (TypeError, UnicodeError, OverflowError):
            raise ValueError("invalid JSON Schema") from None
        return copy.deepcopy(value)

    @field_validator("temperature", "top_p", "min_p", "repeat_penalty", "presence_penalty", mode="before")
    @classmethod
    def sampling_number(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("numeric setting required")
        return value

    @field_validator("stop", mode="before")
    @classmethod
    def stop_list(cls, value):
        if not isinstance(value, list):
            raise ValueError("stop must be a list")
        return value

    @field_validator("stop")
    @classmethod
    def allowed_stop(cls, stops):
        limit = LIMITS["stop"]
        if len(stops) > limit["max_items"] or len(stops) != len(set(stops)):
            raise ValueError("too many or duplicate stops")
        try:
            if any(not stop or len(stop) > limit["max_chars"] or len(stop.encode("utf-8")) > limit["max_bytes"] for stop in stops):
                raise ValueError("invalid stop")
        except UnicodeEncodeError:
            raise ValueError("invalid Unicode") from None
        return stops

    @field_validator("system")
    @classmethod
    def allowed_system(cls, system):
        try:
            if len(system) > LIMITS["system"]["max_chars"] or len(system.encode("utf-8")) > LIMITS["system"]["max_bytes"]:
                raise ValueError("system too long")
        except UnicodeEncodeError:
            raise ValueError("invalid Unicode") from None
        return system

    @model_validator(mode="after")
    def allowed(self):
        for key in ("temperature", "top_k", "top_p", "min_p", "repeat_penalty", "repeat_last_n", "seed", "presence_penalty"):
            value = getattr(self, key)
            if not LIMITS[key]["min"] <= value <= LIMITS[key]["max"] or not math.isfinite(value):
                raise ValueError(f"unsupported {key}")
        if self.num_predict not in LIMITS["num_predict"] or self.num_ctx not in LIMITS["num_ctx"]:
            raise ValueError("unsupported context/output")
        return self
