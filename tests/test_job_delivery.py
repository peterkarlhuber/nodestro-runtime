"""Delivery semantics of the server's run queue (M11 / Axentra plan gap G-8). Real PostgreSQL only.

Nodestro's queue executes a run **at most once per job**: a job whose worker dies or loses its lease is never
claimed again; its run is marked ``run.interrupted`` and a person decides whether to continue it (a new run that
reuses every finished step). These tests back the architecture decision in ``docs/AXENTRA.md`` (Jobs): a model call
or a tool call with effects outside Nodestro is never repeated by the queue itself, not after a crash, not after a
lease loss while the old worker is still alive, not with parallel workers, not after cancel and resume, and not in
nested sub-workflows. Counters stand for money (model calls) and external writes (a tool's side effect).

Properties already proven elsewhere (not repeated here): one active job per run, ``SKIP LOCKED`` claims, owner-only
heartbeat and finish, reaped once and never claimed again, schedule slots and the leader lock (``test_jobs.py``);
a stalled or lease-losing worker never overwrites its run, one decision wins across processes, sub-workflow
decisions continue once (``test_run_fencing.py``); worker capacity slots, a killed worker while a question waits,
lease loss while waiting, load with three workers (``test_worker_approvals.py``); paused tool calls resumed exactly
once under racing decisions and sweeps (``test_worker_checkpoints.py``); continuing asks before tools run again
(``test_resume_side_effects.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import timedelta
import itertools
import multiprocessing
import os
from pathlib import Path
import signal
import threading
import time
from typing import Any

from orkexa import Tool
import postgres_support
import pytest

from nodestro_runtime import resume
from nodestro_runtime.extensions import Integration
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import Run, RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.workspace import Workspace

pytest.importorskip("psycopg")

from nodestro_runtime.jobs import RUN_JOB, JobQueue, QueuedRuns, RunWorker, reap
from nodestro_runtime.postgres_tables import tenant_storage
from nodestro_runtime.storage_postgres import PostgresDatabase

TOOL = "post_invoice"


class SideEffects:
    """The outside world of one test: every call of the tool that "posts an invoice" (an external write)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.lock = threading.Lock()
        #: Set by a test to hold the tool after its effect happened (the worker dies or stalls right after it).
        self.hold: threading.Event | None = None
        self.entered = threading.Event()

    def tool(self) -> Tool:
        def post_invoice(number: str) -> str:
            """Post an invoice to the accounting system (an external write).

            Args:
                number: The invoice number.

            Returns:
                A confirmation.
            """
            with self.lock:
                self.calls.append(number)
            self.entered.set()
            if self.hold is not None:
                self.hold.wait(20)
            return f"posted {number}"

        return Tool(post_invoice, name=TOOL)


class Counting:
    """Model answers per system prompt that count every model call (``calls[prompt]``)."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.lock = threading.Lock()

    def answer(self, prompt: str, respond: Callable[[dict[str, Any]], Any]) -> list[Any]:
        def counted(request: dict[str, Any]) -> Any:
            with self.lock:
                self.calls[prompt] = self.calls.get(prompt, 0) + 1
            return respond(request)

        return [counted] * 100


class Process:
    """The workspaces of one process (API or worker), one per tenant, with the side-effect tool installed."""

    def __init__(self, database: PostgresDatabase, root: Path, script: dict[str, list[Any]], world: SideEffects):
        self.database = database
        self.root = root
        self.client = ScriptedClient(script)
        self.world = world
        self.opened: dict[str, Workspace] = {}

    def __call__(self, tenant_id: str) -> Workspace:
        if tenant_id not in self.opened:
            self.opened[tenant_id] = Workspace(
                self.root / tenant_id,
                client_factory=lambda _provider: self.client,
                integrations=[Integration(name="accounting", tools=[self.world.tool()])],
                storage=tenant_storage(self.database, tenant_id),
            )
        return self.opened[tenant_id]

    def close(self) -> None:
        for workspace in self.opened.values():
            workspace.close()


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
def world() -> SideEffects:
    return SideEffects()


@pytest.fixture
def processes(tmp_path: Path, world: SideEffects) -> Iterator[Callable[..., Process]]:
    made: list[Process] = []

    def factory(database: PostgresDatabase, script: dict[str, list[Any]] | None = None) -> Process:
        process = Process(database, tmp_path / "files", script or {}, world)
        made.append(process)
        return process

    yield factory
    for process in made:
        process.close()


def agent(node_id: str, prompt: str, tools: list[str] | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"name": node_id, "system_prompt": prompt}
    if tools:
        spec["tools"] = tools
    return {"id": node_id, "type": "agent", "label": node_id, "agent": spec}


def edge(source: str, target: str) -> dict[str, Any]:
    return {"id": f"{source}-{target}", "source": source, "target": target}


def chain(name: str, *steps: dict[str, Any]) -> WorkflowIn:
    ids = ["start", *(step["id"] for step in steps)]
    return WorkflowIn.model_validate(
        {
            "name": name,
            "nodes": [{"id": "start", "type": "start"}, *steps],
            "edges": [edge(a, b) for a, b in itertools.pairwise(ids)],
        }
    )


def _answered(request: dict[str, Any]) -> bool:
    for item in request.get("messages", []):
        content = item.get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        ):
            return True
    return False


def invoicing(before_call: threading.Event | None = None) -> Callable[[dict[str, Any]], Any]:
    """The agent posts invoice 42 once, then answers; ``before_call`` holds the answer that asks for the call."""

    def respond(request: dict[str, Any]) -> Any:
        if _answered(request):
            return text("invoice posted")
        if before_call is not None:
            before_call.wait(20)
        return tool_use(TOOL, {"number": "42"})

    return respond


def held(release: threading.Event, started: threading.Event, answer: str = "late") -> Callable[[dict[str, Any]], Any]:
    def respond(_request: dict[str, Any]) -> Any:
        started.set()
        release.wait(20)
        return text(answer)

    return respond


async def api_start(api: Process, queue: JobQueue, flow: WorkflowIn, tenant: str = "alpha") -> Run:
    workspace = api(tenant)
    QueuedRuns(queue, tenant, workspace.runs).attach()
    return await workspace.run(workspace.save_workflow(flow), RunIn(input="x"))


async def until(api: Process, queue: JobQueue, run_id: str, statuses: tuple[str, ...], timeout: float = 20) -> Run:
    """The run once it has one of ``statuses`` and no active job."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = api("alpha").runs.get(run_id)
        assert found is not None
        if found.status in statuses and queue.active("alpha", RUN_JOB, run_id) is None:
            return found
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} is {api('alpha').runs.get(run_id).status}")  # type: ignore[union-attr]


def expire_leases(database: PostgresDatabase) -> None:
    """What a stalled heartbeat looks like to the reaper (a blocked loop, a database stall, a paused VM)."""
    database._write("UPDATE nodestro_jobs SET lease_until = clock_timestamp() - interval '1 second'", ())


# ---- a worker killed in the middle of a model call --------------------------------------------------------------


def _killed_worker(dsn: str, schema: str, root: str, counter: str) -> None:
    """A real worker process whose model call never returns; the test kills it (SIGKILL) during that call."""

    def respond(_request: dict[str, Any]) -> Any:
        with open(counter, "a", encoding="utf-8") as calls:
            calls.write("call\n")
        time.sleep(600)
        return text("never")

    database = PostgresDatabase(dsn, schema=schema)
    process = Process(database, Path(root), {"WRITE": [respond] * 10}, SideEffects())
    worker = RunWorker(JobQueue(database, lease=timedelta(milliseconds=500)), process, name="doomed", poll=0.05)
    asyncio.run(worker.serve(asyncio.Event()))


async def test_a_worker_killed_during_a_model_call_never_has_the_call_repeated(
    schema: tuple[str, str], connect: Callable[[], PostgresDatabase], processes: Callable[..., Process], tmp_path: Path
) -> None:
    api_db = connect()
    api = processes(api_db)
    queue = JobQueue(api_db, lease=timedelta(milliseconds=500))
    started = await api_start(api, queue, chain("Write", agent("write", "WRITE")))
    counter = tmp_path / "model-calls"
    child = multiprocessing.get_context("spawn").Process(
        target=_killed_worker, args=(schema[0], schema[1], str(tmp_path / "files"), str(counter))
    )
    child.start()
    try:
        deadline = time.monotonic() + 60
        while not counter.exists():
            assert time.monotonic() < deadline, "the worker never called the model"
            await asyncio.sleep(0.05)
    finally:
        assert child.pid is not None
        os.kill(child.pid, signal.SIGKILL)
        child.join(10)
    await asyncio.sleep(0.8)  # the lease expires

    # A healthy worker serves the same queue while the reaper runs: it never picks the run up again.
    counting = Counting()
    worker_db = connect()
    healthy = processes(worker_db, {"WRITE": counting.answer("WRITE", lambda _r: text("again"))})
    rescuer = RunWorker(JobQueue(worker_db), healthy, name="rescuer", poll=0.05)
    stop = asyncio.Event()
    serving = asyncio.ensure_future(rescuer.serve(stop))
    try:
        assert [job.ref for job in reap(queue, api)] == [started.id]
        await asyncio.sleep(0.5)
    finally:
        stop.set()
        await asyncio.wait_for(serving, 20)
    interrupted = api("alpha").runs.get(started.id)
    assert interrupted is not None and interrupted.status == "failed" and interrupted.error_code == "run.interrupted"
    assert counter.read_text().count("call") == 1
    assert counting.calls == {}
    assert [(job.status, job.attempts, job.worker) for job in queue.jobs("alpha", RUN_JOB, started.id)] == [
        ("interrupted", 1, "doomed")
    ]


# ---- lease loss while the old worker is still alive ---------------------------------------------------------------


async def test_a_stalled_worker_makes_no_external_call_after_its_run_was_interrupted(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process], world: SideEffects
) -> None:
    """The worker's heartbeat stalls while its model call is in flight; the reaper interrupts the run. When the
    model then asks for the tool, the old worker must not post the invoice: the run is no longer its own."""
    release = threading.Event()
    counting = Counting()
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"BILL": counting.answer("BILL", invoicing(release))})
    queue = JobQueue(api_db, lease=timedelta(milliseconds=300))
    started = await api_start(api, queue, chain("Bill", agent("bill", "BILL", [TOOL])))
    runner = RunWorker(JobQueue(worker_db, lease=timedelta(milliseconds=300)), worker, name="old", heartbeat=60)
    try:
        executing = asyncio.ensure_future(runner.run_once())
        deadline = time.monotonic() + 10
        while counting.calls.get("BILL") != 1:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.5)
        assert [job.ref for job in reap(queue, api)] == [started.id]
    finally:
        release.set()  # the model answers late: "post invoice 42"
    await asyncio.wait_for(executing, 20)
    assert world.calls == []
    stored = api("alpha").runs.get(started.id)
    assert stored is not None and stored.status == "failed" and stored.error_code == "run.interrupted"
    assert counting.calls == {"BILL": 1}  # no further model call either
    assert [job.status for job in queue.jobs("alpha", RUN_JOB, started.id)] == ["interrupted"]


async def test_an_external_write_before_a_lost_lease_is_never_repeated_without_a_person(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process], world: SideEffects
) -> None:
    """The invoice is posted, then the worker loses its lease before the node is stored. The queue never posts it
    again; continuing the run asks first, and only an explicit confirmation calls the tool a second time."""
    world.hold = threading.Event()
    counting = Counting()
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"BILL": counting.answer("BILL", invoicing())})
    queue = JobQueue(api_db)
    started = await api_start(api, queue, chain("Bill", agent("bill", "BILL", [TOOL])))
    runner = RunWorker(JobQueue(worker_db), worker, name="old", heartbeat=0.05, flush=0.02)
    try:
        executing = asyncio.ensure_future(runner.run_once())
        assert await asyncio.to_thread(world.entered.wait, 10)
        expire_leases(api_db)
        assert [job.ref for job in reap(queue, api)] == [started.id]
        await asyncio.sleep(0.3)  # the old worker notices at its next heartbeat
    finally:
        world.hold.set()
    await asyncio.wait_for(executing, 20)
    assert world.calls == ["42"]
    interrupted = api("alpha").runs.get(started.id)
    assert interrupted is not None and interrupted.error_code == "run.interrupted"
    assert queue.claim("anyone") is None

    found = resume.plan(api("alpha"), started.id)
    assert found.needs_confirmation and [n.side_effects for n in found.nodes if n.node_id == "bill"] == [[TOOL]]
    with pytest.raises(NodestroError) as refused:
        await api("alpha").resume(started.id)
    assert refused.value.code == "resume.confirm_side_effects"
    assert world.calls == ["42"]
    world.hold = None
    continued = await api("alpha").resume(started.id, confirm=True)
    await RunWorker(JobQueue(worker_db), worker, name="next").run_once()
    done = await until(api, queue, continued.id, ("succeeded",))
    assert done.status == "succeeded" and world.calls == ["42", "42"]  # the second one only after the confirmation


# ---- parallel workers -------------------------------------------------------------------------------------------


async def test_parallel_workers_execute_every_run_exactly_once(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process], world: SideEffects
) -> None:
    counting = Counting()
    api_db = connect()
    api = processes(api_db)
    queue = JobQueue(api_db)
    flow = chain("Bill", agent("bill", "BILL", [TOOL]))
    runs = [await api_start(api, queue, flow) for _ in range(12)]
    workers: list[tuple[RunWorker, PostgresDatabase]] = []
    for index in range(4):
        database = connect()
        process = processes(database, {"BILL": counting.answer("BILL", invoicing())})
        workers.append((RunWorker(JobQueue(database), process, name=f"w{index}", poll=0.02, flush=0.02), database))
    go = threading.Barrier(len(workers))

    def serve(worker: RunWorker) -> int:
        async def drain() -> int:
            go.wait(10)  # every worker starts claiming at the same moment
            done = 0
            while await worker.run_once() is not None:
                done += 1
            return done

        return asyncio.run(drain())

    executed = await asyncio.gather(*(asyncio.to_thread(serve, worker) for worker, _db in workers))
    assert sum(executed) == len(runs)
    for run in runs:
        stored = api("alpha").runs.get(run.id)
        assert stored is not None and stored.status == "succeeded", stored.error if stored else None
        jobs = queue.jobs("alpha", RUN_JOB, run.id)
        assert [(job.status, job.attempts) for job in jobs] == [("done", 1)]
    assert len(world.calls) == len(runs)  # one invoice per run
    assert counting.calls == {"BILL": 2 * len(runs)}  # the call and the answer after it, per run


# ---- cancel and resume ------------------------------------------------------------------------------------------


async def test_cancel_and_resume_never_repeat_a_finished_step(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process], world: SideEffects
) -> None:
    release, writing = threading.Event(), threading.Event()
    counting = Counting()
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(
        worker_db,
        {
            "BILL": counting.answer("BILL", invoicing()),
            "SUMMARY": counting.answer("SUMMARY", held(release, writing, "summary")),
        },
    )
    queue = JobQueue(api_db)
    flow = chain("Bill then sum up", agent("bill", "BILL", [TOOL]), agent("sum", "SUMMARY"))
    started = await api_start(api, queue, flow)
    runner = RunWorker(JobQueue(worker_db), worker, name="w1", heartbeat=0.05, flush=0.02)
    try:
        executing = asyncio.ensure_future(runner.run_once())
        assert await asyncio.to_thread(writing.wait, 10)
        assert api("alpha").cancel(started.id)  # another process cancels; the worker reads it with its heartbeat
        await asyncio.sleep(0.3)
    finally:
        release.set()  # the model answers after the cancellation
    await asyncio.wait_for(executing, 20)
    cancelled = await until(api, queue, started.id, ("cancelled",))
    assert cancelled.status == "cancelled"
    assert [job.status for job in queue.jobs("alpha", RUN_JOB, started.id)] == ["cancelled"]
    assert queue.claim("anyone") is None  # a cancelled run is never picked up again

    continued = await api("alpha").resume(started.id)  # the invoice step is reused: nothing to confirm
    await RunWorker(JobQueue(worker_db), worker, name="w2").run_once()
    done = await until(api, queue, continued.id, ("succeeded",))
    assert done.nodes["bill"].reused_from == started.id
    assert world.calls == ["42"]
    assert counting.calls == {"BILL": 2, "SUMMARY": 2}  # the cancelled summary ran again; nothing else did
    assert [job.status for job in queue.jobs("alpha", RUN_JOB, continued.id)] == ["done"]


# ---- nested sub-workflows ---------------------------------------------------------------------------------------


async def test_a_sub_workflow_of_a_worker_that_lost_its_lease_stores_and_calls_nothing_more(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process], world: SideEffects
) -> None:
    release, writing = threading.Event(), threading.Event()
    counting = Counting()
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(
        worker_db,
        {
            "INNER": counting.answer("INNER", held(release, writing, "inner done")),
            "BILL": counting.answer("BILL", invoicing()),
        },
    )
    queue = JobQueue(api_db)
    workspace = api("alpha")
    QueuedRuns(queue, "alpha", workspace.runs).attach()
    inner = workspace.save_workflow(chain("Inner", agent("inner", "INNER")))
    middle = workspace.save_workflow(
        chain(
            "Middle", {"id": "team", "type": "subworkflow", "label": "Team", "subworkflow": {"workflow_id": inner.id}}
        )
    )
    outer = workspace.save_workflow(
        chain(
            "Outer",
            {"id": "dept", "type": "subworkflow", "label": "Dept", "subworkflow": {"workflow_id": middle.id}},
            agent("bill", "BILL", [TOOL]),
        )
    )
    started = await workspace.run(outer, RunIn(input="x"))
    runner = RunWorker(JobQueue(worker_db), worker, name="w1", heartbeat=0.05, flush=0.02)
    try:
        executing = asyncio.ensure_future(runner.run_once())
        assert await asyncio.to_thread(writing.wait, 10)
        expire_leases(api_db)
        assert [job.ref for job in reap(queue, api)] == [started.id]
        await asyncio.sleep(0.3)
    finally:
        release.set()  # the innermost agent answers after the lease was lost
    await asyncio.wait_for(executing, 20)

    top = workspace.runs.get(started.id)
    assert top is not None and top.status == "failed" and top.error_code == "run.interrupted"
    middle_id = top.nodes["dept"].child_run_id
    assert middle_id is not None
    middle_run = workspace.runs.get(middle_id)
    assert middle_run is not None and middle_run.status == "failed" and middle_run.error_code == "run.interrupted"
    inner_id = middle_run.nodes["team"].child_run_id
    assert inner_id is not None
    inner_run = workspace.runs.get(inner_id)
    assert inner_run is not None and inner_run.status == "failed" and inner_run.output is None
    assert counting.calls == {"INNER": 1}  # the step after the sub-workflows never ran
    assert world.calls == []
    assert queue.claim("anyone") is None


async def test_a_failing_ownership_check_makes_no_external_call(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process], world: SideEffects
) -> None:
    """When the queue cannot be asked whether the worker still holds the run (a database error), the tool call is
    not made: an effect outside Nodestro is never risked on a guess."""
    counting = Counting()
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    worker = processes(worker_db, {"BILL": counting.answer("BILL", invoicing())})
    queue = JobQueue(api_db)
    started = await api_start(api, queue, chain("Bill", agent("bill", "BILL", [TOOL])))
    runner = RunWorker(JobQueue(worker_db), worker, name="w1", heartbeat=0.05, flush=0.02)
    asked: list[str] = []

    def unreachable(tenant_id: str, run_id: str) -> bool:
        asked.append(run_id)
        if len(asked) > 1:  # the first model request passes; the check before the tool call fails
            raise ConnectionError("the database is gone")
        return True

    runner.owns = unreachable  # type: ignore[method-assign]
    await asyncio.wait_for(runner.run_once(), 20)
    assert asked == [started.id, started.id]
    assert world.calls == []
    assert counting.calls == {"BILL": 1}
    assert [job.status for job in queue.jobs("alpha", RUN_JOB, started.id)] == ["failed"]
    assert queue.claim("anyone") is None
