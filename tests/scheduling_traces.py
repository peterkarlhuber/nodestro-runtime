"""Scheduling scenarios and their traces: the reference for the engine's flow semantics.

``tests/data/scheduling_golden.json`` holds the traces the engine produced *before* it
was moved onto the ``orkexa.flow`` kernel (recorded with the old scheduler at
nodestro-runtime 82abd33). ``tests/test_scheduling_golden.py`` runs every scenario
again and compares: node statuses, error codes, outputs, rendered prompts, decisions,
the events of every node in order, and the run's status, error and output.

Regenerate only on purpose (a deliberate semantic change, documented in the CHANGELOG):
``NODESTRO_WRITE_GOLDEN=1 pytest tests/test_scheduling_golden.py``.

Agents are scripted: ``engine.execute_agent`` is replaced, so the rest of the engine
(prompts, events, usage, decisions, approvals, sub-workflows) runs unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import random
from typing import Any

from nodestro_runtime import engine
from nodestro_runtime.engine import AgentOutcome, RunCancelled, check_workflow
from nodestro_runtime.schemas import RunEvent, RunIn, Workflow, WorkflowIn
from nodestro_runtime.workspace import Workspace

GOLDEN = Path(__file__).parent / "data" / "scheduling_golden.json"
#: Events whose order per node is part of the contract (streamed text and usage are not).
TRACED = {
    "node_started",
    "node_finished",
    "approval_requested",
    "approval_decided",
    "run_started",
    "run_paused",
    "run_finished",
    "run_cancelling",
}
WORDS = ("red", "blue", "green")


def agent(node_id: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "agent",
        "label": node_id.upper(),
        "agent": {"name": node_id.upper(), "system_prompt": f"AGENT {node_id}", "task_template": "{{input}}"},
    }


def condition(node_id: str, *, mode: str = "first", on_invalid: str = "default") -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "condition",
        "label": node_id.upper(),
        "condition": {
            "mode": mode,
            "on_invalid": on_invalid,
            "rules": [
                {"id": "r", "field": "", "operator": "contains", "value": "red"},
                {"id": "b", "field": "", "operator": "contains", "value": "blue"},
            ],
        },
    }


def approval(node_id: str, *, on_reject: str = "branch") -> dict[str, Any]:
    return {"id": node_id, "type": "approval", "label": node_id.upper(), "approval": {"on_reject": on_reject}}


def edge(source: str, target: str, handle: str | None = None) -> dict[str, Any]:
    return {"id": f"{source}-{target}-{handle or ''}", "source": source, "target": target, "source_handle": handle}


def scenario(
    name: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    agents: dict[str, str] | None = None,
    decisions: list[list[str]] | None = None,
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """``agents``: per agent node ``ok`` (default), ``fail``, ``cancelled``, ``cancel_run`` or a word.

    ``decisions``: ``[node, decision, comment]`` in the order a person makes them.
    ``children``: workflows saved first (sub-workflows refer to them by ``id``).
    """
    return {
        "name": name,
        "nodes": nodes,
        "edges": edges,
        "agents": agents or {},
        "decisions": decisions or [],
        "children": children or [],
    }


def child_flow(child_id: str, *, with_approval: bool = False, fail: bool = False) -> dict[str, Any]:
    nodes = [{"id": "s", "type": "start"}, agent(f"{child_id}_work")]
    edges = [edge("s", f"{child_id}_work")]
    if with_approval:
        nodes.append(approval(f"{child_id}_ok"))
        edges.append(edge(f"{child_id}_work", f"{child_id}_ok"))
        nodes.append(agent(f"{child_id}_after"))
        edges.append(edge(f"{child_id}_ok", f"{child_id}_after", "approved"))
    return {
        "id": child_id,
        "nodes": nodes,
        "edges": edges,
        "agents": {f"{child_id}_work": "fail" if fail else "ok"},
    }


def subflow(node_id: str, child_id: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "subworkflow",
        "label": node_id.upper(),
        "subworkflow": {"workflow_id": child_id},
    }


START = {"id": "start", "type": "start"}

HANDWRITTEN: list[dict[str, Any]] = [
    scenario("linear", [START, agent("a"), agent("b")], [edge("start", "a"), edge("a", "b")]),
    scenario(
        "parallel-join",
        [START, agent("a"), agent("b"), agent("j"), {"id": "out", "type": "output", "label": "OUT"}],
        [edge("start", "a"), edge("start", "b"), edge("a", "j"), edge("b", "j"), edge("j", "out")],
    ),
    scenario(
        "failure-blocks",
        [START, agent("a"), agent("b"), agent("j"), agent("free")],
        [edge("start", "a"), edge("start", "b"), edge("a", "j"), edge("b", "j"), edge("start", "free")],
        agents={"a": "fail"},
    ),
    scenario(
        "agent-cancelled-blocks",
        [START, agent("a"), agent("b")],
        [edge("start", "a"), edge("a", "b")],
        agents={"a": "cancelled"},
    ),
    scenario(
        "run-cancelled-while-running",
        [START, agent("a"), agent("b"), agent("c")],
        [edge("start", "a"), edge("a", "b"), edge("start", "c")],
        agents={"a": "cancel_run"},
    ),
    scenario(
        "decision-branches-and-merge",
        [START, agent("w"), condition("d"), agent("x"), agent("y"), agent("z"), agent("m")],
        [
            edge("start", "w"),
            edge("w", "d"),
            edge("d", "x", "r"),
            edge("d", "y", "b"),
            edge("d", "z", "default"),
            edge("x", "m"),
            edge("y", "m"),
            edge("z", "m"),
        ],
        agents={"w": "blue"},
    ),
    scenario(
        "decision-all-mode",
        [START, agent("w"), condition("d", mode="all"), agent("x"), agent("y")],
        [edge("start", "w"), edge("w", "d"), edge("d", "x", "r"), edge("d", "y", "b")],
        agents={"w": "red blue"},
    ),
    scenario(
        "decision-invalid-fails",
        [START, agent("w"), condition("d", on_invalid="fail"), agent("x")],
        [edge("start", "w"), edge("w", "d"), edge("d", "x", "default")],
        agents={"w": "fail"},
    ),
    scenario(
        "approval-approved",
        [START, agent("a"), approval("ok"), agent("yes"), agent("no")],
        [edge("start", "a"), edge("a", "ok"), edge("ok", "yes", "approved"), edge("ok", "no", "rejected")],
        decisions=[["ok", "approved", "fine"]],
    ),
    scenario(
        "approval-rejected-branch",
        [START, agent("a"), approval("ok"), agent("yes"), agent("no")],
        [edge("start", "a"), edge("a", "ok"), edge("ok", "yes", "approved"), edge("ok", "no", "rejected")],
        decisions=[["ok", "rejected", "redo"]],
    ),
    scenario(
        "approval-rejected-stop",
        [START, agent("a"), approval("ok", on_reject="stop"), agent("yes"), agent("par")],
        [edge("start", "a"), edge("a", "ok"), edge("ok", "yes", "approved"), edge("start", "par")],
        decisions=[["ok", "rejected", ""]],
    ),
    scenario(
        "approval-stays-pending",
        [START, agent("a"), approval("ok"), agent("yes"), agent("free")],
        [edge("start", "a"), edge("a", "ok"), edge("ok", "yes", "approved"), edge("start", "free")],
    ),
    scenario(
        "two-approvals",
        [START, approval("p"), approval("q"), agent("pa"), agent("qa"), agent("both")],
        [
            edge("start", "p"),
            edge("start", "q"),
            edge("p", "pa", "approved"),
            edge("q", "qa", "approved"),
            edge("pa", "both"),
            edge("qa", "both"),
        ],
        decisions=[["q", "approved", ""], ["p", "approved", "first"]],
    ),
    scenario(
        "blocked-behind-failure-and-pause",
        [START, agent("bad"), approval("p"), agent("j")],
        [edge("start", "bad"), edge("start", "p"), edge("bad", "j"), edge("p", "j", "approved")],
        agents={"bad": "fail"},
        decisions=[["p", "approved", ""]],
    ),
    scenario(
        "subworkflow-succeeds",
        [START, subflow("call", "child_ok"), agent("after")],
        [edge("start", "call"), edge("call", "after")],
        children=[child_flow("child_ok")],
    ),
    scenario(
        "subworkflow-fails",
        [START, subflow("call", "child_bad"), agent("after")],
        [edge("start", "call"), edge("call", "after")],
        children=[child_flow("child_bad", fail=True)],
    ),
    scenario(
        "subworkflow-pauses",
        [START, subflow("call", "child_ask"), agent("after")],
        [edge("start", "call"), edge("call", "after")],
        children=[child_flow("child_ask", with_approval=True)],
        decisions=[["child:child_ask_ok", "approved", ""]],
    ),
]


def random_scenario(seed: int) -> dict[str, Any] | None:
    rng = random.Random(seed)
    size = rng.randint(2, 9)
    nodes: list[dict[str, Any]] = [START]
    for index in range(size):
        roll = rng.random()
        name = f"n{index}"
        if roll < 0.2:
            nodes.append(condition(name, mode=rng.choice(["first", "all"]), on_invalid=rng.choice(["default", "fail"])))
        elif roll < 0.32:
            nodes.append(approval(name, on_reject=rng.choice(["branch", "stop"])))
        else:
            nodes.append(agent(name))
    ports = {"condition": ["r", "b", "default"], "approval": ["approved", "rejected"]}
    edges: list[dict[str, Any]] = []
    for target in range(1, len(nodes)):
        sources = rng.sample(range(target), k=min(target, rng.randint(1, 2)))
        for source in sources:
            handle = rng.choice(ports[nodes[source]["type"]]) if nodes[source]["type"] in ports else None
            edges.append(edge(nodes[source]["id"], nodes[target]["id"], handle))
    agents = {
        node["id"]: rng.choice(["ok", "fail", *WORDS, *WORDS, "red blue"]) for node in nodes if node["type"] == "agent"
    }
    decisions = [
        [node["id"], rng.choice(["approved", "approved", "rejected"]), rng.choice(["", "note"])]
        for node in nodes
        if node["type"] == "approval"
    ]
    found = scenario(f"random-{seed}", nodes, edges, agents=agents, decisions=decisions)
    if check_workflow(Workflow.model_validate({"name": "x", "nodes": nodes, "edges": edges})):
        return None
    return found


def scenarios() -> list[dict[str, Any]]:
    found = list(HANDWRITTEN)
    seed = 0
    while len(found) < len(HANDWRITTEN) + 60:
        candidate = random_scenario(seed)
        seed += 1
        if candidate is not None:
            found.append(candidate)
    return found


class Scripted:
    """Replaces ``engine.execute_agent``: answers from the scenario, by node id."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.outcomes: dict[str, str] = {}
        self.run_ids: list[str] = []

    def __call__(self, builder: Any, config: Any, prompt: str, *, name: str, **_: Any) -> AgentOutcome:
        outcome = self.outcomes.get(name, "ok")
        if outcome == "fail":
            raise RuntimeError(f"{name} failed")
        if outcome == "cancelled":
            raise RunCancelled("stopped")
        if outcome == "cancel_run":
            self.workspace.runs.cancel(self.run_ids[0])
            raise RunCancelled("stopped")
        return AgentOutcome(output=f"{name}: {outcome if outcome != 'ok' else 'done'}")


def _node_trace(state: Any) -> dict[str, Any]:
    return {
        "status": state.status,
        "error_code": state.error_code,
        "output": state.output,
        "prompt": state.prompt,
        "branches": state.decision.branches if state.decision else None,
        "approval": state.approval.decision if state.approval else None,
    }


async def play(workspace: Workspace, scripted: Scripted, case: dict[str, Any]) -> dict[str, Any]:
    events: list[RunEvent] = []
    workspace.runs.event_sink = events.append
    children: dict[str, str] = {}
    for child in case["children"]:
        scripted.outcomes.update(child["agents"])
        saved = workspace.save_workflow(
            WorkflowIn.model_validate({"name": child["id"], "nodes": child["nodes"], "edges": child["edges"]})
        )
        children[child["id"]] = saved.id
    scripted.outcomes.update(case["agents"])
    nodes = []
    for node in case["nodes"]:
        if node["type"] == "subworkflow":
            node = {**node, "subworkflow": {"workflow_id": children[node["subworkflow"]["workflow_id"]]}}
        nodes.append(node)
    spec = {"name": case["name"], "nodes": nodes, "edges": case["edges"]}
    saved = workspace.save_workflow(WorkflowIn.model_validate(spec))
    started = await workspace.run(saved, RunIn(input="topic"))
    scripted.run_ids[:] = [started.id]
    run = await workspace.wait(started.id, timeout=20)
    for node_id, decision, comment in case["decisions"]:
        if run.status != "paused":
            break
        target = run
        if node_id.startswith("child:"):
            node_id = node_id.split(":", 1)[1]
            child_runs = [state.child_run_id for state in run.nodes.values() if state.child_run_id]
            child = workspace.runs.get(child_runs[0]) if child_runs else None
            if child is None:
                break
            target = child
        state = target.nodes.get(node_id)
        if state is None or state.status != "awaiting_approval":
            continue
        workspace.decide(target.id, node_id, decision, comment)
        await asyncio.sleep(0)
        run = await workspace.wait(started.id, timeout=20)
    per_node: dict[str, list[list[Any]]] = {}
    run_events: list[list[Any]] = []
    for event in events:
        if event.run_id != started.id or event.type not in TRACED:
            continue
        entry = [event.type, event.data.get("status")]
        if event.node_id is None:
            run_events.append(entry)
        else:
            per_node.setdefault(event.node_id, []).append(entry)
    return {
        "run": {"status": run.status, "error_code": run.error_code, "output": run.output},
        "nodes": {node_id: _node_trace(state) for node_id, state in sorted(run.nodes.items())},
        "node_events": dict(sorted(per_node.items())),
        "run_events": run_events,
    }


__all__ = ["GOLDEN", "Scripted", "engine", "play", "scenarios"]
