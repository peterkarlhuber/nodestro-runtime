"""What the core runtime needs to know about MCP tools, without importing the MCP client.

The engine, the agent builder and the tool library use only these names; the client itself
(:mod:`nodestro_runtime.mcp`) is attached to a workspace's tool library at run time. A copy of the runtime without
the client (an exported project) therefore knows no MCP tools and refuses them as unknown.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import os

from .problems import NodestroError

#: Prefix of every MCP tool name agents see: ``mcp__<server slug>__<tool>``.
TOOL_PREFIX = "mcp__"
#: Set to ``1`` by the operator after the MCP security acceptance: MCP tools may then run without a person
#: (schedules, batches, test series, published APIs; ADR-0007 D-7). Off by default.
UNATTENDED_ENV = "NODESTRO_MCP_UNATTENDED"
#: Run triggers without a person watching.
UNATTENDED_TRIGGERS = frozenset({"schedule", "batch", "benchmark", "api"})


def is_mcp_tool(name: str) -> bool:
    return name.startswith(TOOL_PREFIX)


def unattended_mcp_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(UNATTENDED_ENV, "").strip() == "1"


def check_unattended_mcp(names: Iterable[str]) -> None:
    """Refuse MCP tools in a run without a person unless the operator enabled them (ADR-0007 D-7)."""
    mcp = [name for name in names if is_mcp_tool(name)]
    if mcp and not unattended_mcp_enabled():
        raise NodestroError(
            "mcp.unattended_not_enabled",
            "MCP tools cannot be used in runs without a person yet (schedules, batches, test series, published "
            "APIs); this needs the MCP security acceptance",
            names=", ".join(mcp),
        )
