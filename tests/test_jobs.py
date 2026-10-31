"""Job queue, workers, reaper, run events and the scheduler lock on PostgreSQL (needs ``NODESTRO_TEST_POSTGRES``)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import time
from typing import Any

import postgres_support
import pytest

from nodestro_runtime.schemas import Run, RunEvent, RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text
from nodestro_runtime.workspace import Workspace

pytest.importorskip("psycopg")

from nodestro_runtime.jobs import (
    RUN_JOB,
    JobQueue,
    LeaderLock,
    PostgresEventLog,
    QueuedRuns,
    RunWorker,
    ScheduleFirings,
    reap,
)
from nodestro_runtime.postgres_tables import tenant_storage
from nodestro_runtime.storage_postgres import PostgresDatabase


@pytest.fixture
def schema() -> Iterator[tuple[str, str]]:
    dsn = postgres_support.require_postgres()
    name = postgres_support.new_schema()
    database = PostgresDatabase(dsn, schema=name)
    database.migrate()
    database.close()
    yield dsn, name
    postgres_support.drop_schema(name)


@pytest.fixture
def connect(schema: tuple[str, str]) -> Iterator[Callable[[], PostgresDatabase]]:
    """A new connection to the test schema (each one stands for a process)."""
    opened: list[PostgresDatabase] = []

    def factory() -> PostgresDatabase:
        database = PostgresDatabase(schema[0], schema=schema[1])
        opened.append(database)
        return database

    yield factory
    for database in opened:
        database.close()


@pytest.fixture
def database(connect: Callable[[], PostgresDatabase]) -> PostgresDatabase:
    return connect()


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

    def close(self) -> None:
        for workspace in self.opened.values():
            workspace.close()


@pytest.fixture
def processes(tmp_path: Path) -> Iterator[Callable[..., Process]]:
    made: list[Process] = []

    def factory(database: PostgresDatabase, script: dict[str, list[Any]] | None = None) -> Process:
        process = Process(database, tmp_path / "files", script or {})
        made.append(process)
        return process

    yield factory
    for process in made:
        process.close()


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


def blocking(release: threading.Event, started: threading.Event | None = None) -> Callable[[dict[str, Any]], Any]:
    def respond(_request: dict[str, Any]) -> Any:
        if started is not None:
            started.set()
        release.wait(10)
        return text("late")

    return respond


async def api_start(api: Process, queue: JobQueue, tenant: str, flow: WorkflowIn) -> Run:
    workspace = api(tenant)
    QueuedRuns(queue, tenant, workspace.runs).attach()
    saved = workspace.save_workflow(flow)
    return await workspace.run(saved, RunIn(input="x"))


# ---- the queue -------------------------------------------------------------------------------


def test_a_piece_of_work_has_at_most_one_active_job(database: PostgresDatabase) -> None:
    queue = JobQueue(database)
    first = queue.enqueue("alpha", RUN_JOB, "run_1")
    assert first is not None and first.status == "queued"
    assert queue.enqueue("alpha", RUN_JOB, "run_1") is None
    assert queue.enqueue("beta", RUN_JOB, "run_1") is not None  # another tenant's work
    claimed = queue.claim("w1")
    assert claimed is not None and claimed.id == first.id and claimed.status == "running" and claimed.attempts == 1
    assert queue.enqueue("alpha", RUN_JOB, "run_1") is None  # still active while it runs
    assert queue.finish(claimed.id, "w1", "done")
    again = queue.enqueue("alpha", RUN_JOB, "run_1")  # e.g. continued after an approval
    assert again is not None and again.id != first.id


def test_concurrent_workers_never_claim_the_same_job(connect: Callable[[], PostgresDatabase]) -> None:
    setup = JobQueue(connect())
    for index in range(30):
        setup.enqueue("alpha", RUN_JOB, f"run_{index}")
    claimed: dict[str, list[int]] = {}
    barrier = threading.Barrier(4)

    def work(name: str) -> None:
        queue = JobQueue(connect())
        mine = claimed.setdefault(name, [])
        barrier.wait()
        while (job := queue.claim(name)) is not None:
            mine.append(job.id)

    threads = [threading.Thread(target=work, args=(f"w{index}",)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    every = [job_id for ids in claimed.values() for job_id in ids]
    assert len(every) == 30 and len(set(every)) == 30


def test_only_the_owner_renews_or_finishes_a_job(database: PostgresDatabase) -> None:
    queue = JobQueue(database)
    queue.enqueue("alpha", RUN_JOB, "run_1")
    job = queue.claim("w1")
    assert job is not None
    assert queue.heartbeat(job.id, "w2") is None
    assert not queue.finish(job.id, "w2", "done")
    beat = queue.heartbeat(job.id, "w1")
    assert beat is not None and beat.lease_until is not None and job.lease_until is not None
    assert beat.lease_until >= job.lease_until
    with pytest.raises(ValueError):
        queue.finish(job.id, "w1", "interrupted")


def test_cancelling_withdraws_a_queued_job_and_flags_a_running_one(database: PostgresDatabase) -> None:
    queue = JobQueue(database)
    queue.enqueue("alpha", RUN_JOB, "queued")
    withdrawn = queue.request_cancel("alpha", RUN_JOB, "queued")
    assert withdrawn is not None and withdrawn.status == "cancelled"
    assert queue.claim("w1") is None
    queue.enqueue("alpha", RUN_JOB, "running")
    job = queue.claim("w1")
    assert job is not None
    flagged = queue.request_cancel("alpha", RUN_JOB, "running")
    assert flagged is not None and flagged.status == "running" and flagged.cancel_requested
    beat = queue.heartbeat(job.id, "w1")
    assert beat is not None and beat.cancel_requested
    assert queue.request_cancel("alpha", RUN_JOB, "unknown") is None
    assert queue.request_cancel("beta", RUN_JOB, "running") is None  # no cross-tenant cancelling


def test_an_expired_lease_is_reaped_once_and_never_claimed_again(database: PostgresDatabase) -> None:
    queue = JobQueue(database, lease=timedelta(milliseconds=50))
    queue.enqueue("alpha", RUN_JOB, "run_1")
    job = queue.claim("dead-worker")
    assert job is not None
    time.sleep(0.2)
    reaped = queue.reap_expired()
    assert [item.id for item in reaped] == [job.id] and reaped[0].status == "interrupted"
    assert queue.reap_expired() == []
    assert queue.claim("w2") is None
    assert queue.heartbeat(job.id, "dead-worker") is None
    assert not queue.finish(job.id, "dead-worker", "done")


# ---- workers execute runs that API processes queue --------------------------------------------


async def test_a_worker_executes_a_queued_run_and_streams_its_events(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api, worker = processes(api_db), processes(worker_db, {"WRITE": [text("done")]})
    queue = JobQueue(api_db)
    started = await api_start(api, queue, "alpha", simple_flow())
    assert started.status == "queued"
    assert started.id not in api("alpha").runs._tasks  # nothing executes in the API process
    job = queue.active("alpha", RUN_JOB, started.id)
    assert job is not None and job.status == "queued"

    finished_job = await RunWorker(JobQueue(worker_db), worker, name="w1").run_once()
    assert finished_job is not None
    run = api("alpha").runs.get(started.id)
    assert run is not None and run.status == "succeeded" and run.output == "done"
    assert [item.status for item in queue.jobs("alpha", RUN_JOB, started.id)] == ["done"]

    events = PostgresEventLog(api_db, "alpha").events(started.id)
    assert events[-1].type == "run_finished"
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert PostgresEventLog(api_db, "alpha").events(started.id, after=events[-2].seq) == [events[-1]]
    assert PostgresEventLog(api_db, "beta").events(started.id) == []  # other tenants see nothing


async def test_a_decision_queues_the_rest_of_the_run(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"DRAFT": [text("draft")], "SEND": [text("sent")]})
    queue = JobQueue(api_db)
    runner = RunWorker(JobQueue(worker_db), worker, name="w1")
    started = await api_start(api, queue, "alpha", approval_flow())
    await runner.run_once()
    paused = api("alpha").runs.get(started.id)
    assert paused is not None and paused.status == "paused"

    api("alpha").decide(started.id, "check", "approved")
    assert queue.active("alpha", RUN_JOB, started.id) is not None
    await runner.run_once()
    done = api("alpha").runs.get(started.id)
    assert done is not None and done.status == "succeeded" and done.output == "sent"
    assert [item.status for item in queue.jobs("alpha", RUN_JOB, started.id)] == ["done", "done"]
    seqs = [event.seq for event in PostgresEventLog(api_db, "alpha").events(started.id)]
    assert seqs == list(range(1, len(seqs) + 1))  # numbering continues across workers and pauses


async def test_another_process_cancels_a_run_a_worker_executes(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    release, started_call = threading.Event(), threading.Event()
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"WRITE": [blocking(release, started_call)]})
    queue = JobQueue(api_db)
    started = await api_start(api, queue, "alpha", simple_flow())
    runner = RunWorker(JobQueue(worker_db), worker, name="w1", heartbeat=0.05, flush=0.02)
    try:
        executing = asyncio.ensure_future(runner.run_once())
        await asyncio.to_thread(started_call.wait, 10)
        assert api("alpha").cancel(started.id)
        await asyncio.wait_for(executing, 20)
    finally:
        release.set()
    run = api("alpha").runs.get(started.id)
    assert run is not None and run.status == "cancelled"
    assert [item.status for item in queue.jobs("alpha", RUN_JOB, started.id)] == ["cancelled"]


async def test_other_processes_see_a_run_running_while_a_worker_executes_it(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    release, started_call = threading.Event(), threading.Event()
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"WRITE": [blocking(release, started_call)]})
    queue = JobQueue(api_db)
    started = await api_start(api, queue, "alpha", simple_flow())
    runner = RunWorker(JobQueue(worker_db), worker, name="w1", heartbeat=0.05, flush=0.02)
    try:
        executing = asyncio.ensure_future(runner.run_once())
        await asyncio.to_thread(started_call.wait, 10)
        seen = api("alpha").runs.get(started.id)  # read from the database, like an API process
        assert seen is not None and seen.status == "running" and seen.started_at is not None
    finally:
        release.set()
    await asyncio.wait_for(executing, 20)
    done = api("alpha").runs.get(started.id)
    assert done is not None and done.status == "succeeded"


async def test_cancelling_a_queued_run_withdraws_its_job(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db = connect()
    api = processes(api_db)
    queue = JobQueue(api_db)
    started = await api_start(api, queue, "alpha", simple_flow())
    assert api("alpha").cancel(started.id)
    run = api("alpha").runs.get(started.id)
    assert run is not None and run.status == "cancelled"
    assert queue.claim("w1") is None


async def test_a_dead_workers_run_is_interrupted_and_never_executed_twice(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"WRITE": [text("must not run")]})
    queue = JobQueue(api_db, lease=timedelta(milliseconds=50))
    started = await api_start(api, queue, "alpha", simple_flow())
    # A worker claims the job and dies before it executes anything (no heartbeat ever arrives).
    assert queue.claim("dead-worker") is not None
    await asyncio.sleep(0.2)
    reaped = reap(queue, api)
    assert [job.ref for job in reaped] == [started.id]
    run = api("alpha").runs.get(started.id)
    assert run is not None and run.status == "failed" and run.error_code == "run.interrupted"
    assert await RunWorker(JobQueue(worker_db), worker, name="w2").run_once() is None
    assert len(worker.client.messages.script["WRITE"]) == 1  # the model was never called


async def test_a_worker_refuses_a_job_whose_run_is_not_executable(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api, worker = processes(api_db), processes(worker_db)
    queue = JobQueue(api_db)
    started = await api_start(api, queue, "alpha", simple_flow())
    stored = api("alpha").runs.get(started.id)
    assert stored is not None
    stored.status = "succeeded"
    api("alpha").store.put("runs", stored.id, stored)
    await RunWorker(JobQueue(worker_db), worker, name="w1").run_once()
    job = queue.jobs("alpha", RUN_JOB, started.id)[0]
    assert job.status == "failed" and "run.not_executable" in (job.error or "")


async def test_tenant_jobs_execute_in_their_own_tenant(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"WRITE": [text("one"), text("two")]})
    queue = JobQueue(api_db)
    alpha_run = await api_start(api, queue, "alpha", simple_flow())
    beta_run = await api_start(api, queue, "beta", simple_flow())
    runner = RunWorker(JobQueue(worker_db), worker, name="w1")
    await runner.run_once()
    await runner.run_once()
    assert api("alpha").runs.get(beta_run.id) is None and api("beta").runs.get(alpha_run.id) is None
    for tenant, run in (("alpha", alpha_run), ("beta", beta_run)):
        stored = api(tenant).runs.get(run.id)
        assert stored is not None and stored.status == "succeeded"
    assert PostgresEventLog(api_db, "alpha").events(beta_run.id) == []


# ---- workspaces of several processes ---------------------------------------------------------


async def test_opening_a_tenant_workspace_leaves_other_processes_runs_alone(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    first = processes(connect())
    queue = JobQueue(first.database)
    started = await api_start(first, queue, "alpha", simple_flow())
    stored = first("alpha").runs.get(started.id)
    assert stored is not None
    stored.status = "running"
    first("alpha").store.put("runs", stored.id, stored)
    # A second process opens the same tenant (same file area): no directory lock, no recovery of the run.
    second = processes(connect())
    seen = second("alpha").runs.get(started.id)
    assert seen is not None and seen.status == "running"


# ---- events, scheduler leader, schedule firings ----------------------------------------------


def test_events_are_buffered_and_written_by_flush(database: PostgresDatabase) -> None:
    log = PostgresEventLog(database, "alpha", max_buffer=3)
    log.append(RunEvent(seq=1, run_id="r", type="text", data={"delta": "a\u0000b"}))
    assert log.events("r") == []
    log.append(RunEvent(seq=2, run_id="r", type="text"))
    log.append(RunEvent(seq=3, run_id="r", type="text"))  # the buffer is full
    assert [event.seq for event in log.events("r")] == [1, 2, 3]
    assert log.events("r")[0].data == {"delta": "a\u0000b"}  # stored exactly
    log.append(RunEvent(seq=4, run_id="r", type="run_finished"))  # written at once
    assert [event.seq for event in log.events("r")] == [1, 2, 3, 4]
    log.append(RunEvent(seq=4, run_id="r", type="run_finished"))
    assert len(log.events("r")) == 4  # a repeated event is stored once
    assert log.delete("r") == 4


def test_only_one_process_leads_the_scheduler(connect: Callable[[], PostgresDatabase]) -> None:
    first_db, second_db = connect(), connect()
    first, second = LeaderLock(first_db), LeaderLock(second_db)
    assert first.acquire() and first.acquire()
    assert not second.acquire()
    first_db.close()  # the leader dies: its connection ends and so does its lock
    deadline = time.monotonic() + 5
    while not second.acquire():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    second.release()
    assert not second.held


def test_every_schedule_slot_fires_once(connect: Callable[[], PostgresDatabase]) -> None:
    slot = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
    results: list[bool] = []
    barrier = threading.Barrier(5)

    def fire() -> None:
        firings = ScheduleFirings(connect(), "alpha")
        barrier.wait()
        results.append(firings.claim("daily", slot))

    threads = [threading.Thread(target=fire) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(results) == [False, False, False, False, True]
    firings = ScheduleFirings(connect(), "alpha")
    firings.record_run("daily", slot, "run_1")
    assert firings.fired("daily") == [(slot, "run_1")]
    assert ScheduleFirings(connect(), "beta").claim("daily", slot)  # each tenant has its own schedules
    with pytest.raises(ValueError):
        firings.claim("daily", datetime(2026, 10, 31, 9, 0))


# ---- API processes follow runs that workers execute ------------------------------------------


async def test_an_api_process_streams_and_waits_for_a_workers_run(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    release, started_call = threading.Event(), threading.Event()
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"WRITE": [blocking(release, started_call)]})
    queue = JobQueue(api_db)
    started = await api_start(api, queue, "alpha", simple_flow())
    runs = api("alpha").runs
    runs.remote_poll = 0.02
    assert runs.is_live(started.id)  # queued in the database, not in this process
    history, stream = runs.subscribe(started.id)
    assert history == []
    waiting = asyncio.ensure_future(runs.wait(started.id))
    executing = asyncio.ensure_future(RunWorker(JobQueue(worker_db), worker, name="w1", flush=0.02).run_once())
    try:
        await asyncio.to_thread(started_call.wait, 10)
        await asyncio.sleep(0.1)
        assert not waiting.done()
    finally:
        release.set()
    received: list[RunEvent] = []
    while (event := await asyncio.wait_for(stream.get(), 10)) is not None:
        received.append(event)
    runs.unsubscribe(started.id, stream)
    await asyncio.wait_for(executing, 10)
    done = await asyncio.wait_for(waiting, 10)
    assert done is not None and done.status == "succeeded"
    assert received[0].type == "run_started" and received[-1].type == "run_finished"
    assert [event.seq for event in received] == list(range(1, len(received) + 1))
    assert not runs.is_live(started.id)


async def test_a_stream_of_a_withdrawn_run_ends(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db = connect()
    api = processes(api_db)
    started = await api_start(api, JobQueue(api_db), "alpha", simple_flow())
    runs = api("alpha").runs
    runs.remote_poll = 0.02
    _history, stream = runs.subscribe(started.id)
    assert runs.cancel(started.id)
    assert await asyncio.wait_for(stream.get(), 10) is None
    runs.unsubscribe(started.id, stream)
    assert runs._followers == {}


async def test_a_decided_run_is_queued_again_and_waited_for(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"DRAFT": [text("draft")], "SEND": [text("sent")]})
    runner = RunWorker(JobQueue(worker_db), worker, name="w1")
    started = await api_start(api, JobQueue(api_db), "alpha", approval_flow())
    runs = api("alpha").runs
    runs.remote_poll = 0.02
    await runner.run_once()
    decided = api("alpha").decide(started.id, "check", "approved")
    assert decided.status == "queued" and runs.is_live(started.id)
    waiting = asyncio.ensure_future(runs.wait(started.id))
    await asyncio.sleep(0.1)
    assert not waiting.done()
    await runner.run_once()
    done = await asyncio.wait_for(waiting, 10)
    assert done is not None and done.status == "succeeded"
