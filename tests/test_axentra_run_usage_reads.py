"""The worker's periodic run usage settlement reads only what it settles (``NODESTRO_AXENTRA_RUN_USAGE=1``).

Before, every pass (every 30 s on the scheduler leader) read the mirrored events of **every** run of every tenant
(``idempotency_key LIKE 'nodestro:run:%'``) to find what was missing, even when no run had finished since the last
pass: the pass grew with all runs ever settled (measured with ``nodestro-api/tests/bench_mirrors.py``). Now a pass
reads the candidate runs first (changed since the last pass, through the ``updated_at`` index) and the mirrored
events of those runs only, by key; a tenant without candidates costs one indexed read. ``full=True``
(``axentra reconcile``) still reads everything at once.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import postgres_support
import pytest

pytest.importorskip("psycopg")
pytest.importorskip("axentra", reason="Axentra needs Python 3.11+")

from nodestro_runtime import axentra_platform
from nodestro_runtime.schemas import now


@pytest.fixture
def events(identity_db: Any) -> Iterator[Any]:
    from identity_support import axentra_services, create_tenants

    create_tenants(identity_db, [("alpha", "Alpha"), ("beta", "Beta")])
    services = axentra_services(identity_db)
    found = axentra_platform.AxentraEvents(
        axentra_platform.AxentraLink(services, identity_db), audit=False, usage=True, run_usage=True
    )
    found.ensure_meters()
    try:
        yield found
    finally:
        services.close()


@pytest.fixture
def identity_db() -> Iterator[Any]:
    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    opened.migrate()
    opened.test_schema = schema  # type: ignore[attr-defined]
    yield opened
    opened.close()
    postgres_support.drop_schema(schema)


def _run(database: Any, tenant: str, run_id: str, requests: int = 1) -> None:
    database.store(tenant).put_raw(
        "runs",
        run_id,
        {
            "id": run_id,
            "workflow_id": "wf_1",
            "status": "succeeded",
            "trigger": "manual",
            "finished_at": now().isoformat(),
            "usage": {
                "requests": requests,
                "input_tokens": 100,
                "output_tokens": 10,
                "estimated_cost_usd": 0.01,
                "cost_complete": True,
            },
        },
    )


def _usage_reads(call: Any) -> list[str]:
    """The statements on ``axentra_usage_events`` that ``call`` sends through Axentra's transactions."""
    from axentra.data import database as axdb

    seen: list[str] = []
    every = axdb.Tx.all

    def record(self: Any, sql: str, params: Any = None) -> Any:
        if "FROM axentra_usage_events" in sql:
            seen.append(sql)
        return every(self, sql, params)

    axdb.Tx.all = record  # type: ignore[method-assign]
    try:
        call()
    finally:
        axdb.Tx.all = every  # type: ignore[method-assign]
    return seen


def _totals(database: Any) -> dict[str, Decimal]:
    rows = postgres_support.owner_of(database)._query(
        "SELECT m.key, sum(e.quantity) FROM axentra_usage_events e JOIN axentra_usage_meters m ON m.id = e.meter_id"
        " WHERE e.idempotency_key LIKE 'nodestro:run:%%' GROUP BY m.key"
    )
    return {str(key): Decimal(value) for key, value in rows}


def test_a_periodic_pass_reads_the_mirrored_events_of_its_candidate_runs_only(events: Any) -> None:
    database = events.link.database
    for index in range(5):
        _run(database, "alpha", f"run_{index}")
    first = events.settle_runs()
    assert (first["missing"], first["written"]) == (5, 5)
    # Nothing finished since: no tenant reads Axentra's usage events at all.
    reads = _usage_reads(lambda: events.settle_runs(since=now()))
    assert reads == []
    # One new run of alpha: one read by key for alpha, none for beta, nothing of the other runs.
    _run(database, "alpha", "run_new", requests=3)
    report: dict[str, Any] = {}
    reads = _usage_reads(lambda: report.update(events.settle_runs()))
    assert report["written"] == 1 and report["missing"] == 1
    assert len(reads) == 1 and "idempotency_key = ANY" in reads[0] and "LIKE" not in reads[0]
    # Settled exactly once each; a replayed pass (every run a candidate again) writes nothing.
    again = events.settle_runs(since=now().replace(year=2000))
    assert (again["runs"], again["missing"], again["written"]) == (6, 0, 0)
    assert _totals(database)["nodestro.ai.requests"] == 8
    # The reconciliation (full) still reads every run's events in one statement.
    reads = _usage_reads(lambda: events.settle_runs(full=True))
    assert len(reads) == 1 and "LIKE" in reads[0]
