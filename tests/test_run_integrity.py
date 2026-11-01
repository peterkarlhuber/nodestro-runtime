"""Run integrity on the desktop (SQLite): damaged documents (C-10), stored live runs nothing executes (C-11) and the
agents' own thread pool (C-08). The server side of these findings is in ``test_run_fencing.py``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
import concurrent.futures
from pathlib import Path
import threading
from typing import Any

import pytest

from nodestro_runtime.engine import DEFAULT_RUN_THREADS, run_threads
from nodestro_runtime.schemas import NodeRun, Run, RunIn, Workflow, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text
from nodestro_runtime.workspace import Workspace

Make = Callable[..., Workspace]


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]] | None = None, *, data: Path | None = None) -> Workspace:
        fake = ScriptedClient(script or {})
        workspace = Workspace(data or tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def flow(*prompts: str) -> WorkflowIn:
    nodes: list[dict[str, Any]] = [{"id": "start", "type": "start"}]
    edges: list[dict[str, Any]] = []
    for prompt in prompts:
        node_id = prompt.lower()
        nodes.append(
            {"id": node_id, "type": "agent", "label": node_id, "agent": {"name": node_id, "system_prompt": prompt}}
        )
        edges.append({"id": f"start-{node_id}", "source": "start", "target": node_id})
    return WorkflowIn.model_validate({"name": "Flow", "nodes": nodes, "edges": edges})


def raw_row(workspace: Workspace, collection: str, key: str, data: str) -> None:
    with workspace.store.transaction():
        workspace.store._connection.execute(  # type: ignore[attr-defined]
            "INSERT INTO nodestro_documents (collection, id, data) VALUES (?, ?, ?)", (collection, key, data)
        )


# ---- C-10: one damaged document ------------------------------------------------------------------------------


def test_damaged_documents_neither_stop_the_start_nor_empty_a_list(
    make: Make, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = make()
    saved = workspace.save_workflow(flow("ONE"))
    stale = Run(workflow_id=saved.id, workflow_name=saved.name, workflow=saved, status="running")
    workspace.store.put("runs", stale.id, stale)
    raw_row(workspace, "runs", "run_truncated", '{"id": "run_truncated", "status": "runn')
    raw_row(workspace, "runs", "run_newer", '{"id": "run_newer", "status": "running", "workflow": 7}')
    raw_row(workspace, "workflows", "wf_truncated", '{"id": "wf_truncated", "name": "Bro')
    workspace.close()

    reopened = make()  # start-up recovery reads every queued and running run
    recovered = reopened.runs.get(stale.id)
    assert recovered is not None and recovered.status == "failed" and recovered.error_code == "run.interrupted"
    assert [item.id for item in reopened.store.list("workflows", Workflow)] == [saved.id]
    assert [key for key, _item in reopened.store.items("workflows", Workflow)] == [saved.id]
    assert [item.id for item in reopened.store.list_prefix("workflows", "wf", Workflow)] == [saved.id]
    damaged = reopened.store.damaged  # type: ignore[attr-defined]
    assert set(damaged.found) == {("runs", "run_truncated"), ("runs", "run_newer"), ("workflows", "wf_truncated")}
    assert sum("Damaged document" in record.message and record.levelname == "WARNING" for record in caplog.records) == 3
    # Read on its own, a damaged document is still an error (never silently empty).
    with pytest.raises(ValueError):
        reopened.store.get("workflows", "wf_truncated", Workflow)


# ---- C-11: a run stored as live that nothing executes ----------------------------------------------------------


async def test_cancelling_a_restored_running_run_ends_it(make: Make) -> None:
    workspace = make()
    saved = workspace.save_workflow(flow("ONE"))
    # As after restoring a backup taken while the run executed: stored as running, executed by nobody.
    restored = Run(
        workflow_id=saved.id,
        workflow_name=saved.name,
        workflow=saved,
        status="running",
        nodes={"one": NodeRun(status="running")},
    )
    workspace.store.put("runs", restored.id, restored)
    assert workspace.runs.cancel(restored.id)
    stored = workspace.runs.get(restored.id)
    assert stored is not None and stored.status == "cancelled" and stored.error_code == "run.cancelled"
    assert stored.nodes["one"].status == "cancelled"
    assert not workspace.runs.cancel(restored.id)  # nothing left to cancel


# ---- C-08: agents in their own thread pool --------------------------------------------------------------------


def test_the_agent_pool_size_comes_from_the_environment() -> None:
    assert run_threads({}) == DEFAULT_RUN_THREADS
    assert run_threads({"NODESTRO_RUN_THREADS": "4"}) == 4
    assert run_threads({"NODESTRO_RUN_THREADS": "0"}) == 1
    assert run_threads({"NODESTRO_RUN_THREADS": "many"}) == DEFAULT_RUN_THREADS


async def test_waiting_agents_leave_the_default_executor_free(make: Make) -> None:
    release = threading.Event()
    started = threading.Semaphore(0)

    def slow(_request: dict[str, Any]) -> Any:
        started.release()
        release.wait(10)
        return text("late")

    loop = asyncio.get_running_loop()
    helper = concurrent.futures.ThreadPoolExecutor(max_workers=1)  # the test's own waiting, outside both pools
    default = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(default)  # a small default pool, as on a machine with few cores
    workspace = make({"ONE": [slow], "TWO": [slow], "THREE": [slow]})
    workspace.runs.run_threads = 3
    saved = workspace.save_workflow(flow("ONE", "TWO", "THREE"))
    try:
        run = await workspace.run(saved, RunIn(input="x"))
        for _ in range(3):
            assert await loop.run_in_executor(helper, started.acquire, True, 5)
        names = {thread.name for thread in threading.enumerate()}
        assert sum(name.startswith("nodestro-run") for name in names) == 3
        # Three agents wait; background work of the application (backups, uploads, a worker's lease) still runs.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), 2) == "free"
    finally:
        release.set()
        helper.shutdown(wait=False)
    finished = await workspace.wait(run.id, timeout=10)
    assert finished.status == "succeeded"
