"""The engine's bridge to the orkexa.flow kernel (one flow semantics, no second scheduler)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from orkexa import flow
import pytest
from scheduling_traces import GOLDEN, scenarios

from nodestro_runtime.flow_driver import UNCONNECTED, flow_of, result_of, state_of
from nodestro_runtime.schemas import ApprovalRecord, DecisionRecord, NodeRun, Workflow

SOURCE = Path(__file__).resolve().parents[1] / "src" / "nodestro_runtime"


def workflow(nodes: list[dict[str, object]], edges: list[dict[str, object]]) -> Workflow:
    return Workflow.model_validate({"name": "x", "nodes": nodes, "edges": edges})


def test_branching_nodes_declare_their_ports_and_bad_handles_never_open() -> None:
    spec = flow_of(
        workflow(
            [
                {"id": "s", "type": "start"},
                {"id": "d", "type": "condition", "condition": {"rules": [{"id": "r1"}]}},
                {"id": "a", "type": "approval"},
                {"id": "t", "type": "output"},
            ],
            [
                {"id": "e", "source": "s", "target": "d", "source_handle": "ignored"},
                {"id": "e", "source": "d", "target": "a", "source_handle": "r1"},
                {"id": "x", "source": "d", "target": "t", "source_handle": "gone"},
                {"id": "y", "source": "a", "target": "t"},
            ],
        )
    )
    assert flow.validate(spec) == []
    nodes = {node.id: node for node in spec.nodes}
    assert nodes["d"].ports == ("r1", "default", UNCONNECTED)
    assert nodes["a"].ports == ("approved", "rejected", UNCONNECTED)
    assert nodes["s"].ports is None
    assert [(edge.id, edge.port) for edge in spec.edges] == [
        ("e", None),
        ("e#1", "r1"),
        ("x", UNCONNECTED),
        ("y", UNCONNECTED),
    ]


def test_node_states_map_to_invocations_and_results() -> None:
    spec = workflow(
        [
            {"id": "s", "type": "start"},
            {"id": "d", "type": "condition", "condition": {"rules": [{"id": "r1"}]}},
            {"id": "a", "type": "approval", "approval": {"on_reject": "stop"}},
            {"id": "w", "type": "output"},
            {"id": "f", "type": "output"},
        ],
        [],
    )
    nodes = {node.id: node for node in spec.nodes}
    states = {
        "s": NodeRun(status="succeeded"),
        "d": NodeRun(status="succeeded", decision=DecisionRecord(branches=["r1", "stale"])),
        "a": NodeRun(status="awaiting_approval"),
        "w": NodeRun(status="running"),
        "f": NodeRun(status="failed", error_code="run.failed"),
    }
    state = state_of(spec, states)
    assert {(item.id, item.status, item.ports) for item in state.invocations} == {
        ("s//1", "succeeded", None),
        ("d//1", "succeeded", ("r1", "stale")),
        ("a//1", "suspended", None),
        ("f//1", "failed", None),
    }
    assert result_of(nodes["d"], states["d"]) == flow.Result(status="succeeded", ports=("r1",))
    assert result_of(nodes["a"], states["a"]) == flow.Result(status="suspended")
    rejected = NodeRun(status="succeeded", approval=ApprovalRecord(decision="rejected"))
    assert result_of(nodes["a"], rejected) == flow.Result(status="succeeded", ports=())
    assert result_of(nodes["f"], states["f"]).status == "failed"
    assert result_of(nodes["w"], NodeRun(status="cancelled", error_code="run.node_cancelled")).status == "cancelled"
    assert result_of(nodes["w"], NodeRun(status="blocked")).error == "run.failed"


@pytest.mark.parametrize("case", scenarios(), ids=lambda case: case["name"])
def test_every_finished_golden_run_is_a_fixed_point_of_the_kernel(case: dict[str, object]) -> None:
    """Conformance: what the engine recorded is exactly what the kernel would settle."""
    trace = json.loads(GOLDEN.read_text(encoding="utf-8"))[case["name"]]
    spec = workflow(case["nodes"], case["edges"])  # type: ignore[arg-type]
    for node in spec.nodes:
        if node.type == "subworkflow":
            return  # the child's workflow id differs per recording
    states = {
        node_id: NodeRun(
            status=node["status"],
            error_code=node["error_code"],
            decision=DecisionRecord(branches=node["branches"]) if node["branches"] is not None else None,
            approval=ApprovalRecord(decision=node["approval"]) if node["approval"] else None,
        )
        for node_id, node in trace["nodes"].items()
    }
    state = state_of(spec, states)
    step = flow.advance(flow_of(spec), state)
    assert step.actions == ()  # nothing would run again
    if trace["run"]["status"] != "cancelled":  # a cancelled run's waiting nodes stay waiting
        assert step.state.invocations == state.invocations  # nothing left to settle


def test_the_engine_has_no_scheduler_of_its_own() -> None:
    """The kernel is the only scheduler: the engine neither awaits predecessors nor decides skips itself."""
    tree = ast.parse((SOURCE / "engine.py").read_text(encoding="utf-8"))
    functions = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_settle" not in functions
    calls = {
        f"{node.func.value.id}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
    }
    assert {"flow.advance", "flow.complete", "flow.cancel", "flow.resume"} <= calls
    assert "asyncio.gather" not in calls
    for path in SOURCE.glob("*.py"):
        if path.name in ("engine.py", "flow_driver.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "orkexa":
                assert "flow" not in {alias.name for alias in node.names}, path.name
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                assert not any(name.startswith("orkexa.flow") for name in modules), path.name
