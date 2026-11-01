"""Concurrent saves of one workflow never get the same version number (backend audit C-03).

Two connections stand for two server processes. Reading the versions and storing the next one is one transaction;
on PostgreSQL (READ COMMITTED) that alone did not stop two processes from reading the same latest version, so both
stored the same number and one version silently replaced the other. ``DocumentStore.lock`` serializes the step.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import threading
import time
from typing import Any

import postgres_support
import pytest

from nodestro_runtime.schemas import Workflow, WorkflowIn
from nodestro_runtime.storage import DocumentStore, SQLiteStore
from nodestro_runtime.versions import WorkflowVersions

ROUNDS = 6


def _slow_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen the window between reading the latest version and storing the next one (as a busy server would)."""
    original = WorkflowVersions.all

    def all_then_pause(self: WorkflowVersions, workflow_id: str) -> Any:
        found = original(self, workflow_id)
        time.sleep(0.05)
        return found

    monkeypatch.setattr(WorkflowVersions, "all", all_then_pause)


def _race(stores: tuple[DocumentStore, DocumentStore]) -> list[int]:
    workflow = Workflow(**WorkflowIn.model_validate({"name": "Raced", "nodes": [], "edges": []}).model_dump())
    numbers: list[int] = []
    errors: list[BaseException] = []
    for round_ in range(ROUNDS):
        barrier = threading.Barrier(2)

        def save(store: DocumentStore, text: str, barrier: threading.Barrier = barrier) -> None:
            try:
                barrier.wait()
                version = WorkflowVersions(store).record(workflow.model_copy(update={"name": text}))
                assert version is not None
                numbers.append(version.number)
            except BaseException as exc:  # reported below
                errors.append(exc)

        threads = [
            threading.Thread(target=save, args=(store, f"round {round_} by {index}"))
            for index, store in enumerate(stores)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert errors == []
    stored = [version.number for version in WorkflowVersions(stores[0]).all(workflow.id)]
    assert len(stored) == len(numbers), "a save replaced another one's version"
    return numbers


@pytest.fixture
def postgres_pair() -> Iterator[tuple[DocumentStore, DocumentStore]]:
    postgres_support.require_postgres()
    pytest.importorskip("psycopg")
    from nodestro_runtime.storage_postgres import PostgresDatabase

    schema = postgres_support.new_schema()
    first = PostgresDatabase(postgres_support.DSN, schema=schema)
    first.migrate()
    second = PostgresDatabase(postgres_support.DSN, schema=schema)
    yield first.store("acme"), second.store("acme")
    first.close()
    second.close()
    postgres_support.drop_schema(schema)


def test_two_processes_never_get_the_same_version_on_postgresql(
    postgres_pair: tuple[DocumentStore, DocumentStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    _slow_reads(monkeypatch)
    numbers = _race(postgres_pair)
    assert sorted(numbers) == list(range(1, 2 * ROUNDS + 1))


def test_two_connections_never_get_the_same_version_on_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _slow_reads(monkeypatch)
    first, second = SQLiteStore(tmp_path / "nodestro.db"), SQLiteStore(tmp_path / "nodestro.db")
    try:
        numbers = _race((first, second))
    finally:
        first.close()
        second.close()
    assert sorted(numbers) == list(range(1, 2 * ROUNDS + 1))


def test_lock_needs_a_transaction(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nodestro.db")
    try:
        with pytest.raises(RuntimeError):
            store.lock("workflow_versions", "wf")
        with store.transaction():
            store.lock("workflow_versions", "wf")
            store.lock("workflow_versions", "wf")  # again in the same transaction: nothing to wait for
    finally:
        store.close()


def test_lock_needs_a_transaction_on_postgresql(postgres_pair: tuple[DocumentStore, DocumentStore]) -> None:
    store, other = postgres_pair
    with pytest.raises(RuntimeError):
        store.lock("workflow_versions", "wf")
    with store.transaction():
        store.lock("workflow_versions", "wf")
        store.lock("workflow_versions", "wf")
        # Another key, or the same key of another tenant, is not held up.
        with other.transaction():
            other.lock("workflow_versions", "other")
        other_tenant = other.database.store("beta")  # type: ignore[attr-defined]
        with other_tenant.transaction():
            other_tenant.lock("workflow_versions", "wf")


def test_lock_waits_for_the_other_transaction_on_postgresql(postgres_pair: tuple[DocumentStore, DocumentStore]) -> None:
    store, other = postgres_pair
    held, released = threading.Event(), threading.Event()
    waited: list[float] = []

    def holder() -> None:
        with store.transaction():
            store.lock("workflow_versions", "wf")
            held.set()
            released.wait(5)
            time.sleep(0.3)

    thread = threading.Thread(target=holder)
    thread.start()
    held.wait(5)
    released.set()
    began = time.monotonic()
    with other.transaction():
        other.lock("workflow_versions", "wf")
        waited.append(time.monotonic() - began)
    thread.join()
    assert waited[0] >= 0.25
