"""Closed synthetic Python fixtures, evaluated outside the web application.

This is deliberately a small Python subset, NOT a sandbox for arbitrary code.
AST checks, bounded operations and an isolated subprocess reduce the exposure
of these small fixtures; they do not provide OS-level security isolation.
Generated functions never receive modules, files, network handles or secrets.
"""

import ast
import copy
import json
import math
import operator
import re
import subprocess
import sys
import tempfile
from pathlib import Path


MAX_SOURCE = 16_384
MAX_NODES = 1500
MAX_ITEMS = 512
MAX_STEPS = 1024
MAX_STRING = 8192
MAX_NUMBER = 1_000_000_000
LIMITATION = "Closed synthetic fixtures only; not a complete arbitrary-code sandbox."

# Expected values are evaluator data only, never part of the model request.
FIXTURES = {
    "k01": {"name": "normalize_usernames", "args": ["names"], "tests": [
        ("empty", [[]], []),
        ("whitespace_duplicates_order", [["  BOB ", "Alice", "bob", "", " \t", "alice", "Cara"]], ["bob", "alice", "cara"]),
        ("unicode_and_internal_spaces", [[" ЁЖ ", "ёж", "A B", "a b", "Straße", "STRASSE"]], ["ёж", "a b", "straße", "strasse"]),
        ("single_and_tabs", [["\tSolo\n"]], ["solo"]),
    ]},
    "k02": {"name": "mean_or_none", "args": ["numbers"], "tests": [
        ("empty", [[]], None), ("fraction", [[1, 2]], 1.5),
        ("negative_and_float", [[-2, 4.5]], 1.25),
        ("zero", [[0, 0, 0]], 0.0), ("singleton", [[-3.5]], -3.5),
    ]},
    "k03": {"name": "merge_intervals", "args": ["intervals"], "tests": [
        ("empty", [[]], []),
        ("unsorted_touching_nested", [[(5, 7), (1, 3), (3, 6), (2, 2)]], [(1, 7)]),
        ("separate_negative", [[(8, 9), (-4, -2), (0, 1), (-3, -1)]], [(-4, -1), (0, 1), (8, 9)]),
        ("singleton_zero_length", [[(2, 2)]], [(2, 2)]),
        ("mutable_pairs_preserved", [[[5, 6], [1, 2], [2, 4]]], [(1, 4), (5, 6)]),
    ]},
    "kh04": {"name": "word_counts", "args": ["text"], "tests": [
        ("empty", [""], {}), ("whitespace", [" \n\t "], {}),
        ("case_whitespace_punctuation", ["Hi hi\tHI!\nhi!"], {"hi": 2, "hi!": 2}),
        ("unicode", ["Ёж ёж ЁЖ, слово"], {"ёж": 2, "ёж,": 1, "слово": 1}),
    ]},
    "kh05": {"name": "chunk_list", "args": ["items", "size"], "tests": [
        ("empty", [[], 2], []),
        ("remainder", [[1, 2, 3, 4, 5], 2], [[1, 2], [3, 4], [5]]),
        ("exact", [[1, 2, 3, 4], 2], [[1, 2], [3, 4]]),
        ("size_one", [["a", None, {"x": 2}], 1], [["a"], [None], [{"x": 2}]]),
        ("larger_than_input", [[1, 2], 5], [[1, 2]]),
    ]},
    "kh06": {"name": "first_unique", "args": ["items"], "tests": [
        ("empty", [[]], None),
        ("all_repeated", [[2, 3, 2, 3]], None),
        ("first_by_input_order", [[5, 1, 5, 2, 1, 3]], 2),
        ("unique_at_end", [[8, 8, 4, 9, 4]], 9),
        ("negative_and_zero", [[0, -2, 0, 3]], -2),
        ("singleton", [[-7]], -7),
    ]},
    "kh07": {"name": "prefix_sums", "args": ["numbers"], "tests": [
        ("empty", [[]], []),
        ("positive", [[1, 2, 3, 4]], [1, 3, 6, 10]),
        ("negative_and_zero", [[3, -1, 0, 4]], [3, 2, 2, 6]),
        ("zeros", [[0, 0]], [0, 0]),
        ("singleton", [[-7]], [-7]),
    ]},
}

BUILTIN_NAMES = {
    "len", "range", "enumerate", "zip", "sorted", "sum", "min", "max",
    "abs", "round", "bool", "int", "float", "str", "list", "dict", "set",
    "tuple", "any", "all",
}
METHODS = {
    str: {"strip", "lower", "split", "isspace"},
    list: {"append", "extend", "sort", "copy", "pop", "count"},
    set: {"add", "copy"},
    dict: {"get", "items", "keys", "values", "copy"},
}
METHOD_NAMES = set().union(*METHODS.values())
ALLOWED_NODES = {
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
    ast.AugAssign, ast.For, ast.If, ast.Expr, ast.Pass, ast.Break, ast.Continue,
    ast.Name, ast.Constant, ast.Load, ast.Store, ast.List, ast.Tuple, ast.Set,
    ast.Dict, ast.Subscript, ast.Slice, ast.BinOp, ast.UnaryOp, ast.BoolOp,
    ast.Compare, ast.IfExp, ast.Call, ast.Attribute, ast.keyword, ast.Lambda,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
    ast.Add, ast.Sub, ast.Div, ast.FloorDiv, ast.Mod, ast.USub, ast.UAdd, ast.Not,
    ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.Is, ast.IsNot,
}


class SubsetError(ValueError):
    """Outside the accepted subset, not proof that the algorithm is wrong."""


def parse_response(answer):
    """Accept pure Python or one Python fence, with no surrounding prose."""
    if not isinstance(answer, str) or not answer.strip():
        raise SubsetError("Empty response")
    if len(answer.encode("utf-8")) > MAX_SOURCE:
        raise SubsetError("Response exceeds source-size limit")
    fences = list(re.finditer(r"```([^\n]*)\n(.*?)```", answer, re.S))
    if not fences:
        return answer.strip(), True, "Pure Python without a Markdown fence"
    if len(fences) != 1 or answer.count("```") != 2:
        raise SubsetError("Expected exactly one complete code fence")
    fence = fences[0]
    outside = answer[:fence.start()].strip() + answer[fence.end():].strip()
    valid_format = fence.group(1).strip().lower() == "python" and not outside
    return fence.group(2), valid_format, "" if valid_format else "Fence label or extra prose violates format"


def validate_source(source, fixture):
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise SubsetError("AST exceeds node limit")
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise SubsetError("Exactly one top-level function is required")
    function = tree.body[0]
    if function.name != fixture["name"]:
        raise SubsetError("Function name differs from requested signature")
    arguments = function.args
    if ([a.arg for a in arguments.args] != fixture["args"] or arguments.posonlyargs
            or arguments.kwonlyargs or arguments.defaults or arguments.kw_defaults
            or arguments.vararg or arguments.kwarg or function.decorator_list):
        raise SubsetError("Function signature differs from requested signature")
    for node in nodes:
        if type(node) not in ALLOWED_NODES:
            raise SubsetError("Unsupported AST node: " + type(node).__name__)
        if isinstance(node, ast.FunctionDef) and node is not function:
            raise SubsetError("Nested functions are not accepted")
        if isinstance(node, ast.Name):
            if "__" in node.id or node.id.startswith("_guard_"):
                raise SubsetError("Dunder/reserved identifiers are forbidden")
            if isinstance(node.ctx, ast.Store) and node.id in BUILTIN_NAMES:
                raise SubsetError("Builtin shadowing is forbidden")
        if isinstance(node, ast.arg) and ("__" in node.arg or node.arg.startswith("_guard_") or node.arg in BUILTIN_NAMES):
            raise SubsetError("Reserved argument name")
        if isinstance(node, ast.Constant):
            value = node.value
            if type(value) not in (str, int, float, bool, type(None)):
                raise SubsetError("Unsupported literal type")
            if isinstance(value, str) and (len(value) > 256 or "__" in value):
                raise SubsetError("Oversized/dunder string literal")
            if type(value) in (int, float) and (not math.isfinite(value) or abs(value) > 1_000_000):
                raise SubsetError("Numeric literal exceeds limit")
        if isinstance(node, ast.Attribute):
            if node.attr not in METHOD_NAMES or not isinstance(node.ctx, ast.Load):
                raise SubsetError("Only approved bound methods are accepted")
            # Retrieving a method for later use is not supported.
            if not any(isinstance(p, ast.Call) and p.func is node for p in nodes):
                raise SubsetError("Method must be called directly")
        if isinstance(node, ast.Call):
            if not ((isinstance(node.func, ast.Name) and node.func.id in BUILTIN_NAMES)
                    or (isinstance(node.func, ast.Attribute) and node.func.attr in METHOD_NAMES)):
                raise SubsetError("Unapproved call, including recursion")
            if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                raise SubsetError("Expanded call arguments are forbidden")
        if isinstance(node, ast.Lambda):
            if not any(isinstance(p, ast.keyword) and p.arg == "key" and p.value is node for p in nodes):
                raise SubsetError("Lambda is only accepted as a sorting key")
            if node.args.vararg or node.args.kwarg or node.args.defaults or node.args.kwonlyargs or node.args.posonlyargs:
                raise SubsetError("Only simple sorting-key lambdas are accepted")
        if isinstance(node, ast.comprehension) and node.is_async:
            raise SubsetError("Async comprehensions are forbidden")
    # Annotations can name unavailable types or evaluate at function definition.
    if function.returns is not None or any(a.annotation is not None for a in arguments.args):
        raise SubsetError("Annotations are outside the closed subset")
    return tree


def _guard_value(value, depth=0, budget=None):
    if budget is None:
        budget = [4096]
    budget[0] -= 1
    if depth > 16 or budget[0] < 0:
        raise SubsetError("Value nesting/size exceeds limit")
    kind = type(value)
    if kind in (type(None), bool):
        return value
    if kind in (int, float):
        if not math.isfinite(value) or abs(value) > MAX_NUMBER:
            raise SubsetError("Numeric result exceeds limit")
    elif kind is str:
        if len(value) > MAX_STRING:
            raise SubsetError("String exceeds limit")
    elif kind in (list, tuple, set, dict):
        if len(value) > MAX_ITEMS:
            raise SubsetError("Container exceeds limit")
        for item in (value.items() if kind is dict else value):
            _guard_value(item, depth + 1, budget)
    else:
        raise SubsetError("Unsupported runtime value type")
    return value


def _guard_iter(value):
    for count, item in enumerate(value):
        if count >= MAX_STEPS:
            raise SubsetError("Iteration exceeds limit")
        yield _guard_value(item)


def _guard_method(value, name, *args, **kwargs):
    kind = type(value)
    if name not in METHODS.get(kind, set()):
        raise SubsetError("Method is not allowed for this exact builtin type")
    _guard_value(value)
    if name in ("append", "add") and len(value) >= MAX_ITEMS:
        raise SubsetError("Container exceeds limit")
    if name == "extend":
        args = (list(_guard_iter(args[0])),)
        if len(value) + len(args[0]) > MAX_ITEMS:
            raise SubsetError("Container exceeds limit")
    if name in ("items", "keys", "values"):
        return list(getattr(value, name)())
    result = getattr(value, name)(*args, **kwargs)
    _guard_value(value)
    return _guard_value(result)


def _guard_builtin(name, *args, **kwargs):
    if name == "range":
        value = range(*args, **kwargs)
        if len(value) > MAX_STEPS:
            raise SubsetError("Range exceeds limit")
        return value
    if name == "enumerate":
        return enumerate(_guard_iter(args[0]), *args[1:], **kwargs)
    if name == "zip":
        return zip(*(_guard_iter(a) for a in args), **kwargs)
    if name in ("list", "tuple", "set", "dict"):
        builtin = {"list": list, "tuple": tuple, "set": set, "dict": dict}[name]
        if name == "dict" and args and type(args[0]) is dict:
            result = dict(args[0], **kwargs)
        else:
            result = builtin(*((_guard_iter(args[0]),) if args else ()), **kwargs)
        return _guard_value(result)
    if name in ("sorted", "sum", "any", "all"):
        args = (_guard_iter(args[0]), *args[1:])
    elif name in ("min", "max") and len(args) == 1:
        args = (_guard_iter(args[0]),)
    builtin = {"len": len, "sorted": sorted, "sum": sum, "min": min, "max": max,
               "abs": abs, "round": round, "bool": bool, "int": int,
               "float": float, "str": str, "any": any, "all": all}[name]
    return _guard_value(builtin(*args, **kwargs))


_OPERATORS = {"Add": operator.add, "Sub": operator.sub, "Div": operator.truediv,
              "FloorDiv": operator.floordiv, "Mod": operator.mod}


def _guard_binary(name, left, right):
    _guard_value(left)
    _guard_value(right)
    if name == "Mod" and (type(left) not in (int, float) or type(right) not in (int, float)):
        raise SubsetError("Only numeric modulo is accepted")
    if name == "Add" and type(left) in (list, tuple, str) and type(left) is type(right):
        limit = MAX_STRING if type(left) is str else MAX_ITEMS
        if len(left) + len(right) > limit:
            raise SubsetError("Concatenation exceeds limit")
    return _guard_value(_OPERATORS[name](left, right))


def _call(name, *args, keywords=()):
    return ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=list(args), keywords=list(keywords))


class _Bounds(ast.NodeTransformer):
    """Bound operations; never repairs algorithms or changes fixture answers."""

    def visit_Call(self, node):
        node = self.generic_visit(node)
        if isinstance(node.func, ast.Attribute):
            return ast.copy_location(_call("_guard_method", node.func.value, ast.Constant(node.func.attr),
                                           *node.args, keywords=node.keywords), node)
        return ast.copy_location(_call("_guard_builtin", ast.Constant(node.func.id), *node.args,
                                       keywords=node.keywords), node)

    def visit_BinOp(self, node):
        node = self.generic_visit(node)
        return ast.copy_location(_call("_guard_binary", ast.Constant(type(node.op).__name__), node.left, node.right), node)

    def visit_Assign(self, node):
        node = self.generic_visit(node)
        node.value = _call("_guard_value", node.value)
        return node

    def visit_AugAssign(self, node):
        target = self.visit(node.target)
        loaded = copy.deepcopy(target)
        loaded.ctx = ast.Load()
        value = _call("_guard_binary", ast.Constant(type(node.op).__name__), loaded, self.visit(node.value))
        return ast.copy_location(ast.Assign(targets=[target], value=value), node)

    def visit_For(self, node):
        node = self.generic_visit(node)
        node.iter = _call("_guard_iter", node.iter)
        return node

    def visit_comprehension(self, node):
        node = self.generic_visit(node)
        node.iter = _call("_guard_iter", node.iter)
        return node

    def visit_ListComp(self, node):
        node = self.generic_visit(node)
        generator = ast.GeneratorExp(elt=node.elt, generators=node.generators)
        return ast.copy_location(_call("_guard_builtin", ast.Constant("list"), generator), node)

    def visit_SetComp(self, node):
        node = self.generic_visit(node)
        generator = ast.GeneratorExp(elt=node.elt, generators=node.generators)
        return ast.copy_location(_call("_guard_builtin", ast.Constant("set"), generator), node)

    def visit_DictComp(self, node):
        node = self.generic_visit(node)
        generator = ast.GeneratorExp(elt=ast.Tuple(elts=[node.key, node.value], ctx=ast.Load()), generators=node.generators)
        return ast.copy_location(_call("_guard_builtin", ast.Constant("dict"), generator), node)


def _typed_equal(actual, expected, *, allow_numeric_types=False):
    # Numeric means may be integral floats; bool is never accepted as a number.
    if allow_numeric_types and type(expected) in (int, float):
        return type(actual) in (int, float) and actual == expected
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, (list, tuple)):
        return len(actual) == len(expected) and all(_typed_equal(a, e, allow_numeric_types=allow_numeric_types) for a, e in zip(actual, expected))
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_typed_equal(actual[k], v, allow_numeric_types=allow_numeric_types) for k, v in expected.items())
    return actual == expected


def _child(payload):
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (1, 2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    fixture = FIXTURES[payload["case_id"]]
    tree = validate_source(payload["source"], fixture)
    tree = ast.fix_missing_locations(_Bounds().visit(tree))
    scope = {"__builtins__": {}, "_guard_value": _guard_value, "_guard_iter": _guard_iter,
             "_guard_builtin": _guard_builtin, "_guard_method": _guard_method,
             "_guard_binary": _guard_binary}
    exec(compile(tree, "<synthetic-candidate>", "exec"), scope)
    function = scope[fixture["name"]]
    results = []
    for label, original_args, expected in fixture["tests"]:
        arguments = copy.deepcopy(original_args)
        try:
            actual = function(*arguments)
            _guard_value(actual)
            unchanged = _typed_equal(arguments, original_args)
            new_output = not (payload["case_id"] in ("k01", "k03", "kh05", "kh07") and actual is arguments[0])
            value_matches = _typed_equal(actual, expected, allow_numeric_types=payload["case_id"] == "k02")
            results.append({"name": label, "value_matches": value_matches,
                            "input_unchanged": unchanged, "new_output": new_output,
                            "pass": value_matches and unchanged and new_output})
        except Exception as exc:
            results.append({"name": label, "pass": False, "value_matches": False,
                            "input_unchanged": _typed_equal(arguments, original_args),
                            "error": type(exc).__name__ + ": " + str(exc)[:240]})
    return {"tests": results, "behavior_pass": all(r["pass"] for r in results),
            "isolation": {"isolated_python": bool(sys.flags.isolated), "cpu_limit_seconds": 1,
                          "empty_working_directory": not any(Path.cwd().iterdir()), "minimal_environment": True}}


def evaluate_response(answer, case_id, *, timeout_seconds=2.5):
    """Run only the closed fixture set. A format failure remains a strict failure."""
    if case_id not in FIXTURES:
        raise ValueError("Unknown coder fixture")
    if not 0.01 <= timeout_seconds <= 5:
        raise ValueError("Timeout must be between 0.01 and 5 seconds")
    report = {"case_id": case_id, "strict_pass": False, "format_pass": False,
              "markdown_fence": isinstance(answer, str) and "```" in answer,
              "syntax_pass": False, "subset_pass": False, "behavior_pass": False,
              "limitation": LIMITATION, "tests": []}
    try:
        source, report["format_pass"], report["format_note"] = parse_response(answer)
        # Syntax is separate from a valid function outside our accepted subset.
        ast.parse(source)
        report["syntax_pass"] = True
        validate_source(source, FIXTURES[case_id])
        report["subset_pass"] = True
    except (SyntaxError, SubsetError, ValueError, RecursionError) as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)[:240]
        return report
    payload = json.dumps({"case_id": case_id, "source": source})
    with tempfile.TemporaryDirectory(prefix="day29-coder-") as temporary:
        try:
            process = subprocess.run(
                [sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve()), "--child"],
                input=payload, text=True, encoding="utf-8", capture_output=True,
                timeout=timeout_seconds, cwd=temporary, env={"LANG": "C", "LC_ALL": "C"},
            )
        except subprocess.TimeoutExpired:
            report["error"] = "Execution exceeded wall timeout"
            report["timed_out"] = True
            return report
    if process.returncode != 0:
        report["error"] = "Isolated process failed or exceeded CPU limit"
        report["returncode"] = process.returncode
        return report
    try:
        if len(process.stdout) > 32_768:
            raise ValueError("Oversized child report")
        result = json.loads(process.stdout)
        if not isinstance(result, dict) or not isinstance(result.get("tests"), list):
            raise ValueError("Malformed child report")
        report.update(result)
    except (ValueError, TypeError) as exc:
        report["error"] = "Invalid isolated result: " + str(exc)[:240]
        return report
    report["strict_pass"] = report["format_pass"] and report["syntax_pass"] and report["subset_pass"] and report["behavior_pass"]
    return report


if __name__ == "__main__":
    if sys.argv[1:] != ["--child"] or not sys.flags.isolated:
        raise SystemExit("Import evaluate_response; isolated child is internal only.")
    incoming = sys.stdin.read(MAX_SOURCE * 4)
    print(json.dumps(_child(json.loads(incoming)), ensure_ascii=False))
