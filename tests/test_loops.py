"""Loop connections: repeated nodes, feedback in prompts, limits, approvals in loops, stored state."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime import engine
from nodestro_runtime.engine import AgentOutcome, check_workflow, render_task
from nodestro_runtime.schemas import RunEvent, RunIn, Workflow, WorkflowIn
from nodestro_runtime.testing import ScriptedClient
from nodestro_runtime.workspace import Workspace


def agent(node_id: str, template: str = "{{input}}") -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "agent",
        "label": node_id.title(),
        "agent": {"name": node_id.title(), "system_prompt": node_id, "task_template": template},
    }


def edge(source: str, target: str, handle: str | None = None, **loop: Any) -> dict[str, Any]:
    found: dict[str, Any] = {
        "id": f"{source}-{target}-{handle or ''}",
        "source": source,
        "target": target,
        "source_handle": handle,
    }
    if loop:
        found["loop"] = loop
    return found


GATE = {
    "id": "gate",
    "type": "condition",
    "label": "Gate",
    "condition": {"rules": [{"id": "pass", "field": "", "operator": "contains", "value": "PASS"}]},
}


def quality_loop(**loop: Any) -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Quality loop",
            "nodes": [
                {"id": "start", "type": "start"},
                agent(
                    "draft",
                    "Write about {{run.input}}. Round {{iteration}}. Before: {{previous}}. Review: {{feedback}}",
                ),
                agent("review"),
                GATE,
                agent("publish"),
            ],
            "edges": [
                edge("start", "draft"),
                edge("draft", "review"),
                edge("review", "gate"),
                edge("gate", "publish", "pass"),
                edge("gate", "draft", "default", **({"max_iterations": 3} | loop)),
            ],
        }
    )


Answers = Callable[[str, int], str]


@pytest.fixture
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Workspace]]:
    opened: list[Workspace] = []

    def factory(answers: Answers, *, data: Path | None = None) -> Workspace:
        counts: dict[str, int] = {}
        prompts: dict[str, list[str]] = {}

        def scripted(builder: Any, config: Any, prompt: str, *, name: str, **_: Any) -> AgentOutcome:
            counts[name] = counts.get(name, 0) + 1
            prompts.setdefault(name, []).append(prompt)
            return AgentOutcome(output=answers(name, counts[name]))

        monkeypatch.setattr(engine, "execute_agent", scripted)
        fake = ScriptedClient({})
        workspace = Workspace(data or tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
        workspace.prompts = prompts  # type: ignore[attr-defined]
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def reviewer_passes_at(round_: int) -> Answers:
    def answer(name: str, count: int) -> str:
        if name == "review":
            return "PASS" if count >= round_ else f"needs more detail ({count})"
        return f"{name} {count}"

    return answer


async def test_a_quality_loop_repeats_until_the_review_passes(make: Callable[..., Workspace]) -> None:
    workspace = make(reviewer_passes_at(3))
    events: list[RunEvent] = []
    workspace.runs.event_sink = events.append
    saved = workspace.save_workflow(quality_loop())
    run = await workspace.wait((await workspace.run(saved, RunIn(input="tides"))).id, timeout=10)
    assert run.status == "succeeded", run.error
    assert run.output == "publish 1"
    draft = run.nodes["draft"]
    assert draft.iteration == [2] and draft.output == "draft 3"
    assert [item.output for item in draft.earlier] == ["draft 1", "draft 2"]
    assert [item.iteration for item in draft.earlier] == [[0], [1]]
    assert run.nodes["publish"].iteration == []
    prompts = workspace.prompts["draft"]  # type: ignore[attr-defined]
    assert prompts[0].startswith("Write about tides. Round 1. Before: -. Review: -")
    assert prompts[1].startswith("Write about tides. Round 2. Before: draft 1. Review: needs more detail (1)")
    assert "Review: needs more detail (2)" in prompts[2]
    loops = [event for event in events if event.type == "loop_iteration"]
    assert [event.data["iteration"] for event in loops] == [1, 2]
    finished = [
        event.data.get("iteration") for event in events if event.type == "node_finished" and event.node_id == "draft"
    ]
    assert finished == [[0], [1], [2]]
    stored = workspace.runs.get(run.id)
    assert stored is not None and stored.flow_state is not None
    assert stored.flow_state["iterations"] == {"gate-draft-default@": 2}


async def test_a_loop_that_runs_out_fails_the_run(make: Callable[..., Workspace]) -> None:
    workspace = make(reviewer_passes_at(99))
    saved = workspace.save_workflow(quality_loop(max_iterations=2))
    run = await workspace.wait((await workspace.run(saved, RunIn(input="x"))).id, timeout=10)
    assert run.status == "failed"
    assert run.error_code == "run.loop_exhausted"
    assert run.error_params == {"node": "Gate", "max_iterations": 2}
    assert run.nodes["publish"].status == "cancelled"
    assert len(run.nodes["draft"].earlier) == 1


async def test_continue_after_the_last_iteration_follows_the_other_branches(make: Callable[..., Workspace]) -> None:
    workspace = make(reviewer_passes_at(99))
    saved = workspace.save_workflow(quality_loop(max_iterations=2, on_exhausted="continue"))
    run = await workspace.wait((await workspace.run(saved, RunIn(input="x"))).id, timeout=10)
    # Only the loop's branch was open: nothing follows, the publish step is skipped.
    assert run.status == "succeeded"
    assert run.nodes["publish"].status == "skipped"
    assert run.nodes["review"].iteration == [1]


def approval_loop() -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Approval loop",
            "nodes": [
                {"id": "start", "type": "start"},
                agent("draft", "{{input}} {{feedback}}"),
                {"id": "check", "type": "approval", "label": "Check"},
                agent("send"),
            ],
            "edges": [
                edge("start", "draft"),
                edge("draft", "check"),
                edge("check", "send", "approved"),
                edge("check", "draft", "rejected", max_iterations=3),
            ],
        }
    )


async def test_a_rejected_approval_sends_the_draft_back_also_after_a_restart(
    make: Callable[..., Workspace], tmp_path: Path
) -> None:
    def answer(name: str, count: int) -> str:
        return f"{name} {count}"

    first = make(answer, data=tmp_path / "shared")
    saved = first.save_workflow(approval_loop())
    run = await first.wait((await first.run(saved, RunIn(input="offer"))).id, timeout=10)
    assert run.status == "paused"
    first.decide(run.id, "check", "rejected", "too long")
    run = await first.wait(run.id, timeout=10)
    assert run.status == "paused"
    assert run.nodes["check"].iteration == [1] and run.nodes["check"].approval is None
    assert run.nodes["check"].earlier[0].approval is not None
    assert "too long" in first.prompts["draft"][1]  # type: ignore[attr-defined]
    first.close()

    # Another process continues the stored run with its stored flow state.
    second = make(answer, data=tmp_path / "shared")
    second.decide(run.id, "check", "approved")
    done = await second.wait(run.id, timeout=10)
    assert done.status == "succeeded"
    assert done.output == "send 1"
    assert done.nodes["draft"].iteration == [1]


def test_loops_must_start_at_a_branch_and_lead_back() -> None:
    def codes(edges: list[dict[str, Any]]) -> set[str]:
        nodes = [{"id": "start", "type": "start"}, agent("a"), agent("b"), GATE]
        workflow = Workflow.model_validate({"name": "x", "nodes": nodes, "edges": edges})
        return {issue.code for issue in check_workflow(workflow)}

    base = [edge("start", "a"), edge("a", "b"), edge("b", "gate")]
    assert codes([*base, edge("gate", "a", "default", max_iterations=2)]) == set()
    assert codes([*base, edge("b", "a", max_iterations=2)]) == {"workflow.loop_unguarded"}
    assert "workflow.cycle" in codes([*base, edge("gate", "a", "default")])
    with pytest.raises(ValueError):
        Workflow.model_validate({"name": "x", "nodes": [], "edges": [edge("a", "b", max_iterations=21)]})
    with pytest.raises(ValueError):
        Workflow.model_validate({"name": "x", "nodes": [], "edges": [edge("a", "b", max_iterations=1)]})


def test_loop_placeholders_outside_loops_stay_as_they_are() -> None:
    assert render_task("{{feedback}} {{previous}}", run_input="x", inputs=[]) == "{{feedback}} {{previous}}"
    named = render_task(
        "{{feedback}}", run_input="", inputs=[("Feedback", "mine")], loop=engine.LoopContext(iteration=1)
    )
    assert named == "mine"


def test_continuing_a_run_runs_a_whole_loop_again() -> None:
    from nodestro_runtime.graph import continuation
    from nodestro_runtime.schemas import NodeRun

    workflow = Workflow.model_validate(quality_loop().model_dump() | {"id": "w"})
    earlier = {node.id: NodeRun(status="succeeded", fingerprint="same") for node in workflow.nodes}
    prints = {node.id: "same" for node in workflow.nodes}
    actions = {node.id: (action, reason) for node, action, reason in continuation(workflow, earlier, prints)}
    assert actions["start"] == ("reuse", None)
    assert actions["draft"] == ("rerun", "resume.in_loop")
    assert actions["gate"] == ("rerun", "resume.in_loop")
    assert actions["publish"] == ("rerun", "resume.upstream_changed")
