"""The activity log on PostgreSQL (N-13): a failed activity write never aborts the caller's transaction.

An error inside a PostgreSQL transaction aborts all of it; the event is therefore written in a savepoint
(:meth:`~nodestro_runtime.storage_postgres.PostgresDatabase.savepoint`), and a failure rolls back only the event.
Needs ``NODESTRO_TEST_POSTGRES``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import postgres_support
from pydantic import BaseModel
import pytest

pytest.importorskip("psycopg")

from nodestro_runtime import activity
from nodestro_runtime.storage_postgres import PostgresDatabase


class Note(BaseModel):
    text: str


@pytest.fixture
def database() -> Iterator[PostgresDatabase]:
    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    try:
        opened.migrate()
        yield opened
    finally:
        opened.close()
        postgres_support.drop_schema(schema)


def _failing_activity_writes(database: PostgresDatabase, store: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make every activity write fail with a real SQL error on the transaction's connection."""
    real_put = store.put
    failed: list[str] = []

    def put(collection: str, key: str, model: BaseModel) -> None:
        if collection == activity.COLLECTION:
            failed.append(key)
            database._local.connection.execute("SELECT 1 / 0")  # division by zero: aborts what it runs in
        real_put(collection, key, model)

    monkeypatch.setattr(store, "put", put)
    return failed


def test_a_failed_activity_write_keeps_the_callers_transaction(
    database: PostgresDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = database.store("alpha")
    failed = _failing_activity_writes(database, store, monkeypatch)
    with store.transaction():
        store.put("notes", "before", Note(text="before"))
        assert activity.add(store, "workflow", "workflow.started", actor="usr_1") is None
        store.put("notes", "after", Note(text="after"))  # the transaction is still usable
    assert failed  # the write was attempted and failed
    assert {key for key, _ in store.items("notes", Note)} == {"after", "before"}
    assert activity.query(store) == []


def test_a_failed_activity_write_of_an_approval_audit_keeps_the_audit_event(
    database: PostgresDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nodestro_runtime.tool_approvals import AUDIT, ToolApprovalEvent, record_audit

    store = database.store("alpha")
    _failing_activity_writes(database, store, monkeypatch)
    with store.transaction():
        event = record_audit(store, ToolApprovalEvent(kind="decided", actor="usr_1", tool="calc", run_id="run_1"))
        store.put("notes", "after", Note(text="after"))
    assert [item.id for _, item in store.items(AUDIT, ToolApprovalEvent)] == [event.id]
    assert [key for key, _ in store.items("notes", Note)] == ["after"]


def test_activity_events_are_written_on_postgres(database: PostgresDatabase) -> None:
    store = database.store("alpha")
    with store.transaction():
        written = activity.add(store, "file", "file.uploaded", resource_type="input_file", resource_id="in_1")
    outside = activity.add(store, "file", "file.deleted", resource_type="input_file", resource_id="in_1")
    assert written is not None and outside is not None
    assert [event.id for event in activity.query(store)] == [outside.id, written.id]
