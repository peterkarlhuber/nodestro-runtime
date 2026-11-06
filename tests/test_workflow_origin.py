"""A copy of an example remembers where it came from (RT-055): set once by the code that copies, kept by every
later save, never set by an editor or a client."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nodestro_runtime.schemas import Workflow, WorkflowIn, WorkflowOrigin
from nodestro_runtime.workspace import Workspace

ORIGIN = WorkflowOrigin(example_id="offer", version="1.2.3", language="de", offline=["crm"], connected=[])


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    with Workspace(tmp_path / "data", integrations=[]) as opened:
        yield opened


def test_an_edit_keeps_the_origin(workspace: Workspace) -> None:
    copy = workspace.save_workflow(Workflow(name="Offer", origin=ORIGIN))
    assert workspace.workflow(copy.id).origin == ORIGIN
    # The editor saves what it shows: a WorkflowIn, which has no origin.
    edited = Workflow(**WorkflowIn(name="Offer, edited").model_dump(), id=copy.id, created_at=copy.created_at)
    saved = workspace.save_workflow(edited)
    assert saved.name == "Offer, edited" and saved.origin == ORIGIN
    assert workspace.workflow(copy.id).origin == ORIGIN


def test_a_save_cannot_replace_the_origin(workspace: Workspace) -> None:
    copy = workspace.save_workflow(Workflow(name="Offer", origin=ORIGIN))
    forged = WorkflowOrigin(example_id="other", version="9", language="en")
    saved = workspace.save_workflow(copy.model_copy(update={"origin": forged}))
    assert saved.origin == ORIGIN


def test_a_client_cannot_send_an_origin() -> None:
    data = WorkflowIn.model_validate({"name": "X", "origin": ORIGIN.model_dump(mode="json")})
    assert "origin" not in data.model_dump()
    assert Workflow(**data.model_dump()).origin is None


def test_workflows_made_otherwise_have_none(workspace: Workspace) -> None:
    assert workspace.save_workflow(WorkflowIn(name="Mine")).origin is None
