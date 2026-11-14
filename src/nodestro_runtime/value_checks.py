"""Checked values: a number of a structured result that must come from a deterministic tool.

An agent's result field may declare ``source="tool"`` (:class:`~nodestro_runtime.schemas.OutputField`): the value is
a key figure (a share, a total, a margin) that later steps decide on, so the model must not estimate it. The engine
checks it after the agent answered (:func:`unverified_fields`):

- the value must be the result of a successful call of a *computing* tool (:func:`computing_call`: the calculator,
  ``table_totals``, ``days_between``, ``text_statistics`` or a tool of the agent's own tool library) of this step
  or of a step that feeds it (its ancestors in the workflow, loop connections included; never a parallel branch),
  rounded to the decimals the agent reported, and not a number the call
  already had in its input (a calculator called with the value itself, in any spelling, computed nothing; neither
  did one that only rounds or transforms a single number). Of a ``table_totals`` result only what the tool computed
  counts (totals, means, extremes, shares, row counts), never what it repeats from its input (the reference total,
  column and group names);
- when that result came from ``table_totals`` and the step's input has a table with the same columns, the table the
  agent passed must have exactly the rows of that table: none left out (a total over a shortened table does not
  count), none added and none changed. A passed table with other columns is compared with the input's tables that
  have its value column, by the values of that column.

The check proves where a value comes from, not that the tool got the right figures: a calculator call written out
from made-up numbers, or ``table_totals`` over a made-up table whose value column no table of the step's input has
(e.g. a table that came from a tool), still backs its result. Tools that look things up rather than compute (input
files, knowledge search, memory, the current date, MCP servers, provider server tools) never back a value, even when
their result contains the number.

A value that fails is sent back to the agent once with the reason; a second failure fails the step with
``run.value_unverified``, so no decision is made on an unchecked figure.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
import json
import math
import re
from typing import Any

from pydantic import BaseModel

from .builtin_tools import number_of, parse_table, parse_tables
from .schemas import OutputConfig, ToolCallRecord

__all__ = [
    "COMPUTING_BUILTINS",
    "Unverified",
    "as_result_dict",
    "checked_fields",
    "computes",
    "computing_call",
    "correction_prompt",
    "tool_numbers",
    "unverified_fields",
]

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_NUMERIC = ("number", "integer")


@dataclass(frozen=True)
class Unverified:
    """A checked field that failed: ``reason`` is ``missing``, ``not_from_tool``, ``table_incomplete`` or
    ``table_changed`` (rows added or changed)."""

    field: str
    value: Any
    reason: str
    rows_passed: int | None = None
    rows_in_input: int | None = None


#: Nodestro's built-in tools that compute a number from their input (``current_datetime`` only looks one up).
COMPUTING_BUILTINS = frozenset({"calculator", "table_totals", "days_between", "text_statistics"})
#: Names that are never a library tool of an agent (its memory, Orkexa's own tools for files and knowledge search).
_LOOKUPS = frozenset({"memory", "current_datetime", "input_files", "search"})


def computing_call(record: ToolCallRecord, own_tools: Iterable[str] = ()) -> bool:
    """Whether ``record`` is a call of a tool that computes: a built-in computing tool, or a tool of the agent's own
    library (``own_tools``: the names in its configuration, a stored Python tool) that is not an MCP server's tool.
    Lookups (input files, knowledge search, memory, the date, MCP, provider server tools) are not."""
    if record.name in COMPUTING_BUILTINS:
        return True
    if record.name in _LOOKUPS or record.name.startswith("mcp__") or record.provenance.get("source") == "mcp":
        return False
    return record.name in set(own_tools)


def computes(tools: Iterable[str]) -> bool:
    """Whether an agent configured with ``tools`` has a tool whose calls can back a key figure."""
    return any(name not in _LOOKUPS and not name.startswith("mcp__") for name in tools)


def checked_fields(output: OutputConfig) -> list[str]:
    """The numeric fields of a structured result that must come from a tool."""
    if output.mode != "structured":
        return []
    return [item.name for item in output.fields if item.source == "tool" and item.type in _NUMERIC]


def as_result_dict(value: Any) -> Mapping[str, Any] | None:
    """A structured result as a mapping (a model, a mapping or its JSON text); ``None`` otherwise."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def _numbers(text: str) -> list[float]:
    found: list[float] = []
    for match in _NUMBER.finditer(text):
        try:
            found.append(float(match.group(0)))
        except ValueError:
            continue
    return found


#: The values ``table_totals`` computes (:func:`~nodestro_runtime.builtin_tools.summarize_table`), at the top level and
#: per group. Everything else in its result repeats its input (``column``, ``reference_total``, ``group``).
_TOTALS_KEYS = frozenset(
    {
        "rows",
        "skipped_rows",
        "total",
        "mean",
        "min",
        "max",
        "share_of_reference_percent",
        "share_percent",
        "cumulative_share_percent",
    }
)


def _computed_totals(output: str) -> list[float] | None:
    """The numbers ``table_totals`` computed, from its JSON result; ``None`` when the result is not one."""
    try:
        parsed = json.loads(output)
    except ValueError:
        try:
            parsed = ast.literal_eval(output)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return None
    if not isinstance(parsed, Mapping):
        return None
    found: list[float] = []
    for item in (parsed, *(group for group in parsed.get("groups") or () if isinstance(group, Mapping))):
        for key, value in item.items():
            if key in _TOTALS_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
                found.append(float(value))
    return found


def _given(record: ToolCallRecord) -> set[float]:
    """The numbers a call had in its input. For the calculator also every numeric literal as Python reads it
    (``0x1cf``, ``46_3`` and ``4.63e2`` are all 463)."""
    given = set(_numbers(json.dumps(record.input, ensure_ascii=False)))
    expression = record.input.get("expression") if record.name == "calculator" else None
    if isinstance(expression, str):
        try:
            tree = ast.parse(expression, mode="eval")
        except (SyntaxError, ValueError, MemoryError, RecursionError):
            return given
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                with suppress(OverflowError):
                    given.add(float(node.value))
    return given


def _operands(record: ToolCallRecord) -> int:
    """How many numbers (literals, ``pi``, ``e``) a calculator expression combines; ``0`` when it does not parse."""
    expression = record.input.get("expression")
    if not isinstance(expression, str):
        return 0
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return 0
    return sum(
        1
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
        )
        or (isinstance(node, ast.Name) and node.id in ("pi", "e"))
    )


def tool_numbers(record: ToolCallRecord) -> list[float]:
    """The numbers a successful tool call computed: those of its result that its input did not already contain.

    A calculator call with the expression ``463`` returns 463, but computed nothing, so it backs no value; nor does
    one that only rounds or transforms a single number (``round(463.2)``). Of a ``table_totals`` result only what it
    computed counts (the row check guards its input); the reference total and names it repeats do not.
    """
    if record.is_error or not record.output:
        return []
    if record.name == "table_totals":
        computed = _computed_totals(record.output)
        if computed is not None:
            return computed
    if record.name == "calculator" and _operands(record) < 2:
        return []
    found = _numbers(record.output)
    given = _given(record)
    return [number for number in found if number not in given]


def _decimals(value: float) -> int:
    text = repr(float(value))
    if "e" in text or "E" in text:
        return 6
    return min(len(text.partition(".")[2].rstrip("0")), 6)


def _matches(reported: float, computed: float) -> bool:
    """``reported`` is ``computed`` rounded to the decimals the agent gave (half a unit of the last digit)."""
    tolerance: float = 0.5 * 10.0 ** -_decimals(reported) + 1e-9 * max(1.0, abs(computed))
    return abs(reported - computed) <= tolerance


def _table_rows(record: ToolCallRecord) -> tuple[list[str], list[tuple[Any, ...]]] | None:
    """The header and the counted rows (those with a number in the value column) of the table a ``table_totals``
    call was given."""
    table = record.input.get("table")
    if record.name != "table_totals" or not isinstance(table, str):
        return None
    parsed = parse_table(table)
    if parsed is None:
        return None
    header, rows = parsed
    column = str(record.input.get("value_column") or "")
    return header, _counted(header, rows, column)


def _counted(header: list[str], rows: list[list[str]], column: str) -> list[tuple[Any, ...]]:
    """Each row with a number in the value column, compared by that number and its other cells (case and outer
    spaces aside), so a row counts as the same however the table was written (Markdown, CSV, aligned)."""
    found: list[tuple[Any, ...]] = []
    index = _value_index(header, column)
    if index is None:
        return found
    for row in rows:
        if index >= len(row):
            continue
        value = number_of(row[index])
        if value is None:
            continue
        found.append((value, *(cell.strip().casefold() for position, cell in enumerate(row) if position != index)))
    return found


def _value_index(header: list[str], column: str) -> int | None:
    wanted = column.strip().casefold()
    indexes = [index for index, title in enumerate(header) if title.casefold() == wanted] or [
        index for index, title in enumerate(header) if wanted and wanted in title.casefold()
    ]
    return indexes[0] if indexes else None


def _input_tables(source_text: str, header: list[str], column: str) -> tuple[list[list[tuple[Any, ...]]], bool]:
    """The counted rows of every table in the input with the same columns, and ``True``. Without such a table: the
    tables that have the value column (the agent may have left out or renamed other columns), compared by their
    values only, and ``False``. No list: the input has no table to compare with."""
    wanted = [title.casefold() for title in header]
    tables = parse_tables(source_text)
    same = [
        _counted(other_header, rows, column)
        for other_header, rows in tables
        if [title.casefold() for title in other_header] == wanted
    ]
    if same:
        return same, True
    return [
        [(row[0],) for row in _counted(other_header, rows, column)]
        for other_header, rows in tables
        if _value_index(other_header, column) is not None
    ], False


def _table_problem(record: ToolCallRecord, name: str, value: Any, source_text: str) -> Unverified | None:
    """Why the table of a ``table_totals`` call does not back ``value`` (``None``: it does, or the input has no table
    with its value column to compare it with)."""
    rows = _table_rows(record)
    if rows is None:
        return None
    header, passed = rows
    tables, whole_rows = _input_tables(source_text, header, str(record.input.get("value_column") or ""))
    if not tables:
        return None
    given = Counter(passed if whole_rows else [(row[0],) for row in passed])
    best: Unverified | None = None
    for in_input in tables:
        expected = Counter(in_input)
        if given == expected:
            return None
        reason = "table_incomplete" if not given - expected else "table_changed"  # only rows left out, or not
        candidate = Unverified(name, value, reason, rows_passed=len(passed), rows_in_input=len(in_input))
        if best is None or (best.reason == "table_changed" and candidate.reason == "table_incomplete"):
            best = candidate
    return best


def unverified_fields(
    output: OutputConfig, result: Any, calls: Iterable[ToolCallRecord], source_text: str
) -> list[Unverified]:
    """The checked fields of ``result`` that no tool result backs (see the module docstring)."""
    names = checked_fields(output)
    if not names:
        return []
    values = as_result_dict(result)
    records = list(calls)
    required = {item.name: item.required for item in output.fields}
    failed: list[Unverified] = []
    for name in names:
        value = values.get(name) if values is not None else None
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
            if value is None and not required.get(name, True) and values is not None:
                continue
            failed.append(Unverified(name, value, "missing"))
            continue
        backing = [record for record in records if any(_matches(float(value), item) for item in tool_numbers(record))]
        if not backing:
            failed.append(Unverified(name, value, "not_from_tool"))
            continue
        problem: Unverified | None = None
        for record in backing:
            problem = _table_problem(record, name, value, source_text)
            if problem is None:
                break
        if problem is not None:
            failed.append(problem)
    return failed


def correction_prompt(prompt: str, failed: list[Unverified], earlier: Iterable[ToolCallRecord] = ()) -> str:
    """The task again, with why each checked value was not accepted (the agent's one chance to correct it), and what
    the step's tool calls so far returned: the correction is a new turn, and its tools that could have effects
    outside the run are not offered again (RT3-03)."""
    lines: list[str] = []
    for item in failed:
        if item.reason == "table_incomplete":
            lines.append(
                f"- {item.field}: the table you passed to table_totals had {item.rows_passed} rows, the input has "
                f"{item.rows_in_input}. Pass the whole table unchanged."
            )
        elif item.reason == "table_changed":
            lines.append(
                f"- {item.field}: the table you passed to table_totals is not the table of the input (rows were "
                "added or changed). Pass the whole table exactly as it appears in the input."
            )
        elif item.reason == "missing":
            lines.append(f"- {item.field}: the value is missing. Compute it with a tool and report it.")
        else:
            lines.append(
                f"- {item.field}: {item.value} is not the result of a tool call. Compute it with a tool, even if "
                "you or an earlier step already know it: table_totals for totals and shares of table rows, "
                "otherwise one calculator call with the whole calculation written out from the figures it is "
                "based on (for example income minus all expenses, not only a part of it and not the result "
                "itself). Report exactly the tool's result, rounded to at most two decimals."
            )
    text = (
        f"{prompt}\n\nYour previous answer was not accepted, because these values must be computed with a tool:\n"
        + "\n".join(lines)
    )
    done = list(earlier)[:_EARLIER_CALLS]
    if done:
        text += (
            "\n\nYour tool calls so far already ran and must not be repeated; only the tools for computing are "
            "available now. Their results:\n"
            + "\n".join(
                f"- {call.name}({json.dumps(call.input, ensure_ascii=False)[:300]}) -> {(call.output or '')[:500]}"
                for call in done
            )
        )
    return text


#: How many earlier tool calls the correction lists.
_EARLIER_CALLS = 20
