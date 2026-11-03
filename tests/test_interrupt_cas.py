"""``RunManager.interrupt`` writes conditionally (C-06, audit RT2-04).

The reaper and the reconciliation mark a run that nothing executes any more as interrupted. A worker (or a decision)
may store the run between the moment ``interrupt`` reads it and the moment it writes: that later state must win, and
``interrupt`` must not overwrite it with an interrupted copy of the earlier one.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime.engine import _fenced, _FencedRun
from nodestro_runtime.schemas import Run, RunIn, WorkflowIn
from nodestro_runtime.testing import AnswerAllClient
from nodestro_runtime.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    fake = AnswerAllClient()
    opened = Workspace(tmp_path / "d", client_factory=lambda _p: fake, integrations=[])
    yield opened
    opened.close()


async def _finished_run(ws: Workspace) -> Run:
    wf = ws.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "One",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "a", "type": "agent", "agent": {"name": "A", "system_prompt": "Draft"}},
                ],
                "edges": [{"id": "e", "source": "start", "target": "a"}],
            }
        )
    )
    started = await ws.run(wf, RunIn(input="go"))
    return await ws.wait(started.id, timeout=30)


@pytest.mark.parametrize("fenced", [True, False], ids=["fence", "status"])
async def test_interrupt_does_not_overwrite_a_run_stored_meanwhile(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, fenced: bool
) -> None:
    done = await _finished_run(workspace)
    live = done.model_copy(deep=True)
    live.id = "run_live_elsewhere"
    live.status = "running"
    live.finished_at = None
    store = workspace.runs.store
    store.put("runs", live.id, _fenced(live, "fence-of-the-reaper-read") if fenced else live)

    later = live.model_copy(deep=True)
    later.status = "succeeded"
    later.finished_at = done.finished_at
    original_get = store.get
    raced: list[str] = []

    def get(collection: str, key: str, model: type[Any]) -> Any:
        found = original_get(collection, key, model)
        if collection == "runs" and key == live.id and model is _FencedRun and not raced:
            # The worker that still executes the run stores its result right after interrupt read it.
            raced.append(key)
            store.put("runs", live.id, _fenced(later, "fence-of-the-worker") if fenced else later)
        return found

    monkeypatch.setattr(store, "get", get)
    assert workspace.runs.interrupt(live.id) is False
    monkeypatch.undo()
    assert raced == [live.id]
    stored = workspace.runs.get(live.id)
    assert stored is not None
    assert stored.status == "succeeded" and stored.error_code is None


async def test_interrupt_marks_a_run_nobody_wrote_meanwhile(workspace: Workspace) -> None:
    """The counterpart: without a concurrent write the run is marked interrupted."""
    done = await _finished_run(workspace)
    live = done.model_copy(deep=True)
    live.id = "run_orphaned"
    live.status = "running"
    live.finished_at = None
    workspace.runs.store.put("runs", live.id, _fenced(live, "some-fence"))
    assert workspace.runs.interrupt(live.id) is True
    stored = workspace.runs.get(live.id)
    assert stored is not None and stored.status == "failed" and stored.error_code == "run.interrupted"
