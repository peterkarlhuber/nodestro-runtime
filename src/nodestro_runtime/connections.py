"""Service connections: one model for every access to an outside service (ADR-0001, NAL-A1, owner decision E-2).

A :class:`ServiceConnection` is what a built-in tool (and later a database tool of CR-DB) needs to reach a service:
its address and settings, which hosts it may talk to (``egress``) and one secret. Every kind of service is a
*connection type* registered here (:func:`register_connection_type`); this module ships ``search``, ``smtp`` and
``webhook``, and the type ``database`` is reserved for CR-DB, which registers it. There is no second connection model:
the contract (fields, secret rules, tenant binding, egress, versioning) is written down in
``docs/adr/0001-tool-specs-and-service-connections.md`` and tested in ``tests/test_connections_contract.py``.

Secrets:

- A secret is written only (:meth:`ConnectionService.set_secret`) and never returned by any read, view, event,
  log line or export. Tools get it in memory through :meth:`ConnectionService.resolve`.
- It is stored only encrypted, in the same token vault as MCP tokens (:class:`~nodestro_runtime.mcp.vault.TokenVault`:
  the tenant's ``SecretBox`` on a server, the keychain key of the desktop app). The encryption is bound to the tenant
  and to the connection id, so a sealed value copied to another tenant or connection cannot be opened.
- Without a key store the secret lives only in this process's memory (``session``), never in plain text on disk.

Collections (one store per tenant, so tenants never see each other's entries):

``service_connections``  key = connection id
``connection_events``    key = ``<time>_<event id>`` (administration events; never secrets or addresses with queries)
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import ipaddress
import logging
import re
import threading
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .net_address import METADATA_HOSTS, address_problem, url_secret_problem
from .platform_events import platform_events
from .problems import NodestroError
from .schemas import new_id, now

logger = logging.getLogger(__name__)

#: The version of the connection contract (ADR-0001). It changes only when stored connections or callers would break;
#: additive changes (a new optional field, a new type) keep it.
CONTRACT_VERSION = 1

CONNECTIONS = "service_connections"
EVENTS = "connection_events"
#: Kept per tenant; older events are dropped.
MAX_EVENTS = 2000
#: Types other work packages register; creating one before it is registered is refused
#: (``connection.type_unavailable``).
RESERVED_TYPES: frozenset[str] = frozenset({"database"})
#: The longest secret accepted (API keys, passwords, signing keys; not files).
MAX_SECRET_LENGTH = 8192

SecretStorage = Literal["none", "vault", "session"]
CheckStatus = Literal["unchecked", "ok", "failed"]
EventKind = Literal["created", "updated", "deleted", "secret_set", "secret_cleared", "checked"]
Mode = Literal["desktop", "local-container", "server"]

_TYPE_NAME = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*$")
_EMAIL = re.compile(r"^[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,253}$")
_LOOPBACK_NAMES = frozenset({"localhost"})
#: The form of a connection id (``new_id("conn")``); releases and exported packages accept no other.
CONNECTION_ID = re.compile(r"^conn_[0-9a-f]{12}$")
#: A last label of digits or a ``0x`` number: an IPv4 address in a form ``ipaddress`` refuses but resolvers take
#: (``127.1``, ``2130706433``, ``0x7f000001``, ``0177.0.0.1``).
_NUMERIC_LABEL = re.compile(r"^(?:[0-9]+|0x[0-9a-f]*)$")


# ---- settings of the shipped types --------------------------------------------------------------------------------


class _Settings(BaseModel):
    """Settings of one connection type. Unknown fields are refused, so a secret never lands in the settings by
    mistake (the settings are shown and exported as configuration; the secret is not)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SearchSettings(_Settings):
    """A web search service (the adapters come with NAL-A2). ``provider`` names the adapter (``test`` is the offline
    test provider); ``endpoint`` is only for services a user runs themselves (e.g. a SearXNG instance)."""

    endpoint: str | None = Field(default=None, max_length=2000)
    #: The price of one request in US dollars, for the run budget; ``None``: the adapter's documented price.
    price_per_request_usd: Decimal | None = Field(default=None, ge=0, le=100)


class SmtpSettings(_Settings):
    """An outgoing mail server. Only encrypted transport: ``starttls`` (usually port 587) or ``tls`` (465)."""

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=587, ge=1, le=65535)
    security: Literal["starttls", "tls"] = "starttls"
    username: str | None = Field(default=None, max_length=320)
    from_address: str = Field(min_length=3, max_length=320)
    #: How the secret is used: an SMTP password, or an OAuth 2.0 refresh token (XOAUTH2; flows come with NAL-A5).
    auth: Literal["password", "oauth2"] = "password"

    @field_validator("from_address")
    @classmethod
    def _address(cls, value: str) -> str:
        if not _EMAIL.match(value):
            raise ValueError("not an e-mail address")
        return value


class WebhookSettings(_Settings):
    """An HTTP endpoint that receives JSON. The address must not carry a credential: the secret is a signing key
    (``hmac-sha256``) or a bearer token."""

    url: str = Field(min_length=1, max_length=2000)
    method: Literal["POST", "PUT"] = "POST"
    auth: Literal["hmac-sha256", "bearer", "none"] = "hmac-sha256"


# ---- connection types ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """The outcome of :meth:`ConnectionType.check`: ``code`` names the problem (an error code) when not ``ok``."""

    ok: bool
    code: str | None = None
    detail: str = ""
    #: ``configuration``: only the settings were checked; ``service``: the service answered.
    kind: Literal["configuration", "service"] = "configuration"


@dataclass(frozen=True)
class ConnectionType:
    """One kind of service. ``egress`` derives the hosts a connection may reach from its settings and provider;
    ``check`` tests a connection (it gets the secret in memory and must never put it into its result)."""

    name: str
    settings: type[_Settings]
    #: The providers a connection of this type may name; empty: only ``generic``.
    providers: tuple[str, ...] = ()
    #: Whether the type needs a secret before it can be used.
    secret_required: bool = True
    #: What the secret is (shown next to the input): ``api_key``, ``password``, ``signing_key`` (never a connection
    #: string: it would carry the location past the settings-derived ``egress``).
    secret_label: str = "api_key"
    egress: Callable[[str, _Settings], list[str]] = field(default=lambda _provider, _settings: [])
    check: Callable[[ResolvedConnection], CheckResult] | None = None
    #: The type's own address rules (``(settings, mode) -> [{"field", "reason"}]``), e.g. a TCP ``host:port`` of a
    #: database; ``None``: the URL and host rules of the NAL types (https, no credentials, global addresses).
    addresses: Callable[[_Settings, Mode], list[dict[str, str]]] | None = None
    #: The modes a connection of this type can be used in.
    modes: frozenset[Mode] = frozenset({"desktop", "local-container", "server"})

    def describe(self) -> dict[str, Any]:
        """The type for the API: name, providers, secret, settings schema (no values)."""
        return {
            "name": self.name,
            "providers": list(self.providers) or ["generic"],
            "secret_required": self.secret_required,
            "secret_label": self.secret_label,
            "settings_schema": self.settings.model_json_schema(),
            "modes": sorted(self.modes),
            "checkable": self.check is not None,
        }


_TYPES: dict[str, ConnectionType] = {}
_TYPES_LOCK = threading.Lock()


def register_connection_type(item: ConnectionType, *, replace: bool = False) -> None:
    """Register a connection type (CR-DB registers ``database`` this way). A name is registered once."""
    if not _TYPE_NAME.match(item.name):
        raise ValueError(f"invalid connection type name {item.name!r}")
    with _TYPES_LOCK:
        if item.name in _TYPES and not replace:
            raise ValueError(f"connection type {item.name!r} is already registered")
        _TYPES[item.name] = item


def connection_types() -> dict[str, ConnectionType]:
    with _TYPES_LOCK:
        return dict(_TYPES)


def connection_type(name: str) -> ConnectionType:
    with _TYPES_LOCK:
        found = _TYPES.get(name)
    if found is None and name in RESERVED_TYPES:
        raise NodestroError("connection.type_unavailable", f"Connection type {name!r} is not available yet", type=name)
    if found is None:
        raise NodestroError("connection.type_unknown", f"Unknown connection type {name!r}", type=name)
    return found


# ---- addresses ----------------------------------------------------------------------------------------------------


def _host_port(text: str, *, default_port: int) -> tuple[str, int]:
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    try:
        port = parts.port or default_port
    except ValueError:
        raise ValueError("invalid port") from None
    return host, port


def host_problem(host: str, *, mode: Mode) -> str | None:
    """Why ``host`` cannot be a connection's address, or ``None``.

    Names are checked for their form only (the network guard resolves them at call time, NAL-A2). Address literals
    must be global unicast; loopback only outside the server profile (a service on the user's own computer)."""
    if not host:
        return "missing"
    literal = host.strip("[]")
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        if not _HOST.match(host) or _NUMERIC_LABEL.match(host.rsplit(".", 1)[-1]):
            return "invalid"
        if host.rstrip(".") in METADATA_HOSTS:
            return "metadata"
        if host in _LOOPBACK_NAMES or host.endswith(".localhost"):
            return None if mode != "server" else "loopback"
        return None
    return address_problem(address, allow_loopback=mode != "server")


def _url_problem(text: str, *, mode: Mode) -> str | None:
    try:
        parts = urlsplit(text)
        host, _ = _host_port(text, default_port=443)
    except ValueError:
        return "invalid"
    if parts.scheme not in ("https", "http"):
        return "scheme"
    if parts.username or parts.password:
        return "userinfo"
    secret = url_secret_problem(text)
    if secret is not None:
        return f"secret_in_{secret}"
    problem = host_problem(host, mode=mode)
    if problem is not None:
        return problem
    if parts.scheme == "http" and not (host in _LOOPBACK_NAMES or _is_loopback_literal(host)):
        return "not_https"
    return None


def _is_loopback_literal(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _address_issues(settings: _Settings, *, mode: Mode, kind: ConnectionType | None = None) -> list[dict[str, str]]:
    """Problems with the addresses in ``settings`` (field names and reasons; never the value)."""
    if kind is not None and kind.addresses is not None:
        return kind.addresses(settings, mode)
    found: list[dict[str, str]] = []
    if isinstance(settings, SearchSettings) and settings.endpoint:
        reason = _url_problem(settings.endpoint, mode=mode)
        if reason:
            found.append({"field": "endpoint", "reason": reason})
    if isinstance(settings, WebhookSettings):
        reason = _url_problem(settings.url, mode=mode)
        if reason:
            found.append({"field": "url", "reason": reason})
    if isinstance(settings, SmtpSettings):
        reason = host_problem(settings.host.lower(), mode=mode)
        if reason:
            found.append({"field": "host", "reason": reason})
    return found


def _search_egress(_provider: str, settings: _Settings) -> list[str]:
    assert isinstance(settings, SearchSettings)
    if not settings.endpoint:
        return []  # the adapter's own hosts (NAL-A2); the test provider reaches nothing
    host, port = _host_port(settings.endpoint, default_port=443)
    return [f"{host}:{port}"]


def _smtp_egress(_provider: str, settings: _Settings) -> list[str]:
    assert isinstance(settings, SmtpSettings)
    return [f"{settings.host.lower()}:{settings.port}"]


def _webhook_egress(_provider: str, settings: _Settings) -> list[str]:
    assert isinstance(settings, WebhookSettings)
    parts = urlsplit(settings.url)
    host, port = _host_port(settings.url, default_port=443 if parts.scheme == "https" else 80)
    return [f"{host}:{port}"]


def _check_search(connection: ResolvedConnection) -> CheckResult:
    """Without the network guard of NAL-A2 only the offline test provider is contacted."""
    if connection.provider == "test":
        return CheckResult(ok=True, kind="service")
    return CheckResult(ok=False, code="connection.check_unavailable", detail="provider")


def _check_configuration(connection: ResolvedConnection) -> CheckResult:
    """A configuration check: the settings are complete and the secret is there. Contacting the service needs the
    network guard (NAL-A2) and comes with the tools that send (NAL-A4)."""
    if connection.secret is None and connection_type(connection.type).secret_required:
        return CheckResult(ok=False, code="connection.secret_missing")
    return CheckResult(ok=True)


SEARCH = ConnectionType(
    name="search",
    settings=SearchSettings,
    providers=("test",),
    secret_label="api_key",
    egress=_search_egress,
    check=_check_search,
)
SMTP = ConnectionType(
    name="smtp", settings=SmtpSettings, secret_label="password", egress=_smtp_egress, check=_check_configuration
)
WEBHOOK = ConnectionType(
    name="webhook",
    settings=WebhookSettings,
    secret_required=False,
    secret_label="signing_key",
    egress=_webhook_egress,
    check=_check_configuration,
)
for _item in (SEARCH, SMTP, WEBHOOK):
    register_connection_type(_item)


def add_search_provider(name: str) -> None:
    """Allow ``name`` as provider of ``search`` connections (the adapters of NAL-A2 call this)."""
    if not _PROVIDER.match(name):
        raise ValueError(f"invalid provider name {name!r}")
    current = connection_type("search")
    if name not in current.providers:
        register_connection_type(dataclasses.replace(current, providers=(*current.providers, name)), replace=True)


# ---- stored model -------------------------------------------------------------------------------------------------


class ConnectionCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: CheckStatus = "unchecked"
    checked_at: datetime | None = None
    #: The error code of a failed check (``connection.*``); never a message from the service.
    code: str | None = None
    kind: Literal["configuration", "service"] | None = None


class ServiceConnection(BaseModel):
    """A stored connection (contract version :data:`CONTRACT_VERSION`; see ADR-0001)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    contract: int = CONTRACT_VERSION
    type: str
    name: str
    provider: str = "generic"
    description: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)
    #: ``host:port`` pairs this connection may reach, derived from type, provider and settings (never typed in).
    egress: list[str] = Field(default_factory=list)
    secret_storage: SecretStorage = "none"
    #: The sealed secret (``TokenVault``); never leaves the runtime (:class:`ConnectionView` drops it).
    secret_sealed: str | None = None
    check: ConnectionCheck = Field(default_factory=ConnectionCheck)
    tenant_id: str
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    created_by: str | None = None
    updated_by: str | None = None


class ConnectionView(BaseModel):
    """What every read returns: the connection without its secret, with ``secret_set``."""

    id: str
    contract: int
    type: str
    name: str
    provider: str
    description: str
    settings: dict[str, Any]
    egress: list[str]
    secret_set: bool
    secret_storage: SecretStorage
    check: ConnectionCheck
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None
    updated_by: str | None = None
    #: Agents that bind this connection (``workflow name / node``), filled by the workspace.
    used_by: list[str] = Field(default_factory=list)


class ConnectionIn(BaseModel):
    """A new connection. The secret is set separately (:meth:`ConnectionService.set_secret`)."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=80)
    provider: str = Field(default="generic", min_length=1, max_length=40)
    description: str = Field(default="", max_length=500)
    settings: dict[str, Any] = Field(default_factory=dict)


class ConnectionPatch(BaseModel):
    """A change of a connection; the type cannot change."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    provider: str | None = Field(default=None, min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=500)
    settings: dict[str, Any] | None = None


class ConnectionEvent(BaseModel):
    """An administration event (who changed which connection); never secrets or settings values."""

    id: str = Field(default_factory=lambda: new_id("cev"))
    at: datetime = Field(default_factory=now)
    kind: EventKind
    actor: str | None = None
    connection_id: str
    connection_type: str
    details: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedConnection:
    """A connection ready for a tool: settings and the secret, in memory only. ``repr`` never shows the secret."""

    id: str
    type: str
    provider: str
    name: str
    settings: Mapping[str, Any]
    egress: tuple[str, ...]
    secret: str | None = field(default=None, repr=False)
    tenant_id: str = ""

    def __repr__(self) -> str:
        return f"ResolvedConnection(id={self.id!r}, type={self.type!r}, provider={self.provider!r}, secret=<hidden>)"


# ---- the service --------------------------------------------------------------------------------------------------


class SecretVault(Protocol):
    """Where sealed secrets come from: the workspace hands in the MCP token vault under the namespace ``connection``
    (:class:`nodestro_runtime.mcp.vault.TokenVault`), so there is one key store for both."""

    tenant_id: str

    @property
    def persistent(self) -> bool: ...

    def seal(self, server_id: str, token: str) -> str: ...

    def open(self, server_id: str, sealed: str) -> str | None: ...


def _settings_problems(exc: ValidationError) -> list[dict[str, str]]:
    """Validation problems with field names and reasons, never the submitted values (they could be secrets)."""
    return [
        {"field": ".".join(str(part) for part in error.get("loc", ())) or "settings", "reason": str(error.get("type"))}
        for error in exc.errors(include_input=False, include_url=False)
    ]


class ConnectionService:
    """The connections of one tenant (``store`` is the tenant's store; ``vault`` seals with the tenant's key)."""

    def __init__(self, store: Any, vault: SecretVault, *, mode: Mode = "desktop") -> None:
        self.store = store
        self.vault = vault
        self.mode: Mode = mode
        #: Agents that bind a connection (``workflow / node`` labels); set by the workspace.
        self.used_by: Callable[[str], list[str]] = lambda _connection_id: []
        #: Secrets of this process when no key store is available (``session`` storage), by connection id.
        self._session: dict[str, str] = {}
        self._lock = threading.RLock()

    @property
    def tenant_id(self) -> str:
        return self.vault.tenant_id

    # ---- reads -------------------------------------------------------------------------------------------------

    def _all(self) -> list[ServiceConnection]:
        return [item for item in self.store.list(CONNECTIONS, ServiceConnection) if item.tenant_id == self.tenant_id]

    def connections(self, *, type: str | None = None) -> list[ServiceConnection]:
        items = [item for item in self._all() if type is None or item.type == type]
        return sorted(items, key=lambda item: (item.name.lower(), item.id))

    def get(self, connection_id: str) -> ServiceConnection:
        found: ServiceConnection | None = self.store.get(CONNECTIONS, connection_id, ServiceConnection)
        # A document of another tenant (copied into this store by hand) is treated as missing.
        if found is None or found.tenant_id != self.tenant_id:
            raise NodestroError("connection.not_found", f"Connection not found: {connection_id}", id=connection_id)
        return found

    def find(self, connection_id: str) -> ServiceConnection | None:
        try:
            return self.get(connection_id)
        except NodestroError:
            return None

    def secret_set(self, item: ServiceConnection) -> bool:
        if item.secret_storage == "vault":
            return bool(item.secret_sealed)
        if item.secret_storage == "session":
            return item.id in self._session
        return False

    def view(self, item: ServiceConnection) -> ConnectionView:
        return ConnectionView(
            **item.model_dump(exclude={"secret_sealed", "tenant_id"}),
            secret_set=self.secret_set(item),
            used_by=self.used_by(item.id),
        )

    def views(self, *, type: str | None = None) -> list[ConnectionView]:
        return [self.view(item) for item in self.connections(type=type)]

    # ---- validation ----------------------------------------------------------------------------------------------

    def _validated(
        self, kind: ConnectionType, provider: str, settings: Mapping[str, Any]
    ) -> tuple[_Settings, list[str]]:
        if not _PROVIDER.match(provider):
            raise NodestroError("connection.provider_invalid", "Invalid provider name", provider="")
        allowed = kind.providers or ("generic",)
        if provider not in allowed:
            raise NodestroError(
                "connection.provider_unknown",
                f"Provider {provider!r} is not available for {kind.name} connections",
                provider=provider,
                type=kind.name,
            )
        try:
            parsed = kind.settings.model_validate(dict(settings))
        except ValidationError as exc:
            raise NodestroError(
                "connection.settings_invalid",
                f"The settings of the {kind.name} connection are not valid",
                type=kind.name,
                problems=_settings_problems(exc),
            ) from None
        issues = _address_issues(parsed, mode=self.mode, kind=kind)
        if issues:
            raise NodestroError(
                "connection.address_invalid",
                "An address of the connection cannot be used",
                type=kind.name,
                problems=issues,
            )
        return parsed, sorted(dict.fromkeys(kind.egress(provider, parsed)))

    def _unique_name(self, name: str, *, except_id: str | None = None) -> None:
        wanted = name.strip().lower()
        if any(item.name.strip().lower() == wanted and item.id != except_id for item in self._all()):
            raise NodestroError("connection.name_taken", f"A connection named {name!r} exists already", name=name)

    # ---- writes --------------------------------------------------------------------------------------------------

    def create(self, data: ConnectionIn, *, actor: str | None = None) -> ServiceConnection:
        kind = connection_type(data.type)
        if self.mode not in kind.modes:
            raise NodestroError("connection.mode_unsupported", "Not available in this mode", type=kind.name)
        settings, egress = self._validated(kind, data.provider, data.settings)
        with self._lock:
            self._unique_name(data.name)
            item = ServiceConnection(
                id=new_id("conn"),
                type=kind.name,
                name=data.name.strip(),
                provider=data.provider,
                description=data.description,
                settings=settings.model_dump(mode="json"),
                egress=egress,
                tenant_id=self.tenant_id,
                created_by=actor,
                updated_by=actor,
            )
            self.store.put(CONNECTIONS, item.id, item)
        self.event("created", item, actor=actor, provider=item.provider)
        return item

    def update(self, connection_id: str, data: ConnectionPatch, *, actor: str | None = None) -> ServiceConnection:
        with self._lock:
            item = self.get(connection_id)
            kind = connection_type(item.type)
            provider = data.provider if data.provider is not None else item.provider
            raw = data.settings if data.settings is not None else item.settings
            settings, egress = self._validated(kind, provider, raw)
            if data.name is not None:
                self._unique_name(data.name, except_id=item.id)
            changed_target = provider != item.provider or settings.model_dump(mode="json") != item.settings
            # A secret only goes where it was entered for: a new address drops it (ADR-0001, write-only secrets).
            moved = sorted(egress) != sorted(item.egress)
            if moved:
                self._session.pop(item.id, None)
            item = item.model_copy(
                update={
                    "name": data.name.strip() if data.name is not None else item.name,
                    "provider": provider,
                    "description": data.description if data.description is not None else item.description,
                    "settings": settings.model_dump(mode="json"),
                    "egress": egress,
                    # A connection that points somewhere else has to be checked again.
                    "check": ConnectionCheck() if changed_target else item.check,
                    **({"secret_sealed": None, "secret_storage": "none"} if moved else {}),
                    "updated_at": now(),
                    "updated_by": actor,
                }
            )
            self.store.put(CONNECTIONS, item.id, item)
        fields = sorted(name for name in data.model_dump(exclude_none=True))
        if moved:
            self.event("updated", item, actor=actor, fields=fields, target_changed=True, secret_cleared=True)
        else:
            self.event("updated", item, actor=actor, fields=fields)
        return item

    def delete(self, connection_id: str, *, actor: str | None = None, force: bool = False) -> None:
        with self._lock:
            item = self.get(connection_id)
            used = self.used_by(item.id)
            if used and not force:
                raise NodestroError(
                    "connection.in_use",
                    f"The connection is used by {len(used)} agent(s)",
                    count=len(used),
                    names=", ".join(used[:10]),
                )
            self._session.pop(item.id, None)
            self.store.delete(CONNECTIONS, item.id)
        self.event("deleted", item, actor=actor)

    def set_secret(self, connection_id: str, secret: str, *, actor: str | None = None) -> ServiceConnection:
        """Store (or with ``""`` remove) the secret. It can never be read back."""
        if len(secret) > MAX_SECRET_LENGTH:
            raise NodestroError("connection.secret_too_long", "The secret is too long", limit=MAX_SECRET_LENGTH)
        if any(ord(char) < 32 and char not in "\t" for char in secret):
            raise NodestroError("connection.secret_invalid", "The secret contains control characters")
        with self._lock:
            item = self.get(connection_id)
            self._session.pop(item.id, None)
            sealed: str | None = None
            storage: SecretStorage = "none"
            if secret:
                if self.vault.persistent:
                    sealed, storage = self.vault.seal(item.id, secret), "vault"
                else:
                    self._session[item.id] = secret
                    storage = "session"
            item = item.model_copy(
                update={
                    "secret_sealed": sealed,
                    "secret_storage": storage,
                    "check": ConnectionCheck(),
                    "updated_at": now(),
                    "updated_by": actor,
                }
            )
            self.store.put(CONNECTIONS, item.id, item)
        self.event("secret_set" if secret else "secret_cleared", item, actor=actor, storage=storage)
        return item

    # ---- use -----------------------------------------------------------------------------------------------------

    def _secret(self, item: ServiceConnection) -> str | None:
        if item.secret_storage == "vault" and item.secret_sealed:
            return self.vault.open(item.id, item.secret_sealed)
        if item.secret_storage == "session":
            return self._session.get(item.id)
        return None

    def resolve(self, connection_id: str, *, expected_type: str | None = None) -> ResolvedConnection:
        """The connection with its secret, for a tool call (in memory only)."""
        item = self.get(connection_id)
        if expected_type is not None and item.type != expected_type:
            raise NodestroError(
                "connection.type_mismatch",
                f"The connection is a {item.type} connection, a {expected_type} connection is needed",
                type=item.type,
                expected=expected_type,
            )
        kind = connection_type(item.type)
        secret = self._secret(item)
        if item.secret_storage == "vault" and secret is None:
            raise NodestroError("connection.secret_unreadable", "The secret cannot be read; enter it again", id=item.id)
        if kind.secret_required and secret is None:
            raise NodestroError("connection.secret_missing", "The connection has no secret yet", id=item.id)
        return ResolvedConnection(
            id=item.id,
            type=item.type,
            provider=item.provider,
            name=item.name,
            settings=dict(item.settings),
            egress=tuple(item.egress),
            secret=secret,
            tenant_id=self.tenant_id,
        )

    def check(self, connection_id: str, *, actor: str | None = None) -> ServiceConnection:
        """Run the type's check and store the result (status, code; never the service's own message)."""
        item = self.get(connection_id)
        kind = connection_type(item.type)
        if kind.check is None:
            result = CheckResult(ok=False, code="connection.check_unavailable")
        else:
            try:
                result = kind.check(self.resolve(item.id))
            except NodestroError as exc:
                result = CheckResult(ok=False, code=exc.code)
            except Exception as exc:  # the type only: an error text may repeat an address or a credential
                logger.warning("Check of a %s connection failed: %s", item.type, type(exc).__name__)
                result = CheckResult(ok=False, code="connection.check_failed")
        with self._lock:
            item = self.get(connection_id)
            item = item.model_copy(
                update={
                    "check": ConnectionCheck(
                        status="ok" if result.ok else "failed",
                        checked_at=now(),
                        code=result.code,
                        kind=result.kind,
                    )
                }
            )
            self.store.put(CONNECTIONS, item.id, item)
        self.event("checked", item, actor=actor, status=item.check.status, code=result.code)
        return item

    # ---- audit ---------------------------------------------------------------------------------------------------

    def event(self, kind: EventKind, item: ServiceConnection, *, actor: str | None = None, **details: Any) -> None:
        record = ConnectionEvent(
            kind=kind, actor=actor, connection_id=item.id, connection_type=item.type, details=details
        )
        key = f"{record.at.isoformat()}_{record.id}"
        try:
            sink = platform_events(self.store)
            if sink is None:
                self.store.put(EVENTS, key, record)
            else:  # the mirror commits with the event (see nodestro_runtime.platform_events)
                with self.store.transaction():
                    self.store.put(EVENTS, key, record)
                    sink.audit(
                        self.tenant_id,
                        event_id=record.id,
                        at=record.at,
                        action=f"nodestro.connection.{kind}",
                        actor=actor,
                        target_type="service_connection",
                        target_id=item.id,
                        outcome="success",
                        details={"type": item.type, **details},
                    )
        except Exception:
            logger.exception("Could not store connection event %s", kind)

    def events(self, *, limit: int = 200) -> list[ConnectionEvent]:
        items = self.store.items(EVENTS, ConnectionEvent)
        items.sort(key=lambda pair: pair[0], reverse=True)
        if len(items) > MAX_EVENTS:
            for key, _ in items[MAX_EVENTS:]:
                self.store.delete(EVENTS, key)
        return [event for _, event in items[:limit]]


# ---- exported packages -------------------------------------------------------------------------------------------


class StaticConnections:
    """The connections of an exported package: entered on the computer it runs on, never part of the export.

    ``entries`` maps a connection id (the one the release's agents bind) to ``{"type", "provider", "name", "settings",
    "secret"}``, usually from ``<DATA_DIR>/secrets/connections.json`` (written by ``configure``, owner-only file
    permissions). The variable ``NODESTRO_CONNECTION_<ID>_SECRET`` (id in upper case) replaces a secret; the
    environment always wins. Settings are validated like in the studio; a broken entry counts as missing."""

    def __init__(
        self, entries: Mapping[str, Any], *, environ: Mapping[str, str] | None = None, mode: Mode = "desktop"
    ) -> None:
        self._entries: dict[str, ResolvedConnection] = {}
        self.problems: dict[str, str] = {}
        env = environ or {}
        for connection_id, entry in entries.items():
            if not CONNECTION_ID.match(connection_id):
                self.problems[connection_id] = "connection.id_invalid"
                continue
            try:
                self._entries[connection_id] = self._parse(connection_id, entry, env, mode)
            except NodestroError as exc:
                self.problems[connection_id] = exc.code
            except (TypeError, ValueError, AttributeError):
                self.problems[connection_id] = "connection.settings_invalid"

    @staticmethod
    def _parse(connection_id: str, entry: Any, env: Mapping[str, str], mode: Mode) -> ResolvedConnection:
        kind = connection_type(str(entry.get("type", "")))
        provider = str(entry.get("provider") or "generic")
        if provider not in (kind.providers or ("generic",)):
            raise NodestroError("connection.provider_unknown", "Unknown provider", provider=provider, type=kind.name)
        try:
            settings = kind.settings.model_validate(dict(entry.get("settings") or {}))
        except ValidationError:
            raise NodestroError(
                "connection.settings_invalid", "Invalid settings", type=kind.name, problems=[]
            ) from None
        if _address_issues(settings, mode=mode, kind=kind):
            raise NodestroError("connection.address_invalid", "Invalid address", type=kind.name, problems=[])
        secret = env.get(secret_variable(connection_id)) or entry.get("secret") or None
        return ResolvedConnection(
            id=connection_id,
            type=kind.name,
            provider=provider,
            name=str(entry.get("name") or connection_id),
            settings=settings.model_dump(mode="json"),
            egress=tuple(sorted(dict.fromkeys(kind.egress(provider, settings)))),
            secret=str(secret) if secret else None,
        )

    def find(self, connection_id: str) -> ResolvedConnection | None:
        return self._entries.get(connection_id)

    def resolve(self, connection_id: str, *, expected_type: str | None = None) -> ResolvedConnection:
        found = self._entries.get(connection_id)
        if found is None:
            raise NodestroError("connection.not_found", f"Connection not configured: {connection_id}", id=connection_id)
        if expected_type is not None and found.type != expected_type:
            raise NodestroError(
                "connection.type_mismatch",
                f"The connection is a {found.type} connection, a {expected_type} connection is needed",
                type=found.type,
                expected=expected_type,
            )
        if connection_type(found.type).secret_required and not found.secret:
            raise NodestroError("connection.secret_missing", "The connection has no secret yet", id=connection_id)
        return found

    def missing(self, required: Mapping[str, str]) -> list[str]:
        """The ids of ``required`` connections (``{id: "type:provider"}``) that cannot be used as configured."""
        found: list[str] = []
        for connection_id, wanted in sorted(required.items()):
            kind = wanted.split(":", 1)[0]
            try:
                self.resolve(connection_id, expected_type=kind)
            except NodestroError:
                found.append(connection_id)
        return found


def secret_variable(connection_id: str) -> str:
    """The environment variable that replaces the secret of an exported package's connection."""
    return f"NODESTRO_CONNECTION_{re.sub(r'[^A-Za-z0-9]', '_', connection_id).upper()}_SECRET"


__all__ = [
    "CONNECTION_ID",
    "CONTRACT_VERSION",
    "RESERVED_TYPES",
    "CheckResult",
    "ConnectionCheck",
    "ConnectionEvent",
    "ConnectionIn",
    "ConnectionPatch",
    "ConnectionService",
    "ConnectionType",
    "ConnectionView",
    "ResolvedConnection",
    "SearchSettings",
    "SecretVault",
    "ServiceConnection",
    "SmtpSettings",
    "StaticConnections",
    "WebhookSettings",
    "add_search_provider",
    "connection_type",
    "connection_types",
    "host_problem",
    "register_connection_type",
    "secret_variable",
]
