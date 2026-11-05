"""The composition language for user-made full functions.

A full function's ``steps`` is a JSON list. Each step is one of::

    {"call": "<partial or function>", "args": {...}, "as": "var"}
    {"set": "var", "value": <value>}            -- or "expr": "<expression>"
    {"if": "<condition>", "then": [steps], "else": [steps]}
    {"repeat": <count>, "do": [steps], "as": "i"}
    {"while": "<condition>", "do": [steps], "max": 100}
    {"for_each": <expr>, "as": "item", "do": [steps]}
    {"when": {"event": "...", ...}, "do": [steps]}     -- event trigger (see triggers.py)
    {"wait": <seconds or duration>}
    {"return": <expr>}

Values are interpolated: ``"${name}"`` is replaced with a variable (the whole
value keeps its type when the string is exactly one reference, otherwise it is
formatted into the text). ``${args.city}`` and ``${result.x}`` reach into
objects. The function's own arguments are variables too.

Conditions are small Python-like expressions, evaluated by a whitelist
evaluator (no attribute access, no imports, no arbitrary calls):
``app == "firefox" and len(items) > 2``, ``"error" in output``.
"""
from __future__ import annotations

import ast
import operator
import re
from typing import Any, Callable, Iterable

from .base import FunctionError

MAX_LOOP = 1000
_REF = re.compile(r"\$\{([^}]+)\}")


class _Return(Exception):
    def __init__(self, value: Any) -> None:
        self.value = value


def lookup(variables: dict[str, Any], ref: str) -> Any:
    node: Any = variables
    for part in ref.strip().split("."):
        if isinstance(node, dict):
            if part not in node:
                raise FunctionError(f"unknown variable '{ref}'")
            node = node[part]
        elif isinstance(node, (list, tuple)) and part.lstrip("-").isdigit():
            node = node[int(part)]
        else:
            raise FunctionError(f"can't read '{part}' of '{ref}'")
    return node


def interpolate(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        m = _REF.fullmatch(value.strip())
        if m:
            return lookup(variables, m.group(1))
        return _REF.sub(lambda mm: _to_text(lookup(variables, mm.group(1))), value)
    if isinstance(value, list):
        return [interpolate(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: interpolate(v, variables) for k, v in value.items()}
    return value


def _to_text(v: Any) -> str:
    if isinstance(v, str):
        return v
    import json
    try:
        return json.dumps(v)
    except TypeError:
        return str(v)


# ---------------------------------------------------------------------------
# Safe condition evaluator
# ---------------------------------------------------------------------------

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv}
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Gt: operator.gt, ast.GtE: operator.ge, ast.In: lambda a, b: a in b,
        ast.NotIn: lambda a, b: a not in b, ast.Is: operator.is_, ast.IsNot: operator.is_not}
_FUNCS: dict[str, Callable[..., Any]] = {
    "len": len, "str": str, "int": int, "float": float, "bool": bool, "abs": abs, "min": min, "max": max,
    "lower": lambda s: str(s).lower(), "upper": lambda s: str(s).upper(),
    "contains": lambda a, b: str(b).lower() in str(a).lower(),
}


def evaluate(expr: Any, variables: dict[str, Any]) -> Any:
    if not isinstance(expr, str):
        return interpolate(expr, variables)
    expr = _REF.sub(lambda m: m.group(1).replace(".", "__dot__"), expr)
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise FunctionError(f"bad condition {expr!r}: {exc.msg}") from exc
    return _eval(tree.body, variables)


def _eval(node: ast.AST, v: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        name = node.id.replace("__dot__", ".")
        if name in ("true", "True"):
            return True
        if name in ("false", "False"):
            return False
        if name in ("none", "None", "null"):
            return None
        return lookup(v, name)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for val in node.values:
                result = _eval(val, v)
                if not result:
                    return result
            return result
        result = False
        for val in node.values:
            result = _eval(val, v)
            if result:
                return result
        return result
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, v)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        return _BIN[type(node.op)](_eval(node.left, v), _eval(node.right, v))
    if isinstance(node, ast.Compare):
        left = _eval(node.left, v)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, v)
            if not _CMP[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Subscript):
        return _eval(node.value, v)[_eval(node.slice, v)]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, v) for e in node.elts]
    if isinstance(node, ast.Dict):
        return {_eval(k, v): _eval(val, v) for k, val in zip(node.keys, node.values) if k is not None}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS \
            and not node.keywords:
        return _FUNCS[node.func.id](*[_eval(a, v) for a in node.args])
    raise FunctionError(f"not allowed in a condition: {ast.dump(node)[:60]}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_steps(steps: list[dict[str, Any]], ctx: Any, variables: dict[str, Any]) -> Any:
    """Run a composition. ``ctx.call(name, **args)`` runs a partial/function;
    ``ctx.check_cancelled()`` raises when aborted; ``ctx.wait(seconds)`` sleeps
    interruptibly; ``ctx.register_trigger(spec, steps, variables)`` is used for
    ``when``."""
    try:
        return _block(steps, ctx, variables)
    except _Return as r:
        return r.value


def _block(steps: Iterable[dict[str, Any]], ctx: Any, v: dict[str, Any]) -> Any:
    last: Any = None
    for step in steps or []:
        ctx.check_cancelled()
        last = _step(step, ctx, v)
    return last


def _step(step: dict[str, Any], ctx: Any, v: dict[str, Any]) -> Any:
    if not isinstance(step, dict):
        raise FunctionError(f"a step must be an object, got {step!r}")
    if "call" in step:
        args = interpolate(step.get("args", {}), v)
        result = ctx.call(step["call"], **args)
        if step.get("as"):
            v[step["as"]] = result
        v["result"] = result
        return result
    if "set" in step:
        value = step.get("value")
        # {"set": "n", "expr": "n + 1"} evaluates; "value" is taken literally (interpolated)
        v[step["set"]] = evaluate(step["expr"], v) if "expr" in step else interpolate(value, v)
        return v[step["set"]]
    if "if" in step:
        branch = step.get("then", []) if evaluate(step["if"], v) else step.get("else", [])
        return _block(branch, ctx, v)
    if "repeat" in step:
        count = int(interpolate(step["repeat"], v))
        if count > MAX_LOOP:
            raise FunctionError(f"repeat is limited to {MAX_LOOP}")
        last = None
        for i in range(count):
            if step.get("as"):
                v[step["as"]] = i
            last = _block(step.get("do", []), ctx, v)
        return last
    if "while" in step:
        limit = min(int(step.get("max", 100)), MAX_LOOP)
        last, n = None, 0
        while evaluate(step["while"], v):
            if n >= limit:
                raise FunctionError(f"while loop passed its limit of {limit} iterations")
            last = _block(step.get("do", []), ctx, v)
            n += 1
        return last
    if "for_each" in step:
        items = interpolate(step["for_each"], v)
        if isinstance(items, str):
            items = [s for s in items.splitlines() if s.strip()]
        last = None
        for item in list(items)[:MAX_LOOP]:
            v[step.get("as", "item")] = item
            last = _block(step.get("do", []), ctx, v)
        return last
    if "when" in step:
        return ctx.register_trigger(interpolate(step["when"], v), step.get("do", []), dict(v))
    if "wait" in step:
        from ..util import parse_duration
        seconds = parse_duration(interpolate(step["wait"], v))
        ctx.wait(seconds)
        return seconds
    if "return" in step:
        raise _Return(interpolate(step["return"], v))
    raise FunctionError(f"unknown step: {sorted(step)}")


def partials_used(steps: list[dict[str, Any]] | None) -> set[str]:
    out: set[str] = set()
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        if "call" in step:
            out.add(step["call"])
        for key in ("then", "else", "do"):
            out |= partials_used(step.get(key))
        if "when" in step:
            out.add("event_trigger")
        if "wait" in step:
            out.add("wait")
    return out


def validate(steps: Any, known: set[str] | None = None) -> list[str]:
    """Static checks; returns a list of problems (empty = fine)."""
    problems: list[str] = []
    if not isinstance(steps, list):
        return ["steps must be a list"]
    keys = {"call", "set", "if", "repeat", "while", "for_each", "when", "wait", "return"}
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or not (keys & set(step)):
            problems.append(f"step {i + 1}: not a recognised step")
            continue
        if "call" in step and known is not None and step["call"] not in known:
            problems.append(f"step {i + 1}: unknown function '{step['call']}'")
        for key in ("then", "else", "do"):
            if key in step:
                problems += [f"step {i + 1}/{key}: {p}" for p in validate(step[key], known)]
        for key in ("if", "while"):
            if key in step and isinstance(step[key], str):
                try:
                    ast.parse(_REF.sub(lambda m: m.group(1).replace(".", "__dot__"), step[key]), mode="eval")
                except SyntaxError:
                    problems.append(f"step {i + 1}: condition doesn't parse")
    return problems
