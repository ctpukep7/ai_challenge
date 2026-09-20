"""Allowlisted lifecycle tools and safe deterministic user-facing results."""

from __future__ import annotations

import json
import re


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "define_task",
            "description": "Define or refine the task during active planning. Set planning_ready=false only for genuinely blocking questions; otherwise make explicit assumptions and set it true.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["goal", "proposed_plan", "planning_ready", "current_step", "expected_action"],
                "properties": {
                    "goal": {"type": "string", "maxLength": 500},
                    "hard_constraints": {"type": "string", "maxLength": 500},
                    "task_data": {"type": "string", "maxLength": 500},
                    "open_questions": {"type": "string", "maxLength": 500},
                    "planning_ready": {"type": "boolean"},
                    "proposed_plan": {"type": "string", "maxLength": 2000},
                    "current_step": {"type": "string", "maxLength": 500},
                    "expected_action": {"type": "string", "maxLength": 500},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "accept_current_evidence_and_advance",
            "description": "Use only when the current user message explicitly accepts the pending evidence and explicitly asks to continue. The server independently verifies consent and derives approval, event, target and revision.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_current_evidence",
            "description": "Save execution evidence only. Never use this in planning (use define_task) or validation (use record_validation_result). This never approves evidence.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["content"],
                "properties": {"content": {"type": "string", "maxLength": 2000}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_validation_result",
            "description": "Record the typed validation outcome. Use has_blockers=true with a non-empty issue description, or has_blockers=false with an empty blocking_issues string when validation found no remaining blockers.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["has_blockers", "blocking_issues"],
                "properties": {
                    "has_blockers": {"type": "boolean"},
                    "blocking_issues": {"type": "string", "maxLength": 1000},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_transition",
            "description": "Request a lifecycle event. The server derives the target and enforces guards.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["event"],
                "properties": {
                    "event": {"type": "string", "enum": ["advance", "approve"]},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pause_task",
            "description": "Pause the current non-terminal task without changing its phase or evidence.",
            "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resume_task",
            "description": "Resume a paused task without changing its phase or evidence.",
            "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "respond_without_action",
            "description": "Explicitly choose a conversational answer without changing lifecycle state.",
            "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        },
    },
]

ALLOWED_TOOL_NAMES = frozenset(item["function"]["name"] for item in TOOL_DEFINITIONS)


def parse_tool_arguments(raw) -> dict:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        value = raw
    elif isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("Аргументы действия не являются корректным JSON.") from error
    else:
        raise ValueError("Аргументы действия должны быть JSON-объектом.")
    if not isinstance(value, dict):
        raise ValueError("Аргументы действия должны быть JSON-объектом.")
    return value


def fallback_answer(result: dict) -> str:
    action = result["action"]
    outcome = result["outcome"]
    if outcome == "accepted":
        labels = {
            "define_task": "Задача сохранена.",
            "save_current_evidence": "Доказательство сохранено. Для подтверждения требуется отдельное действие пользователя.",
            "record_validation_result": "Результат проверки сохранён.",
            "accept_current_evidence_and_advance": "Ваше подтверждение принято, задача перешла к следующему шагу.",
            "request_transition": "Переход выполнен.",
            "pause_task": "Задача поставлена на паузу.",
            "resume_task": "Задача продолжена с сохранённого состояния.",
        }
        return labels.get(action, "Действие выполнено.")
    unmet = result.get("unmet_requirements") or []
    if unmet:
        return "Переход отклонён. " + " ".join(item["message"] for item in unmet)
    return result.get("message") or "Действие отклонено сервером."


CONSENT_SUBJECT_MARKERS = {
    "plan_approved": r"(?:план\w*|спис\w*|переч\w*|шаг\w*|предложен\w*)",
    "implementation_ready": r"(?:результат\w*|сбор\w*|собран\w*|подготов\w*|выполн\w*|готовност\w*|вещ\w*)",
    "validation_passed": r"(?:проверк\w*|замечан\w*|проблем\w*|недоч[её]т\w*|документ\w*)",
}
CONSENT_ACCEPTANCE_MARKER = r"(?:нрав\w*|соглас\w*|подтвержд\w*|подход\w*|принима\w*|устраива\w*|верн\w*|готов\w*|пройден\w*)"


def consent_decision(text: str, approval_kind: str | None = None) -> tuple[bool, str]:
    normalized=" ".join(text.lower().replace("ё","е").split())
    if any(mark in normalized for mark in ("?","«","»","\"")):
        return False,"consent_ambiguous"
    if re.search(r"\b(если|если бы|может|можно ли|возможно|наверное|пожалуй|цитат|сказал|сказала)\b",normalized):
        return False,"consent_conditional"
    if re.search(r"\bне\s+(нравится|согласен|согласна|подтверждаю|принимаю|устраивает|подходит|готов|готова|надо)\b",normalized):
        return False,"consent_negated"
    accepted=bool(re.search(r"\b(нравится|согласен|согласна|подтверждаю|принимаю|устраивает|все верно|подходит|проверка пройдена)\b",normalized))
    progress=bool(re.search(r"\b(давай\s+дальше|переходим|перейдем|продолжим|можно\s+(выполнять|проверять|завершать|переходить)|давай\s+(выполнять|проверять|завершать|переходить))\b",normalized))
    if not accepted:return False,"consent_acceptance_missing"
    if not progress:return False,"consent_progress_missing"
    subject_pattern=CONSENT_SUBJECT_MARKERS.get(approval_kind or "")
    if not subject_pattern:return False,"consent_subject_missing"
    clauses=[part.strip() for part in re.split(r"[,;.!]",normalized) if part.strip()]
    explicitly_bound=any(
        re.search(subject_pattern,clause) and re.search(CONSENT_ACCEPTANCE_MARKER,clause)
        for clause in clauses
    )
    if not explicitly_bound:return False,"consent_subject_missing"
    return True,"consent_explicit"


def transition_intent_decision(text: str, phase: str, event: str) -> tuple[bool, str]:
    normalized=" ".join(text.lower().replace("ё","е").split())
    if any(mark in normalized for mark in ("?","«","»","\"")):
        return False,"transition_intent_ambiguous"
    if re.search(r"\b(если|если бы|может|можно ли|возможно|наверное|пожалуй|цитат|сказал|сказала)\b",normalized):
        return False,"transition_intent_conditional"
    transition_verb=(
        r"(?:переход\w*|перейд\w*|перейт\w*|продолж\w*|начина\w*|начн\w*|приступ\w*|собира\w*|выполня\w*|"
        r"провер\w*|заверш\w*|заканч\w*|возвращ\w*|дорабат\w*|исправ\w*)"
    )
    direct_negation=rf"\bне\s+{transition_verb}\b"
    auxiliary_negation=(
        rf"\bне\s+(?:надо|нужно|следует|хоч\w*|буд\w*|готов\w*)\s+"
        rf"(?:пока\s+)?{transition_verb}\b"
    )
    request_negation=(
        rf"\bне\s+(?:прошу|просил|просила|просим)\b[^.!?;]{{0,48}}\b{transition_verb}\b"
    )
    rejected_acceptance=r"\bне\s+(?:нравится|соглас\w*|подтвержд\w*)\b"
    if (re.search(direct_negation,normalized) or re.search(auxiliary_negation,normalized)
            or re.search(request_negation,normalized) or re.search(rejected_acceptance,normalized)):
        return False,"transition_intent_negated"
    patterns={
        ("planning","advance"):
            r"\b(?:переход\w*|перейд\w*|перейт\w*|продолж\w*|начина\w*|начн\w*|приступ\w*|давай\b.{0,32}\b(?:собира\w*|выполня\w*|готов\w*))\b",
        ("execution","advance"):
            r"\b(?:переход\w*|перейд\w*|перейт\w*|продолж\w*|давай\b.{0,32}\b(?:провер\w*|свер\w*))\b",
        ("validation","approve"):
            r"\b(?:заверш\w*|заканч\w*|готов\w*|выезжа\w*|подтверд\w*.{0,24}\bзаверш\w*|перейт\w*\s+на\s+следующ\w*\s+шаг)\b",
    }
    pattern=patterns.get((phase,event))
    if not pattern or not re.search(pattern,normalized):
        return False,"transition_intent_missing"
    return True,"transition_intent_explicit"


def explicit_transition_target(text: str) -> str | None:
    """Return an explicitly named lifecycle target from an ordinary user phrase.

    This is deliberately server-side: the model still chooses only an event.  A
    named target is useful even when it is unavailable, because the attempted
    jump must be visible in the audit rather than silently converted into the
    nearest legal transition.
    """
    normalized=" ".join(text.lower().replace("ё","е").split())
    # A consent sentence is handled by the atomic approval path.  It must not
    # be mistaken for a bare request to move to the named next phase.
    if re.search(r"\b(?:нравится|согласен|согласна|подтверждаю|принимаю|устраивает|подходит|все верно|проверка пройдена)\b",normalized):
        return None
    if any(mark in normalized for mark in ("?","«","»",'"')):
        return None
    if re.search(r"\b(если|если бы|может|можно ли|возможно|наверное|пожалуй|цитат|сказал|сказала)\b",normalized):
        return None
    if re.search(r"\bне\s+(?:переход\w*|перейд\w*|перейт\w*|собира\w*|выполня\w*|провер\w*|заверш\w*|заканч\w*|выезжа\w*)",normalized):
        return None
    # Completion wins over a mention of checking: "проверили, можно выезжать".
    if re.search(r"\b(?:завершить|завершаем|закончить|заканчиваем|заканчивай\w*|выезжа\w*|закры\w*\s+подготов)\b",normalized):
        return "done"
    if re.search(r"\b(?:переход\w*|перейд\w*|перейт\w*)\s+(?:к|на)\s+(?:финальн\w*\s+)?провер\w*\b|\b(?:давай|пора)\b[^.!?]{0,32}\bпровер\w*\b",normalized):
        return "validation"
    if re.search(r"\b(?:к|на)\s+выполн\w*\b|\b(?:нач(?:нем|ать)|пора|давай)\b[^.!?]{0,40}\b(?:собира\w*|выполня\w*)\b",normalized):
        return "execution"
    return None


def natural_fallback(result: dict | None, task: dict) -> str:
    if result is None:
        return "Я учёл текущее состояние задачи. Скажите, что хотите сделать дальше."
    if result["outcome"]=="rejected":
        unmet=result.get("unmet_requirements") or []
        if unmet:return "Пока перейти дальше нельзя: " + " ".join(item["message"] for item in unmet)
        return result.get("message") or "Сейчас это действие недоступно. Состояние задачи не изменилось."
    if result["action"]=="record_validation_result":
        if result.get("safe_details",{}).get("has_blockers"):
            return "Найдена блокирующая проблема. Подтверждать её не нужно: устраните проблему и сообщите, когда она исправлена."
        return "Проверка сохранена без блокирующих проблем. Теперь явно подтвердите результат, чтобы завершить подготовку."
    phase=task["phase"]
    if phase=="planning" and not task.get("planning_ready"):
        return "Черновик плана сохранён. Ответьте одним сообщением на оставшиеся критичные вопросы; необязательные детали будут оформлены как допущения."
    messages={
        "planning":"Задача и предложенный план сохранены. Посмотрите план и явно подтвердите его, когда будете готовы продолжить.",
        "execution":"Мы перешли к выполнению. Расскажите, что уже сделано, чтобы я сохранил результат.",
        "validation":"Мы перешли к проверке. Сообщите о найденных замечаниях или подтвердите, что их нет.",
        "done":"Все обязательные проверки пройдены, задача завершена.",
    }
    return messages.get(phase,"Состояние задачи обновлено.")
