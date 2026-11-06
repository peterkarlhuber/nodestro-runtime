"""The semantic reindex as a job of the server's queue (RT-046; needs ``NODESTRO_TEST_POSTGRES``).

An API process queues it (``QueuedReindex``), a worker runs it (``ReindexWorker``) with a lease; every process reads
the same job. The desktop path (a thread of the process) is covered by ``test_knowledge_retrieval.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
import threading
from types import SimpleNamespace
from typing import Any

import postgres_support
import pytest
from test_knowledge_retrieval import DOCS, ConceptEmbeddings

from nodestro_runtime.knowledge import KnowledgeBase
from nodestro_runtime.problems import NodestroError

pytest.importorskip("psycopg")

from nodestro_runtime.jobs import (
    REINDEX_JOB,
    REINDEX_REF,
    RUN_JOB,
    Job,
    JobQueue,
    QueuedReindex,
    ReindexWorker,
    reap,
    reindex_job,
)
from nodestro_runtime.postgres_tables import knowledge_tables
from nodestro_runtime.storage_postgres import PostgresDatabase


class SlowEmbeddings(ConceptEmbeddings):
    """Waits for ``release`` before every batch, so a test can act while the reindex runs."""

    def __init__(self, name: str = "concepts:1") -> None:
        super().__init__(name)
        self.entered = threading.Event()
        self.release = threading.Event()

    def embed_documents(self, texts):  # type: ignore[no-untyped-def]
        self.entered.set()
        self.release.wait(20)
        return super().embed_documents(texts)


class Side:
    """One process: its own connection and its own knowledge base of a tenant."""

    def __init__(self, dsn: str, schema: str, model: ConceptEmbeddings) -> None:
        self.database = PostgresDatabase(dsn, schema=schema)
        self.model = model
        self.opened: dict[str, KnowledgeBase] = {}

    def knowledge(self, tenant: str) -> KnowledgeBase:
        if tenant not in self.opened:
            knowledge = KnowledgeBase(knowledge_tables(self.database, tenant))
            knowledge.retrieval = lambda: ("hybrid", "concepts:1")  # type: ignore[assignment,return-value]
            knowledge.embedding_model = lambda _name: self.model
            self.opened[tenant] = knowledge
        return self.opened[tenant]

    def workspace(self, tenant: str) -> Any:
        return SimpleNamespace(knowledge=self.knowledge(tenant))

    def close(self) -> None:
        for knowledge in self.opened.values():
            knowledge.close()
        self.database.close()


@pytest.fixture
def sides() -> Iterator[Callable[..., Side]]:
    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    setup = PostgresDatabase(dsn, schema=schema)
    setup.migrate()
    setup.close()
    made: list[Side] = []

    def factory(model: ConceptEmbeddings | None = None) -> Side:
        side = Side(dsn, schema, model or ConceptEmbeddings())
        made.append(side)
        return side

    yield factory
    for side in made:
        side.close()
    postgres_support.drop_schema(schema)


def _api(side: Side, tenant: str = "acme") -> KnowledgeBase:
    knowledge = side.knowledge(tenant)
    QueuedReindex(JobQueue(side.database), tenant, knowledge).attach()
    return knowledge


def _worker(side: Side, **options: Any) -> ReindexWorker:
    return ReindexWorker(JobQueue(side.database), side.workspace, name="worker-1", **options)


def _fill(knowledge: KnowledgeBase) -> None:
    for name, text in DOCS.items():
        knowledge.add(text.encode(), name=name)


def test_the_api_queues_a_reindex_and_a_worker_builds_it(sides: Callable[..., Side]) -> None:
    api_side, worker_side = sides(), sides()
    knowledge = _api(api_side)
    _fill(knowledge)
    status = knowledge.start_reindex()
    assert status.job is not None and status.job.state == "running" and status.job.model == "concepts:1"
    assert status.state == "off"  # nothing was built in the API process
    with pytest.raises(NodestroError) as twice:
        knowledge.start_reindex()
    assert twice.value.code == "knowledge.reindex_running"
    other_api = _api(sides(), "acme")  # another API process of the same server: the same single job
    with pytest.raises(NodestroError):
        other_api.start_reindex()
    assert knowledge.reindexing() is False  # nothing runs in this process
    with pytest.raises(NodestroError) as dropping:
        knowledge.drop_vectors()
    assert dropping.value.code == "knowledge.reindex_running"

    job = asyncio.run(_worker(worker_side).run_once())
    assert job is not None and job.kind == REINDEX_JOB and job.ref == REINDEX_REF
    status = knowledge.index_status()
    assert status.state == "ready" and status.embedded == status.chunks == 4
    assert status.job is not None and status.job.state == "done" and status.job.error_code is None
    assert status.job.completed == status.job.total == 4
    assert other_api.index_status().job == status.job  # every process reads the same job
    assert asyncio.run(_worker(worker_side).run_once()) is None  # nothing left
    assert knowledge.start_reindex().job.state == "running"  # type: ignore[union-attr]  # once ended, again


def test_tenants_have_their_own_reindex_jobs(sides: Callable[..., Side]) -> None:
    api_side, worker_side = sides(), sides()
    alpha, beta = _api(api_side, "alpha"), _api(api_side, "beta")
    _fill(alpha)
    beta.add(b"Beta keeps a single note about money.", name="beta.md")
    alpha.start_reindex()
    assert beta.index_status().job is None
    beta.start_reindex()  # not blocked by alpha's job
    worker = _worker(worker_side)
    assert asyncio.run(worker.run_once()) is not None and asyncio.run(worker.run_once()) is not None
    assert alpha.index_status().embedded == 4 and beta.index_status().embedded == 1
    assert len(beta.search("refund money back", mode="vector", k=10)) == 1


def test_a_person_cancels_a_running_reindex_from_any_api_process(sides: Callable[..., Side]) -> None:
    slow = SlowEmbeddings()
    api_side, worker_side = sides(), sides(slow)
    knowledge = _api(api_side)
    _fill(knowledge)
    knowledge.start_reindex()
    worker = _worker(worker_side, heartbeat=0.05)

    async def scenario() -> Job | None:
        running = asyncio.ensure_future(worker.run_once())
        assert await asyncio.to_thread(slow.entered.wait, 20)
        await asyncio.to_thread(_api(sides(), "acme").cancel_reindex)
        await asyncio.sleep(0.3)  # the worker reads the request with its next heartbeat
        slow.release.set()
        return await running

    assert asyncio.run(scenario()) is not None
    job = knowledge.index_status().job
    assert job is not None and (job.state, job.error_code) == ("cancelled", "knowledge.cancelled")
    # Started again, the next worker continues and completes it.
    slow.release.set()
    knowledge.start_reindex()
    assert asyncio.run(_worker(worker_side).run_once()) is not None
    assert knowledge.index_status().state == "ready"


def test_a_queued_reindex_is_withdrawn_at_once(sides: Callable[..., Side]) -> None:
    api_side, worker_side = sides(), sides()
    knowledge = _api(api_side)
    _fill(knowledge)
    knowledge.start_reindex()
    status = knowledge.cancel_reindex()
    assert status.job is not None and status.job.state == "cancelled"
    assert asyncio.run(_worker(worker_side).run_once()) is None  # no worker ever takes it


def test_a_suspended_tenants_reindex_does_not_start_or_stops(sides: Callable[..., Side]) -> None:
    slow = SlowEmbeddings()
    api_side, worker_side = sides(), sides(slow)
    knowledge = _api(api_side)
    _fill(knowledge)
    knowledge.start_reindex()
    suspended = {"acme"}
    worker = _worker(worker_side, heartbeat=0.05, tenant_active=lambda tenant: tenant not in suspended)
    assert asyncio.run(worker.run_once()) is not None
    job = knowledge.index_status().job
    assert job is not None and (job.state, job.error_code) == ("failed", "identity.tenant_suspended")
    assert not slow.entered.is_set()  # no embedding request was made

    suspended.clear()
    knowledge.start_reindex()

    async def scenario() -> None:
        running = asyncio.ensure_future(worker.run_once())
        assert await asyncio.to_thread(slow.entered.wait, 20)
        suspended.add("acme")
        await asyncio.sleep(0.3)
        slow.release.set()
        await running

    asyncio.run(scenario())
    job = knowledge.index_status().job
    assert job is not None and (job.state, job.error_code) == ("failed", "identity.tenant_suspended")
    assert knowledge.index_status().active is None  # the switch never happened


def test_a_failing_model_fails_the_job_with_its_code(sides: Callable[..., Side]) -> None:
    down = ConceptEmbeddings()
    api_side, worker_side = sides(), sides(down)
    knowledge = _api(api_side)
    _fill(knowledge)
    knowledge.start_reindex()
    down.down = True
    assert asyncio.run(_worker(worker_side).run_once()) is not None
    job = knowledge.index_status().job
    assert job is not None and (job.state, job.error_code) == ("failed", "local.unreachable")
    assert job.error and "does not answer" in job.error


def test_the_reaper_interrupts_the_reindex_of_a_dead_worker(sides: Callable[..., Side]) -> None:
    api_side, worker_side = sides(), sides()
    knowledge = _api(api_side)
    _fill(knowledge)
    knowledge.start_reindex()
    queue = JobQueue(worker_side.database)
    claimed = queue.claim("dead-worker", (REINDEX_JOB,))
    assert claimed is not None
    worker_side.database._write("UPDATE nodestro_jobs SET lease_until = clock_timestamp() - interval '1 second'", ())
    touched: list[str] = []
    reaped = reap(queue, lambda tenant: touched.append(tenant))  # a reindex has no run to interrupt
    assert [item.id for item in reaped] == [claimed.id] and touched == []
    job = knowledge.index_status().job
    assert job is not None and (job.state, job.error_code) == ("failed", "knowledge.reindex_interrupted")
    assert knowledge.start_reindex().job.state == "running"  # type: ignore[union-attr]
    assert queue.claim("worker-2", (RUN_JOB,)) is None  # run workers never take a reindex


def test_a_worker_that_lost_its_lease_stores_nothing_more(sides: Callable[..., Side]) -> None:
    slow = SlowEmbeddings()
    api_side, worker_side = sides(), sides(slow)
    knowledge = _api(api_side)
    _fill(knowledge)
    knowledge.start_reindex()
    worker = _worker(worker_side, heartbeat=0.05)

    async def scenario() -> None:
        running = asyncio.ensure_future(worker.run_once())
        assert await asyncio.to_thread(slow.entered.wait, 20)
        await asyncio.to_thread(
            worker_side.database._write,
            "UPDATE nodestro_jobs SET status = 'interrupted', finished_at = clock_timestamp()",
            (),
        )
        await asyncio.sleep(0.3)
        slow.release.set()
        await running

    asyncio.run(scenario())
    job = knowledge.index_status().job
    assert job is not None and job.error_code == "knowledge.reindex_interrupted"  # the worker did not overwrite it


def test_a_queue_job_reads_as_a_reindex_job() -> None:
    started = datetime(2026, 11, 6, tzinfo=UTC)

    def job(status: str, **fields: Any) -> Job:
        return Job.model_validate(
            {
                "id": 1,
                "tenant_id": "t",
                "kind": REINDEX_JOB,
                "ref": REINDEX_REF,
                "status": status,
                "payload": {"model": "m", "completed": 2, "total": 5},
                "created_at": started,
                **fields,
            }
        )

    assert reindex_job(job("queued")).state == "running"
    found = reindex_job(job("running", started_at=started))
    assert (found.state, found.model, found.completed, found.total) == ("running", "m", 2, 5)
    failed = reindex_job(job("failed", error="local.unreachable: The model server does not answer"))
    assert (failed.error_code, failed.error) == ("local.unreachable", "The model server does not answer")
    plain = reindex_job(job("failed", error="something broke"))
    assert (plain.error_code, plain.error) == ("knowledge.reindex_failed", "something broke")
    assert reindex_job(job("done", error=None)).error_code is None
    assert reindex_job(job("interrupted")).error_code == "knowledge.reindex_interrupted"
