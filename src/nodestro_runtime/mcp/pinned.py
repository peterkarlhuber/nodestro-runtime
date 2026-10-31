"""The MCP tools of a release (WP-9): only the tools frozen in its bundle, only at their published definitions.

A release run (a published API) must not pick up tools or definitions that were approved after publishing, and
must stop when a frozen tool changed. :class:`PinnedMcp` stands in for the workspace's
:class:`~nodestro_runtime.mcp.service.McpService` in a release's tool library: every lookup and every call checks
the live policy (server enabled, tool approved, write gate) *and* that the approved hash and classification still
equal the frozen :class:`~nodestro_runtime.mcp.models.McpToolRef`. Tokens stay in the tenant's configuration;
the bundle names servers by id only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from orkexa import Tool

from ..problems import NodestroError
from ..release import McpToolRef
from .hub import CallResult
from .models import McpServer, McpTool
from .service import McpService


class PinnedMcp:
    """The subset of :class:`McpService` a tool library uses, limited to a release's frozen MCP tools."""

    def __init__(self, service: McpService, refs: Mapping[str, McpToolRef]) -> None:
        self.service = service
        self.refs = dict(refs)

    def servers(self) -> list[McpServer]:
        ids = {ref.server_id for ref in self.refs.values()}
        return [server for server in self.service.servers() if server.id in ids]

    def find(self, tool_name: str) -> tuple[McpServer, McpTool] | None:
        if tool_name not in self.refs:
            return None
        return self.service.find(tool_name)

    def check(self, tool_name: str) -> tuple[McpServer, McpTool]:
        ref = self.refs.get(tool_name)
        if ref is None:
            raise NodestroError("mcp.tool_not_in_release", f"{tool_name} is not part of this release", name=tool_name)
        server, tool = self.service.check(tool_name)
        if server.id != ref.server_id or tool.approved_hash != ref.hash or tool.access != ref.access:
            raise NodestroError(
                "mcp.release_tool_changed",
                f"{tool_name} was approved differently after this release was published; publish again",
                name=tool_name,
                server=server.name,
            )
        return server, tool

    def usable(self) -> list[McpTool]:
        result = []
        for name in self.refs:
            try:
                result.append(self.check(name)[1])
            except NodestroError:
                continue
        return result

    def agent_tool(self, tool_name: str, *, cancelled: Callable[[], bool] | None = None) -> Tool:
        from .tools import McpAgentTool

        server, tool = self.check(tool_name)
        return McpAgentTool(self, server, tool, cancelled=cancelled)  # type: ignore[arg-type]

    def call(
        self, tool_name: str, arguments: dict[str, Any], *, cancelled: Callable[[], bool] | None = None
    ) -> tuple[McpServer, McpTool, CallResult]:
        self.check(tool_name)
        return self.service.call(tool_name, arguments, cancelled=cancelled)

    def provenance(self, tool_name: str) -> dict[str, Any]:
        found = self.service.provenance(tool_name)
        ref = self.refs.get(tool_name)
        if found and ref is not None:
            found["release_hash"] = ref.hash
        return found
