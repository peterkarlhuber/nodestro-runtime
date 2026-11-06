"""The answering test double finishes orchestrations (RT-056): it staffs a team when it must, delegates the task to
one team and finishes once that team returned its result, so orchestrated workflows run end to end without a
script."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.testing import AnswerAllClient
from nodestro_runtime.workspace import Workspace


@pytest.fixture
def fake() -> AnswerAllClient:
    return AnswerAllClient()


@pytest.fixture
def workspace(tmp_path: Path, fake: AnswerAllClient) -> Iterator[Workspace]:
    found = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
    yield found
    found.close()


def _team(workspace: Workspace, name: str) -> str:
    return workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": name,
                "description": f"{name} team",
                "nodes": [
                    {"id": "s", "type": "start"},
                    {"id": "w", "type": "agent", "label": "W", "agent": {"name": "W", "system_prompt": name.upper()}},
                ],
                "edges": [{"id": "e", "source": "s", "target": "w"}],
            }
        )
    ).id


@pytest.mark.parametrize(
    "config",
    [{}, {"staffing": True}, {"max_parallel": 2}, {"staffing": True, "max_team": 1}],
    ids=["plain", "staffing", "parallel", "small-team"],
)
async def test_an_orchestration_delegates_once_and_finishes(
    workspace: Workspace, fake: AnswerAllClient, config: dict[str, Any]
) -> None:
    research, writing = _team(workspace, "Research"), _team(workspace, "Writing")
    orchestrator = {"members": [{"workflow_id": research}, {"workflow_id": writing}], **config}
    flow = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Lead",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "lead", "type": "orchestrator", "label": "Lead", "orchestrator": orchestrator},
                    {"id": "out", "type": "output"},
                ],
                "edges": [
                    {"id": "e1", "source": "start", "target": "lead"},
                    {"id": "e2", "source": "lead", "target": "out"},
                ],
            }
        )
    )
    started = await workspace.run(flow, RunIn(input="Find out about otters"))
    run = await workspace.wait(started.id, timeout=60)
    assert run.status == "succeeded", run.error
    assert run.output == "Ergebnis des Agenten."
    systems = [str(call.get("system", "")) for call in fake.messages.calls]
    # One team ran, with the task; no decision was refused.
    assert sum("RESEARCH" in system for system in systems) == 1
    prompts = " ".join(str(call.get("messages")) for call in fake.messages.calls)
    assert "Invalid answer" not in prompts
