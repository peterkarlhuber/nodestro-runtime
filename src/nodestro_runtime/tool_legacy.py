"""Legacy exemptions for per-call tool approvals (S-2, decision D10b).

Tool bindings that exist when per-call approvals are activated keep working without asking, but **visibly**: the
activation step (:func:`stamp_workflow`, called for every stored workflow and its versions by
:meth:`~nodestro_runtime.workspace.Workspace.stamp_legacy_tool_bindings`) writes each such binding as
``ask: "never"`` with ``legacy: true`` and the tool version it was bound to. Nothing else creates a legacy exemption:

- :func:`keep_only_inherited` runs on every save of a workflow and drops legacy entries that the stored workflow did
  not already have for the same node, tool and version. A copied agent, an imported or new workflow, an agent from a
  template, or an entry an API client invents therefore becomes a new binding (``auto``).
- A new tool version ends the exemption at the next call (``tool_policy.wants_question`` compares the version).
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, Field

from .schemas import AgentConfig, Workflow, WorkflowIn
from .tool_policy import ToolBinding

#: The current version of a tool by name; ``None`` when there is no such tool (nothing is stamped for it).
VersionOf = Callable[[str], int | None]


class LegacyBinding(BaseModel):
    workflow_id: str | None = None
    workflow_version: int | None = None
    node_id: str
    tool: str
    bound_version: int


def stamp_agent(config: AgentConfig, version_of: VersionOf) -> tuple[AgentConfig, list[tuple[str, int]]]:
    """``config`` with every tool that has no binding yet stamped as a legacy exemption; and what was stamped."""
    policies = dict(config.tool_policies)
    stamped: list[tuple[str, int]] = []
    for name in config.tools:
        if name in policies:
            continue
        version = version_of(name)
        if version is None:
            continue
        policies[name] = ToolBinding(ask="never", legacy=True, bound_version=version)
        stamped.append((name, version))
    if not stamped:
        return config, []
    return config.model_copy(update={"tool_policies": policies}), stamped


def stamp_workflow(
    workflow: WorkflowIn | Workflow, version_of: VersionOf
) -> tuple[WorkflowIn | Workflow, list[LegacyBinding]]:
    """A workflow (or a version snapshot) with its existing bindings stamped; idempotent."""
    nodes = []
    found: list[LegacyBinding] = []
    for node in workflow.nodes:
        if node.agent is None:
            nodes.append(node)
            continue
        agent, stamped = stamp_agent(node.agent, version_of)
        nodes.append(node.model_copy(update={"agent": agent}) if stamped else node)
        found.extend(LegacyBinding(node_id=node.id, tool=tool, bound_version=version) for tool, version in stamped)
    if not found:
        return workflow, []
    return workflow.model_copy(update={"nodes": nodes}), found


def _legacy_entries(workflow: WorkflowIn | Workflow | None) -> set[tuple[str, str, int | None]]:
    if workflow is None:
        return set()
    return {
        (node.id, name, binding.bound_version)
        for node in workflow.nodes
        if node.agent is not None
        for name, binding in node.agent.tool_policies.items()
        if binding.legacy
    }


def keep_only_inherited(workflow: Workflow, previous: Workflow | None) -> Workflow:
    """Drop legacy entries the stored workflow did not have (same node, tool and bound version): a legacy exemption
    is never created by saving, copying or importing, only by the activation step."""
    inherited = _legacy_entries(previous)
    changed = False
    nodes = []
    for node in workflow.nodes:
        agent = node.agent
        if agent is None or not any(binding.legacy for binding in agent.tool_policies.values()):
            nodes.append(node)
            continue
        policies = {
            name: binding
            for name, binding in agent.tool_policies.items()
            if not binding.legacy or (node.id, name, binding.bound_version) in inherited
        }
        if len(policies) != len(agent.tool_policies):
            changed = True
            node = node.model_copy(update={"agent": agent.model_copy(update={"tool_policies": policies})})
        nodes.append(node)
    return workflow.model_copy(update={"nodes": nodes}) if changed else workflow


class LegacyReport(BaseModel):
    """What the activation step stamped (shown as *exceptions from before S-2*)."""

    bindings: list[LegacyBinding] = Field(default_factory=list)
    workflows: int = 0
    versions: int = 0


__all__ = [
    "LegacyBinding",
    "LegacyReport",
    "VersionOf",
    "keep_only_inherited",
    "stamp_agent",
    "stamp_workflow",
]
