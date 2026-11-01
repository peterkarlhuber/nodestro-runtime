"""Tool changes and deletions made by another process reach every server worker (S-2 pilot finding F1).

The API process and two workers are separate workspaces on separate PostgreSQL connections that share one data
directory, as on a server. Before the fix, a worker kept the tools it read when it opened the tenant: after a change
or deletion in the API it ran the old code, and an advance approval or a decision for the old version still let it
run. Real PostgreSQL only (``NODESTRO_REQUIRE_POSTGRES=1`` turns a missing database into a failure). A server checks
and runs tool code only in containers, so the tests need a reachable Docker daemon: without one they are skipped,
except in CI (``GITHUB_ACTIONS``) or with ``NODESTRO_REQUIRE_DOCKER=1``, where a missing daemon is a failure.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import timedelta
import os
from pathlib import Path
import threading
import time
from typing import Any

import postgres_support
import pytest

from nodestro_runtime.launcher import launch
from nodestro_runtime.schemas import Run, RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import ToolApprovalRequest, ToolApprovals, ToolGate, ToolPreGrants
from nodestro_runtime.workspace import Workspace

pytest.importorskip("psycopg")

from nodestro_runtime.jobs import RUN_JOB, JobQueue, QueuedRuns, RunWorker
from nodestro_runtime.postgres_tables import tenant_storage
from nodestro_runtime.storage_postgres import PostgresDatabase


def _docker_daemon() -> bool:
    from orkexa.dynamic import DockerSandbox

    return bool(DockerSandbox.available())


_DOCKER_REQUIRED = os.environ.get("NODESTRO_REQUIRE_DOCKER") == "1" or os.environ.get("GITHUB_ACTIONS") == "true"
pytestmark = pytest.mark.skipif(
    not _DOCKER_REQUIRED and not _docker_daemon(),
    reason="needs a Docker daemon (a server runs tool code in containers)",
)

TOOL = "celsius_to_fahrenheit"
SCHEDULE = "sch_pilot"


def code(offset: int) -> str:
    return f'''\
def celsius_to_fahrenheit(celsius: float) -> float:
    """Convert a temperature from degrees Celsius to degrees Fahrenheit.

    Args:
        celsius: Temperature in degrees Celsius.

    Returns:
        The temperature in degrees Fahrenheit.
    """
    return celsius * 9 / 5 + 32 + {offset}
'''


def checks_for(offset: int) -> str:
    return f"""\
from tool import celsius_to_fahrenheit


def test_freezing_point():
    assert celsius_to_fahrenheit(0) == 32 + {offset}
"""


def output(offset: int) -> str:
    return f"{212.0 + offset}"


def calling(before: threading.Event | None = None) -> Callable[[dict[str, Any]], Any]:
    """Calls the tool once (after ``before`` is set, if given), then answers."""

    def respond(request: dict[str, Any]) -> Any:
        answered = any(
            isinstance(item.get("content"), list)
            and any(isinstance(block, dict) and block.get("type") == "tool_result" for block in item["content"])
            for item in request.get("messages", [])
        )
        if answered:
            return text("done")
        if before is not None:
            before.wait(20)
        return tool_use(TOOL, {"celsius": 100})

    return respond


class Process:
    """The workspaces of one process (the API or a worker), one per tenant, sharing the data directory ``root``."""

    def __init__(self, database: PostgresDatabase, root: Path, respond: Callable[[dict[str, Any]], Any], gate: bool):
        self.database = database
        self.root = root
        self.client = ScriptedClient({"AGENT": [respond] * 200})
        self.gate = gate
        self.opened: dict[str, Workspace] = {}

    def __call__(self, tenant_id: str) -> Workspace:
        if tenant_id not in self.opened:
            workspace = Workspace(
                self.root / tenant_id,
                client_factory=lambda _provider: self.client,
                integrations=[],
                storage=tenant_storage(self.database, tenant_id),
            )
            if self.gate:
                approvals = ToolApprovals(workspace.store, wait=timedelta(minutes=15), poll_seconds=0.05)
                workspace.runs.tool_gate = ToolGate(approvals, pre_grants=ToolPreGrants(workspace.store).of_scope)
            self.opened[tenant_id] = workspace
        return self.opened[tenant_id]

    def close(self) -> None:
        for workspace in self.opened.values():
            workspace.close()


@pytest.fixture
def connect() -> Iterator[Callable[[], PostgresDatabase]]:
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


Make = Callable[..., Process]


@pytest.fixture
def processes(tmp_path: Path, connect: Callable[[], PostgresDatabase]) -> Iterator[Make]:
    made: list[Process] = []

    def factory(respond: Callable[[dict[str, Any]], Any] | None = None, *, gate: bool = False) -> Process:
        process = Process(connect(), tmp_path / "files", respond or calling(), gate)
        made.append(process)
        return process

    yield factory
    for process in made:
        process.close()


class Server:
    """An API process and two workers on one database and one data directory."""

    def __init__(self, processes: Make, respond: Callable[[dict[str, Any]], Any] | None = None, gate: bool = False):
        self.api = processes(gate=gate)
        self.queue = JobQueue(self.api.database)
        self.workers = [processes(respond, gate=gate) for _ in range(2)]
        self.saved: dict[str, Any] = {}

    def save(self, offset: int, tenant: str = "alpha") -> int:
        tools = self.api(tenant).tools
        result, info = tools.save_custom(
            code(offset), checks_for(offset), TOOL, trust=tools.editor_trust(TOOL, code(offset))
        )
        assert result.ok and info is not None and info.version is not None, result
        return info.version

    def delete(self, tenant: str = "alpha") -> None:
        self.api(tenant).tools.delete(TOOL)

    def warm(self, tenant: str = "alpha") -> None:
        """Every worker opens the tenant (and reads its tools), as after serving a first run."""
        for worker in self.workers:
            assert TOOL in worker(tenant).tools

    async def start(self, tenant: str = "alpha", *, schedule: bool = False) -> Run:
        workspace = self.api(tenant)
        QueuedRuns(self.queue, tenant, workspace.runs).attach()
        if tenant not in self.saved:
            flow = {
                "name": "Convert",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "a", "type": "agent", "agent": {"name": "A", "system_prompt": "AGENT", "tools": [TOOL]}},
                ],
                "edges": [{"id": "e", "source": "start", "target": "a"}],
            }
            self.saved[tenant] = workspace.save_workflow(WorkflowIn.model_validate(flow))
        if schedule:
            return await launch(
                workspace, self.saved[tenant], RunIn(input="x"), trigger="schedule", trigger_ref=SCHEDULE
            )
        return await workspace.run(self.saved[tenant], RunIn(input="x"))

    def run(self, run_id: str, tenant: str = "alpha") -> Run:
        found = self.api(tenant).runs.get(run_id)
        assert found is not None
        return found

    async def until_done(self, run_id: str, tenant: str = "alpha", timeout: float = 30) -> Run:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.run(run_id, tenant)
            jobs = self.queue.jobs(tenant, RUN_JOB, run_id)
            if (
                found.status in ("succeeded", "failed", "cancelled")
                and jobs
                and jobs[-1].status not in ("queued", "running")
            ):
                return found
            await asyncio.sleep(0.05)
        raise AssertionError(f"run {run_id} is {self.run(run_id, tenant).status}")

    async def pending(self, run_id: str, tenant: str = "alpha", timeout: float = 20) -> ToolApprovalRequest:
        approvals = ToolApprovals(self.api(tenant).store)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = approvals.pending(run_id)
            if found:
                return found[0]
            await asyncio.sleep(0.05)
        raise AssertionError(f"run {run_id} never asked")

    def serve(self, *indexes: int) -> list[Serving]:
        return [
            Serving(
                RunWorker(JobQueue(self.workers[i].database), self.workers[i], name=f"w{i + 1}", poll=0.05, flush=0.05)
            )
            for i in indexes
        ]

    async def on(self, index: int, tenant: str = "alpha", **start: Any) -> Run:
        """One run, executed by worker ``index`` alone."""
        (serving,) = self.serve(index)
        try:
            started = await self.start(tenant, **start)
            return await self.until_done(started.id, tenant)
        finally:
            await serving.close()


class Serving:
    def __init__(self, worker: RunWorker) -> None:
        self.stop = asyncio.Event()
        self.task = asyncio.ensure_future(worker.serve(self.stop))

    async def close(self) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, 30)


def call_of(run: Run) -> Any:
    calls = run.nodes["a"].tool_calls
    assert len(calls) == 1, (run.status, run.error, calls)
    return calls[0]


def ran(run: Run) -> bool:
    """Whether the tool's code produced a result in this run."""
    return any(not call.is_error and call.output for call in run.nodes["a"].tool_calls)


async def test_a_change_in_the_api_reaches_every_worker(processes: Make) -> None:
    server = Server(processes)
    server.save(0)
    server.warm()
    for index in (0, 1):
        assert call_of(await server.on(index)).output == output(0)
    server.save(1000)  # version 2, saved by the API process
    for index in (0, 1):
        done = await server.on(index)
        assert call_of(done).output == output(1000), f"worker {index + 1} ran the old version"
    server.save(0)  # version 3: back to the first code, still a new version
    for index in (0, 1):
        assert call_of(await server.on(index)).output == output(0)


async def test_a_tool_deleted_in_the_api_runs_on_no_worker(processes: Make) -> None:
    server = Server(processes)
    for index in (0, 1):
        server.save(0)
        server.warm()
        queued = await server.start()  # started while the tool existed
        server.delete()
        (serving,) = server.serve(index)
        try:
            done = await server.until_done(queued.id)
        finally:
            await serving.close()
        assert done.status == "failed" and not ran(done), f"worker {index + 1} ran a deleted tool"
        assert done.nodes["a"].error_code == "config.unknown_tools", done.nodes["a"]


@pytest.mark.parametrize("change", ["save", "delete"])
async def test_a_change_while_a_step_runs_refuses_the_old_version(processes: Make, change: str) -> None:
    release = threading.Event()
    server = Server(processes, calling(release))
    server.save(0)
    server.warm()
    (serving,) = server.serve(0)
    try:
        started = await server.start()
        for _ in range(200):  # the step started and holds its tools
            if (
                server.run(started.id).nodes.get("a") is not None
                and server.run(started.id).nodes["a"].status == "running"
            ):
                break
            await asyncio.sleep(0.05)
        server.save(1000) if change == "save" else server.delete()
        release.set()
        done = await server.until_done(started.id)
    finally:
        release.set()
        await serving.close()
    record = call_of(done)
    assert record.is_error and "changed or deleted" in str(record.output), record
    assert not ran(done)


async def test_an_advance_approval_for_the_old_version_does_not_carry_over(processes: Make) -> None:
    server = Server(processes, gate=True)
    version = server.save(0)
    tools = server.api("alpha").tools
    ToolPreGrants(server.api("alpha").store).grant(
        tool=TOOL,
        kind="custom",
        capability=tools.get(TOOL).capability,
        version=version,
        scope="schedule",
        scope_id=SCHEDULE,
        by="usr_1",
    )
    server.warm()
    for index in (0, 1):
        record = call_of(await server.on(index, schedule=True))
        assert record.approval.status == "allowed" and record.output == output(0), record
    server.save(1000)
    for index in (0, 1):
        done = await server.on(index, schedule=True)
        record = call_of(done)
        assert record.is_error and record.approval.status == "denied", f"worker {index + 1}: {record}"
        assert record.approval.code == "tool.approval_unattended"
        assert not ran(done)


@pytest.mark.parametrize("change", ["save", "delete"])
async def test_a_decision_for_the_old_version_does_not_run_another(processes: Make, change: str) -> None:
    server = Server(processes, gate=True)
    server.save(0)
    server.warm()
    (serving,) = server.serve(0)
    try:
        started = await server.start()
        request = await server.pending(started.id)
        assert request.version == 1
        server.save(1000) if change == "save" else server.delete()  # while the person decides
        ToolApprovals(server.api("alpha").store).decide(
            started.id, request.id, approved=True, by="usr_1", arguments=request.arguments_hash
        )
        done = await server.until_done(started.id)
    finally:
        await serving.close()
    record = call_of(done)
    assert record.is_error and "changed or deleted" in str(record.output), record
    assert not ran(done)


async def test_tenants_keep_their_own_tools(processes: Make) -> None:
    server = Server(processes)
    server.save(0, "alpha")
    server.save(0, "beta")
    server.warm("alpha")
    server.warm("beta")
    server.save(1000, "alpha")
    for index in (0, 1):
        assert call_of(await server.on(index, "alpha")).output == output(1000)
        assert call_of(await server.on(index, "beta")).output == output(0)


async def test_parallel_runs_on_both_workers_use_the_new_version(processes: Make) -> None:
    server = Server(processes)
    server.save(0)
    server.warm()
    server.save(1000)
    servings = server.serve(0, 1)
    try:
        started = [await server.start() for _ in range(6)]
        done = await asyncio.gather(*(server.until_done(run.id) for run in started))
    finally:
        for serving in servings:
            await serving.close()
    assert [call_of(run).output for run in done] == [output(1000)] * 6
    workers = {server.queue.jobs("alpha", RUN_JOB, run.id)[-1].worker for run in started}
    assert len(workers) == 2, workers
