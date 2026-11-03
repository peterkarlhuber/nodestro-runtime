"""RT2 probe: pending_approvals() only looks at the 5000 most recently updated runs, so an older paused run that
waits for an approval disappears from the approvals inbox once 5000 newer runs exist."""

from __future__ import annotations

from pathlib import Path
import time

from nodestro_runtime.run_views import pending_approvals
from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.testing import AnswerAllClient
from nodestro_runtime.workspace import Workspace


async def test_rt2_02_an_old_paused_run_stays_in_the_inbox(tmp_path: Path) -> None:
    fake = AnswerAllClient()
    ws = Workspace(tmp_path / "d", client_factory=lambda _p: fake, integrations=[])
    try:
        wf = ws.save_workflow(
            WorkflowIn.model_validate(
                {
                    "name": "Needs OK",
                    "nodes": [
                        {"id": "start", "type": "start"},
                        {"id": "a", "type": "agent", "agent": {"name": "A", "system_prompt": "Draft"}},
                        {"id": "ok", "type": "approval", "label": "OK?"},
                    ],
                    "edges": [
                        {"id": "e", "source": "start", "target": "a"},
                        {"id": "f", "source": "a", "target": "ok"},
                    ],
                }
            )
        )
        started = await ws.run(wf, RunIn(input="go"))
        paused = await ws.wait(started.id, timeout=30)
        assert paused.status == "paused"
        before = [p.run_id for p in pending_approvals(ws)]
        assert paused.id in before
        filler = paused.model_copy(deep=True)
        filler.status = "succeeded"
        with ws.store.transaction():
            for i in range(5000):
                filler.id = f"filler_{i:05d}"
                ws.store.put("runs", filler.id, filler)
        t0 = time.perf_counter()
        after = [p.run_id for p in pending_approvals(ws)]
        ms = (time.perf_counter() - t0) * 1000
        print(f"pending approvals before: {before}; after 5000 newer runs: {after} ({ms:.0f} ms)")
        stored = ws.runs.get(paused.id)
        print("stored status of the paused run:", stored.status)
        assert stored.status == "paused"
        assert paused.id in after  # desired (RT2-02): every paused run, however old
    finally:
        ws.close()
