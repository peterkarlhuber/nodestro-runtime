"""Output token limits of Nodestro's own AI operations (Workflow Architect, assistants, run chat, ...).

Agents carry their own ``max_tokens`` (``AgentConfig``). The operations here have no agent configuration; their limit
is resolved in three levels, the first that applies wins:

1. **Task-specific setting**: the workspace setting ``output_limits[<operation>]`` (Settings > Model output limits).
2. **Built-in default** of the operation (:data:`OPERATIONS`).
3. **Model maximum**: the value is then capped by the model's documented or declared maximum output
   (``ModelLimits.max_output_tokens``) and, where only a context window is known (local models), by half of it, so the
   prompt keeps room. A lowered value is never silent: :class:`EffectiveLimit` says so and why (``reduced``), and the
   callers log or show it.

``max_tokens`` covers the answer *and* the model's reasoning (thinking tokens count against it); the context window
is the total of input and output. The value goes to every provider under its own name (``max_tokens``,
``max_output_tokens``, ``maxTokens``, ``max_completion_tokens``; Ollama maps it to ``num_predict``), and the budget
pre-check reserves exactly this value (``orkexa.core.cost_bound``), so a higher limit is never a way around a budget.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from orkexa import ModelLimits

from .problems import NodestroError

OperationName = Literal[
    "architect_analysis",
    "architect_plan",
    "architect_agent",
    "architect_repair",
    "assistant_chat",
    "agent_design",
    "memory_proposal",
    "api_guide",
    "run_chat",
    "orchestrator",
    "quality_judge",
    "tool_generation",
]

#: The built-in output limit of each operation (tokens). The architect's plan holds the whole workflow (every agent
#: with its prompt and task), so it gets the most room; the earlier 16,000 cut off large workflows.
OPERATIONS: dict[str, int] = {
    "architect_analysis": 8_000,
    "architect_plan": 32_000,
    "architect_agent": 16_000,
    "architect_repair": 8_000,
    "assistant_chat": 16_000,
    "agent_design": 16_000,
    "memory_proposal": 8_000,
    "api_guide": 8_000,
    "run_chat": 4_000,
    "orchestrator": 8_000,
    "quality_judge": 8_000,
    "tool_generation": 16_000,
}

#: The range a setting may take: below 256 tokens no operation can answer, above 1,000,000 is a typo.
SETTING_RANGE = (256, 1_000_000)
#: How high the Workflow Architect may raise a step's limit when an answer was cut off (``architect_retry_max_tokens``).
ARCHITECT_RETRY_DEFAULT = 64_000


@dataclass(frozen=True)
class EffectiveLimit:
    """The limit an operation sends and how it came about."""

    operation: str
    #: The value sent as the model's output limit.
    value: int
    #: The configured value (setting or default) before the model's maximum was applied.
    configured: int
    #: ``setting`` (the workspace setting) or ``default`` (built in).
    source: Literal["setting", "default"]
    #: The model's maximum output (documented or declared; ``None``: unknown).
    model_maximum: int | None = None
    #: Why ``value`` is lower than ``configured``: ``model_maximum`` or ``context_window``; ``None`` when not lowered.
    reduced: str | None = None

    def params(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "max_tokens": self.value,
            "configured": self.configured,
            "source": self.source,
            "model_maximum": self.model_maximum,
            "reduced": self.reduced or "",
        }


def check_overrides(values: Mapping[str, Any]) -> dict[str, int]:
    """Validated overrides of a settings change: known operations, whole numbers in :data:`SETTING_RANGE`; ``None``
    or ``0`` removes an override (the default applies again)."""
    unknown = sorted(set(values) - set(OPERATIONS))
    if unknown:
        raise NodestroError(
            "settings.output_limit_unknown", f"Unknown operations: {', '.join(unknown)}", names=", ".join(unknown)
        )
    low, high = SETTING_RANGE
    result: dict[str, int] = {}
    for name, value in values.items():
        if value in (None, 0):
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise NodestroError(
                "settings.output_limit_invalid",
                f"The output limit of {name} must be a whole number from {low} to {high}",
                operation=name,
                min=low,
                max=high,
            )
        result[name] = value
    return result


def stored_overrides(values: Mapping[str, Any] | None) -> dict[str, int]:
    """The stored overrides, ignoring anything invalid (e.g. an imported value): the default applies instead."""
    result: dict[str, int] = {}
    low, high = SETTING_RANGE
    for name, value in (values or {}).items():
        if name in OPERATIONS and isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
            result[name] = value
    return result


def model_ceiling(limits: ModelLimits) -> tuple[int | None, str | None]:
    """The highest output a request to this model may ask for, and which limit sets it."""
    if limits.max_output_tokens is not None:
        return limits.max_output_tokens, "model_maximum"
    if limits.context_window is not None:
        return max(limits.context_window // 2, SETTING_RANGE[0]), "context_window"
    return None, None


def effective(operation: str, overrides: Mapping[str, int], limits: ModelLimits) -> EffectiveLimit:
    """The output limit ``operation`` sends to a model with ``limits`` (see the module docstring)."""
    if operation not in OPERATIONS:
        raise KeyError(operation)
    configured = overrides.get(operation)
    source: Literal["setting", "default"] = "setting" if configured is not None else "default"
    value = configured if configured is not None else OPERATIONS[operation]
    ceiling, why = model_ceiling(limits)
    if ceiling is not None and value > ceiling:
        return EffectiveLimit(operation, ceiling, value, source, limits.max_output_tokens, why)
    return EffectiveLimit(operation, value, value, source, limits.max_output_tokens)


def retry_limit(current: int, cap: int, limits: ModelLimits) -> int | None:
    """The next, higher limit for a response that was cut off: double, at most ``cap`` and the model's ceiling;
    ``None`` when no higher limit is allowed."""
    ceiling, _why = model_ceiling(limits)
    highest = min(cap, ceiling) if ceiling is not None else cap
    proposed = min(current * 2, highest)
    return proposed if proposed > current else None
