"""Warnings before an unattended start: which tool calls the per-call rules would refuse (S-2, risk R1).

A schedule, a batch, a test series or a published API runs without anybody who could answer a question. When
per-call tool approvals are switched on (``NODESTRO_TOOL_APPROVALS=1``), a call that would need a person's
approval is then refused at call time (``tool.approval_unattended``). :func:`unattended_tool_issues` finds those
calls **before** the start or the publication, so the person can fix them first.

It is not a second rule engine: it asks :func:`~nodestro_runtime.tool_policy.decide` with the same call context the
engine builds (:meth:`~nodestro_runtime.tool_approvals.ToolGate.call_context`), the same capability, version and
kind (:func:`~nodestro_runtime.tool_approvals.tool_facts`) and the agent's own binding, so it sees the tool's
classification, its version, legacy exemptions and the stored pre-grants exactly as the call would. It only warns:
it never grants, never changes a binding and never lets a call through; the engine still decides at every call.

What it cannot know in advance (stated in the findings' documentation, not guessed):

- tools an orchestrator narrows at run time (``CallContext.grants``) and teams it selects by capability;
- a pre-grant given, revoked or expired between this check and the call;
- a batch or a test series run that does not exist yet: pre-grants name a started batch, so before the start none
  can match (the finding says so);
- Nodestro's own run tools (memory, run files) and the provider's server tools, which never pass the gate.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from .engine import MAX_DEPTH, node_label
from .graph import workflow_refs
from .problems import CheckIssue
from .schemas import Workflow, now
from .tool_approvals import tool_facts
from .tool_policy import ToolBinding, decide

if TYPE_CHECKING:
    from .workspace import Workspace

#: An agent node as the check sees it: (workflow, is_root, node_id, label, tools, bindings).
_Agent = tuple[Workflow, bool, str, str, list[str], dict[str, ToolBinding]]

UnattendedTrigger = Literal["schedule", "batch", "benchmark", "api"]

#: The codes of the findings (``errors.tool.*`` in the studio): why the call would be refused, and so how to fix it.
CODES = {
    "destructive": "tool.unattended_destructive",
    "mcp": "tool.unattended_mcp",
    "api": "tool.unattended_api",
    "not_started": "tool.unattended_not_started",
    "pre_grant_outdated": "tool.unattended_pre_grant_outdated",
    "pre_grant_missing": "tool.unattended_pre_grant_missing",
    "not_granted": "tool.unattended_denied",
}


def _agents(ctx: Workspace, workflow: Workflow) -> Iterator[_Agent]:
    """Every agent node of ``workflow``, its sub-workflows and its named orchestrator teams (as a run would pin
    them): ``(workflow, is_root, node_id, label, tools, bindings)``."""
    seen: set[tuple[str, int | None]] = set()

    def walk(current: Workflow, depth: int, root: bool) -> Iterator[_Agent]:
        for node in current.nodes:
            if node.type == "agent" and node.agent is not None:
                yield current, root, node.id, node_label(node), list(node.agent.tools), dict(node.agent.tool_policies)
            if depth >= MAX_DEPTH:
                continue
            for ref in workflow_refs(node):
                key = (ref.workflow_id, ref.version)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    child, _ = ctx.resolve_workflow(ref.workflow_id, ref.version)
                except KeyError:
                    continue  # reported by the workflow check
                yield from walk(child, depth + 1, False)

    yield from walk(workflow, 0, True)


def unattended_tool_issues(
    ctx: Workspace,
    workflow: Workflow,
    *,
    trigger: UnattendedTrigger,
    trigger_ref: str | None = None,
    at: datetime | None = None,
) -> list[CheckIssue]:
    """The tool calls of ``workflow`` that would be refused in a run started by ``trigger`` (``trigger_ref``: the
    schedule or the started batch; ``None`` before it exists). Empty while per-call approvals are off: then nothing
    is refused. Warnings only, one per agent and tool; nodes of sub-workflows carry the workflow's name."""
    gate = ctx.runs.tool_gate
    if gate is None:
        return []
    at = at or now()
    found: list[CheckIssue] = []
    reported: set[tuple[str, str, str]] = set()
    for flow, root, node_id, label, tools, bindings in _agents(ctx, workflow):
        situation = gate.call_context(run_id="preflight", node_id=node_id, trigger=trigger, trigger_ref=trigger_ref)
        for name in tools:
            capability, version, kind, _ = tool_facts(ctx.tools, name)
            verdict = decide(
                tool=name,
                kind=kind,
                capability=capability,
                binding=bindings.get(name),
                context=situation,
                at=at,
                version=version,
            )
            if verdict.action != "deny":
                continue
            key = (flow.id, node_id, name)
            if key in reported:
                continue
            reported.add(key)
            if verdict.code != "tool.approval_unattended":
                why = "not_granted"
            elif kind == "mcp":
                why = "mcp"
            elif capability.access == "destructive":
                why = "destructive"
            elif trigger == "api":
                why = "api"
            elif situation.scope is None:
                why = "not_started"
            elif any(grant.tool == name and grant.revoked_at is None for grant in situation.pre_grants):
                why = "pre_grant_outdated"  # a grant for another version, or one that expired
            else:
                why = "pre_grant_missing"
            found.append(
                CheckIssue(
                    code=CODES[why],
                    message=f"{label}: calls of {name} would be refused ({why.replace('_', ' ')})",
                    level="warning",
                    node_id=node_id if root else None,
                    params={
                        "tool": name,
                        "node": label,
                        "workflow": flow.name,
                        "trigger": trigger,
                        "access": capability.access,
                        "reach": capability.reach,
                        "version": version,
                        "denial": verdict.code or "",
                    },
                )
            )
    return found


__all__ = ["CODES", "UnattendedTrigger", "unattended_tool_issues"]
