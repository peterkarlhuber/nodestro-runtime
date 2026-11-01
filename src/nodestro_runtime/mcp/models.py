"""The stored MCP configuration of a tenant: servers, discovered tools with their approvals, audit events.

These models need no MCP SDK; the API and the studio can read and change the configuration on every Python version.
Collections (one store per tenant, so tenants never see each other's entries):

``mcp_servers``  key = server id
``mcp_tools``    key = ``<server id>/<tool name on the server>``
``mcp_events``   key = event id (administration and policy events, never secrets)
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..mcp_access import TOOL_PREFIX, is_mcp_tool, unattended_mcp_enabled
from ..schemas import new_id, now
from ..tool_policy import ToolCapability, capability_of

__all__ = ["TOOL_PREFIX", "is_mcp_tool", "unattended_mcp_enabled"]

SERVERS = "mcp_servers"
TOOLS = "mcp_tools"
EVENTS = "mcp_events"
#: Slugs of removed servers, never given to a new server (independent review L-6).
RETIRED_SLUGS = "mcp_retired_slugs"

_TOOL_NAME_MAX = 128
_DESCRIPTION_MAX = 4000
_SCHEMA_MAX_BYTES = 64_000

AuthKind = Literal["none", "bearer", "oauth"]
#: Where a server's token lives: nowhere, encrypted in the database, or only in this process's memory.
TokenStorage = Literal["none", "vault", "session"]
#: ``new``: discovered, not decided; ``approved``: an administrator allowed it; ``rejected``: not allowed;
#: ``changed``: the server changed an approved definition, blocked until an administrator accepts the change;
#: ``removed``: the server no longer offers it.
ToolStatus = Literal["new", "approved", "rejected", "changed", "removed"]
#: The administrator's classification (the shared tool capability model of S-1): ``read`` changes nothing anywhere;
#: ``write`` creates or changes data (the default: annotations are hints, never a guarantee); ``destructive``
#: deletes, pays, sends or cannot be undone.
ToolAccess = Literal["read", "write", "destructive"]
#: How Nodestro reaches a server: Streamable HTTP, or a local server started over stdio in an isolated container
#: (WP-8, D-5: only in a container, never on the host).
Transport = Literal["http", "stdio"]

#: A Python module path (``package.module``) a stdio server is started with (``python -m``).
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,7}")
#: One requirement line: a name, optional extras and a version specifier; no URLs, paths or options.
_REQUIREMENT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"  # name
    r"(\[[A-Za-z0-9._,-]{1,100}\])?"  # extras
    r"([<>=!~][<>=!~ 0-9A-Za-z.*+,-]{0,99})?"  # version specifier
)
#: A stdio server's address in the configuration (shown and audited like a URL; never contacted).
STDIO_SCHEME = "stdio:"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class StdioCommand(_Model):
    """A local MCP server started over stdio (WP-8): Python packages from PyPI, installed only after an administrator
    approved their hash-pinned versions, and run with ``python -m <module> <args>`` in a container of Nodestro's
    sandbox image: no network, read-only root file system, no capabilities, user ``nobody``, memory/CPU/process
    limits. Never on the host."""

    #: The packages (``name[extras]<specifier>`` lines; no URLs, paths or pip options).
    requirements: list[str] = Field(min_length=1, max_length=16)
    module: str = Field(min_length=1, max_length=200)
    args: list[str] = Field(default_factory=list, max_length=16)
    memory_mb: int = Field(default=512, ge=128, le=2048)

    @field_validator("requirements")
    @classmethod
    def _requirements(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        for item in cleaned:
            if not _REQUIREMENT.fullmatch(item):
                raise ValueError(f"not a plain package requirement: {item[:100]!r}")
        return cleaned

    @field_validator("module")
    @classmethod
    def _module(cls, value: str) -> str:
        if not _MODULE.fullmatch(value):
            raise ValueError("the module must be a Python module path such as package.server")
        return value

    @field_validator("args")
    @classmethod
    def _args(cls, value: list[str]) -> list[str]:
        for item in value:
            if len(item) > 200 or any(ord(char) < 32 for char in item):
                raise ValueError("an argument is at most 200 printable characters")
        return value

    def address(self) -> str:
        return f"{STDIO_SCHEME}{self.module}"


class McpServer(_Model):
    id: str = Field(default_factory=lambda: new_id("mcp"))
    slug: str
    name: str
    url: str
    enabled: bool = True
    #: A desktop's consent to reach a local development server over loopback ``http``.
    allow_loopback: bool = False
    auth: AuthKind = "none"
    transport: Transport = "http"
    #: The command of a ``stdio`` server (``url`` is then ``stdio:<module>``).
    stdio: StdioCommand | None = None
    token_storage: TokenStorage = "none"
    #: The encrypted token (``vault``); never returned by the API.
    token_sealed: str | None = Field(default=None, repr=False)
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    max_output_chars: int = Field(default=20_000, ge=1_000, le=200_000)
    max_parallel_calls: int = Field(default=4, ge=1, le=16)
    #: OAuth summary (WP-6) the API may show: issuer, scope, expiry, a running sign-in's deadline. The credentials
    #: (tokens, client registration) live sealed in ``token_sealed`` like every other token.
    oauth: dict[str, Any] = Field(default_factory=dict)
    #: A started sign-in (PKCE verifier, discovered metadata), sealed; and the digest of its ``state`` for the
    #: callback's lookup. Never returned by the API.
    oauth_pending_sealed: str | None = Field(default=None, repr=False)
    oauth_state_digest: str | None = Field(default=None, repr=False)
    protocol_version: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    last_checked_at: datetime | None = None
    last_error_code: str | None = None
    last_error_params: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class RetiredSlug(_Model):
    """The slug of a removed server (independent review L-6): kept so no new server gets it."""

    server_id: str
    retired_at: datetime = Field(default_factory=now)


class McpServerIn(_Model):
    name: str = Field(min_length=1, max_length=80)
    #: The server's address; for ``stdio`` it is derived from the command (anything given is ignored).
    url: str = Field(default="", max_length=2000)
    enabled: bool = True
    allow_loopback: bool = False
    auth: AuthKind = "none"
    transport: Transport = "http"
    stdio: StdioCommand | None = None
    #: Write-only. ``None`` keeps the stored token, ``""`` removes it.
    token: str | None = Field(default=None, repr=False, max_length=8192)
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    max_output_chars: int = Field(default=20_000, ge=1_000, le=200_000)
    max_parallel_calls: int = Field(default=4, ge=1, le=16)


class McpServerView(_Model):
    """A server as the API shows it: without its token."""

    id: str
    slug: str
    name: str
    url: str
    enabled: bool
    allow_loopback: bool
    auth: AuthKind
    transport: Transport = "http"
    stdio: StdioCommand | None = None
    #: A stdio server's package environment: ``approval_needed``, ``installing``, ``ready``, ... (see
    #: :class:`~nodestro_runtime.python_envs.DependencyStatus`); empty for HTTP servers.
    environment_state: str = ""
    token_set: bool
    token_storage: TokenStorage
    timeout_seconds: float
    max_output_chars: int
    max_parallel_calls: int
    protocol_version: str | None
    server_name: str | None
    server_version: str | None
    last_checked_at: datetime | None
    last_error_code: str | None
    last_error_params: dict[str, Any]
    oauth: dict[str, Any] = Field(default_factory=dict)
    tools_total: int = 0
    tools_approved: int = 0
    tools_pending: int = 0
    created_at: datetime
    updated_at: datetime


class ToolDefinition(_Model):
    """What a server says about a tool: the part whose hash pins an approval."""

    name: str
    title: str | None = None
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] = Field(default_factory=dict)

    def digest(self) -> str:
        return definition_hash(self)


class McpTool(_Model):
    server_id: str
    #: The server's own tool name.
    name: str
    #: The name agents use (``mcp__<slug>__<tool>``).
    tool_name: str
    definition: ToolDefinition
    hash: str
    status: ToolStatus = "new"
    access: ToolAccess = "write"
    #: The definition an administrator approved (kept while a changed one waits for a decision).
    approved_definition: ToolDefinition | None = None
    approved_hash: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=now)
    last_seen_at: datetime = Field(default_factory=now)

    @property
    def key(self) -> str:
        return tool_key(self.server_id, self.name)

    @property
    def qualified(self) -> str:
        return f"mcp:{self.server_id}/{self.name}"

    def capability(self) -> ToolCapability:
        """The tool's capability statement in the shared model (S-1): the administrator's classification; MCP tools
        always reach outside Nodestro."""
        return capability_of(name=self.tool_name, kind="mcp", stated={"access": self.access, "reach": "external"})

    def suggested_access(self) -> ToolAccess:
        """What the annotations suggest; shown as a hint only (the administrator decides)."""
        hints = self.definition.annotations
        if hints.get("destructiveHint") is True:
            return "destructive"
        if hints.get("readOnlyHint") is True:
            return "read"
        return "write"


EventKind = Literal[
    "server_added",
    "server_changed",
    "server_removed",
    "token_changed",
    "tools_discovered",
    "tool_approved",
    "tool_rejected",
    "tool_changed",
    "tool_removed",
    "call_refused",
    "oauth_started",
    "oauth_connected",
    "oauth_failed",
    "oauth_disconnected",
]


class McpEvent(_Model):
    id: str = Field(default_factory=lambda: new_id("mev"))
    at: datetime = Field(default_factory=now)
    kind: EventKind
    actor: str | None = None
    server_id: str | None = None
    tool: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


def tool_key(server_id: str, name: str) -> str:
    return f"{server_id}/{name}"


def make_slug(name: str, taken: set[str]) -> str:
    """A short, unique, readable server slug from its display name (``[a-z0-9]`` and single ``-``)."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24].strip("-") or "server"
    slug, number = base, 2
    while slug in taken:
        suffix = f"-{number}"
        slug = base[: 24 - len(suffix)] + suffix
        number += 1
    return slug


def agent_tool_name(slug: str, tool: str) -> str:
    """``mcp__<slug>__<tool>`` within Orkexa's rule ``[A-Za-z0-9_-]{1,128}`` (hash suffix when shortened)."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", tool) or "tool"
    name = f"{TOOL_PREFIX}{slug}__{safe}"
    if len(name) > _TOOL_NAME_MAX or safe != tool:
        digest = hashlib.sha256(tool.encode()).hexdigest()[:8]
        room = _TOOL_NAME_MAX - len(TOOL_PREFIX) - len(slug) - 2 - 9
        name = f"{TOOL_PREFIX}{slug}__{safe[:room]}_{digest}"
    return name


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def definition_hash(definition: ToolDefinition) -> str:
    payload = definition.model_dump(mode="json")
    return "sha256:" + hashlib.sha256(_canonical(payload).encode()).hexdigest()


#: The name of the tool that reads a server's resources (WP-10). ``/`` is not allowed in MCP tool names, so no tool a
#: server lists can take it (one that tries is skipped at discovery).
RESOURCE_TOOL = "resources/read"
_RESOURCES_MAX = 100
_RESOURCE_URI_MAX = 500


def resource_definition(resources: list[dict[str, Any]]) -> ToolDefinition | None:
    """The tool definition that offers a server's listed resources for reading (WP-10), or ``None`` without any.

    Only the resources the server listed are readable (the ``uri`` is an enum, templates are not offered), so an agent
    cannot make Nodestro fetch an arbitrary address through the server. The definition, and with it its hash, contains
    the whole list: when the server changes its resources, an approval no longer covers the new list and an
    administrator reviews it again, exactly like a changed tool. Names and descriptions are foreign text (T3) and are
    shortened."""
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in resources:
        uri = str(item.get("uri") or "")
        if not uri or len(uri) > _RESOURCE_URI_MAX or uri in seen or any(ord(char) < 32 for char in uri):
            continue
        name = str(item.get("title") or item.get("name") or "")[:120]
        about = " ".join(str(item.get("description") or "").split())[:200]
        mime = str(item.get("mimeType") or item.get("mime_type") or "")[:80]
        label = " - ".join(part for part in (name, about) if part)
        if mime:
            label = f"{label} ({mime})" if label else mime
        seen.add(uri)
        entries.append((uri, label))
        if len(entries) >= _RESOURCES_MAX:
            break
    if not entries:
        return None
    lines = [f"- {uri}" + (f": {label}" if label else "") for uri, label in entries]
    description = "Read one of the documents (resources) this MCP service offers. Available:\n" + "\n".join(lines)
    schema = {
        "type": "object",
        "properties": {
            "uri": {
                "type": "string",
                "enum": [uri for uri, _label in entries],
                "description": "The address of the resource to read.",
            }
        },
        "required": ["uri"],
        "additionalProperties": False,
    }
    return ToolDefinition(
        name=RESOURCE_TOOL,
        title="Read resources",
        description=description[:_DESCRIPTION_MAX],
        input_schema=schema,
        annotations={"readOnlyHint": True},
    )


def clean_definition(raw: dict[str, Any]) -> ToolDefinition:
    """A server's tool entry, limited in size (descriptions and schemas are foreign text, threat model T3)."""
    schema = raw.get("inputSchema") or raw.get("input_schema") or {"type": "object", "properties": {}}
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        schema = {"type": "object", "properties": {}}
    schema = {key: value for key, value in schema.items() if key not in ("$schema", "$id")}
    schema.setdefault("type", "object")
    if len(_canonical(schema)) > _SCHEMA_MAX_BYTES:
        raise ValueError("schema too large")
    output = raw.get("outputSchema") or raw.get("output_schema")
    if output is not None and (not isinstance(output, dict) or len(_canonical(output)) > _SCHEMA_MAX_BYTES):
        output = None
    annotations = raw.get("annotations") or {}
    if not isinstance(annotations, dict):
        annotations = {}
    annotations = {key: value for key, value in annotations.items() if isinstance(value, (bool, str, int, float))}
    description = str(raw.get("description") or "")[:_DESCRIPTION_MAX]
    title = raw.get("title")
    if str(raw["name"]) == RESOURCE_TOOL:
        raise ValueError("reserved name")
    return ToolDefinition(
        name=str(raw["name"]),
        title=str(title)[:200] if title else None,
        description=description,
        input_schema=schema,
        output_schema=output,
        annotations=annotations,
    )
