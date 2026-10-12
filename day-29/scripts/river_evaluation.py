"""Validate submitted river-crossing plans. No solution search, repair or model calls."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

BANKS = ("L", "R")
EMPTY_CARGO = frozenset({"пусто", "без груза", "ничего", "empty", "none", "-", "—", "∅"})


class PlanFormatError(ValueError):
    def __init__(self, code, message, *, line=None, trip=None):
        super().__init__(message)
        self.error = {"code": code, "message": message}
        if line is not None:
            self.error["line"] = line
        if trip is not None:
            self.error["trip"] = trip


@dataclass(frozen=True)
class RiverInstance:
    id: str
    entities: tuple[str, ...]
    controller: str
    forbidden_pairs: tuple[tuple[str, str], ...]
    start_bank: str = "L"
    goal_bank: str = "R"
    boat_capacity: int = 2  # Includes the controller, who always travels in the boat.
    aliases: Mapping[str, str] = field(default_factory=dict)
    question: str = ""

    def __post_init__(self):
        entities = tuple(self.entities)
        pairs = tuple(tuple(pair) for pair in self.forbidden_pairs)
        if not entities or any(not isinstance(e, str) or not e.strip() for e in entities):
            raise ValueError("Нужны непустые имена участников.")
        if len(set(entities)) != len(entities) or not isinstance(self.controller, str) or not self.controller.strip() or self.controller in entities:
            raise ValueError("Имена участников и сопровождающего должны различаться.")
        if self.start_bank not in BANKS or self.goal_bank not in BANKS or self.start_bank == self.goal_bank:
            raise ValueError("Начальный и конечный берега должны быть L/R и различаться.")
        if isinstance(self.boat_capacity, bool) or not isinstance(self.boat_capacity, int) or self.boat_capacity < 1:
            raise ValueError("Вместимость лодки включает сопровождающего и должна быть положительной.")
        seen = set()
        for pair in pairs:
            if len(pair) != 2 or pair[0] == pair[1] or any(e not in entities for e in pair) or frozenset(pair) in seen:
                raise ValueError("Некорректная или повторная запрещённая пара.")
            seen.add(frozenset(pair))
        normalized = {}
        for alias, entity in [*((e, e) for e in entities), *self.aliases.items()]:
            if not isinstance(alias, str) or not alias.strip() or entity not in entities:
                raise ValueError("Некорректный псевдоним участника.")
            key = alias.strip().casefold()
            if key in EMPTY_CARGO or (key in normalized and normalized[key] != entity):
                raise ValueError("Псевдонимы участников конфликтуют.")
            normalized[key] = entity
        object.__setattr__(self, "entities", entities)
        object.__setattr__(self, "forbidden_pairs", pairs)
        object.__setattr__(self, "aliases", MappingProxyType(normalized))


def normalize_plan(value, instance: RiverInstance):
    """Strict normalized JSON schema. Cargo contains IDs; the controller is implicit."""
    if not isinstance(value, dict) or set(value) != {"trips"} or not isinstance(value["trips"], list):
        raise PlanFormatError("plan_schema", "Нужен объект с единственным полем trips — массивом рейсов.")
    if not value["trips"]:
        raise PlanFormatError("empty_plan", "План не содержит рейсов.")
    trips = []
    for index, trip in enumerate(value["trips"], 1):
        if not isinstance(trip, dict) or set(trip) != {"from", "to", "cargo"}:
            raise PlanFormatError("trip_schema", "Рейс должен содержать только from, to и cargo.", trip=index)
        if trip["from"] not in BANKS or trip["to"] not in BANKS:
            raise PlanFormatError("bank_schema", "Берега рейса должны быть L или R.", trip=index)
        cargo = trip["cargo"]
        if not isinstance(cargo, list) or any(not isinstance(entity, str) for entity in cargo):
            raise PlanFormatError("cargo_schema", "Груз должен быть массивом имён участников.", trip=index)
        if len(cargo) != len(set(cargo)):
            raise PlanFormatError("duplicate_cargo", "Один участник указан в грузе несколько раз.", trip=index)
        if any(entity not in instance.entities for entity in cargo):
            raise PlanFormatError("unknown_cargo", "В грузе неизвестный участник; сопровождающий не записывается как груз.", trip=index)
        trips.append({"from": trip["from"], "to": trip["to"], "cargo": list(cargo)})
    return {"trips": trips}


def parse_json_plan(text, instance: RiverInstance):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise PlanFormatError("duplicate_json_key", "В JSON повторяется одно поле.")
            result[key] = value
        return result
    def constant(_value):
        raise PlanFormatError("nonfinite_json", "NaN и Infinity не являются допустимым JSON.")
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (json.JSONDecodeError, TypeError, UnicodeError):
        raise PlanFormatError("invalid_json", "План не является одним корректным JSON объектом.") from None
    return normalize_plan(value, instance)


LINE = re.compile(r"^\s*(?:(?P<number>[1-9]\d*)[.)]\s*)?(?P<source>[LRЛП])\s*(?:->|→)\s*(?P<target>[LRЛП])\s*:\s*(?P<cargo>.+?)\s*$", re.IGNORECASE)
BANK_NAMES = {"l": "L", "r": "R", "л": "L", "п": "R"}


def parse_compact_plan(text, instance: RiverInstance):
    """Parse the entire answer, never extract a favorable subplan or discard prose."""
    if not isinstance(text, str):
        raise PlanFormatError("text_schema", "Компактный план должен быть строкой.")
    trips, numbered = [], None
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = LINE.fullmatch(line)
        if not match:
            raise PlanFormatError("line_schema", "Ожидается строка вида «1. Л→П: коза» или «L->R: goat»; дополнительные строки не отбрасываются.", line=line_number)
        number = match.group("number")
        if numbered is None:
            numbered = number is not None
        if numbered != (number is not None):
            raise PlanFormatError("mixed_numbering", "Все рейсы должны иметь последовательные номера либо быть без номеров.", line=line_number)
        if number is not None and number != str(len(trips) + 1):
            raise PlanFormatError("trip_number", "Номера рейсов должны идти подряд с единицы, без повторов.", line=line_number)
        raw_cargo = match.group("cargo").strip()
        if raw_cargo.casefold() in EMPTY_CARGO:
            cargo = []
        else:
            cargo = []
            for name in re.split(r"\s*(?:,|\+)\s*", raw_cargo):
                key = name.strip().casefold()
                if key not in instance.aliases:
                    raise PlanFormatError("unknown_cargo", "Неизвестное имя груза или лишнее пояснение в строке рейса.", line=line_number)
                cargo.append(instance.aliases[key])
        trips.append({"from": BANK_NAMES[match.group("source").casefold()],
                      "to": BANK_NAMES[match.group("target").casefold()], "cargo": cargo})
    return normalize_plan({"trips": trips}, instance)


def _state(instance, positions, in_boat=()):
    order = (instance.controller, *instance.entities)
    return {**{bank: [entity for entity in order if positions[entity] == bank] for bank in BANKS}, "boat": list(in_boat)}


def _unsafe(instance, positions):
    for bank in BANKS:
        if positions[instance.controller] == bank:
            continue
        for pair in instance.forbidden_pairs:
            if all(positions[entity] == bank for entity in pair):
                return bank, pair
    return None


def _result(valid, error_type, errors, trace, state, goal):
    return {"valid": valid, "error_type": error_type,
            "format_errors": errors if error_type == "format" else [],
            "simulation_errors": errors if error_type == "simulation" else [],
            "trace": trace, "final_state": state, "goal_reached": goal}


def evaluate_plan(value, instance: RiverInstance):
    """Simulate only submitted moves. Stop at an invalid transition without repairing it."""
    positions = {entity: instance.start_bank for entity in (instance.controller, *instance.entities)}
    trace = []
    try:
        plan = normalize_plan(value, instance)
    except PlanFormatError as exc:
        return _result(False, "format", [exc.error], trace, _state(instance, positions), False)
    for index, trip in enumerate(plan["trips"], 1):
        source, target, cargo = trip["from"], trip["to"], trip["cargo"]
        error = None
        if source == target:
            error = {"code": "same_bank", "message": "Рейс должен перемещать лодку на другой берег."}
        elif positions[instance.controller] != source:
            error = {"code": "controller_bank", "message": "Сопровождающий находится на другом берегу; телепортация запрещена."}
        elif len(cargo) + 1 > instance.boat_capacity:
            error = {"code": "boat_capacity", "message": "Лодка перегружена с учётом сопровождающего."}
        elif any(positions[entity] != source for entity in cargo):
            error = {"code": "cargo_bank", "message": "Указанный груз отсутствует на берегу отправления."}
        if error:
            return _result(False, "simulation", [{**error, "trip": index, "phase": "departure"}], trace, _state(instance, positions), False)
        before = _state(instance, positions)
        moving = [instance.controller, *cargo]
        transit_positions = positions.copy()
        for entity in moving:
            transit_positions[entity] = None
        transit = _state(instance, transit_positions, moving)
        unsafe = _unsafe(instance, transit_positions)
        if unsafe:
            bank, pair = unsafe
            error = {"code": "unsafe_bank", "message": "Запрещённая пара остаётся без сопровождающего во время рейса.",
                     "trip": index, "phase": "in_transit", "bank": bank, "pair": list(pair)}
            trace.append({"trip": index, "submitted": trip, "before": before, "in_transit": transit, "after": None})
            return _result(False, "simulation", [error], trace, before, False)
        after_positions = positions.copy()
        for entity in moving:
            after_positions[entity] = target
        after = _state(instance, after_positions)
        unsafe = _unsafe(instance, after_positions)
        trace.append({"trip": index, "submitted": trip, "before": before, "in_transit": transit, "after": after})
        if unsafe:
            bank, pair = unsafe
            error = {"code": "unsafe_bank", "message": "Запрещённая пара осталась без сопровождающего после рейса.",
                     "trip": index, "phase": "arrival", "bank": bank, "pair": list(pair)}
            return _result(False, "simulation", [error], trace, after, False)
        positions = after_positions
    goal = all(bank == instance.goal_bank for bank in positions.values())
    if not goal:
        return _result(False, "simulation", [{"code": "incomplete", "message": "План закончился до достижения цели для всех участников."}], trace, _state(instance, positions), False)
    return _result(True, None, [], trace, _state(instance, positions), True)


def evaluate_text(text, instance: RiverInstance, *, format="compact"):
    if format not in {"compact", "json"}:
        raise ValueError("format должен быть compact или json")
    try:
        plan = parse_compact_plan(text, instance) if format == "compact" else parse_json_plan(text, instance)
    except PlanFormatError as exc:
        positions = {entity: instance.start_bank for entity in (instance.controller, *instance.entities)}
        return _result(False, "format", [exc.error], [], _state(instance, positions), False)
    return evaluate_plan(plan, instance)


_ORIGINAL_ALIASES = {"волк": "wolf", "волка": "wolf", "коза": "goat", "козу": "goat", "капуста": "cabbage", "капусту": "cabbage"}
_ORIGINAL_QUESTION = "Фермер должен перевезти через реку волка, козу и капусту. Лодка вмещает фермера и только одного пассажира или предмет. Без фермера волк съест козу, а коза — капусту. Все начинают на левом берегу. Дай пошаговый план, чтобы все оказались справа. Для каждого рейса укажи направление и кого или что везёт фермер; пустые возвращения тоже перечисли."
ORIGINAL = RiverInstance("original", ("wolf", "goat", "cabbage"), "farmer", (("wolf", "goat"), ("goat", "cabbage")), aliases=_ORIGINAL_ALIASES, question=_ORIGINAL_QUESTION)
RENAMED = RiverInstance("renamed", ("fox", "chicken", "grain"), "keeper", (("fox", "chicken"), ("chicken", "grain")),
    aliases={"лиса": "fox", "лису": "fox", "курица": "chicken", "курицу": "chicken", "зерно": "grain"},
    question="Смотритель перевозит лису, курицу и зерно с левого берега на правый. Лодка вмещает смотрителя и один груз. Без смотрителя лиса съест курицу, а курица — зерно. Укажи направление и груз каждого рейса, включая пустые возвращения.")
REVERSED = RiverInstance("reversed", ("wolf", "goat", "cabbage"), "farmer", (("wolf", "goat"), ("goat", "cabbage")), start_bank="R", goal_bank="L", aliases=_ORIGINAL_ALIASES,
    question="Фермер, капуста, коза и волк находятся на правом берегу. Их нужно перевезти на левый. Лодка вмещает фермера и один груз. Без фермера волк съест козу, а коза — капусту. Укажи направление и груз каждого рейса, включая пустые возвращения.")
REORDERED = RiverInstance("reordered", ("cabbage", "wolf", "goat"), "farmer", (("goat", "cabbage"), ("wolf", "goat")), aliases=_ORIGINAL_ALIASES,
    question="На левом берегу находятся капуста, волк и коза с фермером. Всех нужно доставить направо. Коза ест капусту, волк ест козу, если фермер отсутствует. Лодкой управляет фермер; кроме него вмещается только один груз. Перечисли все рейсы: направление и груз, включая пустые возвращения.")
INSTANCES = {instance.id: instance for instance in (ORIGINAL, RENAMED, REVERSED, REORDERED)}
