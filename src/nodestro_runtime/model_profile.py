"""What one model offers an agent: capabilities, token limits and which agent options apply.

The editor shows the agent's options by what the chosen model really supports instead of by provider name, and the
agent builder refuses the same options with the same reasons. Everything comes from Orkexa (``ModelCapabilities``,
``ModelLimits``), the provider's declared values (OpenAI-compatible endpoints) and, for local models, what the local
model server reports; an unknown value stays unknown and is never guessed.

The four kinds of tools stay apart: **provider-native tools** (run at the provider, e.g. Anthropic's web search;
:data:`~nodestro_runtime.catalog.SERVER_TOOLS`), the model's **tool calling** (capability ``tools``), **Nodestro tools**
(the tool library, run by Nodestro under its approvals and sandbox rules) and **MCP tools** (Nodestro tools whose calls
go to an MCP server, under the same approvals). Only the first depends on the provider beyond tool calling.
"""

from __future__ import annotations

from typing import Any, Literal

from orkexa import ModelCapabilities, ModelLimits
from pydantic import BaseModel, Field

#: Agent options that depend on the model, with the value that means "not used".
OPTION_DEFAULTS: dict[str, Any] = {
    "thinking": None,
    "thinking_display": None,
    "thinking_budget": None,
    "cache_control": True,
    "cache_ttl": "5m",
    "fallbacks": [],
}

Support = Literal["supported", "unsupported", "unknown"]


class OptionSupport(BaseModel):
    """Whether an agent option can be used with a model; ``reason`` says why not (a code the editor translates).

    Reasons: ``provider`` (the provider's API has no such setting), ``model`` (the model does not accept it),
    ``automatic`` (the provider does it by itself, e.g. prompt caching at OpenAI and Gemini), ``not_mapped`` (the
    provider has a similar feature that Nodestro does not offer as this option), ``unknown`` (not documented for this
    model)."""

    available: bool
    reason: str | None = None
    #: The values the model accepts, where the option has a fixed set (``thinking``).
    values: list[str] | None = None


class NativeToolSupport(BaseModel):
    """A provider-native tool (run by the provider, never by Nodestro) and whether this model can use it."""

    key: str
    available: bool
    #: ``provider`` (only for other providers), ``not_integrated`` (the provider has such a tool, but Nodestro does not
    #: offer it: its fees cannot be priced and limited yet), ``capability`` (the model has no provider-native tools).
    reason: str | None = None


class ModelLimitsInfo(BaseModel):
    """:class:`orkexa.ModelLimits` for the interface (``None``: unknown)."""

    context_window: int | None = None
    max_output_tokens: int | None = None
    efforts: list[str] | None = None
    thinking_modes: list[str] | None = None
    thinking_budget: bool | None = None
    #: ``documented``, ``declared`` (provider settings or the local model server) or ``unknown``.
    source: str = "unknown"


class ModelProfile(BaseModel):
    """Capabilities, limits and agent options of one model as the workspace runs it."""

    #: The qualified model id (``provider:model``), or the given text when it is invalid.
    model: str
    provider: str | None = None
    #: Why the id cannot be used (an error code, e.g. ``provider.unknown``), else ``None``.
    error: str | None = None
    capabilities: dict[str, Support] = Field(default_factory=dict)
    #: Provider explanations of unsupported capabilities (English).
    reasons: dict[str, str] = Field(default_factory=dict)
    limits: ModelLimitsInfo = Field(default_factory=ModelLimitsInfo)
    #: The highest ``max_tokens`` an agent of this model may use: the model's documented or declared maximum, else
    #: the configuration bound.
    max_tokens_limit: int
    options: dict[str, OptionSupport] = Field(default_factory=dict)
    native_tools: list[NativeToolSupport] = Field(default_factory=list)


def limits_info(limits: ModelLimits) -> ModelLimitsInfo:
    return ModelLimitsInfo(
        context_window=limits.context_window,
        max_output_tokens=limits.max_output_tokens,
        efforts=list(limits.efforts) if limits.efforts is not None else None,
        thinking_modes=list(limits.thinking_modes) if limits.thinking_modes is not None else None,
        thinking_budget=limits.thinking_budget,
        source=limits.source,
    )


def option_support(provider: str, caps: ModelCapabilities, limits: ModelLimits) -> dict[str, OptionSupport]:
    """Which model-dependent agent options apply to a model of ``provider`` (see :data:`OPTION_DEFAULTS`).

    Decided by capabilities and documented settings, not by the provider's name alone: a provider-specific option
    is offered wherever Orkexa maps it (``thinking`` where the model has a thinking switch, ``thinking_budget`` where
    it takes a token budget), and refused with a reason everywhere else."""
    modes = limits.thinking_modes
    if modes:
        thinking = OptionSupport(available=True, values=list(modes))
    elif modes is None:
        # A Claude id Orkexa does not document (a newer model): the Messages API option stays usable, unchecked.
        thinking = (
            OptionSupport(available=True)
            if provider == "anthropic"
            else OptionSupport(available=False, reason="provider")
        )
    else:
        thinking = OptionSupport(available=False, reason="model" if provider == "anthropic" else "provider")
    display = (
        OptionSupport(available=True)
        if (modes and "adaptive" in modes) or (modes is None and provider == "anthropic")
        else OptionSupport(available=False, reason=thinking.reason or "model")
    )
    if limits.thinking_budget:
        budget = OptionSupport(available=True)
    else:
        budget = OptionSupport(
            available=False, reason="model" if provider == "gemini" and limits.thinking_budget is False else "provider"
        )
    if provider in ("anthropic", "bedrock"):
        # Bedrock: cache points, mapped by Orkexa (BedrockOptions.prompt_caching) for the models that support them.
        cache = OptionSupport(available=caps.supports("caching"), reason=None if caps.supports("caching") else "model")
    elif provider in ("openai", "gemini"):
        cache = OptionSupport(available=False, reason="automatic")
    else:
        cache = OptionSupport(available=False, reason="provider")
    fallbacks = (
        OptionSupport(available=True) if provider == "anthropic" else OptionSupport(available=False, reason="provider")
    )
    return {
        "thinking": thinking,
        "thinking_display": display,
        "thinking_budget": budget,
        "cache_control": cache,
        "cache_ttl": cache,
        "fallbacks": fallbacks,
    }


def native_tool_support(provider: str, caps: ModelCapabilities) -> list[NativeToolSupport]:
    """Each provider-native tool of the catalog for a model of ``provider``.

    OpenAI's and Gemini's own web search exist and Orkexa passes such tools through, but their fees are not in the
    price catalog: the cost of a run could not be estimated or limited, so Nodestro does not offer them
    (``not_integrated``) instead of running them unpriced."""
    from .catalog import SERVER_TOOLS

    found: list[NativeToolSupport] = []
    for key, info in SERVER_TOOLS.items():
        if provider in info.providers:
            supported = caps.supports("server_tools")
            found.append(NativeToolSupport(key=key, available=supported, reason=None if supported else "capability"))
        elif provider in info.unpriced_providers:
            found.append(NativeToolSupport(key=key, available=False, reason="not_integrated"))
        else:
            found.append(NativeToolSupport(key=key, available=False, reason="provider"))
    return found


def unavailable_options(config: Any, support: dict[str, OptionSupport]) -> list[tuple[str, OptionSupport]]:
    """The options ``config`` (an ``AgentConfig``) sets although the model cannot use them at all. A value the model
    does not accept (e.g. ``thinking="disabled"`` on Claude Opus 5.5) is left to Orkexa, whose checks name the exact
    rule (``config.thinking_not_disableable``, ``config.between_tools_model``)."""
    found: list[tuple[str, OptionSupport]] = []
    for name, default in OPTION_DEFAULTS.items():
        if getattr(config, name) != default and not support[name].available:
            found.append((name, support[name]))
    return found
