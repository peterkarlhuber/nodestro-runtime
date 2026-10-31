"""``replace_if`` on PostgreSQL across separate database connections and processes (S-2a, decision D10).

Approval decisions must never be lost or overwritten by a racing writer: every contender uses its own connection
(the situation of an API process deciding while a worker process expires the same request), and exactly one wins.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import multiprocessing
from typing import Any

import postgres_support
from pydantic import BaseModel
import pytest

from nodestro_runtime.storage_postgres import PostgresDatabase, PostgresStore


class Request(BaseModel):
    id: str
    status: str = "pending"
    deadline: str | None = None
    by: str = ""


@pytest.fixture
def schema() -> Iterator[str]:
    postgres_support.require_postgres()
    name = postgres_support.new_schema()
    database = PostgresDatabase(postgres_support.DSN, schema=name)
    database.migrate()
    database.close()
    yield name
    postgres_support.drop_schema(name)


def _contend(dsn: str, schema: str, tenant: str, who: str, status: str, start: Any, results: Any) -> None:
    store = PostgresStore.connect(dsn, tenant, schema=schema)
    try:
        start.wait()
        won = store.replace_if(
            "tool_approvals",
            "run_1:call_1",
            Request(id="run_1:call_1", status=status, by=who),
            field="status",
            expected="pending",
            live_until="deadline" if status != "expired" else None,
        )
        results.put((who, won))
    finally:
        store.close()


def test_one_decision_wins_across_processes(schema: str) -> None:
    store = PostgresStore.connect(postgres_support.DSN, "tenant-a", schema=schema)
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    store.put("tool_approvals", "run_1:call_1", Request(id="run_1:call_1", deadline=deadline))
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    contenders = [("api-1", "approved"), ("api-2", "rejected"), ("worker", "expired"), ("api-3", "approved")]
    processes = [
        context.Process(target=_contend, args=(postgres_support.DSN, schema, "tenant-a", who, status, start, results))
        for who, status in contenders
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0
    outcome = dict(results.get(timeout=5) for _ in contenders)
    winners = [who for who, won in outcome.items() if won]
    assert len(winners) == 1
    stored = store.get("tool_approvals", "run_1:call_1", Request)
    assert stored is not None and stored.by == winners[0]
    store.close()


def test_a_decision_after_the_deadline_is_refused_by_the_database_clock(schema: str) -> None:
    store = PostgresStore.connect(postgres_support.DSN, "tenant-a", schema=schema)
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store.put("tool_approvals", "r", Request(id="r", deadline=past))
    other = PostgresStore.connect(postgres_support.DSN, "tenant-a", schema=schema)
    late = Request(id="r", status="approved", by="late")
    assert not other.replace_if("tool_approvals", "r", late, field="status", expected="pending", live_until="deadline")
    assert store.get("tool_approvals", "r", Request) == Request(id="r", deadline=past)
    store.close()
    other.close()


def test_tenants_cannot_decide_each_others_requests(schema: str) -> None:
    owner = PostgresStore.connect(postgres_support.DSN, "tenant-a", schema=schema)
    stranger = PostgresStore.connect(postgres_support.DSN, "tenant-b", schema=schema)
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    owner.put("tool_approvals", "r", Request(id="r", deadline=deadline))
    forged = Request(id="r", status="approved", by="intruder")
    assert not stranger.replace_if("tool_approvals", "r", forged, field="status", expected="pending")
    assert stranger.get("tool_approvals", "r", Request) is None
    stored = owner.get("tool_approvals", "r", Request)
    assert stored is not None and stored.status == "pending"
    owner.close()
    stranger.close()
