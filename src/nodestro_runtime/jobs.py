"""Background jobs of the server profile in PostgreSQL (ADR-0004): queue, run events, workers, reaper, scheduler lock.

The desktop executes runs in its own process. A server separates the two: API processes store a run and enqueue a
job (:class:`QueuedRuns` connects a tenant's :class:`~nodestro_runtime.engine.RunManager` to the queue), and worker
processes (:class:`RunWorker`) claim jobs and execute them.

- A worker **claims** a job with ``FOR UPDATE SKIP LOCKED``: two workers never get the same job, and at most one job
  per run is active at any time (a unique index on the active jobs).
- A claimed job has a **lease** that the worker renews with :meth:`JobQueue.heartbeat`. When a worker dies, its
  lease expires and :func:`reap` marks the job and its run as **interrupted**. Such a job is never executed again
  automatically: model calls cost money and tools have side effects, so a person decides whether to continue.
  Before every model request and tool call, an executing run asks whether its worker still holds the job
  (:meth:`RunWorker.owns`, which also renews the lease): a worker whose heartbeat stalled stops there instead of
  acting for a run that the reaper already interrupted. This queue is deliberately not Axentra's at-least-once job
  queue (M11 / G-8, ``docs/AXENTRA.md``).
- **Cancelling** a queued job withdraws it at once; a running job gets ``cancel_requested``, which its worker reads
  with the next heartbeat and passes to the run.
- A worker **holds** at most ``max_runs`` runs (default 2), of which at most ``max_waiting`` (default 1) may wait for a
  tool approval (S-2). It claims a new job only while it holds fewer than ``max_runs`` runs and none of them
  executes (every held run waits): with nothing waiting it executes one run at a time as before. An approved run
  continues in the slot it already holds, so resuming never adds capacity; a run that would wait while the
  worker's waiting capacity is used up is denied at once (``tool.approval_capacity``), never queued.
- Every event of a run executing in a worker is written to ``nodestro_run_events`` (:class:`PostgresEventLog`), so
  any API process can stream it.
- Exactly one process fires schedules (:class:`LeaderLock`), and every schedule slot fires at most once
  (:class:`ScheduleFirings`), also when a leader dies while firing.

- A **semantic reindex** of a tenant's knowledge base is a job too (:data:`REINDEX_JOB`, RT-046): the API process
  queues it (:class:`QueuedReindex`), a worker runs it (:class:`ReindexWorker`) with a lease, reports its progress in
  the job's payload and stops it when a person cancels it, the tenant is suspended or the lease is lost. A reindex
  that stopped continues where it stopped when started again; the reaper marks it interrupted, like a run.

Runs and reindex jobs are executed through the queue; the other long-running work of the desktop (batches, test
series, assistant drafts, Python environment installations) still runs in the process that started it.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Iterable, Mapping
import concurrent.futures
import contextlib
from datetime import datetime, timedelta
import json
import logging
import os
import socket
import threading
import time
from typing import Any, Literal, cast
import uuid

from pydantic import BaseModel, Field

from .error_texts import failure_text
from .knowledge import ReindexJob
from .problems import NodestroError
from .schemas import Run, RunEvent
from .storage_postgres import PostgresDatabase
from .tool_approvals import WaitingSlots

logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "running", "done", "failed", "cancelled", "interrupted"]
#: The job kind of a run.
RUN_JOB = "run"
#: The job kind of a semantic reindex of a tenant's knowledge base (RT-046); its ref is always :data:`REINDEX_REF`,
#: so a tenant has at most one queued or running reindex on the whole server.
REINDEX_JOB = "reindex"
REINDEX_REF = "knowledge"
#: Runs a worker holds at the same time (executing or waiting for a tool approval), and how many of them may wait.
DEFAULT_MAX_RUNS = 2
DEFAULT_MAX_WAITING = 1

#: The advisory lock of the schedule leader (any constant different from the migration lock).
SCHEDULER_LOCK = 0x6E6F64657374726F

_COLUMNS = (
    "id, tenant_id, kind, ref, status, payload, attempts, worker, lease_until, cancel_requested, error,"
    " created_at, started_at, finished_at"
)
#: The queued or running job of a piece of work (tenant, kind, ref).
_ACTIVE = (
    f"SELECT {_COLUMNS} FROM nodestro_jobs"
    " WHERE tenant_id = %s AND kind = %s AND ref = %s AND status IN ('queued', 'running')"
)
#: The payload key of :meth:`JobQueue.enqueue`'s client token (JQ-3): after a lost connection it identifies the row
#: this call inserted. Reserved; it is not part of :attr:`Job.payload`.
ENQUEUE_TOKEN = "_enqueue_token"


class _Claims:
    """The claims of one worker name on one database (connection string and schema) in this process, shared by every
    :class:`JobQueue` on it (JQ-3).

    ``held``: jobs :meth:`JobQueue.claim` handed out and not finished yet. ``abandoned``: jobs whose ``finish`` raised
    (the last 1024); neither is ever handed out again. ``unsure``: the kinds of claims whose outcome is unknown (the
    connection was lost and what the server did could not be shown); the next claim of such a kind first adopts the
    worker's running jobs that were never handed out. ``lock`` runs the claims of the name one at a time; ``guard``
    is only ever held for a change of the sets."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.guard = threading.Lock()
        self.held: set[int] = set()
        self.abandoned: deque[int] = deque(maxlen=1024)
        self.unsure: set[str] = set()

    def excluded(self) -> list[int]:
        with self.guard:
            return sorted(self.held.union(self.abandoned))


_CLAIMS: dict[tuple[str, str | None, str], _Claims] = {}
_CLAIMS_GUARD = threading.Lock()


def _claims(database: PostgresDatabase, worker: str) -> _Claims:
    key = (database._conninfo, database.schema, worker)
    with _CLAIMS_GUARD:
        found = _CLAIMS.get(key)
        if found is None:
            found = _CLAIMS[key] = _Claims()
        return found


class Job(BaseModel):
    id: int
    tenant_id: str
    kind: str
    ref: str
    status: JobStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
    worker: str | None = None
    lease_until: datetime | None = None
    cancel_requested: bool = False
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


def _job(row: tuple[Any, ...]) -> Job:
    payload = dict(row[5] or {})
    payload.pop(ENQUEUE_TOKEN, None)
    return Job(
        id=row[0],
        tenant_id=row[1],
        kind=row[2],
        ref=row[3],
        status=row[4],
        payload=payload,
        attempts=row[6],
        worker=row[7],
        lease_until=row[8],
        cancel_requested=row[9],
        error=row[10],
        created_at=row[11],
        started_at=row[12],
        finished_at=row[13],
    )


def worker_name() -> str:
    """A name for this worker process that no other process has (host, process, random part)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class JobQueue:
    """The jobs of a server database (table ``nodestro_jobs`` of migration ``0004``)."""

    def __init__(self, database: PostgresDatabase, *, lease: timedelta = timedelta(seconds=60)) -> None:
        if lease <= timedelta(0):
            raise ValueError("The lease must be positive")
        self.database = database
        self.lease = lease

    def enqueue(self, tenant_id: str, kind: str, ref: str, payload: Mapping[str, Any] | None = None) -> Job | None:
        """Queue a job; ``None`` when the same work (tenant, kind, ref) is already queued or running.

        Never repeated (RT2-03). The row carries a client token (:data:`ENQUEUE_TOKEN`); when the connection is
        lost, the row with this token is the answer, whatever its status by now (a worker may have run it already).
        Without it the insert was not applied, or met an active job of the same work (the answer would have been
        ``None``); the two cannot be told apart, so the error of the lost connection is raised, as it is when the
        outcome cannot be shown (:meth:`PostgresDatabase._write_returning`)."""
        token = uuid.uuid4().hex
        rows = self.database._write_returning(
            "INSERT INTO nodestro_jobs (tenant_id, kind, ref, status, payload) VALUES (%s, %s, %s, 'queued', %s)"
            " ON CONFLICT (tenant_id, kind, ref) WHERE status IN ('queued', 'running') DO NOTHING"
            f" RETURNING {_COLUMNS}",
            (tenant_id, kind, ref, json.dumps({**dict(payload or {}), ENQUEUE_TOKEN: token})),
            if_lost=(
                f"SELECT {_COLUMNS} FROM nodestro_jobs"
                " WHERE tenant_id = %s AND kind = %s AND ref = %s AND payload ->> %s = %s",
                (tenant_id, kind, ref, ENQUEUE_TOKEN, token),
            ),
        )
        return _job(rows[0]) if rows else None

    def claim(self, worker: str, kinds: Iterable[str] = (RUN_JOB,)) -> Job | None:
        """The oldest queued job of ``kinds``, now running for ``worker`` with a fresh lease; ``None``: none.

        Never repeated (RT2-03): a repeated claim would take a second job and leave the first one running without
        anyone executing it. When the connection is lost and the server is seen to be done with the statement
        (:meth:`PostgresDatabase._write_returning`), the claimed job is the newest running job of ``worker`` of
        ``kinds`` that was never handed out; with none, the claim was not applied and the error is raised. When the
        outcome cannot be shown, the error is raised and the claim stays unsure: the next claim of these kinds for
        ``worker`` first adopts such a job (it may have committed late) before it claims a new one.

        Two rules make "the worker's job that was never handed out" exact (JQ-3, L-3): ``worker`` is a name no other
        process uses (:func:`worker_name`; a name given to a worker by configuration must be unique on the
        server), and the queue's record of handed-out jobs is shared by every :class:`JobQueue` of this process on
        the same database, so ``heartbeat`` and ``finish`` may go through another instance. A claim cannot run
        inside a transaction (lock order: the name's claim lock, then the process lock of the database)."""
        if self.database.in_transaction:
            raise RuntimeError("claim() cannot run inside a transaction")
        wanted = list(kinds)
        claims = _claims(self.database, worker)
        with claims.lock:
            with claims.guard:
                unsure = sorted(claims.unsure.intersection(wanted))
            if unsure:
                found = self.database._query(*self._unheld(worker, unsure, claims.excluded()))
                if found:
                    return self._hand_out(claims, found[0])
                with claims.guard:
                    claims.unsure.difference_update(unsure)
            try:
                rows = self.database._write_returning(
                    "UPDATE nodestro_jobs SET status = 'running', worker = %s, attempts = attempts + 1,"
                    " started_at = clock_timestamp(), lease_until = clock_timestamp() + %s"
                    " WHERE id = (SELECT id FROM nodestro_jobs WHERE status = 'queued' AND kind = ANY(%s)"
                    " ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)"
                    f" RETURNING {_COLUMNS}",
                    (worker, self.lease, wanted),
                    if_lost=self._unheld(worker, wanted, claims.excluded()),
                )
            except Exception:
                with claims.guard:
                    claims.unsure.update(wanted)
                raise
            return self._hand_out(claims, rows[0]) if rows else None

    @staticmethod
    def _unheld(worker: str, kinds: list[str], excluded: list[int]) -> tuple[str, tuple[Any, ...]]:
        """The read of the newest running job of ``worker`` and ``kinds`` that was never handed out."""
        return (
            f"SELECT {_COLUMNS} FROM nodestro_jobs WHERE worker = %s AND status = 'running'"
            " AND kind = ANY(%s) AND NOT (id = ANY(%s)) ORDER BY started_at DESC, id DESC LIMIT 1",
            (worker, kinds, excluded),
        )

    @staticmethod
    def _hand_out(claims: _Claims, row: tuple[Any, ...]) -> Job:
        job = _job(row)
        with claims.guard:
            claims.held.add(job.id)
        return job

    def _release(self, job_id: int, worker: str, *, abandoned: bool = False) -> None:
        """Forget a job :meth:`claim` handed to ``worker`` (it finished or its lease was lost); an ``abandoned`` one
        (its ``finish`` failed) is never handed out again."""
        claims = _claims(self.database, worker)
        with claims.guard:
            claims.held.discard(job_id)
            if abandoned:
                claims.abandoned.append(job_id)

    def heartbeat(self, job_id: int, worker: str) -> Job | None:
        """Renew the lease of a job ``worker`` runs; ``None`` when the job is no longer its own (lease lost)."""
        # Safe to repeat after a lost reply (JQ-3, tests/test_query_writes.py): it only renews the lease again.
        rows = self.database._query(
            "UPDATE nodestro_jobs SET lease_until = clock_timestamp() + %s"
            " WHERE id = %s AND worker = %s AND status = 'running'"
            f" RETURNING {_COLUMNS}",
            (self.lease, job_id, worker),
        )
        if not rows:
            self._release(job_id, worker)
            return None
        return _job(rows[0])

    def finish(self, job_id: int, worker: str, status: JobStatus, error: str | None = None) -> bool:
        """End a running job of ``worker``; ``False`` when it was no longer its own (it was reaped)."""
        if status not in ("done", "failed", "cancelled"):
            raise ValueError(f"A job cannot finish as {status!r}")
        try:
            count = self.database._write(
                "UPDATE nodestro_jobs SET status = %s, error = %s, finished_at = clock_timestamp(),"
                " lease_until = NULL WHERE id = %s AND worker = %s AND status = 'running'",
                (status, error, job_id, worker),
            )
        except BaseException:
            self._release(job_id, worker, abandoned=True)
            raise
        self._release(job_id, worker)
        return count == 1

    def request_cancel(self, tenant_id: str, kind: str, ref: str) -> Job | None:
        """Withdraw a queued job (returned as ``cancelled``) or ask the worker of a running one to stop.

        ``None`` when the work has no active job."""
        with self.database._transaction() as db:
            row = db.execute(
                "UPDATE nodestro_jobs SET status = 'cancelled', finished_at = clock_timestamp()"
                " WHERE tenant_id = %s AND kind = %s AND ref = %s AND status = 'queued'"
                f" RETURNING {_COLUMNS}",
                (tenant_id, kind, ref),
            ).fetchone()
            if row is None:
                row = db.execute(
                    "UPDATE nodestro_jobs SET cancel_requested = true"
                    " WHERE tenant_id = %s AND kind = %s AND ref = %s AND status = 'running'"
                    f" RETURNING {_COLUMNS}",
                    (tenant_id, kind, ref),
                ).fetchone()
        return _job(row) if row is not None else None

    def reap_expired(self) -> list[Job]:
        """Mark every running job whose lease has expired as interrupted (its worker died); returns them.

        Never repeated (RT2-03): after a lost reply a repeat would find nothing and report no job, so the error is
        raised instead. The jobs it may have marked are not reported; their runs are interrupted by the next
        :func:`reap` as runs without an active job (C-11, after twice the lease)."""
        rows = self.database._write_returning(
            "UPDATE nodestro_jobs SET status = 'interrupted', finished_at = clock_timestamp(),"
            " error = 'The worker stopped renewing its lease'"
            " WHERE id IN (SELECT id FROM nodestro_jobs WHERE status = 'running' AND lease_until < clock_timestamp()"
            " FOR UPDATE SKIP LOCKED)"
            f" RETURNING {_COLUMNS}"
        )
        return [_job(row) for row in rows]

    def report(self, job: Job, worker: str, progress: Mapping[str, Any]) -> Job | None:
        """Merge ``progress`` into the payload of a job ``worker`` runs and renew its lease (one statement); ``None``
        when the job is no longer its own (lease lost)."""
        # Safe to repeat after a lost reply (JQ-3, tests/test_query_writes.py): the same keys are merged again.
        rows = self.database._query(
            "UPDATE nodestro_jobs SET payload = payload || %s::jsonb, lease_until = clock_timestamp() + %s"
            " WHERE tenant_id = %s AND id = %s AND worker = %s AND status = 'running'"
            f" RETURNING {_COLUMNS}",
            (json.dumps(dict(progress)), self.lease, job.tenant_id, job.id, worker),
        )
        if not rows:
            self._release(job.id, worker)
            return None
        return _job(rows[0])

    def latest(self, tenant_id: str, kind: str, ref: str) -> Job | None:
        """The newest job of a piece of work (active or ended), if any."""
        rows = self.database._query(
            f"SELECT {_COLUMNS} FROM nodestro_jobs WHERE tenant_id = %s AND kind = %s AND ref = %s"
            " ORDER BY id DESC LIMIT 1",
            (tenant_id, kind, ref),
        )
        return _job(rows[0]) if rows else None

    def get(self, job_id: int) -> Job | None:
        rows = self.database._query(f"SELECT {_COLUMNS} FROM nodestro_jobs WHERE id = %s", (job_id,))
        return _job(rows[0]) if rows else None

    def active(self, tenant_id: str, kind: str, ref: str) -> Job | None:
        """The queued or running job of a piece of work, if any."""
        rows = self.database._query(_ACTIVE, (tenant_id, kind, ref))
        return _job(rows[0]) if rows else None

    def jobs(self, tenant_id: str, kind: str, ref: str) -> list[Job]:
        """Every job of a piece of work, oldest first."""
        rows = self.database._query(
            f"SELECT {_COLUMNS} FROM nodestro_jobs WHERE tenant_id = %s AND kind = %s AND ref = %s ORDER BY id",
            (tenant_id, kind, ref),
        )
        return [_job(row) for row in rows]

    def cleanup(self, *, older_than: timedelta = timedelta(days=7)) -> int:
        """Delete finished jobs older than ``older_than``; returns how many."""
        return self.database._write(
            "DELETE FROM nodestro_jobs WHERE status NOT IN ('queued', 'running')"
            " AND finished_at < clock_timestamp() - %s",
            (older_than,),
        )


class PostgresEventLog:
    """The events of one tenant's runs in ``nodestro_run_events``, readable by every process.

    :meth:`append` buffers; the buffer is written by :meth:`flush` (a worker calls it a few times per second) and at
    once for events that end or pause a run, so a stream never waits long and the database is not written per token.
    """

    #: Event types written at once (the end of a run is never left in a buffer).
    IMMEDIATE = frozenset({"run_started", "run_finished", "run_paused", "run_cancelling", "approval_requested"})

    def __init__(self, database: PostgresDatabase, tenant_id: str, *, max_buffer: int = 200) -> None:
        self.database = database
        self.tenant_id = tenant_id
        self.max_buffer = max_buffer
        self._buffer: list[RunEvent] = []
        self._lock = threading.Lock()

    def append(self, event: RunEvent) -> None:
        with self._lock:
            self._buffer.append(event)
            full = len(self._buffer) >= self.max_buffer
        if full or event.type in self.IMMEDIATE:
            self.flush()

    def flush(self) -> int:
        """Write the buffered events; returns how many."""
        with self._lock:
            events, self._buffer = self._buffer, []
        if not events:
            return 0
        rows = [(self.tenant_id, event.run_id, event.seq, event.model_dump_json()) for event in events]
        try:
            with self.database._transaction() as db, db.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO nodestro_run_events (tenant_id, run_id, seq, event) VALUES (%s, %s, %s, %s)"
                    " ON CONFLICT DO NOTHING",
                    rows,
                )
        except BaseException:
            with self._lock:
                self._buffer[:0] = events
            raise
        return len(events)

    def events(self, run_id: str, *, after: int = 0, limit: int = 1000) -> list[RunEvent]:
        """The stored events of a run with ``seq`` above ``after``, in order."""
        rows = self.database._query(
            "SELECT event FROM nodestro_run_events WHERE tenant_id = %s AND run_id = %s AND seq > %s"
            " ORDER BY seq LIMIT %s",
            (self.tenant_id, run_id, after, limit),
        )
        return [RunEvent.model_validate_json(row[0]) for row in rows]

    def delete(self, run_id: str) -> int:
        return self.database._write(
            "DELETE FROM nodestro_run_events WHERE tenant_id = %s AND run_id = %s", (self.tenant_id, run_id)
        )

    @staticmethod
    def cleanup(database: PostgresDatabase, *, older_than: timedelta = timedelta(days=2)) -> int:
        """Delete the events of every tenant older than ``older_than`` (a stream only replays recent runs)."""
        return database._write(
            "DELETE FROM nodestro_run_events WHERE created_at < clock_timestamp() - %s", (older_than,)
        )


class QueuedRuns:
    """Connects one tenant's :class:`~nodestro_runtime.engine.RunManager` to the job queue (API processes).

    After :meth:`attach`, starting or continuing a run stores it and queues a job instead of executing it,
    cancelling a run a worker holds goes through the queue, and event streams and waits follow the worker's run
    through ``nodestro_run_events``."""

    def __init__(self, queue: JobQueue, tenant_id: str, runs: Any) -> None:
        self.queue = queue
        self.tenant_id = tenant_id
        self.runs = runs

    def attach(self) -> None:
        log = PostgresEventLog(self.queue.database, self.tenant_id)
        self.runs.dispatch = self.dispatch
        self.runs.remote_cancel = self.cancel
        self.runs.remote_events = lambda run_id, after: log.events(run_id, after=after)

    def dispatch(self, run: Run) -> None:
        if self.queue.enqueue(self.tenant_id, RUN_JOB, run.id) is None:
            raise NodestroError("run.already_queued", "This run is already queued or executing", run=run.id)

    def cancel(self, run: Run) -> bool:
        job = self.queue.request_cancel(self.tenant_id, RUN_JOB, run.id)
        if job is None:
            return False
        if job.status == "cancelled":
            # Withdrawn before any worker took it: nobody else will store its end.
            self.runs.cancel_unclaimed(run.id)
        return True


def reindex_job(job: Job) -> ReindexJob:
    """A reindex job of the queue as the knowledge base's :class:`~nodestro_runtime.knowledge.ReindexJob`: queued
    and running are ``running``; a reaped job is ``failed`` with ``knowledge.reindex_interrupted`` (start it again to
    continue where it stopped). The error is stored as ``code: message``."""
    state: Literal["running", "done", "failed", "cancelled"]
    code: str | None = None
    message: str | None = None
    if job.status in ("queued", "running"):
        state = "running"
    elif job.status == "interrupted":
        state, code = "failed", "knowledge.reindex_interrupted"
        message = "The worker building the semantic index stopped; start it again to continue"
    else:
        state = cast(Literal["done", "failed", "cancelled"], job.status)
    if job.error and code is None:
        code, _, message = job.error.partition(": ")
        if not message:
            code, message = "knowledge.reindex_failed", job.error
    return ReindexJob(
        state=state,
        model=str(job.payload.get("model", "")),
        completed=int(job.payload.get("completed", 0) or 0),
        total=int(job.payload.get("total", 0) or 0),
        error_code=code if state in ("failed", "cancelled") else None,
        error=message if state in ("failed", "cancelled") else None,
        started_at=job.started_at or job.created_at,
        finished_at=job.finished_at,
    )


class QueuedReindex:
    """Connects one tenant's :class:`~nodestro_runtime.knowledge.KnowledgeBase` to the job queue (API processes,
    RT-046): after :meth:`attach` a reindex is queued for a worker instead of running in a thread of this process,
    and its state and cancellation go through the queue, so every API process sees the same job."""

    def __init__(self, queue: JobQueue, tenant_id: str, knowledge: Any) -> None:
        self.queue = queue
        self.tenant_id = tenant_id
        self.knowledge = knowledge

    def attach(self) -> None:
        self.knowledge.reindex_queue = self

    def start(self, model: str) -> None:
        if self.queue.enqueue(self.tenant_id, REINDEX_JOB, REINDEX_REF, {"model": model}) is None:
            raise NodestroError("knowledge.reindex_running", "The semantic index is already being built")

    def cancel(self) -> None:
        self.queue.request_cancel(self.tenant_id, REINDEX_JOB, REINDEX_REF)

    def current(self) -> ReindexJob | None:
        job = self.queue.latest(self.tenant_id, REINDEX_JOB, REINDEX_REF)
        return reindex_job(job) if job is not None else None


class ReindexWorker:
    """Claims reindex jobs and runs them in this process, one at a time, renewing their leases (a worker process,
    RT-046).

    ``workspaces(tenant_id)`` returns the tenant's workspace (an object with ``knowledge``). While the reindex runs in
    a thread, the worker reports its progress (which renews the lease) every ``heartbeat`` seconds; it stops the
    reindex (which continues where it stopped when started again) when a person cancels it, the tenant is suspended
    (``tenant_active``), the lease is lost or the worker stops."""

    def __init__(
        self,
        queue: JobQueue,
        workspaces: WorkspaceOf,
        *,
        name: str | None = None,
        heartbeat: float | None = None,
        poll: float = 1.0,
        tenant_active: Callable[[str], bool] | None = None,
    ) -> None:
        self.queue = queue
        self.workspaces = workspaces
        self.name = name or worker_name()
        self.heartbeat_seconds = heartbeat if heartbeat is not None else queue.lease.total_seconds() / 3
        self.poll = poll
        self.tenant_active = tenant_active

    async def run_once(self, stop: asyncio.Event | None = None) -> Job | None:
        """Claim one reindex job and run it to its end; ``None`` when none was queued."""
        job: Job | None = await asyncio.to_thread(self.queue.claim, self.name, (REINDEX_JOB,))
        if job is None:
            return None
        await self._execute(job, stop or asyncio.Event())
        return job

    async def _execute(self, job: Job, stop: asyncio.Event) -> None:
        if self.tenant_active is not None and not await asyncio.to_thread(self.tenant_active, job.tenant_id):
            await asyncio.to_thread(
                self.queue.finish, job.id, self.name, "failed", "identity.tenant_suspended: the tenant is suspended"
            )
            return
        try:
            knowledge = self.workspaces(job.tenant_id).knowledge
        except Exception as exc:
            logger.exception("Opening tenant %s for reindex job %s failed", job.tenant_id, job.id)
            code = exc.code if isinstance(exc, NodestroError) else "knowledge.reindex_failed"
            await asyncio.to_thread(self.queue.finish, job.id, self.name, "failed", f"{code}: {exc}")
            return
        cancel = threading.Event()
        progress: dict[str, int] = {}

        def note(done: int, total: int) -> None:
            progress["completed"], progress["total"] = done, total

        running = asyncio.ensure_future(
            asyncio.to_thread(
                knowledge.execute_reindex, str(job.payload.get("model", "")), cancel=cancel, progress=note
            )
        )
        lost = False
        stopped_by: str | None = None
        while not running.done():
            await asyncio.wait({running}, timeout=self.heartbeat_seconds)
            if running.done() or lost:
                continue
            beat = await asyncio.to_thread(self.queue.report, job, self.name, dict(progress))
            if beat is None:
                lost = True
                cancel.set()
                logger.error("Reindex job %s lost its lease; stopping it", job.id)
            elif beat.cancel_requested and not cancel.is_set():
                cancel.set()
            elif stop.is_set() and not cancel.is_set():
                stopped_by = "the worker stopped"
                cancel.set()
            elif self.tenant_active is not None and not cancel.is_set():
                try:
                    active = await asyncio.to_thread(self.tenant_active, job.tenant_id)
                except Exception:  # unknown: stop, as for a run (SR-03)
                    active = False
                if not active:
                    stopped_by = "identity.tenant_suspended"
                    cancel.set()
        outcome = running.result()
        if lost:
            return
        if progress:
            await asyncio.to_thread(self.queue.report, job, self.name, dict(progress))
        status: JobStatus = outcome.state if outcome.state in ("done", "failed", "cancelled") else "failed"
        error = f"{outcome.error_code}: {outcome.error}" if outcome.error_code else None
        if stopped_by == "identity.tenant_suspended" and status == "cancelled":
            status, error = "failed", "identity.tenant_suspended: the tenant was suspended; start it again to continue"
        elif stopped_by is not None and status == "cancelled":
            error = f"knowledge.cancelled: {stopped_by}; start it again to continue"
        if not await asyncio.to_thread(self.queue.finish, job.id, self.name, status, error):
            logger.warning("Reindex job %s was no longer this worker's when it ended", job.id)

    async def serve(self, stop: asyncio.Event) -> None:
        """Run reindex jobs until ``stop`` is set (a running one is stopped then; started again it continues)."""
        while not stop.is_set():
            try:
                job = await self.run_once(stop)
            except Exception:
                logger.exception("The reindex worker loop failed; retrying")
                job = None
            if job is None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll)


#: The workspace (or anything with ``runs``) of a tenant, opened by the worker on demand.
WorkspaceOf = Callable[[str], Any]


#: How long a worker reuses a tenant's "active" answer before it asks again (seconds, see :class:`RunWorker`).
TENANT_CHECK_SECONDS = 2.0


class RunWorker:
    """Claims run jobs and executes them in this process, renewing their leases (a worker process).

    ``workspaces(tenant_id)`` returns the tenant's workspace (an object with ``runs``); the worker gives its run
    manager an event sink (:class:`PostgresEventLog`) and never dispatches runs itself."""

    def __init__(
        self,
        queue: JobQueue,
        workspaces: WorkspaceOf,
        *,
        name: str | None = None,
        heartbeat: float | None = None,
        poll: float = 1.0,
        flush: float = 0.25,
        max_runs: int = DEFAULT_MAX_RUNS,
        max_waiting: int = DEFAULT_MAX_WAITING,
        tenant_active: Callable[[str], bool] | None = None,
        tenant_check: float = TENANT_CHECK_SECONDS,
    ) -> None:
        check_capacity(max_runs, max_waiting)
        #: Whether a tenant may still execute runs (the identity backend's answer; ``None``: not asked). :meth:`owns`
        #: asks it before every model request and tool call, so a suspended tenant's run stops at its next step
        #: (security review SR-03), not only at the worker's periodic sweep.
        self.tenant_active = tenant_active
        #: An "active" answer is reused for this long (seconds); "suspended" is never cached.
        self.tenant_check = tenant_check
        self._tenant_seen: dict[str, float] = {}
        self.max_runs = max_runs
        #: The waiting capacity of this worker, shared by the tool gates of all tenants it executes runs of.
        self.slots = WaitingSlots(max_waiting)
        self._held: set[asyncio.Task[None]] = set()
        #: The most runs held at the same time so far (for tests and monitoring).
        self.peak_held = 0
        self.queue = queue
        self.workspaces = workspaces
        self.name = name or worker_name()
        self.heartbeat_seconds = heartbeat if heartbeat is not None else queue.lease.total_seconds() / 3
        self.poll = poll
        self.flush_seconds = flush
        self._logs: dict[str, PostgresEventLog] = {}
        #: The job of every run executing here, by ``(tenant, run)``: what :meth:`owns` asks the queue about.
        self._jobs: dict[tuple[str, str], int] = {}
        #: The worker's own threads for its queue work (claims, lease renewal, event flushes; C-08), one per held run
        #: and one for claims: they never wait behind agents or other work in asyncio's default executor.
        self._queue_threads = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_runs + 1, thread_name_prefix="nodestro-lease"
        )

    async def _off_loop(self, call: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._queue_threads, call, *args)

    def _manager(self, tenant_id: str) -> Any:
        runs = self.workspaces(tenant_id).runs
        log = self._logs.get(tenant_id)
        if log is None:
            log = self._logs[tenant_id] = PostgresEventLog(self.queue.database, tenant_id)
        runs.event_sink = log.append
        gate = getattr(runs, "tool_gate", None)
        if gate is not None:
            gate.slots = self.slots
        runs.owns = lambda run_id: self.owns(tenant_id, run_id)
        runs.dispatch = None
        runs.remote_cancel = None
        runs.remote_events = None
        return runs

    def owns(self, tenant_id: str, run_id: str) -> bool:
        """Whether this worker still holds the job of a run it executes (renewing its lease); the run manager asks
        before every model request and tool call (M11), so a worker that lost its lease stops before it pays for
        or does anything more, not only at its next heartbeat."""
        job_id = self._jobs.get((tenant_id, run_id))
        if job_id is None or not self._tenant_allows(tenant_id):
            return False
        return self.queue.heartbeat(job_id, self.name) is not None

    def _tenant_allows(self, tenant_id: str) -> bool:
        """Whether ``tenant_id`` is active (:attr:`tenant_active`), with an "active" answer reused for
        :attr:`tenant_check` seconds. A failing check raises: the run manager counts that as lost."""
        if self.tenant_active is None:
            return True
        seen = self._tenant_seen.get(tenant_id)
        if seen is not None and time.monotonic() - seen < self.tenant_check:
            return True
        if not self.tenant_active(tenant_id):
            self._tenant_seen.pop(tenant_id, None)
            logger.warning("Tenant %s is suspended: its runs stop at their next step", tenant_id)
            return False
        self._tenant_seen[tenant_id] = time.monotonic()
        return True

    async def run_once(self) -> Job | None:
        """Claim one job and execute it to its end (or pause); ``None`` when the queue was empty."""
        job = self.queue.claim(self.name, (RUN_JOB,))
        if job is None:
            return None
        await self._execute(job)
        return job

    async def _execute(self, job: Job) -> None:
        key = (job.tenant_id, job.ref)
        self._jobs[key] = job.id
        try:
            await self._execute_held(job)
        finally:
            if self._jobs.get(key) == job.id:
                del self._jobs[key]

    async def _execute_held(self, job: Job) -> None:
        try:
            runs = self._manager(job.tenant_id)
            runs.execute_claimed(job.ref)
        except Exception as exc:  # the run cannot be executed (gone, finished, cancelled): the job ends here
            # Value-free (TABRION MIG-NOD-3): the job's error is shown to clients, and a database error's text may hold
            # the server's DETAIL values.
            reason = str(exc) if isinstance(exc, NodestroError) else failure_text(exc)
            logger.warning("Job %s (run %s) cannot be executed: %s", job.id, job.ref, reason)
            code = exc.code if isinstance(exc, NodestroError) else type(exc).__name__
            self.queue.finish(job.id, self.name, "failed", f"{code}: {reason}")
            return
        log = self._logs[job.tenant_id]
        waiting = asyncio.ensure_future(runs.wait(job.ref))
        last_beat = time.monotonic()
        cancel_sent = False
        lost = False
        try:
            while not waiting.done():
                await asyncio.wait({waiting}, timeout=self.flush_seconds)
                await self._off_loop(_flush_quietly, log)
                if time.monotonic() - last_beat >= self.heartbeat_seconds and not waiting.done() and not lost:
                    last_beat = time.monotonic()
                    beat = await self._off_loop(self.queue.heartbeat, job.id, self.name)
                    if beat is None:
                        # The lease was lost (the reaper marked the run interrupted, C-06): the run is no longer
                        # this worker's. Its execution stops without storing anything more of it.
                        lost = True
                        logger.error("Job %s lost its lease; stopping run %s", job.id, job.ref)
                        runs.abandon(job.ref, because=f"job {job.id} lost its lease")
                    elif beat.cancel_requested and not cancel_sent:
                        cancel_sent = True
                        runs.cancel(job.ref)
            run = waiting.result()
        finally:
            _flush_quietly(log)
        if runs.lost_fence(job.ref) and not lost:
            # The run was changed by another process while it executed here (C-06): its outcome was not stored.
            self.queue.finish(job.id, self.name, "failed", "run.fence_lost: the run was changed elsewhere")
            return
        if lost:
            return
        status: JobStatus = "done"
        if run is None or run.status == "failed":
            status = "failed"
        elif run.status == "cancelled":
            status = "cancelled"
        if not self.queue.finish(job.id, self.name, status, run.error if run is not None else "run missing"):
            logger.warning("Job %s of run %s was no longer this worker's when it ended", job.id, job.ref)

    @property
    def held(self) -> int:
        return len(self._held)

    @property
    def executing(self) -> int:
        """Held runs that do not wait for a tool approval."""
        return max(0, len(self._held) - self.slots.waiting)

    def can_claim(self) -> bool:
        """Claim only below ``max_runs`` and only while every held run waits (none executes)."""
        return len(self._held) < self.max_runs and self.executing == 0

    async def claim_next(self) -> Job | None:
        """Claim one job if :meth:`can_claim` and start executing it as a held run; ``None`` otherwise."""
        if not self.can_claim():
            return None
        job: Job | None = await self._off_loop(self.queue.claim, self.name, (RUN_JOB,))
        if job is None:
            return None
        task = asyncio.ensure_future(self._execute(job))
        self._held.add(task)
        self.peak_held = max(self.peak_held, len(self._held))
        task.add_done_callback(self._release)
        return job

    def _release(self, task: asyncio.Task[None]) -> None:
        self._held.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("A held job failed", exc_info=task.exception())

    async def serve(self, stop: asyncio.Event) -> None:
        """Execute jobs until ``stop`` is set, holding up to ``max_runs`` runs (see the module documentation); then
        wait for the held runs to end."""
        try:
            while not stop.is_set():
                try:
                    job = await self.claim_next()
                except Exception:
                    logger.exception("The worker loop failed; retrying")
                    job = None
                if job is not None:
                    continue
                waits: set[asyncio.Future[Any]] = {asyncio.ensure_future(stop.wait()), *self._held}
                done, _ = await asyncio.wait(waits, timeout=self.poll, return_when=asyncio.FIRST_COMPLETED)
                for future in waits - set(self._held):
                    if future not in done:
                        future.cancel()
        finally:
            if self._held:
                await asyncio.gather(*self._held, return_exceptions=True)


def check_capacity(max_runs: int, max_waiting: int) -> None:
    """Refuse limits that would let a worker hold no run, or let every held run wait with none left to claim."""
    if max_runs < 1:
        raise ValueError("max_runs must be at least 1")
    if not 0 <= max_waiting < max_runs:
        raise ValueError("max_waiting must be at least 0 and less than max_runs")


def _flush_quietly(log: PostgresEventLog) -> None:
    try:
        log.flush()
    except Exception:  # a stream misses events; the run itself goes on
        logger.exception("Writing run events failed")


def reap(queue: JobQueue, workspaces: WorkspaceOf, *, orphans_after: timedelta | None = None) -> list[Job]:
    """Mark the runs of jobs whose worker died as interrupted (any process may call it periodically).

    Then the runs stored as queued or running that no active job executes (C-11: imported from a desktop, restored,
    a job that was never queued, or a reaper that failed earlier) are marked interrupted too, once they have not
    changed for ``orphans_after`` (default: twice the lease), so a run that is being queued right now is left alone.
    Returns the reaped jobs."""
    reaped = queue.reap_expired()
    for job in reaped:
        if job.kind != RUN_JOB:
            continue
        try:
            workspaces(job.tenant_id).runs.interrupt(job.ref)
        except Exception:
            logger.exception("Marking run %s of tenant %s as interrupted failed", job.ref, job.tenant_id)
    for tenant_id, run_id in orphaned_runs(
        queue, older_than=queue.lease * 2 if orphans_after is None else orphans_after
    ):
        try:
            if workspaces(tenant_id).runs.interrupt(run_id):
                logger.warning(
                    "Run %s of tenant %s was stored as executing without a job: interrupted", run_id, tenant_id
                )
        except Exception:
            logger.exception("Marking run %s of tenant %s as interrupted failed", run_id, tenant_id)
    return reaped


def orphaned_runs(queue: JobQueue, *, older_than: timedelta) -> list[tuple[str, str]]:
    """``(tenant, run)`` of every top-level run stored as queued or running, unchanged for ``older_than``, that no
    queued or running job executes (sub-workflow runs follow their top-level run; imported traces never execute)."""
    rows = queue.database._query(
        "SELECT d.tenant_id, d.id FROM nodestro_documents d"
        " WHERE d.collection = 'runs' AND d.data ->> 'status' IN ('queued', 'running')"
        " AND d.data ->> 'parent_run_id' IS NULL AND d.data ->> 'imported_at' IS NULL"
        " AND d.updated_at < clock_timestamp() - %s"
        " AND NOT EXISTS (SELECT 1 FROM nodestro_jobs j WHERE j.tenant_id = d.tenant_id AND j.kind = %s"
        " AND j.ref = d.id AND j.status IN ('queued', 'running'))"
        " ORDER BY d.tenant_id, d.id",
        (older_than, RUN_JOB),
    )
    return [(str(row[0]), str(row[1])) for row in rows]


class LeaderLock:
    """A session-level advisory lock: at most one connection holds it, and it is released when that one closes.

    The scheduler of a server fires schedules only while :meth:`acquire` returned ``True``; a leader that dies loses
    the lock with its connection, and another process takes over at its next attempt."""

    def __init__(self, database: PostgresDatabase, key: int = SCHEDULER_LOCK) -> None:
        self.database = database
        self.key = key
        self.held = False

    def acquire(self) -> bool:
        # The lock lives on the database's own session connection: when the server dropped it, the lock is gone
        # (another process may hold it now), so it is checked and taken again instead of trusted.
        if self.held and self.database.holds_session_lock(self.key):
            return True
        self.held = self.database.try_session_lock(self.key)
        return self.held

    def release(self) -> None:
        if self.held and not self.database.closed:
            self.database.release_session_lock(self.key)
        self.held = False


class ScheduleFirings:
    """Which schedule slots have fired (``nodestro_schedule_firings``): every slot fires at most once."""

    def __init__(self, database: PostgresDatabase, tenant_id: str) -> None:
        self.database = database
        self.tenant_id = tenant_id

    def claim(self, schedule_id: str, slot: datetime) -> bool:
        """Record that ``slot`` of a schedule fires now; ``False`` when it fired already (do not start a run)."""
        if slot.tzinfo is None:
            raise ValueError("A schedule slot needs a time zone")
        count = self.database._write(
            "INSERT INTO nodestro_schedule_firings (tenant_id, schedule_id, slot) VALUES (%s, %s, %s)"
            " ON CONFLICT DO NOTHING",
            (self.tenant_id, schedule_id, slot),
        )
        return count == 1

    def record_run(self, schedule_id: str, slot: datetime, run_id: str) -> None:
        self.database._write(
            "UPDATE nodestro_schedule_firings SET run_id = %s WHERE tenant_id = %s AND schedule_id = %s AND slot = %s",
            (run_id, self.tenant_id, schedule_id, slot),
        )

    def fired(self, schedule_id: str) -> list[tuple[datetime, str | None]]:
        rows = self.database._query(
            "SELECT slot, run_id FROM nodestro_schedule_firings WHERE tenant_id = %s AND schedule_id = %s"
            " ORDER BY slot",
            (self.tenant_id, schedule_id),
        )
        return [(cast(datetime, row[0]), cast("str | None", row[1])) for row in rows]


__all__ = [
    "RUN_JOB",
    "SCHEDULER_LOCK",
    "TENANT_CHECK_SECONDS",
    "Job",
    "JobQueue",
    "JobStatus",
    "LeaderLock",
    "PostgresEventLog",
    "QueuedRuns",
    "RunWorker",
    "ScheduleFirings",
    "orphaned_runs",
    "reap",
    "worker_name",
]
