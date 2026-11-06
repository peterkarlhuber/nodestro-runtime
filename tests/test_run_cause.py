"""RT-012 (AD-02): a failed run names the step that failed first and its problem, on the run and its summary."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime.engine import run_cause
from nodestro_runtime.schemas import NodeRun, Run, RunIn, RunSummary, Workflow, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    fake = ScriptedClient({})
    found = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
    found.fake = fake  # type: ignore[attr-defined]
    yield found
    found.close()


def agent(node_id: str, prompt: str, **config: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "agent",
        "label": node_id.title(),
        "agent": {"name": node_id, "system_prompt": prompt, **config},
    }


def flow() -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Cause",
            "nodes": [{"id": "start", "type": "start"}, agent("writer", "WRITER", max_tool_rounds=0)],
            "edges": [{"id": "e1", "source": "start", "target": "writer"}],
        }
    )


async def test_a_failed_run_names_its_first_failed_step_and_why(workspace: Workspace) -> None:
    # The model asks for a tool although the step allows no tool round: the step fails at run time.
    workspace.fake.messages.script["WRITER"] = [tool_use("calculator", {"expression": "1"})]  # type: ignore[attr-defined]
    saved = workspace.save_workflow(flow())
    started = await workspace.run(saved, RunIn(input="x"))
    run = await workspace.wait(started.id, timeout=30)
    assert run.status == "failed" and run.error_code == "run.nodes_failed"
    assert run.cause is not None
    assert run.cause.node_id == "writer" and run.cause.label == "Writer"
    assert run.cause.code == run.nodes["writer"].error_code == "run.agent.MaxToolRoundsError"
    assert run.cause.params == run.nodes["writer"].error_params and run.cause.params["rounds"] == 0
    # The summary (run lists) carries it too, read by the store's projection.
    page, _ = workspace.store.list_page("runs", RunSummary, limit=10, summary=True)
    assert page[0].cause == run.cause
    # Continued after the cause was fixed, the run succeeds and the cause is gone.
    workspace.fake.messages.script["WRITER"] = [text("done")]  # type: ignore[attr-defined]
    continued = await workspace.resume(run.id, confirm=True)
    done = await workspace.wait(continued.id, timeout=30)
    assert done.status == "succeeded" and done.cause is None and done.error_code is None


async def test_a_successful_run_has_no_cause(workspace: Workspace) -> None:
    workspace.fake.messages.script["OK"] = [text("done")]  # type: ignore[attr-defined]
    saved = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Fine",
                "nodes": [{"id": "start", "type": "start"}, agent("writer", "OK")],
                "edges": [{"id": "e1", "source": "start", "target": "writer"}],
            }
        )
    )
    started = await workspace.run(saved, RunIn(input="x"))
    run = await workspace.wait(started.id, timeout=30)
    assert run.status == "succeeded" and run.cause is None


def test_the_earliest_failure_is_the_cause() -> None:
    t0 = datetime(2026, 11, 6, tzinfo=UTC)
    workflow = Workflow.model_validate(
        {
            "id": "wf",
            "name": "Two",
            "nodes": [{"id": "start", "type": "start"}, agent("a", "A"), agent("b", "B"), agent("c", "C")],
            "edges": [],
        }
    )
    nodes = {
        "a": NodeRun(status="failed", error_code="run.node_cancelled", finished_at=t0 + timedelta(seconds=5)),
        "b": NodeRun(
            status="failed", error_code="provider.rate_limited", error_params={"provider": "x"}, finished_at=t0
        ),
        "c": NodeRun(status="failed", error_code="run.failed"),  # no end time: after those with one
    }
    run = Run(workflow_id="wf", workflow_name="Two", workflow=workflow, nodes=nodes)
    cause = run_cause(run)
    assert cause is not None and (cause.node_id, cause.code, cause.params) == (
        "b",
        "provider.rate_limited",
        {"provider": "x"},
    )
    assert cause.label == "B"
    assert run_cause(Run(workflow_id="wf", workflow_name="Two", workflow=workflow)) is None
