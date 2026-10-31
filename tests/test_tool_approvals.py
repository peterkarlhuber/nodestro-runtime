"""Stored tool approval requests (S-2a, decision D10): atomic decisions, deadlines, no reuse, tenants.

Every case runs on SQLite and on PostgreSQL. The race cases use separate connections (and, for PostgreSQL, separate
processes): a person deciding in an API process while the worker that holds the run expires the same request.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
import multiprocessing
from pathlib import Path
import threading
from typing import Any

import postgres_support
import pytest

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.storage import DocumentStore, SQLiteStore
from nodestro_runtime.tool_approvals import (
    COLLECTION,
    MAX_WAIT,
    ToolApprovalRequest,
    ToolApprovals,
    arguments_hash,
)
from nodestro_runtime.tool_policy import ToolCapability

WRITE = ToolCapability(access="write", reach="external", source="author")
ARGS = {"to": "team@example.com", "subject": "Report"}


class Opened:
    """A store plus a way to open more connections to the same data (same file, same schema and tenant)."""

    def __init__(self, store: DocumentStore, again: Callable[[], DocumentStore], dsn: str | None, schema: str | None):
        self.store = store
        self.again = again
        self.dsn = dsn
        self.schema = schema


def _sqlite(tmp: Path, _cleanup: list[Callable[[], None]]) -> Opened:
    path = tmp / "documents.db"
    return Opened(SQLiteStore(path), lambda: SQLiteStore(path), None, None)


def _postgres(_tmp: Path, cleanup: list[Callable[[], None]]) -> Opened:
    from nodestro_runtime.storage_postgres import PostgresDatabase, PostgresStore

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    cleanup.append(lambda: postgres_support.drop_schema(schema))
    database = PostgresDatabase(dsn, schema=schema)
    database.migrate()
    database.close()
    return Opened(
        PostgresStore.connect(dsn, "tenant-a", schema=schema),
        lambda: PostgresStore.connect(dsn, "tenant-a", schema=schema),
        dsn,
        schema,
    )


@pytest.fixture(params=["sqlite", "postgres"])
def opened(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Opened]:
    cleanup: list[Callable[[], None]] = []
    try:
        found = (_sqlite if request.param == "sqlite" else _postgres)(tmp_path, cleanup)
        yield found
        found.store.close()
    finally:
        for step in cleanup:
            step()


@pytest.fixture
def postgres(tmp_path: Path) -> Iterator[Opened]:
    """PostgreSQL only (never skipped when ``NODESTRO_REQUIRE_POSTGRES=1``)."""
    cleanup: list[Callable[[], None]] = []
    try:
        found = _postgres(tmp_path, cleanup)
        yield found
        found.store.close()
    finally:
        for step in cleanup:
            step()


class Clock:
    def __init__(self) -> None:
        self.at = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.at


def ask(approvals: ToolApprovals, call_id: str = "call_1") -> ToolApprovalRequest:
    return approvals.request(
        run_id="run_1", node_id="a", call_id=call_id, tool="mailer", arguments=ARGS, capability=WRITE, version=3
    )


def test_a_request_binds_to_tool_version_and_arguments(opened: Opened) -> None:
    approvals = ToolApprovals(opened.store)
    found = ask(approvals)
    assert found.status == "pending" and found.deadline - found.requested_at == timedelta(minutes=15)
    assert found.arguments_hash == arguments_hash("mailer", ARGS, version=3, definition_hash=None)
    assert found.arguments_hash != arguments_hash(
        "mailer", {**ARGS, "to": "x@example.com"}, version=3, definition_hash=None
    )
    assert found.arguments_hash != arguments_hash("mailer", ARGS, version=4, definition_hash=None)
    # The same call id again (some providers repeat ids) is a new question, never the old answer.
    again = ask(approvals)
    assert again.id != found.id and again.key == f"run_1:{again.id}"


def test_a_decision_is_final_and_never_reused(opened: Opened) -> None:
    approvals = ToolApprovals(opened.store)
    found = ask(approvals)
    with pytest.raises(NodestroError) as error:
        approvals.decide("run_1", found.id, approved=True, by="usr_1", arguments="other")
    assert error.value.code == "tool.approval_stale"
    rejected = approvals.decide(
        "run_1", found.id, approved=False, by="usr_1", arguments=found.arguments_hash, comment="Not now"
    )
    assert rejected.status == "rejected" and rejected.decided_by == "usr_1"
    with pytest.raises(NodestroError) as error:
        approvals.decide("run_1", found.id, approved=True, by="usr_2", arguments=found.arguments_hash)
    assert error.value.code == "tool.approval_decided"
    assert approvals.close("run_1", found.id, "expired") is False
    assert approvals.get("run_1", found.id) == rejected
    # another call of the same tool with the same arguments gets its own request
    assert ask(approvals, "call_2").status == "pending"


def test_a_decision_after_the_deadline_is_refused_by_the_store(opened: Opened) -> None:
    clock = Clock()
    approvals = ToolApprovals(opened.store, clock=clock)
    found = ask(approvals)
    # The stored deadline is in the past by the store's own clock, the deciding process's clock is behind.
    opened.store.put(COLLECTION, found.key, found.model_copy(update={"deadline": datetime.now(timezone.utc)}))
    clock.at -= timedelta(minutes=30)
    with pytest.raises(NodestroError) as error:
        approvals.decide("run_1", found.id, approved=True, by="usr_1", arguments=found.arguments_hash)
    assert error.value.code == "tool.approval_expired"
    assert approvals.get("run_1", found.id).status == "expired"  # type: ignore[union-attr]


def test_the_waiter_sees_a_decision_made_on_another_connection(opened: Opened) -> None:
    approvals = ToolApprovals(opened.store, poll_seconds=0.01)
    found = ask(approvals)
    other = opened.again()
    try:
        timer = threading.Timer(
            0.1,
            lambda: ToolApprovals(other).decide(
                "run_1", found.id, approved=True, by="usr_1", arguments=found.arguments_hash
            ),
        )
        timer.start()
        assert approvals.wait("run_1", found.id).status == "approved"
        timer.join()
    finally:
        other.close()


def test_the_waiter_expires_at_the_deadline(opened: Opened) -> None:
    clock = Clock()
    ticks: list[float] = []

    def sleep(seconds: float) -> None:
        ticks.append(seconds)
        clock.at += timedelta(minutes=5)

    approvals = ToolApprovals(opened.store, clock=clock, sleep=sleep)
    found = ask(approvals)
    assert approvals.wait("run_1", found.id).status == "expired"
    assert len(ticks) == 3
    with pytest.raises(NodestroError) as error:
        approvals.decide("run_1", found.id, approved=True, by="usr_1", arguments=found.arguments_hash)
    assert error.value.code == "tool.approval_expired"


def test_cancelling_the_wait_cancels_the_request(opened: Opened) -> None:
    approvals = ToolApprovals(opened.store, poll_seconds=0.01)
    found = ask(approvals)

    class Cancelled(Exception):
        pass

    def check() -> None:
        raise Cancelled

    with pytest.raises(Cancelled):
        approvals.wait("run_1", found.id, check=check)
    assert approvals.get("run_1", found.id).status == "cancelled"  # type: ignore[union-attr]


def test_a_stopped_run_expires_its_pending_requests_only(opened: Opened) -> None:
    approvals = ToolApprovals(opened.store)
    first = ask(approvals, "call_1")
    ask(approvals, "call_2")
    approvals.decide("run_1", first.id, approved=True, by="usr_1", arguments=first.arguments_hash)
    assert approvals.expire_run("run_1") == 1
    assert [item.status for item in approvals.of_run("run_1")] == ["approved", "expired"]
    assert approvals.pending("run_1") == []


def test_the_wait_is_capped() -> None:
    store = SQLiteStore(":memory:")
    with pytest.raises(ValueError):
        ToolApprovals(store, wait=MAX_WAIT + timedelta(seconds=1))
    with pytest.raises(ValueError):
        ToolApprovals(store, wait=timedelta(0))


def test_decide_and_expire_race_in_threads(opened: Opened) -> None:
    approvals = ToolApprovals(opened.store)
    found = ask(approvals)
    stores = [opened.again() for _ in range(6)]
    start = threading.Barrier(len(stores))
    outcomes: list[str] = []
    lock = threading.Lock()

    def contend(index: int, store: DocumentStore) -> None:
        mine = ToolApprovals(store)
        start.wait()
        try:
            if index % 3 == 0:
                result = "expired" if mine.close("run_1", found.id, "expired") else "lost"
            else:
                mine.decide(
                    "run_1", found.id, approved=index % 3 == 1, by=f"usr_{index}", arguments=found.arguments_hash
                )
                result = "decided"
        except NodestroError as error:
            result = error.code
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=contend, args=(index, store)) for index, store in enumerate(stores)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for store in stores:
        store.close()
    winners = [item for item in outcomes if item in ("expired", "decided")]
    assert len(winners) == 1, outcomes
    assert all(
        item in ("lost", "tool.approval_decided", "tool.approval_expired") for item in outcomes if item not in winners
    )
    final = approvals.get("run_1", found.id)
    assert final is not None and final.status != "pending"


# ---- PostgreSQL: separate processes --------------------------------------------------------------------------


def _decide_in_process(
    dsn: str, schema: str, index: int, request_id: str, digest: str, start: Any, results: Any
) -> None:
    from nodestro_runtime.storage_postgres import PostgresStore

    store = PostgresStore.connect(dsn, "tenant-a", schema=schema)
    try:
        approvals = ToolApprovals(store)
        start.wait()
        try:
            if index == 0:
                results.put((index, "expired" if approvals.close("run_1", request_id, "expired") else "lost"))
                return
            approvals.decide("run_1", request_id, approved=index % 2 == 1, by=f"usr_{index}", arguments=digest)
            results.put((index, "decided"))
        except NodestroError as error:
            results.put((index, error.code))
    finally:
        store.close()


def test_one_decision_wins_across_processes_and_a_run_save_does_not_touch_it(postgres: Opened) -> None:
    opened = postgres
    assert opened.dsn is not None and opened.schema is not None
    approvals = ToolApprovals(opened.store)
    found = ask(approvals)
    context = multiprocessing.get_context("spawn")
    start = context.Barrier(4)
    results = context.Queue()
    processes = [
        context.Process(
            target=_decide_in_process,
            args=(opened.dsn, opened.schema, index, found.id, found.arguments_hash, start, results),
        )
        for index in range(4)
    ]
    for process in processes:
        process.start()
    # Meanwhile the worker saves the whole run document over and over (as the engine does).
    for n in range(50):
        opened.store.put_raw("runs", "run_1", {"id": "run_1", "status": "running", "n": n})
    for process in processes:
        process.join(60)
        assert process.exitcode == 0
    outcomes = [results.get(timeout=5) for _ in processes]
    winners = [item for item in outcomes if item[1] in ("decided", "expired")]
    assert len(winners) == 1, outcomes
    final = approvals.get("run_1", found.id)
    assert final is not None and final.status in ("approved", "rejected", "expired")
    if winners[0][1] == "decided":
        assert final.decided_by == f"usr_{winners[0][0]}"


def test_tenants_never_see_each_others_requests(postgres: Opened) -> None:
    opened = postgres
    assert opened.dsn is not None and opened.schema is not None
    from nodestro_runtime.storage_postgres import PostgresStore

    found = ask(ToolApprovals(opened.store))
    other = PostgresStore.connect(opened.dsn, "tenant-b", schema=opened.schema)
    try:
        theirs = ToolApprovals(other)
        assert theirs.get("run_1", found.id) is None
        with pytest.raises(NodestroError) as error:
            theirs.decide("run_1", found.id, approved=True, by="usr_b", arguments=found.arguments_hash)
        assert error.value.code == "tool.approval_not_found"
        assert theirs.expire_run("run_1") == 0
    finally:
        other.close()
    assert ToolApprovals(opened.store).get("run_1", found.id).status == "pending"  # type: ignore[union-attr]
