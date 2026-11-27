"""``nodestro-mcp/1``: the configuration of MCP services as an exchange file (N-14, :mod:`nodestro_runtime.exchange`).

Kept with the MCP client: the core runtime never imports it (``tests/test_mcp_architecture.py``).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict, Field

from ..exchange import (
    MAX_ITEMS,
    Envelope,
    ExchangeError,
    ImportItem,
    ImportReport,
    StrictModel,
    ensure_no_secrets,
    envelope,
    parse,
    read_json,
    refuse_item,
    require_no_refusals,
    secret_values,
)
from ..problems import NodestroError
from .models import AuthKind, McpServer, McpServerIn, StdioCommand, Transport

if TYPE_CHECKING:
    from .service import McpService


class McpStdioEntry(StdioCommand):
    model_config = ConfigDict(extra="forbid")


class McpServerEntry(StrictModel):
    """An MCP service's configuration without anything of this workspace: no id, token, sign-in, state, approvals."""

    name: str = Field(min_length=1, max_length=80)
    transport: Transport = "http"
    #: The address (``stdio:<module>`` for a stdio server, derived from its command on import).
    url: str = Field(default="", max_length=2000)
    stdio: McpStdioEntry | None = None
    auth: AuthKind = "none"
    allow_loopback: bool = False
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    max_output_chars: int = Field(default=20_000, ge=1_000, le=200_000)
    max_parallel_calls: int = Field(default=4, ge=1, le=16)


class McpPackage(Envelope):
    servers: list[McpServerEntry] = Field(max_length=MAX_ITEMS)


def mcp_entry(server: McpServer) -> McpServerEntry:
    return McpServerEntry(
        name=server.name,
        transport=server.transport,
        url=server.url,
        stdio=McpStdioEntry(**server.stdio.model_dump()) if server.stdio is not None else None,
        auth=server.auth,
        allow_loopback=server.allow_loopback,
        timeout_seconds=server.timeout_seconds,
        max_output_chars=server.max_output_chars,
        max_parallel_calls=server.max_parallel_calls,
    )


def export_mcp(service: McpService, server_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """``nodestro-mcp/1`` of the workspace's MCP services (``server_ids``: only these), checked for secrets."""
    wanted = None if server_ids is None else set(server_ids)
    servers = [server for server in service.servers() if wanted is None or server.id in wanted]
    if wanted is not None and len(servers) != len(wanted):
        missing = sorted(wanted - {server.id for server in servers})[0]
        raise ExchangeError("mcp.server_not_found", f"MCP server not found: {missing}", id=missing)
    document = {
        **envelope("nodestro-mcp"),
        "servers": [mcp_entry(server).model_dump(mode="json") for server in servers],
    }
    known = [value for server in servers for value in secret_values(service.token(server))]
    known += [value for server in servers for value in secret_values(server.token_sealed)]
    ensure_no_secrets(document, known)
    return document


def _mcp_input(entry: McpServerEntry) -> McpServerIn:
    """What adding the server by hand would send: switched off, without a token."""
    return McpServerIn(
        name=entry.name,
        url=entry.url if entry.transport == "http" else "",
        enabled=False,
        allow_loopback=entry.allow_loopback,
        auth=entry.auth,
        transport=entry.transport,
        stdio=StdioCommand(**entry.stdio.model_dump()) if entry.stdio is not None else None,
        token=None,
        timeout_seconds=entry.timeout_seconds,
        max_output_chars=entry.max_output_chars,
        max_parallel_calls=entry.max_parallel_calls,
    )


def import_mcp(service: McpService, raw: bytes, *, dry_run: bool, actor: str | None = None) -> ImportReport:
    """Preview (``dry_run``) or apply an ``nodestro-mcp/1`` file: every server is checked by the rules of adding one
    by hand (address rules, stdio only in a container, the server limit); applying adds all of them, switched off and
    without tokens, or none."""
    package = parse(McpPackage, read_json(raw, "nodestro-mcp"))
    report = ImportReport(format=package.format, dry_run=dry_run, left_out=["enabled", "tool_approvals", "secrets"])
    for number, entry in enumerate(package.servers, start=1):
        item = ImportItem(kind="mcp_server", name=entry.name, needs_secret=entry.auth != "none")
        try:
            service.check_new(_mcp_input(entry), adding=number)
        except NodestroError as exc:
            refuse_item(item, exc)
        report.items.append(item)
    if dry_run:
        return report
    require_no_refusals(report)
    created: list[str] = []
    try:
        for item, entry in zip(report.items, package.servers, strict=True):
            server = service.add(_mcp_input(entry), actor=actor)
            created.append(server.id)
            item.status, item.id = "created", server.id
    except BaseException:
        for server_id in created:
            service.remove(server_id, actor=actor)
        raise
    report.secrets_to_reenter = [f"mcp:{item.id}" for item in report.items if item.needs_secret]
    return report
