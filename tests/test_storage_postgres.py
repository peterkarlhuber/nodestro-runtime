"""PostgreSQL storage (ADR-0002): tenant isolation, migrations, and refusals.

The DocumentStore contract itself runs in ``test_storage_contract.py``; these cases need a database too and are
skipped without ``NODESTRO_TEST_POSTGRES`` (they fail instead when ``NODESTRO_REQUIRE_POSTGRES=1``).
"""

from __future__ import annotations

from collections.abc import Iterator
import threading
from typing import Any

import postgres_support
from pydantic import BaseModel
import pytest

from nodestro_runtime.problems import NodestroError

psycopg = pytest.importorskip("psycopg")

from nodestro_runtime.storage_postgres import (  # noqa: E402 - needs the optional driver
    POSTGRES_SCHEMA_VERSION,
    PostgresDatabase,
    PostgresStore,
    postgres_migrations,
)


class Doc(BaseModel):
    id: str
    status: str = ""


@pytest.fixture
def schema() -> Iterator[str]:
    postgres_support.require_postgres()
    name = postgres_support.new_schema()
    yield name
    postgres_support.drop_schema(name)


@pytest.fixture
def database(schema: str) -> Iterator[PostgresDatabase]:
    opened = PostgresDatabase(postgres_support.DSN, schema=schema)
    yield opened
    opened.close()


def _tables(database: PostgresDatabase) -> set[str]:
    rows = database._query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (database.schema,)
    )
    return {str(row[0]) for row in rows}


# ---- tenants ------------------------------------------------------------------------------------


def test_tenants_never_see_each_others_documents(database: PostgresDatabase) -> None:
    database.migrate()
    alpha, beta = database.store("alpha"), database.store("beta")
    alpha.put("docs", "same-id", Doc(id="same-id", status="open"))
    alpha.put_raw("raw", "r", {"secret": "alpha"})
    beta.put("docs", "same-id", Doc(id="same-id", status="beta"))

    assert alpha.get("docs", "same-id", Doc) == Doc(id="same-id", status="open")
    assert beta.get("docs", "same-id", Doc) == Doc(id="same-id", status="beta")
    assert beta.get_raw("raw", "r") is None
    assert [doc.status for doc in beta.list("docs", Doc)] == ["beta"]
    assert beta.find("docs", "status", ["open"], Doc) == []
    assert beta.items("raw", Doc) == []
    assert beta.list_prefix("docs", "same", Doc) == [Doc(id="same-id", status="beta")]
    assert beta.search_raw(["raw"], "alpha") == []

    assert beta.delete("docs", "same-id") is True
    assert beta.delete_prefix("raw", "") == 0
    assert alpha.get("docs", "same-id", Doc) is not None
    assert alpha.get_raw("raw", "r") == {"secret": "alpha"}


def test_a_rolled_back_transaction_of_one_tenant_keeps_the_others(database: PostgresDatabase) -> None:
    database.migrate()
    alpha, beta = database.store("alpha"), database.store("beta")
    beta.put("docs", "kept", Doc(id="kept"))
    with pytest.raises(RuntimeError), alpha.transaction():
        alpha.put("docs", "gone", Doc(id="gone"))
        raise RuntimeError("stop")
    assert alpha.get("docs", "gone", Doc) is None
    assert beta.get("docs", "kept", Doc) == Doc(id="kept")


@pytest.mark.parametrize("tenant", ["", "Upper", "a b", "../x", "a" * 64, "-lead", "x;drop"])
def test_invalid_tenant_ids_are_refused(database: PostgresDatabase, tenant: str) -> None:
    with pytest.raises(ValueError):
        database.store(tenant)


def test_the_table_refuses_an_empty_tenant(database: PostgresDatabase) -> None:
    database.migrate()
    with pytest.raises(psycopg.errors.CheckViolation):
        database._write(
            "INSERT INTO nodestro_documents (tenant_id, collection, id, data) VALUES ('', 'c', 'i', '{}')", ()
        )


@pytest.mark.parametrize("schema_name", ["Public", "a-b", "1abc", 'x"; drop'])
def test_invalid_schema_names_are_refused(schema_name: str) -> None:
    with pytest.raises(ValueError):
        PostgresDatabase(postgres_support.require_postgres(), schema=schema_name)


def test_find_ignores_values_that_are_not_strings(database: PostgresDatabase) -> None:
    database.migrate()
    store = database.store("local")
    store.put_raw("docs", "n", {"id": "n", "status": 1})
    store.put("docs", "s", Doc(id="s", status="1"))
    assert store.find("docs", "status", ["1"], Doc) == [Doc(id="s", status="1")]
    with pytest.raises(ValueError):
        store.find("docs", "status') OR true --", ["1"], Doc)


def test_a_closed_store_refuses_work_and_leaves_the_database_open(database: PostgresDatabase) -> None:
    database.migrate()
    first, second = database.store("first"), database.store("second")
    first.close()
    assert first.closed is True
    with pytest.raises(RuntimeError):
        first.get("docs", "a", Doc)
    second.put("docs", "a", Doc(id="a"))
    assert second.closed is False
    database.close()
    assert second.closed is True


def test_connect_owns_its_database(schema: str) -> None:
    store = PostgresStore.connect(postgres_support.DSN, "local", schema=schema)
    store.database.migrate()
    store.put("docs", "a", Doc(id="a"))
    store.close()
    assert store.database.closed is True


# ---- migrations ---------------------------------------------------------------------------------


def test_a_fresh_database_migrates_without_a_backup(database: PostgresDatabase) -> None:
    assert database.schema_version == 0
    assert database.migrate() == 0
    assert database.schema_version == POSTGRES_SCHEMA_VERSION
    assert {"nodestro_documents", "nodestro_schema_migrations"} <= _tables(database)
    database.require_current_schema()
    rows = database._query("SELECT version, name FROM nodestro_schema_migrations ORDER BY version")
    assert [(int(v), str(n)) for v, n in rows] == [(v, n) for v, (n, _sql) in postgres_migrations().items()]


def test_migrating_again_changes_nothing(database: PostgresDatabase) -> None:
    database.migrate()
    calls: list[Any] = []
    assert database.migrate(before_migration=lambda *a: calls.append(a)) == POSTGRES_SCHEMA_VERSION
    assert calls == []


def test_an_unmigrated_database_is_refused_for_service(database: PostgresDatabase) -> None:
    with pytest.raises(NodestroError) as caught:
        database.require_current_schema()
    assert caught.value.code == "storage.schema_mismatch"


def _steps(*extra: tuple[int, tuple[str, Any]]) -> dict[int, tuple[str, Any]]:
    steps: dict[int, tuple[str, Any]] = dict(postgres_migrations())
    steps.update(extra)
    return steps


def test_a_later_migration_backs_up_first(database: PostgresDatabase) -> None:
    database.migrate()
    database.store("local").put("docs", "a", Doc(id="a"))
    calls: list[tuple[int, int]] = []
    newer = _steps((POSTGRES_SCHEMA_VERSION + 1, ("next_extra.sql", "CREATE TABLE nodestro_extra (id text)")))

    def backup(current: int, target: int) -> None:
        calls.append((current, target))
        assert "nodestro_extra" not in _tables(database)

    assert database.migrate(before_migration=backup, migrations=newer) == POSTGRES_SCHEMA_VERSION
    assert calls == [(POSTGRES_SCHEMA_VERSION, POSTGRES_SCHEMA_VERSION + 1)]
    assert "nodestro_extra" in _tables(database)
    assert database.schema_version == POSTGRES_SCHEMA_VERSION + 1


def test_a_later_migration_without_a_backup_is_refused(database: PostgresDatabase) -> None:
    database.migrate()
    newer = _steps((POSTGRES_SCHEMA_VERSION + 1, ("next_extra.sql", "CREATE TABLE nodestro_extra (id text)")))
    with pytest.raises(NodestroError) as caught:
        database.migrate(migrations=newer)
    assert caught.value.code == "storage.backup_required"
    assert "nodestro_extra" not in _tables(database)


def test_a_failing_backup_changes_nothing(database: PostgresDatabase) -> None:
    database.migrate()
    newer = _steps((POSTGRES_SCHEMA_VERSION + 1, ("next_extra.sql", "CREATE TABLE nodestro_extra (id text)")))

    def backup(_current: int, _target: int) -> None:
        raise OSError("disk full")

    with pytest.raises(NodestroError) as caught:
        database.migrate(before_migration=backup, migrations=newer)
    assert caught.value.code == "storage.backup_failed"
    assert database.schema_version == POSTGRES_SCHEMA_VERSION


def test_a_failing_step_leaves_the_database_unchanged(database: PostgresDatabase) -> None:
    database.migrate()
    database.store("local").put("docs", "a", Doc(id="a"))
    newer = _steps(
        (POSTGRES_SCHEMA_VERSION + 1, ("next_extra.sql", "CREATE TABLE nodestro_extra (id text)")),
        (POSTGRES_SCHEMA_VERSION + 2, ("next_broken.sql", "DELETE FROM nodestro_documents; SELECT 1/0")),
    )
    with pytest.raises(NodestroError) as caught:
        database.migrate(before_migration=lambda *_: None, migrations=newer)
    assert caught.value.code == "storage.migration_failed"
    assert caught.value.params["step"] == POSTGRES_SCHEMA_VERSION + 2
    assert database.schema_version == POSTGRES_SCHEMA_VERSION
    assert "nodestro_extra" not in _tables(database)
    assert database.store("local").get("docs", "a", Doc) == Doc(id="a")
    database.store("local").put("docs", "b", Doc(id="b"))  # the connection is usable again


def test_a_failing_first_migration_creates_nothing(database: PostgresDatabase) -> None:
    broken = {1: ("0001_broken.sql", "CREATE TABLE nodestro_documents (id text); SELECT 1/0")}
    with pytest.raises(NodestroError) as caught:
        database.migrate(migrations=broken)
    assert caught.value.code == "storage.migration_failed"
    assert "nodestro_documents" not in _tables(database)


def test_a_missing_step_is_refused(database: PostgresDatabase) -> None:
    database.migrate()
    gap = _steps((POSTGRES_SCHEMA_VERSION + 2, ("gap.sql", "SELECT 1")))
    with pytest.raises(NodestroError) as caught:
        database.migrate(before_migration=lambda *_: None, migrations=gap)
    assert caught.value.code == "storage.migration_missing"


def test_a_database_of_a_newer_release_is_refused(database: PostgresDatabase) -> None:
    database.migrate()
    database._write(
        "INSERT INTO nodestro_schema_migrations (version, name) VALUES (%s, 'future.sql')",
        (POSTGRES_SCHEMA_VERSION + 5,),
    )
    with pytest.raises(NodestroError) as caught:
        database.migrate()
    assert caught.value.code == "storage.schema_too_new"
    with pytest.raises(NodestroError):
        database.require_current_schema()


def test_migrate_refuses_to_run_inside_a_transaction(database: PostgresDatabase) -> None:
    with pytest.raises(RuntimeError), database.transaction():
        database.migrate()


def test_concurrent_processes_apply_each_step_once(schema: str) -> None:
    databases = [PostgresDatabase(postgres_support.DSN, schema=schema) for _ in range(4)]
    errors: list[BaseException] = []
    start = threading.Barrier(len(databases))

    def run(database: PostgresDatabase) -> None:
        try:
            start.wait(5)
            database.migrate()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(database,)) for database in databases]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    try:
        assert errors == []
        rows = databases[0]._query("SELECT count(*) FROM nodestro_schema_migrations")
        assert int(rows[0][0]) == len(postgres_migrations())
    finally:
        for database in databases:
            database.close()


def test_the_wheel_ships_the_migrations() -> None:
    migrations = postgres_migrations()
    assert list(migrations) == list(range(1, len(migrations) + 1))
    assert all(name.endswith(".sql") and sql.strip() for name, sql in migrations.values())


def test_without_the_driver_the_store_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from nodestro_runtime import storage_postgres

    monkeypatch.setattr(storage_postgres, "psycopg", None)
    with pytest.raises(NodestroError) as caught:
        PostgresDatabase("host=unused")
    assert caught.value.code == "storage.postgres_unavailable"
