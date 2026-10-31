"""Continuing a failed run without running its successful steps again.

A continued run is a new run that is linked to the failed one (``resumed_from``); the
failed run stays exactly as it was. Before continuing, :func:`plan` decides for every
node whether its earlier result can be taken over:

- a result is reused when the node succeeded (or was deliberately skipped) in the
  earlier run, its fingerprint is unchanged and all its predecessors are reused too.
  The fingerprint (:func:`nodestro_runtime.graph.fingerprints`) covers the node's configuration,
  its connections, its tools' code, the input and files of the run and, for sub-workflows,
  the version they ran in; so a changed agent, tool or input means the node runs again;
- approval decisions of reused approval nodes are taken over and documented as such;
  nobody is asked twice, and nothing a person did not decide is ever approved;
- a node that runs again after it had already called tools of its own (which might have
  effects outside Nodestro) is reported, and continuing needs an explicit confirmation.

Reused nodes count no cost in the new run: their cost stays with the earlier run, so the
usage dashboard never counts it twice. When nothing can be reused safely, the plan says
so and offers a complete new start instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, Field

from .engine import node_label
from .graph import continuation, data_sources
from .launcher import launch
from .preflight import PROVIDER_CHECKS, preflight
from .problems import CheckIssue, NodestroError
from .schemas import NodeRun, Run, RunIn, Usage, Workflow, total_cost

if TYPE_CHECKING:
    from .workspace import Workspace


class PublishedRuns(Protocol):
    """Runs of published releases; the HTTP API provides it (publishing lives there)."""

    def release_exists(self, release_id: str) -> bool: ...

    async def resume(self, source: Run, *, mode: Literal["continue", "restart"], confirm: bool) -> Run:
        """Continue or restart ``source`` with its release and the publication's limits."""
        ...


ResumeAction = Literal["reuse", "rerun", "run"]


class ResumeNode(BaseModel):
    node_id: str
    label: str
    type: str
    #: ``reuse``: the earlier result is taken over; ``rerun``: it ran before and runs again;
    #: ``run``: it did not run before (or did not finish) and runs now.
    action: ResumeAction
    #: Why it runs (again), as a code for the UI.
    reason: str | None = None
    #: Tools it already called before it failed; they would be called again.
    side_effects: list[str] = Field(default_factory=list)
    #: The cost of the earlier result that is not spent again (``None``: unknown).
    saved_cost_usd: float | None = 0.0


class ResumePlan(BaseModel):
    run_id: str
    workflow_id: str
    #: False when continuing is not possible; a complete new start may still be.
    possible: bool
    can_restart: bool
    reused: int = 0
    rerun: int = 0
    nodes: list[ResumeNode] = Field(default_factory=list)
    issues: list[CheckIssue] = Field(default_factory=list)
    #: Continuing would call tools again; the user has to confirm.
    needs_confirmation: bool = False
    workflow_changed: bool = False
    saved_cost_usd: float | None = 0.0


def _issue(
    code: str, message: str, level: Literal["error", "warning", "info"] = "error", **params: object
) -> CheckIssue:
    node_id = params.pop("node_id", None)
    return CheckIssue(code=code, message=message, level=level, params=dict(params), node_id=node_id)  # type: ignore[arg-type]


def _source(ctx: Workspace, run_id: str) -> Run:
    run = ctx.runs.get(run_id)
    if run is None:
        raise KeyError(run_id)
    return run


def plan(ctx: Workspace, run_id: str) -> ResumePlan:
    source = _source(ctx, run_id)
    result = ResumePlan(run_id=source.id, workflow_id=source.workflow_id, possible=False, can_restart=False)
    issues = result.issues
    if source.imported_at is not None:
        issues.append(_issue("resume.imported", "An imported trace cannot be continued"))
        return result
    if source.parent_run_id is not None:
        issues.append(_issue("resume.child", "Continue the run this sub-workflow belongs to"))
        return result
    if source.status not in ("failed", "cancelled"):
        issues.append(
            _issue("resume.not_failed", "Only failed or cancelled runs can be continued", status=source.status)
        )
        return result
    release_id = source.release_id if source.trigger == "api" else None
    if release_id is not None:
        # A run of a published API continues with its release, never with the studio's workflow.
        if ctx.published is None or not ctx.published.release_exists(release_id):
            issues.append(_issue("resume.release_deleted", "The published version of this run no longer exists"))
            return result
        result.can_restart = True
        workflow = source.workflow
    else:
        current = ctx.store.get("workflows", source.workflow_id, Workflow)
        if current is None:
            issues.append(_issue("resume.workflow_deleted", "The workflow of this run no longer exists"))
            return result
        result.can_restart = True
        workflow = ctx.runs.pin_subworkflows(current)
        check = preflight(ctx, workflow)
        for item in check.problems:
            if item.level == "error" and item.code not in PROVIDER_CHECKS:
                issues.append(item)
        for node_id, agent_issues in check.agents.items():
            for agent_issue in agent_issues:
                if agent_issue.level == "error":
                    issues.append(
                        _issue(
                            "resume.agent_invalid",
                            f"{agent_issue.message}",
                            node_id=node_id,
                            detail=agent_issue.code,
                        )
                    )
        version = ctx.current_version(current)
        result.workflow_changed = version is None or version != source.workflow_version
    if result.workflow_changed:
        issues.append(
            _issue(
                "resume.workflow_changed",
                "The workflow changed since this run; changed steps and everything after them run again",
                level="info",
                version=source.workflow_version or "",
            )
        )
    probe = Run(
        workflow_id=workflow.id,
        workflow_name=workflow.name,
        workflow=workflow,
        input=source.input,
        values=source.values,
        file_ids=source.file_ids,
        release_id=release_id,
    )
    current_prints = ctx.runs.fingerprints(probe)
    known_files = ctx.file_catalog()
    starts = {node.id for node in workflow.nodes if node.type == "start"}
    missing_files = [file_id for file_id in source.file_ids if file_id not in known_files]
    custom = set(ctx.tools.builtin)
    for node, action, reason in continuation(workflow, source.nodes, current_prints):
        old = source.nodes.get(node.id)
        entry = ResumeNode(
            node_id=node.id,
            label=node_label(node),
            type=node.type,
            action=action,
            reason=reason,
        )
        if action == "reuse" and old is not None:
            entry.saved_cost_usd = old.usage.estimated_cost_usd
        if entry.action != "reuse" and old is not None:
            entry.side_effects = sorted(
                {call.name for call in old.tool_calls if call.name not in custom and not call.is_error}
            )
        if entry.action != "reuse" and missing_files and node.agent is not None:
            sources = data_sources(workflow, node.id)
            if not sources or any(item in starts for item in sources):
                issues.append(
                    _issue(
                        "resume.files_missing",
                        f"{len(missing_files)} files of this run no longer exist",
                        node_id=node.id,
                        count=len(missing_files),
                    )
                )
                missing_files = []
        result.nodes.append(entry)
    working = [entry for entry in result.nodes if entry.type in ("agent", "subworkflow", "orchestrator")]
    result.reused = sum(entry.action == "reuse" for entry in working)
    result.rerun = sum(entry.action != "reuse" for entry in working)
    result.saved_cost_usd = total_cost(entry.saved_cost_usd for entry in result.nodes)
    result.needs_confirmation = any(entry.side_effects for entry in result.nodes)
    if result.needs_confirmation:
        names = sorted({name for entry in result.nodes for name in entry.side_effects})
        issues.append(
            _issue(
                "resume.side_effects",
                f"Steps that run again had already called these tools: {', '.join(names)}",
                level="warning",
                tools=", ".join(names),
            )
        )
    if not result.reused:
        issues.append(
            _issue(
                "resume.nothing_reusable",
                "No result can be reused safely; everything runs again (a new start)",
                level="info",
            )
        )
    result.possible = not any(item.level == "error" for item in issues)
    return result


def _request(source: Run) -> RunIn:
    text = source.text_input if source.values else source.input
    return RunIn(input=text or "", values=source.values, file_ids=[] if source.values else source.file_ids)


async def resume(ctx: Workspace, run_id: str, *, mode: Literal["continue", "restart"], confirm: bool) -> Run:
    """Start the continued run (or, with ``restart``, a complete new run linked to the old one)."""
    source = _source(ctx, run_id)
    found = plan(ctx, run_id)
    if source.trigger == "api" and source.release_id:
        return await _resume_api(ctx, source, found, mode=mode, confirm=confirm)
    current = ctx.store.get("workflows", source.workflow_id, Workflow)
    if current is None or not found.can_restart:
        first = found.issues[0] if found.issues else None
        raise NodestroError(first.code if first else "resume.impossible", first.message if first else "Not possible")
    request = _request(source)
    if source.values:
        request.file_ids = [item for item in source.file_ids if item not in _form_files(source)]
    if mode == "restart":
        return await launch(ctx, current, request, trigger="resume", trigger_ref=source.id, resumed_from=source.id)
    if not found.possible:
        blocking = next(item for item in found.issues if item.level == "error")
        raise NodestroError(blocking.code, blocking.message, **blocking.params)
    if found.needs_confirmation and not confirm:
        raise NodestroError(
            "resume.confirm_side_effects", "Confirm that tools of steps that run again may be called again"
        )
    preset: dict[str, NodeRun] = {}
    for entry in found.nodes:
        if entry.action != "reuse":
            continue
        state = source.nodes[entry.node_id].model_copy(deep=True)
        state.reused_from = source.id
        state.usage = Usage()
        if state.approval is not None and state.approval.reused_from is None:
            state.approval.reused_from = source.id
        preset[entry.node_id] = state
    return await launch(
        ctx, current, request, trigger="resume", trigger_ref=source.id, resumed_from=source.id, preset=preset
    )


async def _resume_api(
    ctx: Workspace, source: Run, found: ResumePlan, *, mode: Literal["continue", "restart"], confirm: bool
) -> Run:
    """Continue (or restart) an API run from the studio: same release, same limits (see :class:`PublishedRuns`)."""
    if not found.can_restart or (mode == "continue" and not found.possible):
        first = next((item for item in found.issues if item.level == "error"), None)
        raise NodestroError(first.code if first else "resume.impossible", first.message if first else "Not possible")
    if ctx.published is None:
        raise NodestroError("resume.release_deleted", "The published version of this run no longer exists")
    return await ctx.published.resume(source, mode=mode, confirm=confirm)


def _form_files(source: Run) -> set[str]:
    found: set[str] = set()
    for value in source.values.values():
        if isinstance(value, list):
            found.update(str(item) for item in value)
    return found
