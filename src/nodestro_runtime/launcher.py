"""Starting a run the same way from everywhere: the editor, schedules, batches and test series.

:func:`launch` runs the pre-check (:mod:`nodestro_runtime.preflight`), refuses a run that would fail
for a known reason, installs approved Python libraries whose environment is missing and
hands the run to the engine. There is one engine and one way in.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pydantic import BaseModel

from .engine import WorkflowError, node_label
from .preflight import PROVIDER_CHECKS, agent_models, preflight
from .problems import NodestroError
from .python_tools import state_problem
from .schemas import NodeRun, Run, RunIn, RunSummary, RunTrigger, Workflow

if TYPE_CHECKING:
    from .workspace import Workspace


def blocking_problems(ctx: Workspace, workflow: Workflow) -> None:
    """Raise when the pre-check finds an error (missing provider settings aside: :meth:`launch` reports them)."""
    check = preflight(ctx, workflow)
    labels = {node.id: node_label(node) for node in workflow.nodes}
    blocking = [
        (labels.get(node_id, node_id), issue)
        for node_id, issues in check.agents.items()
        for issue in issues
        if issue.level == "error"
    ]
    if blocking:
        raise NodestroError(
            "run.agents_invalid",
            "; ".join(f"{label}: {issue.message}" for label, issue in blocking),
            agents=", ".join(dict.fromkeys(label for label, _ in blocking)),
        )
    problems = [item for item in check.problems if item.level == "error" and item.code not in PROVIDER_CHECKS]
    if problems:
        raise WorkflowError(
            "workflow.blocked",
            "; ".join(item.message for item in problems),
            problems=[item.model_dump() for item in problems],
        )


async def launch(
    ctx: Workspace,
    workflow: Workflow,
    request: RunIn,
    *,
    trigger: RunTrigger = "manual",
    trigger_ref: str | None = None,
    version: int | None = None,
    resumed_from: str | None = None,
    preset: dict[str, NodeRun] | None = None,
) -> Run:
    blocking_problems(ctx, workflow)
    ctx.require_providers(agent_models(workflow))  # fail fast: every provider the agents use is configured
    # Approved libraries whose environment is missing (e.g. after a restore) are installed now,
    # exactly in the approved versions.
    needed = ctx.python.needed_for_workflow(workflow)
    if needed.requirements:
        status = await asyncio.to_thread(ctx.python.ensure, "workflow", workflow.id, needed)
        if status.state != "ready":
            found = state_problem(status)
            raise NodestroError(found.code, found.message, **found.params)
    return ctx.runs.start(
        workflow,
        request,
        trigger=trigger,
        trigger_ref=trigger_ref,
        version=version if version is not None else ctx.current_version(workflow),
        resumed_from=resumed_from,
        preset=preset,
    )


def external_tools(ctx: Workspace, workflow: Workflow, *, depth: int = 0) -> list[str]:
    """Own and generated tools of a workflow and its sub-workflows: they may act outside Nodestro.

    Automatic starts (schedules, batches, test series) name them and need the user's
    explicit agreement, so no external action happens unnoticed.
    """
    found: set[str] = set()
    for node in workflow.nodes:
        if node.agent is not None:
            found.update(name for name in node.agent.tools if name not in ctx.tools.builtin)
        if node.subworkflow is not None and node.subworkflow.workflow_id and depth < 5:
            try:
                child, _ = ctx.resolve_workflow(node.subworkflow.workflow_id, node.subworkflow.version)
            except KeyError:
                continue
            found.update(external_tools(ctx, child, depth=depth + 1))
    return sorted(found)


def approval_steps(ctx: Workspace, workflow: Workflow, *, depth: int = 0) -> int:
    """How many approval nodes a run may stop at (sub-workflows included)."""
    count = sum(node.type == "approval" for node in workflow.nodes)
    for node in workflow.nodes:
        if node.subworkflow is not None and node.subworkflow.workflow_id and depth < 5:
            try:
                child, _ = ctx.resolve_workflow(node.subworkflow.workflow_id, node.subworkflow.version)
            except KeyError:
                continue
            count += approval_steps(ctx, child, depth=depth + 1)
    return count


class CostEstimate(BaseModel):
    """What a number of runs will probably cost, from earlier runs of the workflow."""

    runs: int
    #: Average cost of one run (``None``: no earlier run to learn from).
    per_run_usd: float | None = None
    total_usd: float | None = None
    #: Upper bound from the budget limit of each run (``None``: runs have no limit).
    max_total_usd: float | None = None
    based_on: int = 0


def estimate_cost(ctx: Workspace, workflow_id: str, runs: int) -> CostEstimate:
    summaries = [
        summary
        for summary in ctx.store.list("runs", RunSummary, limit=500)
        if summary.workflow_id == workflow_id
        and summary.status == "succeeded"
        and summary.parent_run_id is None
        and summary.imported_at is None
        and summary.usage.estimated_cost_usd is not None
    ][:20]
    estimate = CostEstimate(runs=runs, based_on=len(summaries))
    if summaries:
        average = sum(summary.usage.estimated_cost_usd or 0.0 for summary in summaries) / len(summaries)
        estimate.per_run_usd = round(average, 6)
        estimate.total_usd = round(average * runs, 4)
    budget = ctx.settings().default_budget_usd
    if budget is not None:
        estimate.max_total_usd = round(budget * runs, 4)
    return estimate
