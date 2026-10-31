"""The engine's bridge to the ``orkexa.flow`` kernel: workflows as flows, node states as flow state.

The kernel decides which node runs, waits, is skipped, blocked or cancelled; the engine
executes the nodes (:mod:`nodestro_runtime.engine`). There is no second scheduler: the
rules in :mod:`nodestro_runtime.graph` describe what the kernel does with a workflow.

The flow state is derived from the run's node states every time the engine picks a run
up (start, resume after an approval, resume after a restart), so stored runs of earlier
versions continue without a migration.
"""

from __future__ import annotations

from typing import Any

from orkexa import flow

from .graph import BRANCHING, loop_bodies, opened_ports, ports
from .schemas import Node, NodeRun, Workflow

__all__ = ["UNCONNECTED", "flow_of", "result_of", "state_of"]

#: The port of a connection that leaves a branching node without a valid port: it is never opened.
UNCONNECTED = "~unconnected"

_FINAL = {"succeeded", "failed", "skipped", "blocked", "cancelled"}


def flow_of(workflow: Workflow) -> flow.Flow:
    """The kernel's view of a workflow (call after :func:`~nodestro_runtime.engine.topological_order`).

    Every node keeps its id and type; branching nodes declare their ports. Connections
    without a valid port of a branching source get :data:`UNCONNECTED`, which no result
    opens, so they are never active (as before the kernel).
    """
    by_id = {node.id: node for node in workflow.nodes}
    nodes = [
        flow.Node(id=node.id, type=node.type, ports=(*ports(node), UNCONNECTED) if node.type in BRANCHING else None)
        for node in workflow.nodes
    ]
    edges: list[flow.Edge] = []
    seen: set[str] = set()
    for index, edge in enumerate(workflow.edges):
        source = by_id[edge.source]
        port: str | None = None
        if source.type in BRANCHING:
            port = edge.source_handle if edge.source_handle in ports(source) else UNCONNECTED
        ident = edge.id if edge.id and edge.id not in seen else f"{edge.id}#{index}"
        seen.add(ident)
        loop = (
            flow.Loop(max_iterations=edge.loop.max_iterations, on_exhausted=edge.loop.on_exhausted)
            if edge.loop is not None
            else None
        )
        edges.append(flow.Edge(id=ident, source=edge.source, target=edge.target, port=port, loop=loop))
    return flow.Flow(nodes=tuple(nodes), edges=tuple(edges))


def _invocation(node: Node, state: NodeRun) -> flow.Invocation | None:
    ident = f"{node.id}//1"
    if state.status in _FINAL:
        opened = opened_ports(node, state) if state.status == "succeeded" else None
        return flow.Invocation(
            id=ident,
            node=node.id,
            status=state.status,  # type: ignore[arg-type]
            ports=tuple(opened) if opened is not None else None,
            error=state.error_code,
            dispatches=1,
        )
    if state.status == "awaiting_approval":
        return flow.Invocation(id=ident, node=node.id, status="suspended", dispatches=1)
    # waiting, idle, or running (a run that stopped mid-node runs it again, as before)
    return None


def state_of(workflow: Workflow, states: dict[str, NodeRun], stored: dict[str, Any] | None = None) -> flow.FlowState:
    """The flow state of a run: the stored one, else derived from its node states.

    Runs of earlier versions (and continued runs, whose reused nodes are preset) have no stored
    state. Deriving is exact outside loops; inside loops the stored state is needed, and a
    continued run never reuses a node inside a loop (:mod:`nodestro_runtime.resume`).
    """
    if stored is not None:
        return flow.FlowState.model_validate(stored)
    looped = set().union(*loop_bodies(workflow).values()) if workflow.edges else set()
    found = []
    for node in workflow.nodes:
        if node.id in looped:
            continue  # without a stored state, a node inside a loop runs again
        state = states.get(node.id)
        item = _invocation(node, state) if state is not None else None
        if item is not None:
            found.append(item)
    return flow.FlowState(invocations=tuple(found), dispatched=len(found))


def result_of(node: Node, state: NodeRun) -> flow.Result:
    """What an executed node reports to the kernel."""
    if state.status == "succeeded":
        opened = opened_ports(node, state)
        if opened is None:
            return flow.Result(status="succeeded")
        known = set(ports(node))
        return flow.Result(status="succeeded", ports=tuple(port for port in opened if port in known))
    if state.status == "awaiting_approval":
        return flow.Result(status="suspended")
    if state.status == "cancelled":
        return flow.Result(status="cancelled", error=state.error_code)
    return flow.Result(status="failed", error=state.error_code or "run.failed")
