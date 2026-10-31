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
from dataclasses import dataclass, field
from decimal import Decimal
import json
import logging
import re
import threading
import time
from typing import Any, Literal, TypeVar

from orkexa import (
    ContentBlock,
    Criterion,
    EphemeralMemory,
    Evaluator,
    MemoryBackend,
    OrkexaError,
    ProviderError,
    Tool,
    ToolCall,
    UsageRecord,
    UsageSummary,
    flow,
    refine,
)
from orkexa.dynamic import BudgetExceededError, BudgetTracker, Sandbox
from orkexa.memory import MEMORY_TOOL_NAME
from orkexa.providers.base import redact
from orkexa.usage import summarize_usage
from pydantic import BaseModel

from . import flow_driver, graph, tool_policy
from .agent_builder import AgentBuilder
from .conditions import APPROVED, MISSING, REJECTED, decide, lookup, rule_problems
from .cost_tracking import PricedTracker, PriceMode, stale_requests
from .file_delivery import collect, deliver
from .forms import FileRef, field_problems, name_of, prepare, prompt_values
from .graph import BRANCHING, TRANSPARENT, WORKING, data_sources, edge_active, ports
from .input_files import InputFiles
from .mcp_access import UNATTENDED_TRIGGERS, is_mcp_tool
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
from .tool_approvals import INTERRUPTED, ToolApprovals, ToolGate

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

T = TypeVar("T")


class RunCancelled(Exception):
    """Raised inside agent hooks when the user cancels the run."""


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

    def check(self) -> None:
        if self.cancelled.is_set():
            raise RunCancelled("The run was cancelled")


def _no_workflow(workflow_id: str, _version: int | None) -> tuple[Workflow, int | None]:
    raise KeyError(workflow_id)


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
        if not any(node.type == "subworkflow" for node in workflow.nodes):
            return workflow
        pinned = workflow.model_copy(deep=True)
        for node in pinned.nodes:
            config = node.subworkflow
            if config is not None and config.workflow_id and config.version is None:
                with suppress(KeyError, NodestroError):
                    config.version = self.resolve_workflow(config.workflow_id, None)[1]
        return pinned

    def _launch(self, run: Run, *, local: bool = False) -> Run:
        if self.dispatch is not None and not local:
            # Queue execution: the run is stored and a worker executes it (ADR-0004). A paused run whose decision
            # was recorded is queued again, so everybody waiting for it sees that it goes on.
            run.status = "queued"
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
        self._contexts[run.id] = context
        self._events.setdefault(run.id, [])
        self._subscribers.setdefault(run.id, [])
        self.store.put("runs", run.id, run)
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
        run = self.store.get("runs", run_id, Run)
        if run is None:
            raise KeyError(run_id)
        if run.status not in ("queued", "paused"):
            raise NodestroError("run.not_executable", "Only a queued or paused run can be executed", status=run.status)
        return self._launch(run, local=True)

    def interrupt(self, run_id: str) -> bool:
        """Mark a run that stopped executing elsewhere (its worker died) as interrupted; it can be continued.

        Returns ``False`` for a run that is not queued or running (nothing to do)."""
        run = self.store.get("runs", run_id, Run)
        if run is None or run.status not in ("queued", "running") or run.id in self._contexts:
            return False
        _interrupt(run)
        self.store.put("runs", run.id, run)
        self._expire_approvals(run.id)
        self._notify(run)
        return True

    def cancel_unclaimed(self, run_id: str) -> bool:
        """Cancel a stored run that no worker executes (its queued job was withdrawn); ``False`` otherwise."""
        run = self.store.get("runs", run_id, Run)
        if run is None or run.status not in ("queued", "paused") or run.id in self._contexts:
            return False
        self._cancel_stored(run)
        return True

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
        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
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
        if self._contexts.get(self.root_of(run).id) is None:
            # The run continues on an event loop: refuse before anything is recorded.
            _require_loop()
        state.approval = ApprovalRecord(decision=decision, comment=text, decided_by=decided_by)  # type: ignore[arg-type]
        context = self._contexts.get(run_id)
        if context is not None:
            self._emit(context, "approval_decided", node_id, decision=decision, comment=text, decided_by=decided_by)
        else:
            self.store.put("runs", run.id, run)
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
        if stored.status == "paused":
            self._cancel_stored(stored)
            return True
        return False

    def _cancel_stored(self, run: Run) -> None:
        """Cancel a paused or queued run that is not executing (its pending approvals end undecided)."""
        for node in run.workflow.nodes:
            state = run.nodes.get(node.id)
            if state is None:
                continue
            if state.status == "awaiting_approval":
                state.status = "cancelled"
                state.finished_at = now()
                _fail(state, problem("run.node_cancelled", "Cancelled"))
                if state.child_run_id:
                    child = self.store.get("runs", state.child_run_id, Run)
                    if child is not None and child.status == "paused":
                        self._cancel_stored(child)
        run.status = "cancelled"
        run.finished_at = now()
        _fail(run, problem("run.cancelled", "The run was cancelled"))
        self.store.put("runs", run.id, run)
        self._forget_events(run.id)
        self._notify(run)

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
        for run in self.store.find("runs", "status", ("queued", "running"), Run):
            if run.imported_at is not None or run.id in self._contexts:
                continue
            _interrupt(run)
            self.store.put("runs", run.id, run)
            self._expire_approvals(run.id)
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
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)

    # ---- events --------------------------------------------------------------------

    def _emit(self, context: _RunContext, kind: str, node_id: str | None = None, **data: Any) -> None:
        """Record and publish an event; safe to call from worker threads."""
        if node_id is not None:
            state = context.run.nodes.get(node_id)
            if state is not None and state.iteration:
                data.setdefault("iteration", list(state.iteration))
        loop = context.root.loop or self._loop
        if loop is None:
            # No loop yet: called outside a run (e.g. cancelling a stored run); nobody listens.
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
        self.store.put("runs", snapshot.id, snapshot)

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
            state.status = "succeeded"
            state.output = inputs[0][1] if len(inputs) == 1 else {name: value for name, value in inputs}
            state.finished_at = now()
            self._emit(context, "node_finished", node.id, status="succeeded", output=state.output)
            return
        if node.type == "condition":
            self._decide(context, node, inputs)
            return
        if node.type == "subworkflow":
            await self._subworkflow(context, node, inputs, absent)
            return
        assert node.agent is not None
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
        await asyncio.to_thread(self._run_agent_sync, context, node, prompt, self._feeds_from_start(run, node.id))
        self._save(context)

    def _prompt_values(self, run: Run, *, mark_empty: bool = True) -> dict[str, Any]:
        """The run's form values as task placeholders show them (file names, empty fields marked)."""
        if not run.values:
            return run.values
        names = self.file_names() if self.file_names is not None else {}
        return prompt_values(start_fields(run.workflow), run.values, names, mark_empty=mark_empty)

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
        child.budget_usd = child.workflow.budget_usd
        child_context = _RunContext(
            child,
            _ChildTracker(context.tracker, node.id, _remaining(child.workflow.budget_usd, child)),
            context.env,
            cancelled=CancelFlag(context.cancelled),
            stack=(*context.stack, run.workflow_id),
            root=context.root,
        )
        self._contexts[child.id] = child_context
        self._events.setdefault(child.id, [])
        try:
            await self._execute_body(child_context)
        finally:
            self._contexts.pop(child.id, None)
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
    ) -> str | None:
        """Apply the per-call rules (S-2) to one call in the agent's thread: ``None`` runs it, a string denies it.

        Asking waits here for a stored decision (at most the deadline); the run stays ``running``. An approval only
        lets the call run as it would have without the question, in its sandbox (ADR-0006)."""
        try:
            info = context.env.builder.tools.get(call.name)
            capability, version, kind, definition = info.capability, info.version, info.kind, info.definition_hash
        except KeyError:
            capability, version, kind, definition = tool_policy.UNKNOWN, None, "custom", None
        root = context.root.run
        scope: tuple[tool_policy.PreGrantScope, str] | None = None
        if root.trigger in ("schedule", "batch", "benchmark") and root.trigger_ref:
            scope = (root.trigger, root.trigger_ref)
        situation = tool_policy.CallContext(
            run_id=context.run.id,
            node_id=node_id,
            trigger=root.trigger,
            attended=root.trigger not in UNATTENDED_TRIGGERS,
            scope=scope,
            pre_grants=tuple(gate.pre_grants(*scope)) if scope is not None else (),
            mcp_unattended=gate.mcp_unattended(),
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
            return None
        if verdict.action == "deny":
            return self._deny_call(
                context, node_id, call, record, verdict.code or "tool.denied_by_policy", verdict.reason or "", shown
            )
        run_id = context.run.id
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
        code = {"rejected": "tool.approval_rejected", "expired": "tool.approval_timeout"}.get(final.status)
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
            return None
        if final.status == "rejected":
            return "A person rejected this call" + (f": {final.comment}" if final.comment else "")
        if final.status == "expired":
            return "Nobody approved this call in time, so it was not made"
        context.check()
        raise RunCancelled("The run was cancelled")

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

    def _run_agent_sync(self, context: _RunContext, node: Node, prompt: str, with_run_files: bool) -> None:
        """Runs in a worker thread: build the agent, ask (or refine), record the result."""
        assert node.agent is not None
        config = node.agent
        state = context.run.nodes[node.id]
        node_id = node.id
        calls: dict[str, ToolCallRecord] = {}

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
            record = ToolCallRecord(id=call.id, name=call.name, input=call.input)
            mcp = context.env.builder.tools.mcp
            if mcp is not None and is_mcp_tool(call.name):
                record.provenance = mcp.provenance(call.name)
            calls[call.id] = record
            with context.lock:
                state.tool_calls.append(record)
            denial = None
            own = {tool.name for tool in extra_tools} | {MEMORY_TOOL_NAME}
            if self.tool_gate is not None and call.name not in own:
                denial = self._gate_call(context, node_id, config, call, record, self.tool_gate)
            self._emit(context, "tool_started", node_id, id=call.id, name=call.name, input=call.input)
            return denial

        def after_tool(call: ToolCall, content: str, is_error: bool) -> None:
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
        except RunCancelled:
            state.status = "cancelled"
            _fail(state, problem("run.node_cancelled", "Cancelled"))
        except Exception as exc:  # API errors, budget, invalid configuration
            state.status = "failed"
            _fail(state, node_problem(exc))
        finally:
            state.usage = self._node_usage(context, node_id)
            state.finished_at = now()
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
) -> AgentOutcome:
    """Build the agent and ask it once, or let a judge check and refine the answer.

    Used by workflow runs and by the agent test in the inspector, so both behave the same.
    Agent and judge each use the connection of their own provider.
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
        result = agent.ask(prompt)
    memory = built.memory.files() if isinstance(built.memory, EphemeralMemory) else None
    return AgentOutcome(output=_jsonable(result), evaluation=evaluation, memory=memory)


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
