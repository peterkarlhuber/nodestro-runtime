"""Indexes of the list and find queries (backend audit C-05, C-14).

SQLite: schema version 1 adds an index for ``list`` (newest first) and partial expression indexes for the ``find``
fields on hot paths, through the migration framework (backup first, forward only). PostgreSQL: ``find`` names the
field as a literal so that the expression indexes (``status`` and ``trigger_ref``, partial since 0006) apply.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import sqlite3

import postgres_support
from pydantic import BaseModel
import pytest

from nodestro_runtime import storage
from nodestro_runtime.storage import INDEXED_FIELDS, SCHEMA_VERSION, SQLiteStore
from nodestro_runtime.workspace import Workspace


class Doc(BaseModel):
    id: str
    status: str = "done"
    trigger_ref: str | None = None


def _version_0(path: Path) -> None:
    """A data directory as earlier releases left it: schema version 0, documents, no indexes."""
    old = SQLiteStore(path)
    for index in range(50):
        old.put("runs", f"r{index}", Doc(id=f"r{index}", trigger_ref="pub_1" if index % 10 == 0 else None))
    old.put_raw("broken", "x", {"a": 1})
    old.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE nodestro_documents SET data = '{not json' WHERE collection = 'broken'")
        connection.commit()
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        connection.close()


def _plan(store: SQLiteStore, query: str, params: tuple[object, ...]) -> str:
    with store.maintenance() as connection:
        return " | ".join(str(row[-1]) for row in connection.execute("EXPLAIN QUERY PLAN " + query, params))


def test_the_migration_adds_the_indexes_after_a_backup(tmp_path: Path) -> None:
    path = tmp_path / "nodestro.db"
    _version_0(path)
    store = SQLiteStore(path)
    calls: list[tuple[int, int]] = []
    try:
        assert store.migrate(before_migration=lambda *versions: calls.append(versions)) == 0
        assert calls == [(0, SCHEMA_VERSION)] and SCHEMA_VERSION == 1
        assert store.schema_version == 1
        with store.maintenance() as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"nodestro_documents_updated", *(f"nodestro_documents_{field}" for field in INDEXED_FIELDS)} <= names
        # The queries use them: no sort of the whole collection, no scan for a field value.
        listing = _plan(
            store,
            "SELECT data FROM nodestro_documents WHERE collection = ? ORDER BY updated_at DESC, id LIMIT 20",
            ("runs",),
        )
        assert "nodestro_documents_updated" in listing and "TEMP B-TREE" not in listing
        found = _plan(
            store,
            "SELECT data FROM nodestro_documents WHERE collection = ? AND json_valid(data)"
            " AND json_extract(data, '$.trigger_ref') IN (?) ORDER BY +updated_at, id",
            ("runs", "pub_1"),
        )
        assert "nodestro_documents_trigger_ref" in found
        # The results are unchanged, and a damaged document stops neither a write nor a find.
        with store.maintenance() as connection:
            unindexed = [
                row[0]
                for row in connection.execute(
                    "SELECT json_extract(data, '$.id') FROM nodestro_documents NOT INDEXED WHERE collection = 'runs'"
                    " ORDER BY updated_at DESC, id LIMIT 5"
                )
            ]
        assert [doc.id for doc in store.list("runs", Doc, limit=5)] == unindexed
        assert sorted(doc.id for doc in store.find("runs", "trigger_ref", ["pub_1"], Doc)) == [
            f"r{index}" for index in range(0, 50, 10)
        ]
        assert len(store.find("runs", "status", ["done"], Doc)) == 50
        store.put_raw("broken", "y", {"status": "done"})
        with store.maintenance() as connection:
            connection.execute("UPDATE nodestro_documents SET data = '{not json' WHERE collection = 'broken'")
        # Opening again: nothing to do.
        assert store.migrate(before_migration=None) == 1
    finally:
        store.close()


def test_a_new_database_needs_no_backup(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nodestro.db")
    try:
        assert store.migrate(before_migration=None) == 0
        assert store.schema_version == SCHEMA_VERSION
    finally:
        store.close()


def test_an_old_data_directory_is_backed_up_and_migrated_when_opened(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _version_0(data / "nodestro.db")
    workspace = Workspace(data, integrations=[])
    try:
        assert workspace.sqlite_store.schema_version == SCHEMA_VERSION
    finally:
        workspace.close()
    backups = list((data / "backups").glob("pre-migration-0-to-1-*.db"))
    assert len(backups) == 1
    copy = sqlite3.connect(backups[0])
    try:
        assert copy.execute("PRAGMA user_version").fetchone()[0] == 0
        assert copy.execute("SELECT count(*) FROM nodestro_documents WHERE collection = 'runs'").fetchone()[0] == 50
    finally:
        copy.close()


def test_a_new_data_directory_writes_no_backup(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fresh", integrations=[])
    try:
        assert workspace.sqlite_store.schema_version == SCHEMA_VERSION
    finally:
        workspace.close()
    assert not (tmp_path / "fresh" / "backups").exists()


def test_the_release_ships_exactly_this_migration() -> None:
    assert sorted(storage.MIGRATIONS) == [1]


# ---- PostgreSQL ---------------------------------------------------------------------------------------------


@pytest.fixture
def postgres() -> Iterator[object]:
    postgres_support.require_postgres()
    pytest.importorskip("psycopg")
    from nodestro_runtime.storage_postgres import PostgresDatabase

    schema = postgres_support.new_schema()
    database = PostgresDatabase(postgres_support.DSN, schema=schema)
    database.migrate()
    yield database
    database.close()
    postgres_support.drop_schema(schema)


def test_find_uses_the_expression_indexes_on_postgresql(postgres: object) -> None:
    from nodestro_runtime.storage_postgres import PostgresDatabase

    assert isinstance(postgres, PostgresDatabase)
    store = postgres.store("acme")
    rows = [
        ("acme", "runs", f"r{index}", Doc(id=f"r{index}", trigger_ref=f"pub_{index % 500}").model_dump_json())
        for index in range(3000)
    ]
    with postgres._transaction() as db, db.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO nodestro_documents (tenant_id, collection, id, data) VALUES (%s, %s, %s, %s::jsonb)", rows
        )
    with postgres._transaction() as db:
        db.execute("ANALYZE nodestro_documents")
    captured: list[str] = []
    original = postgres._query

    def explain(query: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        if query.startswith("SELECT") and "data ->> " in query:
            captured.append("\n".join(str(row[0]) for row in original("EXPLAIN " + query, params)))
        return original(query, params)

    postgres._query = explain  # type: ignore[method-assign]
    try:
        found = store.find("runs", "trigger_ref", ["pub_1"], Doc)
        running = store.find("runs", "status", ["queued", "running"], Doc)
    finally:
        del postgres._query
    assert sorted(doc.id for doc in found) == sorted(f"r{index}" for index in range(1, 3000, 500))
    assert running == []
    assert "nodestro_documents_trigger_ref" in captured[0]
    assert "nodestro_documents_status" in captured[1]
