"""Server workers with waiting tool approvals (S-2a step 3; concept nodestro-studio
``docs/evolution/s2-worker-concept.md`` §6, tests 1-12). Real PostgreSQL only: every test fails without it when
``NODESTRO_REQUIRE_POSTGRES=1`` (security tests that are skipped do not count as passed).

API processes and workers are separate workspaces on separate database connections (one stands for one process);
test 9 kills a real worker process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
import contextlib
from datetime import timedelta
import multiprocessing
import os
from pathlib import Path
import random
import signal
import threading
import time
from typing import Any

from orkexa import Tool
import postgres_support
import pytest

from nodestro_runtime.extensions import Integration
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import Run, RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import ToolApprovalRequest, ToolApprovals, ToolGate, WaitingSlots
from nodestro_runtime.workspace import Workspace

pytest.importorskip("psycopg")

from nodestro_runtime.jobs import RUN_JOB, JobQueue, QueuedRuns, RunWorker, check_capacity, reap
from nodestro_runtime.postgres_tables import tenant_storage
from nodestro_runtime.storage_postgres import PostgresDatabase

TOOL = "send_note"


def send_note(value: str) -> str:
    """Send a note (a writing tool without a capability statement: it asks before every call).

    Args:
        value: The note.

    Returns:
        A confirmation.
    """
    return f"sent {value}"


def notes() -> Integration:
    return Integration(name="notes", tools=[Tool(send_note, name=TOOL)])


def _answered(request: dict[str, Any]) -> bool:
    for item in request.get("messages", []):
        content = item.get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        ):
            return True
    return False


def asking(after: threading.Event | None = None) -> Callable[[dict[str, Any]], Any]:
    """Calls the tool once, then answers (after ``after`` is set, if given)."""

    def respond(request: dict[str, Any]) -> Any:
        if not _answered(request):
            return tool_use(TOOL, {"value": "hi"})
        if after is not None:
            after.wait(20)
        return text("done")

    return respond


def blocking(release: threading.Event) -> Callable[[dict[str, Any]], Any]:
    def respond(_request: dict[str, Any]) -> Any:
        release.wait(20)
        return text("slow done")

    return respond


def script(**entries: Callable[[dict[str, Any]], Any]) -> dict[str, list[Any]]:
    return {key: [respond] * 200 for key, respond in entries.items()}


class Process:
    """The workspaces of one process (API or worker), one per tenant, with per-call approvals switched on."""

    def __init__(self, database: PostgresDatabase, root: Path, entries: dict[str, list[Any]], wait: timedelta):
        self.database = database
        self.root = root
        self.client = ScriptedClient(entries)
        self.wait = wait
        self.opened: dict[str, Workspace] = {}

    def __call__(self, tenant_id: str) -> Workspace:
        if tenant_id not in self.opened:
            workspace = Workspace(
                self.root / tenant_id,
                client_factory=lambda _provider: self.client,
                integrations=[notes()],
                storage=tenant_storage(self.database, tenant_id),
            )
            workspace.runs.tool_gate = ToolGate(ToolApprovals(workspace.store, wait=self.wait, poll_seconds=0.05))
            self.opened[tenant_id] = workspace
        return self.opened[tenant_id]

    def approvals(self, tenant_id: str) -> ToolApprovals:
        return ToolApprovals(self(tenant_id).store)

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
    opened: list[PostgresDatabase] = []

    def factory() -> PostgresDatabase:
        database = PostgresDatabase(schema[0], schema=schema[1])
        opened.append(database)
        return database

    yield factory
    for database in opened:
        database.close()


@pytest.fixture
def processes(tmp_path: Path) -> Iterator[Callable[..., Process]]:
    made: list[Process] = []

    def factory(
        database: PostgresDatabase, entries: dict[str, list[Any]] | None = None, *, wait: float = 900.0
    ) -> Process:
        process = Process(database, tmp_path / "files", entries or {}, timedelta(seconds=wait))
        made.append(process)
        return process

    yield factory
    for process in made:
        process.close()


def flow(prompt: str, name: str | None = None) -> WorkflowIn:
    agent = {"name": "Agent", "system_prompt": prompt, "tools": [TOOL]}
    return WorkflowIn.model_validate(
        {
            "name": name or prompt.title(),
            "nodes": [{"id": "start", "type": "start"}, {"id": "a", "type": "agent", "agent": agent}],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )


class Api:
    def __init__(self, process: Process, queue: JobQueue) -> None:
        self.process = process
        self.queue = queue
        self.saved: dict[tuple[str, str], Any] = {}

    async def start(self, prompt: str, tenant: str = "alpha") -> Run:
        workspace = self.process(tenant)
        QueuedRuns(self.queue, tenant, workspace.runs).attach()
        key = (tenant, prompt)
        if key not in self.saved:
            self.saved[key] = workspace.save_workflow(flow(prompt))
        return await workspace.run(self.saved[key], RunIn(input="x"))

    def run(self, run_id: str, tenant: str = "alpha") -> Run:
        found = self.process(tenant).runs.get(run_id)
        assert found is not None
        return found

    def job_status(self, run_id: str, tenant: str = "alpha") -> str | None:
        job = self.queue.jobs(tenant, RUN_JOB, run_id)
        return job[-1].status if job else None

    async def pending(self, run_id: str, tenant: str = "alpha", timeout: float = 15) -> ToolApprovalRequest:
        approvals = self.process.approvals(tenant)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = approvals.pending(run_id)
            if found:
                return found[0]
            await asyncio.sleep(0.05)
        raise AssertionError(f"run {run_id} never asked")

    def decide(self, request: ToolApprovalRequest, approved: bool = True, tenant: str = "alpha") -> None:
        self.process.approvals(tenant).decide(
            request.run_id, request.id, approved=approved, by="usr_1", arguments=request.arguments_hash
        )

    async def until(self, run_id: str, statuses: tuple[str, ...], tenant: str = "alpha", timeout: float = 20) -> Run:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.run(run_id, tenant)
            if found.status in statuses and self.job_status(run_id, tenant) not in ("queued", "running"):
                return found
            await asyncio.sleep(0.05)
        raise AssertionError(f"run {run_id} is {self.run(run_id, tenant).status}")


class Serving:
    """A worker serving in the background of the test's event loop."""

    def __init__(self, worker: RunWorker) -> None:
        self.worker = worker
        self.stop = asyncio.Event()
        self.task = asyncio.ensure_future(worker.serve(self.stop))

    async def close(self) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, 30)


def worker_of(process: Process, database: PostgresDatabase, name: str = "w1", **options: Any) -> RunWorker:
    lease = options.pop("lease", timedelta(seconds=60))
    return RunWorker(JobQueue(database, lease=lease), process, name=name, poll=0.05, flush=0.05, **options)


def call_of(run: Run) -> Any:
    calls = run.nodes["a"].tool_calls
    assert len(calls) == 1, calls
    return calls[0]


# ---- limits -----------------------------------------------------------------------------------------------------


def test_the_limits_are_validated() -> None:
    check_capacity(2, 1)
    check_capacity(1, 0)
    for runs, waiting in ((0, 0), (2, 2), (2, -1), (1, 1)):
        with pytest.raises(ValueError):
            check_capacity(runs, waiting)
    with pytest.raises(ValueError):
        WaitingSlots(-1)


# ---- 1-4: claiming ----------------------------------------------------------------------------------------------


async def test_01_without_questions_runs_execute_one_after_another(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    release = threading.Event()
    api_db, worker_db = connect(), connect()
    api = Api(processes(api_db), JobQueue(api_db))
    worker = worker_of(processes(worker_db, script(SLOW=blocking(release))), worker_db)
    first, second = await api.start("SLOW"), await api.start("SLOW")
    serving = Serving(worker)
    try:
        await asyncio.sleep(1.0)
        assert worker.held == 1 and api.job_status(second.id) == "queued"
        release.set()
        for run in (first, second):
            assert (await api.until(run.id, ("succeeded",))).output == "slow done"
    finally:
        release.set()
        await serving.close()
    assert worker.peak_held == 1


async def test_02_a_waiting_run_lets_the_worker_claim_the_next(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = Api(processes(api_db), JobQueue(api_db))
    worker = worker_of(processes(worker_db, script(ASK=asking(), PLAIN=lambda _r: text("plain"))), worker_db)
    serving = Serving(worker)
    try:
        a = await api.start("ASK")
        request = await api.pending(a.id)
        b = await api.start("PLAIN")
        assert (await api.until(b.id, ("succeeded",))).output == "plain"
        assert api.run(a.id).status == "running"
        api.decide(request)
        done = await api.until(a.id, ("succeeded",))
        assert call_of(done).output == "sent hi" and call_of(done).approval.status == "approved"
    finally:
        await serving.close()
    assert worker.peak_held <= 2


async def test_03_a_second_question_is_denied_when_the_waiting_capacity_is_used(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = Api(processes(api_db), JobQueue(api_db))
    worker = worker_of(processes(worker_db, script(ASK=asking())), worker_db)
    serving = Serving(worker)
    try:
        a = await api.start("ASK")
        first = await api.pending(a.id)
        b = await api.start("ASK")
        done = await api.until(b.id, ("succeeded",))
        record = call_of(done)
        assert record.is_error and record.approval.code == "tool.approval_capacity"
        assert api.process.approvals("alpha").of_run(b.id) == []  # nothing queued
        api.decide(first)
        await api.until(a.id, ("succeeded",))
    finally:
        await serving.close()


async def test_04_an_approved_run_continues_in_its_own_slot(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    release_a, release_b = threading.Event(), threading.Event()
    api_db, worker_db = connect(), connect()
    api = Api(processes(api_db), JobQueue(api_db))
    entries = script(ASK=asking(release_a), SLOW=blocking(release_b), PLAIN=lambda _r: text("plain"))
    worker = worker_of(processes(worker_db, entries), worker_db)
    serving = Serving(worker)
    try:
        a = await api.start("ASK")
        request = await api.pending(a.id)
        b = await api.start("SLOW")
        for _ in range(100):
            if api.job_status(b.id) == "running":
                break
            await asyncio.sleep(0.05)
        api.decide(request)
        await asyncio.sleep(0.5)  # A executes again (waits for release_a), B executes
        c = await api.start("PLAIN")
        await asyncio.sleep(1.0)
        assert worker.held == 2 and worker.executing == 2
        assert api.job_status(c.id) == "queued"  # no third claim while both execute
        release_a.set()
        release_b.set()
        for run in (a, b, c):
            await api.until(run.id, ("succeeded",))
    finally:
        release_a.set()
        release_b.set()
        await serving.close()
    assert worker.peak_held == 2


# ---- 5-9: decisions, deadlines and failures ------------------------------------------------------------------


async def test_05_approve_and_expire_at_the_same_moment_have_one_winner(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = Api(processes(api_db), JobQueue(api_db))
    worker = worker_of(processes(worker_db, script(ASK=asking())), worker_db)
    serving = Serving(worker)
    others = [processes(connect()) for _ in range(2)]
    try:
        a = await api.start("ASK")
        request = await api.pending(a.id)
        start = threading.Barrier(2)
        outcomes: list[str] = []

        def approve() -> None:
            start.wait()
            try:
                others[0].approvals("alpha").decide(
                    a.id, request.id, approved=True, by="usr_1", arguments=request.arguments_hash
                )
                outcomes.append("approved")
            except NodestroError as error:
                outcomes.append(error.code)

        def expire() -> None:
            start.wait()
            outcomes.append("expired" if others[1].approvals("alpha").close(a.id, request.id, "expired") else "lost")

        threads = [threading.Thread(target=approve), threading.Thread(target=expire)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [item for item in outcomes if item in ("approved", "expired")]
        assert len(winners) == 1, outcomes
        done = await api.until(a.id, ("succeeded",))
        assert call_of(done).approval.status == winners[0]
        assert (call_of(done).output == "sent hi") is (winners[0] == "approved")
    finally:
        await serving.close()


async def test_06_the_deadline_denies_and_a_late_decision_is_refused(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = Api(processes(api_db), JobQueue(api_db))
    worker = worker_of(processes(worker_db, script(ASK=asking()), wait=0.6), worker_db)
    serving = Serving(worker)
    try:
        a = await api.start("ASK")
        request = await api.pending(a.id)
        done = await api.until(a.id, ("succeeded",))
        assert call_of(done).approval.code == "tool.approval_timeout"
        with pytest.raises(NodestroError) as error:
            api.decide(request)
        assert error.value.code == "tool.approval_expired"
    finally:
        await serving.close()


async def test_07_cancelling_a_waiting_run_ends_the_wait(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = Api(processes(api_db), JobQueue(api_db))
    worker = worker_of(processes(worker_db, script(ASK=asking())), worker_db, heartbeat=0.1)
    serving = Serving(worker)
    try:
        a = await api.start("ASK")
        request = await api.pending(a.id)
        assert api.process("alpha").cancel(a.id)
        done = await api.until(a.id, ("cancelled",))
        assert done.status == "cancelled"
        stored = api.process.approvals("alpha").get(a.id, request.id)
        assert stored is not None and stored.status == "cancelled"
        assert worker.held == 0 and worker.slots.waiting == 0
    finally:
        await serving.close()


async def test_08_a_lost_lease_while_waiting_stops_the_run_and_expires_the_question(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api_process = processes(api_db)
    queue = JobQueue(api_db, lease=timedelta(milliseconds=300))
    api = Api(api_process, queue)
    worker = worker_of(
        processes(worker_db, script(ASK=asking())), worker_db, heartbeat=1.0, lease=timedelta(milliseconds=300)
    )
    serving = Serving(worker)
    try:
        a = await api.start("ASK")
        request = await api.pending(a.id)
        await asyncio.sleep(0.5)  # the lease expired: no heartbeat came in time
        assert [job.ref for job in reap(queue, api_process)] == [a.id]
        stored = api_process.approvals("alpha").get(a.id, request.id)
        assert stored is not None and stored.status == "expired"
        for _ in range(100):  # the worker notices at its next heartbeat and stops the run
            if worker.held == 0:
                break
            await asyncio.sleep(0.05)
        assert worker.held == 0 and worker.slots.waiting == 0
        with pytest.raises(NodestroError) as error:
            api.decide(request)
        assert error.value.code == "tool.approval_expired"
        # The reaper marked it interrupted; the worker then stops it locally (as before S-2): it never runs on.
        stopped = await api.until(a.id, ("failed", "cancelled"))
        assert call_of(stopped).output != "sent hi"  # the call was never made
        assert stored.closed_because == "run.interrupted"
    finally:
        await serving.close()


def _killed_worker(dsn: str, schema: str, root: str) -> None:
    """A worker process that is killed while its run waits (test 9)."""
    database = PostgresDatabase(dsn, schema=schema)
    process = Process(database, Path(root), script(ASK=asking()), timedelta(minutes=15))
    worker = RunWorker(
        JobQueue(database, lease=timedelta(milliseconds=500)), process, name="doomed", poll=0.05, flush=0.05
    )
    asyncio.run(worker.serve(asyncio.Event()))


async def test_09_a_killed_worker_leaves_an_interrupted_run_and_an_expired_question(
    schema: tuple[str, str], connect: Callable[[], PostgresDatabase], processes: Callable[..., Process], tmp_path: Path
) -> None:
    api_db = connect()
    api_process = processes(api_db)
    queue = JobQueue(api_db, lease=timedelta(milliseconds=500))
    api = Api(api_process, queue)
    a = await api.start("ASK")
    child = multiprocessing.get_context("spawn").Process(
        target=_killed_worker, args=(schema[0], schema[1], str(tmp_path / "files"))
    )
    child.start()
    try:
        request = await api.pending(a.id, timeout=60)
    finally:
        assert child.pid is not None
        os.kill(child.pid, signal.SIGKILL)
        child.join(10)
    await asyncio.sleep(0.8)
    assert [job.ref for job in reap(queue, api_process)] == [a.id]
    interrupted = api.run(a.id)
    assert interrupted.status == "failed" and interrupted.error_code == "run.interrupted"
    stored = api_process.approvals("alpha").get(a.id, request.id)
    assert stored is not None and stored.status == "expired"
    # Continuing creates new questions only; the old one is never matched.
    worker_db = connect()
    worker = worker_of(processes(worker_db, script(ASK=asking())), worker_db)
    serving = Serving(worker)
    try:
        continued = await api_process("alpha").resume(a.id)
        again = await api.pending(continued.id)
        assert again.id != request.id
        api.decide(again)
        await api.until(continued.id, ("succeeded",))
    finally:
        await serving.close()
    assert api_process.approvals("alpha").get(a.id, request.id).status == "expired"  # type: ignore[union-attr]


# ---- 10-12: tenants, heartbeats, load --------------------------------------------------------------------------


async def test_10_tenants_never_decide_each_others_questions(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = Api(processes(api_db), JobQueue(api_db))
    worker = worker_of(processes(worker_db, script(ASK=asking())), worker_db, max_runs=3, max_waiting=2)
    serving = Serving(worker)
    try:
        a = await api.start("ASK", "alpha")
        b = await api.start("ASK", "beta")
        mine = await api.pending(a.id, "alpha")
        theirs = await api.pending(b.id, "beta")
        beta = api.process.approvals("beta")
        assert beta.get(a.id, mine.id) is None and beta.pending(a.id) == []
        with pytest.raises(NodestroError) as error:
            beta.decide(a.id, mine.id, approved=True, by="usr_b", arguments=mine.arguments_hash)
        assert error.value.code == "tool.approval_not_found"
        assert api.process.approvals("alpha").get(b.id, theirs.id) is None
        api.decide(mine, tenant="alpha")
        api.decide(theirs, approved=False, tenant="beta")
        assert call_of(await api.until(a.id, ("succeeded",), "alpha")).approval.status == "approved"
        assert call_of(await api.until(b.id, ("succeeded",), "beta")).approval.status == "rejected"
    finally:
        await serving.close()


async def test_11_a_long_wait_keeps_every_held_lease_alive(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    release = threading.Event()
    api_db, worker_db = connect(), connect()
    api_process = processes(api_db)
    queue = JobQueue(api_db, lease=timedelta(milliseconds=600))
    api = Api(api_process, queue)
    worker = worker_of(
        processes(worker_db, script(ASK=asking(), SLOW=blocking(release))),
        worker_db,
        heartbeat=0.15,
        lease=timedelta(milliseconds=600),
    )
    serving = Serving(worker)
    try:
        a = await api.start("ASK")
        request = await api.pending(a.id)
        b = await api.start("SLOW")
        await asyncio.sleep(2.0)  # A's agent thread waits and B's blocks, both longer than the lease
        assert reap(queue, api_process) == []
        assert api.job_status(a.id) == "running" and api.job_status(b.id) == "running"
        api.decide(request)
        release.set()
        await api.until(a.id, ("succeeded",))
        await api.until(b.id, ("succeeded",))
    finally:
        release.set()
        await serving.close()


class CountingSlots(WaitingSlots):
    def __init__(self, max_waiting: int) -> None:
        super().__init__(max_waiting)
        self.peak = 0

    def enter(self, run_id: str) -> bool:
        entered = super().enter(run_id)
        self.peak = max(self.peak, self.waiting)
        return entered


async def test_12_load_three_workers_thirty_runs(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db = connect()
    api = Api(processes(api_db), JobQueue(api_db))
    workers: list[RunWorker] = []
    for index in range(3):
        database = connect()
        worker = worker_of(
            processes(database, script(ASK=asking(), PLAIN=lambda _r: text("plain"))), database, name=f"w{index}"
        )
        worker.slots = CountingSlots(1)
        workers.append(worker)
    serving = [Serving(worker) for worker in workers]
    rng = random.Random(7)
    runs = [await api.start("ASK" if index % 3 == 0 else "PLAIN") for index in range(30)]
    approvals = api.process.approvals("alpha")
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            for run in runs:
                for request in approvals.pending(run.id):
                    if rng.random() < 0.5:
                        await asyncio.sleep(rng.random() * 0.2)
                        with contextlib.suppress(NodestroError):  # the deadline may have won
                            api.decide(request, approved=rng.random() < 0.7)
            if all(api.job_status(run.id) not in ("queued", "running") for run in runs):
                break
            for worker in workers:
                assert worker.held <= 2 and worker.slots.waiting <= 1
            await asyncio.sleep(0.05)
    finally:
        for item in serving:
            await item.close()
    finals = [api.run(run.id) for run in runs]
    assert all(run.status == "succeeded" for run in finals), [(run.id, run.status, run.error) for run in finals]
    for worker in workers:
        assert worker.peak_held <= 2
        assert isinstance(worker.slots, CountingSlots) and worker.slots.peak <= 1
    for run in runs:
        assert approvals.pending(run.id) == []
    asked = [call_of(run).approval for run in finals if run.workflow.nodes[1].agent.system_prompt == "ASK"]
    assert len(asked) == 10
    assert {item.status for item in asked} <= {"approved", "rejected", "denied"}
