"""The MCP service of one workspace: configuration, discovery, tool approvals, the policy check and audit events.

Access to an MCP tool is the intersection of three decisions (threat model, principle 2): the server is enabled, an
administrator approved this exact tool definition (pinned by its hash), and the agent lists the tool. The check runs
when an agent is built and again right before every call, against the definition the server lists at that moment
(threat model T4): a changed definition is blocked (``changed``) until an administrator accepts it.

Until per-call approvals exist (WP-5), only tools classified as ``read`` are usable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import logging
import threading
from typing import Any

from orkexa import Tool

from ..problems import NodestroError
from ..schemas import now
from ..storage import DocumentStore
from .hub import CallResult, Discovery, McpHub
from .models import (
    EVENTS,
    SERVERS,
    TOOLS,
    EventKind,
    McpEvent,
    McpServer,
    McpServerIn,
    McpServerView,
    McpTool,
    ToolAccess,
    ToolDefinition,
    agent_tool_name,
    definition_hash,
    is_mcp_tool,
    make_slug,
    tool_key,
)
from .vault import TokenVault

logger = logging.getLogger(__name__)

#: Kept per tenant; older events are dropped (they hold no secrets and no tool arguments).
MAX_EVENTS = 2000


class McpService:
    def __init__(self, store: DocumentStore, hub: McpHub, vault: TokenVault) -> None:
        self.store = store
        self.hub = hub
        self.vault = vault
        #: Per-call approval of tools that change data (WP-5). ``None``: such tools are not usable yet.
        self.write_gate: Callable[[McpTool, dict[str, Any]], None] | None = None
        self._lock = threading.RLock()

    # ---- servers ---------------------------------------------------------------------------

    def servers(self) -> list[McpServer]:
        return sorted(self.store.list(SERVERS, McpServer), key=lambda item: (item.name.lower(), item.id))

    def server(self, server_id: str) -> McpServer:
        found = self.store.get(SERVERS, server_id, McpServer)
        if found is None:
            raise NodestroError("mcp.server_not_found", f"MCP server not found: {server_id}", id=server_id)
        return found

    def view(self, server: McpServer) -> McpServerView:
        tools = self.tools(server.id)
        return McpServerView(
            **server.model_dump(exclude={"token_sealed"}),
            token_set=self.token(server) is not None,
            tools_total=len([tool for tool in tools if tool.status != "removed"]),
            tools_approved=len([tool for tool in tools if tool.status == "approved"]),
            tools_pending=len([tool for tool in tools if tool.status in ("new", "changed")]),
        )

    def views(self) -> list[McpServerView]:
        return [self.view(server) for server in self.servers()]

    def _set_token(self, server: McpServer, token: str | None) -> None:
        if token is None:
            return
        self.hub.session_tokens.pop(server.id, None)
        server.token_sealed = None
        if not token:
            server.token_storage = "none"
            return
        if self.vault.persistent:
            server.token_sealed = self.vault.seal(server.id, token)
            server.token_storage = "vault"
        else:
            self.hub.session_tokens[server.id] = token
            server.token_storage = "session"

    def token(self, server: McpServer) -> str | None:
        if server.token_storage == "vault" and server.token_sealed:
            return self.vault.open(server.id, server.token_sealed)
        if server.token_storage == "session":
            return self.hub.session_tokens.get(server.id)
        return None

    def add(self, data: McpServerIn, *, actor: str | None = None) -> McpServer:
        self.hub.policy.check_url(data.url, allow_loopback=data.allow_loopback)
        with self._lock:
            taken = {item.slug for item in self.servers()}
            server = McpServer(
                slug=make_slug(data.name, taken),
                **data.model_dump(exclude={"token"}),
            )
            self._set_token(server, data.token)
            self.store.put(SERVERS, server.id, server)
        self.event("server_added", actor=actor, server_id=server.id, url=server.url, name=server.name)
        if data.token:
            self.event("token_changed", actor=actor, server_id=server.id, storage=server.token_storage)
        return server

    def update(self, server_id: str, data: McpServerIn, *, actor: str | None = None) -> McpServer:
        self.hub.policy.check_url(data.url, allow_loopback=data.allow_loopback)
        with self._lock:
            server = self.server(server_id)
            moved = data.url != server.url
            for key, value in data.model_dump(exclude={"token"}).items():
                setattr(server, key, value)
            if moved:
                # A token belongs to its origin (threat model T9): a new address needs the token again.
                if data.token is None and server.token_storage != "none":
                    self._set_token(server, "")
                # Approvals belong to the server they were made for.
                for tool in self.tools(server.id):
                    if tool.status == "approved":
                        tool.status = "changed"
                        self.store.put(TOOLS, tool.key, tool)
            self._set_token(server, data.token)
            server.updated_at = now()
            self.store.put(SERVERS, server.id, server)
        self.event("server_changed", actor=actor, server_id=server.id, url=server.url, enabled=server.enabled)
        if data.token is not None:
            self.event("token_changed", actor=actor, server_id=server.id, storage=server.token_storage)
        return server

    def remove(self, server_id: str, *, actor: str | None = None) -> None:
        with self._lock:
            server = self.server(server_id)
            self.store.delete_prefix(TOOLS, f"{server.id}/")
            self.store.delete(SERVERS, server.id)
            self.hub.session_tokens.pop(server.id, None)
        self.event("server_removed", actor=actor, server_id=server_id, name=server.name)

    # ---- discovery -------------------------------------------------------------------------

    def _record_check(self, server: McpServer, discovery: Discovery | None, error: NodestroError | None) -> None:
        server.last_checked_at = now()
        if discovery is not None:
            server.protocol_version = discovery.protocol_version
            server.server_name = (discovery.server_name or "")[:200] or None
            server.server_version = (discovery.server_version or "")[:100] or None
            server.last_error_code, server.last_error_params = None, {}
        if error is not None:
            server.last_error_code, server.last_error_params = error.code, dict(error.params)
        self.store.put(SERVERS, server.id, server)

    async def adiscover(self, server_id: str, *, actor: str | None = None) -> tuple[McpServer, Discovery]:
        """Connect, list the tools and update the stored records (new, changed, removed)."""
        server = self.server(server_id)
        try:
            discovery = await self.hub.acall(self.hub.adiscover(server, self.token(server)))
        except NodestroError as exc:
            self._record_check(server, None, exc)
            raise
        return self._discovered(server, discovery, actor)

    def discover(self, server_id: str, *, actor: str | None = None) -> tuple[McpServer, Discovery]:
        """:meth:`adiscover` for callers without an event loop of their own (scripts, worker threads)."""
        server = self.server(server_id)
        try:
            discovery = self.hub.discover(server, self.token(server))
        except NodestroError as exc:
            self._record_check(server, None, exc)
            raise
        return self._discovered(server, discovery, actor)

    def _discovered(self, server: McpServer, discovery: Discovery, actor: str | None) -> tuple[McpServer, Discovery]:
        self._record_check(server, discovery, None)
        self.apply_discovery(server, discovery.tools, actor=actor)
        return server, discovery

    def apply_discovery(
        self, server: McpServer, definitions: Iterable[ToolDefinition], *, actor: str | None = None
    ) -> list[McpTool]:
        seen: set[str] = set()
        counts = {"new": 0, "changed": 0, "removed": 0}
        with self._lock:
            known = {tool.name: tool for tool in self.tools(server.id)}
            for definition in definitions:
                seen.add(definition.name)
                digest = definition_hash(definition)
                tool = known.get(definition.name)
                if tool is None:
                    tool = McpTool(
                        server_id=server.id,
                        name=definition.name,
                        tool_name=agent_tool_name(server.slug, definition.name),
                        definition=definition,
                        hash=digest,
                    )
                    counts["new"] += 1
                else:
                    tool.last_seen_at = now()
                    if tool.status == "removed":
                        tool.status = "new" if tool.approved_hash is None else "changed"
                    if digest != tool.hash:
                        tool.definition, tool.hash = definition, digest
                        if tool.status == "approved":
                            tool.status = "changed"
                            counts["changed"] += 1
                            self.event(
                                "tool_changed",
                                actor=actor,
                                server_id=server.id,
                                tool=tool.name,
                                approved_hash=tool.approved_hash,
                                new_hash=digest,
                            )
                    elif tool.status == "changed" and digest == tool.approved_hash:
                        tool.status = "approved"  # the server went back to the approved definition
                self.store.put(TOOLS, tool.key, tool)
            for name, tool in known.items():
                if name not in seen and tool.status != "removed":
                    tool.status = "removed"
                    counts["removed"] += 1
                    self.store.put(TOOLS, tool.key, tool)
                    self.event("tool_removed", actor=actor, server_id=server.id, tool=name)
        self.event(
            "tools_discovered",
            actor=actor,
            server_id=server.id,
            total=len(seen),
            new=counts["new"],
            changed=counts["changed"],
            removed=counts["removed"],
        )
        return self.tools(server.id)

    # ---- tools and approvals ---------------------------------------------------------------

    def tools(self, server_id: str) -> list[McpTool]:
        return sorted(self.store.list_prefix(TOOLS, f"{server_id}/", McpTool), key=lambda tool: tool.name)

    def all_tools(self) -> list[McpTool]:
        return sorted(self.store.list(TOOLS, McpTool), key=lambda tool: tool.tool_name)

    def tool(self, server_id: str, name: str) -> McpTool:
        found = self.store.get(TOOLS, tool_key(server_id, name), McpTool)
        if found is None:
            raise NodestroError("mcp.tool_not_found", f"MCP tool not found: {name}", name=name)
        return found

    def decide(
        self,
        server_id: str,
        name: str,
        *,
        approve: bool,
        access: ToolAccess | None = None,
        expected_hash: str | None = None,
        actor: str | None = None,
    ) -> McpTool:
        """Approve or reject the tool's current definition. ``expected_hash`` guards against approving a definition
        that changed after the administrator looked at it."""
        with self._lock:
            tool = self.tool(server_id, name)
            if expected_hash is not None and expected_hash != tool.hash:
                raise NodestroError(
                    "mcp.definition_moved",
                    "The tool's definition changed since it was shown; review it again",
                    name=name,
                )
            if tool.status == "removed":
                raise NodestroError("mcp.tool_removed", f"The server no longer offers {name}", name=name)
            if access is not None:
                tool.access = access
            if approve:
                tool.status = "approved"
                tool.approved_definition = tool.definition
                tool.approved_hash = tool.hash
                tool.approved_by = actor
                tool.approved_at = now()
            else:
                tool.status = "rejected"
            self.store.put(TOOLS, tool.key, tool)
        self.event(
            "tool_approved" if approve else "tool_rejected",
            actor=actor,
            server_id=server_id,
            tool=name,
            hash=tool.hash,
            access=tool.access,
        )
        return tool

    # ---- the policy ------------------------------------------------------------------------

    def find(self, tool_name: str) -> tuple[McpServer, McpTool] | None:
        if not is_mcp_tool(tool_name):
            return None
        matches = self.store.find(TOOLS, "tool_name", [tool_name], McpTool)
        if not matches:
            return None
        tool = matches[0]
        server = self.store.get(SERVERS, tool.server_id, McpServer)
        return (server, tool) if server is not None else None

    def check(self, tool_name: str) -> tuple[McpServer, McpTool]:
        """The server and tool behind ``tool_name`` if an agent may use it now; a stable refusal otherwise."""
        found = self.find(tool_name)
        if found is None:
            raise NodestroError("mcp.tool_not_found", f"MCP tool not found: {tool_name}", name=tool_name)
        server, tool = found
        if not server.enabled:
            raise NodestroError(
                "mcp.server_disabled",
                f"The MCP server {server.name} is switched off",
                name=tool_name,
                server=server.name,
            )
        if tool.status == "changed":
            raise NodestroError(
                "mcp.tool_changed",
                f"{server.name} changed the definition of {tool.name}; an administrator must review the change",
                name=tool_name,
                server=server.name,
            )
        if tool.status != "approved" or tool.approved_hash is None or tool.approved_hash != tool.hash:
            raise NodestroError(
                "mcp.tool_not_approved", f"{tool_name} is not approved by an administrator", name=tool_name
            )
        if tool.access != "read" and self.write_gate is None:
            raise NodestroError(
                "mcp.write_not_supported",
                f"{tool_name} changes data; such tools need per-call approval, which is not available yet",
                name=tool_name,
            )
        return server, tool

    def agent_tool(self, tool_name: str, *, cancelled: Callable[[], bool] | None = None) -> Tool:
        """The tool an agent gets for ``tool_name`` (policy checked now and again before every call)."""
        from .tools import McpAgentTool

        server, tool = self.check(tool_name)
        return McpAgentTool(self, server, tool, cancelled=cancelled)

    def provenance(self, tool_name: str) -> dict[str, Any]:
        """Provenance of a call for the run record (threat model T14); empty for unknown names."""
        found = self.find(tool_name)
        if found is None:
            return {}
        server, tool = found
        return {
            "source": "mcp",
            "server_id": server.id,
            "server_name": server.name,
            "qualified_name": tool.qualified,
            "definition_hash": tool.approved_hash,
            "access": tool.access,
            "external_cost": "unknown",
        }

    def usable(self) -> list[McpTool]:
        result = []
        for tool in self.all_tools():
            try:
                self.check(tool.tool_name)
            except NodestroError:
                continue
            result.append(tool)
        return result

    def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[McpServer, McpTool, CallResult]:
        """Check the policy, call the tool and check the definition the server listed right before the call."""
        server, tool = self.check(tool_name)
        if tool.access != "read" and self.write_gate is not None:
            self.write_gate(tool, arguments)
        result = self.hub.call_tool(server, self.token(server), tool.name, arguments, cancelled=cancelled)
        if result.definition is None:
            self._mark(tool, "removed")
            raise NodestroError("mcp.tool_removed", f"{server.name} no longer offers {tool.name}", name=tool_name)
        live = definition_hash(result.definition)
        if live != tool.approved_hash:
            # Too late to stop this call's request? No: the listing came first and the definition matched or not
            # before the call was sent only when the server is honest. Record and block all further calls.
            tool.definition, tool.hash = result.definition, live
            self._mark(tool, "changed")
            self.event(
                "tool_changed", server_id=server.id, tool=tool.name, approved_hash=tool.approved_hash, new_hash=live
            )
            raise NodestroError(
                "mcp.tool_changed",
                f"{server.name} changed the definition of {tool.name}; an administrator must review the change",
                name=tool_name,
                server=server.name,
            )
        return server, tool, result

    def _mark(self, tool: McpTool, status: Any) -> None:
        with self._lock:
            tool.status = status
            self.store.put(TOOLS, tool.key, tool)

    # ---- audit -----------------------------------------------------------------------------

    def event(
        self,
        kind: EventKind,
        *,
        actor: str | None = None,
        server_id: str | None = None,
        tool: str | None = None,
        **details: Any,
    ) -> McpEvent:
        record = McpEvent(kind=kind, actor=actor, server_id=server_id, tool=tool, details=details)
        try:
            self.store.put(EVENTS, f"{record.at.isoformat()}_{record.id}", record)
        except Exception:
            logger.exception("Could not store MCP event %s", kind)
        return record

    def events(self, *, limit: int = 200) -> list[McpEvent]:
        items = self.store.items(EVENTS, McpEvent)
        items.sort(key=lambda pair: pair[0], reverse=True)
        if len(items) > MAX_EVENTS:
            for key, _ in items[MAX_EVENTS:]:
                self.store.delete(EVENTS, key)
        return [event for _, event in items[:limit]]
