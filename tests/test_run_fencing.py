"""Conditional run writes on a server (PostgreSQL, needs ``NODESTRO_TEST_POSTGRES``): one approval decision wins
across processes (C-02), a worker that lost its lease never overwrites its run (C-06), runs stored as live without a
job are reconciled (C-11), and a worker's lease renewal does not wait behind other work (C-08)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
import concurrent.futures
from datetime import timedelta
from pathlib import Path
import threading
from typing import Any

import postgres_support
import pytest

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import NodeRun, Run, RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text
from nodestro_runtime.workspace import Workspace

pytest.importorskip("psycopg")

from nodestro_runtime.jobs import RUN_JOB, JobQueue, PostgresEventLog, QueuedRuns, RunWorker, orphaned_runs, reap
from nodestro_runtime.postgres_tables import tenant_storage
from nodestro_runtime.storage_postgres import PostgresDatabase


@pytest.fixture
def connect() -> Iterator[Callable[[], PostgresDatabase]]:
    """A new connection to a fresh test schema (each one stands for a process)."""
    dsn = postgres_support.require_postgres()
    name = postgres_support.new_schema()
    first = PostgresDatabase(dsn, schema=name)
    first.migrate()
    opened = [first]

    def factory() -> PostgresDatabase:
        database = PostgresDatabase(dsn, schema=name)
        opened.append(database)
        return database

    yield factory
    for database in opened:
        database.close()
    postgres_support.drop_schema(name)


class Process:
    """The workspaces one process (API or worker) opened, one per tenant."""

    def __init__(self, database: PostgresDatabase, root: Path, script: dict[str, list[Any]]) -> None:
        self.database = database
        self.root = root
        self.client = ScriptedClient(script)
        self.opened: dict[str, Workspace] = {}

    def __call__(self, tenant_id: str) -> Workspace:
        if tenant_id not in self.opened:
            self.opened[tenant_id] = Workspace(
                self.root / tenant_id,
                client_factory=lambda _provider: self.client,
                integrations=[],
                storage=tenant_storage(self.database, tenant_id),
            )
        return self.opened[tenant_id]


@pytest.fixture
def processes(tmp_path: Path) -> Iterator[Callable[..., Process]]:
    made: list[Process] = []

    def factory(database: PostgresDatabase, script: dict[str, list[Any]] | None = None) -> Process:
        process = Process(database, tmp_path / "files", script or {})
        made.append(process)
        return process

    yield factory
    for process in made:
        for workspace in process.opened.values():
            workspace.close()


def agent(node_id: str, prompt: str) -> dict[str, Any]:
    return {"id": node_id, "type": "agent", "label": node_id, "agent": {"name": node_id, "system_prompt": prompt}}


def edge(source: str, target: str, handle: str | None = None) -> dict[str, Any]:
    return {"id": f"{source}-{target}", "source": source, "target": target, "source_handle": handle}


def simple_flow() -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Simple",
            "nodes": [{"id": "start", "type": "start"}, agent("write", "WRITE")],
            "edges": [edge("start", "write")],
        }
    )


def approval_flow() -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Approval",
            "nodes": [
                {"id": "start", "type": "start"},
                agent("draft", "DRAFT"),
                {"id": "check", "type": "approval", "label": "Check"},
                agent("send", "SEND"),
            ],
            "edges": [edge("start", "draft"), edge("draft", "check"), edge("check", "send", "approved")],
        }
    )


def blocking(release: threading.Event, started: threading.Event) -> Callable[[dict[str, Any]], Any]:
    def respond(_request: dict[str, Any]) -> Any:
        started.set()
        release.wait(10)
        return text("late")

    return respond


async def api_start(api: Process, queue: JobQueue, tenant: str, flow: WorkflowIn) -> Run:
    workspace = api(tenant)
    QueuedRuns(queue, tenant, workspace.runs).attach()
    saved = workspace.save_workflow(flow)
    return await workspace.run(saved, RunIn(input="x"))


class ReadThen:
    """A store whose first read of a run is followed by ``then()``: another process acts between this process's read
    and its write (the interleaving of two reviewers clicking at the same moment)."""

    def __init__(self, store: Any, then: Callable[[], None]) -> None:
        self._store = store
        self._then: Callable[[], None] | None = then

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def get(self, collection: str, key: str, model: Any) -> Any:
        found = self._store.get(collection, key, model)
        then, self._then = self._then, None
        if collection == "runs" and then is not None:
            then()
        return found


# ---- C-02: one decision per approval across processes --------------------------------------------------------


async def test_two_processes_deciding_one_approval_record_exactly_one_decision(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"DRAFT": [text("draft")]})
    queue = JobQueue(api_db)
    started = await api_start(api, queue, "alpha", approval_flow())
    await RunWorker(JobQueue(worker_db), worker, name="w1").run_once()
    assert api("alpha").runs.get(started.id).status == "paused"  # type: ignore[union-attr]

    first, second = processes(connect()), processes(connect())
    for process in (first, second):
        QueuedRuns(JobQueue(process.database), "alpha", process("alpha").runs).attach()
    outcomes: dict[str, str] = {}

    def second_decides() -> None:
        second("alpha").decide(started.id, "check", "rejected")
        outcomes["second"] = "accepted"

    # The first reviewer's process has read the paused run when the second one decides and queues it.
    first("alpha").runs.store = ReadThen(first("alpha").runs.store, second_decides)
    with pytest.raises(NodestroError) as refused:
        first("alpha").decide(started.id, "check", "approved")
    assert refused.value.code == "approval.not_pending"
    assert outcomes == {"second": "accepted"}

    stored = api("alpha").runs.get(started.id)
    assert stored is not None and stored.status == "queued"
    approval = stored.nodes["check"].approval
    assert approval is not None and approval.decision == "rejected"  # the acknowledged decision is the stored one
    assert [job.status for job in queue.jobs("alpha", RUN_JOB, started.id)] == ["done", "queued"]


async def test_a_decision_on_a_run_that_went_on_meanwhile_is_refused_before_it_is_stored(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"DRAFT": [text("draft")]})
    queue = JobQueue(api_db)
    started = await api_start(api, queue, "alpha", approval_flow())
    await RunWorker(JobQueue(worker_db), worker, name="w1").run_once()
    other = processes(connect())
    QueuedRuns(JobQueue(other.database), "alpha", other("alpha").runs).attach()

    def cancel_elsewhere() -> None:
        assert api("alpha").cancel(started.id)

    other("alpha").runs.store = ReadThen(other("alpha").runs.store, cancel_elsewhere)
    with pytest.raises(NodestroError) as refused:
        other("alpha").decide(started.id, "check", "approved")
    assert refused.value.code == "approval.not_pending"
    stored = api("alpha").runs.get(started.id)
    assert stored is not None and stored.status == "cancelled" and stored.nodes["check"].approval is None


# ---- C-06: a worker that lost its lease ------------------------------------------------------------------------


async def test_a_stalled_worker_never_overwrites_its_interrupted_run(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    release, called = threading.Event(), threading.Event()
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"WRITE": [blocking(release, called)]})
    queue = JobQueue(api_db, lease=timedelta(milliseconds=300))
    started = await api_start(api, queue, "alpha", simple_flow())
    # The worker's lease renewal stalls (a blocked loop, a database stall, a paused VM): no heartbeat in time.
    runner = RunWorker(JobQueue(worker_db, lease=timedelta(milliseconds=300)), worker, name="w1", heartbeat=60)
    try:
        executing = asyncio.ensure_future(runner.run_once())
        await asyncio.to_thread(called.wait, 10)
        await asyncio.sleep(0.5)
        assert [job.ref for job in reap(queue, api)] == [started.id]
        interrupted = api("alpha").runs.get(started.id)
        assert interrupted is not None and interrupted.status == "failed"
        assert interrupted.error_code == "run.interrupted"
    finally:
        release.set()
    await asyncio.wait_for(executing, 20)
    # The model answered after the reaper took the run: nothing of that late execution is stored.
    stored = api("alpha").runs.get(started.id)
    assert stored is not None and stored.status == "failed" and stored.error_code == "run.interrupted"
    assert stored.output is None
    assert [job.status for job in queue.jobs("alpha", RUN_JOB, started.id)] == ["interrupted"]
    events = PostgresEventLog(api_db, "alpha").events(started.id)
    assert "run_finished" not in [event.type for event in events]


async def test_a_worker_that_loses_its_lease_stops_its_run_without_storing_it(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    release, called = threading.Event(), threading.Event()
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"WRITE": [blocking(release, called)]})
    queue = JobQueue(api_db)
    started = await api_start(api, queue, "alpha", simple_flow())
    runner = RunWorker(JobQueue(worker_db), worker, name="w1", heartbeat=0.05, flush=0.02)
    try:
        executing = asyncio.ensure_future(runner.run_once())
        await asyncio.to_thread(called.wait, 10)
        # The lease expires (as if heartbeats had stalled) and another worker's reaper takes the run.
        api_db._write("UPDATE nodestro_jobs SET lease_until = clock_timestamp() - interval '1 second'", ())
        assert [job.ref for job in reap(queue, api)] == [started.id]
        # The worker notices at its next heartbeat and stops executing the run.
        await asyncio.sleep(0.3)
        seen = worker("alpha").store.get("runs", started.id, Run)
        assert seen is not None and seen.status == "failed"
    finally:
        release.set()
    await asyncio.wait_for(executing, 20)
    stored = api("alpha").runs.get(started.id)
    assert stored is not None and stored.status == "failed" and stored.error_code == "run.interrupted"
    assert [job.status for job in queue.jobs("alpha", RUN_JOB, started.id)] == ["interrupted"]
    assert started.id not in worker("alpha").runs._contexts


# ---- C-11: runs stored as live without a job --------------------------------------------------------------------


def _stored_live(workspace: Workspace, status: str = "running", **fields: Any) -> Run:
    saved = workspace.save_workflow(simple_flow())
    run = Run(
        workflow_id=saved.id,
        workflow_name=saved.name,
        workflow=saved,
        status=status,  # type: ignore[arg-type]
        nodes={"write": NodeRun(status="running" if status == "running" else "waiting")},
        **fields,
    )
    workspace.store.put("runs", run.id, run)
    return run


async def test_the_reaper_interrupts_runs_stored_as_live_without_a_job(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db = connect()
    api = processes(api_db)
    queue = JobQueue(api_db)
    imported = _stored_live(api("alpha"))  # e.g. imported from a desktop backup taken while it executed
    queued = _stored_live(api("beta"), "queued")  # its job never arrived
    child = _stored_live(api("alpha"), parent_run_id="run_parent", parent_node_id="sub")
    with_job = await api_start(api, queue, "alpha", simple_flow())

    assert orphaned_runs(queue, older_than=timedelta(hours=1)) == []  # just stored: may still be being queued
    assert set(orphaned_runs(queue, older_than=timedelta(0))) == {("alpha", imported.id), ("beta", queued.id)}
    reap(queue, api, orphans_after=timedelta(0))
    for tenant, run in (("alpha", imported), ("beta", queued)):
        stored = api(tenant).runs.get(run.id)
        assert stored is not None and stored.status == "failed" and stored.error_code == "run.interrupted"
    assert api("alpha").runs.get(child.id).status == "running"  # type: ignore[union-attr]  # follows its parent
    assert api("alpha").runs.get(with_job.id).status == "queued"  # type: ignore[union-attr]


async def test_cancelling_a_live_run_without_a_job_ends_it(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db = connect()
    api = processes(api_db)
    workspace = api("alpha")
    QueuedRuns(JobQueue(api_db), "alpha", workspace.runs).attach()
    imported = _stored_live(workspace)
    assert workspace.cancel(imported.id)
    stored = workspace.runs.get(imported.id)
    assert stored is not None and stored.status == "cancelled" and stored.nodes["write"].status == "cancelled"


# ---- C-08: a worker's lease renewal does not wait behind other work --------------------------------------------


async def test_a_worker_renews_its_lease_while_the_default_executor_is_busy(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"WRITE": [text("done")]})
    queue = JobQueue(api_db)
    started = await api_start(api, queue, "alpha", simple_flow())
    loop = asyncio.get_running_loop()
    busy = threading.Event()
    default = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(default)
    blocker = loop.run_in_executor(None, busy.wait, 20)  # the default executor is full (uploads, backups ...)
    try:
        job = await asyncio.wait_for(RunWorker(JobQueue(worker_db), worker, name="w1").run_once(), 10)
    finally:
        busy.set()
        await blocker
    assert job is not None
    stored = api("alpha").runs.get(started.id)
    assert stored is not None and stored.status == "succeeded"


async def test_a_sub_workflow_paused_in_a_worker_is_decided_once_and_continues(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"DRAFT": [text("draft")], "SEND": [text("sent")]})
    queue = JobQueue(api_db)
    workspace = api("alpha")
    QueuedRuns(queue, "alpha", workspace.runs).attach()
    child_flow = workspace.save_workflow(approval_flow())
    parent = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Parent",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {
                        "id": "team",
                        "type": "subworkflow",
                        "label": "Team",
                        "subworkflow": {"workflow_id": child_flow.id},
                    },
                ],
                "edges": [edge("start", "team")],
            }
        )
    )
    started = await workspace.run(parent, RunIn(input="x"))
    runner = RunWorker(JobQueue(worker_db), worker, name="w1")
    await runner.run_once()
    paused = workspace.runs.get(started.id)
    assert paused is not None and paused.status == "paused"
    child_id = paused.nodes["team"].child_run_id
    assert child_id is not None

    other = processes(connect())
    QueuedRuns(JobQueue(other.database), "alpha", other("alpha").runs).attach()

    def decided_elsewhere() -> None:
        workspace.decide(child_id, "check", "approved")

    other("alpha").runs.store = ReadThen(other("alpha").runs.store, decided_elsewhere)
    with pytest.raises(NodestroError) as refused:
        other("alpha").decide(child_id, "check", "rejected")
    assert refused.value.code in ("approval.not_pending", "run.not_paused")
    await runner.run_once()
    done = workspace.runs.get(started.id)
    assert done is not None and done.status == "succeeded", done.error
    child = workspace.runs.get(child_id)
    assert child is not None and child.status == "succeeded"
    assert child.nodes["check"].approval is not None and child.nodes["check"].approval.decision == "approved"
