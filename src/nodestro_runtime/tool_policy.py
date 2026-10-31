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

Asking before a call (S-2) builds on it: a tool *binding* of an agent says when to ask (:class:`ToolBinding`), and
:func:`decide` turns capability, binding and the run's situation into allow, deny or ask, without any I/O. Waiting
for the answer is :mod:`nodestro_runtime.tool_approvals`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    if isinstance(value, dict) and source is not None:
        value = {**value, "source": source}
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


# ---- S-2: when to ask before a call ---------------------------------------------------------------------------

AskMode = Literal["auto", "always", "never"]
GateAction = Literal["allow", "deny", "ask"]
#: The run triggers where a pre-grant can stand in for a person (the scopes of :class:`ToolPreGrant`).
PreGrantScope = Literal["schedule", "batch", "benchmark"]


class ToolBinding(BaseModel):
    """When an agent asks before calling one of its tools.

    - ``auto`` (every new binding): ask when the tool does more than read.
    - ``always``: ask for every call.
    - ``never``: do not ask. Counts only for ``read`` tools, for ``write`` tools with a ``reason`` and for legacy
      exemptions; never for ``destructive`` tools. Otherwise it is treated as ``auto``.

    A **legacy exemption** (``legacy``) is a binding that existed before S-2; it holds only for the tool version it
    was bound to (``bound_version``): a new version of the tool ends it.
    """

    model_config = ConfigDict(frozen=True)

    ask: AskMode = "auto"
    reason: str | None = Field(default=None, min_length=10, max_length=500)
    legacy: bool = False
    bound_version: int | None = None


#: A binding nobody configured: a new binding.
NEW_BINDING = ToolBinding()


class ToolPreGrant(BaseModel):
    """A person's advance approval of one tool for one unattended scope (D10c): narrow, traceable, revocable.

    Matches only the tool name, its version (own and generated tools) and the scope; voided by a new tool version,
    by its expiry and by revocation. Never covers ``destructive`` or MCP tools."""

    model_config = ConfigDict(frozen=True)

    id: str
    tool: str
    version: int | None = None
    scope: PreGrantScope
    scope_id: str
    granted_by: str
    granted_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None


class CallContext(BaseModel):
    """Where a call happens."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    node_id: str
    #: The trigger of the root run (a sub-workflow inherits its parent's).
    trigger: str
    #: A person can answer a question while the run waits.
    attended: bool
    #: The tools an orchestrator granted (AO-11); ``None``: no grant limits the agent.
    grants: frozenset[str] | None = None
    #: Who set ``grants`` (node id or orchestrator step), recorded with a ``tool.not_granted`` denial.
    grant_scope: str | None = None
    #: The unattended scope of the run (e.g. ``("schedule", "sch_1")``) and the pre-grants stored for it.
    scope: tuple[PreGrantScope, str] | None = None
    pre_grants: tuple[ToolPreGrant, ...] = ()
    #: ADR-0007 D-7: approved read-only MCP tools may run unattended (only after the MCP security acceptance).
    mcp_unattended: bool = False


class GateDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: GateAction
    code: str | None = None
    reason: str | None = None
    #: The pre-grant that allowed an unattended call.
    pre_grant_id: str | None = None


ALLOW = GateDecision(action="allow")
ASK = GateDecision(action="ask")


def _deny(code: str, reason: str) -> GateDecision:
    return GateDecision(action="deny", code=code, reason=reason)


def wants_question(capability: ToolCapability, binding: ToolBinding | None, *, version: int | None = None) -> bool:
    """Whether a person would be asked before this call (the binding's rule; see :class:`ToolBinding`)."""
    binding = binding or NEW_BINDING
    if binding.legacy and (binding.ask != "never" or binding.bound_version is None or binding.bound_version != version):
        binding = NEW_BINDING  # an exemption never outlives the tool version it was bound to
    if binding.ask == "always":
        return True
    if binding.ask == "never":
        if capability.access == "destructive":
            return True
        # A write tool needs a recorded reason (or a legacy exemption) to go unasked.
        return capability.access == "write" and not binding.legacy and binding.reason is None
    return capability.access != "read"


def matching_pre_grant(
    pre_grants: Iterable[ToolPreGrant],
    *,
    tool: str,
    version: int | None,
    scope: tuple[PreGrantScope, str] | None,
    at: datetime,
) -> ToolPreGrant | None:
    if scope is None:
        return None
    for grant in pre_grants:
        if grant.tool != tool or (grant.scope, grant.scope_id) != scope:
            continue
        if grant.version is not None and grant.version != version:
            continue
        if version is not None and grant.version is None:
            continue  # a grant for "any version" would survive a changed tool
        if grant.revoked_at is not None and grant.revoked_at <= at:
            continue
        if grant.expires_at is not None and grant.expires_at <= at:
            continue
        return grant
    return None


def decide(
    *,
    tool: str,
    kind: str,
    capability: ToolCapability,
    binding: ToolBinding | None,
    context: CallContext,
    at: datetime,
    version: int | None = None,
) -> GateDecision:
    """Allow, deny or ask for one call (no I/O). The order: grants narrow first, then the binding decides whether to
    ask; an unattended run cannot ask, so it denies unless a matching pre-grant exists. An approval never changes the
    sandbox the call runs in."""
    if context.grants is not None and tool not in context.grants:
        scope = context.grant_scope or "orchestrator"
        return _deny("tool.not_granted", f"The tool {tool} was not granted to this agent (by {scope})")
    if kind == "mcp" and not context.attended and not (context.mcp_unattended and capability.reads_only):
        return _deny("tool.approval_unattended", f"The MCP tool {tool} cannot run without a person watching")
    if kind == "mcp" and not capability.reads_only:
        # MCP is strict (D10c): a changing MCP call is asked every time, whatever the binding says. No ``never`` and
        # no legacy exemption applies, because such calls were refused outright before per-call approvals existed.
        return ASK
    if not wants_question(capability, binding, version=version):
        return ALLOW
    if context.attended:
        return ASK
    if kind != "mcp" and capability.access != "destructive":
        grant = matching_pre_grant(context.pre_grants, tool=tool, version=version, scope=context.scope, at=at)
        if grant is not None:
            return GateDecision(action="allow", pre_grant_id=grant.id)
    return _deny(
        "tool.approval_unattended",
        f"The tool {tool} needs a person's approval for each call, and nobody can answer in this run",
    )


__all__ = [
    "ALLOW",
    "ASK",
    "BUILTIN_CAPABILITIES",
    "NEW_BINDING",
    "SERVER_TOOL_CAPABILITIES",
    "UNKNOWN",
    "Access",
    "AskMode",
    "CallContext",
    "CapabilitySource",
    "GateAction",
    "GateDecision",
    "PreGrantScope",
    "Reach",
    "ToolBinding",
    "ToolCapability",
    "ToolPreGrant",
    "capability_of",
    "decide",
    "matching_pre_grant",
    "parse_capability",
    "server_tool_capability",
    "wants_question",
]
