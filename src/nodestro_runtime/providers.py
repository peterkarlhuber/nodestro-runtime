"""The AI providers Nodestro offers and the connection an agent of one provider uses.

Nodestro runs every agent on Orkexa (``orkexa.Agent``). A model is named as Orkexa
names it, ``"provider:model"`` (``"openai:gpt-5.5"``, ``"ollama:llama3.1:8b"``); a name
without a registered provider prefix belongs to Anthropic, Orkexa's default provider, so
bare Claude model ids keep their meaning without being rewritten. New configurations store
the qualified form.

Credentials and endpoints are kept per provider (:class:`Connection`); a provider's key is
only ever given to agents of that provider. Nothing here falls back to another provider:
an agent whose provider is not configured fails with :class:`MissingApiKeyError`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import os
from typing import Any, Literal

from orkexa import AnthropicOptions, CompatibleOptions, GeminiOptions, ModelCapabilities, OpenAIOptions
from orkexa.dynamic import ProviderSettings
from orkexa.providers import ProviderConfig, create_provider, resolve_model, split_model
from orkexa.providers.base import redact
from pydantic import BaseModel, Field

from .problems import NodestroError

ProviderId = Literal["anthropic", "openai", "gemini", "compatible", "ollama", "vllm"]
#: The capability names of Orkexa that can be declared for OpenAI-compatible endpoints.
CAPABILITY_NAMES: tuple[str, ...] = (
    "tools",
    "forced_tool_choice",
    "parallel_tool_calls",
    "strict_tool_schemas",
    "structured_output",
    "streaming",
    "image_input",
    "document_input",
    "reasoning",
    "caching",
    "system_messages",
)
#: Retries of the SDK clients.
MAX_RETRIES = 3


class ProviderInfo(BaseModel):
    """What a provider needs and offers (static; the interface translates it by ``id``)."""

    id: ProviderId
    label: str
    #: Whether an API key is required (OpenAI-compatible servers usually need none).
    needs_key: bool
    #: The environment variables Orkexa's SDKs read for the key, in order of precedence.
    env_vars: list[str] = Field(default_factory=list)
    #: Whether a base URL is used; ``default_base_url`` is Orkexa's default for the preset.
    base_url: bool = False
    default_base_url: str | None = None
    #: Capabilities are ``unknown`` until declared (OpenAI-compatible endpoints).
    declare_capabilities: bool = False
    #: An example model id (from the Orkexa documentation), shown as a placeholder.
    example_model: str = ""


PROVIDERS: dict[str, ProviderInfo] = {
    info.id: info
    for info in (
        ProviderInfo(
            id="anthropic",
            label="Anthropic",
            needs_key=True,
            env_vars=["ANTHROPIC_API_KEY"],
            example_model="claude-sonnet-5-5",
        ),
        ProviderInfo(id="openai", label="OpenAI", needs_key=True, env_vars=["OPENAI_API_KEY"], example_model="gpt-5.5"),
        ProviderInfo(
            id="gemini",
            label="Google Gemini",
            needs_key=True,
            env_vars=["GEMINI_API_KEY", "GOOGLE_API_KEY"],
            example_model="gemini-3-pro",
        ),
        ProviderInfo(
            id="compatible",
            label="OpenAI-compatible endpoint",
            needs_key=False,
            base_url=True,
            declare_capabilities=True,
            example_model="my-model",
        ),
        ProviderInfo(
            id="ollama",
            label="Ollama",
            needs_key=False,
            base_url=True,
            default_base_url="http://localhost:11434/v1",
            declare_capabilities=True,
            example_model="llama3.1:8b",
        ),
        ProviderInfo(
            id="vllm",
            label="vLLM",
            needs_key=False,
            base_url=True,
            default_base_url="http://localhost:8000/v1",
            declare_capabilities=True,
            example_model="my-model",
        ),
    )
}


class MissingApiKeyError(NodestroError):
    """The provider of an agent is not configured (no API key, no endpoint)."""

    def __init__(self, provider: str) -> None:
        info = PROVIDERS.get(provider)
        label = info.label if info is not None else provider
        if info is not None and info.base_url and not info.needs_key:
            super().__init__(
                "provider.not_configured",
                f"{label} is not configured: enter its base URL in the settings",
                provider=provider,
            )
        else:
            variables = " or ".join(info.env_vars) if info is not None else ""
            super().__init__(
                "api_key_missing",
                f"No API key for {label}: set {variables} or enter it in the settings",
                provider=provider,
            )


# ---- model names ------------------------------------------------------------------------


def split(model: str) -> tuple[str, str]:
    """``(provider, model id)`` of a model name; a name without provider prefix is Anthropic's."""
    try:
        return resolve_model(model)
    except ValueError as exc:
        raise NodestroError("config.model_invalid", str(exc), model=model) from None


def qualify(model: str) -> str:
    """The qualified name ``provider:model``; unchanged when it is qualified already."""
    provider, model_id = split(model)
    return f"{provider}:{model_id}"


def model_id(model: str) -> str:
    """The provider's own id of a model (without the prefix)."""
    return split(model)[1]


def provider_of(model: str) -> str:
    return split(model)[0]


def check_model(model: str) -> str:
    """The qualified name of a model Nodestro can run; raises for unknown providers and empty ids."""
    if not model.strip():
        raise NodestroError("config.model_missing", "Choose a model")
    prefix, _rest = split_model(model)
    provider, model_name = split(model)
    if provider not in PROVIDERS:
        raise NodestroError("provider.unknown", f"Unknown provider {provider!r}", provider=provider)
    if not model_name.strip():
        raise NodestroError("config.model_missing", "Choose a model")
    if prefix is None and ":" in model:
        # "llama3.1:8b" without a provider would be sent to Anthropic: name the provider instead.
        raise NodestroError(
            "config.model_provider_missing",
            f"Model {model!r} has no provider; write it as provider:model, e.g. ollama:{model}",
            model=model,
        )
    return f"{provider}:{model_name}"


# ---- connections ------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class Connection:
    """How agents of one provider reach it: key, endpoint, declared capabilities or a client.

    ``client`` replaces the SDK client (tests and scripted e2e servers); the key is never
    shown in ``repr``.
    """

    provider: str
    api_key: str | None = None
    base_url: str | None = None
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    client: Any = None

    def __repr__(self) -> str:
        base_url = redact(self.base_url) if self.base_url else None
        return f"Connection(provider={self.provider!r}, base_url={base_url!r}, key={'set' if self.api_key else None})"

    def agent_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``orkexa.Agent``/``Evaluator``: provider, credentials, endpoint, overrides."""
        kwargs: dict[str, Any] = {"provider": self.provider}
        if self.client is not None:
            kwargs["client"] = self.client
        else:
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.base_url:
                kwargs["base_url"] = self.base_url
        if self.capabilities:
            kwargs["capabilities"] = dict(self.capabilities)
        return kwargs

    def options(self, request_options: Mapping[str, Any] | None = None) -> Any:
        """The provider's default Orkexa options (with ``request_options``)."""
        extra = dict(request_options or {})
        if self.provider == "anthropic":
            return AnthropicOptions(request_options=extra)
        if self.provider == "openai":
            return OpenAIOptions(request_options=extra)
        if self.provider == "gemini":
            return GeminiOptions(request_options=extra)
        if self.provider in ("compatible", "ollama", "vllm"):
            return CompatibleOptions(request_options=extra)
        return None

    def settings(self) -> ProviderSettings:
        """The connection as ``orkexa.dynamic.ProviderSettings`` (Coding Agent, Agent Architect).

        The options are always given, so the dynamic components build native ``orkexa.Agent``
        objects for every provider.
        """
        kwargs = self.agent_kwargs()
        return ProviderSettings(
            api_key=kwargs.get("api_key"),
            base_url=kwargs.get("base_url"),
            client=kwargs.get("client"),
            options=self.options(),
            capabilities=kwargs.get("capabilities"),
        )

    def model_capabilities(self, model: str) -> ModelCapabilities:
        """What ``model`` supports through this connection (Orkexa's values plus declared overrides)."""
        info = PROVIDERS.get(self.provider)
        default = info.default_base_url if info is not None else None
        config = ProviderConfig(base_url=self.base_url or default, client=object())
        adapter = create_provider(self.provider, config)
        return adapter.capabilities(model_id(model)).merged(dict(self.capabilities) or None)

    def effort(self, model: str, wanted: Any) -> Any:
        """``wanted`` when the model supports a reasoning effort, else ``None`` (internal assistants)."""
        return wanted if self.model_capabilities(model).supports("reasoning") else None


def env_key(provider: str, environ: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """The API key from the environment and the variable it came from.

    Endpoint presets (``compatible``, ``ollama``, ``vllm``) read ``<PROVIDER>_API_KEY``.
    """
    env = os.environ if environ is None else environ
    info = PROVIDERS.get(provider)
    variables = (info.env_vars or [f"{provider.upper()}_API_KEY"]) if info is not None else []
    for variable in variables:
        value = env.get(variable)
        if value:
            return value, variable
    return None, None


def base_url_variable(provider: str) -> str:
    """The environment variable of an endpoint's base URL in exported projects, e.g. ``OLLAMA_BASE_URL``."""
    return f"{provider.upper()}_BASE_URL"


def has_credentials(url: str) -> bool:
    """Whether a URL carries credentials (``user:password@`` or a key parameter); such URLs are not exported."""
    return redact(url) != url


def environment_connection(
    provider: str,
    *,
    environ: Mapping[str, str] | None = None,
    base_urls: Mapping[str, str] | None = None,
    capabilities: Mapping[str, Mapping[str, Any]] | None = None,
    client: Any = None,
) -> Connection:
    """A connection from environment variables only (exported projects).

    Keys come from the provider's variables (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``,
    ``GEMINI_API_KEY``/``GOOGLE_API_KEY``, ``<PROVIDER>_API_KEY`` for endpoints); a base URL
    from ``<PROVIDER>_BASE_URL``, else the exported one, else Orkexa's preset default.
    """
    info = PROVIDERS.get(provider)
    if info is None:
        raise NodestroError("provider.unknown", f"Unknown provider {provider!r}", provider=provider)
    env = os.environ if environ is None else environ
    key = env_key(provider, env)[0]
    base_url = None
    if info.base_url:
        base_url = env.get(base_url_variable(provider)) or (base_urls or {}).get(provider) or info.default_base_url
    if client is None and ((info.needs_key and not key) or (info.base_url and not base_url)):
        raise MissingApiKeyError(provider)
    return Connection(
        provider=provider,
        api_key=key,
        base_url=base_url,
        capabilities=dict((capabilities or {}).get(provider) or {}),
        client=client,
    )


# ---- settings ---------------------------------------------------------------------------


class ProviderSettingsIn(BaseModel):
    """A change of one provider's settings: ``None`` keeps a value, an empty text removes it."""

    api_key: str | None = None
    base_url: str | None = None
    #: Declared capabilities of an OpenAI-compatible endpoint (replaces the stored ones).
    capabilities: dict[str, Literal["supported", "unsupported", "unknown"]] | None = None


class ProviderStatus(BaseModel):
    """A provider as the settings page shows it; never contains the key itself."""

    id: str
    label: str
    configured: bool
    #: Where the key comes from: ``environment`` (with ``env_var``), ``settings``, ``none`` or ``not_needed``.
    key_source: Literal["environment", "settings", "none", "not_needed"]
    env_var: str | None = None
    #: The stored key, also when the environment overrides it.
    key_stored: bool = False
    base_url: str | None = None
    base_url_default: str | None = None
    capabilities: dict[str, str] = Field(default_factory=dict)


def provider_status(
    provider: str, stored: Mapping[str, Any], environ: Mapping[str, str] | None = None
) -> ProviderStatus:
    """``environ``: where keys from the environment are looked for (default: ``os.environ``; a server's tenants
    pass ``{}``, because the server's own variables belong to no tenant)."""
    info = PROVIDERS[provider]
    key, variable = env_key(provider, environ)
    stored_key = bool(stored.get("api_key"))
    if variable is not None:
        source: Literal["environment", "settings", "none", "not_needed"] = "environment"
    elif stored_key:
        source = "settings"
    else:
        source = "none" if info.needs_key else "not_needed"
    base_url = stored.get("base_url") or None
    # An endpoint counts once its base URL is saved (Orkexa's default is only a suggestion), so no
    # local server is used that the user never chose.
    configured = (key is not None or stored_key) if info.needs_key else bool(base_url)
    return ProviderStatus(
        id=provider,
        label=info.label,
        configured=configured,
        key_source=source,
        env_var=variable,
        key_stored=stored_key,
        # Credentials in a stored URL (``user:password@``, ``?api_key=``) are never shown.
        base_url=redact(base_url) if base_url else None,
        base_url_default=info.default_base_url,
        capabilities=dict(stored.get("capabilities") or {}),
    )


def check_base_url(url: str) -> str:
    url = url.strip()
    if url and not url.startswith(("http://", "https://")):
        shown = redact(url)
        raise NodestroError(
            "provider.base_url_invalid", f"Invalid base URL {shown!r}: use http:// or https://", url=shown
        )
    if has_credentials(url) or "[REDACTED]" in url:
        raise NodestroError(
            "provider.base_url_credentials",
            "The base URL must not contain credentials: enter the key in the API key field",
        )
    return url.rstrip("/")


def endpoint_origin(url: str) -> str:
    """``scheme://host[:port]`` of a base URL, lower-case (how an allowlist of model endpoints names it)."""
    from urllib.parse import urlsplit

    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    scheme = parts.scheme.lower()
    if (scheme, port) in (("http", 80), ("https", 443)):
        port = None
    return f"{scheme}://{host}" + (f":{port}" if port is not None else "")


def check_endpoint_allowed(url: str, allowed: Iterable[str] | None) -> None:
    """Refuse a model endpoint the server's operator did not allow (``allowed``: origins; ``None``: any, desktop).

    A server sends requests to a tenant's endpoint from inside its network; without this check a tenant could make
    it reach internal services (server-side request forgery)."""
    if allowed is None:
        return
    origins = {endpoint_origin(item) for item in allowed if item.strip()}
    if endpoint_origin(url) not in origins:
        shown = redact(url)
        raise NodestroError(
            "provider.endpoint_not_allowed",
            f"The model endpoint {shown!r} is not allowed on this server; its operator lists the allowed endpoints",
            url=shown,
        )


def check_capabilities(values: Mapping[str, str]) -> dict[str, str]:
    unknown = sorted(set(values) - set(CAPABILITY_NAMES))
    if unknown:
        raise NodestroError(
            "provider.capability_unknown", f"Unknown capabilities: {', '.join(unknown)}", names=", ".join(unknown)
        )
    return {name: value for name, value in values.items() if value != "unknown"}
