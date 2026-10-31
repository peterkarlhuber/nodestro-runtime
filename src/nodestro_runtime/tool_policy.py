"""What a tool may do: the capability statement of every tool (S-1, decision D10).

One shared model for built-in, own (``custom``), generated and MCP tools, for the provider's server tools and for
the tools of orchestrated agents (evolution plan S-1/S-2, adaptive orchestration AO-11, MCP WP-5; see the design in
nodestro-studio ``docs/evolution/s-tool-approvals.md``). There is no second permission model next to it.

A :class:`ToolCapability` says

- ``access``: ``read`` (changes nothing anywhere), ``write`` (creates or changes data) or ``destructive`` (deletes,
  pays, sends or cannot be undone);
- ``reach``: ``internal`` (stays inside Nodestro) or ``external`` (talks to systems outside it);
- ``source``: who made the statement: ``nodestro`` (fixed for built-in and provider tools), ``author`` (the person
  who saved the tool), ``admin`` (an administrator's classification, e.g. of an MCP tool), ``derived`` (computed
  from where the tool runs) or ``default`` (nobody said anything).

A missing or unknown statement means ``write`` + ``external`` (:data:`UNKNOWN`): only Nodestro or a person can make
a tool look harmless, never its own code, its description or a model. The statement is security information, not
proof of harmless behavior: a ``read`` + ``external`` tool can still send data out through its arguments.

This module only describes; it changes no behavior. Asking before a call (S-2) builds on it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .sandbox_policy import Isolation

Access = Literal["read", "write", "destructive"]
Reach = Literal["internal", "external"]
CapabilitySource = Literal["nodestro", "author", "admin", "derived", "default"]


class ToolCapability(BaseModel):
    """What a tool may do (see the module documentation)."""

    model_config = ConfigDict(frozen=True)

    access: Access
    reach: Reach
    source: CapabilitySource

    @property
    def reads_only(self) -> bool:
        return self.access == "read"


#: Nobody made a statement: assume the worst that is not destructive.
UNKNOWN = ToolCapability(access="write", reach="external", source="default")

_READ_INTERNAL = ToolCapability(access="read", reach="internal", source="nodestro")

#: The built-in tools of Nodestro itself (``builtin_tools.BUILTIN_TOOLS``). Tools that extensions add are not in
#: here: without their own statement they count as :data:`UNKNOWN`.
BUILTIN_CAPABILITIES: dict[str, ToolCapability] = {
    "calculator": _READ_INTERNAL,
    "current_datetime": _READ_INTERNAL,
    "days_between": _READ_INTERNAL,
    "text_statistics": _READ_INTERNAL,
}

#: The provider's server tools (``catalog.SERVER_TOOLS``). They run at the provider and never pass Nodestro's tool
#: hook, so they cannot be held per call.
SERVER_TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    "web_search": ToolCapability(access="read", reach="external", source="nodestro"),
    "web_fetch": ToolCapability(access="read", reach="external", source="nodestro"),
    "code_execution": ToolCapability(access="write", reach="internal", source="nodestro"),
}

#: Sandboxes without any network (ADR-0006): a container, or the built-in Linux sandbox (seccomp, no sockets).
_NO_NETWORK: frozenset[Isolation] = frozenset({"container", "kernel"})


def parse_capability(value: Any, *, source: CapabilitySource | None = None) -> ToolCapability | None:
    """A stored statement (a :class:`ToolCapability` or its JSON); ``None`` when it is missing or not valid.

    ``source`` replaces the stored source, so a statement can never claim to come from someone else (e.g. a tool
    file that says ``"source": "nodestro"``).
    """
    if value is None:
        return None
    try:
        found = value if isinstance(value, ToolCapability) else ToolCapability.model_validate(value)
    except (TypeError, ValueError):
        return None
    return found if source is None else found.model_copy(update={"source": source})


def capability_of(
    *,
    name: str,
    kind: str,
    stated: Any = None,
    stated_by: CapabilitySource = "author",
    isolation: Isolation | None = None,
) -> ToolCapability:
    """The capability of one tool.

    - ``builtin``: Nodestro's own statement; other trusted tools (extensions) count as unknown unless ``stated``.
    - ``mcp``: the administrator's classification in ``stated``; ``reach`` is always ``external``.
    - ``custom`` and ``generated``: the author's statement in ``stated``; without one :data:`UNKNOWN`.
    - Without network (``isolation`` is a container or the built-in sandbox), ``reach`` is ``internal`` and the
      statement is marked ``derived`` when it was not ``internal`` already. ``access`` is never derived.
    """
    if kind == "builtin" and name in BUILTIN_CAPABILITIES:
        return BUILTIN_CAPABILITIES[name]
    if kind == "mcp":
        found = parse_capability(stated, source="admin") or UNKNOWN
        return found.model_copy(update={"reach": "external"})
    found = parse_capability(stated, source=stated_by) or UNKNOWN
    if kind in ("custom", "generated") and isolation in _NO_NETWORK and found.reach == "external":
        return ToolCapability(access=found.access, reach="internal", source="derived")
    return found


def server_tool_capability(key: str) -> ToolCapability:
    """The capability of a provider server tool; unknown keys count as :data:`UNKNOWN`."""
    return SERVER_TOOL_CAPABILITIES.get(key, UNKNOWN)


__all__ = [
    "BUILTIN_CAPABILITIES",
    "SERVER_TOOL_CAPABILITIES",
    "UNKNOWN",
    "Access",
    "CapabilitySource",
    "Reach",
    "ToolCapability",
    "capability_of",
    "parse_capability",
    "server_tool_capability",
]
