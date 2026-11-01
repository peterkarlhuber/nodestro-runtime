"""The MCP service of one workspace: configuration, discovery, tool approvals, the policy check and audit events.

Access to an MCP tool is the intersection of three decisions (threat model, principle 2): the server is enabled, an
administrator approved this exact tool definition (pinned by its hash), and the agent lists the tool. The check runs
when an agent is built and again right before every call, against the definition the server lists at that moment
(threat model T4): a changed definition is blocked (``changed``) until an administrator accepts it.

Tools classified as ``write`` or ``destructive`` are usable only while the shared per-call approval gate is active
(``write_gate``, set by the workspace together with the run engine's tool gate; evolution plan S-2, WP-5) **and** the
caller asks that gate before every call (``gated``). The process-wide switch alone never opens them: every path that
builds an agent or runs a tool outside the gated engine path (agent test, tool test panel, releases) gets the default
``gated=False`` and is refused (independent review H-1).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterable
import logging
import secrets
import threading
from typing import Any

from orkexa import Tool

from ..problems import NodestroError
from ..release import McpToolRef
from ..schemas import now
from ..storage import DocumentStore
from . import oauth as oauth_flow
from .hub import CallResult, Credential, Discovery, McpHub
from .models import (
    EVENTS,
    RETIRED_SLUGS,
    SERVERS,
    TOOLS,
    EventKind,
    McpEvent,
    McpServer,
    McpServerIn,
    McpServerView,
    McpTool,
    RetiredSlug,
    ToolAccess,
    ToolDefinition,
    agent_tool_name,
    definition_hash,
    is_mcp_tool,
    make_slug,
    tool_key,
)
from .netguard import is_loopback_url, redact_url, url_secret_problem
from .oauth import OAuthCredentials, PendingAuthorization
from .stdio import StdioLaunch, launch
from .vault import TokenVault

logger = logging.getLogger(__name__)

#: Kept per tenant; older events are dropped (they hold no secrets and no tool arguments).
MAX_EVENTS = 2000


class McpService:
    def __init__(self, store: DocumentStore, hub: McpHub, vault: TokenVault) -> None:
        self.store = store
        self.hub = hub
        self.vault = vault
        #: True while the run engine asks for approval before every call that changes data (the shared tool gate,
        #: S-2/WP-5). False: MCP tools that change data are refused (``mcp.write_not_supported``).
        self.write_gate: bool = False
        #: The workspace's Python environments (:class:`~nodestro_runtime.python_envs.PythonEnvironments`): the
        #: approved packages and the container image of a local stdio server (WP-8). ``None``: stdio is refused.
        self.python: Any = None
        self._lock = threading.RLock()
        hub.stdio_launcher = self.stdio_launch
        self._scrub_url_secrets()

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
            **server.model_dump(exclude={"token_sealed", "oauth_pending_sealed", "oauth_state_digest"}),
            environment_state=self._environment_state(server),
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

    def credential(self, server: McpServer) -> Credential:
        """What a connection presents: a static token, or OAuth credentials for a signed-in OAuth server."""
        token = self.token(server)
        if server.auth != "oauth" or token is None:
            return token if server.auth == "bearer" else None
        return OAuthCredentials.loads(token)

    def _keep_refreshed(self, server: McpServer, credential: Credential) -> None:
        """Store tokens the hub refreshed during an operation (sealed, like the sign-in stored them)."""
        if not isinstance(credential, OAuthCredentials) or not credential.changed:
            return
        with self._lock:
            current = self.server(server.id)
            if current.auth != "oauth" or current.token_storage == "none":
                return  # signed out meanwhile
            credential.changed = False
            self._set_token(current, credential.dumps())
            current.oauth = {**current.oauth, **credential.summary()}
            self.store.put(SERVERS, current.id, current)
            # The caller saves its copy of the record afterwards (the check result): keep it in step.
            server.token_sealed, server.token_storage, server.oauth = (
                current.token_sealed,
                current.token_storage,
                current.oauth,
            )

    def _scrub_url_secrets(self) -> None:
        """Stored configurations from before addresses were checked for keys (independent review M-1): an address
        that carries one loses its query, fragment or secret-looking path, the server is switched off until an
        administrator enters the key as a token, and the address is removed from older audit events."""
        try:
            servers = self.store.items(SERVERS, McpServer)
        except Exception:  # an unreadable store fails later, where it is reported
            return
        for key, server in servers:
            if server.transport != "http" or url_secret_problem(server.url) is None:
                continue
            with self._lock:
                server.url = redact_url(server.url).replace("?…", "").replace("…", "")
                server.enabled = False
                server.last_error_code = "mcp.url_secret_removed"
                server.last_error_params = {}
                server.updated_at = now()
                self.store.put(SERVERS, key, server)
            self.event("server_changed", server_id=server.id, url=server.url, enabled=False, reason="url_secret")
        for key, event in self.store.items(EVENTS, McpEvent):
            address = event.details.get("url")
            if isinstance(address, str) and url_secret_problem(address) is not None:
                event.details["url"] = redact_url(address)
                self.store.put(EVENTS, key, event)

    def _check_input(self, data: McpServerIn) -> None:
        if data.transport == "stdio":
            self._check_stdio(data)
            return
        if data.stdio is not None:
            raise NodestroError("mcp.stdio_invalid", "Only a stdio server has a command")
        if not data.url:
            raise NodestroError("mcp.url_invalid", "The server needs an address", url="")
        self.hub.policy.check_url(data.url, allow_loopback=data.allow_loopback)
        where = url_secret_problem(data.url)
        if where is not None:
            # The address is stored and shown in the clear (independent review M-1, D-3): keys go in the token.
            raise NodestroError(
                "mcp.url_secret",
                "The address seems to contain a key or token. Remove it from the address and enter it as the "
                "server's token instead; tokens are stored sealed.",
                where=where,
            )
        if data.allow_loopback and not is_loopback_url(data.url):
            # The consent is for a development server on this computer only (independent review L-2).
            raise NodestroError(
                "mcp.loopback_not_local",
                "Loopback access can only be allowed for an address on this computer (localhost or 127.0.0.1)",
            )
        if data.auth == "oauth" and data.token:
            raise NodestroError(
                "mcp.oauth_token_input", "An OAuth server gets its tokens from the sign-in, not from a pasted token"
            )

    def _check_stdio(self, data: McpServerIn) -> None:
        if data.stdio is None:
            raise NodestroError("mcp.stdio_invalid", "A stdio server needs a command (packages and module)")
        if data.auth != "none" or data.token:
            # A local server gets no credentials from Nodestro (there is nothing to protect on the way: no network).
            raise NodestroError("mcp.stdio_invalid", "A local stdio server takes no token or sign-in")
        if data.allow_loopback:
            raise NodestroError("mcp.stdio_invalid", "A local stdio server has no address")
        self.require_isolation()

    def require_isolation(self) -> None:
        """A local server runs only in a container (D-5, ADR-0006): refuse without the Docker sandbox."""
        if self.python is None or not self.python.containers():
            raise NodestroError(
                "sandbox.isolation_required",
                "A local MCP server runs only in an isolated container; switch on the Docker sandbox first",
            )

    @staticmethod
    def _fields(data: McpServerIn) -> dict[str, Any]:
        fields = data.model_dump(exclude={"token"})
        if data.transport == "stdio" and data.stdio is not None:
            fields["url"] = data.stdio.address()
        return fields

    def add(self, data: McpServerIn, *, actor: str | None = None) -> McpServer:
        self._check_input(data)
        with self._lock:
            existing = self.servers()
            if len(existing) >= self.hub.limits.max_servers:
                raise NodestroError(
                    "mcp.too_many_servers",
                    f"At most {self.hub.limits.max_servers} MCP servers can be connected",
                    limit=self.hub.limits.max_servers,
                )
            # Slugs of removed servers are never given again (independent review L-6): an agent that still lists
            # ``mcp__<old slug>__<tool>`` must not silently reach a new server that happens to get the same name.
            taken = {item.slug for item in existing} | {key for key, _ in self.store.items(RETIRED_SLUGS, RetiredSlug)}
            server = McpServer(slug=make_slug(data.name, taken), **self._fields(data))
            self._set_token(server, data.token)
            self.store.put(SERVERS, server.id, server)
        self.event("server_added", actor=actor, server_id=server.id, url=redact_url(server.url), name=server.name)
        if data.token:
            self.event("token_changed", actor=actor, server_id=server.id, storage=server.token_storage)
        return server

    def update(self, server_id: str, data: McpServerIn, *, actor: str | None = None) -> McpServer:
        self._check_input(data)
        with self._lock:
            server = self.server(server_id)
            fields = self._fields(data)
            # A changed command is another program: like a new address, it loses its approvals.
            moved = fields["url"] != server.url or fields.get("stdio") != (
                server.stdio.model_dump() if server.stdio is not None else None
            )
            if moved or data.auth != server.auth:
                # OAuth credentials belong to the address and the issuer they were granted for.
                self._clear_oauth(server)
                if server.auth == "oauth" and data.token is None and server.token_storage != "none":
                    self._set_token(server, "")
            for key, value in fields.items():
                setattr(server, key, value)
            server.stdio = data.stdio
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
        self.event(
            "server_changed", actor=actor, server_id=server.id, url=redact_url(server.url), enabled=server.enabled
        )
        if data.token is not None:
            self.event("token_changed", actor=actor, server_id=server.id, storage=server.token_storage)
        return server

    def remove(self, server_id: str, *, actor: str | None = None) -> None:
        with self._lock:
            server = self.server(server_id)
            self.store.delete_prefix(TOOLS, f"{server.id}/")
            self.store.delete(SERVERS, server.id)
            self.store.put(RETIRED_SLUGS, server.slug, RetiredSlug(server_id=server.id))
            self.hub.session_tokens.pop(server.id, None)
            self.hub.session_tokens.pop(self._pending_key(server.id), None)
            self.hub.forget_oauth(server.id)
        if server.transport == "stdio" and self.python is not None:
            self.python.forget(self._scope(server.id))
        self.event("server_removed", actor=actor, server_id=server_id, name=server.name)

    # ---- local stdio servers (WP-8) ---------------------------------------------------------

    @staticmethod
    def _scope(server_id: str) -> str:
        return f"mcp:{server_id}"

    def _needed(self, server: McpServer) -> Any:
        from ..python_envs import needed_for_requirements

        if server.transport != "stdio" or server.stdio is None:
            raise NodestroError("mcp.stdio_invalid", "Only a stdio server has packages")
        return needed_for_requirements(server.stdio.requirements, server.slug)

    def _environment_state(self, server: McpServer) -> str:
        if server.transport != "stdio" or self.python is None:
            return ""
        try:
            return str(self.environment(server.id).state)
        except NodestroError:
            return "unsupported"

    def environment(self, server_id: str) -> Any:
        """The state of a stdio server's packages (:class:`~nodestro_runtime.python_envs.DependencyStatus`)."""
        server = self.server(server_id)
        needed = self._needed(server)
        if self.python is None:
            raise NodestroError("sandbox.isolation_required", "A local MCP server runs only in an isolated container")
        return self.python.status("mcp", server.id, needed, label=server.name)

    def plan_environment(self, server_id: str) -> Any:
        """Resolve the hash-pinned versions of a stdio server's packages for the administrator's approval (the
        approval and installation are the Python environments' own: ``approve(plan_id)``)."""
        self.require_isolation()
        server = self.server(server_id)
        return self.python.plan("mcp", server.id, self._needed(server), label=server.name)

    def stdio_launch(self, server: McpServer) -> StdioLaunch:
        """How the hub starts a connection: the server's command in a container of its approved image."""
        self.require_isolation()
        if server.transport != "stdio" or server.stdio is None:
            raise NodestroError("mcp.stdio_invalid", "Only a stdio server is started locally")
        image = self.python.image(self._scope(server.id))
        if image is None:
            raise NodestroError(
                "mcp.environment_not_ready",
                f"The packages of {server.name} are not approved and installed yet",
                server=server.name,
            )
        images = self.python.images
        return launch(
            image=image,
            module=server.stdio.module,
            args=list(server.stdio.args),
            memory_mb=server.stdio.memory_mb,
            docker=images.docker,
            docker_env=images._cli_environment(),
        )

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
            credential = self.credential(server)
            try:
                discovery = await self.hub.acall(self.hub.adiscover(server, credential))
            finally:
                self._keep_refreshed(server, credential)
        except NodestroError as exc:
            self._record_check(server, None, exc)
            raise
        return self._discovered(server, discovery, actor)

    def discover(self, server_id: str, *, actor: str | None = None) -> tuple[McpServer, Discovery]:
        """:meth:`adiscover` for callers without an event loop of their own (scripts, worker threads)."""
        server = self.server(server_id)
        try:
            credential = self.credential(server)
            try:
                discovery = self.hub.discover(server, credential)
            finally:
                self._keep_refreshed(server, credential)
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
        that changed after the administrator looked at it; an approval needs it (independent review L-3), so an
        approval always names the exact definition the administrator reviewed."""
        with self._lock:
            tool = self.tool(server_id, name)  # an unknown tool stays "not found"
            if approve and not expected_hash:
                raise NodestroError(
                    "mcp.hash_required",
                    "An approval must name the definition hash that was reviewed",
                    name=name,
                )
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

    def check(self, tool_name: str, *, gated: bool = False) -> tuple[McpServer, McpTool]:
        """The server and tool behind ``tool_name`` if an agent may use it now; a stable refusal otherwise.

        ``gated`` says that the caller asks the shared per-call approval gate before every call of this tool (the run
        engine with a tool gate). Only then are tools that change data usable, and only while ``write_gate`` is on."""
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
        if tool.access != "read" and not (self.write_gate and gated):
            raise NodestroError(
                "mcp.write_not_supported",
                f"{tool_name} changes data; it runs only in workflow runs that ask for approval before every call",
                name=tool_name,
            )
        return server, tool

    def agent_tool(self, tool_name: str, *, cancelled: Callable[[], bool] | None = None, gated: bool = False) -> Tool:
        """The tool an agent gets for ``tool_name`` (policy checked now and again before every call).

        ``gated``: the caller asks the per-call approval gate before every call (see :meth:`check`)."""
        from .tools import McpAgentTool

        server, tool = self.check(tool_name, gated=gated)
        return McpAgentTool(self, server, tool, cancelled=cancelled, gated=gated)

    def pin(self, tool_name: str) -> McpToolRef:
        """Freeze a usable tool for a release bundle (WP-9): server id and slug, approved hash, classification and
        definition; never an address or a token. Refuses tools an agent could not use now (in a gated run; the
        release itself never asks, so :class:`~nodestro_runtime.mcp.pinned.PinnedMcp` refuses changing tools when
        it runs)."""
        server, tool = self.check(tool_name, gated=True)
        assert tool.approved_hash is not None
        return McpToolRef(
            tool_name=tool.tool_name,
            server_id=server.id,
            server_slug=server.slug,
            name=tool.name,
            hash=tool.approved_hash,
            access=tool.access,
            definition=(tool.approved_definition or tool.definition).model_dump(mode="json"),
        )

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
                self.check(tool.tool_name, gated=True)  # assignable: usable in a run that asks before every call
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
        gated: bool = False,
    ) -> tuple[McpServer, McpTool, CallResult]:
        """Check the policy, call the tool and check the definition the server listed right before the call."""
        server, tool = self.check(tool_name, gated=gated)
        credential = self.credential(server)
        try:
            result = self.hub.call_tool(
                server,
                credential,
                tool.name,
                arguments,
                expected_hash=tool.approved_hash,
                cancelled=cancelled,
            )
        finally:
            self._keep_refreshed(server, credential)
        if result.definition is None:
            self._mark(tool, "removed", seen_hash=tool.approved_hash)
            raise NodestroError("mcp.tool_removed", f"{server.name} no longer offers {tool.name}", name=tool_name)
        live = definition_hash(result.definition)
        if live != tool.approved_hash:
            # The hub compared the listed definition with the approved hash on the same connection and did not send
            # the request (result.sent is False). Record the change and block all further calls.
            self._mark(tool, "changed", seen_hash=tool.approved_hash, definition=result.definition, live=live)
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

    def _mark(
        self,
        tool: McpTool,
        status: Any,
        *,
        seen_hash: str | None,
        definition: ToolDefinition | None = None,
        live: str | None = None,
    ) -> None:
        """Record what a call found out about ``tool``, on the stored record as it is now (independent review L-7):
        the copy read before the call may be stale, e.g. an administrator decided in the meantime. If the approval
        the call used is no longer the stored one, the administrator's newer decision stands and nothing changes."""
        with self._lock:
            current = self.store.get(TOOLS, tool.key, McpTool)
            if current is None or current.approved_hash != seen_hash:
                return
            if definition is not None and live is not None:
                current.definition, current.hash = definition, live
            current.status = status
            self.store.put(TOOLS, current.key, current)

    # ---- OAuth (WP-6) ----------------------------------------------------------------------

    @staticmethod
    def _pending_key(server_id: str) -> str:
        return f"{server_id}:oauth-pending"

    def _clear_oauth(self, server: McpServer) -> None:
        server.oauth = {}
        server.oauth_pending_sealed = None
        server.oauth_state_digest = None
        self.hub.session_tokens.pop(self._pending_key(server.id), None)
        self.hub.forget_oauth(server.id)

    def _oauth_server(self, server_id: str) -> McpServer:
        server = self.server(server_id)
        if server.auth != "oauth":
            raise NodestroError("mcp.oauth_not_configured", f"{server.name} does not use OAuth", server=server.name)
        self.hub.policy.check_url(server.url, allow_loopback=server.allow_loopback)
        return server

    def _begin_flow(
        self, server_id: str, redirect_uri: str, actor: str | None, client_metadata_url: str | None
    ) -> tuple[McpServer, Coroutine[Any, Any, tuple[str, PendingAuthorization]]]:
        server = self._oauth_server(server_id)
        previous = self.credential(server)
        return server, oauth_flow.guarded(
            oauth_flow.abegin(
                self.hub.policy,
                server,
                redirect_uri,
                previous=previous if isinstance(previous, OAuthCredentials) else None,
                client_metadata_url=client_metadata_url,
                resolver=self.hub.resolver,
                verify=self.hub.verify,
                actor=actor,
            )
        )

    def _started(self, server_id: str, pending: PendingAuthorization, actor: str | None) -> None:
        with self._lock:
            server = self.server(server_id)
            server.oauth_state_digest = oauth_flow.state_digest(pending.state)
            server.oauth = {**server.oauth, "pending_until": pending.expires_at}
            if self.vault.persistent:
                server.oauth_pending_sealed = self.vault.seal(self._pending_key(server.id), pending.dumps())
            else:
                server.oauth_pending_sealed = None
                self.hub.session_tokens[self._pending_key(server.id)] = pending.dumps()
            self.store.put(SERVERS, server.id, server)
        self.event("oauth_started", actor=actor, server_id=server_id, issuer=pending.issuer)

    async def abegin_oauth(
        self, server_id: str, redirect_uri: str, *, actor: str | None = None, client_metadata_url: str | None = None
    ) -> str:
        """Start a sign-in; returns the authorization URL the person opens in the browser."""
        server, flow = self._begin_flow(server_id, redirect_uri, actor, client_metadata_url)
        try:
            url, pending = await self.hub.acall(flow)
        except NodestroError as exc:
            self.event("oauth_failed", actor=actor, server_id=server.id, code=exc.code, step="start")
            raise
        self._started(server.id, pending, actor)
        return url

    def begin_oauth(
        self, server_id: str, redirect_uri: str, *, actor: str | None = None, client_metadata_url: str | None = None
    ) -> str:
        """:meth:`abegin_oauth` for callers without an event loop of their own."""
        server, flow = self._begin_flow(server_id, redirect_uri, actor, client_metadata_url)
        try:
            url, pending = self.hub.run(flow, timeout=server.timeout_seconds * 4 + 5)
        except NodestroError as exc:
            self.event("oauth_failed", actor=actor, server_id=server.id, code=exc.code, step="start")
            raise
        self._started(server.id, pending, actor)
        return url

    def _pending(self, state: str) -> tuple[McpServer, PendingAuthorization]:
        digest = oauth_flow.state_digest(state)
        for server in self.servers():
            if server.auth != "oauth" or not server.oauth_state_digest:
                continue
            if not secrets.compare_digest(server.oauth_state_digest, digest):
                continue
            if server.oauth_pending_sealed:
                text = self.vault.open(self._pending_key(server.id), server.oauth_pending_sealed)
            else:
                text = self.hub.session_tokens.get(self._pending_key(server.id))
            pending = PendingAuthorization.loads(text) if text else None
            if pending is not None:
                return server, pending
        raise NodestroError("mcp.oauth_state_invalid", "The sign-in does not belong to a started request")

    def _complete_flow(
        self, state: str, code: str, iss: str | None
    ) -> tuple[McpServer, PendingAuthorization, Coroutine[Any, Any, OAuthCredentials]]:
        server, pending = self._pending(state)
        flow = oauth_flow.guarded(
            oauth_flow.acomplete(
                self.hub.policy,
                server,
                pending,
                state=state,
                code=code,
                iss=iss,
                resolver=self.hub.resolver,
                verify=self.hub.verify,
            )
        )
        return server, pending, flow

    def _completed(
        self,
        server: McpServer,
        pending: PendingAuthorization,
        credentials: OAuthCredentials | None,
        error: NodestroError | None,
        actor: str | None,
    ) -> McpServer:
        actor = actor or pending.actor
        with self._lock:
            current = self.server(server.id)
            self._clear_pending(current)
            if credentials is not None:
                self.hub.forget_oauth(current.id)
                self._set_token(current, credentials.dumps())
                current.oauth = credentials.summary()
                current.updated_at = now()
            self.store.put(SERVERS, current.id, current)
        if error is not None:
            self.event("oauth_failed", actor=actor, server_id=server.id, code=error.code, step="callback")
            raise error
        assert credentials is not None
        self.event(
            "oauth_connected",
            actor=actor,
            server_id=server.id,
            issuer=credentials.issuer,
            scope=credentials.scope,
            storage=current.token_storage,
        )
        return current

    async def acomplete_oauth(
        self, state: str, code: str, iss: str | None = None, *, actor: str | None = None
    ) -> McpServer:
        """The browser's callback: check it, exchange the code and store the credentials sealed.

        A started sign-in is used once: it is discarded whether the exchange succeeds or fails."""
        server, pending, flow = self._complete_flow(state, code, iss)
        try:
            credentials = await self.hub.acall(flow)
        except NodestroError as exc:
            return self._completed(server, pending, None, exc, actor)
        return self._completed(server, pending, credentials, None, actor)

    def complete_oauth(self, state: str, code: str, iss: str | None = None, *, actor: str | None = None) -> McpServer:
        """:meth:`acomplete_oauth` for callers without an event loop of their own."""
        server, pending, flow = self._complete_flow(state, code, iss)
        try:
            credentials = self.hub.run(flow, timeout=server.timeout_seconds * 2 + 5)
        except NodestroError as exc:
            return self._completed(server, pending, None, exc, actor)
        return self._completed(server, pending, credentials, None, actor)

    def _clear_pending(self, server: McpServer) -> None:
        server.oauth_pending_sealed = None
        server.oauth_state_digest = None
        server.oauth.pop("pending_until", None)
        self.hub.session_tokens.pop(self._pending_key(server.id), None)

    def sign_out(self, server_id: str, *, actor: str | None = None) -> McpServer:
        """Forget the OAuth credentials (the authorization server's grant stays until it expires or is revoked)."""
        with self._lock:
            server = self._oauth_server(server_id)
            self._clear_oauth(server)
            self._set_token(server, "")
            server.updated_at = now()
            self.store.put(SERVERS, server.id, server)
        self.event("oauth_disconnected", actor=actor, server_id=server.id)
        return server

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
