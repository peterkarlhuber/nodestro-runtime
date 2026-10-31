"""The check before a run: structure, agent configurations, connections and settings.

``POST /api/workflows/check`` and the start of a run use the same function, so the
editor shows exactly what would stop a run. Nothing here calls the Anthropic API: the
agent configurations are checked by building the agents with a client that is never
used (see :meth:`nodestro_runtime.agent_builder.AgentBuilder.validate`).

Findings carry a level: ``error`` blocks the run, ``warning`` and ``info`` only explain.
Each finding names the node or connection it belongs to, so the editor can mark it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from orkexa.pricing import resolve_pricing
from pydantic import BaseModel

from . import graph
from .agent_builder import ConfigIssue
from .engine import _PLACEHOLDER, MAX_DEPTH, check_workflow, node_label, start_fields
from .graph import BRANCHING, ancestors, comment_sources, data_sources
from .problems import CheckIssue, NodestroError
from .providers import PROVIDERS, MissingApiKeyError, model_id, provider_of
from .schemas import Workflow

if TYPE_CHECKING:
    from .workspace import Workspace

_RUN_INPUT_NAMES = {"input", "run.input", "workflow.input"}


class WorkflowCheck(BaseModel):
    #: False when anything blocks the run (an error here or in an agent configuration).
    ok: bool
    problems: list[CheckIssue]
    agents: dict[str, list[ConfigIssue]]
    errors: int = 0
    warnings: int = 0


def _issue(
    code: str, message: str, *, level: Literal["error", "warning", "info"] = "warning", **fields: Any
) -> CheckIssue:
    node_id = fields.pop("node_id", None)
    edge_id = fields.pop("edge_id", None)
    return CheckIssue(code=code, message=message, params=fields, level=level, node_id=node_id, edge_id=edge_id)


def _connection_issues(workflow: Workflow) -> list[CheckIssue]:
    found: list[CheckIssue] = []
    seen: dict[tuple[str, str], str] = {}
    for edge in workflow.edges:
        key = (edge.source, edge.target)
        if key in seen:
            found.append(
                _issue("check.edge_duplicate", "Two connections link the same nodes", edge_id=edge.id, level="warning")
            )
        seen.setdefault(key, edge.id)
    return found


def _node_issues(workflow: Workflow, known_files: set[str]) -> list[CheckIssue]:
    found: list[CheckIssue] = []
    nodes = {node.id: node for node in workflow.nodes}
    labels = {node.id: node_label(node) for node in workflow.nodes}
    predecessors: dict[str, list[str]] = {node.id: [] for node in workflow.nodes}
    successors: dict[str, list[str]] = {node.id: [] for node in workflow.nodes}
    for edge in workflow.edges:
        if edge.source in nodes and edge.target in nodes:
            predecessors[edge.target].append(edge.source)
            successors[edge.source].append(edge.target)
    has_start = any(node.type == "start" for node in workflow.nodes)
    form_starts = {node.id for node in workflow.nodes if node.type == "start" and node.start and node.start.fields}
    has_output = any(node.type == "output" for node in workflow.nodes)

    agent_labels: dict[str, list[str]] = {}
    for node in workflow.nodes:
        if node.type == "agent":
            agent_labels.setdefault(labels[node.id].strip().lower(), []).append(node.id)
    for label_key, ids in agent_labels.items():
        if len(ids) > 1 and label_key:
            for node_id in ids:
                found.append(
                    _issue(
                        "check.duplicate_name",
                        f"Several agents are called {labels[node_id]!r}; placeholders cannot tell them apart",
                        node_id=node_id,
                        name=labels[node_id],
                    )
                )

    fields = {field.name.lower() for field in start_fields(workflow)}
    outgoing = graph.successors(workflow)
    for node in workflow.nodes:
        label = labels[node.id]
        if node.type == "condition" and node.condition is not None:
            used = {edge.source_handle for edge in outgoing[node.id]}
            for rule in node.condition.rules:
                if rule.id not in used:
                    found.append(
                        _issue(
                            "check.branch_unconnected",
                            f"Branch {rule.label or rule.id} of {label} leads nowhere",
                            node_id=node.id,
                            name=label,
                            branch=rule.label or rule.id,
                        )
                    )
            if "default" not in used:
                found.append(
                    _issue(
                        "check.default_unconnected",
                        f"The default branch of {label} leads nowhere; when no rule holds, the path ends here",
                        node_id=node.id,
                        level="info",
                        name=label,
                    )
                )
            continue
        if node.type == "approval":
            used = {edge.source_handle for edge in outgoing[node.id]}
            if "approved" not in used:
                found.append(
                    _issue(
                        "check.approval_no_approved",
                        f"Nothing follows an approval at {label}",
                        node_id=node.id,
                        name=label,
                    )
                )
            on_reject = node.approval.on_reject if node.approval else "branch"
            if "rejected" not in used and on_reject == "branch":
                found.append(
                    _issue(
                        "check.approval_rejected_ends",
                        f"After a rejection at {label} its path simply ends",
                        node_id=node.id,
                        level="info",
                        name=label,
                    )
                )
            continue
        if node.type == "subworkflow":
            if has_start and not predecessors[node.id]:
                found.append(
                    _issue(
                        "check.agent_no_input",
                        f"{label} has no incoming connection and gets the run input directly",
                        node_id=node.id,
                        name=label,
                    )
                )
            continue
        if node.type == "start":
            missing = [file_id for file_id in (node.start.file_ids if node.start else []) if file_id not in known_files]
            if missing:
                found.append(
                    _issue(
                        "check.start_files_missing",
                        f"{len(missing)} start files no longer exist",
                        node_id=node.id,
                        count=len(missing),
                    )
                )
            if not successors[node.id] and len(workflow.nodes) > 1:
                found.append(_issue("check.start_unconnected", "The start node is not connected", node_id=node.id))
            continue
        if node.type == "output":
            if not predecessors[node.id]:
                found.append(
                    _issue("check.output_unconnected", "The result node has no input", node_id=node.id, level="error")
                )
            continue
        if node.agent is None:
            continue
        config = node.agent
        if has_start and not predecessors[node.id]:
            found.append(
                _issue(
                    "check.agent_no_input",
                    f"{label} has no incoming connection and gets the run input directly",
                    node_id=node.id,
                    name=label,
                )
            )
        template = config.task_template or ""
        uses_input = not template.strip() or any(
            match.group(1).strip().lower() == "input" for match in _PLACEHOLDER.finditer(template)
        )
        if uses_input and form_starts & set(data_sources(workflow, node.id)):
            # A filled-in form makes the start node's result the form values alone; the additional
            # free text then reaches the agent only through {{run.input}}.
            found.append(
                _issue(
                    "check.input_form_only",
                    f"{label} reads the start node with {{{{input}}}}: with a filled-in form that is only the form "
                    "values, without the additional text; use {{run.input}} for both",
                    node_id=node.id,
                    name=label,
                    input="{{input}}",
                    run_input="{{run.input}}",
                )
            )
        if has_output and not successors[node.id]:
            found.append(
                _issue(
                    "check.agent_result_unused",
                    f"The result of {label} goes nowhere",
                    node_id=node.id,
                    level="info",
                    name=label,
                )
            )
        upstream = {
            labels[source].strip().lower()
            for source in [*data_sources(workflow, node.id), *comment_sources(workflow, node.id)]
        }
        everyone = {value.strip().lower() for key, value in labels.items() if key != node.id}
        for match in _PLACEHOLDER.finditer(config.task_template or ""):
            name = match.group(1).strip()
            lowered = name.lower()
            if lowered in _RUN_INPUT_NAMES or lowered in upstream or lowered == "form":
                continue
            if lowered.startswith("form."):
                if lowered[5:].split(".")[0] not in fields:
                    found.append(
                        _issue(
                            "check.form_field_unknown",
                            f"{{{{{name}}}}} names no field of the input form",
                            node_id=node.id,
                            placeholder=name,
                            name=label,
                        )
                    )
                continue
            if lowered in everyone:
                found.append(
                    _issue(
                        "check.placeholder_not_connected",
                        f"{{{{{name}}}}} names an agent that is not connected to {label}",
                        node_id=node.id,
                        placeholder=name,
                        name=label,
                    )
                )
            else:
                found.append(
                    _issue(
                        "check.placeholder_unknown",
                        f"{{{{{name}}}}} in the task of {label} matches no predecessor and stays as it is",
                        node_id=node.id,
                        placeholder=name,
                        name=label,
                    )
                )
        missing_files = [file_id for file_id in config.knowledge.file_ids if file_id not in known_files]
        if missing_files:
            found.append(
                _issue(
                    "check.knowledge_missing",
                    f"{len(missing_files)} knowledge files of {label} no longer exist",
                    node_id=node.id,
                    count=len(missing_files),
                    name=label,
                )
            )
        models = {config.model, *config.fallbacks}
        if config.quality.enabled:
            models.add(config.quality.judge_model)
        for model in sorted(models):
            if not _priced(model):
                found.append(
                    _issue(
                        "check.model_unpriced",
                        f"No prices are known for {model}; costs and the budget cannot be tracked",
                        node_id=node.id,
                        model=model,
                    )
                )
    return found


#: Pre-check codes of a provider without settings (no key, no endpoint).
PROVIDER_CHECKS = ("check.api_key_missing", "check.provider_not_configured")


def _priced(model: str) -> bool:
    """Whether Orkexa knows prices for a model (only Anthropic's are built in)."""
    try:
        return resolve_pricing(model_id(model), provider=provider_of(model)) is not None
    except NodestroError:
        return True  # an invalid name is reported by the agent's own check


def agent_models(workflow: Workflow) -> set[str]:
    """The models of the workflow's agents and their judges."""
    models: set[str] = set()
    for node in workflow.nodes:
        if node.type == "agent" and node.agent is not None:
            models.add(node.agent.model)
            if node.agent.quality.enabled:
                models.add(node.agent.quality.judge_model)
    return models


def _provider_issues(nodestro: Workspace, workflow: Workflow) -> list[CheckIssue]:
    """Every provider the workflow's agents and judges use must be configured; none replaces another."""
    providers: set[str] = set()
    for model in agent_models(workflow):
        try:
            providers.add(provider_of(model))
        except NodestroError:
            continue  # reported by the agent's own check
    found: list[CheckIssue] = []
    configured = {item.id for item in nodestro.settings().providers if item.configured}
    for provider in sorted(providers):
        if provider not in PROVIDERS or provider in configured:
            continue  # an unknown provider is reported by the agent's own check
        exc = MissingApiKeyError(provider)
        code = "check.api_key_missing" if exc.code == "api_key_missing" else "check.provider_not_configured"
        found.append(_issue(code, str(exc), level="error", **exc.params))
    return found


def _flow_issues(workflow: Workflow) -> list[CheckIssue]:
    """Unreachable nodes and merges whose behaviour may surprise."""
    found: list[CheckIssue] = []
    nodes = {node.id: node for node in workflow.nodes}
    labels = {node.id: node_label(node) for node in workflow.nodes}
    incoming = graph.predecessors(workflow)
    outgoing = graph.successors(workflow)
    starts = [node.id for node in workflow.nodes if node.type == "start"]
    if starts:
        reached = set(starts)
        todo = list(starts)
        while todo:
            for edge in outgoing[todo.pop()]:
                if edge.target not in reached:
                    reached.add(edge.target)
                    todo.append(edge.target)
        for node in workflow.nodes:
            if node.id not in reached and incoming[node.id]:
                found.append(
                    _issue(
                        "check.unreachable",
                        f"{labels[node.id]} cannot be reached from the start",
                        node_id=node.id,
                        name=labels[node.id],
                    )
                )
    branching = {node.id for node in workflow.nodes if node.type in BRANCHING}
    if not branching:
        return found
    for node in workflow.nodes:
        edges = incoming[node.id]
        if len(edges) < 2 or node.type in BRANCHING:
            continue
        gates = []
        for edge in edges:
            upstream = ancestors(workflow, edge.source) | {edge.source}
            gates.append(upstream & branching)
        everywhere = set.intersection(*gates)
        partial = set.union(*gates) - everywhere
        if partial:
            names = ", ".join(sorted(labels[item] for item in partial))
            found.append(
                _issue(
                    "check.merge_partial",
                    f"{labels[node.id]} merges a conditional path ({names}) with other inputs; "
                    "it runs with whatever arrives, also when the branch is skipped",
                    node_id=node.id,
                    level="info",
                    name=labels[node.id],
                    branches=names,
                )
            )
            continue
        direct = [edge for edge in edges if edge.source in branching and nodes[edge.source].type == "condition"]
        sources = {edge.source for edge in direct}
        for source in sources:
            handles = {edge.source_handle for edge in direct if edge.source == source}
            condition = nodes[source].condition
            if len(handles) > 1 and condition is not None and condition.mode == "first":
                found.append(
                    _issue(
                        "check.merge_exclusive",
                        f"{labels[node.id]} joins branches of {labels[source]}; exactly one of them arrives",
                        node_id=node.id,
                        level="info",
                        name=labels[node.id],
                        decision=labels[source],
                    )
                )
    return found


def _subworkflow_issues(nodestro: Workspace, workflow: Workflow) -> list[CheckIssue]:
    """Sub-workflows: do they exist in their version, fit their inputs, and never call themselves?"""
    found: list[CheckIssue] = []

    def calls(child: Workflow, chain: tuple[str, ...]) -> str | None:
        """The name of a workflow that would be called again (a cycle) or nested too deep."""
        if len(chain) > MAX_DEPTH:
            return child.name
        for item in child.nodes:
            config = item.subworkflow
            if item.type != "subworkflow" or config is None or not config.workflow_id:
                continue
            if config.workflow_id in chain:
                return child.name
            try:
                grandchild, _ = nodestro.resolve_workflow(config.workflow_id, config.version)
            except KeyError:
                continue
            found_cycle = calls(grandchild, (*chain, config.workflow_id))
            if found_cycle:
                return found_cycle
        return None

    for node in workflow.nodes:
        config = node.subworkflow
        if node.type != "subworkflow" or config is None or not config.workflow_id:
            continue
        label = node_label(node)
        try:
            child, number = nodestro.resolve_workflow(config.workflow_id, config.version)
        except KeyError:
            exists = nodestro.store.get_raw("workflows", config.workflow_id) is not None
            code = "check.subworkflow_version_missing" if exists else "check.subworkflow_missing"
            found.append(
                _issue(
                    code,
                    f"{label}: the workflow (or its version) no longer exists",
                    level="error",
                    node_id=node.id,
                    name=label,
                    version=config.version or "",
                )
            )
            continue
        if config.workflow_id == workflow.id or calls(child, (workflow.id, config.workflow_id)):
            found.append(
                _issue(
                    "check.subworkflow_cycle",
                    f"{label} would call itself (directly or through other sub-workflows)",
                    level="error",
                    node_id=node.id,
                    name=label,
                )
            )
            continue
        errors = [item for item in check_workflow(child) if item.level == "error"]
        if errors:
            found.append(
                _issue(
                    "check.subworkflow_invalid",
                    f"{label}: the workflow {child.name} has {len(errors)} errors",
                    level="error",
                    node_id=node.id,
                    name=label,
                    workflow=child.name,
                    count=len(errors),
                )
            )
        child_fields = {field.name: field for field in start_fields(child)}
        for field in child_fields.values():
            if field.required and field.default in (None, "", []) and not config.values.get(field.name, "").strip():
                found.append(
                    _issue(
                        "check.subworkflow_field_unmapped",
                        f"{label}: the required field {field.label or field.name} gets no value",
                        level="error",
                        node_id=node.id,
                        name=label,
                        field=field.label or field.name,
                    )
                )
        for key in config.values:
            if key not in child_fields:
                found.append(
                    _issue(
                        "check.subworkflow_field_unknown",
                        f"{label}: {child.name} has no field {key}",
                        node_id=node.id,
                        name=label,
                        field=key,
                    )
                )
        if any(item.type == "approval" for item in child.nodes):
            found.append(
                _issue(
                    "check.subworkflow_approval",
                    f"{label}: {child.name} pauses for an approval; this run waits for it",
                    level="info",
                    node_id=node.id,
                    name=label,
                    workflow=child.name,
                )
            )
        if config.version is not None:
            try:
                _, latest = nodestro.resolve_workflow(config.workflow_id, None)
            except KeyError:
                latest = None
            if latest is not None and number is not None and latest > number:
                found.append(
                    _issue(
                        "check.subworkflow_outdated",
                        f"{label} runs v{number} of {child.name}; v{latest} is newer",
                        level="info",
                        node_id=node.id,
                        name=label,
                        version=number,
                        latest=latest,
                    )
                )
    return found


def _mark_unapproved(issues: list[ConfigIssue], pending: set[str]) -> list[ConfigIssue]:
    """Unknown tools that are drafts awaiting approval are reported as such."""
    result = []
    for issue in issues:
        names = [name.strip() for name in str(issue.params.get("names", "")).split(",") if name.strip()]
        if issue.code == "config.unknown_tools" and names and set(names) <= pending:
            issue = ConfigIssue(
                level="error",
                field="tools",
                code="config.tool_unapproved",
                params={"names": ", ".join(names)},
                message=f"Tools awaiting your approval: {', '.join(names)}",
            )
        result.append(issue)
    return result


def _dependency_issues(nodestro: Workspace, workflow: Workflow) -> list[CheckIssue]:
    """Third-party libraries of the workflow's tools: resolvable, approved, installed?"""
    from .python_tools import libraries_problem, needs_isolated_libraries, state_problem

    tool_nodes: dict[str, str] = {}
    for node in workflow.nodes:
        if node.agent is not None:
            for name in node.agent.tools:
                tool_nodes.setdefault(name, node.id)
    found: list[CheckIssue] = []
    needed = nodestro.python.needed_for_workflow(workflow)
    for issue in needed.issues:
        if issue.blocking:
            found.append(
                _issue(
                    "deps.unresolved_tool",
                    f"Tool {issue.tool}: {issue.message}",
                    level="error",
                    node_id=tool_nodes.get(issue.tool),
                    tool=issue.tool,
                    detail_code=issue.code,
                    **{key: str(value) for key, value in issue.params.items()},
                )
            )
    if needed.requirements and not needed.blocking:
        status = nodestro.python.status("workflow", workflow.id, needed, label=workflow.name)
        names = ", ".join(item.name for item in needed.requirements)
        if status.state == "ready":
            found.append(
                _issue("deps.ready", f"Approved libraries are installed: {names}", level="info", packages=names)
            )
        else:
            reason = state_problem(status)
            level: Literal["error", "warning", "info"] = "warning" if status.state == "install_needed" else "error"
            found.append(_issue(reason.code, reason.message, level=level, **reason.params))
    for name, node_id in tool_nodes.items():
        if name not in nodestro.tools or name in nodestro.tools.builtin:
            continue
        info = nodestro.tools.get(name)
        own = nodestro.python.needed_for_sources([(name, info.code or "", info.tests or "")])
        if needs_isolated_libraries(nodestro.tools, nodestro.python, info.trust, own):
            found_problem = libraries_problem(name)
            found.append(
                _issue(
                    found_problem.code, found_problem.message, level="error", node_id=node_id, **found_problem.params
                )
            )
    drafts = {draft.name: draft for draft in nodestro.tool_drafts.pending() if draft.dependencies}
    for name, node_id in tool_nodes.items():
        draft = drafts.get(name)
        if draft is not None:
            packages = ", ".join(draft.dependencies)
            found.append(
                _issue(
                    "deps.draft_libraries",
                    f"Tool draft {name} needs the libraries {packages}; they are not approved yet",
                    level="info",
                    node_id=node_id,
                    tool=name,
                    packages=packages,
                )
            )
    return found


def preflight(nodestro: Workspace, workflow: Workflow, *, pending_tools: set[str] | None = None) -> WorkflowCheck:
    """Everything that blocks or affects a run of ``workflow``; never calls the API.

    ``pending_tools`` are tool drafts that are not approved yet, in addition to the stored ones.
    """
    known_files = {entry.id for entry in nodestro.knowledge.store.list()}
    problems: list[CheckIssue] = [
        *check_workflow(workflow),
        *_connection_issues(workflow),
        *_node_issues(workflow, known_files),
        *_flow_issues(workflow),
        *_subworkflow_issues(nodestro, workflow),
    ]
    budget = workflow.budget_usd if workflow.budget_usd is not None else nodestro.settings().default_budget_usd
    if budget is not None and budget <= 0:
        problems.append(_issue("check.budget_none", "The budget is 0: the run would stop at the first request"))
    problems += _dependency_issues(nodestro, workflow)
    problems += _provider_issues(nodestro, workflow)
    pending = nodestro.tool_drafts.pending_names() | (pending_tools or set())
    agents = {
        node.id: _mark_unapproved(nodestro.builder.validate(node.agent), pending)
        for node in workflow.nodes
        if node.type == "agent" and node.agent is not None
    }
    errors = sum(item.level == "error" for item in problems) + sum(
        issue.level == "error" for issues in agents.values() for issue in issues
    )
    warnings = sum(item.level == "warning" for item in problems) + sum(
        issue.level == "warning" for issues in agents.values() for issue in issues
    )
    order = {"error": 0, "warning": 1, "info": 2}
    problems.sort(key=lambda item: order[item.level])
    return WorkflowCheck(ok=errors == 0, problems=problems, agents=agents, errors=errors, warnings=warnings)
