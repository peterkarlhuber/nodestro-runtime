"""Deterministic decisions on results: the rules of decision nodes and test checks.

A rule compares one value of a result (``field`` is a path such as ``category`` or
``customer.tier``; empty means the whole result) with a configured value. Evaluating
never calls the API and never guesses: a rule returns ``True`` or ``False`` when the
comparison is well defined, and ``None`` when it is not (missing field, a number
operator on a text, an unreadable result). The decision node then follows its
configured behaviour for invalid data (default branch or failure), so the outcome
is always explainable.

The same rules check the results of workflow test series (:mod:`nodestro_api.benchmarks`).
"""

from __future__ import annotations

from datetime import date
import json
import re
from typing import Any

from .schemas import ConditionConfig, ConditionRule, DecisionRecord

#: Marks a field that does not exist (as opposed to an existing ``None``).
MISSING = object()
DEFAULT_BRANCH = "default"
APPROVED = "approved"
REJECTED = "rejected"

_TRUE = {"true", "yes", "ja", "sí", "si", "oui", "sim", "1", "y", "wahr"}
_FALSE = {"false", "no", "nein", "non", "não", "nao", "0", "n", "falsch"}
_NUMBER = re.compile(r"^\s*[-+]?\d+(?:[.,]\d+)?\s*$")
_DATE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")


def parse_result(value: Any) -> Any:
    """A result as data: a text that is a JSON object or list (also in a code fence) is parsed."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    if text[:1] in "{[":
        try:
            return json.loads(text)
        except ValueError:
            return value
    return value


def lookup(value: Any, path: str) -> Any:
    """The value at ``path`` (dots separate keys and list positions); ``MISSING`` if absent."""
    current = parse_result(value)
    if not path.strip():
        return current
    for part in path.strip().split("."):
        part = part.strip()
        if isinstance(current, dict):
            if part in current:
                current = current[part]
                continue
            # Field names are matched without regard to case, as users type them.
            folded = {str(key).casefold(): key for key in current}
            if part.casefold() not in folded:
                return MISSING
            current = current[folded[part.casefold()]]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return MISSING
    return current


def as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and _NUMBER.match(value):
        return float(value.strip().replace(",", "."))
    return None


def as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().casefold() in _TRUE | _FALSE:
        return value.strip().casefold() in _TRUE
    return None


def as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        match = _DATE.match(value)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                return None
    return None


def _text(value: Any, case_sensitive: bool) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    text = "" if value is None else str(value).strip()
    return text if case_sensitive else text.casefold()


def categories(value: Any) -> list[str]:
    """The categories of an ``in``/``not_in`` rule: a list, or a comma-separated text."""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _empty(actual: Any) -> bool:
    return actual is MISSING or actual is None or actual == "" or actual == [] or actual == {}


def evaluate(rule: ConditionRule, result: Any) -> bool | None:
    """Whether ``rule`` holds for ``result``; ``None`` when it cannot be decided."""
    actual = lookup(result, rule.field)
    operator = rule.operator
    if operator == "is_empty":
        return _empty(actual) or (isinstance(actual, str) and not actual.strip())
    if operator == "is_not_empty":
        return not (_empty(actual) or (isinstance(actual, str) and not actual.strip()))
    if actual is MISSING or actual is None:
        return None
    sensitive = rule.case_sensitive
    if operator in ("is_true", "is_false"):
        flag = as_bool(actual)
        return None if flag is None else (flag if operator == "is_true" else not flag)
    if operator in ("gt", "gte", "lt", "lte"):
        left, right = as_number(actual), as_number(rule.value)
        if left is None or right is None:
            day, other = as_date(actual), as_date(rule.value)
            if day is None or other is None:
                return None
            left, right = float(day.toordinal()), float(other.toordinal())
        return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[operator]
    if operator in ("equals", "not_equals"):
        if isinstance(actual, (list, dict)):
            return None
        same: bool
        if isinstance(actual, bool) or isinstance(rule.value, bool):
            left_flag, right_flag = as_bool(actual), as_bool(rule.value)
            if left_flag is None or right_flag is None:
                return None
            same = left_flag == right_flag
        elif as_number(actual) is not None and as_number(rule.value) is not None:
            same = as_number(actual) == as_number(rule.value)
        else:
            same = _text(actual, sensitive) == _text(rule.value, sensitive)
        return same if operator == "equals" else not same
    if operator in ("contains", "not_contains"):
        needle = _text(rule.value, sensitive)
        if isinstance(actual, list):
            found = any(_text(item, sensitive) == needle for item in actual)
        else:
            found = needle in _text(actual, sensitive)
        return found if operator == "contains" else not found
    if operator in ("starts_with", "ends_with"):
        if isinstance(actual, (list, dict)):
            return None
        text, part = _text(actual, sensitive), _text(rule.value, sensitive)
        return text.startswith(part) if operator == "starts_with" else text.endswith(part)
    if operator in ("in", "not_in"):
        allowed = {_text(item, sensitive) for item in categories(rule.value)}
        if isinstance(actual, dict):
            return None
        values = actual if isinstance(actual, list) else [actual]
        found = any(_text(item, sensitive) in allowed for item in values)
        return found if operator == "in" else not found
    return None


def _short(value: Any) -> Any:
    if value is MISSING:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > 500:
        return text[:500] + "…"
    return value


def decide(config: ConditionConfig, result: Any, *, available: bool = True) -> DecisionRecord:
    """Which branches a decision node opens for ``result``.

    Raises nothing: invalid data is reported in ``fallback_code``; the caller decides
    whether that means the default branch or a failure (``config.on_invalid``).
    """
    record = DecisionRecord()
    if not available:
        record.fallback_code = "condition.no_input"
        record.branches = [DEFAULT_BRANCH]
        record.labels = [config.default_label]
        return record
    parsed = parse_result(result)
    if len(config.rules) == 1 or not config.rules:
        record.value = _short(lookup(parsed, config.rules[0].field) if config.rules else parsed)
    else:
        fields = {rule.field for rule in config.rules}
        record.value = _short(lookup(parsed, next(iter(fields)))) if len(fields) == 1 else _short(parsed)
    undecidable: list[ConditionRule] = []
    for rule in config.rules:
        outcome = evaluate(rule, parsed)
        record.results[rule.id] = outcome
        if outcome is None:
            undecidable.append(rule)
        elif outcome:
            record.branches.append(rule.id)
            record.labels.append(rule.label)
            if config.mode == "first":
                break
    if not record.branches:
        record.branches = [DEFAULT_BRANCH]
        record.labels = [config.default_label]
        if undecidable:
            rule = undecidable[0]
            missing = lookup(parsed, rule.field) is MISSING
            record.fallback_code = "condition.field_missing" if missing else "condition.not_evaluable"
            record.fallback_params = {"field": rule.field or "-", "rule": rule.label or rule.id}
        else:
            record.fallback_code = "condition.no_match"
    return record


def rule_problems(rule: ConditionRule) -> list[tuple[str, str]]:
    """Configuration errors of one rule: ``(code, English message)``."""
    found: list[tuple[str, str]] = []
    operator = rule.operator
    if operator in ("is_empty", "is_not_empty", "is_true", "is_false"):
        return found
    if operator in ("in", "not_in"):
        if not categories(rule.value):
            found.append(("condition.rule_no_categories", "The rule lists no categories"))
        return found
    if rule.value is None or (isinstance(rule.value, str) and not rule.value.strip()):
        found.append(("condition.rule_no_value", "The rule has no value to compare with"))
    elif operator in ("gt", "gte", "lt", "lte") and as_number(rule.value) is None and as_date(rule.value) is None:
        found.append(
            ("condition.rule_not_number", "The rule compares with a value that is neither a number nor a date")
        )
    return found
