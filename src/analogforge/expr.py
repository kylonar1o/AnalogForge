"""Restricted expression evaluator.

Used for YAML design values (``w: Wn*2``), PDK device templates
(``W={w*1e6:.6g}``), optimization constraints (``f_osc >= 2.3e9``) and signal
expressions (``v(outp)-v(outn)``).  Only literals, names from the supplied
namespace, arithmetic/comparison/boolean operators, conditional expressions
and calls to whitelisted functions are allowed - no attribute access,
subscripts, comprehensions or lambdas - so YAML files cannot execute
arbitrary Python.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any, Callable, Mapping

import numpy as np

from .units import parse_si

_BIN = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
_CMP = {
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
}


def _db20(x):
    return 20 * np.log10(np.maximum(np.abs(x), 1e-300))


def _db10(x):
    return 10 * np.log10(np.maximum(np.abs(x), 1e-300))


SAFE_FUNCS: dict[str, Callable] = {
    "abs": np.abs, "sqrt": np.sqrt, "exp": np.exp, "log": np.log, "ln": np.log,
    "log10": np.log10, "sin": np.sin, "cos": np.cos, "tan": np.tan, "atan": np.arctan,
    "atan2": np.arctan2, "sinh": np.sinh, "cosh": np.cosh, "tanh": np.tanh,
    "min": min, "max": max, "floor": math.floor, "ceil": math.ceil, "round": round,
    "int": int, "float": float, "real": np.real, "imag": np.imag, "mag": np.abs,
    "phase": lambda x: np.degrees(np.angle(x)), "db20": _db20, "db10": _db10, "db": _db20,
    "si": parse_si,
}
SAFE_CONSTS = {"pi": math.pi, "e": math.e, "k_B": 1.380649e-23, "q_e": 1.602176634e-19,
               "inf": math.inf, "true": True, "false": False}


class ExpressionError(ValueError):
    pass


# SPICE-style numbers with scale suffix (10u, 2.2k, 1meg) are rewritten to floats
_SI_TOKEN = re.compile(r"(?<![A-Za-z_\d.])(\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?(meg|mil|[tgkmunpfa])(?![A-Za-z_\d])",
                       re.IGNORECASE)


def _rewrite_si(text: str) -> str:
    def repl(m: re.Match) -> str:
        return repr(parse_si(m.group(0)))
    return _SI_TOKEN.sub(repl, text)


def _eval(node: ast.AST, ns: Mapping[str, Any], funcs: Mapping[str, Callable]):
    if isinstance(node, ast.Expression):
        return _eval(node.body, ns, funcs)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, complex, str, bool)) or node.value is None:
            return node.value
        raise ExpressionError(f"constant {node.value!r} not allowed")
    if isinstance(node, ast.Name):
        if node.id in ns:
            return ns[node.id]
        if node.id in SAFE_CONSTS:
            return SAFE_CONSTS[node.id]
        raise ExpressionError(f"unknown name {node.id!r}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        return _BIN[type(node.op)](_eval(node.left, ns, funcs), _eval(node.right, ns, funcs))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand, ns, funcs))
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, ns, funcs) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(vals)
        return any(vals)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, ns, funcs)
        for op, comp in zip(node.ops, node.comparators):
            if type(op) not in _CMP:
                raise ExpressionError("comparison operator not allowed")
            right = _eval(comp, ns, funcs)
            if not _CMP[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body, ns, funcs) if _eval(node.test, ns, funcs) else _eval(node.orelse, ns, funcs)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise ExpressionError("only simple function calls are allowed")
        name = node.func.id
        fn = funcs.get(name) or SAFE_FUNCS.get(name)
        if fn is None:
            raise ExpressionError(f"function {name!r} is not allowed")
        return fn(*[_eval(a, ns, funcs) for a in node.args])
    raise ExpressionError(f"syntax element {type(node).__name__} not allowed")


def evaluate(text: Any, namespace: Mapping[str, Any] | None = None,
             functions: Mapping[str, Callable] | None = None) -> Any:
    """Evaluate ``text`` safely.  Numbers (int/float) are returned unchanged."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return text
    if not isinstance(text, str):
        raise ExpressionError(f"cannot evaluate {text!r}")
    src = _rewrite_si(text.strip())
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"invalid expression {text!r}: {exc.msg}") from None
    try:
        return _eval(tree, namespace or {}, functions or {})
    except ExpressionError as exc:
        raise ExpressionError(f"{exc} in expression {text!r}") from None
    except ZeroDivisionError:
        raise ExpressionError(f"division by zero in expression {text!r}") from None


def evaluate_number(text: Any, namespace: Mapping[str, Any] | None = None) -> float:
    value = evaluate(text, namespace)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ExpressionError(f"expression {text!r} did not evaluate to a number") from None


_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def render_template(template: str, namespace: Mapping[str, Any]) -> str:
    """Replace ``{expr}`` / ``{expr:fmt}`` placeholders using the safe evaluator."""

    def repl(m: re.Match) -> str:
        body = m.group(1)
        fmt = ""
        # split off a format spec (":.6g") but not "::" or comparison text
        mm = re.match(r"^(.*?):([<>^=+\- #0-9,._]*[bcdeEfFgGnosxX%]?)$", body)
        if mm and mm.group(2):
            body, fmt = mm.group(1), mm.group(2)
        value = evaluate(body, namespace)
        if fmt:
            return format(value, fmt)
        if isinstance(value, float):
            return f"{value:.10g}"
        return str(value)

    return _PLACEHOLDER.sub(repl, template)


def parse_constraint(text: str) -> tuple[str, str, float | str]:
    """Split ``'metric >= value'`` into ``(lhs, op, rhs)``; rhs may be an expression."""
    m = re.match(r"^\s*(.+?)\s*(<=|>=|<|>|==)\s*(.+?)\s*$", text)
    if not m:
        raise ExpressionError(f"constraint {text!r} must look like 'metric >= value'")
    return m.group(1), m.group(2), m.group(3)
