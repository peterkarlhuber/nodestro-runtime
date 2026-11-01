"""Runs workflows: a DAG of nodes, each executed as soon as its inputs are settled.

Each agent node becomes an ``orkexa.Agent`` that runs in a worker thread, so
independent branches run in parallel. The agent's hooks (``on_text``,
``on_thinking``, ``before_tool``, ``after_tool``, ``on_response``) become
:class:`~nodestro_runtime.schemas.RunEvent` objects that the API streams to the browser.
One ``BudgetTracker`` per run counts tokens and cost of every agent and stops
the run when the budget is used up.

Besides agents the engine knows decision nodes (a deterministic choice of branches,
:mod:`nodestro_runtime.conditions`), approval nodes (the run pauses until a person decides)
and sub-workflow nodes (another saved workflow runs as a child run with this same
engine; its cost counts in the parent's budget). The rules of the data flow are
described in :mod:`nodestro_runtime.graph`.

A run is executed by :meth:`RunManager._execute`. Nodes whose state is already final
(succeeded, skipped, failed ...) are never executed again: this lets a paused run
continue in place after an approval (also after a restart, from the stored run), and
lets a continued run (:mod:`nodestro_runtime.resume`) take over results of an earlier run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
import concurrent.futures
from contextlib import suppress
import contextvars
from dataclasses import dataclass, field
from decimal import Decimal
import functools
import json
import logging
import os
import re
import threading
import time
from typing import Any, Literal, TypeVar
import uuid

from orkexa import (
    CheckpointError,
    ContentBlock,
    Criterion,
    EphemeralMemory,
    Evaluator,
    MemoryBackend,
    OrkexaError,
    ProviderError,
    Tool,
    ToolCall,
    ToolCallSuspended,
    TurnSuspended,
    UsageRecord,
    UsageSummary,
    flow,
    orchestration,
    refine,
)
from orkexa.dynamic import BudgetExceededError, BudgetTracker, Sandbox
from orkexa.providers.base import redact
from orkexa.usage import summarize_usage
from pydantic import BaseModel

from . import contracts, flow_driver, graph, sandbox_cancel, tool_checkpoints, tool_journal, tool_policy
from .agent_builder import AgentBuilder, run_own_tool
from .conditions import APPROVED, MISSING, REJECTED, decide, lookup, rule_problems
from .cost_tracking import PricedTracker, PriceMode, stale_requests
from .file_delivery import collect, deliver
from .forms import FileRef, field_problems, name_of, prepare, prompt_values
from .graph import BRANCHING, TRANSPARENT, WORKING, data_sources, edge_active, ports
from .input_files import InputFiles
from .mcp_access import UNATTENDED_TRIGGERS, is_mcp_tool
from .orchestrator import (
    child_outcome,
    decision_fields,
    decision_of,
    member_parameters,
    orchestrator_problems,
    orchestrator_prompt,
    select_teams,
)
from .pricing import PricingService
from .problems import CheckIssue, NodestroError, Problem, library_problem, price_problem, problem
from .providers import Connection, check_model, model_id, provider_of
from .schemas import (
    DEFAULT_ASSISTANT_MODEL,
    AgentConfig,
    ApprovalConfig,
    ApprovalRecord,
    Edge,
    EvaluationRecord,
    InputField,
    KnowledgeProposal,
    Node,
    NodeRun,
    OutputConfig,
    Run,
    RunEvent,
    RunIn,
    RunTrigger,
    ToolCallApproval,
    ToolCallRecord,
    Usage,
    Workflow,
    now,
)
from .storage import DocumentStore
from .tool_approvals import (
    EXECUTION_WINDOW,
    INTERRUPTED,
    RECLASSIFIED,
    SWITCH_OFF,
    CallBinding,
    ToolApprovalRequest,
    ToolApprovals,
    ToolGate,
    arguments_hash,
    bind_call,
    checkpoints_enabled,
    reclassified,
    tool_facts,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
#: Events of a live run kept for late subscribers; beyond it ``text`` and ``thinking`` deltas are
#: no longer kept (they are still published, and the node state holds the whole text).
_MAX_EVENT_HISTORY = 5000
#: The ``seq`` of the last event of each paused run (store collection), kept across restarts.
_EVENT_SEQ = "run_event_seq"
#: Events a subscriber may fall behind; a subscriber that stops reading is ended instead of
#: growing without limit (the client then loads the run once more, see ``subscribe``).
_MAX_SUBSCRIBER_BACKLOG = 10_000
#: How long :meth:`RunManager.shutdown` waits for runs to stop by themselves.
_SHUTDOWN_GRACE_SECONDS = 5.0
#: Threads that execute the agents and orchestrator decisions of one :class:`RunManager`'s runs, including agents
#: that wait in their thread for a tool approval (C-08). Configurable with :data:`RUN_THREADS_ENV`. The pool is
#: separate from asyncio's default executor, so busy or waiting agents never delay the application's other
#: background work (backups, uploads, a worker's lease renewal); an agent beyond the limit waits for a thread.
DEFAULT_RUN_THREADS = 32
RUN_THREADS_ENV = "NODESTRO_RUN_THREADS"
#: How often a conditional write of a stored run is tried again after another process wrote the run meanwhile.
_CAS_ATTEMPTS = 5

T = TypeVar("T")


def run_threads(environ: Mapping[str, str] | None = None) -> int:
    """The size of a run manager's agent thread pool: ``NODESTRO_RUN_THREADS`` (at least 1), else the default."""
    value = (os.environ if environ is None else environ).get(RUN_THREADS_ENV, "").strip()
    if not value:
        return DEFAULT_RUN_THREADS
    try:
        return max(1, int(value))
    except ValueError:
        logger.warning("Ignoring %s=%r: not a whole number; using %s", RUN_THREADS_ENV, value, DEFAULT_RUN_THREADS)
        return DEFAULT_RUN_THREADS


class _FencedRun(Run):
    """A run document as stored, with its write fence (C-02, C-06): a random token that every conditional write of
    the run compares (compare-and-set) and replaces. Internal to the engine: the API's :class:`Run` ignores it, and a
    plain ``put`` of a run leaves it out (the next conditional write then compares the status instead)."""

    fence: str | None = None


def _split(stored: _FencedRun) -> tuple[Run, str | None]:
    """A stored run as a plain :class:`Run`, and its fence."""
    values = dict(stored)
    fence = values.pop("fence", None)
    return Run.model_construct(**values), fence


def _fenced(run: Run, fence: str) -> _FencedRun:
    values = dict(run)
    values["fence"] = fence
    return _FencedRun.model_construct(**values)


def _new_fence() -> str:
    return uuid.uuid4().hex


class RunCancelled(Exception):
    """Raised inside agent hooks when the user cancels the run."""


class _TeamStopped(Exception):
    """A team of an orchestrator stopped its run (a rejection with ``on_reject="stop"``)."""

    def __init__(self, stop: Problem) -> None:
        super().__init__("stopped by a team")
        self.problem = stop


def _cancelled_orchestration(record: dict[str, Any]) -> dict[str, Any]:
    """An orchestration record whose waiting teams were cancelled with their run (no decision is pending)."""
    try:
        current = orchestration.OrchestrationState.model_validate(record)
    except ValueError:
        return record
    if current.pending is None:
        return record
    outcomes = tuple(
        item.model_copy(update={"status": "cancelled", "error": "run.cancelled"}) if item.status == "waiting" else item
        for item in current.pending.outcomes
    )
    ended = orchestration.complete_pending(
        current, outcomes, limits=orchestration.OrchestrationLimits(max_decisions=50)
    )
    return ended.model_copy(update={"outcome": "stopped", "stop_code": "run.cancelled"}).model_dump(mode="json")


class WorkflowError(NodestroError):
    """The workflow cannot run (cycle, no agents, broken edges)."""


#: How deep sub-workflows may be nested.
MAX_DEPTH = 5


#: Orkexa's price qualities, most trustworthy first.
_QUALITY_ORDER = ("verified", "contract", "unverified", "estimated")
_MAX_LISTED = 50


def _lowest_quality(*qualities: str | None) -> str | None:
    known = [quality for quality in qualities if quality]
    if not known:
        return None
    return max(known, key=lambda q: _QUALITY_ORDER.index(q) if q in _QUALITY_ORDER else len(_QUALITY_ORDER))


def _merged(*lists: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for items in lists for item in items))[:_MAX_LISTED]


def usage_of(
    summary: UsageSummary, records: Iterable[UsageRecord] = (), *, stale_requests: int = 0, uncapped: int = 0
) -> Usage:
    """Nodestro's usage of an Orkexa summary; ``records`` (the summarized requests) add the price references."""
    cost = summary.estimated_cost_usd
    used = list(records)
    return Usage(
        requests=summary.requests,
        input_tokens=summary.input_tokens,
        output_tokens=summary.output_tokens,
        cache_read_input_tokens=summary.cache_read_input_tokens,
        cache_creation_input_tokens=summary.cache_creation_input_tokens,
        thinking_tokens=summary.thinking_tokens,
        web_search_requests=summary.web_search_requests,
        total_tokens=summary.total_tokens,
        estimated_cost_usd=float(cost) if cost is not None else None,
        known_cost_usd=float(summary.known_cost_usd),
        cost_complete=cost is not None,
        unpriced_requests=summary.unpriced_requests,
        stale_price_requests=stale_requests,
        missing_cost_components=list(summary.missing_cost_components)[:_MAX_LISTED],
        pricing_versions=_merged([record.pricing_version for record in used if record.pricing_version]),
        pricing_sources=_merged([record.pricing_source for record in used if record.pricing_source]),
        pricing_quality=_lowest_quality(*(record.pricing_quality for record in used)),
        uncapped_requests=uncapped,
    )


def uncapped_requests(tracker: object) -> int:
    """Requests ``tracker`` admitted without capping their cost (0 for trackers without a budget)."""
    if not isinstance(tracker, BudgetTracker):
        return 0
    return tracker.unbounded_requests + tracker.oversized_requests


def _hard_maximum(run: Run) -> bool | None:
    """Whether the run's budget capped every request (``None`` without a budget)."""
    if run.budget_usd is None:
        return None
    return run.usage.uncapped_requests == 0 and run.usage.unpriced_requests == 0


def _cost_parts(usage: Usage) -> tuple[float, int, bool | None]:
    """(known cost, unpriced requests, complete) of a usage, also of one stored before price references."""
    if usage.requests == 0:
        return 0.0, 0, True
    if usage.known_cost_usd is None:  # recorded before PRICING-1
        known = usage.estimated_cost_usd
        if known is None:
            return 0.0, usage.requests, False
        return known, 0, None  # priced, but whether every part of it was known was not recorded
    return usage.known_cost_usd, usage.unpriced_requests, usage.cost_complete


def add_usage(first: Usage, second: Usage) -> Usage:
    """The sum of two usages: an unknown cost stays unknown, known parts add up, references are merged."""
    cost = (
        None
        if first.estimated_cost_usd is None or second.estimated_cost_usd is None
        else first.estimated_cost_usd + second.estimated_cost_usd
    )
    known_a, unpriced_a, complete_a = _cost_parts(first)
    known_b, unpriced_b, complete_b = _cost_parts(second)
    complete: bool | None
    if complete_a is False or complete_b is False:
        complete = False
    elif complete_a is None or complete_b is None:
        complete = None
    else:
        complete = True
    counted = (
        "requests",
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "thinking_tokens",
        "web_search_requests",
        "total_tokens",
        "stale_price_requests",
        "uncapped_requests",
    )
    data = {name: getattr(first, name) + getattr(second, name) for name in counted}
    return Usage(
        **data,
        estimated_cost_usd=cost,
        known_cost_usd=known_a + known_b,
        cost_complete=complete,
        unpriced_requests=unpriced_a + unpriced_b,
        missing_cost_components=_merged(first.missing_cost_components, second.missing_cost_components),
        pricing_versions=_merged(first.pricing_versions, second.pricing_versions),
        pricing_sources=_merged(first.pricing_sources, second.pricing_sources),
        pricing_quality=_lowest_quality(first.pricing_quality, second.pricing_quality),
    )


def topological_order(workflow: Workflow) -> list[Node]:
    """The nodes in an order where every node comes after its predecessors."""
    nodes = {node.id: node for node in workflow.nodes}
    incoming: dict[str, int] = dict.fromkeys(nodes, 0)
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for edge in workflow.edges:
        if edge.source not in nodes or edge.target not in nodes:
            raise WorkflowError(
                "workflow.edge_dangling", f"Connection {edge.id} points to a node that does not exist", edge=edge.id
            )
        if edge.source == edge.target:
            raise WorkflowError("workflow.self_loop", "A node cannot be connected to itself")
        if edge.loop is not None:
            continue  # loops run against the direction of the graph on purpose
        outgoing[edge.source].append(edge.target)
        incoming[edge.target] += 1
    ready = [node_id for node_id, count in incoming.items() if count == 0]
    order: list[Node] = []
    while ready:
        node_id = ready.pop(0)
        order.append(nodes[node_id])
        for target in outgoing[node_id]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if len(order) != len(nodes):
        raise WorkflowError("workflow.cycle", "The workflow contains a cycle; connections must run in one direction")
    return order


def _cycle_nodes(workflow: Workflow) -> list[str]:
    """Ids of nodes on or behind a cycle (valid connections only); empty when there is none."""
    ids = {node.id for node in workflow.nodes}
    incoming: dict[str, int] = dict.fromkeys(ids, 0)
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for edge in workflow.edges:
        if edge.source in ids and edge.target in ids and edge.source != edge.target and edge.loop is None:
            outgoing[edge.source].append(edge.target)
            incoming[edge.target] += 1
    ready = [node_id for node_id, count in incoming.items() if count == 0]
    while ready:
        node_id = ready.pop()
        for target in outgoing[node_id]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    return [node.id for node in workflow.nodes if incoming[node.id] > 0]


def _port_problems(edge: Edge, nodes: dict[str, Node]) -> list[CheckIssue]:
    found = []
    target, source = nodes[edge.target], nodes[edge.source]
    if target.type == "start":
        found.append(
            CheckIssue(
                code="workflow.start_has_inputs",
                message="The start node cannot have inputs",
                node_id=edge.target,
                edge_id=edge.id,
            )
        )
    if source.type == "output":
        found.append(
            CheckIssue(
                code="workflow.output_has_outputs",
                message="The result node cannot have outputs",
                node_id=edge.source,
                edge_id=edge.id,
            )
        )
    if source.type in BRANCHING and edge.source_handle not in ports(source):
        found.append(
            CheckIssue(
                code="workflow.branch_unknown",
                message=f"A connection of {node_label(source)} starts at no branch",
                params={"node": node_label(source)},
                node_id=edge.source,
                edge_id=edge.id,
            )
        )
    return found


def _node_problems(node: Node, workflow: Workflow) -> list[CheckIssue]:
    """Configuration errors of decision, approval, sub-workflow and start nodes."""
    label = node_label(node)
    found: list[CheckIssue] = []

    def add(code: str, message: str, **params: Any) -> None:
        found.append(CheckIssue(code=code, message=message, params={"node": label, **params}, node_id=node.id))

    incoming = [edge for edge in workflow.edges if edge.target == node.id]
    if node.type == "start" and node.start is not None:
        for code, message, params in field_problems(node.start.fields):
            add(code, message, **params)
    if node.type in BRANCHING and not incoming:
        add("workflow.branch_no_input", f"{label} has no incoming connection")
    if node.type == "condition":
        config = node.condition
        if config is None or not config.rules:
            add("workflow.condition_no_rules", f"The decision {label} has no rules")
            return found
        for rule in config.rules:
            for code, message in rule_problems(rule):
                add(code, f"{label}, rule {rule.label or rule.id}: {message}", rule=rule.label or rule.id)
        sources = data_sources(workflow, node.id)
        if config.source:
            if config.source not in graph.ancestors(workflow, node.id):
                add("condition.source_not_upstream", f"{label} evaluates a node that does not come before it")
        elif len(sources) > 1:
            add("condition.source_ambiguous", f"{label} receives several results; choose the one to evaluate")
    if node.type == "subworkflow" and (node.subworkflow is None or not node.subworkflow.workflow_id):
        add("workflow.subworkflow_missing", f"{label} has no workflow selected")
    if node.type == "output" and node.output is not None:
        for code, message, params in contracts.contract_problems(node.output.contract):
            add(code, f"{label}: {message}", **params)
    for code, message, params in orchestrator_problems(node, workflow):
        add(code, f"{label} {message}", **params)
    return found


def check_workflow(workflow: Workflow) -> list[CheckIssue]:
    """Problems that prevent a run, attributed to nodes and connections where possible.

    The editor shows them in the UI language by their code; :mod:`nodestro_runtime.preflight`
    adds warnings and the checks of each agent configuration.
    """
    problems: list[CheckIssue] = []
    nodes = {node.id: node for node in workflow.nodes}
    if len(nodes) != len(workflow.nodes):
        # The second node of an id would silently replace the first one in every run.
        seen: set[str] = set()
        for node in workflow.nodes:
            if node.id in seen:
                problems.append(
                    CheckIssue(
                        code="workflow.node_duplicate",
                        message=f"Two nodes have the id {node.id}",
                        params={"id": node.id},
                        node_id=node.id,
                    )
                )
            seen.add(node.id)
    for edge in workflow.edges:
        if edge.source not in nodes or edge.target not in nodes:
            problems.append(
                CheckIssue(
                    code="workflow.edge_dangling",
                    message=f"Connection {edge.id} points to a node that does not exist",
                    params={"edge": edge.id},
                    edge_id=edge.id,
                )
            )
        elif edge.source == edge.target:
            problems.append(
                CheckIssue(
                    code="workflow.self_loop",
                    message="A node cannot be connected to itself",
                    node_id=edge.source,
                    edge_id=edge.id,
                )
            )
        else:
            problems.extend(_port_problems(edge, nodes))
    cycle = _cycle_nodes(workflow)
    if cycle:
        names = ", ".join(node_label(nodes[node_id]) for node_id in cycle)
        problems.append(
            CheckIssue(
                code="workflow.cycle",
                message="The workflow contains a cycle; connections must run in one direction",
                params={"nodes": names},
                node_id=cycle[0],
            )
        )
    agents = [node for node in workflow.nodes if node.type == "agent"]
    if not any(node.type in WORKING for node in workflow.nodes):
        problems.append(CheckIssue(code="workflow.no_agents", message="The workflow has no agent yet"))
    for node in agents:
        if node.agent is None:
            label = node.label or node.id
            problems.append(
                CheckIssue(
                    code="workflow.agent_missing",
                    message=f"Node {label} has no agent configuration",
                    params={"node": label},
                    node_id=node.id,
                )
            )
    if not cycle:
        for node in workflow.nodes:
            problems.extend(_node_problems(node, workflow))
        if not problems and graph.loop_edges(workflow):
            problems.extend(_loop_problems(workflow))
    return problems


#: The kernel's loop checks, as the runtime's workflow problems.
_LOOP_CODES = {
    "flow.loop_unguarded": ("workflow.loop_unguarded", "A loop must start at a branch of a decision or approval"),
    "flow.loop_target": ("workflow.loop_target", "A loop must lead back to a node before its decision or approval"),
    "flow.loop_overlap": ("workflow.loop_overlap", "Two loops overlap without one lying inside the other"),
    "flow.loop_head_shared": ("workflow.loop_shared", "A node starts more than one loop"),
    "flow.loop_source_shared": ("workflow.loop_shared", "A decision or approval closes more than one loop"),
}


def _loop_problems(workflow: Workflow) -> list[CheckIssue]:
    found: list[CheckIssue] = []
    for issue in flow.validate(flow_driver.flow_of(workflow)):
        code, message = _LOOP_CODES.get(issue.code, ("workflow.loop_invalid", issue.message))
        edge = next((item for item in workflow.edges if item.id == issue.edge), None)
        found.append(
            CheckIssue(
                code=code,
                message=message,
                node_id=edge.source if edge is not None else issue.node,
                edge_id=edge.id if edge is not None else None,
            )
        )
    return found


def node_label(node: Node) -> str:
    """The name of a node in prompts, placeholders and results.

    The defaults of unnamed nodes are German for compatibility: they are keys of stored run
    outputs and names in the prompts of existing workflows, so they do not change.
    """
    if node.label:
        return node.label
    if node.agent is not None:
        return node.agent.name
    return {
        "start": "Start",
        "output": "Ausgabe",
        "condition": "Entscheidung",
        "approval": "Freigabe",
        "subworkflow": "Sub-Workflow",
        "orchestrator": "Orchestrator",
    }.get(node.type, node.id)


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


_FILE_TEMPLATE = re.compile(r"^\s*\{\{\s*(form\.[A-Za-z_][A-Za-z0-9_]*|files|run\.files)\s*\}\}\s*$")


def _file_references(template: str, run: Run) -> list[str]:
    """The file ids a sub-workflow's file field receives: ``{{form.<file field>}}`` or ``{{files}}`` (all)."""
    found: list[str] = []
    for part in re.split(r"[,\n]", template or ""):
        match = _FILE_TEMPLATE.match(part)
        if match is None:
            continue
        key = match.group(1)
        if key in ("files", "run.files"):
            found += run.file_ids
        else:
            value = run.values.get(key[5:])
            if isinstance(value, list):
                found += [str(item) for item in value]
    return list(dict.fromkeys(found))


@dataclass(frozen=True)
class LoopContext:
    """What a node inside a loop can refer to in its task (see :func:`render_task`)."""

    iteration: int
    previous: Any = None
    feedback: list[tuple[str, Any]] = field(default_factory=list)


def render_task(
    template: str,
    *,
    run_input: str,
    inputs: list[tuple[str, Any]],
    values: dict[str, Any] | None = None,
    absent: set[str] | frozenset[str] = frozenset(),
    append: bool = True,
    run_names: set[str] | frozenset[str] = frozenset(),
    loop: LoopContext | None = None,
) -> str:
    """Fill ``{{input}}``, ``{{run.input}}``, ``{{form.<field>}}`` and ``{{<node name>}}`` in a task template.

    ``{{input}}`` is the output of the single predecessor, or all predecessor
    outputs as labelled sections. Predecessor outputs the template does not
    mention are appended, so no information is lost silently. ``absent`` names
    predecessors whose branch was skipped in this run: their placeholder becomes ``-``.
    ``run_names`` are the names of start nodes: a template that uses the run input or a form
    value already has what the start node delivers, so it is not appended again.
    Inside a loop (``loop``), ``{{iteration}}`` is the iteration (from 1), ``{{previous}}`` the
    node's own result of the previous iteration and ``{{feedback}}`` what arrived over the loop
    connection; both read ``-`` in the first iteration. A node named like them wins.
    """
    by_name = {name.lower(): value for name, value in inputs}
    form = {str(key).lower(): value for key, value in (values or {}).items()}
    used: set[str] = set()

    def combined() -> str:
        if len(inputs) == 1:
            return as_text(inputs[0][1])
        return "\n\n".join(f"## {name}\n\n{as_text(value)}" for name, value in inputs)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        lowered = key.lower()
        if lowered == "input":
            used.update(by_name)
            return combined() if inputs else run_input
        if lowered in ("run.input", "workflow.input"):
            used.update(name.lower() for name in run_names)
            return run_input
        if lowered in ("form", "run.values"):
            used.update(name.lower() for name in run_names)
            return as_text(values or {})
        if lowered.startswith("form."):
            used.update(name.lower() for name in run_names)
            found = lookup(form, lowered[5:])
            return match.group(0) if found is MISSING else as_text(found)
        if lowered in by_name:
            used.add(lowered)
            return as_text(by_name[lowered])
        if lowered in absent:
            return "-"
        if loop is not None:
            if lowered == "iteration":
                return str(loop.iteration)
            if lowered == "previous":
                return "-" if loop.previous is None else as_text(loop.previous)
            if lowered == "feedback":
                used.update(name.lower() for name, _ in loop.feedback)
                if not loop.feedback:
                    return "-"
                if len(loop.feedback) == 1:
                    return as_text(loop.feedback[0][1])
                return "\n\n".join(f"## {name}\n\n{as_text(value)}" for name, value in loop.feedback)
        return match.group(0)

    text = _PLACEHOLDER.sub(replace, template or "")
    # The heading of appended inputs and the fallback task are part of the prompts of existing
    # workflows (and of their tests in other repositories); they stay as they are.
    if not append:
        return text.strip()
    rest = [(name, value) for name, value in inputs if name.lower() not in used]
    if rest:
        sections = "\n\n".join(f"## {name}\n\n{as_text(value)}" for name, value in rest)
        text = f"{text.strip()}\n\n# Eingaben\n\n{sections}" if text.strip() else sections
    if not text.strip():
        text = run_input
    return text.strip() or "Bitte beginne."


def start_fields(workflow: Workflow) -> list[InputField]:
    """The input form of a workflow (the fields of its start node)."""
    start = next((node for node in workflow.nodes if node.type == "start"), None)
    return list(start.start.fields) if start is not None and start.start is not None else []


def _start_names(workflow: Workflow) -> set[str]:
    return {node_label(node) for node in workflow.nodes if node.type == "start"}


def _file_summary(file_id: str, ref: Any) -> dict[str, Any]:
    """A file as a value of the start node's result: id, name, format and size."""
    summary: dict[str, Any] = {"id": file_id, "name": name_of(ref, file_id)}
    if isinstance(ref, FileRef):
        summary.update(format=ref.format, size=ref.size, source=ref.source)
    return summary


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


class CancelFlag:
    """Cancellation of a run; a sub-workflow's run is also cancelled when its parent is."""

    def __init__(self, parent: CancelFlag | None = None) -> None:
        self._event = threading.Event()
        self.parent = parent

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set() or (self.parent is not None and self.parent.is_set())


class _ChildTracker(PricedTracker):
    """A sub-workflow's usage: kept for the child run and, renamed, counted in the parent's budget.

    It prices with the parent's catalog and price mode, so a sub-workflow is priced exactly like its parent.
    The sub-workflow's own budget (``limit_usd``) applies in addition: Orkexa passes every record, check and
    reservation on to the parent (``parent=``), so a request is sent only while both the child's and every
    parent's budget can hold its most expensive outcome, and neither side can spend past the other's limit.
    """

    def __init__(self, parent: BudgetTracker, prefix: str, limit_usd: float | None = None) -> None:
        if isinstance(parent, PricedTracker):
            # The parent warns about unpriced usage once; the child only rejects it in strict mode.
            super().__init__(
                limit_usd,
                pricing=parent.pricing,
                mode=parent.mode,
                parent=parent,
                prefix=prefix,
                on_unpriced="reject" if parent.mode == "strict" else "ignore",
            )
        else:
            super().__init__(limit_usd, parent=parent, prefix=prefix, on_unpriced="ignore")
            self.price_resolver = parent.price_resolver

    def before_request(self, *, agent: str, provider: str, model: str, priced: bool) -> None:
        if self.limit_usd is not None and self.spent_usd >= self.limit_usd:
            raise BudgetExceededError(
                f"Budget of the sub-workflow used up: spent ${self.spent_usd:.4f} of ${self.limit_usd:.4f}; "
                f"request of agent {agent!r} to {provider}:{model} not sent"
            )
        super().before_request(agent=agent, provider=provider, model=model, priced=priced)


@dataclass
class RunEnvironment:
    """What a run executes with: its agents' tools, its sub-workflows and the tools' sandbox.

    Ordinary runs use the manager's own (the tool library and the saved workflow versions).
    A run of a published API uses the immutable snapshot of its release instead
    (:mod:`nodestro_api.publishing`), so later edits never change what an API runs.
    """

    builder: AgentBuilder
    resolve_workflow: Callable[[str, int | None], tuple[Workflow, int | None]]
    tool_sandbox: Callable[[str], Sandbox | None]
    tool_marker: Callable[[str], str]


class _RunContext:
    """Everything one execution of a run shares across its node threads."""

    def __init__(
        self,
        run: Run,
        tracker: BudgetTracker,
        env: RunEnvironment,
        *,
        cancelled: CancelFlag | None = None,
        stack: tuple[str, ...] = (),
        root: _RunContext | None = None,
    ) -> None:
        self.run = run
        self.tracker = tracker
        self.env = env
        self.cancelled = cancelled or CancelFlag()
        self.lock = threading.RLock()
        #: The workflows of the parent runs (a sub-workflow must not call one of them again).
        self.stack = stack
        self.root: _RunContext = root or self
        #: The run's usage before this execution (a paused run continues in place).
        self.base_usage = run.usage.model_copy()
        #: A decision arrived while the run was still executing other branches.
        self.resume_requested = False
        #: Set when a rejection ended the run.
        self.stop: Problem | None = None
        #: Set when the flow kernel stopped the run (a loop ran out of iterations, a limit was reached).
        self.flow_stop: flow.Stop | None = None
        #: Set when the run's time limit was reached and its running nodes were cancelled.
        self.timed_out = False
        self.fingerprints: dict[str, str] = {}
        #: The event loop the run executes on (events from worker threads are handed to it).
        self.loop: asyncio.AbstractEventLoop | None = root.loop if root is not None else None
        #: Queue execution (a worker's claimed run, C-06): the fence of the stored run as this execution last wrote
        #: it. Every write of the run is a compare-and-set on it, so a worker whose run was taken from it (its lease
        #: expired and the reaper marked the run interrupted) never overwrites the run. ``None`` on the root
        #: context: the runs are written without a condition (the desktop).
        self.fence: str | None = None
        #: Set (root context) when this execution lost its run (a fence did not match, or the worker lost its lease):
        #: nothing of it is stored or announced any more, and it is cancelled.
        self.fenced_out = False
        self.fence_lock = threading.Lock()

    def check(self) -> None:
        if self.cancelled.is_set():
            raise RunCancelled("The run was cancelled")


def _no_workflow(workflow_id: str, _version: int | None) -> tuple[Workflow, int | None]:
    raise KeyError(workflow_id)


def _usable_team(workflow: Workflow) -> bool:
    """Whether a selected workflow can run as a team: no errors (approval steps pause the orchestrator)."""
    return not any(issue.level == "error" for issue in check_workflow(workflow))


class RunManager:
    """Starts runs, keeps their live event streams and persists their results.

    Runs execute as ``asyncio`` tasks on the event loop that started them; agents run in worker
    threads and hand their events to that loop. :meth:`start`, :meth:`resume`, :meth:`decide`
    and :meth:`cancel` may also be called from another thread while that loop runs (e.g. an
    application that runs its loop in a background thread): the call is carried out on the loop.

    Event stream contract: every event of a run has a ``seq`` one higher than the event before,
    also beyond the history limit and across a pause in the same process. A run's history is
    dropped when it finishes or pauses; a paused run keeps only its counter.
    """

    def __init__(
        self,
        store: DocumentStore,
        builder: AgentBuilder,
        *,
        connect: Callable[[str], Connection],
        default_budget: Callable[[], float | None] = lambda: None,
        assistant_model: Callable[[], str] = lambda: DEFAULT_ASSISTANT_MODEL,
        tool_sandbox: Callable[[str], Sandbox | None] = lambda _workflow_id: None,
    ) -> None:
        self.store = store
        #: The sandbox for a workflow's tools: its Python environment, if it has one.
        self.tool_sandbox = tool_sandbox
        self.builder = builder
        #: The connection to a provider (key, endpoint); raises if the provider is not configured.
        self.connect = connect
        self.default_budget = default_budget
        self.assistant_model = assistant_model
        #: The workspace's price catalog and price mode (PRICING-1); without a catalog Orkexa's built-in prices apply.
        self.pricing: PricingService | None = None
        self.price_mode: Callable[[], PriceMode] = lambda: "standard"
        #: A saved workflow in a version (``None``: the latest), with the version's number.
        self.resolve_workflow: Callable[[str, int | None], tuple[Workflow, int | None]] = _no_workflow
        #: What identifies a tool's current code (for fingerprints of continued runs).
        self.tool_marker: Callable[[str], str] = lambda name: name
        #: The files a run can reference by id: name, or :class:`~nodestro_runtime.forms.FileRef` with format and size
        #: (checks the file fields of input forms).
        self.file_names: Callable[[], Mapping[str, str] | Mapping[str, FileRef]] | None = None
        #: The input files uploaded for runs (:mod:`nodestro_runtime.input_files`); ``None``: knowledge files only.
        self.inputs: InputFiles | None = None
        #: Called with every run that finished (not paused), e.g. to write its result files.
        self.after_run: Callable[[Run], object] | None = None
        #: Called whenever a run stops executing (finished or paused): batches, test series, schedules.
        self.listeners: list[Callable[[Run], None]] = []
        #: The environment of a run that does not use the manager's own (``None``): published APIs.
        self.environments: Callable[[Run], RunEnvironment | None] = lambda _run: None
        #: Queue execution (the server, ADR-0004): runs are handed to ``dispatch`` instead of executing in this
        #: process; a worker executes them with :meth:`execute_claimed`. ``None``: in-process (the desktop).
        self.dispatch: Callable[[Run], None] | None = None
        #: Asks the worker of a queued or running run (of another process) to cancel it; ``True`` when asked.
        self.remote_cancel: Callable[[Run], bool] | None = None
        #: Receives every event of the runs executing in this process (the server's event table).
        self.event_sink: Callable[[RunEvent], None] | None = None
        #: Reads the stored events of a run executing in another process: ``(run_id, after_seq)`` -> the next
        #: events in order (the server's event table). With it, :meth:`subscribe` and :meth:`is_live` follow such
        #: runs, and :meth:`wait` waits for them.
        self.remote_events: Callable[[str, int], list[RunEvent]] | None = None
        #: How often a run of another process is looked at (seconds).
        self.remote_poll = 0.25
        #: Per-call tool approvals (S-2, D10); ``None``: off (the default), runs call tools as before S-2.
        self.tool_gate: ToolGate | None = None
        #: How many agents (and orchestrator decisions) of this manager's runs execute at the same time, each in a
        #: thread of the manager's own pool (C-08; :data:`RUN_THREADS_ENV`). Read when the pool is first used.
        self.run_threads = run_threads()
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._executor_lock = threading.Lock()
        #: Runs whose execution in this process lost its fence (C-06), until :meth:`lost_fence` is asked.
        self._fence_lost: set[str] = set()
        self._followers: dict[int, asyncio.Task[None]] = {}
        self._contexts: dict[str, _RunContext] = {}
        self._events: dict[str, list[RunEvent]] = {}
        #: The ``seq`` of the last event of each live or paused run.
        self._seq: dict[str, int] = {}
        self._subscribers: dict[str, list[asyncio.Queue[RunEvent | None]]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        #: The loop of the most recently started run (calls from other threads are carried out there).
        self._loop: asyncio.AbstractEventLoop | None = None

    def _on_loop(self, call: Callable[[], T]) -> T | None:
        """Carry out ``call`` on the runs' event loop when it is called from a thread without a loop.

        Returns ``None`` when the call may run right here: this thread runs an event loop (runs it
        starts execute on that loop), or no loop runs at all.
        """
        loop = self._loop
        if loop is None or not loop.is_running() or loop.is_closed():
            return None
        with suppress(RuntimeError):
            asyncio.get_running_loop()
            return None
        done: concurrent.futures.Future[T] = concurrent.futures.Future()

        def execute() -> None:
            try:
                done.set_result(call())
            except BaseException as exc:
                done.set_exception(exc)

        loop.call_soon_threadsafe(execute)
        return done.result()

    # ---- public API ----------------------------------------------------------------

    def start(
        self,
        workflow: Workflow,
        request: RunIn,
        *,
        trigger: RunTrigger = "manual",
        trigger_ref: str | None = None,
        version: int | None = None,
        resumed_from: str | None = None,
        preset: dict[str, NodeRun] | None = None,
        release_id: str | None = None,
        release_number: int | None = None,
        api_key_id: str | None = None,
    ) -> Run:
        """Start a new run; ``preset`` node states are taken over and never executed again.

        ``release_id`` marks a run of a published API: it executes with its release's
        environment (see :attr:`environments`).
        """
        elsewhere = self._on_loop(
            lambda: self.start(
                workflow,
                request,
                trigger=trigger,
                trigger_ref=trigger_ref,
                version=version,
                resumed_from=resumed_from,
                preset=preset,
                release_id=release_id,
                release_number=release_number,
                api_key_id=api_key_id,
            )
        )
        if elsewhere is not None:
            return elsewhere
        _require_loop()
        problems = check_workflow(workflow)
        if problems:
            raise WorkflowError(
                "workflow.blocked",
                "; ".join(item.message for item in problems),
                problems=[item.model_dump() for item in problems],
            )
        prepared = prepare(
            start_fields(workflow),
            request.values,
            request.input,
            request.file_ids,
            file_names=self.file_names() if self.file_names is not None else None,
        )
        run = Run(
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            workflow=self.pin_subworkflows(workflow),
            input=prepared.input,
            values=prepared.values,
            text_input=request.input if prepared.values else None,
            file_ids=prepared.file_ids,
            nodes={node.id: NodeRun(status="waiting") for node in workflow.nodes},
            trigger=trigger,
            trigger_ref=trigger_ref,
            workflow_version=version,
            resumed_from=resumed_from,
            release_id=release_id,
            release_number=release_number,
            api_key_id=api_key_id,
        )
        if preset:
            run.nodes.update({node_id: state for node_id, state in preset.items() if node_id in run.nodes})
        return self._launch(run)

    def tracker(self, budget: float | None) -> PricedTracker:
        """The tracker of one run: the budget in USD (``None``: none), priced with the workspace's catalog."""
        return PricedTracker(
            Decimal(str(budget)) if budget is not None else None, pricing=self.pricing, mode=self.price_mode()
        )

    def environment(self, run: Run) -> RunEnvironment:
        """What ``run`` executes with (a sub-workflow's run: ask with its top-level run)."""
        found = self.environments(run)
        if found is not None:
            return found
        return RunEnvironment(
            builder=self.builder,
            resolve_workflow=self.resolve_workflow,
            tool_sandbox=self.tool_sandbox,
            tool_marker=self.tool_marker,
        )

    def pin_subworkflows(self, workflow: Workflow) -> Workflow:
        """A copy whose sub-workflow nodes name the exact version they will run."""
        selecting = [
            node
            for node in workflow.nodes
            if node.orchestrator is not None
            and node.orchestrator.select is not None
            and not node.orchestrator.select.done
        ]
        if not selecting and not any(graph.workflow_refs(node) for node in workflow.nodes):
            return workflow
        pinned = workflow.model_copy(deep=True)
        for node in pinned.nodes:
            for ref in graph.workflow_refs(node):
                if ref.version is None:
                    with suppress(KeyError, NodestroError):
                        ref.version = self.resolve_workflow(ref.workflow_id, None)[1]
        if selecting:
            candidates = self.team_candidates()
            for node in pinned.nodes:
                config = node.orchestrator
                if config is not None and config.select is not None and not config.select.done:
                    config.members += select_teams(config, workflow.id, candidates, _usable_team)
                    config.select.done = True
        return pinned

    def team_candidates(self) -> list[tuple[Workflow, int]]:
        """Saved workflows with capabilities, in their latest saved version (for pool selection)."""
        found: list[tuple[Workflow, int]] = []
        for current in self.store.list("workflows", Workflow):
            if not current.capabilities:
                continue
            with suppress(KeyError, NodestroError):
                snapshot, version = self.resolve_workflow(current.id, None)
                if version is not None and snapshot.capabilities:
                    found.append((snapshot, version))
        return found

    def _launch(self, run: Run, *, local: bool = False, fence: str | None = None) -> Run:
        if self.dispatch is not None and not local:
            # Queue execution: the run is stored and a worker executes it (ADR-0004). A paused run whose decision
            # was recorded is queued again, so everybody waiting for it sees that it goes on. Several processes can
            # continue the same paused run at once (a decision and the sweeps of every worker): only the one whose
            # compare-and-set from ``paused`` wins queues it, so there is one job and no stale run document
            # written over a run that already goes on.
            was = run.status
            run.status = "queued"
            if was == "paused":
                if not self.store.replace_if("runs", run.id, run, field="status", expected="paused"):
                    raise NodestroError("run.not_paused", "This run already continues elsewhere", status="queued")
            else:
                self.store.put("runs", run.id, run)
            self.dispatch(run)
            return run
        loop = _require_loop()
        workflow = run.workflow
        limit = workflow.budget_usd if workflow.budget_usd is not None else self.default_budget()
        run.budget_usd = limit
        budget = _remaining(limit, run)
        context = _RunContext(run, self.tracker(budget), self.environment(run))
        context.loop = loop
        context.fence = fence
        self._contexts[run.id] = context
        self._events.setdefault(run.id, [])
        self._subscribers.setdefault(run.id, [])
        self._save(context)
        self._loop = loop
        self._tasks[run.id] = loop.create_task(self._execute(context), name=f"run-{run.id}")
        return run

    def resume(self, run_id: str) -> Run:
        """Continue a paused run in place (after an approval decision), also after a restart."""
        elsewhere = self._on_loop(lambda: self.resume(run_id))
        if elsewhere is not None:
            return elsewhere
        context = self._contexts.get(run_id)
        if context is not None:
            context.root.resume_requested = True
            return context.run
        run = self.store.get("runs", run_id, Run)
        if run is None:
            raise KeyError(run_id)
        if run.status != "paused":
            raise NodestroError("run.not_paused", "Only a paused run can continue", status=run.status)
        return self._launch(run)

    def execute_claimed(self, run_id: str) -> Run:
        """Execute a run that a worker claimed from the queue, in this process (returns at once; see :meth:`wait`).

        Only a queued run, or a paused run whose decision was recorded, is executed; anything else raises
        ``run.not_executable`` (a claimed job never executes a run twice)."""
        elsewhere = self._on_loop(lambda: self.execute_claimed(run_id))
        if elsewhere is not None:
            return elsewhere
        if run_id in self._contexts:
            raise NodestroError("run.not_executable", "This run is already executing here", status="running")
        stored = self.store.get("runs", run_id, _FencedRun)
        if stored is None:
            raise KeyError(run_id)
        run, fence = _split(stored)
        if run.status not in ("queued", "paused"):
            raise NodestroError("run.not_executable", "Only a queued or paused run can be executed", status=run.status)
        # The run becomes this execution's (C-06): from now on only a write that still finds this fence stores it.
        claimed = self._replace_run(run, fence=fence, status=run.status)
        if claimed is None:
            raise NodestroError("run.not_executable", "This run changed while it was claimed", status=run.status)
        return self._launch(run, local=True, fence=claimed)

    def interrupt(self, run_id: str) -> bool:
        """Mark a run that stopped executing elsewhere (its worker died, or nothing executes it) as interrupted; it
        can be continued. Its queued or running sub-workflow runs are interrupted with it.

        Returns ``False`` for a run that is not queued or running (nothing to do). The write is conditional (C-06): a
        worker that still executes the run cannot overwrite it afterwards, and a run that went on meanwhile (a
        decision, another process) is not marked."""
        for _attempt in range(_CAS_ATTEMPTS):
            stored = self.store.get("runs", run_id, _FencedRun)
            if stored is None or stored.status not in ("queued", "running") or run_id in self._contexts:
                return False
            run, fence = _split(stored)
            status = run.status
            _interrupt(run)
            if self._replace_run(run, fence=fence, status=status) is None:
                continue  # written meanwhile: look again
            self._expire_approvals(run.id)
            for child_id in _child_ids(run):
                with suppress(Exception):
                    self.interrupt(child_id)
            self._notify(run)
            return True
        logger.warning("Run %s changed during every attempt to mark it interrupted", run_id)
        return False

    def cancel_unclaimed(self, run_id: str) -> bool:
        """Cancel a stored run that no worker executes (its queued job was withdrawn); ``False`` otherwise."""
        if run_id in self._contexts:
            return False
        return self._cancel_stored(run_id, ("queued", "paused"))

    def lost_fence(self, run_id: str) -> bool:
        """Whether the last execution of ``run_id`` in this process lost the run (C-06); asking forgets it."""
        if run_id in self._fence_lost:
            self._fence_lost.discard(run_id)
            return True
        return False

    def abandon(self, run_id: str, *, because: str) -> bool:
        """Stop executing a run that is no longer this process's (its worker lost the lease, C-06): nothing more of
        the execution is stored or announced, and it is cancelled. ``False`` when it does not execute here."""
        context = self._contexts.get(run_id)
        if context is None:
            return False
        self._lose_fence(context.root, because)
        return True

    def _replace_run(self, run: Run, *, fence: str | None, status: str) -> str | None:
        """Store ``run`` in place of the stored run it was read from only if nobody wrote that one since: a
        compare-and-set on the fence read with it, or (a run stored without one: the desktop, earlier versions) on
        the status read with it. Returns the run's new fence, ``None`` when another write came first."""
        new = _new_fence()
        stored = _fenced(run, new)
        if fence is not None:
            done = self.store.replace_if("runs", run.id, stored, field="fence", expected=fence)
        else:
            done = self.store.replace_if("runs", run.id, stored, field="status", expected=status)
        return new if done else None

    def _lose_fence(self, root: _RunContext, because: str) -> None:
        with root.fence_lock:
            if root.fenced_out:
                return
            root.fenced_out = True
        self._fence_lost.add(root.run.id)
        logger.error("Run %s is no longer executed here (%s); it stops without storing", root.run.id, because)
        root.cancelled.set()

    def root_of(self, run: Run) -> Run:
        """The top run of a sub-workflow's run (the run itself otherwise)."""
        seen = {run.id}
        while run.parent_run_id:
            parent = self.get(run.parent_run_id)
            if parent is None or parent.id in seen:
                break
            seen.add(parent.id)
            run = parent
        return run

    def decide(self, run_id: str, node_id: str, decision: str, comment: str = "", *, decided_by: str = "local") -> Run:
        """Record a person's decision at an approval node and let the run continue.

        Returns the top-level run. Nothing is decided without this call: a pending approval
        stays pending through restarts, test series and scheduled runs. A decision is recorded
        once: a second one for the same approval is refused.
        """
        elsewhere = self._on_loop(lambda: self.decide(run_id, node_id, decision, comment, decided_by=decided_by))
        if elsewhere is not None:
            return elsewhere
        context = self._contexts.get(run_id)
        fence: str | None = None
        if context is not None:
            run = context.run
        else:
            stored = self.store.get("runs", run_id, _FencedRun)
            if stored is None:
                raise KeyError(run_id)
            run, fence = _split(stored)
        read_status = run.status
        node = next((item for item in run.workflow.nodes if item.id == node_id), None)
        if node is None or node.type != "approval":
            raise KeyError(node_id)
        state = run.nodes.get(node_id)
        if state is None or state.status != "awaiting_approval" or state.approval is not None:
            raise NodestroError("approval.not_pending", "This approval is not waiting for a decision")
        if decision not in (APPROVED, REJECTED):
            raise NodestroError("approval.invalid", "Decide with approved or rejected")
        config = node.approval or ApprovalConfig()
        text = comment.strip()[:4000] if config.allow_comment else ""
        if decision == REJECTED and config.require_comment_on_reject and not text:
            raise NodestroError("approval.comment_required", "Please give a reason for the rejection")
        top = self.root_of(run)
        if self._contexts.get(top.id) is None:
            # The run continues on an event loop: refuse before anything is recorded.
            _require_loop()
            if top.status != "paused":
                # Only a paused run continues: refused before anything is recorded (not after, C-02).
                raise NodestroError("run.not_paused", "Only a paused run can continue", status=top.status)
        state.approval = ApprovalRecord(decision=decision, comment=text, decided_by=decided_by)  # type: ignore[arg-type]
        if context is not None:
            self._emit(context, "approval_decided", node_id, decision=decision, comment=text, decided_by=decided_by)
        elif self._replace_run(run, fence=fence, status=read_status) is None:
            # Another process (a second reviewer, a retried request) recorded a decision first (C-02): a
            # compare-and-set lets exactly one decision win, and this one is refused before anything is queued.
            raise NodestroError("approval.not_pending", "This approval was decided meanwhile")
        root = self.root_of(run)
        self.resume(root.id)
        return self.get(root.id) or root

    def cancel(self, run_id: str) -> bool:
        """Stop a running or paused run (its top-level run); ``False`` when it is neither."""
        elsewhere = self._on_loop(lambda: self.cancel(run_id))
        if elsewhere is not None:
            return elsewhere
        run = self.get(run_id)
        if run is None:
            return False
        root = self.root_of(run)
        context = self._contexts.get(root.id)
        if context is not None and context.run.status in ("queued", "running", "paused"):
            context.cancelled.set()
            self._emit(context, "run_cancelling")
            return True
        stored = self.store.get("runs", root.id, Run)
        if stored is None:
            return False
        # Queue execution: a worker may hold the run (also a paused run whose decision was just recorded).
        if (
            stored.status in ("queued", "running", "paused")
            and self.remote_cancel is not None
            and self.remote_cancel(stored)
        ):
            return True
        # Paused, or stored as queued or running while nothing executes it (C-11: restored from a backup, imported,
        # a queue entry that never arrived): it ends here as cancelled.
        return self._cancel_stored(root.id, ("queued", "running", "paused"))

    def _cancel_stored(self, run_id: str, statuses: tuple[str, ...] = ("paused",)) -> bool:
        """Cancel a stored run in one of ``statuses`` that is not executing (its pending approvals end undecided,
        nodes stored as running end cancelled); ``False`` when it is in none of them. The write is conditional: a
        run another process changed meanwhile is looked at again."""
        for _attempt in range(_CAS_ATTEMPTS):
            stored = self.store.get("runs", run_id, _FencedRun)
            if stored is None or stored.status not in statuses:
                return False
            run, fence = _split(stored)
            status = run.status
            children: list[str] = []
            for node in run.workflow.nodes:
                state = run.nodes.get(node.id)
                if state is None:
                    continue
                if state.status == "running":
                    state.status = "cancelled"
                    state.finished_at = now()
                    _fail(state, problem("run.node_cancelled", "Cancelled"))
                    children += [child_id for child_id in [state.child_run_id, *state.child_run_ids] if child_id]
                if state.status == "awaiting_approval":
                    state.status = "cancelled"
                    state.finished_at = now()
                    _fail(state, problem("run.node_cancelled", "Cancelled"))
                    children += [child_id for child_id in [state.child_run_id, *state.child_run_ids] if child_id]
                    if state.orchestration is not None:
                        state.orchestration = _cancelled_orchestration(state.orchestration)
            run.status = "cancelled"
            run.finished_at = now()
            _fail(run, problem("run.cancelled", "The run was cancelled"))
            if self._replace_run(run, fence=fence, status=status) is None:
                continue
            for child_id in children:
                self._cancel_stored(child_id, statuses)
            self.discard_tool_waits(run.id, because="cancelled")
            self._forget_events(run.id)
            self._notify(run)
            return True
        logger.warning("Run %s changed during every attempt to cancel it", run_id)
        return False

    # ---- runs paused at a tool call (M6b) ------------------------------------------------------------------------

    def discard_tool_waits(self, run_id: str, *, because: str) -> None:
        """A run paused at tool calls ends (cancelled, deleted, gone): its questions are closed and its checkpoints
        deleted, so nothing it waited for can run later."""
        ToolApprovals(self.store).close_checkpointed(run_id, because=because)
        tool_checkpoints.delete_run(self.store, run_id)
        if self.store.get_raw("runs", run_id) is None:
            # Only for a deleted run: the notes of started calls are what lets continuing a failed or cancelled
            # run ask before it calls tools again (:mod:`nodestro_runtime.tool_journal`).
            tool_journal.delete_run(self.store, run_id)

    def decide_tool_call(
        self,
        run_id: str,
        request_id: str,
        *,
        approved: bool,
        by: str,
        arguments: str,
        comment: str | None = None,
        current: tool_policy.ToolCapability | None = None,
    ) -> ToolApprovalRequest:
        """Approve or reject one tool call (:meth:`ToolApprovals.decide`). ``current`` is the tool's classification
        now: an approval of a call asked with another one is refused and closes the request (re-review R-1). A run
        paused at the call (M6b) continues once none of its calls waits any more; a run waiting in a thread reads the
        decision itself."""
        approvals = ToolApprovals(self.store)
        try:
            decided = approvals.decide(
                run_id, request_id, approved=approved, by=by, arguments=arguments, comment=comment, current=current
            )
        except NodestroError:
            found = approvals.get(run_id, request_id)
            if found is not None and found.checkpointed and found.closed_because == RECLASSIFIED:
                self._continue_paused(run_id)  # the paused run goes on and refuses the call
            raise
        if decided.checkpointed:
            self._continue_paused(run_id)
        return decided

    def _continue_paused(self, run_id: str) -> bool:
        """Continue the top-level run of a run paused at tool calls once none of them is pending; ``False`` when
        it cannot continue here (still waiting, not paused, executing, or no event loop: the sweep does it)."""
        approvals = ToolApprovals(self.store)
        if any(item.checkpointed for item in approvals.pending(run_id)):
            return False
        run = self.get(run_id)
        if run is None:
            return False
        root = self.root_of(run)
        if root.status != "paused" or root.id in self._contexts:
            return False
        # What this continue answers, read before the run goes on (a worker may reach the calls at once).
        answered = [item for item in approvals.of_run(run_id) if item.checkpointed and item.use == "open"]
        try:
            self.resume(root.id)
        except NodestroError:
            # No event loop here (``run.no_event_loop``: the next sweep continues it), or another process
            # continued it first (``run.not_paused``): only the process that queued the run audits.
            return False
        except KeyError:
            return False
        for item in answered:
            if item.status == "approved":
                # Marked when the resumed turn reaches the call (executed or stale).
                approvals.audit("resumed", item, outcome=item.status)
            elif item.status in ("rejected", "expired") and approvals.use(item, "closed") is not None:
                # Never usable again: closing it (compare-and-set) audits it once, not at every later continue.
                approvals.audit("resumed", item, outcome=item.status)
        return True

    def sweep_tool_waits(self, *, resume: bool = True) -> list[str]:
        """The deadline sweep of runs paused at tool calls (M6b; at start before anything continues, then
        periodically). Returns the runs it continued.

        - With the checkpoint switch off, every paused run's questions are closed (``switch_off``); when the run
          continues, its node ends without running them (and its checkpoint is deleted). No call runs without the
          gate.
        - Pending questions past their deadline expire (the call counts as not approved in time).
        - Checkpoints of runs that are gone or not paused any more are deleted, with their questions.
        - A paused run with no pending question left continues (also when the process stopped after a decision).
        """
        approvals = ToolApprovals(self.store)
        gate = self.tool_gate
        enabled = gate is not None and gate.checkpoints and checkpoints_enabled()
        holding = tool_checkpoints.runs_with_checkpoints(self.store)
        if not enabled:
            for run_id in holding:
                for item in approvals.of_run(run_id):
                    if item.checkpointed and (
                        item.status == "pending" or (item.status == "approved" and item.use == "open")
                    ):
                        approvals.audit("switch_off", item)
                # The checkpoint stays until the run continues: then the node ends without running the call.
                approvals.close_checkpointed(run_id, because=SWITCH_OFF)
        for item in approvals.overdue():
            approvals.close(item.run_id, item.id, "expired", because="deadline")
        continued: list[str] = []
        for run_id in holding:
            run = self.store.get("runs", run_id, Run)
            if run is None or run.status in ("succeeded", "failed", "cancelled"):
                self.discard_tool_waits(run_id, because="run.gone")
                continue
            if run.status == "paused" and resume and self._continue_paused(run_id):
                continued.append(run_id)
        return continued

    def _notify(self, run: Run) -> None:
        for listener in list(self.listeners):
            try:
                listener(run)
            except Exception:
                logger.exception("A listener of run %s failed", run.id)

    def recover(self) -> int:
        """After a restart: runs that were executing when Nodestro stopped are marked interrupted.

        Paused runs stay paused (their approvals are stored). Interrupted runs can be continued.
        Every stored run is considered, however many there are; runs executing in this process
        are left alone.
        """
        count = 0
        # A damaged run document is left out by ``find`` (and logged); one run that cannot be marked never stops the
        # start (C-10).
        for run in self.store.find("runs", "status", ("queued", "running"), Run):
            if run.imported_at is not None or run.id in self._contexts:
                continue
            try:
                _interrupt(run)
                self.store.put("runs", run.id, run)
                self._expire_approvals(run.id)
            except Exception:
                logger.exception("Marking run %s as interrupted failed; it stays as stored", run.id)
                continue
            count += 1
        return count

    def get(self, run_id: str) -> Run | None:
        context = self._contexts.get(run_id)
        if context is not None:
            return context.run
        return self.store.get("runs", run_id, Run)

    def is_live(self, run_id: str) -> bool:
        context = self._contexts.get(run_id)
        if context is not None:
            return context.run.status in ("queued", "running")
        return self.remote_events is not None and self._remote_active(run_id)

    def _remote_active(self, run_id: str) -> bool:
        """A run of another process (a worker) that is queued or executing."""
        stored = self.store.get("runs", run_id, Run) if not self.store.closed else None
        return stored is not None and stored.status in ("queued", "running")

    def has_live_runs(self) -> bool:
        return any(self.is_live(run_id) for run_id in list(self._contexts))

    def live_workflow_ids(self) -> set[str]:
        return {context.run.workflow_id for run_id, context in list(self._contexts.items()) if self.is_live(run_id)}

    def live_runs(self) -> list[Run]:
        return [context.run for context in list(self._contexts.values())]

    def subscribe(self, run_id: str) -> tuple[list[RunEvent], asyncio.Queue[RunEvent | None]]:
        """Past events of a live run plus a queue for the next ones (``None`` ends the stream)."""
        queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
        if run_id not in self._contexts and self.remote_events is not None and self._remote_active(run_id):
            # The run executes in a worker: its events come from the event table (from the first one on).
            self._followers[id(queue)] = asyncio.get_running_loop().create_task(
                self._follow(run_id, queue), name=f"follow-{run_id}"
            )
            return [], queue
        self._subscribers.setdefault(run_id, []).append(queue)
        return list(self._events.get(run_id, [])), queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[RunEvent | None]) -> None:
        follower = self._followers.pop(id(queue), None)
        if follower is not None:
            follower.cancel()
        with suppress(ValueError, KeyError):
            self._subscribers[run_id].remove(queue)

    async def _follow(self, run_id: str, queue: asyncio.Queue[RunEvent | None]) -> None:
        """Feed ``queue`` with the stored events of a run of another process until it ends or pauses."""
        read = self.remote_events
        assert read is not None
        last = 0
        try:
            while True:
                events = await asyncio.to_thread(read, run_id, last)
                for event in events:
                    last = event.seq
                    queue.put_nowait(event)
                    if event.type in ("run_finished", "run_paused"):
                        queue.put_nowait(None)
                        return
                if not events and not await asyncio.to_thread(self._remote_active, run_id):
                    # Ended without a final event (e.g. withdrawn from the queue or interrupted): read once more.
                    for event in await asyncio.to_thread(read, run_id, last):
                        queue.put_nowait(event)
                    queue.put_nowait(None)
                    return
                if queue.qsize() >= _MAX_SUBSCRIBER_BACKLOG:
                    queue.put_nowait(None)
                    return
                await asyncio.sleep(self.remote_poll)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Following the events of run %s failed", run_id)
            queue.put_nowait(None)

    async def wait(self, run_id: str) -> Run | None:
        """Wait until a run has finished or paused (used by tests, test series and batches).

        A run that a worker executes (queue execution) is waited for by looking at its stored state."""
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.shield(task)
        elif self.dispatch is not None:
            while await asyncio.to_thread(self._remote_active, run_id):
                await asyncio.sleep(self.remote_poll)
        return self.get(run_id)

    async def shutdown(self, *, timeout: float = _SHUTDOWN_GRACE_SECONDS) -> None:
        """Cancel every run of this process and wait until each one has stored its final state.

        Runs get ``timeout`` seconds to stop by themselves (an agent stops at its next model
        response or tool call); a run that does not is stopped at once and stored as cancelled.
        Paused runs are not affected: they wait in the database for their approval.
        """
        for context in list(self._contexts.values()):
            context.cancelled.set()
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending)
        executor, self._executor = self._executor, None
        if executor is not None:
            # Agents that did not stop yet end with their next model response or tool call.
            executor.shutdown(wait=False)

    # ---- events --------------------------------------------------------------------

    def _emit(self, context: _RunContext, kind: str, node_id: str | None = None, **data: Any) -> None:
        """Record and publish an event; safe to call from worker threads."""
        if node_id is not None:
            state = context.run.nodes.get(node_id)
            if state is not None and state.iteration:
                data.setdefault("iteration", list(state.iteration))
        loop = context.root.loop or self._loop
        if loop is None or context.root.fenced_out:
            # No loop yet: called outside a run (e.g. cancelling a stored run); nobody listens. Or the run is no
            # longer this process's (C-06): its stream belongs to whoever has it now.
            return
        if _in_loop(loop):
            self._publish(context.run.id, kind, node_id, data)
        else:
            # A closed loop (the application stopped) has no listeners left.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._publish, context.run.id, kind, node_id, data)

    def _publish(self, run_id: str, kind: str, node_id: str | None, data: dict[str, Any]) -> None:
        if run_id not in self._seq:
            # A run paused by an earlier process continues its numbering, so a client reconnecting
            # with Last-Event-ID never mistakes the new events for ones it has already seen.
            stored = self.store.get_raw(_EVENT_SEQ, run_id)
            self._seq[run_id] = stored if isinstance(stored, int) else 0
        seq = self._seq[run_id] + 1
        self._seq[run_id] = seq
        event = RunEvent(seq=seq, run_id=run_id, type=kind, node_id=node_id, data=data)
        if self.event_sink is not None:
            try:
                self.event_sink(event)
            except Exception:  # the stream of another process misses it; the run itself goes on
                logger.exception("Recording an event of run %s failed", run_id)
        events = self._events.setdefault(run_id, [])
        if len(events) < _MAX_EVENT_HISTORY or kind not in ("text", "thinking"):
            events.append(event)
        for queue in list(self._subscribers.get(run_id, [])):
            if queue.qsize() >= _MAX_SUBSCRIBER_BACKLOG:
                self._drop_subscriber(run_id, queue)
            else:
                queue.put_nowait(event)
        if kind in ("run_finished", "run_paused"):
            for queue in list(self._subscribers.get(run_id, [])):
                queue.put_nowait(None)

    def _drop_subscriber(self, run_id: str, queue: asyncio.Queue[RunEvent | None]) -> None:
        """End the stream of a subscriber that stopped reading: its queue is emptied and closed."""
        logger.warning("A subscriber of run %s fell %d events behind; its stream ends", run_id, queue.qsize())
        self.unsubscribe(run_id, queue)
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(None)

    def _save(self, context: _RunContext) -> None:
        with context.lock:
            snapshot = context.run.model_copy(deep=True)
        root = context.root
        if root.fence is None:
            self.store.put("runs", snapshot.id, snapshot)
            return
        # Queue execution (C-06): the run (and each sub-workflow run) is stored only while it is still this
        # execution's: a compare-and-set on the fence this execution wrote last.
        with root.fence_lock:
            if root.fenced_out:
                return
            expected = context.fence
            if expected is None and context is not root:
                # A sub-workflow run's first write in this execution compares the fence it is stored with.
                current = self.store.get("runs", snapshot.id, _FencedRun)
                expected = current.fence if current is not None else None
            fence = _new_fence()
            if expected is None:
                # A new sub-workflow run (or one stored without a fence): nothing to compare yet.
                self.store.put("runs", snapshot.id, _fenced(snapshot, fence))
                context.fence = fence
                return
            if self.store.replace_if("runs", snapshot.id, _fenced(snapshot, fence), field="fence", expected=expected):
                context.fence = fence
                return
        self._lose_fence(root, "the stored run was changed by another process")

    # ---- execution -----------------------------------------------------------------

    async def _execute(self, context: _RunContext) -> None:
        run = context.run
        try:
            await self._execute_body(context)
        except asyncio.CancelledError:
            # Stopped from outside (shutdown): the final state is already stored.
            pass
        finally:
            self._contexts.pop(run.id, None)
            self._tasks.pop(run.id, None)

    async def _execute_body(self, context: _RunContext) -> None:
        """Execute a run (also a sub-workflow's run) until it finished or waits for a person."""
        run = context.run
        run.status = "running"
        run.started_at = run.started_at or now()
        run.finished_at = None
        run.error = run.error_code = None
        run.error_params = {}
        self._emit(context, "run_started", status=run.status)
        # Stored at once: other processes (the API of a server) read the run from the database, not this context.
        self._save(context)
        try:
            while True:
                await self._run_graph(context)
                if context.resume_requested and not context.cancelled.is_set():
                    context.resume_requested = False
                    continue
                break
            self._conclude(context)
        except asyncio.CancelledError:
            # The task itself was cancelled (shutdown): the run must not stay "running" in the database.
            context.cancelled.set()
            _abandon(run)
            raise
        except Exception as exc:
            logger.exception("Run %s failed", run.id)
            run.status = "failed"
            _fail(run, node_problem(exc))
        finally:
            run.usage = add_usage(
                context.base_usage,
                usage_of(
                    context.tracker.usage,
                    context.tracker.records,
                    stale_requests=stale_requests(context.tracker),
                    uncapped=uncapped_requests(context.tracker),
                ),
            )
            run.budget_hard_maximum = _hard_maximum(run)
            paused = _is_paused(run)
            if not paused:
                run.finished_at = now()
            self._save(context)
            if context.root.fenced_out:
                # The run is no longer this process's (C-06): its end is not stored, announced or delivered here.
                self._events.pop(run.id, None)
                self._seq.pop(run.id, None)
                self._subscribers.pop(run.id, None)
            else:
                self._finished(context, paused)

    def _finished(self, context: _RunContext, paused: bool) -> None:
        """What follows a stored end (or pause) of an execution: result files, listeners, the last event."""
        run = context.run
        if not paused and self.after_run is not None:
            try:
                self.after_run(run)
            except Exception:  # result files are extra; the run's result stays valid
                logger.exception("Writing the result files of run %s failed", run.id)
        self._notify(run)
        self._emit(
            context,
            "run_paused" if paused else "run_finished",
            status=run.status,
            error=run.error,
            error_code=run.error_code,
            error_params=run.error_params,
            output=run.output,
            usage=run.usage.model_dump(mode="json"),
        )
        if paused:
            # Its numbering continues when it resumes, also after a restart; its history is not needed.
            self._events.pop(run.id, None)
            if run.id in self._seq:
                self.store.put_raw(_EVENT_SEQ, run.id, self._seq[run.id])
        else:
            self._forget_events(run.id)

    async def _in_run_thread(self, call: Callable[..., T], /, *args: Any) -> T:
        """``asyncio.to_thread`` on this manager's own agent pool (C-08; see :attr:`run_threads`)."""
        executor = self._executor
        if executor is None:
            with self._executor_lock:
                executor = self._executor
                if executor is None:
                    executor = self._executor = concurrent.futures.ThreadPoolExecutor(
                        max_workers=max(1, self.run_threads), thread_name_prefix="nodestro-run"
                    )
        context = contextvars.copy_context()
        return await asyncio.get_running_loop().run_in_executor(executor, functools.partial(context.run, call, *args))

    def _forget_events(self, run_id: str) -> None:
        """Drop the event history of a run that finished: streams only replay events of live runs."""
        self._events.pop(run_id, None)
        self._seq.pop(run_id, None)
        self._subscribers.pop(run_id, None)
        if not self.store.closed:
            self.store.delete(_EVENT_SEQ, run_id)

    def _conclude(self, context: _RunContext) -> None:
        run = context.run
        statuses = {state.status for state in run.nodes.values()}
        if context.stop is not None:
            run.status = "cancelled"
            _fail(run, context.stop)
        elif context.flow_stop is not None and context.flow_stop.code == "flow.loop_exhausted":
            run.status = "failed"
            loop = next((edge for edge in run.workflow.edges if edge.id == context.flow_stop.params.get("loop")), None)
            source = next((node for node in run.workflow.nodes if loop is not None and node.id == loop.source), None)
            label = node_label(source) if source is not None else ""
            limit = context.flow_stop.params.get("max_iterations")
            _fail(
                run,
                problem(
                    "run.loop_exhausted",
                    f"The loop at {label} ran {limit} times without reaching its goal",
                    node=label,
                    max_iterations=limit,
                ),
            )
        elif context.timed_out or (context.flow_stop is not None and context.flow_stop.code == "flow.time_limit"):
            run.status = "failed"
            limit = run.workflow.limits.max_duration_s
            _fail(
                run,
                problem(
                    "run.time_limit",
                    f"The run reached its time limit of {limit:g} seconds",
                    max_duration_s=limit,
                ),
            )
        elif context.flow_stop is not None and context.flow_stop.code == "flow.invocation_limit":
            run.status = "failed"
            steps = run.workflow.limits.max_steps
            _fail(run, problem("run.step_limit", f"The run reached its limit of {steps} steps", max_steps=steps))
        elif context.cancelled.is_set():
            run.status = "cancelled"
            _fail(run, problem("run.cancelled", "The run was cancelled"))
        elif "awaiting_approval" in statuses:
            run.status = "paused"
        elif statuses & {"failed", "blocked", "cancelled"}:
            run.status = "failed"
            failed = [node_label(node) for node in run.workflow.nodes if run.nodes[node.id].status == "failed"]
            _fail(
                run,
                problem("run.nodes_failed", f"Failed: {', '.join(failed)}", nodes=", ".join(failed))
                if failed
                else problem("run.incomplete", "Not all nodes ran"),
            )
        else:
            run.status = "succeeded"
        run.output = self._run_output(run)

    async def _run_graph(self, context: _RunContext) -> None:
        """Execute the nodes the ``orkexa.flow`` kernel dispatches until none is left to run.

        The kernel decides; this method executes nodes and applies what the kernel settled on
        its own (skipped, blocked, cancelled) to the node states (:mod:`nodestro_runtime.flow_driver`).
        The kernel's clock is the run's active time, so the workflow's ``limits.max_duration_s`` does not
        count time spent waiting for approvals; when it is reached, running nodes are cancelled.
        """
        run = context.run
        workflow = run.workflow
        topological_order(workflow)  # the runtime's own errors for broken connections and cycles
        spec = flow_driver.flow_of(workflow)
        nodes = {node.id: node for node in workflow.nodes}
        for node in workflow.nodes:
            run.nodes.setdefault(node.id, NodeRun(status="waiting"))
        if not context.fingerprints:
            context.fingerprints = self.fingerprints(run, context.env)
        segment = time.monotonic()
        base = run.active_seconds

        def clock() -> float:
            return base + (time.monotonic() - segment)

        state = flow_driver.state_of(workflow, run.nodes, run.flow_state)
        try:
            for item in state.invocations:
                if item.status == "running":
                    # Stopped mid-node with its process (the node is already marked failed): never run it twice.
                    failed = flow.Result(status="failed", error="run.interrupted")
                    state = self._kernel_step(
                        context, flow.complete(spec, state, item.id, failed, now=clock()), item.id
                    ).state
            if context.cancelled.is_set():
                step = self._kernel_step(context, flow.cancel(spec, state, code="run.node_cancelled", now=clock()))
            elif any(item.status == "suspended" for item in state.invocations):
                # Paused nodes are asked again: a decided approval succeeds, an open one stays paused.
                step = self._kernel_step(context, flow.resume(spec, state, now=clock()))
            else:
                step = self._kernel_step(context, flow.advance(spec, state, now=clock()))
            state = step.state
            tasks: dict[asyncio.Task[flow.Result], str] = {}
            actions = list(step.actions)
            limit = workflow.limits.max_duration_s
            while True:
                for action in actions:
                    node = nodes[action.invocation.node]
                    task = asyncio.ensure_future(self._invoke(context, node, action.invocation.path))
                    tasks[task] = action.invocation.id
                actions = []
                if not tasks:
                    break
                timeout = None if limit is None or state.stop is not None else max(limit - clock(), 0.0)
                finished, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if not finished:
                    # The time limit is reached while nodes still run: the kernel stops the flow, the
                    # running nodes are cancelled and report back like any cancelled node.
                    state = self._kernel_step(context, flow.advance(spec, state, now=clock())).state
                    context.timed_out = True
                    context.cancelled.set()  # running agents and tools stop at their next check
                    continue
                for task in finished:
                    invocation_id = tasks.pop(task)
                    if context.cancelled.is_set() and state.stop is None:
                        state = self._kernel_step(
                            context, flow.cancel(spec, state, code="run.node_cancelled", now=clock())
                        ).state
                    result = task.result()
                    step = self._kernel_step(
                        context, flow.complete(spec, state, invocation_id, result, now=clock()), invocation_id
                    )
                    state = step.state
                    actions.extend(step.actions)
        finally:
            run.active_seconds = clock()

    def _kernel_step(self, context: _RunContext, step: flow.Step, reported: str | None = None) -> flow.Step:
        """Store the kernel's new state and apply the nodes it settled without executing them.

        ``reported`` is the invocation whose result this step recorded (settled by the node itself).
        """
        run = context.run
        run.flow_state = step.state.model_dump(mode="json")
        if step.state.stop is not None and step.state.stop.code.startswith("flow."):
            context.flow_stop = step.state.stop
        for event in step.events:
            if event.kind == "loop_iteration" and event.node is not None:
                self._emit(context, "loop_iteration", event.node, **event.data)
            if event.kind != "invocation_settled" or event.node is None or event.invocation == reported:
                continue
            status = event.data.get("status")
            if status not in ("skipped", "blocked", "cancelled"):
                continue
            state = self._current(run, event.node, event.path)
            if state.status == status:
                continue
            state.fingerprint = context.fingerprints.get(event.node)
            state.status = status
            state.finished_at = now()
            if status == "skipped":
                # A regular outcome of a decision or approval: not an error.
                state.error = state.error_code = None
                state.error_params = {}
            elif status == "blocked":
                _fail(state, problem("run.blocked", "Not run because a predecessor did not succeed"))
            else:
                _fail(state, problem("run.node_cancelled", "Cancelled"))
            self._emit_settled(context, event.node)
        return step

    @staticmethod
    def _current(run: Run, node_id: str, path: tuple[int, ...]) -> NodeRun:
        """The node's state for the invocation at ``path``; a later loop iteration starts a fresh one."""
        state = run.nodes[node_id]
        if list(path) == state.iteration:
            return state
        if state.status == "waiting" and not state.earlier and state.started_at is None:
            state.iteration = list(path)
            return state
        earlier = state.model_copy(update={"earlier": []})
        fresh = NodeRun(status="waiting", iteration=list(path), earlier=[*state.earlier, earlier])
        run.nodes[node_id] = fresh
        return fresh

    async def _invoke(self, context: _RunContext, node: Node, path: tuple[int, ...] = ()) -> flow.Result:
        """Execute one node the kernel dispatched and report how it ended."""
        state = self._current(context.run, node.id, path)
        state.fingerprint = context.fingerprints.get(node.id)
        if context.cancelled.is_set():
            # Stopped by a node dispatched together with this one (a rejection that ends the run).
            state.status = "cancelled"
            state.finished_at = now()
            _fail(state, problem("run.node_cancelled", "Cancelled"))
            self._emit_settled(context, node.id)
            return flow_driver.result_of(node, state)
        try:
            if node.type == "approval":
                self._approval(context, node)
            else:
                await self._run_node(context, node)
        except Exception as exc:  # recorded on the node; the run continues with other branches
            logger.exception("Node %s failed", node.id)
            state.status = "failed"
            state.finished_at = state.finished_at or now()
            _fail(state, node_problem(exc))
            self._emit_settled(context, node.id)
        return flow_driver.result_of(node, state)

    def fingerprints(self, run: Run, env: RunEnvironment | None = None) -> dict[str, str]:
        env = env or self.environment(run)

        def child_tools(workflow_id: str, version: int | None) -> list[str]:
            try:
                child, number = env.resolve_workflow(workflow_id, version)
            except (KeyError, NodestroError):
                return ["missing"]
            names = sorted({name for node in child.nodes if node.agent for name in node.agent.tools})
            return [str(number), *(env.tool_marker(name) for name in names)]

        try:
            return graph.fingerprints(
                run.workflow,
                run_input=run.input,
                values=run.values,
                file_ids=run.file_ids,
                tool_marker=env.tool_marker,
                child_tools=child_tools,
            )
        except Exception:  # a fingerprint only enables reuse later; never let it stop a run
            logger.exception("Fingerprints of run %s failed", run.id)
            return {}

    def _emit_settled(self, context: _RunContext, node_id: str) -> None:
        state = context.run.nodes[node_id]
        self._emit(
            context,
            "node_finished",
            node_id,
            status=state.status,
            error=state.error,
            error_code=state.error_code,
            error_params=state.error_params,
        )

    def _inputs(
        self, run: Run, node_id: str, *, approval_comments: bool = True, loops: bool = True
    ) -> tuple[list[tuple[str, Any]], set[str]]:
        """The results arriving at a node over active connections, and the names of skipped ones.

        With ``approval_comments`` (agents, sub-workflows, approvals), a decided approval's comment
        arrives under the approval's name. Result and decision nodes get the results only: a
        reviewer's internal comment must not become part of the run's result, its result files or
        its published answer, and must not change what a decision evaluates.

        In a later iteration of a loop, the loop's first node also receives what arrives over the
        loop connection (the previous iteration's results behind the decision or approval).
        """
        nodes = {node.id: node for node in run.workflow.nodes}
        incoming = graph.predecessors(run.workflow)
        iteration = run.nodes[node_id].iteration if node_id in run.nodes else []
        looping = [
            edge
            for edge in graph.loop_edges(run.workflow)
            if loops and edge.target == node_id and iteration and iteration[-1] > 0
        ]
        found: list[tuple[str, Any]] = []
        seen: set[str] = set()
        silent: set[str] = set()

        def visit(target: str) -> None:
            for edge in [*incoming.get(target, []), *(looping if target == node_id else [])]:
                if edge.source in seen or not edge_active(edge, nodes, run.nodes):
                    continue
                seen.add(edge.source)
                source = nodes[edge.source]
                state = run.nodes[source.id]
                if source.type in TRANSPARENT:
                    visit(source.id)
                    if source.type != "approval" or not approval_comments:
                        continue
                    if state.approval is not None and state.approval.comment:
                        found.append(
                            (
                                node_label(source),
                                {"decision": state.approval.decision, "comment": state.approval.comment},
                            )
                        )
                    else:
                        # Decided without a comment: its placeholder reads "-" like a skipped result.
                        silent.add(node_label(source).strip().lower())
                else:
                    found.append((node_label(source), state.output))

        visit(node_id)
        arrived = {name.lower() for name, _ in found}
        absent = {
            node_label(nodes[source]).strip().lower()
            for source in data_sources(run.workflow, node_id)
            if node_label(nodes[source]).strip().lower() not in arrived
        } | silent
        return found, absent

    def _loop_context(self, run: Run, node_id: str, inputs: list[tuple[str, Any]]) -> LoopContext | None:
        """The loop placeholders of a node inside a loop (``None`` outside loops)."""
        if not any(node_id in body for body in graph.loop_bodies(run.workflow).values()):
            return None
        state = run.nodes[node_id]
        index = state.iteration[-1] if state.iteration else 0
        previous = None
        if state.earlier and state.earlier[-1].iteration[:-1] == state.iteration[:-1] and index > 0:
            previous = state.earlier[-1].output if state.earlier[-1].status == "succeeded" else None
        without, _ = self._inputs(run, node_id, loops=False)
        plain = {name for name, _ in without}
        return LoopContext(
            iteration=index + 1,
            previous=previous,
            feedback=[(name, value) for name, value in inputs if name not in plain],
        )

    def _feeds_from_start(self, run: Run, node_id: str) -> bool:
        sources = data_sources(run.workflow, node_id)
        starts = {item.id for item in run.workflow.nodes if item.type == "start"}
        return not sources or any(source in starts for source in sources)

    def _start_output(self, run: Run) -> Any:
        if not run.values:
            return run.input
        names: Mapping[str, Any] = self.file_names() if self.file_names is not None else {}
        fields = {field.name: field for field in start_fields(run.workflow)}
        output: dict[str, Any] = {}
        for key, value in run.values.items():
            field = fields.get(key)
            if field is not None and field.type == "file" and isinstance(value, list):
                # The files themselves reach the agents separately; the result names them.
                described = [_file_summary(str(item), names.get(str(item))) for item in value]
                output[key] = described if field.multiple else (described[0] if described else None)
            else:
                output[key] = value
        return output

    def _approval(self, context: _RunContext, node: Node) -> None:
        state = context.run.nodes[node.id]
        config = node.approval or ApprovalConfig()
        if state.approval is None:
            state.status = "awaiting_approval"
            state.started_at = state.started_at or now()
            self._emit(context, "approval_requested", node.id, status=state.status)
            self._save(context)
            return
        state.status = "succeeded"
        state.started_at = state.started_at or now()
        state.finished_at = now()
        if state.approval.decision == REJECTED and config.on_reject == "stop":
            label = node_label(node)
            context.stop = problem("run.approval_rejected", f"Rejected at {label}; the run ended", node=label)
            context.cancelled.set()
        self._emit(
            context,
            "node_finished",
            node.id,
            status=state.status,
            approval=state.approval.model_dump(mode="json"),
        )

    def _decide(self, context: _RunContext, node: Node, inputs: list[tuple[str, Any]]) -> None:
        run = context.run
        state = run.nodes[node.id]
        config = node.condition
        assert config is not None
        if config.source:
            source = run.nodes.get(config.source)
            available = source is not None and source.status == "succeeded"
            value = source.output if available and source is not None else None
        elif len(inputs) == 1:
            available, value = True, inputs[0][1]
        else:
            available, value = bool(inputs), dict(inputs)
        record = decide(config, value, available=available)
        invalid = record.fallback_code in ("condition.field_missing", "condition.not_evaluable", "condition.no_input")
        state.decision = record
        state.finished_at = now()
        if invalid and config.on_invalid == "fail":
            record.branches, record.labels = [], []
            state.status = "failed"
            _fail(
                state,
                problem(
                    record.fallback_code or "condition.not_evaluable",
                    f"{node_label(node)}: the result cannot be evaluated",
                    **record.fallback_params,
                ),
            )
        else:
            state.status = "succeeded"
        self._emit(
            context,
            "node_finished",
            node.id,
            status=state.status,
            decision=record.model_dump(mode="json"),
            error=state.error,
            error_code=state.error_code,
            error_params=state.error_params,
        )

    def _run_output(self, run: Run) -> Any:
        settled = {node_id for node_id, state in run.nodes.items() if state.status == "succeeded"}
        outputs = [node for node in run.workflow.nodes if node.type == "output"]
        if outputs:
            values = {node_label(node): run.nodes[node.id].output for node in outputs if node.id in settled}
        else:
            sources = {edge.source for edge in run.workflow.edges}
            sinks = [node for node in run.workflow.nodes if node.type in WORKING and node.id not in sources]
            values = {node_label(node): run.nodes[node.id].output for node in sinks if node.id in settled}
        if not values:
            return None
        return next(iter(values.values())) if len(values) == 1 else values

    async def _run_node(self, context: _RunContext, node: Node) -> None:
        run = context.run
        state = run.nodes[node.id]
        inputs, absent = self._inputs(run, node.id, approval_comments=node.type in WORKING)
        state.started_at = now()
        if node.type == "start":
            state.status = "succeeded"
            state.output = self._start_output(run)
            state.finished_at = now()
            self._emit(context, "node_finished", node.id, status="succeeded", output=state.output)
            return
        if node.type == "output":
            contract = node.output.contract if node.output is not None else []
            if contract:
                try:
                    state.output = contracts.apply_contract(contract, contracts.merged_input(inputs))
                    state.status = "succeeded"
                except contracts.ContractError as error:
                    state.status = "failed"
                    names = ", ".join(error.fields)
                    _fail(
                        state,
                        problem(
                            "run.output_contract",
                            f"The result does not fulfil the workflow's contract: {names}",
                            fields=names,
                        ),
                    )
            else:
                state.status = "succeeded"
                state.output = inputs[0][1] if len(inputs) == 1 else {name: value for name, value in inputs}
            state.finished_at = now()
            self._emit(
                context,
                "node_finished",
                node.id,
                status=state.status,
                output=state.output,
                error=state.error,
                error_code=state.error_code,
                error_params=state.error_params,
            )
            return
        if node.type == "condition":
            self._decide(context, node, inputs)
            return
        if node.type == "subworkflow":
            await self._subworkflow(context, node, inputs, absent)
            return
        if node.type == "orchestrator":
            await self._orchestrate(context, node, inputs, absent)
            return
        assert node.agent is not None
        # Paused at a tool call (M6b): the turn continues from its checkpoint, the model is not asked again.
        resuming = state.status == "awaiting_approval"
        state.status = "running"
        config = node.agent
        prompt = render_task(
            config.task_template,
            run_input=run.input,
            inputs=inputs,
            values=self._prompt_values(run),
            absent=absent,
            run_names=_start_names(run.workflow),
            loop=self._loop_context(run, node.id, inputs),
        )
        state.prompt = prompt
        self._emit(context, "node_started", node.id, prompt=prompt, model=config.model)
        await self._in_run_thread(
            self._run_agent_sync, context, node, prompt, self._feeds_from_start(run, node.id), resuming
        )
        self._save(context)
        if state.tool_calls and not context.root.fenced_out:
            # The call records are stored with the node now; the notes written before each call are not needed.
            # A run another process took over (C-06) was not stored here: its notes stay for that process.
            tool_journal.clear_node(self.store, run.id, node.id)

    def _prompt_values(self, run: Run, *, mark_empty: bool = True) -> dict[str, Any]:
        """The run's form values as task placeholders show them (file names, empty fields marked)."""
        if not run.values:
            return run.values
        names = self.file_names() if self.file_names is not None else {}
        return prompt_values(start_fields(run.workflow), run.values, names, mark_empty=mark_empty)

    async def _execute_child(self, context: _RunContext, node: Node, child: Run) -> _RunContext:
        """Run a child run to its end (or pause): its own budget inside the parent's, cost counted at ``node``."""
        child.budget_usd = child.workflow.budget_usd
        child_context = _RunContext(
            child,
            _ChildTracker(context.tracker, node.id, _remaining(child.workflow.budget_usd, child)),
            context.env,
            cancelled=CancelFlag(context.cancelled),
            stack=(*context.stack, context.run.workflow_id),
            root=context.root,
        )
        self._contexts[child.id] = child_context
        self._events.setdefault(child.id, [])
        try:
            await self._execute_body(child_context)
        finally:
            self._contexts.pop(child.id, None)
        return child_context

    async def _subworkflow(
        self, context: _RunContext, node: Node, inputs: list[tuple[str, Any]], absent: set[str]
    ) -> None:
        """Run another workflow as one step: a child run with this engine, cost counted here."""
        run = context.run
        state = run.nodes[node.id]
        config = node.subworkflow
        assert config is not None
        prior = state.usage.model_copy()
        label = node_label(node)
        child = self.get(state.child_run_id) if state.child_run_id else None
        if child is None:
            if config.workflow_id == run.workflow_id or config.workflow_id in context.stack:
                raise NodestroError(
                    "run.subworkflow_recursive", f"{label} would call a workflow that is already running", node=label
                )
            if len(context.stack) >= MAX_DEPTH:
                raise NodestroError(
                    "run.subworkflow_too_deep", f"Sub-workflows nested too deeply at {label}", node=label
                )
            try:
                snapshot, version = context.env.resolve_workflow(config.workflow_id, config.version)
            except KeyError:
                raise NodestroError(
                    "run.subworkflow_missing", f"The workflow of {label} no longer exists", node=label
                ) from None
            shown = self._prompt_values(run)
            text = render_task(
                config.input_template or "{{input}}",
                run_input=run.input,
                inputs=inputs,
                values=shown,
                absent=absent,
                run_names=_start_names(run.workflow),
            )
            child_fields = {item.name: item for item in start_fields(snapshot)}
            values: dict[str, Any] = {}
            for key, template in config.values.items():
                field = child_fields.get(key)
                if field is not None and field.type == "file":
                    # Files are passed on by reference: the same stored files, no copies.
                    values[key] = _file_references(template, run)
                else:
                    values[key] = render_task(
                        template,
                        run_input=run.input,
                        inputs=inputs,
                        values=self._prompt_values(run, mark_empty=False),
                        absent=absent,
                        append=False,
                    )
            prepared = prepare(
                start_fields(snapshot),
                values,
                text,
                run.file_ids if self._feeds_from_start(run, node.id) else [],
                file_names=self.file_names() if self.file_names is not None else None,
            )
            child = Run(
                workflow_id=snapshot.id,
                workflow_name=snapshot.name,
                workflow=self.pin_subworkflows(snapshot),
                input=prepared.input,
                values=prepared.values,
                file_ids=prepared.file_ids,
                nodes={item.id: NodeRun(status="waiting") for item in snapshot.nodes},
                trigger="subworkflow",
                trigger_ref=run.id,
                parent_run_id=run.id,
                parent_node_id=node.id,
                workflow_version=version,
            )
            state.child_run_id = child.id
            state.child_version = version
            state.prompt = prepared.input
            self.store.put("runs", child.id, child)
        state.status = "running"
        self._emit(context, "node_started", node.id, child_run_id=child.id, version=state.child_version)
        self._save(context)
        child_context = await self._execute_child(context, node, child)
        state.usage = add_usage(prior, self._node_usage(context, node.id))
        state.finished_at = now()
        if child.status == "succeeded":
            state.status = "succeeded"
            state.output = child.output
        elif child.status == "paused":
            state.status = "awaiting_approval"
            state.finished_at = None
        elif child.status == "cancelled":
            state.status = "cancelled"
            _fail(state, problem("run.node_cancelled", "Cancelled"))
            if child_context.stop is not None and not context.cancelled.is_set():
                context.stop = child_context.stop
                context.cancelled.set()
        else:
            state.status = "failed"
            _fail(
                state,
                problem(
                    "run.subworkflow_failed",
                    f"Sub-workflow {child.workflow_name} failed: {child.error or ''}",
                    name=child.workflow_name,
                    error=child.error or "",
                ),
            )
        self._emit(
            context,
            "node_finished",
            node.id,
            status=state.status,
            output=state.output,
            child_run_id=child.id,
            error=state.error,
            error_code=state.error_code,
            error_params=state.error_params,
            usage=state.usage.model_dump(mode="json"),
        )
        self._save(context)

    def _gate_call(
        self,
        context: _RunContext,
        node_id: str,
        config: AgentConfig,
        call: ToolCall,
        record: ToolCallRecord,
        gate: ToolGate,
        *,
        suspend: bool = False,
    ) -> str | None:
        """Apply the per-call rules (S-2) to one call in the agent's thread: ``None`` runs it, a string denies it.

        Asking waits here for a stored decision (at most the deadline); the run stays ``running``. An approval only
        lets the call run as it would have without the question, in its sandbox (ADR-0006).

        With ``suspend`` (agent checkpoints, M6b) asking stores the same request and stops the turn instead
        (:class:`~orkexa.ToolCallSuspended`): the run pauses and frees its worker; the decision continues it
        (:meth:`_gate_resumed_call`)."""
        capability, version, kind, definition = tool_facts(context.env.builder.tools, call.name)
        root = context.root.run
        situation = gate.call_context(
            run_id=context.run.id, node_id=node_id, trigger=root.trigger, trigger_ref=root.trigger_ref
        )
        verdict = tool_policy.decide(
            tool=call.name,
            kind=kind,
            capability=capability,
            binding=config.tool_policies.get(call.name),
            context=situation,
            at=now(),
            version=version,
        )
        shown = capability.model_dump()
        if verdict.action == "allow":
            record.approval = ToolCallApproval(status="allowed", pre_grant_id=verdict.pre_grant_id, capability=shown)
            bind_call(CallBinding(tool=call.name, capability=capability, definition_hash=definition))
            return None
        if verdict.action == "deny":
            return self._deny_call(
                context, node_id, call, record, verdict.code or "tool.denied_by_policy", verdict.reason or "", shown
            )
        run_id = context.run.id
        if suspend:
            return self._suspend_call(context, node_id, call, record, gate, capability, version, definition, shown)
        if not gate.slots.enter(run_id):
            return self._deny_call(
                context,
                node_id,
                call,
                record,
                "tool.approval_capacity",
                "Too many runs are waiting for an approval right now, so this call was not made",
                shown,
            )
        try:
            request = gate.approvals.request(
                run_id=run_id,
                node_id=node_id,
                iteration=context.run.nodes[node_id].iteration,
                call_id=call.id,
                tool=call.name,
                arguments=call.input,
                capability=capability,
                version=version,
                definition_hash=definition,
            )
            record.approval = ToolCallApproval(
                status="pending", request_id=request.id, deadline=request.deadline, capability=shown
            )
            self._emit(
                context,
                "tool_approval_requested",
                node_id,
                id=call.id,
                name=call.name,
                request_id=request.id,
                deadline=request.deadline.isoformat(),
                arguments_hash=request.arguments_hash,
            )
            self._save(context)
            try:
                final = gate.approvals.wait(run_id, request.id, check=context.check)
            except BaseException:
                record.approval = record.approval.model_copy(update={"status": "cancelled"})
                raise
        finally:
            gate.slots.leave(run_id)
        if final.status == "expired" and final.closed_because == INTERRUPTED:
            # Stopped elsewhere (the reaper after a lost lease, a restart): this execution must not go on.
            record.approval = record.approval.model_copy(update={"status": "expired", "code": "run.interrupted"})
            raise RunCancelled("The run was interrupted while it waited for an approval")
        code = _decision_code(final)
        record.approval = ToolCallApproval(
            status=final.status,
            request_id=final.id,
            code=code,
            deadline=final.deadline,
            decided_by=final.decided_by,
            decided_at=final.decided_at,
            comment=final.comment,
            capability=shown,
        )
        self._emit(
            context,
            "tool_approval_decided",
            node_id,
            id=call.id,
            request_id=final.id,
            status=final.status,
            decided_by=final.decided_by,
        )
        self._save(context)
        if final.status == "approved":
            # The approval binds the call to the tool as it was asked about (arguments, version, MCP definition
            # hash, classification). An administrator may have approved another MCP definition (independent review
            # L-4) or classified the tool differently (re-review R-1) while the person decided: then this approval
            # does not cover the call that would run now. Own tools are also bound to their version by the tool object
            # itself (it refuses to run after a change).
            capability, _version, _kind, definition = tool_facts(context.env.builder.tools, call.name)
            same = _current_hash(context.env.builder.tools, call) == final.arguments_hash
            stale = None
            if not same and is_mcp_tool(call.name):
                stale = "changed"
            elif same and reclassified(final.capability, capability):
                stale = "reclassified"
            if stale is not None:
                gate.approvals.audit("stale", final, outcome=stale)
                return self._deny_call(
                    context,
                    node_id,
                    call,
                    record,
                    "tool.approval_stale",
                    "The tool changed while this call waited for its approval, so it was not made",
                    shown,
                )
            # The call runs as approved, and only so: an MCP tool checks this again right before it sends the call.
            bind_call(CallBinding(tool=call.name, capability=final.capability, definition_hash=definition))
            return None
        if final.status == "expired" and final.closed_because == RECLASSIFIED:
            return self._deny_call(
                context,
                node_id,
                call,
                record,
                "tool.approval_stale",
                "The tool was classified differently while this call waited for its approval, so it was not made",
                shown,
            )
        if final.status == "rejected":
            return "A person rejected this call" + (f": {final.comment}" if final.comment else "")
        if final.status == "expired":
            return "Nobody approved this call in time, so it was not made"
        context.check()
        raise RunCancelled("The run was cancelled")

    def _suspend_call(
        self,
        context: _RunContext,
        node_id: str,
        call: ToolCall,
        record: ToolCallRecord,
        gate: ToolGate,
        capability: tool_policy.ToolCapability,
        version: int | None,
        definition: str | None,
        shown: dict[str, str],
    ) -> str | None:
        """Ask about a call by pausing the run at it (M6b): store the request, then stop the turn."""
        run_id = context.run.id
        paused = tool_checkpoints.runs_with_checkpoints(self.store) - {run_id}
        if len(paused) >= gate.max_paused:
            return self._deny_call(
                context,
                node_id,
                call,
                record,
                "tool.approval_capacity",
                "Too many runs are waiting for an approval right now, so this call was not made",
                shown,
            )
        request = gate.approvals.request(
            run_id=run_id,
            node_id=node_id,
            iteration=context.run.nodes[node_id].iteration,
            call_id=call.id,
            tool=call.name,
            arguments=call.input,
            capability=capability,
            version=version,
            definition_hash=definition,
            checkpointed=True,
        )
        record.approval = ToolCallApproval(
            status="pending", request_id=request.id, deadline=request.deadline, capability=shown
        )
        gate.approvals.audit("suspended", request)
        self._emit(
            context,
            "tool_approval_requested",
            node_id,
            id=call.id,
            name=call.name,
            request_id=request.id,
            deadline=request.deadline.isoformat(),
            arguments_hash=request.arguments_hash,
        )
        raise ToolCallSuspended("waits for a person's approval", ref=request.id)

    def _gate_resumed_call(
        self,
        context: _RunContext,
        node_id: str,
        config: AgentConfig,
        call: ToolCall,
        record: ToolCallRecord,
        gate: ToolGate,
        iteration: list[int],
        released: dict[str, ToolApprovalRequest],
    ) -> str | None:
        """Decide a call of a resumed turn (M6b; Evolution conditions 1, 2 and 5).

        Only the stored request of exactly this call answers it (run, node, iteration, call id, arguments hash and
        tool version or definition hash); nobody is asked again and no new request is made. The rules are applied
        again and a new deny wins. An approved call runs only within :data:`EXECUTION_WINDOW` after its decision and
        at most once: ``use`` is set to ``executed`` by compare-and-set right before it runs, so a second resume (or
        a crash after the mark) never runs it again (``tool.result_lost``). The released request is put in
        ``released``; what really happened (``executed``, or ``stale`` when the tool changed in between and refused
        the call) is audited when the call ends (:meth:`_audit_released`)."""
        approvals = gate.approvals
        run_id = context.run.id
        shown = record.approval.capability if record.approval is not None else {}
        found = approvals.matching(run_id, node_id=node_id, iteration=iteration, call_id=call.id)
        if found is None:
            return self._deny_call(
                context, node_id, call, record, "tool.approval_missing", "The approval of this call is gone", shown
            )
        if found.status == "pending":
            # Still undecided (another call of the same turn was decided): keep waiting, without a new question.
            raise ToolCallSuspended("waits for a person's approval", ref=found.id)
        capability, version, kind, definition = tool_facts(context.env.builder.tools, call.name)
        shown = capability.model_dump()
        current = _current_hash(context.env.builder.tools, call)
        if found.status != "approved":
            return self._record_decision(context, node_id, record, found, shown)
        if found.use == "executed":
            approvals.audit("result_lost", found)
            return self._deny_call(
                context,
                node_id,
                call,
                record,
                "tool.result_lost",
                "This call may have been made, but its result was lost; it is not made again",
                shown,
            )
        if found.use != "open":
            return self._deny_call(
                context, node_id, call, record, "tool.approval_stale", "This approval can no longer be used", shown
            )
        stale: str | None = None
        if current != found.arguments_hash:
            stale = "changed"
        elif reclassified(found.capability, capability):
            stale = "reclassified"
        elif found.decided_at is None or found.decided_at > found.deadline:
            stale = "late_decision"
        elif now() > found.decided_at + EXECUTION_WINDOW:
            stale = "execution_window"
        if stale is not None:
            if approvals.use(found, "stale") is not None:
                approvals.audit("stale", found, outcome=stale)
            return self._deny_call(
                context,
                node_id,
                call,
                record,
                "tool.approval_stale",
                "The approved call changed or waited too long, so it was not made",
                shown,
            )
        root = context.root.run
        situation = gate.call_context(
            run_id=run_id, node_id=node_id, trigger=root.trigger, trigger_ref=root.trigger_ref
        )
        verdict = tool_policy.decide(
            tool=call.name,
            kind=kind,
            capability=capability,
            binding=config.tool_policies.get(call.name),
            context=situation,
            at=now(),
            version=version,
        )
        if verdict.action == "deny":
            if approvals.use(found, "stale") is not None:
                approvals.audit("stale", found, outcome=verdict.code or "tool.denied_by_policy")
            return self._deny_call(
                context, node_id, call, record, verdict.code or "tool.denied_by_policy", verdict.reason or "", shown
            )
        marked = approvals.use(found, "executed")
        if marked is None:
            # Another process used it first: never run the call twice.
            approvals.audit("result_lost", found, outcome="raced")
            return self._deny_call(
                context, node_id, call, record, "tool.result_lost", "This call was already handled elsewhere", shown
            )
        released[call.id] = marked
        self._record_decision(context, node_id, record, marked, shown)
        bind_call(CallBinding(tool=call.name, capability=marked.capability, definition_hash=definition))
        return None

    def _audit_released(
        self, context: _RunContext, gate: ToolGate, call: ToolCall, request: ToolApprovalRequest, is_error: bool
    ) -> None:
        """Audit what became of a released call of a resumed turn. A call that failed because the tool changed
        after its release (a pinned tool refuses to run another version) did not run: it is audited ``stale``
        (``tool.changed``), not ``executed``. The request stays ``executed``, so the call never runs later."""
        tools = context.env.builder.tools
        if is_error and _current_hash(tools, call) != request.arguments_hash:
            gate.approvals.audit("stale", request, outcome="tool.changed")
        elif is_error and reclassified(request.capability, tool_facts(tools, call.name)[0]):
            gate.approvals.audit("stale", request, outcome="tool.reclassified")
        else:
            gate.approvals.audit("executed", request)

    def _record_decision(
        self,
        context: _RunContext,
        node_id: str,
        record: ToolCallRecord,
        final: ToolApprovalRequest,
        shown: dict[str, str],
    ) -> str | None:
        """Record a decided request on the call; ``None`` when approved, the denial text otherwise."""
        code = _decision_code(final)
        if final.status == "cancelled":
            code = final.closed_because or "run.cancelled"
        record.approval = ToolCallApproval(
            status=final.status,
            request_id=final.id,
            code=code,
            deadline=final.deadline,
            decided_by=final.decided_by,
            decided_at=final.decided_at,
            comment=final.comment,
            capability=shown,
        )
        self._emit(
            context,
            "tool_approval_decided",
            node_id,
            id=record.id,
            request_id=final.id,
            status=final.status,
            decided_by=final.decided_by,
        )
        if final.status == "approved":
            return None
        if final.status == "rejected":
            return "A person rejected this call" + (f": {final.comment}" if final.comment else "")
        if final.status == "expired":
            return "Nobody approved this call in time, so it was not made"
        return "The question about this call was closed, so it was not made"

    def _suspend_agent(
        self,
        context: _RunContext,
        node_id: str,
        state: NodeRun,
        iteration: list[int],
        stop: AgentSuspended,
        gate: ToolGate,
    ) -> None:
        """Store where the turn stopped and let the node wait (the run pauses)."""
        turn = stop.checkpoint
        refs = [item.ref for item in turn.waiting if item.ref]
        requests = [found for ref in refs if (found := gate.approvals.get(context.run.id, ref)) is not None]
        latest = max((item.deadline for item in requests), default=now())
        tool_checkpoints.save(
            self.store,
            tool_checkpoints.AgentCheckpoint(
                run_id=context.run.id,
                node_id=node_id,
                iteration=iteration,
                turn=turn.model_dump(mode="json"),
                memory=stop.memory,
                request_ids=refs,
                expires_at=latest + EXECUTION_WINDOW,
            ),
        )
        state.status = "awaiting_approval"

    def _checkpoint_lost(self, context: _RunContext, node: Node, state: NodeRun, prior: Usage | None) -> None:
        """A paused agent whose checkpoint is gone, expired or switched off: the waiting calls never run."""
        gate = self.tool_gate
        if gate is not None:
            self._close_node_waits(context.run.id, node.id, list(state.iteration), gate.approvals, because=SWITCH_OFF)
        tool_checkpoints.delete(self.store, context.run.id, node.id, list(state.iteration))
        state.status = "failed"
        if prior is not None:
            state.usage = prior
        state.finished_at = now()
        _fail(
            state,
            problem(
                "tool.checkpoint_expired",
                "The waiting tool call can no longer continue (it expired or the setting was switched off)",
            ),
        )
        self._emit(
            context,
            "node_finished",
            node.id,
            status=state.status,
            error=state.error,
            error_code=state.error_code,
            error_params=state.error_params,
            usage=state.usage.model_dump(mode="json"),
        )

    @staticmethod
    def _close_node_waits(
        run_id: str, node_id: str, iteration: list[int], approvals: ToolApprovals, *, because: str
    ) -> None:
        """Close the open requests of one node's turn: pending ones are cancelled, approved unused ones closed."""
        for item in approvals.of_run(run_id):
            if not item.checkpointed or item.node_id != node_id or item.iteration != iteration:
                continue
            if item.status == "pending":
                approvals.close(run_id, item.id, "cancelled", because=because)
            elif item.status == "approved" and item.use == "open":
                approvals.use(item, "closed")

    def _deny_call(
        self,
        context: _RunContext,
        node_id: str,
        call: ToolCall,
        record: ToolCallRecord,
        code: str,
        reason: str,
        shown: dict[str, str],
    ) -> str:
        record.approval = ToolCallApproval(status="denied", code=code, capability=shown)
        self._emit(context, "tool_denied", node_id, id=call.id, name=call.name, code=code, reason=reason)
        return reason

    def _expire_approvals(self, run_id: str) -> None:
        """A run stopped without its process (interrupted, crashed, lost lease): its open questions expire."""
        ToolApprovals(self.store).expire_run(run_id)

    def _orchestration_pool(
        self, context: _RunContext, node: Node
    ) -> tuple[orchestration.Pool, dict[str, tuple[Workflow, int | None]]]:
        """The teams of an orchestrator, resolved in their version (pinned at the start of the run)."""
        config = node.orchestrator
        assert config is not None
        label = node_label(node)
        if len(context.stack) >= MAX_DEPTH:
            raise NodestroError("run.subworkflow_too_deep", f"Sub-workflows nested too deeply at {label}", node=label)
        teams: dict[str, tuple[Workflow, int | None]] = {}
        members: list[orchestration.Member] = []
        for item in config.members:
            if item.workflow_id == context.run.workflow_id or item.workflow_id in context.stack:
                if item.matched:
                    continue  # selected by capability: a team that is already running is simply not offered
                raise NodestroError(
                    "run.subworkflow_recursive", f"{label} would call a workflow that is already running", node=label
                )
            try:
                snapshot, version = context.env.resolve_workflow(item.workflow_id, item.version)
            except KeyError:
                raise NodestroError(
                    "run.orchestrator_member_missing", f"A team of {label} no longer exists", node=label
                ) from None
            if snapshot.id in teams:
                continue
            teams[snapshot.id] = (snapshot, version)
            members.append(
                orchestration.Member(
                    id=snapshot.id,
                    name=(snapshot.name or snapshot.id)[:200],
                    description=snapshot.description[:2000],
                    capabilities=tuple(snapshot.capabilities),
                    contract=tuple(field.name for field in contracts.contract_of(snapshot)),
                    parameters=member_parameters(start_fields(snapshot)) if config.allow_values else (),
                )
            )
        if not members:
            if config.select is not None and config.select.capabilities:
                raise NodestroError(
                    "run.orchestrator_no_match",
                    f"No saved team of {label} has one of the capabilities it selects",
                    node=label,
                    capabilities=", ".join(config.select.capabilities),
                )
            raise NodestroError("workflow.orchestrator_no_members", f"{label} has no teams to choose from", node=label)
        return orchestration.Pool(members=tuple(members)), teams

    async def _orchestrate(
        self, context: _RunContext, node: Node, inputs: list[tuple[str, Any]], absent: set[str]
    ) -> None:
        """Let an AI model delegate the task to teams of its pool (``orkexa.orchestration``, ADR 0010/0011).

        The model only selects: it has no tools, creates nothing and has no permissions of its own. Every
        chosen team runs as a linked child run with its own budget, limits and tool approvals. With
        ``staffing`` it assembles its team from the pool while it runs, with ``max_parallel`` several teams
        work at once, with ``allow_values`` it fills the teams' start forms (checked like a manual start).

        A team that waits for a person (an approval step, also deeper in nested teams or orchestrators) pauses
        this node (``awaiting_approval``): the record keeps the pending decision with the team's run, and
        after the decision the same team run continues; the model is not asked again. A rejection that stops
        a team's run (``on_reject="stop"``) is recorded as ``rejected`` and stops this run, like a
        sub-workflow: no other team can get around it. Every step is stored in ``NodeRun.orchestration``.
        """
        run = context.run
        state = run.nodes[node.id]
        config = node.orchestrator
        assert config is not None
        label = node_label(node)
        prior = state.usage.model_copy() if state.status == "awaiting_approval" else None
        pool, teams = self._orchestration_pool(context, node)
        limits = orchestration.OrchestrationLimits(
            max_decisions=config.max_decisions,
            max_per_member=config.max_per_member,
            max_parallel=config.max_parallel,
            staffing=config.staffing,
            max_team=min(config.max_team, len(pool.members)),
        )
        recorded: orchestration.OrchestrationState | None = None
        if state.status == "awaiting_approval" and state.orchestration is not None:
            recorded = orchestration.OrchestrationState.model_validate(state.orchestration)
            task = recorded.task
        else:
            task = render_task(
                config.task_template,
                run_input=run.input,
                inputs=inputs,
                values=self._prompt_values(run),
                absent=absent,
                run_names=_start_names(run.workflow),
            )
            state.prompt = task
        state.status = "running"
        state.finished_at = None
        self._emit(context, "node_started", node.id, resumed=recorded is not None)
        self._save(context)
        stopped: list[Problem] = []

        def node_usage() -> Usage:
            current = self._node_usage(context, node.id)
            return add_usage(prior, current) if prior is not None else current

        async def decide(prompt: str) -> Any:
            return await self._in_run_thread(self._decide_sync, context, node, prompt)

        def start_child(item: orchestration.Delegation) -> Run:
            snapshot, version = teams[item.member.id]
            prepared = prepare(
                start_fields(snapshot),
                dict(item.values) if config.allow_values else {},
                item.task,
                [],
                file_names=None,
            )
            child = Run(
                workflow_id=snapshot.id,
                workflow_name=snapshot.name,
                workflow=self.pin_subworkflows(snapshot),
                input=prepared.input,
                values=prepared.values,
                file_ids=prepared.file_ids,
                nodes={entry.id: NodeRun(status="waiting") for entry in snapshot.nodes},
                trigger="subworkflow",
                trigger_ref=run.id,
                parent_run_id=run.id,
                parent_node_id=node.id,
                workflow_version=version,
            )
            self.store.put("runs", child.id, child)
            state.child_run_ids.append(child.id)
            self._emit(
                context,
                "orchestration_delegated",
                node.id,
                member=item.member.id,
                child_run_id=child.id,
                step=item.step,
                slot=item.slot,
            )
            self._save(context)
            return child

        async def execute(item: orchestration.Delegation) -> str:
            if item.ref is None:
                child = start_child(item)
            else:
                # Continue the team run that waited; never start it again.
                found = self.get(item.ref)
                if found is None or found.parent_run_id != run.id or item.ref not in state.child_run_ids:
                    raise orchestration.DelegationEnded("failed", "run.orchestration_team_missing")
                child = found
                self._emit(context, "orchestration_resumed", node.id, member=item.member.id, child_run_id=child.id)
                if child.status != "paused":
                    return self._team_result(child, item.member, context)
            child_context = await self._execute_child(context, node, child)
            state.usage = node_usage()
            if child.status == "cancelled" and child_context.stop is not None and not context.cancelled.is_set():
                stopped.append(child_context.stop)
            return self._team_result(child, item.member, context)

        def on_step(current: orchestration.OrchestrationState) -> None:
            state.orchestration = current.model_dump(mode="json")
            if current.paused:
                assert current.pending is not None
                self._emit(context, "orchestration_waiting", node.id, pending=current.pending.model_dump(mode="json"))
            else:
                self._emit(context, "orchestration_step", node.id, step=current.steps[-1].model_dump(mode="json"))
            self._save(context)
            if stopped:
                raise _TeamStopped(stopped[0])

        try:
            result = await orchestration.arun_orchestration(
                task,
                pool,
                decide=decide,
                execute=execute,
                limits=limits,
                state=recorded,
                on_step=on_step,
                stop_on=(RunCancelled,),
            )
            if result.paused:
                state.status = "awaiting_approval"
            elif result.outcome == "finished":
                state.status = "succeeded"
                state.output = result.answer
            else:
                state.status = "failed"
                _fail(
                    state,
                    problem(
                        "run.orchestration_limit",
                        f"{label} made {config.max_decisions} decisions without finishing",
                        node=label,
                        max_decisions=config.max_decisions,
                    ),
                )
        except _TeamStopped as stop:
            # A team's rejection ended its run: this run ends too (like a sub-workflow), nobody gets around it.
            state.status = "cancelled"
            _fail(state, problem("run.node_cancelled", "Cancelled"))
            if not context.cancelled.is_set():
                context.stop = stop.problem
                context.cancelled.set()
        except RunCancelled:
            state.status = "cancelled"
            _fail(state, problem("run.node_cancelled", "Cancelled"))
        except Exception as exc:  # API errors, budget, invalid configuration
            state.status = "failed"
            _fail(state, node_problem(exc))
        finally:
            state.usage = node_usage()
            own = [record for record in context.tracker.records if record.agent == node.id]
            decisions = usage_of(summarize_usage(own), own, stale_requests=stale_requests(context.tracker, own))
            state.decision_usage = (
                add_usage(state.decision_usage, decisions)
                if prior is not None and state.decision_usage is not None
                else decisions
            )
            state.finished_at = None if state.status == "awaiting_approval" else now()
        if state.status == "awaiting_approval":
            self._emit(context, "approval_requested", node.id, status=state.status)
        else:
            self._emit(
                context,
                "node_finished",
                node.id,
                status=state.status,
                output=state.output,
                error=state.error,
                error_code=state.error_code,
                error_params=state.error_params,
                usage=state.usage.model_dump(mode="json"),
            )
        self._save(context)

    def _team_result(self, child: Run, member: orchestration.Member, context: _RunContext) -> str:
        """A team run's result for the orchestrator, or the exception that says how it ended without one."""
        status = child_outcome(child.status, child.error_code)
        if status == "succeeded":
            return as_text(child.output)
        if context.cancelled.is_set():
            raise RunCancelled
        if status == "waiting":
            raise orchestration.DelegationSuspended(child.id, f"{member.name} waits for an approval")
        if status == "failed":
            raise RuntimeError(f"{member.name} failed: {child.error or child.status}")
        raise orchestration.DelegationEnded(status, f"{member.name}: {child.error or status}", ref=child.id)

    def _decide_sync(self, context: _RunContext, node: Node, prompt: str) -> Any:
        """One decision of an orchestrator, in a worker thread: a tool-less agent with a structured answer."""
        config = node.orchestrator
        assert config is not None

        def on_request(_args: dict[str, Any]) -> None:
            context.check()
            context.tracker.check()

        agent = AgentConfig(
            name=node_label(node),
            model=config.model,
            system_prompt=orchestrator_prompt(config)
            + (f"\n\nInstructions:\n{config.instructions}" if config.instructions else ""),
            output=OutputConfig(mode="structured", name="Decision", fields=decision_fields(config)),
            effort="medium",
            max_tool_rounds=0,
        )
        outcome = execute_agent(
            context.env.builder,
            agent,
            prompt,
            connect=self.connect,
            tracker=context.tracker,
            name=node.id,
            hooks={"on_request": on_request},
            cancelled=context.cancelled.is_set,
            attended=context.root.run.trigger not in UNATTENDED_TRIGGERS,
        )
        return decision_of(outcome.output)

    def _run_agent_sync(
        self, context: _RunContext, node: Node, prompt: str, with_run_files: bool, resuming: bool = False
    ) -> None:
        """Runs in a worker thread: build the agent, ask (or refine), record the result.

        With agent checkpoints (M6b) a call that waits for a person stops the turn: the checkpoint is stored, the
        node waits (``awaiting_approval``) and the run pauses. ``resuming`` continues that turn from its checkpoint
        without asking the model again."""
        assert node.agent is not None
        config = node.agent
        state = context.run.nodes[node.id]
        node_id = node.id
        calls: dict[str, ToolCallRecord] = {}
        released: dict[str, ToolApprovalRequest] = {}
        gate = self.tool_gate
        # A judge refines whole answers, which a checkpoint cannot hold: such agents keep waiting in the thread.
        checkpointing = (
            gate is not None and gate.checkpoints and not (config.quality.enabled and config.quality.criteria)
        )
        iteration = list(state.iteration)
        prior = state.usage.model_copy() if resuming else None
        checkpoint: tool_checkpoints.AgentCheckpoint | None = None
        if resuming:
            checkpoint = tool_checkpoints.load(self.store, context.run.id, node_id, iteration)
            if checkpoint is None or gate is None or not checkpointing:
                self._checkpoint_lost(context, node, state, prior)
                return

        def on_text(delta: str) -> None:
            context.check()
            state.text += delta
            self._emit(context, "text", node_id, delta=delta)

        def on_thinking(delta: str) -> None:
            context.check()
            state.thinking += delta
            self._emit(context, "thinking", node_id, delta=delta)

        def on_request(_args: dict[str, Any]) -> None:
            context.check()
            context.tracker.check()

        def before_tool(call: ToolCall) -> str | None:
            context.check()
            # A resumed call keeps the record it got when it was asked about.
            record = next((item for item in state.tool_calls if item.id == call.id), None) if call.resumed else None
            if record is None:
                record = ToolCallRecord(id=call.id, name=call.name, input=call.input)
                mcp = context.env.builder.tools.mcp
                if mcp is not None and is_mcp_tool(call.name):
                    record.provenance = mcp.provenance(call.name)
                with context.lock:
                    state.tool_calls.append(record)
            calls[call.id] = record
            denial = None
            bind_call(None)  # every call gets its own binding (or none) from the gate below
            own = run_own_tool(config, call.name, extra_tools)
            if gate is not None and not own:
                if checkpointing and call.resumed:
                    denial = self._gate_resumed_call(context, node_id, config, call, record, gate, iteration, released)
                else:
                    denial = self._gate_call(context, node_id, config, call, record, gate, suspend=checkpointing)
            elif call.resumed:
                pass  # an own tool never waits, so it is never resumed
            if denial is None and not own:
                # Stored before the call runs: if the process dies now, the run still shows that this node may
                # have had effects outside Nodestro, and continuing it asks first (audit B-02).
                tool_journal.record(self.store, context.run.id, node_id, iteration, call.id, call.name, call.input)
            self._emit(context, "tool_started", node_id, id=call.id, name=call.name, input=call.input)
            return denial

        def after_tool(call: ToolCall, content: str, is_error: bool) -> None:
            request = released.pop(call.id, None)
            if request is not None and gate is not None:
                self._audit_released(context, gate, call, request, is_error)
            record = calls.get(call.id)
            if record is not None:
                record.output = content[:20_000]
                record.is_error = is_error
                record.finished_at = now()
            self._emit(
                context, "tool_finished", node_id, id=call.id, name=call.name, output=content[:4000], is_error=is_error
            )

        def on_response(_response: Any) -> None:
            state.usage = self._node_usage(context, node_id)
            self._emit(context, "usage", node_id, usage=state.usage.model_dump(mode="json"))

        hooks: dict[str, Any] = {
            "on_text": on_text,
            "on_request": on_request,
            "before_tool": before_tool,
            "after_tool": after_tool,
            "on_response": on_response,
        }
        if config.thinking_display in ("summarized", "updates"):
            hooks["on_thinking"] = on_thinking

        extra: list[ContentBlock] = []
        extra_tools: list[Tool] = []
        if with_run_files and context.run.file_ids:
            run = context.run
            files = collect(
                run.file_ids, start_fields(run.workflow), run.values, self.inputs, context.env.builder.knowledge
            )
            capabilities = context.env.builder.capabilities(config.model)
            delivery = deliver(files, capabilities, self.inputs, context.env.builder.knowledge)
            extra = delivery.blocks
            state.files = delivery.provided
            if delivery.tool is not None:
                extra_tools.append(delivery.tool)
            self._emit(context, "files_provided", node_id, files=[item.model_dump() for item in state.files])
        if config.knowledge_proposals.enabled:
            extra_tools.append(
                _proposal_tool(state, config, lambda: self._emit(context, "knowledge_proposed", node_id))
            )
        suspended = False
        # Cancelling the run also stops a sandboxed call that is running (audit B-03, :mod:`.sandbox_cancel`).
        cancel_token = sandbox_cancel.bind(context.cancelled.is_set)
        try:
            outcome = execute_agent(
                context.env.builder,
                config,
                prompt,
                connect=self.connect,
                tracker=context.tracker,
                name=node_id,
                hooks=hooks,
                extra_context=extra,
                on_evaluation_start=lambda: self._emit(context, "evaluation_started", node_id),
                sandbox=context.env.tool_sandbox(context.run.workflow_id),
                extra_tools=extra_tools,
                cancelled=context.cancelled.is_set,
                attended=context.root.run.trigger not in UNATTENDED_TRIGGERS,
                run_memory=(
                    (lambda: EphemeralMemory(checkpoint.memory or {})) if checkpoint is not None else EphemeralMemory
                ),
                resume=checkpoint.turn if checkpoint is not None else None,
                # before_tool asks the tool gate for every call except the run's own tools (S-2).
                gated=gate is not None,
            )
            state.output = outcome.output
            state.memory = outcome.memory
            if config.knowledge_proposals.enabled and outcome.output not in (None, ""):
                state.proposals.append(
                    KnowledgeProposal(
                        title=node_label(node),
                        content=_proposal_text(outcome.output),
                        content_kind=config.knowledge_proposals.content_kind,
                        collection=config.knowledge_proposals.collection,
                    )
                )
            if outcome.evaluation is not None:
                state.evaluation = outcome.evaluation
                self._emit(context, "evaluation", node_id, **outcome.evaluation.model_dump())
            state.status = "succeeded"
        except AgentSuspended as stop:
            suspended = True
            assert gate is not None
            self._suspend_agent(context, node_id, state, iteration, stop, gate)
        except RunCancelled:
            state.status = "cancelled"
            _fail(state, problem("run.node_cancelled", "Cancelled"))
        except CheckpointError as exc:
            # The agent changed since the turn stopped (prompt, tools, model): the waiting calls never run.
            state.status = "failed"
            _fail(state, problem("tool.checkpoint_stale", f"The agent changed while it waited: {exc}"))
        except Exception as exc:  # API errors, budget, invalid configuration
            state.status = "failed"
            _fail(state, node_problem(exc))
        finally:
            sandbox_cancel.reset(cancel_token)
            if checkpointing and not suspended:
                # The turn ended (or failed): what it waited for is over, nothing of it may run later.
                if gate is not None and (resuming or checkpoint is not None):
                    self._close_node_waits(context.run.id, node_id, iteration, gate.approvals, because="turn_ended")
                tool_checkpoints.delete(self.store, context.run.id, node_id, iteration)
                for item in state.tool_calls:
                    if item.approval is not None and item.approval.status == "pending":
                        code = state.error_code or "run.node_cancelled"
                        item.approval = item.approval.model_copy(update={"status": "cancelled", "code": code})
            state.usage = self._node_usage(context, node_id)
            if prior is not None:
                state.usage = add_usage(prior, state.usage)
            state.finished_at = None if suspended else now()
            self._emit(
                context,
                "node_finished",
                node_id,
                status=state.status,
                output=state.output,
                error=state.error,
                error_code=state.error_code,
                error_params=state.error_params,
                usage=state.usage.model_dump(mode="json"),
                evaluation=state.evaluation.model_dump() if state.evaluation else None,
            )

    @staticmethod
    def _node_usage(context: _RunContext, node_id: str) -> Usage:
        names = {node_id, f"{node_id}:judge"}
        prefix = f"{node_id}/"
        records: list[UsageRecord] = [
            record for record in context.tracker.records if record.agent in names or record.agent.startswith(prefix)
        ]
        return usage_of(summarize_usage(records), records, stale_requests=stale_requests(context.tracker, records))


def _proposal_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    from .reports import to_markdown

    return to_markdown(value)


def _proposal_tool(state: NodeRun, config: AgentConfig, notify: Callable[[], None]) -> Tool:
    """``propose_knowledge``: the agent proposes content; a person decides later whether it is stored."""

    def propose_knowledge(
        title: str, content: str, content_kind: Literal["extracted", "generated", "assumption"] = "generated"
    ) -> str:
        """Propose content for the knowledge base. Nothing is stored now: a person reviews the proposal later.

        Propose only content worth keeping beyond this run, e.g. facts extracted from the input files or
        a summary. Mark what it is: extracted (taken from the inputs), generated (your own conclusions)
        or assumption (not verified).

        Args:
            title: A short, descriptive title.
            content: The content in Markdown.
            content_kind: extracted, generated or assumption.

        Returns:
            A confirmation that the proposal was recorded.
        """
        if len(state.proposals) >= 20:
            return "Not recorded: at most 20 proposals per step."
        state.proposals.append(
            KnowledgeProposal(
                title=title.strip()[:200] or "Proposal",
                content=content.strip()[:200_000],
                content_kind=content_kind,
                origin="tool",
                collection=config.knowledge_proposals.collection,
            )
        )
        notify()
        return "Recorded. A person will review the proposal; it is not stored in knowledge yet."

    return Tool(propose_knowledge, name="propose_knowledge")


@dataclass
class AgentOutcome:
    """What one agent produced: its answer, the judge's verdict and its run memory."""

    output: Any
    evaluation: EvaluationRecord | None = None
    memory: dict[str, str] | None = None


def execute_agent(
    builder: AgentBuilder,
    config: AgentConfig,
    prompt: str,
    *,
    connect: Callable[[str], Connection],
    tracker: BudgetTracker,
    name: str,
    hooks: dict[str, Any],
    extra_context: list[ContentBlock] | None = None,
    run_memory: Callable[[], MemoryBackend] = EphemeralMemory,
    on_evaluation_start: Callable[[], None] | None = None,
    sandbox: Sandbox | None = None,
    extra_tools: list[Tool] | None = None,
    cancelled: Callable[[], bool] | None = None,
    attended: bool = True,
    resume: Mapping[str, Any] | None = None,
    gated: bool = False,
) -> AgentOutcome:
    """Build the agent and ask it once, or let a judge check and refine the answer.

    ``resume`` continues a turn from its orkexa checkpoint instead of asking (M6b); a turn that stops at a waiting
    call raises :class:`AgentSuspended` with the checkpoint and the run memory.

    Used by workflow runs and by the agent test in the inspector, so both behave the same.
    Agent and judge each use the connection of their own provider. ``gated`` must be true only when the
    ``before_tool`` hook applies the shared per-call approval rules to every call (MCP tools that change data need it).
    """
    built = builder.build(
        config,
        connection=connect(provider_of(config.model)),
        name=name,
        usage_tracker=tracker,
        run_memory=run_memory,
        hooks=hooks,
        extra_context=extra_context,
        sandbox=sandbox,
        extra_tools=extra_tools,
        cancelled=cancelled,
        attended=attended,
        gated=gated,
    )
    agent = built.agent
    evaluation: EvaluationRecord | None = None
    if config.quality.enabled and config.quality.criteria:
        criteria = [
            Criterion(name=c.name, description=c.description, required=c.required) for c in config.quality.criteria
        ]
        judge = check_model(config.quality.judge_model)
        judge_connection = connect(provider_of(judge))
        evaluator = Evaluator(
            criteria,
            model=model_id(judge),
            options=judge_connection.options(),
            usage_tracker=tracker,
            name=f"{name}:judge",
            **judge_connection.agent_kwargs(),
        )
        if "on_request" in hooks:
            evaluator.agent.on_request = hooks["on_request"]
        if on_evaluation_start is not None:
            on_evaluation_start()
        refinement = refine(agent, prompt, evaluator, max_revisions=config.quality.max_revisions)
        result = refinement.output
        evaluation = EvaluationRecord(
            passed=refinement.passed,
            attempts=refinement.attempts,
            feedback=refinement.evaluation.feedback(),
        )
    else:
        try:
            result = agent.resume(resume) if resume is not None else agent.ask(prompt)
        except TurnSuspended as stop:
            files = built.memory.files() if isinstance(built.memory, EphemeralMemory) else None
            raise AgentSuspended(stop.checkpoint, files) from None
    memory = built.memory.files() if isinstance(built.memory, EphemeralMemory) else None
    return AgentOutcome(output=_jsonable(result), evaluation=evaluation, memory=memory)


class AgentSuspended(Exception):
    """An agent's turn stopped at a tool call that waits (M6b): its checkpoint and its run memory."""

    def __init__(self, checkpoint: Any, memory: dict[str, str] | None) -> None:
        super().__init__("the agent waits for a tool approval")
        self.checkpoint = checkpoint
        self.memory = memory


def _decision_code(final: ToolApprovalRequest) -> str | None:
    """The problem code of a decided or closed request on its call (``None`` when approved or cancelled)."""
    if final.status == "expired" and final.closed_because == RECLASSIFIED:
        return "tool.approval_stale"
    return {"rejected": "tool.approval_rejected", "expired": "tool.approval_timeout"}.get(final.status)


def _current_hash(tools: Any, call: ToolCall) -> str:
    """The arguments hash of a call against the tool as it is now (version or MCP definition)."""
    _capability, version, _kind, definition = tool_facts(tools, call.name)
    return arguments_hash(call.name, call.input, version=version, definition_hash=definition)


def node_problem(exc: BaseException) -> Problem:
    """What went wrong in a node, as a code for the UI and the technical text.

    The text is stored with the run and streamed to clients, so anything that looks like a
    credential (keys, tokens, passwords in URLs) is removed from it first.
    """
    if isinstance(exc, NodestroError):
        return exc.problem
    kind = type(exc).__name__
    text = redact(f"{kind}: {exc}" if str(exc) else kind)
    priced = price_problem(exc, text)
    if priced is not None:
        return priced
    if isinstance(exc, BudgetExceededError):
        return problem("run.budget_exceeded", text)
    if isinstance(exc, ProviderError):
        return problem("run.api_error", text, status=exc.status_code or "", provider=exc.provider or "")
    if isinstance(exc, OrkexaError):
        return problem(f"run.agent.{kind}", text, type=kind)
    found = library_problem(exc, code="run.failed")
    return found.model_copy(update={"message": text})


def _fail(target: NodeRun | Run, found: Problem) -> None:
    """Record a problem on a node or run: the technical text plus its code for the UI."""
    target.error = found.message
    target.error_code = found.code
    target.error_params = found.params


def _is_paused(run: Run) -> bool:
    return run.status == "paused"


def _child_ids(run: Run) -> list[str]:
    """The sub-workflow runs of a run's nodes."""
    return [child for state in run.nodes.values() for child in [state.child_run_id, *state.child_run_ids] if child]


def _interrupt(run: Run) -> None:
    """A run whose execution stopped with its process: failed with ``run.interrupted``, continuable."""
    for state in run.nodes.values():
        if state.status == "running":
            state.status = "failed"
            state.finished_at = now()
            _fail(state, problem("run.interrupted", "Nodestro stopped while this ran"))
    run.status = "failed"
    run.finished_at = now()
    _fail(run, problem("run.interrupted", "Nodestro stopped while this ran"))


def _abandon(run: Run) -> None:
    """A run whose task was stopped from outside: it and its unfinished nodes end as cancelled."""
    for state in run.nodes.values():
        if state.status == "running":
            state.status = "cancelled"
            state.finished_at = now()
            _fail(state, problem("run.node_cancelled", "Cancelled"))
    run.status = "cancelled"
    _fail(run, problem("run.cancelled", "The run was cancelled"))


def _remaining(budget: float | None, run: Run) -> float | None:
    """What is left of ``budget`` for a run that may already have spent part of it (a continued run)."""
    return None if budget is None else max(budget - _known_cost(run), 0.0)


def _known_cost(run: Run) -> float:
    """What a run already spent, as far as it is known: the run's cost, or the sum of its nodes' known costs.

    Used for the remaining budget of a continued run; an unknown total never counts as zero when
    parts of it are known.
    """
    if run.usage.estimated_cost_usd is not None:
        return run.usage.estimated_cost_usd
    return sum(state.usage.estimated_cost_usd or 0.0 for state in run.nodes.values())


def _require_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        raise NodestroError(
            "run.no_event_loop",
            "Runs execute on an asyncio event loop: call this from async code (e.g. inside asyncio.run())",
        ) from None


def _in_loop(loop: asyncio.AbstractEventLoop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False
