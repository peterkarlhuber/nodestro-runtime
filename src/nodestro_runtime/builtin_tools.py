"""Hand-written tools that ship with Nodestro (trusted: they run in the host process)."""

from __future__ import annotations

import ast
from datetime import date, datetime
import math
import operator
import re
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


# ---- tables (table_totals; self-contained, so the sandbox probes can run this module as plain code) ----------


def number_of(text: str) -> float | None:
    """The number in a table cell (``1,234.5``, ``1.234,5``, ``14 400``, ``45 min``, ``4.6 %``); ``None`` without one.

    With both separators the last one is the decimal mark. A lone comma is a thousands separator only in groups of
    three digits (``1,500``), otherwise the decimal mark (``1,5``); a lone point is the decimal mark (``4.618``) unless
    it repeats in groups of three (``1.234.567``).
    """
    cleaned = text.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ").replace("\u2212", "-")
    match = re.search(r"[-+]?\d(?:[\d.,' ]*\d)?", cleaned)
    if match is None:
        return None
    raw = re.sub(r"[ ']", "", match.group(0))
    if "," in raw and "." in raw:
        raw = raw.replace(",", "") if raw.rfind(".") > raw.rfind(",") else raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", "") if re.fullmatch(r"[-+]?\d{1,3}(,\d{3})+", raw) else raw.replace(",", ".")
    elif raw.count(".") > 1:
        if not re.fullmatch(r"[-+]?\d{1,3}(\.\d{3})+", raw):
            return None
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _cells(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def parse_tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    """Every table in a text: Markdown pipe tables, and CSV/semicolon/tab text when the text is one table."""
    found: list[tuple[list[str], list[list[str]]]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("|") and line.count("|") >= 2:
            block: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                block.append(lines[index])
                index += 1
            rows = [_cells(item) for item in block if not re.fullmatch(r"[\s|:\-]+", item)]
            if len(rows) >= 2:
                found.append((rows[0], rows[1:]))
            continue
        index += 1
    if found:
        return found
    candidates = [item for item in lines if item.strip()]
    for separator in ("\t", ";", ","):
        if len(candidates) >= 2 and all(item.count(separator) >= 1 for item in candidates):
            counts = {item.count(separator) for item in candidates}
            if len(counts) == 1:
                rows = [[cell.strip().strip('"') for cell in item.split(separator)] for item in candidates]
                return [(rows[0], rows[1:])]
    return found


def parse_table(text: str) -> tuple[list[str], list[list[str]]] | None:
    """The first table of a text (see :func:`parse_tables`)."""
    tables = parse_tables(text)
    return tables[0] if tables else None


def _column(header: list[str], name: str) -> int:
    wanted = name.strip().casefold()
    exact = [index for index, title in enumerate(header) if title.casefold() == wanted]
    if exact:
        return exact[0]
    partial = [index for index, title in enumerate(header) if wanted and wanted in title.casefold()]
    if len(partial) == 1:
        return partial[0]
    columns = ", ".join(repr(title) for title in header)
    if partial:
        raise ValueError(f"the column {name!r} is ambiguous; the columns are {columns}")
    raise ValueError(f"no column {name!r}; the columns are {columns}")


def summarize_table(
    parsed: tuple[list[str], list[list[str]]], value_column: str, group_by: str = "", reference_total: float = 0
) -> dict[str, Any]:
    """Exact totals of one column of a parsed table (the result of :func:`table_totals`)."""
    header, rows = parsed
    value_index = _column(header, value_column)
    group_index = _column(header, group_by) if group_by.strip() else None
    values: list[float] = []
    groups: dict[str, list[float]] = {}
    skipped = 0
    for row in rows:
        cell = row[value_index] if value_index < len(row) else ""
        value = number_of(cell)
        if value is None:
            skipped += 1
            continue
        values.append(value)
        if group_index is not None:
            key = row[group_index].strip() if group_index < len(row) else ""
            groups.setdefault(key, []).append(value)
    if not values:
        raise ValueError(f"the column {header[value_index]!r} has no numbers")
    total = math.fsum(values)
    result: dict[str, Any] = {
        "column": header[value_index],
        "rows": len(values),
        "skipped_rows": skipped,
        "total": total,
        "mean": total / len(values),
        "min": min(values),
        "max": max(values),
    }
    if reference_total:
        result["reference_total"] = reference_total
        result["share_of_reference_percent"] = total / reference_total * 100
    if group_index is not None:
        ranked = sorted(groups.items(), key=lambda item: (-math.fsum(item[1]), item[0]))
        cumulative = 0.0
        result["groups"] = []
        for key, items in ranked:
            subtotal = math.fsum(items)
            cumulative += subtotal
            group: dict[str, Any] = {
                "group": key,
                "rows": len(items),
                "total": subtotal,
                "mean": subtotal / len(items),
                "share_percent": subtotal / total * 100 if total else 0.0,
                "cumulative_share_percent": cumulative / total * 100 if total else 0.0,
            }
            if reference_total:
                group["share_of_reference_percent"] = subtotal / reference_total * 100
            result["groups"].append(group)
    return result


@Tool
def table_totals(table: str, value_column: str, group_by: str = "", reference_total: float = 0) -> dict[str, Any]:
    """Add up a column of a table exactly: total, count, mean, per-group totals and the share of a reference.

    Use it for every total, share or ranking over table rows instead of adding numbers yourself. Pass the whole
    table exactly as it appears in the input (a Markdown table or CSV text with its header row); rows that are
    left out are missing from the result.

    Args:
        table: The table text with its header row, every row copied unchanged.
        value_column: The column to add up, e.g. "Duration (min)" (its name or a unique part of it).
        group_by: Optional column to group by, e.g. "Machine"; the groups come largest first (Pareto order).
        reference_total: Optional total to compare with, e.g. the planned production time; the result then has
            the share of the column total in it as a percentage.

    Returns:
        A JSON object with rows, total, mean, min, max, share_of_reference_percent and groups.
    """
    parsed = parse_table(table)
    if parsed is None:
        raise ToolResultError("Error: no table found; pass a Markdown table or CSV text with a header row")
    try:
        return summarize_table(parsed, value_column, group_by, reference_total)
    except ValueError as exc:
        raise ToolResultError(f"Error: {exc}") from None


BUILTIN_TOOLS: tuple[Tool, ...] = (calculator, current_datetime, days_between, text_statistics, table_totals)
