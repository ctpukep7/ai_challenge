import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("river_evaluation", Path(__file__).resolve().parents[1] / "scripts/river_evaluation.py")
river = importlib.util.module_from_spec(spec)
# Dataclasses inspect the defining module during class creation.
import sys
sys.modules[spec.name] = river
spec.loader.exec_module(river)


def trip(source, target, *cargo):
    return {"from": source, "to": target, "cargo": list(cargo)}


# Complete examples live only in tests; the evaluator has no solution generator.
VALID = {"trips": [trip("L", "R", "goat"), trip("R", "L"), trip("L", "R", "wolf"),
                   trip("R", "L", "goat"), trip("L", "R", "cabbage"), trip("R", "L"), trip("L", "R", "goat")]}
COMPACT = "1. Л→П: коза\n2. П→Л: пусто\n3. Л→П: волк\n4. П→Л: коза\n5. Л→П: капуста\n6. П→Л: пусто\n7. Л→П: коза"


def test_valid_seven_trips_full_departure_transit_arrival_trace_and_goal():
    result = river.evaluate_plan(VALID, river.ORIGINAL)
    assert result["valid"] is True and result["goal_reached"] is True
    assert result["error_type"] is None and result["format_errors"] == result["simulation_errors"] == []
    assert len(result["trace"]) == 7
    assert result["final_state"] == {"L": [], "R": ["farmer", "wolf", "goat", "cabbage"], "boat": []}
    first = result["trace"][0]
    assert first["before"] == {"L": ["farmer", "wolf", "goat", "cabbage"], "R": [], "boat": []}
    assert first["in_transit"] == {"L": ["wolf", "cabbage"], "R": [], "boat": ["farmer", "goat"]}
    assert first["after"] == {"L": ["wolf", "cabbage"], "R": ["farmer", "goat"], "boat": []}
    assert VALID["trips"][0] == trip("L", "R", "goat")  # Submitted plan is untouched.


def test_illegal_empty_fourth_return_detected_while_controller_in_boat():
    wrong = {"trips": [*VALID["trips"][:3], trip("R", "L"), *VALID["trips"][4:]]}
    result = river.evaluate_plan(wrong, river.ORIGINAL)
    assert result["valid"] is False and result["error_type"] == "simulation"
    error = result["simulation_errors"][0]
    assert error["code"] == "unsafe_bank" and error["trip"] == 4 and error["phase"] == "in_transit"
    assert error["bank"] == "R" and error["pair"] == ["wolf", "goat"]
    assert result["trace"][-1]["after"] is None
    assert result["trace"][-1]["in_transit"]["boat"] == ["farmer"]
    assert result["trace"][-1]["in_transit"]["R"] == ["wolf", "goat"]
    assert result["final_state"] == result["trace"][-1]["before"]  # Invalid move is not repaired or committed.


@pytest.mark.parametrize("plan,code,at", [
    ({"trips": [trip("L", "L", "goat")]}, "same_bank", 1),
    ({"trips": [trip("R", "L", "goat")]}, "controller_bank", 1),
    ({"trips": [trip("L", "R", "goat"), trip("L", "R", "wolf")]}, "controller_bank", 2),
    ({"trips": [trip("L", "R", "goat"), trip("R", "L", "wolf")]}, "cargo_bank", 2),
    ({"trips": [trip("L", "R", "goat", "wolf")]}, "boat_capacity", 1),
    ({"trips": [trip("L", "R", "wolf")]}, "unsafe_bank", 1),
    ({"trips": [trip("L", "R", "goat")]}, "incomplete", None),
])
def test_invalid_transitions_never_teleport_overload_or_complete_missing_moves(plan, code, at):
    result = river.evaluate_plan(plan, river.ORIGINAL)
    assert result["valid"] is False and result["error_type"] == "simulation"
    error = result["simulation_errors"][0]
    assert error["code"] == code
    if at is not None:
        assert error["trip"] == at
    assert not result["goal_reached"]


@pytest.mark.parametrize("plan,code", [
    ([], "plan_schema"), ({"trips": []}, "empty_plan"),
    ({"trips": VALID["trips"], "alternative": VALID["trips"]}, "plan_schema"),
    ({"trips": [{"from": "L", "to": "R", "cargo": ["goat"], "reason": "safe"}]}, "trip_schema"),
    ({"trips": [trip("left", "R", "goat")]}, "bank_schema"),
    ({"trips": [{"from": "L", "to": "R", "cargo": "goat"}]}, "cargo_schema"),
    ({"trips": [trip("L", "R", "goat", "goat")]}, "duplicate_cargo"),
    ({"trips": [trip("L", "R", "fox")]}, "unknown_cargo"),
    ({"trips": [trip("L", "R", "farmer")]}, "unknown_cargo"),
    ({"trips": [trip("L", "R", 0)]}, "cargo_schema"),
])
def test_json_schema_errors_separate_from_logic_and_no_discarded_extra_fields(plan, code):
    result = river.evaluate_plan(plan, river.ORIGINAL)
    assert result["valid"] is False and result["error_type"] == "format"
    assert result["format_errors"][0]["code"] == code
    assert result["simulation_errors"] == [] and result["trace"] == []


def test_compact_russian_numbered_english_unnumbered_and_aliases():
    assert river.evaluate_text(COMPACT, river.ORIGINAL)["valid"] is True
    english = "L->R: goat\nR->L: empty\nL->R: wolf\nR->L: goat\nL->R: cabbage\nR->L: none\nL->R: goat"
    assert river.evaluate_text(english, river.ORIGINAL)["valid"] is True
    russian_accusative = COMPACT.replace("коза", "козу").replace("волк", "волка").replace("капуста", "капусту")
    assert river.evaluate_text(russian_accusative, river.ORIGINAL)["valid"] is True
    assert river.evaluate_text("\n" + COMPACT + "\n\n", river.ORIGINAL)["valid"] is True


@pytest.mark.parametrize("text,code", [
    ("План:\n" + COMPACT, "line_schema"),
    (COMPACT + "\nНо четвёртый рейс пусть будет пустым.", "line_schema"),
    (COMPACT + "\n" + COMPACT, "trip_number"),
    (COMPACT.replace("4. П→Л: коза", "4. П→Л: пусто") + "\nИсправленный план:\n" + COMPACT, "line_schema"),
    (COMPACT.replace("4.", "3."), "trip_number"),
    (COMPACT.replace("1.", "2."), "trip_number"),
    (COMPACT.replace("2. П→Л", "П→Л"), "mixed_numbering"),
    (COMPACT.replace("коза", "дракон", 1), "unknown_cargo"),
    (COMPACT.replace("коза", "коза, коза", 1), "duplicate_cargo"),
    (COMPACT.replace("коза", "коза (первый рейс)", 1), "unknown_cargo"),
    (COMPACT.replace("коза", "коза, пусто", 1), "unknown_cargo"),
])
def test_strict_compact_parser_does_not_extract_successful_subplan_or_ignore_contradictions(text, code):
    result = river.evaluate_text(text, river.ORIGINAL)
    assert result["valid"] is False and result["error_type"] == "format"
    assert result["format_errors"][0]["code"] == code
    assert result["simulation_errors"] == []  # Header alone is not evidence of bad logic.


def test_valid_prefix_followed_by_conflicting_trip_is_not_accepted_at_first_goal():
    result = river.evaluate_plan({"trips": [*VALID["trips"], trip("R", "L")]}, river.ORIGINAL)
    assert result["valid"] is False and result["simulation_errors"][0]["trip"] == 8
    assert result["simulation_errors"][0]["code"] == "unsafe_bank"


def test_nonminimal_but_fully_valid_plan_is_allowed_without_searching_for_optimality():
    result = river.evaluate_plan({"trips": [*VALID["trips"], trip("R", "L", "goat"), trip("L", "R", "goat")]}, river.ORIGINAL)
    assert result["valid"] is True and len(result["trace"]) == 9


def test_fixture_renaming_reversal_and_entity_order_are_generic():
    assert set(river.INSTANCES) == {"original", "renamed", "reversed", "reordered"}
    assert all(instance.question and "1. Л→П: коза" not in instance.question for instance in river.INSTANCES.values())
    renamed = "L->R: chicken\nR->L: empty\nL->R: fox\nR->L: chicken\nL->R: grain\nR->L: empty\nL->R: chicken"
    assert river.evaluate_text(renamed, river.RENAMED)["valid"] is True
    assert river.evaluate_text(COMPACT, river.RENAMED)["error_type"] == "format"
    reversed_plan = {"trips": [trip(t["to"], t["from"], *t["cargo"]) for t in VALID["trips"]]}
    reverse = river.evaluate_plan(reversed_plan, river.REVERSED)
    assert reverse["valid"] is True and reverse["final_state"]["R"] == []
    assert river.evaluate_plan(VALID, river.REVERSED)["simulation_errors"][0]["code"] == "controller_bank"
    assert river.evaluate_plan(VALID, river.REORDERED)["valid"] is True


def test_arbitrary_entities_pairs_and_capacity_including_controller():
    instance = river.RiverInstance("custom", ("A", "B"), "pilot", (), boat_capacity=3, aliases={"alpha": "A", "beta": "B"})
    plan = river.parse_compact_plan("L->R: alpha + beta", instance)
    assert river.evaluate_plan(plan, instance)["valid"] is True
    small = river.RiverInstance("small", ("A", "B"), "pilot", (), boat_capacity=2)
    assert river.evaluate_plan(plan, small)["simulation_errors"][0]["code"] == "boat_capacity"
    alone = river.RiverInstance("alone", ("A",), "pilot", (), boat_capacity=1)
    assert river.evaluate_plan({"trips": [trip("L", "R", "A")]}, alone)["simulation_errors"][0]["code"] == "boat_capacity"


def test_json_parser_consumes_one_complete_object_and_rejects_duplicate_keys():
    assert river.evaluate_text(json.dumps(VALID), river.ORIGINAL, format="json")["valid"] is True
    for text, code in [(json.dumps(VALID) + json.dumps(VALID), "invalid_json"),
                       ('{"trips": [], "trips": []}', "duplicate_json_key"),
                       ('{"trips": NaN}', "nonfinite_json"),
                       ('```json\n' + json.dumps(VALID) + '\n```', "invalid_json")]:
        result = river.evaluate_text(text, river.ORIGINAL, format="json")
        assert result["error_type"] == "format" and result["format_errors"][0]["code"] == code


@pytest.mark.parametrize("kwargs", [
    {"entities": ("A", "A")}, {"controller": "A"}, {"boat_capacity": True}, {"boat_capacity": 0},
    {"start_bank": "R", "goal_bank": "R"}, {"forbidden_pairs": (("A", "missing"),)},
    {"forbidden_pairs": (("A", "A"),)}, {"forbidden_pairs": (("A", "B"), ("B", "A"))},
    {"aliases": {"empty": "A"}}, {"aliases": {"a": "B"}},
])
def test_invalid_instances_rejected(kwargs):
    arguments = {"id": "bad", "entities": ("A", "B"), "controller": "pilot", "forbidden_pairs": ()}
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        river.RiverInstance(**arguments)
