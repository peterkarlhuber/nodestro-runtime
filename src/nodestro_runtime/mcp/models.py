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
import os
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import new_id, now

SERVERS = "mcp_servers"
TOOLS = "mcp_tools"
EVENTS = "mcp_events"

#: Prefix of every MCP tool name agents see: ``mcp__<server slug>__<tool>``.
TOOL_PREFIX = "mcp__"
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


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class McpServer(_Model):
    id: str = Field(default_factory=lambda: new_id("mcp"))
    slug: str
    name: str
    url: str
    enabled: bool = True
    #: A desktop's consent to reach a local development server over loopback ``http``.
    allow_loopback: bool = False
    auth: AuthKind = "none"
    token_storage: TokenStorage = "none"
    #: The encrypted token (``vault``); never returned by the API.
    token_sealed: str | None = Field(default=None, repr=False)
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    max_output_chars: int = Field(default=20_000, ge=1_000, le=200_000)
    max_parallel_calls: int = Field(default=4, ge=1, le=16)
    #: OAuth state (WP-6): issuer, client id, scopes; tokens live in ``token_sealed`` like every other token.
    oauth: dict[str, Any] = Field(default_factory=dict)
    protocol_version: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    last_checked_at: datetime | None = None
    last_error_code: str | None = None
    last_error_params: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class McpServerIn(_Model):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=2000)
    enabled: bool = True
    allow_loopback: bool = False
    auth: AuthKind = "none"
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

    def capability(self) -> dict[str, str]:
        """The tool's capability statement in the shared model (S-1): MCP tools always reach outside Nodestro."""
        return {"access": self.access, "reach": "external", "source": "admin"}

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
    "oauth_connected",
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


#: Set to ``1`` by the operator after the MCP security acceptance: MCP tools may then run without a person
#: (schedules, batches, test series, published APIs; ADR-0007 D-7). Off by default.
UNATTENDED_ENV = "NODESTRO_MCP_UNATTENDED"


def unattended_mcp_enabled(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(UNATTENDED_ENV, "").strip() == "1"


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


def is_mcp_tool(name: str) -> bool:
    return name.startswith(TOOL_PREFIX)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def definition_hash(definition: ToolDefinition) -> str:
    payload = definition.model_dump(mode="json")
    return "sha256:" + hashlib.sha256(_canonical(payload).encode()).hexdigest()


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
    return ToolDefinition(
        name=str(raw["name"]),
        title=str(title)[:200] if title else None,
        description=description,
        input_schema=schema,
        output_schema=output,
        annotations=annotations,
    )
