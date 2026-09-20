"""Pure deterministic invariant checks for structured candidates."""

from __future__ import annotations


OPERATORS = frozenset({"equals", "not_equals", "one_of", "not_one_of"})


def _normalized(value: str) -> str:
    return value.strip().casefold()


def _expected_values(invariant: dict) -> list[str]:
    if invariant["operator"] in ("one_of", "not_one_of"):
        return [_normalized(value) for value in invariant["expected_value"].split("|") if value.strip()]
    return [_normalized(invariant["expected_value"])]


def _violates(value: str, operator: str, expected: list[str]) -> bool:
    if operator == "equals":
        return value != expected[0]
    if operator == "not_equals":
        return value == expected[0]
    if operator == "one_of":
        return value not in expected
    if operator == "not_one_of":
        return value in expected
    raise ValueError(f"Неизвестный оператор инварианта: {operator}")


def check_candidate(candidate: dict, invariants: list[dict]) -> dict:
    """Return a serializable result without hidden reasoning or model calls."""
    candidate_value = _normalized(candidate["value"])
    applicable = [
        invariant
        for invariant in invariants
        if invariant["active"] and invariant["field"] == candidate["field"]
    ]
    violations = []
    for invariant in applicable:
        expected = _expected_values(invariant)
        if not _violates(candidate_value, invariant["operator"], expected):
            continue
        violations.append(
            {
                "invariant_id": invariant["id"],
                "description": invariant["description"],
                "reason_code": f"invariant_{invariant['operator']}",
                "reason": invariant["reason"],
                "compatible_alternative": {
                    "field": invariant["field"],
                    "value": invariant["compatible_alternative"],
                },
            }
        )
    return {
        "allowed": not violations,
        "candidate": candidate,
        "checked_invariant_ids": [invariant["id"] for invariant in applicable],
        "violations": violations,
    }
