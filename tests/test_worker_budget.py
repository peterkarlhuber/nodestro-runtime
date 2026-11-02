"""Server workers with runs waiting for a budget decision (BUDGET-1). Real PostgreSQL only.

A run whose next model request does not fit its budget pauses before the request is sent and frees its worker (the
job ends). The decision, made in another process, queues the run again and a worker continues it from the saved
checkpoint: nothing is sent twice, no tool runs twice, and a raise never answers a tool approval. Covers tenant
isolation and racing decisions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from nodestro_runtime import tool_checkpoints
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.run_budget import BudgetRequest, BudgetRequests
from nodestro_runtime.schemas import RunIn, WorkflowIn

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

from nodestro_runtime.jobs import JobQueue, QueuedRuns

__all__ = ["connect", "processes", "schema"]  # fixtures used by the tests below


def budgeted(prompt: str, budget: float) -> WorkflowIn:
    agent = {"name": "Agent", "system_prompt": prompt, "tools": [TOOL], "max_tokens": 300}
    return WorkflowIn.model_validate(
        {
            "name": f"{prompt.title()} budget",
            "nodes": [{"id": "start", "type": "start"}, {"id": "a", "type": "agent", "agent": agent}],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
            "budget_usd": budget,
        }
    )


async def waiting(api: Api, prompt: str, budget: float = 0.001, tenant: str = "alpha") -> tuple[str, BudgetRequest]:
    workspace = api.process(tenant)
    QueuedRuns(api.queue, tenant, workspace.runs).attach()
    saved = workspace.save_workflow(budgeted(prompt, budget))
    run = await workspace.run(saved, RunIn(input="x"), started_by="usr_1")
    found = await api.until(run.id, ("paused", "succeeded", "failed"), tenant)
    assert found.status == "paused", found.error
    assert found.nodes["a"].status == "awaiting_budget"
    (request,) = [item for item in BudgetRequests(workspace.store).of_run(run.id) if item.status == "pending"]
    assert found.budget is not None and found.budget.pending_request_id == request.id
    return run.id, request


async def test_a_budget_wait_frees_its_worker_and_continues_after_the_raise(
    connect: Callable[[], Any], processes: Callable[..., Process]
) -> None:
    entries = script(AGENT=asking())
    api_db, worker_db = connect(), connect()
    api = Api(processes(api_db, entries), JobQueue(api_db))
    worker = processes(worker_db, entries)
    serving = Serving(worker_of(worker, worker_db, max_runs=2, max_waiting=1))
    try:
        run_id, request = await waiting(api, "AGENT")
        assert api.job_status(run_id) == "done"  # the job ended: the worker holds nothing while the run waits
        assert serving.worker.held == 0
        assert len(worker.client.messages.calls) == 0  # paused before the request was sent
        assert tool_checkpoints.runs_with_checkpoints(api.process("alpha").store, "budget") == {run_id}
        api.process("alpha").runs.decide_budget(run_id, request.id, action="raise", by="usr_1", limit_usd="1")
        # The raise is no tool approval: the tool call still asks its own question.
        api.decide(await api.pending(run_id))
        done = await api.until(run_id, ("succeeded", "failed"))
        assert done.status == "succeeded", done.error
        assert call_of(done).output == "sent hi"  # the tool ran exactly once
        assert len(worker.client.messages.calls) == 2
        assert done.budget is not None and [raised.new_limit_usd for raised in done.budget.raises] == [1]
        assert tool_checkpoints.runs_with_checkpoints(api.process("alpha").store) == set()
    finally:
        await serving.close()


async def test_another_tenant_can_neither_see_nor_decide_the_request(
    connect: Callable[[], Any], processes: Callable[..., Process]
) -> None:
    entries = script(AGENT=asking())
    api_db, worker_db = connect(), connect()
    api = Api(processes(api_db, entries), JobQueue(api_db))
    serving = Serving(worker_of(processes(worker_db, entries), worker_db))
    try:
        run_id, request = await waiting(api, "AGENT")
        other = api.process("beta")
        assert BudgetRequests(other.store).get(run_id, request.id) is None
        assert BudgetRequests(other.store).pending() == []
        with pytest.raises(KeyError):
            other.runs.decide_budget(run_id, request.id, action="raise", by="x", limit_usd="5")
        assert api.run(run_id).status == "paused"
    finally:
        await serving.close()


async def test_racing_decisions_from_two_processes_have_one_winner(
    connect: Callable[[], Any], processes: Callable[..., Process]
) -> None:
    entries = script(AGENT=asking())
    api_db, other_db, worker_db = connect(), connect(), connect()
    api = Api(processes(api_db, entries), JobQueue(api_db))
    second = Api(processes(other_db, entries), JobQueue(other_db))
    worker = processes(worker_db, entries)
    serving = Serving(worker_of(worker, worker_db))
    try:
        run_id, request = await waiting(api, "AGENT")
        QueuedRuns(second.queue, "alpha", second.process("alpha").runs).attach()

        def decide(target: Api, action: str) -> str:
            try:
                target.process("alpha").runs.decide_budget(
                    run_id,
                    request.id,
                    action=action,  # type: ignore[arg-type]
                    by="usr_1",
                    limit_usd="1" if action == "raise" else None,
                )
                return action
            except NodestroError as error:
                return error.code

        results = await asyncio.gather(
            asyncio.to_thread(decide, api, "raise"), asyncio.to_thread(decide, second, "end")
        )
        assert "budget.not_pending" in results
        (winner,) = [result for result in results if result != "budget.not_pending"]
        if winner == "raise":
            api.decide(await api.pending(run_id))
        done = await api.until(run_id, ("succeeded", "failed", "cancelled"))
        if winner == "raise":
            assert done.status == "succeeded", done.error
            assert len(worker.client.messages.calls) == 2
        else:
            assert done.status == "cancelled" and done.error_code == "run.budget_ended"
            assert len(worker.client.messages.calls) == 0
        stored = BudgetRequests(api.process("alpha").store).get(run_id, request.id)
        assert stored is not None and stored.status == ("raised" if winner == "raise" else "ended")
    finally:
        await serving.close()
