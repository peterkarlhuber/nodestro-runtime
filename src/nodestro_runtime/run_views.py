"""Views over stored runs: pending approvals and the usage of a run per model.

Approvals live in the runs themselves (``NodeRun.approval``), so they survive restarts
and need no extra storage. A sub-workflow's run is stored on its own but belongs to
its parent: its approvals are listed with the parent's name, and its cost is counted
once, in the parent run.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from .engine import add_usage, node_label
from .schemas import ApprovalConfig, Run, RunSummary, RunTrigger, Usage

if TYPE_CHECKING:
    from .engine import RunManager
    from .workspace import Workspace


class ReviewItem(BaseModel):
    node_id: str
    label: str
    value: Any = None


class PendingApproval(BaseModel):
    run_id: str
    #: The top-level run (differs for an approval inside a sub-workflow).
    root_run_id: str
    workflow_id: str
    workflow_name: str
    #: Workflow names from the top-level run down to this one.
    path: list[str] = Field(default_factory=list)
    node_id: str
    label: str
    instructions: str = ""
    allow_comment: bool = True
    require_comment_on_reject: bool = False
    on_reject: str = "branch"
    #: Whether a rejection has a path to follow (else the rejected branch simply ends).
    has_rejected_branch: bool = False
    has_approved_branch: bool = False
    waiting_since: datetime | None = None
    trigger: RunTrigger = "manual"
    items: list[ReviewItem] = Field(default_factory=list)


def _paused_runs(ctx: Workspace) -> list[Run]:
    found: dict[str, Run] = {}
    for summary in ctx.store.list("runs", RunSummary, limit=5000):
        if summary.status == "paused":
            run = ctx.runs.get(summary.id)
            if run is not None:
                found[run.id] = run
    for run in ctx.runs.live_runs():
        if any(state.status == "awaiting_approval" for state in run.nodes.values()):
            found[run.id] = run
    return list(found.values())


def _path(runs: RunManager, run: Run) -> list[str]:
    names = [run.workflow_name]
    seen = {run.id}
    current = run
    while current.parent_run_id:
        parent = runs.get(current.parent_run_id)
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        names.insert(0, parent.workflow_name)
        current = parent
    return names


def approvals_in(runs: RunManager, run: Run) -> list[PendingApproval]:
    """The approvals of one run (not of its sub-workflows' runs) that wait for a person."""
    result: list[PendingApproval] = []
    for node in run.workflow.nodes:
        state = run.nodes.get(node.id)
        if node.type != "approval" or state is None:
            continue
        if state.status != "awaiting_approval" or state.approval is not None:
            continue
        config = node.approval or ApprovalConfig()
        inputs, _absent = runs._inputs(run, node.id)
        shown = {node_label(item) for item in run.workflow.nodes if item.id in config.show}
        labels = {node_label(item): item.id for item in run.workflow.nodes}
        items = [
            ReviewItem(node_id=labels.get(name, ""), label=name, value=value)
            for name, value in inputs
            if not shown or name in shown
        ]
        handles = {edge.source_handle for edge in run.workflow.edges if edge.source == node.id}
        root = runs.root_of(run)
        result.append(
            PendingApproval(
                run_id=run.id,
                root_run_id=root.id,
                workflow_id=run.workflow_id,
                workflow_name=run.workflow_name,
                path=_path(runs, run),
                node_id=node.id,
                label=node_label(node),
                instructions=config.instructions,
                allow_comment=config.allow_comment,
                require_comment_on_reject=config.require_comment_on_reject,
                on_reject=config.on_reject,
                has_rejected_branch="rejected" in handles,
                has_approved_branch="approved" in handles,
                waiting_since=state.started_at,
                trigger=root.trigger,
                items=items,
            )
        )
    return result


def approvals_of_tree(runs: RunManager, run: Run, depth: int = 0) -> list[PendingApproval]:
    """The waiting approvals of a run and of its sub-workflows' runs."""
    found = approvals_in(runs, run)
    for state in run.nodes.values():
        if state.child_run_id and state.status == "awaiting_approval" and depth < 6:
            child = runs.get(state.child_run_id)
            if child is not None:
                found += approvals_of_tree(runs, child, depth + 1)
    return found


def pending_approvals(ctx: Workspace) -> list[PendingApproval]:
    result: list[PendingApproval] = []
    for run in _paused_runs(ctx):
        result += approvals_in(ctx.runs, run)
    return sorted(result, key=lambda item: item.waiting_since.isoformat() if item.waiting_since else "")


def models_of_run(ctx: Workspace, run: Run, depth: int = 0) -> dict[str, Usage]:
    """The usage of a run per model, including the agents of its sub-workflows."""
    found: dict[str, Usage] = {}
    for node in run.workflow.nodes:
        state = run.nodes.get(node.id)
        if state is None or not state.usage.requests:
            continue
        if node.agent is not None:
            found[node.agent.model] = add_usage(found.get(node.agent.model, Usage()), state.usage)
        elif node.type == "subworkflow" and state.child_run_id and depth < 6:
            child = ctx.store.get("runs", state.child_run_id, Run)
            if child is not None:
                for model, usage in models_of_run(ctx, child, depth + 1).items():
                    found[model] = add_usage(found.get(model, Usage()), usage)
        elif node.type == "orchestrator" and node.orchestrator is not None and depth < 6:
            # Its own decisions count for its model; its teams' agents for theirs.
            if state.decision_usage is not None and state.decision_usage.requests:
                model = node.orchestrator.model
                found[model] = add_usage(found.get(model, Usage()), state.decision_usage)
            for child_id in state.child_run_ids:
                team = ctx.store.get("runs", child_id, Run)
                if team is None:
                    continue
                for model, usage in models_of_run(ctx, team, depth + 1).items():
                    found[model] = add_usage(found.get(model, Usage()), usage)
    return found
