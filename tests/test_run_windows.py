"""RT-042: the cost estimate and the context preview find a workflow's runs however many other runs are stored (they
used to look only at the 500 most recently updated runs)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nodestro_runtime.launcher import ESTIMATE_RUNS, estimate_cost
from nodestro_runtime.preview import latest_run
from nodestro_runtime.run_views import workflow_runs
from nodestro_runtime.schemas import Run, Usage, Workflow
from nodestro_runtime.workspace import Workspace

T0 = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    found = Workspace(tmp_path / "data", integrations=[])
    yield found
    found.close()


def _run(run_id: str, workflow_id: str, at: datetime, *, cost: float | None = 0.5, **fields: object) -> Run:
    return Run.model_validate(
        {
            "id": run_id,
            "workflow_id": workflow_id,
            "workflow_name": workflow_id,
            "workflow": Workflow(id=workflow_id, name=workflow_id).model_dump(),
            "status": "succeeded",
            "usage": Usage(estimated_cost_usd=cost).model_dump(),
            "created_at": at,
            "finished_at": at + timedelta(seconds=5),
            **fields,
        }
    )


def test_a_workflow_s_runs_are_found_behind_many_newer_runs(workspace: Workspace) -> None:
    store = workspace.store
    store.put("runs", "old_1", _run("old_1", "wf_x", T0, cost=1.0))
    store.put("runs", "old_2", _run("old_2", "wf_x", T0 + timedelta(minutes=1), cost=3.0))
    store.put("runs", "old_child", _run("old_child", "wf_x", T0 + timedelta(minutes=2), parent_run_id="p"))
    store.put("runs", "old_failed", _run("old_failed", "wf_x", T0 + timedelta(minutes=3), status="failed"))
    for index in range(620):  # newer, and updated after the runs of wf_x
        store.put("runs", f"n{index}", _run(f"n{index}", "wf_other", T0 + timedelta(hours=1, seconds=index)))
    estimate = estimate_cost(workspace, "wf_x", 10)
    assert estimate.based_on == 2 and estimate.per_run_usd == 2.0 and estimate.total_usd == 20.0
    # The preview takes predecessor outputs from the newest finished run, also one as a sub-workflow.
    found = latest_run(workspace, "wf_x")
    assert found is not None and found.id == "old_failed"
    assert [run.id for run in workflow_runs(store, "wf_x")] == ["old_failed", "old_2", "old_1"]
    assert latest_run(workspace, "wf_missing") is None


def test_the_estimate_learns_from_the_newest_runs_only(workspace: Workspace) -> None:
    for index in range(ESTIMATE_RUNS + 130):
        cost = 1.0 if index >= 130 else 100.0  # the 130 oldest runs are expensive
        workspace.store.put("runs", f"r{index}", _run(f"r{index}", "wf_x", T0 + timedelta(seconds=index), cost=cost))
    estimate = estimate_cost(workspace, "wf_x", 1)
    assert estimate.based_on == ESTIMATE_RUNS and estimate.per_run_usd == 1.0


def test_usage_entries_of_a_period_are_filtered_by_the_store(workspace: Workspace) -> None:
    """RT-009: ``UsageLog.entries(since)`` asks the store for the period (newest first) instead of reading all."""
    from nodestro_runtime.usage_log import UsageEntry, UsageLog

    log = UsageLog(workspace.store)
    for index in range(5):
        entry = UsageEntry(id=f"u{index}", at=T0 + timedelta(days=index), source="agent_test", model="m", usage=Usage())
        workspace.store.put("usage_log", entry.id, entry)
    assert [entry.id for entry in log.entries(T0 + timedelta(days=3))] == ["u4", "u3"]
    assert [entry.id for entry in log.entries()] == ["u4", "u3", "u2", "u1", "u0"]
    calls: list[str] = []
    original = workspace.store.list
    workspace.store.list = lambda *args, **kwargs: calls.append("list") or original(*args, **kwargs)  # type: ignore[method-assign]
    log.entries(T0)
    assert calls == []  # no whole-collection read for a period
