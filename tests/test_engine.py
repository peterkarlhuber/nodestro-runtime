from __future__ import annotations

import pytest

from nodestro_runtime.engine import WorkflowError, check_workflow, render_task, topological_order
from nodestro_runtime.schemas import AgentConfig, Edge, Node, Workflow


def _workflow(edges: list[tuple[str, str]], *, agents: tuple[str, ...] = ("a", "b", "c")) -> Workflow:
    nodes = [Node(id=name, type="agent", label=name.upper(), agent=AgentConfig(name=name)) for name in agents]
    return Workflow(nodes=nodes, edges=[Edge(source=s, target=t) for s, t in edges])


def test_topological_order_puts_predecessors_first() -> None:
    order = [node.id for node in topological_order(_workflow([("b", "c"), ("a", "b")]))]
    assert order.index("a") < order.index("b") < order.index("c")


def test_cycle_is_rejected() -> None:
    with pytest.raises(WorkflowError, match="cycle") as caught:
        topological_order(_workflow([("a", "b"), ("b", "a")]))
    assert caught.value.code == "workflow.cycle"


def test_dangling_edge_is_rejected() -> None:
    with pytest.raises(WorkflowError):
        topological_order(_workflow([("a", "x")]))


def test_check_workflow_reports_missing_agents_and_wrong_ports() -> None:
    workflow = Workflow(
        nodes=[Node(id="s", type="start"), Node(id="o", type="output")],
        edges=[Edge(source="o", target="s")],
    )
    codes = {problem.code for problem in check_workflow(workflow)}
    assert codes == {"workflow.no_agents", "workflow.start_has_inputs", "workflow.output_has_outputs"}


def test_render_single_input() -> None:
    assert render_task("Fasse zusammen:\n{{input}}", run_input="x", inputs=[("A", "Text")]) == "Fasse zusammen:\nText"


def test_render_named_inputs_and_run_input() -> None:
    result = render_task(
        "Thema {{run.input}}; A={{A}}; B={{ b }}",
        run_input="KI",
        inputs=[("A", "eins"), ("B", {"x": 1})],
    )
    assert result.startswith("Thema KI; A=eins; B={")
    assert "Eingaben" not in result


def test_render_appends_unused_inputs() -> None:
    result = render_task("Schreibe einen Text.", run_input="", inputs=[("Recherche", "Fakten")])
    assert "# Eingaben" in result and "## Recherche" in result and "Fakten" in result


def test_render_without_predecessors_uses_run_input() -> None:
    assert render_task("{{input}}", run_input="Hallo", inputs=[]) == "Hallo"
    assert render_task("", run_input="Hallo", inputs=[]) == "Hallo"


def test_render_combines_several_inputs() -> None:
    result = render_task("{{input}}", run_input="", inputs=[("A", "1"), ("B", "2")])
    assert "## A" in result and "## B" in result
