"""Checked values: a number of a structured result that must come from a deterministic tool.

An agent's result field may declare ``source="tool"`` (:class:`~nodestro_runtime.schemas.OutputField`): the value is
a key figure (a share, a total, a margin) that later steps decide on, so the model must not estimate it. The engine
checks it after the agent answered (:func:`unverified_fields`):

- the value must be the result of a successful tool call of this step or of an earlier step of the run, rounded to
  the decimals the agent reported (the calculator, ``table_totals``, a stored tool);
- when that result came from ``table_totals``, the table the agent passed must have every row of the matching table
  in the step's input, so a total over a shortened table does not count.

A value that fails is sent back to the agent once with the reason; a second failure fails the step with
``run.value_unverified``, so no decision is made on an unchecked figure.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
import math
import re
from typing import Any

from pydantic import BaseModel

from .builtin_tools import number_of, parse_table, parse_tables
from .schemas import OutputConfig, ToolCallRecord

__all__ = ["Unverified", "checked_fields", "correction_prompt", "tool_numbers", "unverified_fields"]

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_NUMERIC = ("number", "integer")


@dataclass(frozen=True)
class Unverified:
    """A checked field that failed: ``reason`` is ``missing``, ``not_from_tool`` or ``table_incomplete``."""

    field: str
    value: Any
    reason: str
    rows_passed: int | None = None
    rows_in_input: int | None = None


def checked_fields(output: OutputConfig) -> list[str]:
    """The numeric fields of a structured result that must come from a tool."""
    if output.mode != "structured":
        return []
    return [item.name for item in output.fields if item.source == "tool" and item.type in _NUMERIC]


def _as_dict(value: Any) -> Mapping[str, Any] | None:
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


def tool_numbers(record: ToolCallRecord) -> list[float]:
    """The numbers in a successful tool call's result."""
    if record.is_error or not record.output:
        return []
    found: list[float] = []
    for match in _NUMBER.finditer(record.output):
        try:
            found.append(float(match.group(0)))
        except ValueError:
            continue
    return found


def _decimals(value: float) -> int:
    text = repr(float(value))
    if "e" in text or "E" in text:
        return 6
    return min(len(text.partition(".")[2].rstrip("0")), 6)


def _matches(reported: float, computed: float) -> bool:
    """``reported`` is ``computed`` rounded to the decimals the agent gave (half a unit of the last digit)."""
    tolerance: float = 0.5 * 10.0 ** -_decimals(reported) + 1e-9 * max(1.0, abs(computed))
    return abs(reported - computed) <= tolerance


def _table_rows(record: ToolCallRecord) -> tuple[list[str], int] | None:
    table = record.input.get("table")
    if record.name != "table_totals" or not isinstance(table, str):
        return None
    parsed = parse_table(table)
    if parsed is None:
        return None
    header, rows = parsed
    column = str(record.input.get("value_column") or "")
    counted = sum(1 for row in rows if _value_cell(header, row, column) is not None)
    return header, counted


def _value_cell(header: list[str], row: list[str], column: str) -> float | None:
    wanted = column.strip().casefold()
    indexes = [index for index, title in enumerate(header) if title.casefold() == wanted] or [
        index for index, title in enumerate(header) if wanted and wanted in title.casefold()
    ]
    if not indexes or indexes[0] >= len(row):
        return None
    return number_of(row[indexes[0]])


def _input_rows(source_text: str, header: list[str], column: str) -> int | None:
    """The data rows of the table in the input with the same columns; ``None`` when the input has none."""
    wanted = [title.casefold() for title in header]
    best: int | None = None
    for other_header, rows in parse_tables(source_text):
        if [title.casefold() for title in other_header] != wanted:
            continue
        counted = sum(1 for row in rows if _value_cell(other_header, row, column) is not None)
        best = counted if best is None else max(best, counted)
    return best


def unverified_fields(
    output: OutputConfig, result: Any, calls: Iterable[ToolCallRecord], source_text: str
) -> list[Unverified]:
    """The checked fields of ``result`` that no tool result backs (see the module docstring)."""
    names = checked_fields(output)
    if not names:
        return []
    values = _as_dict(result)
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
        shortened: Unverified | None = None
        for record in backing:
            rows = _table_rows(record)
            if rows is None:
                shortened = None
                break
            header, passed = rows
            in_input = _input_rows(source_text, header, str(record.input.get("value_column") or ""))
            if in_input is None or passed >= in_input:
                shortened = None
                break
            shortened = Unverified(name, value, "table_incomplete", rows_passed=passed, rows_in_input=in_input)
        if shortened is not None:
            failed.append(shortened)
    return failed


def correction_prompt(prompt: str, failed: list[Unverified]) -> str:
    """The task again, with why each checked value was not accepted (the agent's one chance to correct it)."""
    lines: list[str] = []
    for item in failed:
        if item.reason == "table_incomplete":
            lines.append(
                f"- {item.field}: the table you passed to table_totals had {item.rows_passed} rows, the input has "
                f"{item.rows_in_input}. Pass the whole table unchanged."
            )
        elif item.reason == "missing":
            lines.append(f"- {item.field}: the value is missing. Compute it with a tool and report it.")
        else:
            lines.append(
                f"- {item.field}: {item.value} is not the result of a tool call. Compute it with a tool "
                "(table_totals for totals and shares of table rows, otherwise the calculator) and report exactly "
                "the tool's result, rounded to at most two decimals."
            )
    return (
        f"{prompt}\n\nYour previous answer was not accepted, because these values must be computed with a tool:\n"
        + "\n".join(lines)
    )
