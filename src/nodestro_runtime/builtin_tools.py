"""Hand-written tools that ship with Nodestro (trusted: they run in the host process)."""

from __future__ import annotations

import ast
from datetime import date, datetime
import math
import operator
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orkexa import Tool, ToolResultError

_BINARY: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type[ast.unaryop], Any] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS: dict[str, Any] = {
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
}
_CONSTANTS = {"pi": math.pi, "e": math.e}


def _evaluate(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _evaluate(node.left), _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ToolResultError("Error: exponent too large")
        return float(_BINARY[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return float(_UNARY[type(node.op)](_evaluate(node.operand)))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCTIONS
        and not node.keywords
    ):
        function: Any = _FUNCTIONS[node.func.id]
        return float(function(*(_evaluate(arg) for arg in node.args)))
    raise ToolResultError(f"Error: unsupported expression element {type(node).__name__}")


@Tool
def calculator(expression: str) -> float:
    """Evaluate an arithmetic expression exactly instead of computing it in your head.

    Supports + - * / // % **, parentheses, pi, e and the functions sqrt, log, log10,
    exp, abs, round, min, max, sin, cos, tan.

    Args:
        expression: The expression, e.g. "(1250 - 980) / 980 * 100".

    Returns:
        The numeric result.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolResultError(f"Error: invalid expression: {exc.msg}") from None
    try:
        return float(_evaluate(tree))
    except ZeroDivisionError:
        raise ToolResultError("Error: division by zero") from None
    except (OverflowError, ValueError) as exc:
        raise ToolResultError(f"Error: {exc}") from None


@Tool
def current_datetime(timezone: str = "Europe/Vienna") -> str:
    """Get the current date, time and weekday in a time zone.

    Args:
        timezone: IANA time zone name, e.g. "Europe/Vienna" or "America/New_York".

    Returns:
        The current local date and time in ISO 8601 format with the weekday.
    """
    try:
        moment = datetime.now(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        raise ToolResultError(f"Error: unknown time zone {timezone!r}") from None
    return f"{moment.isoformat(timespec='seconds')} ({moment.strftime('%A')})"


@Tool
def days_between(start: date, end: date) -> int:
    """Count the calendar days from one date to another.

    Args:
        start: First date (YYYY-MM-DD).
        end: Second date (YYYY-MM-DD).

    Returns:
        The number of days from start to end; negative if end is before start.
    """
    return (end - start).days


@Tool
def text_statistics(text: str) -> dict[str, int]:
    """Count characters, words, sentences and paragraphs of a text.

    Args:
        text: The text to analyse.

    Returns:
        A JSON object with the counts.
    """
    sentences = sum(text.count(mark) for mark in ".!?")
    paragraphs = len([part for part in text.split("\n\n") if part.strip()])
    return {
        "characters": len(text),
        "words": len(text.split()),
        "sentences": sentences,
        "paragraphs": paragraphs,
    }


BUILTIN_TOOLS: tuple[Tool, ...] = (calculator, current_datetime, days_between, text_statistics)
