"""Writes whose reply is lost after they were applied (JQ-1, RT2-03; needs ``NODESTRO_TEST_POSTGRES``).

``PostgresDatabase._query`` repeats a statement on another connection when its connection is lost; that is right for
a read and wrong for a write that may already have been applied. The nine writing statements that went through it
(``jobs.py`` ``enqueue``, ``claim``, ``heartbeat``, ``reap_expired``, ``report``; ``identity_store.py``
``create_tenant``, ``set_tenant_status``, ``login``, ``take_login_state``) are driven here through a
:class:`~postgres_support.FaultProxy` that lets the statement commit and then loses its reply. Five of them
(``enqueue``, ``claim``, ``reap_expired``, ``create_tenant``, ``take_login_state``) failed these tests before the fix
(JQ-3: they now run through ``PostgresDatabase._write_returning``, never repeated); the four idempotent ones stay on
``_query`` (``tests/test_query_writes.py``). The counter-probes
(``finish``, ``cleanup``, ``request_cancel``) show the paths that never repeated a write, and the reads show that the
read retry of RT2-03 stays. Every test asserts that the fault really fired. Nothing here sleeps: leases are expired
by moving ``lease_until`` into the past.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import postgres_support
from postgres_support import FaultProxy
import pytest

from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text
from nodestro_runtime.workspace import Workspace

pytest.importorskip("psycopg")

import psycopg

from nodestro_runtime.identity_store import IdentityStore
from nodestro_runtime.jobs import (
    REINDEX_JOB,
    REINDEX_REF,
    RUN_JOB,
    JobQueue,
    QueuedReindex,
    QueuedRuns,
    RunWorker,
    reap,
)
from nodestro_runtime.postgres_tables import tenant_storage
from nodestro_runtime.storage_postgres import PostgresDatabase


@pytest.fixture
def schema() -> Iterator[str]:
    postgres_support.require_postgres()
    name = postgres_support.new_schema()
    database = PostgresDatabase(postgres_support.DSN, schema=name)
    database.migrate()
    database.close()
    yield name
    postgres_support.drop_schema(name)


@pytest.fixture
def direct(schema: str) -> Iterator[PostgresDatabase]:
    """The test schema without the proxy: setup and checks of what the server really stored."""
    database = PostgresDatabase(postgres_support.DSN, schema=schema)
    yield database
    database.close()


@pytest.fixture
def proxied(schema: str, fault_proxy: FaultProxy) -> Iterator[PostgresDatabase]:
    """The test schema through the fault proxy (as the restricted runtime role under ``NODESTRO_TEST_RUNTIME_ROLE``)."""
    base = postgres_support.runtime_dsn() if postgres_support.RUNTIME_ROLE_MODE else postgres_support.DSN
    database = PostgresDatabase(fault_proxy.dsn(base), schema=schema, pool_size=2)
    yield database
    database.close()


def jobs(direct: PostgresDatabase) -> list[tuple[Any, ...]]:
    """``(ref, status, worker, attempts)`` of every job, by id."""
    with postgres_support.owner_of(direct)._transaction() as db:
        rows: list[tuple[Any, ...]] = db.execute(
            "SELECT ref, status, worker, attempts FROM nodestro_jobs ORDER BY id"
        ).fetchall()
    return rows


def expire_leases(direct: PostgresDatabase) -> None:
    with postgres_support.owner_of(direct)._transaction() as db:
        db.execute(
            "UPDATE nodestro_jobs SET lease_until = clock_timestamp() - interval '1 second' WHERE status = 'running'"
        )


def lost_reply(proxy: FaultProxy) -> None:
    """The armed statement fired, reached the server, and its reply was lost after the server ended it."""
    assert (proxy.fired, proxy.replies_lost, proxy.armed) == (1, 1, False)


# ---- 1 enqueue ----------------------------------------------------------------------------------

ENQUEUE = b"INSERT INTO nodestro_jobs"


def test_an_enqueue_whose_reply_is_lost_returns_the_queued_job(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    fault_proxy.arm(ENQUEUE)
    job = JobQueue(proxied).enqueue("alpha", RUN_JOB, "run-1")
    lost_reply(fault_proxy)
    assert jobs(direct) == [("run-1", "queued", None, 0)]
    assert job is not None and (job.ref, job.status) == ("run-1", "queued")


def test_a_dispatch_whose_reply_is_lost_does_not_report_run_already_queued(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    fault_proxy.arm(ENQUEUE)
    QueuedRuns(JobQueue(proxied), "alpha", runs=None).dispatch(SimpleNamespace(id="run-1"))  # type: ignore[arg-type]
    lost_reply(fault_proxy)
    assert jobs(direct) == [("run-1", "queued", None, 0)]


def test_a_reindex_whose_reply_is_lost_does_not_report_reindex_running(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    fault_proxy.arm(ENQUEUE)
    QueuedReindex(JobQueue(proxied), "alpha", knowledge=None).start("model-a")
    lost_reply(fault_proxy)
    assert [row[:2] for row in jobs(direct)] == [(REINDEX_REF, "queued")]


# ---- 2 claim ------------------------------------------------------------------------------------

CLAIM = b"SET status = 'running'"


def test_a_claim_whose_reply_is_lost_returns_the_claimed_job(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    JobQueue(direct).enqueue("alpha", RUN_JOB, "run-1")
    JobQueue(direct).enqueue("alpha", RUN_JOB, "run-2")
    fault_proxy.arm(CLAIM)
    job = JobQueue(proxied).claim("w1")
    lost_reply(fault_proxy)
    # The first job is the worker's and the second one is still queued; nothing runs without its worker knowing.
    assert jobs(direct) == [("run-1", "running", "w1", 1), ("run-2", "queued", None, 0)]
    assert job is not None and (job.ref, job.status, job.worker) == ("run-1", "running", "w1")


def test_a_lost_claim_never_gives_one_job_to_two_workers(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    """Double processing (JQ-2): whatever the lost claim does, every job is claimed at most once."""
    for ref in ("run-1", "run-2", "run-3"):
        JobQueue(direct).enqueue("alpha", RUN_JOB, ref)
    fault_proxy.arm(CLAIM)
    first = JobQueue(proxied).claim("w1")
    lost_reply(fault_proxy)
    second = JobQueue(direct).claim("w2")
    assert second is not None
    if first is not None:
        assert first.id != second.id
    assert all(attempts <= 1 for _ref, _status, _worker, attempts in jobs(direct))


class Process:
    """The workspaces one process opened, one per tenant (as in ``test_jobs.py``)."""

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


def simple_flow() -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Simple",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "write",
                    "type": "agent",
                    "label": "write",
                    "agent": {"name": "write", "system_prompt": "WRITE"},
                },
            ],
            "edges": [{"id": "start-write", "source": "start", "target": "write", "source_handle": None}],
        }
    )


async def start_run(api: Process, queue: JobQueue, tenant: str) -> str:
    workspace = api(tenant)
    QueuedRuns(queue, tenant, workspace.runs).attach()
    run = await workspace.run(workspace.save_workflow(simple_flow()), RunIn(input="x"))
    assert run.status == "queued"
    return run.id


async def test_a_worker_whose_claim_reply_is_lost_executes_the_run(
    proxied: PostgresDatabase,
    direct: PostgresDatabase,
    fault_proxy: FaultProxy,
    processes: Callable[..., Process],
) -> None:
    """The lost job end to end (JQ-2): the worker executes the run it claimed instead of leaving it to the reaper."""
    api, worker = processes(direct), processes(proxied, {"WRITE": [text("done")]})
    run_id = await start_run(api, JobQueue(direct), "alpha")
    fault_proxy.arm(CLAIM)
    job = await RunWorker(JobQueue(proxied), worker, name="w1").run_once()
    lost_reply(fault_proxy)
    run = api("alpha").runs.get(run_id)
    assert run is not None and run.status == "succeeded", run and (run.status, run.error_code)
    assert job is not None and job.ref == run_id
    assert jobs(direct) == [(run_id, "done", "w1", 1)]


async def test_an_api_start_whose_enqueue_reply_is_lost_succeeds(
    proxied: PostgresDatabase,
    direct: PostgresDatabase,
    fault_proxy: FaultProxy,
    processes: Callable[..., Process],
) -> None:
    """``run.already_queued`` (JQ-2): the start of a run whose job was queued reports success, not a conflict."""
    api = processes(proxied)
    fault_proxy.arm(ENQUEUE)
    run_id = await start_run(api, JobQueue(proxied), "alpha")
    lost_reply(fault_proxy)
    assert jobs(direct) == [(run_id, "queued", None, 0)]


# ---- 3 heartbeat (idempotent) -------------------------------------------------------------------


def test_a_heartbeat_whose_reply_is_lost_renews_the_lease(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    JobQueue(direct).enqueue("alpha", RUN_JOB, "run-1")
    job = JobQueue(direct).claim("w1")
    assert job is not None
    fault_proxy.arm(b"SET lease_until = clock_timestamp()")
    beat = JobQueue(proxied).heartbeat(job.id, "w1")
    lost_reply(fault_proxy)
    assert beat is not None and beat.lease_until is not None and job.lease_until is not None
    assert beat.lease_until > job.lease_until
    assert jobs(direct) == [("run-1", "running", "w1", 1)]


# ---- 4 reap_expired -----------------------------------------------------------------------------

REAP = b"SET status = 'interrupted'"


def test_a_reap_whose_reply_is_lost_fails_instead_of_reporting_nothing(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    JobQueue(direct).enqueue("alpha", RUN_JOB, "run-1")
    assert JobQueue(direct).claim("dead-worker") is not None
    expire_leases(direct)
    fault_proxy.arm(REAP)
    with pytest.raises(psycopg.OperationalError):
        JobQueue(proxied).reap_expired()
    lost_reply(fault_proxy)
    assert jobs(direct) == [("run-1", "interrupted", "dead-worker", 1)]


async def test_the_run_of_a_lost_reap_is_healed_by_the_orphan_check(
    proxied: PostgresDatabase,
    direct: PostgresDatabase,
    fault_proxy: FaultProxy,
    processes: Callable[..., Process],
) -> None:
    """Healing (JQ-2): the run whose reaped job was not reported is interrupted by the next reaper (C-11)."""
    api = processes(direct)
    run_id = await start_run(api, JobQueue(direct), "alpha")
    assert JobQueue(direct).claim("dead-worker") is not None
    expire_leases(direct)
    fault_proxy.arm(REAP)
    with pytest.raises(psycopg.OperationalError):
        reap(JobQueue(proxied), api)
    lost_reply(fault_proxy)
    assert reap(JobQueue(direct), api, orphans_after=timedelta(0)) == []
    run = api("alpha").runs.get(run_id)
    assert run is not None and run.status == "failed" and run.error_code == "run.interrupted"


# ---- 5 report (idempotent) ----------------------------------------------------------------------


def test_a_report_whose_reply_is_lost_merges_the_progress_once(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    JobQueue(direct).enqueue("alpha", REINDEX_JOB, "reindex", {"model": "m"})
    job = JobQueue(direct).claim("w1", (REINDEX_JOB,))
    assert job is not None
    fault_proxy.arm(b"SET payload = payload ||")
    reported = JobQueue(proxied).report(job, "w1", {"completed": 3, "total": 9})
    lost_reply(fault_proxy)
    assert reported is not None and reported.payload == {"model": "m", "completed": 3, "total": 9}
    stored = JobQueue(direct).get(job.id)
    assert stored is not None and stored.payload == {"model": "m", "completed": 3, "total": 9}


# ---- 6 create_tenant ----------------------------------------------------------------------------


def test_a_tenant_creation_whose_reply_is_lost_returns_the_tenant(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    fault_proxy.arm(b"INSERT INTO nodestro_tenants")
    tenant = IdentityStore(proxied).create_tenant("alpha", "Alpha")
    lost_reply(fault_proxy)
    assert (tenant.id, tenant.name, tenant.status) == ("alpha", "Alpha", "active")
    assert [item.id for item in IdentityStore(direct).tenants()] == ["alpha"]


# ---- 7 set_tenant_status (idempotent) -----------------------------------------------------------


def test_a_tenant_status_change_whose_reply_is_lost_returns_the_new_status(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    IdentityStore(direct).create_tenant("alpha", "Alpha")
    fault_proxy.arm(b"UPDATE nodestro_tenants SET status")
    tenant = IdentityStore(proxied).set_tenant_status("alpha", "suspended")
    lost_reply(fault_proxy)
    assert tenant.status == "suspended"
    found = IdentityStore(direct).tenant("alpha")
    assert found is not None and found.status == "suspended"


# ---- 8 login (idempotent) -----------------------------------------------------------------------


def test_a_first_login_whose_reply_is_lost_returns_the_one_stored_user(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    fault_proxy.arm(b"INSERT INTO nodestro_users")
    user = IdentityStore(proxied).login("https://idp.example", "sub-1", email="a@example.com", name="A")
    lost_reply(fault_proxy)
    with postgres_support.owner_of(direct)._transaction() as db:
        rows = db.execute("SELECT id, issuer, subject FROM nodestro_users").fetchall()
    assert rows == [(user.id, "https://idp.example", "sub-1")]


# ---- 9 take_login_state -------------------------------------------------------------------------


def test_a_login_state_whose_reply_is_lost_fails_instead_of_reporting_it_expired(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    state = IdentityStore(direct).create_login_state(nonce="n", code_verifier="v", return_to="/")
    fault_proxy.arm(b"DELETE FROM nodestro_login_states")
    with pytest.raises(psycopg.OperationalError):
        IdentityStore(proxied).take_login_state(state)
    lost_reply(fault_proxy)
    # The state is used up either way (fail-safe): it can never be taken twice.
    assert IdentityStore(direct).take_login_state(state) is None


# ---- counter-probes: paths that never repeated a write ------------------------------------------


def test_a_finish_whose_reply_is_lost_fails_and_is_not_repeated(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    JobQueue(direct).enqueue("alpha", RUN_JOB, "run-1")
    job = JobQueue(direct).claim("w1")
    assert job is not None
    fault_proxy.arm(b"finished_at = clock_timestamp(), lease_until = NULL")
    with pytest.raises(psycopg.OperationalError):
        JobQueue(proxied).finish(job.id, "w1", "done")
    lost_reply(fault_proxy)
    assert jobs(direct) == [("run-1", "done", "w1", 1)]


def test_a_cleanup_whose_reply_is_lost_fails_and_is_not_repeated(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    queue = JobQueue(direct)
    queue.enqueue("alpha", RUN_JOB, "run-1")
    job = queue.claim("w1")
    assert job is not None and queue.finish(job.id, "w1", "done")
    fault_proxy.arm(b"DELETE FROM nodestro_jobs")
    with pytest.raises(psycopg.OperationalError):
        JobQueue(proxied).cleanup(older_than=timedelta(0))
    lost_reply(fault_proxy)
    assert jobs(direct) == []


def test_a_cancel_whose_reply_is_lost_fails_and_changes_nothing(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    """``request_cancel`` runs in a transaction: the lost connection rolls it back, nothing is repeated."""
    JobQueue(direct).enqueue("alpha", RUN_JOB, "run-1")
    fault_proxy.arm(b"SET status = 'cancelled'")
    with pytest.raises(psycopg.OperationalError):
        JobQueue(proxied).request_cancel("alpha", RUN_JOB, "run-1")
    lost_reply(fault_proxy)
    assert jobs(direct) == [("run-1", "queued", None, 0)]


# ---- reads keep their retry (RT2-03) ------------------------------------------------------------


def test_a_read_whose_reply_is_lost_is_repeated_on_another_connection(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    created = JobQueue(direct).enqueue("alpha", RUN_JOB, "run-1")
    assert created is not None
    fault_proxy.arm(b"FROM nodestro_jobs WHERE id = $1")
    found = JobQueue(proxied).get(created.id)
    lost_reply(fault_proxy)
    assert found is not None and found.id == created.id


def test_a_read_whose_request_is_lost_is_repeated_on_another_connection(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    created = JobQueue(direct).enqueue("alpha", RUN_JOB, "run-1")
    assert created is not None
    fault_proxy.arm(b"FROM nodestro_jobs WHERE id = $1", lose="request")
    found = JobQueue(proxied).get(created.id)
    assert (fault_proxy.fired, fault_proxy.replies_lost) == (1, 0)
    assert found is not None and found.id == created.id


# ---- a write whose request is lost is not repeated either (RT2-03) ------------------------------


def test_an_enqueue_whose_request_is_lost_fails_and_queues_nothing(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    """The client cannot tell a lost request from a lost reply: with no active job found, the error is raised."""
    fault_proxy.arm(ENQUEUE, lose="request")
    with pytest.raises(psycopg.OperationalError):
        JobQueue(proxied).enqueue("alpha", RUN_JOB, "run-1")
    assert (fault_proxy.fired, fault_proxy.replies_lost) == (1, 0)
    assert jobs(direct) == []


def test_a_claim_whose_request_is_lost_fails_and_leaves_the_job_queued(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    JobQueue(direct).enqueue("alpha", RUN_JOB, "run-1")
    queue = JobQueue(proxied)
    fault_proxy.arm(CLAIM, lose="request")
    with pytest.raises(psycopg.OperationalError):
        queue.claim("w1")
    assert (fault_proxy.fired, fault_proxy.replies_lost) == (1, 0)
    assert jobs(direct) == [("run-1", "queued", None, 0)]
    job = queue.claim("w1")
    assert job is not None and job.ref == "run-1"


def test_a_lost_claim_never_returns_a_job_the_worker_already_holds(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    """A worker holding a run (S-2) loses a claim: the newer job is found, the held one is never handed out twice."""
    for ref in ("run-1", "run-2"):
        JobQueue(direct).enqueue("alpha", RUN_JOB, ref)
    queue = JobQueue(proxied)
    held = queue.claim("w1")
    assert held is not None and held.ref == "run-1"
    fault_proxy.arm(CLAIM)
    job = queue.claim("w1")
    lost_reply(fault_proxy)
    assert job is not None and job.ref == "run-2"
    fault_proxy.arm(CLAIM, lose="request")
    with pytest.raises(psycopg.OperationalError):
        queue.claim("w1")  # nothing queued was claimed; the two held jobs are not candidates
    assert queue.finish(held.id, "w1", "done") and queue.finish(job.id, "w1", "done")
    assert queue._claimed == {}


def test_a_tenant_creation_whose_request_is_lost_fails_and_creates_nothing(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    fault_proxy.arm(b"INSERT INTO nodestro_tenants", lose="request")
    with pytest.raises(psycopg.OperationalError):
        IdentityStore(proxied).create_tenant("alpha", "Alpha")
    assert IdentityStore(direct).tenants() == []


def test_a_write_inside_a_transaction_joins_it_and_is_not_reconciled(
    proxied: PostgresDatabase, direct: PostgresDatabase, fault_proxy: FaultProxy
) -> None:
    """In a transaction the lost connection rolls everything back; nothing is looked up or repeated."""
    fault_proxy.arm(ENQUEUE)
    with pytest.raises(psycopg.OperationalError), proxied.transaction():
        JobQueue(proxied).enqueue("alpha", RUN_JOB, "run-1")
    assert fault_proxy.fired == 1
    assert jobs(direct) == []
