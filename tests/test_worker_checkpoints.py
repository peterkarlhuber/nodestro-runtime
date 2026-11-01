"""Server workers with runs paused at a tool call (M6b agent checkpoints). Real PostgreSQL only.

A paused run frees its worker (the job ends); the decision, made in another process, queues the run again and a
worker continues it under its lease. Covers tenant isolation, racing decisions and resumes (the call runs once).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from nodestro_runtime import tool_checkpoints
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.tool_approvals import ToolApprovalRequest, ToolApprovals, ToolGate

pytest.importorskip("psycopg")

from test_worker_approvals import (
    TOOL,
    Api,
    Process,
    Serving,
    asking,
    call_of,
    connect,
    processes,
    schema,
    script,
    worker_of,
)

from nodestro_runtime.jobs import JobQueue

__all__ = ["connect", "processes", "schema"]  # fixtures used by the tests below


class CheckpointProcess(Process):
    """A process whose tool gates pause runs at a waiting call."""

    def __call__(self, tenant_id: str) -> Any:
        fresh = tenant_id not in self.opened
        workspace = super().__call__(tenant_id)
        if fresh:
            workspace.runs.tool_gate = ToolGate(
                ToolApprovals(workspace.store, wait=self.wait), checkpoints=True, max_paused=5
            )
        return workspace


def checkpointing(make: Callable[..., Process], database: Any, entries: dict[str, list[Any]]) -> CheckpointProcess:
    process = make(database, entries)
    process.__class__ = CheckpointProcess  # the fixture closes it like any other process
    assert isinstance(process, CheckpointProcess)
    return process


async def paused(api: Api, prompt: str, tenant: str = "alpha") -> tuple[str, ToolApprovalRequest]:
    run = await api.start(prompt, tenant)
    found = await api.until(run.id, ("paused", "succeeded", "failed"), tenant)
    assert found.status == "paused", found.error
    (request,) = api.process.approvals(tenant).pending(run.id)
    assert request.checkpointed
    return run.id, request


async def test_a_paused_run_frees_its_worker_and_continues_after_the_decision(
    connect: Callable[[], Any], processes: Callable[..., Process]
) -> None:
    entries = script(AGENT=asking())
    api_db, worker_db = connect(), connect()
    api = Api(checkpointing(processes, api_db, entries), JobQueue(api_db))
    worker = checkpointing(processes, worker_db, entries)
    serving = Serving(worker_of(worker, worker_db, max_runs=2, max_waiting=1))
    try:
        run_id, request = await paused(api, "AGENT")
        assert api.job_status(run_id) == "done"  # the job ended: the worker holds nothing while the run waits
        assert serving.worker.held == 0
        assert tool_checkpoints.runs_with_checkpoints(api.process("alpha").store) == {run_id}
        api.process("alpha").runs.decide_tool_call(
            run_id, request.id, approved=True, by="usr_1", arguments=request.arguments_hash
        )
        done = await api.until(run_id, ("succeeded", "failed"))
        assert done.status == "succeeded", done.error
        assert call_of(done).output == "sent hi"
        stored = api.process.approvals("alpha").get(run_id, request.id)
        assert stored is not None and stored.use == "executed"
        assert tool_checkpoints.runs_with_checkpoints(api.process("alpha").store) == set()
    finally:
        await serving.close()


async def test_another_tenant_sees_neither_the_question_nor_the_checkpoint(
    connect: Callable[[], Any], processes: Callable[..., Process]
) -> None:
    entries = script(AGENT=asking())
    api_db, worker_db = connect(), connect()
    api = Api(checkpointing(processes, api_db, entries), JobQueue(api_db))
    serving = Serving(worker_of(checkpointing(processes, worker_db, entries), worker_db))
    try:
        run_id, request = await paused(api, "AGENT")
        other = api.process("beta")
        assert ToolApprovals(other.store).get(run_id, request.id) is None
        assert tool_checkpoints.load(other.store, run_id, "a", []) is None
        with pytest.raises(NodestroError) as error:
            other.runs.decide_tool_call(run_id, request.id, approved=True, by="x", arguments=request.arguments_hash)
        assert error.value.code == "tool.approval_not_found"
        assert api.run(run_id).status == "paused"
    finally:
        await serving.close()


async def test_racing_decisions_and_resumes_run_the_call_once(
    connect: Callable[[], Any], processes: Callable[..., Process]
) -> None:
    entries = script(AGENT=asking())
    api_db, other_db, worker_db = connect(), connect(), connect()
    api = Api(checkpointing(processes, api_db, entries), JobQueue(api_db))
    second = Api(checkpointing(processes, other_db, entries), JobQueue(other_db))
    serving = Serving(worker_of(checkpointing(processes, worker_db, entries), worker_db))
    try:
        run_id, request = await paused(api, "AGENT")
        second.process("alpha")  # open the second process's workspace
        from nodestro_runtime.jobs import QueuedRuns

        QueuedRuns(second.queue, "alpha", second.process("alpha").runs).attach()

        def decide(target: Api, approved: bool) -> str:
            try:
                target.process("alpha").runs.decide_tool_call(
                    run_id, request.id, approved=approved, by="usr", arguments=request.arguments_hash
                )
                return "ok"
            except NodestroError as error:
                return error.code

        results = await asyncio.gather(asyncio.to_thread(decide, api, True), asyncio.to_thread(decide, second, False))
        assert sorted(results) == ["ok", "tool.approval_decided"]
        # Both processes try to continue the run again: it is queued at most once.
        for target in (api, second):
            target.process("alpha").runs._continue_paused(run_id)
        done = await api.until(run_id, ("succeeded", "failed"))
        assert done.status == "succeeded", done.error
        record = call_of(done)
        stored = api.process.approvals("alpha").get(run_id, request.id)
        assert stored is not None
        if stored.status == "approved":
            assert record.output == "sent hi" and stored.use == "executed"
        else:
            assert record.output != "sent hi" and stored.use == "closed"
        assert sum(1 for item in done.nodes["a"].tool_calls if item.name == TOOL) == 1
    finally:
        await serving.close()
