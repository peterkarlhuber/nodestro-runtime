"""The shape of a workflow: ports, branches, data sources and fingerprints.

Pure functions on :class:`~nodestro_runtime.schemas.Workflow` objects, shared by the engine,
the workflow check, the context preview and continued runs.

Rules of the data flow
----------------------

- Every node has one output port, except the branching nodes: a decision node has one
  port per rule plus ``default``; an approval node has ``approved`` and ``rejected``.
  A connection from a branching node names its port in ``Edge.source_handle``.
- A connection is *active* in a run when its source succeeded and, for a branching
  source, its port is among the opened branches.
- A node waits until all its predecessors are settled. It runs when at least one
  incoming connection is active, it is *skipped* (deliberately, not an error) when none
  is, and it is *blocked* when a predecessor failed. Merging branches therefore needs no
  setting: a node behind both branches of a decision runs with what arrives.
- A *loop* connection (``Edge.loop``) leaves a decision or approval node through a branch and
  leads back to an earlier node. Taking that branch runs the nodes from the loop's target to its
  source again, at most ``max_iterations`` times; nodes after the loop wait until it is over and
  see its last iteration. In later iterations the loop's target also receives what arrives over
  the loop connection (for example the reviewer's verdict behind a decision). All other functions
  here ignore loop connections: the rest of the graph stays acyclic.
- Decision and approval nodes pass data through: a node behind them receives the
  results that arrived at them, named after the agents that produced them. An
  approval's comment is passed on as well.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import hashlib
import json
from typing import Any, Literal

from .conditions import APPROVED, DEFAULT_BRANCH, REJECTED
from .schemas import Edge, Node, NodeRun, OrchestratorMember, SubworkflowConfig, Workflow, WorkflowIn


def workflow_refs(node: Node) -> list[SubworkflowConfig | OrchestratorMember]:
    """The references to other workflows in ``node`` (a sub-workflow, or an orchestrator's teams).

    Each has ``workflow_id`` and ``version``; changing ``version`` pins the reference.
    """
    if node.type == "subworkflow" and node.subworkflow is not None and node.subworkflow.workflow_id:
        return [node.subworkflow]
    if node.type == "orchestrator" and node.orchestrator is not None:
        return [member for member in node.orchestrator.members if member.workflow_id]
    return []


#: Node types that pass the data arriving at them on to their successors.
TRANSPARENT = frozenset({"condition", "approval"})
BRANCHING = frozenset({"condition", "approval"})
#: Node types that do work without being an agent.
WORKING = frozenset({"agent", "subworkflow", "orchestrator"})


def ports(node: Node) -> list[str]:
    """The output ports of a branching node (empty for nodes with one output)."""
    if node.type == "condition":
        rules = node.condition.rules if node.condition else []
        return [rule.id for rule in rules] + [DEFAULT_BRANCH]
    if node.type == "approval":
        return [APPROVED, REJECTED]
    return []


def port_label(node: Node, handle: str | None) -> str:
    if node.type == "condition" and node.condition is not None:
        if handle == DEFAULT_BRANCH:
            return node.condition.default_label or DEFAULT_BRANCH
        for rule in node.condition.rules:
            if rule.id == handle:
                return rule.label or rule.id
    return handle or ""


def predecessors(workflow: Workflow | WorkflowIn) -> dict[str, list[Edge]]:
    """Incoming connections per node (only connections between existing nodes, without loops)."""
    ids = {node.id for node in workflow.nodes}
    found: dict[str, list[Edge]] = {node.id: [] for node in workflow.nodes}
    for edge in workflow.edges:
        if edge.source in ids and edge.target in ids and edge.source != edge.target and edge.loop is None:
            found[edge.target].append(edge)
    return found


def successors(workflow: Workflow | WorkflowIn) -> dict[str, list[Edge]]:
    """Outgoing connections per node (only connections between existing nodes, without loops)."""
    ids = {node.id for node in workflow.nodes}
    found: dict[str, list[Edge]] = {node.id: [] for node in workflow.nodes}
    for edge in workflow.edges:
        if edge.source in ids and edge.target in ids and edge.source != edge.target and edge.loop is None:
            found[edge.source].append(edge)
    return found


def loop_edges(workflow: Workflow | WorkflowIn) -> list[Edge]:
    """The loop connections between existing nodes."""
    ids = {node.id for node in workflow.nodes}
    return [edge for edge in workflow.edges if edge.loop is not None and edge.source in ids and edge.target in ids]


def loop_bodies(workflow: Workflow | WorkflowIn) -> dict[str, set[str]]:
    """Per loop connection, the nodes it repeats: every node on a path from its target to its source."""
    incoming = predecessors(workflow)
    outgoing = successors(workflow)

    def reach(start: str, step: dict[str, list[Edge]], end: Callable[[Edge], str]) -> set[str]:
        seen = {start}
        todo = [start]
        while todo:
            for edge in step.get(todo.pop(), []):
                if end(edge) not in seen:
                    seen.add(end(edge))
                    todo.append(end(edge))
        return seen

    return {
        edge.id: reach(edge.target, outgoing, lambda item: item.target)
        & reach(edge.source, incoming, lambda item: item.source)
        for edge in loop_edges(workflow)
    }


def data_sources(workflow: Workflow | WorkflowIn, node_id: str) -> list[str]:
    """The nodes whose results reach ``node_id``, looking through decision and approval nodes."""
    nodes = {node.id: node for node in workflow.nodes}
    incoming = predecessors(workflow)
    found: list[str] = []
    seen: set[str] = set()

    def visit(target: str) -> None:
        for edge in incoming.get(target, []):
            source = edge.source
            if source in seen:
                continue
            seen.add(source)
            if nodes[source].type in TRANSPARENT:
                visit(source)
            else:
                found.append(source)

    visit(node_id)
    return found


def comment_sources(workflow: Workflow | WorkflowIn, node_id: str) -> list[str]:
    """The approval nodes on the way to ``node_id`` whose comment the node receives (by their name)."""
    nodes = {node.id: node for node in workflow.nodes}
    incoming = predecessors(workflow)
    found: list[str] = []
    seen: set[str] = set()

    def visit(target: str) -> None:
        for edge in incoming.get(target, []):
            source = edge.source
            if source in seen or nodes[source].type not in TRANSPARENT:
                continue
            seen.add(source)
            if nodes[source].type == "approval":
                found.append(source)
            visit(source)

    visit(node_id)
    return found


def ancestors(workflow: Workflow | WorkflowIn, node_id: str) -> set[str]:
    incoming = predecessors(workflow)
    seen: set[str] = set()
    todo = [node_id]
    while todo:
        for edge in incoming.get(todo.pop(), []):
            if edge.source not in seen:
                seen.add(edge.source)
                todo.append(edge.source)
    return seen


def opened_ports(node: Node, state: NodeRun) -> list[str] | None:
    """The opened ports of a settled branching node; ``None`` for nodes with one output."""
    if node.type == "condition":
        return state.decision.branches if state.decision else []
    if node.type == "approval":
        if state.approval is None:
            return []
        if state.approval.decision == "approved":
            return [APPROVED]
        on_reject = node.approval.on_reject if node.approval else "branch"
        return [REJECTED] if on_reject == "branch" else []
    return None


def edge_active(edge: Edge, nodes: dict[str, Node], states: dict[str, NodeRun]) -> bool:
    state = states.get(edge.source)
    if state is None or state.status != "succeeded":
        return False
    opened = opened_ports(nodes[edge.source], state)
    return opened is None or edge.source_handle in opened


# ---- fingerprints --------------------------------------------------------------------


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:24]


def fingerprints(
    workflow: Workflow,
    *,
    run_input: str,
    values: dict[str, Any],
    file_ids: list[str],
    tool_marker: Callable[[str], str],
    child_tools: Callable[[str, int | None], list[str]] = lambda _id, _version: [],
) -> dict[str, str]:
    """What each node's result depends on, as a hash per node.

    A node's fingerprint covers its configuration (not its position), how it is connected,
    the fingerprints of its predecessors, the versions of its tools and its files; the
    start node's also covers the run input. Equal fingerprints mean that running the node
    again would get the same task with the same means, so its result may be reused.
    """
    from .engine import topological_order

    incoming = predecessors(workflow)
    result: dict[str, str] = {}
    for node in topological_order(workflow):
        config = node.model_dump(mode="json", exclude={"position"})
        parts: dict[str, Any] = {"node": config}
        parts["inputs"] = sorted(
            (result.get(edge.source, ""), edge.source, edge.source_handle or "") for edge in incoming[node.id]
        )
        if node.type == "start":
            parts["run"] = {"input": run_input, "values": values, "files": sorted(file_ids)}
        if node.agent is not None:
            parts["tools"] = [tool_marker(name) for name in sorted(node.agent.tools)]
            # Agents fed by the start node also receive the run's files.
            parts["files"] = sorted(file_ids)
        if node.subworkflow is not None:
            parts["child"] = child_tools(node.subworkflow.workflow_id, node.subworkflow.version)
        if node.orchestrator is not None:
            parts["teams"] = [child_tools(member.workflow_id, member.version) for member in node.orchestrator.members]
        result[node.id] = _hash(parts)
    return result


def continuation(
    workflow: Workflow, earlier: dict[str, NodeRun], current_prints: dict[str, str]
) -> list[tuple[Node, Literal["reuse", "rerun", "run"], str | None]]:
    """How continuing a failed run treats each node, in execution order: ``(node, action, reason)``.

    ``reuse`` takes the earlier result over: the node succeeded (or was deliberately skipped),
    its fingerprint is unchanged, it is not part of a loop and all its predecessors are reused too.
    ``rerun`` runs a node again that ran before; ``run`` one that did not run (or did not finish). ``reason`` is a code
    for the UI. Used by :mod:`nodestro_runtime.resume` and by continued API runs (:mod:`nodestro_api.serving`).
    """
    from .engine import topological_order

    incoming = predecessors(workflow)
    looped = set().union(*loop_bodies(workflow).values()) if workflow.edges else set()
    reused: set[str] = set()
    result: list[tuple[Node, Literal["reuse", "rerun", "run"], str | None]] = []
    for node in topological_order(workflow):
        old = earlier.get(node.id)
        upstream_ok = all(edge.source in reused for edge in incoming[node.id])
        action: Literal["reuse", "rerun", "run"] = "run"
        reason: str | None = None
        if old is None:
            reason = "resume.new_node"
        elif node.id in looped:
            # A loop's iterations depend on each other: the whole loop runs again.
            action, reason = "rerun", "resume.in_loop"
        elif old.status not in ("succeeded", "skipped"):
            action = "rerun" if old.status in ("failed", "cancelled") else "run"
            # A step that stopped with its process says so, not "failed" (AD-10).
            reason = "resume.was_interrupted" if old.error_code == "run.interrupted" else f"resume.was_{old.status}"
        elif not old.fingerprint:
            action, reason = "rerun", "resume.no_fingerprint"
        elif old.fingerprint != current_prints.get(node.id):
            action = "rerun"
            reason = "resume.upstream_changed" if not upstream_ok else "resume.changed"
        elif not upstream_ok:
            action, reason = "rerun", "resume.upstream_changed"
        else:
            action = "reuse"
            reused.add(node.id)
        result.append((node, action, reason))
    return result


def unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))
