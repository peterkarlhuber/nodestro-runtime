"""Static catalogs: models, server tools and the option lists the editor offers.

Texts here are English; the frontend shows its own translations by model id and tool key.

The model list is a suggestion, not a limit: every provider accepts any model id
(``provider:model``). Prices come from the workspace's :class:`~nodestro_runtime.pricing.PricingService`
(reference prices plus overrides); without one, from Orkexa's built-in prices, which know only
Anthropic's models. A model without a known price has no price fields and the status ``unknown``:
its cost is shown as unknown, never as zero.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from orkexa import MODEL_PRICING
from orkexa.pricing import BUILTIN_SNAPSHOT, PriceSnapshot
from pydantic import BaseModel, model_validator

from .tool_policy import UNKNOWN, ToolCapability, server_tool_capability

if TYPE_CHECKING:
    from .pricing import PricingService


class ModelInfo(BaseModel):
    #: The qualified name ``provider:model`` that agent configurations store.
    id: str
    provider: str
    #: The provider's own model id (the frontend translates labels by it).
    model: str
    label: str
    tier: str
    description: str
    #: Prices in USD per million tokens; ``None`` when Orkexa knows no price for the model.
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    cache_write_5m_usd_per_mtok: float | None = None
    cache_write_1h_usd_per_mtok: float | None = None
    cache_read_usd_per_mtok: float | None = None
    recommended: bool = False
    #: ``current``, ``stale``, ``pending_review``, ``custom`` (an individual price) or ``unknown`` (no price).
    price_status: str = "unknown"
    #: Orkexa's quality of the price (``verified``, ``contract``, ``unverified``, ``estimated``).
    price_quality: str | None = None
    price_source: str | None = None
    price_version: str | None = None


_MODEL_TEXT: dict[str, tuple[str, str, str]] = {
    "claude-opus-5-5": ("Claude Opus 5.5", "Opus", "Demanding reasoning, agentic and coding work"),
    "claude-sonnet-5-5": ("Claude Sonnet 5.5", "Sonnet", "Everyday agent work at lower cost"),
    "claude-haiku-5-5": ("Claude Haiku 5.5", "Haiku", "Fast and cheap: classifying, extracting"),
    "claude-fable-5-1": ("Claude Fable 5.1", "Fable", "The strongest model for the hardest tasks"),
    "claude-fable-5": ("Claude Fable 5", "Fable", "Predecessor of Fable 5.1"),
    "claude-opus-5": ("Claude Opus 5", "Opus", "Predecessor of Opus 5.5"),
    "claude-sonnet-5": ("Claude Sonnet 5", "Sonnet", "Predecessor of Sonnet 5.5"),
    "claude-opus-4-8": ("Claude Opus 4.8", "Opus", "Previous generation"),
    "claude-opus-4-7": ("Claude Opus 4.7", "Opus", "Previous generation"),
    "claude-opus-4-6": ("Claude Opus 4.6", "Opus", "Previous generation"),
    "claude-sonnet-4-6": ("Claude Sonnet 4.6", "Sonnet", "Previous generation"),
    "claude-haiku-4-5": ("Claude Haiku 4.5", "Haiku", "Previous generation"),
}

_RECOMMENDED = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1")

#: Example models of the other providers (the ones Orkexa's documentation uses); any other id works too.
_OTHER_MODELS: tuple[tuple[str, str, str, str, str], ...] = (
    ("openai", "gpt-5.5", "GPT-5.5", "GPT", "OpenAI model (example; any OpenAI model id can be entered)"),
    ("openai", "gpt-6.1-sol", "GPT-6.1 Sol", "GPT", "OpenAI model (example; any OpenAI model id can be entered)"),
    ("gemini", "gemini-3-pro", "Gemini 3 Pro", "Gemini", "Google model (example; any Gemini model id can be entered)"),
    (
        "gemini",
        "gemini-3.8-flash",
        "Gemini 3.8 Flash",
        "Gemini",
        "Google model (example; any Gemini model id can be entered)",
    ),
)


def _priced(
    provider: str,
    model_id: str,
    label: str,
    tier: str,
    description: str,
    recommended: bool,
    *,
    snapshot: PriceSnapshot,
    pricing: PricingService | None,
) -> ModelInfo:
    info = ModelInfo(
        id=f"{provider}:{model_id}",
        provider=provider,
        model=model_id,
        label=label,
        tier=tier,
        description=description,
        recommended=recommended,
    )
    price = snapshot.get(provider, model_id)
    if price is None:
        return info
    rates = price.rates
    info.input_usd_per_mtok = float(rates.input)
    info.output_usd_per_mtok = float(rates.output)
    if rates.cache_write_5m is not None:
        info.cache_write_5m_usd_per_mtok = float(rates.cache_write_5m)
    if rates.cache_write_1h is not None:
        info.cache_write_1h_usd_per_mtok = float(rates.cache_write_1h)
    if rates.cache_read is not None:
        info.cache_read_usd_per_mtok = float(rates.cache_read)
    info.price_quality = price.quality
    info.price_source = price.source
    if pricing is None:
        info.price_status = "current"
        info.price_version = snapshot.version
        return info
    entry = pricing.entry(provider, model_id)
    if entry is not None:
        info.price_status = entry.status
        info.price_version = entry.version
    return info


def models(pricing: PricingService | None = None) -> list[ModelInfo]:
    """The suggested models: Anthropic's (current generation first), then examples of other providers.

    Prices come from ``pricing`` (the workspace's catalog) or, without it, from Orkexa's built-in prices.
    """
    snapshot = pricing.effective_snapshot() if pricing is not None else BUILTIN_SNAPSHOT
    ordered = [*_RECOMMENDED, *(m for m in MODEL_PRICING if m not in _RECOMMENDED)]
    result = []
    for model_id in ordered:
        if model_id not in MODEL_PRICING:
            continue
        label, tier, description = _MODEL_TEXT.get(model_id, (model_id, "", ""))
        result.append(
            _priced(
                "anthropic",
                model_id,
                label,
                tier,
                description,
                model_id in _RECOMMENDED,
                snapshot=snapshot,
                pricing=pricing,
            )
        )
    for provider, model_id, label, tier, description in _OTHER_MODELS:
        result.append(_priced(provider, model_id, label, tier, description, True, snapshot=snapshot, pricing=pricing))
    return result


class ServerToolInfo(BaseModel):
    """A tool run by the provider itself; these are Anthropic's server tools."""

    key: str
    label: str
    description: str
    definition: dict[str, Any]
    #: What the tool may do (S-1). Server tools run at the provider and never pass Nodestro's per-call rules.
    capability: ToolCapability = UNKNOWN

    @model_validator(mode="before")
    @classmethod
    def _default_capability(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("capability") is None:
            data = {**data, "capability": server_tool_capability(str(data.get("key", "")))}
        return data


SERVER_TOOLS: dict[str, ServerToolInfo] = {
    info.key: info
    for info in (
        ServerToolInfo(
            key="web_search",
            label="Web search",
            description="Searches the web and cites its sources (run by Anthropic; $10 per 1,000 searches).",
            definition={"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
        ),
        ServerToolInfo(
            key="web_fetch",
            label="Web fetch",
            description="Loads the content of a web page or PDF from a URL.",
            definition={"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5},
        ),
        ServerToolInfo(
            key="code_execution",
            label="Code execution",
            description="Runs Python in a sandbox at Anthropic. Not together with web search or web fetch.",
            definition={"type": "code_execution_20260521", "name": "code_execution"},
        ),
    )
}
