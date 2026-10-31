"""Messages for the user: a stable code with parameters plus the English technical text.

The backend never writes user-facing prose in a particular UI language. Every
error, validation issue and status message carries a ``code`` (e.g.
``trace.not_json``) and ``params`` that the frontend turns into a sentence in
the selected language. ``message`` is the technical original in English; the
UI shows it under "technical details" and uses it when it does not know a code.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

Params = dict[str, Any]


class Problem(BaseModel):
    code: str
    params: Params = Field(default_factory=dict)
    message: str


class NodestroError(ValueError):
    """A request that cannot be carried out; reported as HTTP 400 with its code."""

    def __init__(self, code: str, message: str, **params: Any) -> None:
        super().__init__(message)
        self.code = code
        self.params = params

    @property
    def problem(self) -> Problem:
        return Problem(code=self.code, params=self.params, message=str(self))


class CheckIssue(Problem):
    """A finding of the workflow check, attributed to a node or a connection where possible."""

    level: Literal["error", "warning", "info"] = "error"
    node_id: str | None = None
    edge_id: str | None = None


def problem(code: str, message: str, **params: Any) -> Problem:
    return Problem(code=code, params=params, message=message)


# The library's own validation messages (orkexa, English) mapped to codes, so the editor
# can explain them in the UI language. Unknown messages keep the generic code "library".
_LIBRARY_PATTERNS: list[tuple[re.Pattern[str], str, tuple[str, ...]]] = [
    (re.compile(r"^(?P<model>\S+) rejects a forced tool_choice"), "config.forced_tool_choice", ("model",)),
    (re.compile(r"^(?P<model>\S+) cannot disable thinking"), "config.thinking_not_disableable", ("model",)),
    (re.compile(r"^(?P<model>\S+) disables thinking only at effort"), "config.thinking_disable_effort", ("model",)),
    (
        re.compile(r"^thinking='between_tools' is accepted only by (?P<model>\S+)"),
        "config.between_tools_model",
        ("model",),
    ),
    (re.compile(r"^thinking='between_tools' is accepted only at effort"), "config.between_tools_effort", ()),
    (re.compile(r"^thinking_display cannot be combined"), "config.thinking_display_conflict", ()),
    (re.compile(r"^(?P<model>\S+) does not accept system messages"), "config.no_system_messages", ("model",)),
    (
        re.compile(r"^Set these via constructor arguments, not request_options: (?P<keys>.+)$"),
        "config.reserved_request_options",
        ("keys",),
    ),
    (re.compile(r"^Duplicate tool name: (?P<name>.+)$"), "config.duplicate_tool", ("name",)),
    (re.compile(r"^max_tokens and max_parallel_tools must be positive"), "config.limits_invalid", ()),
    (re.compile(r"^Invalid tool_choice"), "config.tool_choice_invalid", ()),
]


_PRICED_MODEL = re.compile(r"\b(?:to|for) (?P<model>[\w.-]+:[\w./@-]+?)[\s;:,)]")


def price_problem(exc: BaseException, message: str | None = None) -> Problem | None:
    """The strict price mode's refusals (Orkexa's ``UnpricedModelError`` and ``UnreliablePriceError``).

    They are subclasses of ``BudgetExceededError`` but are not about a used-up budget: the model has no known price,
    or its price failed the price policy (for example a price that was not checked for too long).
    """
    from orkexa.dynamic import UnpricedModelError, UnreliablePriceError

    if not isinstance(exc, UnpricedModelError):
        return None
    text = message if message is not None else str(exc)
    match = _PRICED_MODEL.search(f"{exc} ")
    model = match.group("model") if match else ""
    if isinstance(exc, UnreliablePriceError):
        return problem("pricing.price_unreliable", text, model=model)
    return problem("pricing.price_unknown", text, model=model)


def library_problem(exc: BaseException, *, code: str = "library") -> Problem:
    """A problem for an exception raised by orkexa (or a NodestroError)."""
    from orkexa import UnknownCapabilityError, UnsupportedCapabilityError

    if isinstance(exc, NodestroError):
        return exc.problem
    priced = price_problem(exc)
    if priced is not None:
        return priced
    message = str(exc.args[0] if isinstance(exc, KeyError) and exc.args else exc)
    for pattern, matched_code, names in _LIBRARY_PATTERNS:
        match = pattern.search(message)
        if match:
            return problem(matched_code, message, **{name: match.group(name) for name in names})
    # A feature the provider or model does not support (or that must be declared for an endpoint).
    if isinstance(exc, UnknownCapabilityError):
        return problem("config.capability_unknown", message, capability=exc.capability or "")
    if isinstance(exc, UnsupportedCapabilityError):
        return problem("config.capability_unsupported", message, capability=exc.capability or "")
    return problem(code, message)
