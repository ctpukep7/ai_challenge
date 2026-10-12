"""Meaningful fixture behavior and rejection tests; never call a model."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location(
    "coder_evaluation", Path(__file__).resolve().parents[1] / "scripts/coder_evaluation.py"
)
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


GOOD = {
    "k01": """def normalize_usernames(names):
    seen = set()
    result = []
    for name in names:
        clean = name.strip().lower()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result
""",
    "k02": """def mean_or_none(numbers):
    return sum(numbers) / len(numbers) if numbers else None
""",
    "k03": """def merge_intervals(intervals):
    ordered = sorted(intervals, key=lambda pair: pair[0])
    result = []
    for start, end in ordered:
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result
""",
    "kh04": """def word_counts(text):
    result = {}
    for word in text.lower().split():
        result[word] = result.get(word, 0) + 1
    return result
""",
    "kh05": """def chunk_list(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]
""",
    "kh06": """def first_unique(items):
    for item in items:
        if items.count(item) == 1:
            return item
    return None
""",
    "kh07": """def prefix_sums(numbers):
    result = []
    total = 0
    for number in numbers:
        total += number
        result.append(total)
    return result
""",
}


@pytest.mark.parametrize("case_id", GOOD)
def test_real_fixture_execution(case_id):
    result = evaluator.evaluate_response("```python\n" + GOOD[case_id] + "```", case_id)
    assert result["strict_pass"], result
    assert result["markdown_fence"]
    assert len(result["tests"]) == len(evaluator.FIXTURES[case_id]["tests"])
    assert result["isolation"] == {
        "isolated_python": True, "cpu_limit_seconds": 1,
        "empty_working_directory": True, "minimal_environment": True,
    }


def test_pure_python_is_accepted_without_rewriting_it():
    result = evaluator.evaluate_response(GOOD["k01"], "k01")
    assert result["strict_pass"]
    assert not result["markdown_fence"]


@pytest.mark.parametrize("answer", [
    "Explanation\n```python\n" + GOOD["k01"] + "```",
    "```javascript\n" + GOOD["k01"] + "```",
    "```python\n" + GOOD["k01"] + "```\nExtra explanation",
])
def test_format_failure_is_separate_from_correct_behavior(answer):
    result = evaluator.evaluate_response(answer, "k01")
    assert not result["strict_pass"]
    assert not result["format_pass"]
    assert result["behavior_pass"]


@pytest.mark.parametrize("answer", [
    "", "```python\ndef normalize_usernames(names):\n return []",
    "```python\n" + GOOD["k01"] + "```\n```python\n" + GOOD["k01"] + "```",
    "def normalize_usernames(names)\n return []",
])
def test_empty_incomplete_multiple_and_invalid_syntax(answer):
    result = evaluator.evaluate_response(answer, "k01")
    assert not result["strict_pass"]
    assert not result["behavior_pass"]
    assert result["error"]


@pytest.mark.parametrize("body", [
    "import os\n    return []",
    "return open('/tmp/coder-evaluator-forbidden', 'w')",
    "return __import__('socket')",
    "return names.__class__",
    "return eval('1')",
    "return getattr(names, 'copy')()",
    "return normalize_usernames(names)",
    "return (lambda x: x)(names)",
    "return [0] * 1000000",
    "return 2 ** 1000000",
    "while True:\n        pass",
    "return globals()",
    "return names.copy",  # Bound methods cannot escape to later calls.
    "len = names\n    return []",
    "return names.append.__globals__",
])
def test_unapproved_constructs_never_start_child(body):
    answer = "def normalize_usernames(names):\n    " + body
    with patch.object(evaluator.subprocess, "run", side_effect=AssertionError("Child must not run")):
        result = evaluator.evaluate_response(answer, "k01")
    assert not result["subset_pass"]
    assert result["error"]


@pytest.mark.parametrize("answer", [
    "def wrong_name(names):\n return []",
    "def normalize_usernames(names, more=None):\n return []",
    "def normalize_usernames(names: list):\n return []",
    "def normalize_usernames(names):\n return []\ndef another():\n return 1",
])
def test_wrong_signature_or_additional_functions(answer):
    result = evaluator.evaluate_response(answer, "k01")
    assert result["syntax_pass"]
    assert not result["subset_pass"]


def test_source_limit_prevents_child_execution():
    result = evaluator.evaluate_response("#" + "x" * evaluator.MAX_SOURCE, "k01")
    assert "source-size" in result["error"]
    assert not result["subset_pass"]


@pytest.mark.parametrize("case_id,source,test_name", [
    ("k02", "def mean_or_none(numbers):\n return sum(numbers) // len(numbers) if numbers else None", "fraction"),
    ("k02", "def mean_or_none(numbers):\n return sum(numbers) / len(numbers)", "empty"),
    ("k01", "def normalize_usernames(names):\n return sorted(set(name.strip().lower() for name in names if name.strip()))", "whitespace_duplicates_order"),
    ("k03", GOOD["k03"].replace("start <=", "start <"), "unsorted_touching_nested"),
    ("k03", GOOD["k03"].replace("result.append((start, end))", "result.append([start, end])"), "singleton_zero_length"),
    ("kh04", GOOD["kh04"].replace("text.lower().split()", "text.lower().strip('!,').split()"), "case_whitespace_punctuation"),
    ("kh05", "def chunk_list(items, size):\n return [items[i:i + size] for i in range(0, len(items) - size + 1, size)]", "remainder"),
])
def test_real_algorithm_failures_on_edge_cases(case_id, source, test_name):
    result = evaluator.evaluate_response(source, case_id)
    assert result["syntax_pass"] and result["subset_pass"]
    assert not result["behavior_pass"], result
    assert not next(row for row in result["tests"] if row["name"] == test_name)["pass"]


def test_input_mutation_is_independent_of_return_value():
    source = GOOD["k01"].replace("return result", "names.append('added')\n    return result")
    result = evaluator.evaluate_response(source, "k01")
    assert all(row["value_matches"] for row in result["tests"])
    assert not any(row["input_unchanged"] for row in result["tests"])
    assert not result["strict_pass"]


def test_interval_input_sort_is_mutation_even_with_correct_output():
    source = GOOD["k03"].replace("ordered = sorted(intervals, key=lambda pair: pair[0])", "intervals.sort(key=lambda pair: pair[0])\n    ordered = intervals")
    result = evaluator.evaluate_response(source, "k03")
    assert all(row["value_matches"] for row in result["tests"])
    assert any(not row["input_unchanged"] for row in result["tests"])
    assert not result["strict_pass"]


def test_new_list_requirement_rejects_alias_on_empty_input():
    source = GOOD["k01"].replace("seen = set()", "if not names:\n        return names\n    seen = set()")
    result = evaluator.evaluate_response(source, "k01")
    empty = result["tests"][0]
    assert empty["value_matches"] and empty["input_unchanged"]
    assert not empty["new_output"]
    assert not result["strict_pass"]


def test_augmented_dictionary_assignment_is_supported():
    source = """def word_counts(text):
    result = {}
    for word in text.lower().split():
        if word not in result:
            result[word] = 0
        result[word] += 1
    return result
"""
    assert evaluator.evaluate_response(source, "kh04")["strict_pass"]


def test_runtime_type_check_blocks_method_on_wrong_type():
    result = evaluator.evaluate_response("def normalize_usernames(names):\n return names.lower()", "k01")
    assert result["subset_pass"]
    assert all("Method is not allowed" in row["error"] for row in result["tests"])


def test_isspace_is_supported_only_on_exact_strings():
    source = GOOD["kh04"].replace("result = {}", "if not text or text.isspace():\n        return {}\n    result = {}")
    assert evaluator.evaluate_response(source, "kh04")["strict_pass"]
    result = evaluator.evaluate_response("def normalize_usernames(names):\n return names.isspace()", "k01")
    assert result["subset_pass"]
    assert all("Method is not allowed" in row["error"] for row in result["tests"])


def test_first_unique_must_use_global_counts_and_first_input_order():
    source = "def first_unique(items):\n return min(items) if items else None"
    result = evaluator.evaluate_response(source, "kh06")
    assert not result["behavior_pass"]
    assert not next(row for row in result["tests"] if row["name"] == "all_repeated")["pass"]
    assert not next(row for row in result["tests"] if row["name"] == "first_by_input_order")["pass"]


def test_first_unique_input_sort_is_rejected_as_mutation():
    source = GOOD["kh06"].replace("for item in items:", "items.sort()\n    for item in items:")
    result = evaluator.evaluate_response(source, "kh06")
    assert any(not row["input_unchanged"] for row in result["tests"])
    assert not result["strict_pass"]


def test_prefix_sums_needs_new_list_and_preserves_integer_types():
    same_list = "def prefix_sums(numbers):\n return numbers"
    result = evaluator.evaluate_response(same_list, "kh07")
    assert not result["tests"][0]["new_output"]
    assert not result["strict_pass"]
    floats = GOOD["kh07"].replace("total = 0", "total = 0.0")
    result = evaluator.evaluate_response(floats, "kh07")
    assert not next(row for row in result["tests"] if row["name"] == "positive")["value_matches"]


def test_prefix_sums_detects_in_place_mutation_despite_matching_values():
    source = """def prefix_sums(numbers):
    for i in range(1, len(numbers)):
        numbers[i] += numbers[i - 1]
    return numbers.copy()
"""
    result = evaluator.evaluate_response(source, "kh07")
    assert all(row["value_matches"] for row in result["tests"])
    assert any(not row["input_unchanged"] for row in result["tests"])
    assert not result["strict_pass"]


@pytest.mark.parametrize("body", [
    "return list(range(1025))",
    "result = []\n    for i in range(513):\n        result.append(i)\n    return result",
    "return [i for i in range(513)]",
    "result = [0]\n    for i in range(20):\n        result = result + result\n    return result",
])
def test_container_and_iteration_limits_are_enforced(body):
    result = evaluator.evaluate_response("def normalize_usernames(names):\n    " + body, "k01")
    assert result["subset_pass"]
    assert not result["behavior_pass"]
    assert all("exceeds limit" in row["error"] for row in result["tests"])


def test_real_wall_timeout_terminates_expensive_bounded_loops():
    source = """def normalize_usernames(names):
    for a in range(1024):
        for b in range(1024):
            for c in range(1024):
                pass
    return []
"""
    result = evaluator.evaluate_response(source, "k01", timeout_seconds=0.2)
    assert result["timed_out"]
    assert not result["behavior_pass"]


def test_child_uses_minimal_env_temp_cwd_and_isolated_python():
    actual_run = evaluator.subprocess.run
    calls = []

    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return actual_run(*args, **kwargs)

    with patch.object(evaluator.subprocess, "run", side_effect=capture):
        assert evaluator.evaluate_response(GOOD["k02"], "k02")["strict_pass"]
    command = calls[0][0][0]
    options = calls[0][1]
    assert "-I" in command and "-B" in command
    assert options["env"] == {"LANG": "C", "LC_ALL": "C"}
    assert options["cwd"] != str(Path.cwd())
    assert not Path(options["cwd"]).exists()
    assert json.loads(options["input"])["case_id"] == "k02"


@pytest.mark.parametrize("case_id,timeout", [("unknown", 2), ("k01", 0), ("k01", 6)])
def test_invalid_runner_arguments(case_id, timeout):
    with pytest.raises(ValueError):
        evaluator.evaluate_response(GOOD["k01"], case_id, timeout_seconds=timeout)
