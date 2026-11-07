"""The partition guard and the table prefix of Orkexa's PostgreSQL backends as Nodestro uses them (MUP-ONA B4:
T-GUARD-1, T-GUARD-2; B6: the same cases against ``orkexa.backends.postgres`` at the pinned commit, with Nodestro's
layout ``nodestro_``).

* **T-GUARD-1** the executor (``orkexa.backends.postgres._sql.Executor``) refuses a partitioned statement whose
  ``tenant_id`` is missing, ``None``, empty, whitespace, not a string, 10,000 characters long, holds U+0000 or is
  another tenant with ``RetrievalError("partition.missing")`` -- for every partitioned catalog entry, and through the
  index, file store and memory -- and a spy source proves that no transaction was opened (no database access).
  Nodestro's own classes (:mod:`nodestro_runtime.postgres_tables`) refuse such a tenant when they are built.
* **T-GUARD-2** a table prefix must match ``^[a-z][a-z0-9_]{0,30}_$``; anything else is refused before a statement is
  rendered or a store is built.

The cases on a real server (``NODESTRO_TEST_POSTGRES``) check Nodestro's ``ConnectionSource`` adapter
(:class:`nodestro_runtime.storage_postgres.DatabaseSource`): its modes, and the transaction-local tenant setting
(the hook for row-level security, MUP-ONA A9) when it is switched on.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from orkexa.backends.postgres import (
    CATALOG,
    ConnectionSource,
    PostgresFileStore,
    PostgresIndex,
    PostgresLayout,
    PostgresMemory,
    render,
)
from orkexa.backends.postgres._sql import MAX_TENANT_CHARS, Executor, check_prefix, jsonb_text
from orkexa.retrieval import RetrievalError
import postgres_support
import pytest

from nodestro_runtime import postgres_tables
from nodestro_runtime.postgres_tables import LAYOUT

TENANT = "tenant-a"
PARTITIONED = sorted(name for name, entry in CATALOG.items() if entry.partitioned)


class SpySource:
    """A ``ConnectionSource`` that records every transaction and fails the test if a statement runs."""

    def __init__(self) -> None:
        self.opened: list[tuple[str, str | None]] = []

    @contextmanager
    def transaction(self, mode: str = "transaction", *, tenant_id: str | None = None) -> Iterator[Any]:
        self.opened.append((mode, tenant_id))
        yield self

    def execute(self, query: Any, params: Any = None) -> Any:
        raise AssertionError(f"a statement reached the database: {query!r}")


def _params(name: str, **override: Any) -> dict[str, Any]:
    params: dict[str, Any] = dict.fromkeys(CATALOG[name].params, "x")
    params["tenant_id"] = TENANT
    params.update(override)
    return params


#: Tenant values the guard refuses (T-GUARD-1).
REFUSED: dict[str, Any] = {
    "empty": "",
    "none": None,
    "whitespace": " \t\n",
    "10000 characters": "a" * 10_000,
    "129 characters": "a" * (MAX_TENANT_CHARS + 1),
    "foreign tenant": "tenant-b",
    "padded own tenant": f" {TENANT}",
    "number": 1,
    "list": [TENANT],
    # PostgreSQL text cannot hold U+0000 (orkexa B5 refuses it in the guard; it used to reach the server as an error).
    "NUL": f"{TENANT}\x00",
}


# MUP-ONA T-GUARD-1
@pytest.mark.parametrize("value", list(REFUSED.values()), ids=list(REFUSED))
@pytest.mark.parametrize("name", PARTITIONED)
def test_the_guard_refuses_an_unbound_tenant_before_any_database_access(name: str, value: Any) -> None:
    spy = SpySource()
    executor = Executor(spy, layout=LAYOUT, tenant_id=TENANT)
    with pytest.raises(RetrievalError) as raised:
        executor.execute(name, _params(name, tenant_id=value))
    assert raised.value.code == "partition.missing"
    assert spy.opened == []


@pytest.mark.parametrize("name", PARTITIONED)
def test_the_guard_refuses_a_missing_tenant(name: str) -> None:
    spy = SpySource()
    params = _params(name)
    del params["tenant_id"]
    with pytest.raises(RetrievalError) as raised:
        Executor(spy, layout=LAYOUT, tenant_id=TENANT).execute(name, params)
    assert raised.value.code == "partition.missing"
    assert spy.opened == []


def test_the_guard_also_holds_inside_a_transaction() -> None:
    spy = SpySource()
    executor = Executor(spy, layout=LAYOUT, tenant_id=TENANT)
    with executor.transaction(), pytest.raises(RetrievalError):
        executor.execute("chunks_count", _params("chunks_count", tenant_id="tenant-b"))
    assert spy.opened == [("transaction", TENANT)]  # the block's own transaction; the statement never ran


def test_the_instance_tenant_must_pass_the_guard_too() -> None:
    for value in REFUSED.values():
        if value in ("tenant-b", f" {TENANT}"):
            continue  # valid tenants of their own
        with pytest.raises(ValueError):
            Executor(SpySource(), layout=LAYOUT, tenant_id=value)


def test_only_catalog_names_with_their_parameters_run() -> None:
    spy = SpySource()
    executor = Executor(spy, layout=LAYOUT, tenant_id=TENANT)
    with pytest.raises(ValueError, match="Unknown statement"):
        executor.execute("SELECT * FROM nodestro_chunks", {"tenant_id": TENANT})
    with pytest.raises(ValueError, match="parameters"):
        executor.execute("files_entry", _params("files_entry", extra=1))
    params = _params("files_entry")
    del params["id"]
    with pytest.raises(ValueError, match="parameters"):
        executor.execute("files_entry", params)
    with pytest.raises(ValueError, match="Unknown statement"):
        render("nope", "nodestro_")
    assert spy.opened == []


def _stores(source: Any) -> list[Any]:
    return [
        PostgresIndex(source, TENANT, layout=LAYOUT, namespace="n"),
        PostgresFileStore(source, TENANT, layout=LAYOUT, namespace="n"),
        PostgresMemory(source, TENANT, layout=LAYOUT, namespace="n"),
    ]


@pytest.mark.parametrize("value", ["tenant-b", "", None, " ", "a" * 10_000, f"{TENANT}\x00"])
def test_a_store_whose_tenant_was_changed_reaches_no_row(value: Any) -> None:
    """The stores bind ``self.tenant_id``; the executor keeps the tenant they were built for. Changing the attribute
    afterwards (a bug, or an attack on the object) is caught by the guard, not sent to the database."""
    spy = SpySource()
    index, files, memory = _stores(spy)
    calls = [
        lambda: len(index),
        lambda: index.sources(),
        lambda: index.delete("s"),
        lambda: index.retrieve("alpha", mode="fts"),
        lambda: files._entries(),
        lambda: files._remove("f"),
        lambda: memory._read("/memories/a"),
        lambda: memory.clear(),
    ]
    for store in (index, files, memory):
        store.tenant_id = value
    for call in calls:
        with pytest.raises(RetrievalError) as raised:
            call()
        assert raised.value.code == "partition.missing"
    assert spy.opened == []


class _NoDatabase:
    """Stands in for a ``PostgresDatabase``: any use fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the database was used: {name}")


@pytest.mark.parametrize(
    "value", ["", " ", "Tenant-A", "a" * 64, f"{TENANT}\x00", "\x00", "tenant a", "tenant'a"], ids=repr
)
def test_nodestro_refuses_a_bad_tenant_before_it_builds_a_store(value: str) -> None:
    """Nodestro's rule for tenant ids (``storage_postgres.TENANT_ID``) is stricter than Orkexa's guard; it refuses a
    tenant id with U+0000 (the case orkexa B5 added to its guard) and every other bad value before Orkexa's class or
    the database is reached."""
    database: Any = _NoDatabase()
    for build in (
        lambda: postgres_tables.PostgresIndex(database, value),
        lambda: postgres_tables.PostgresFileStore(database, value),
        lambda: postgres_tables.PostgresMemory(database, value),
        lambda: postgres_tables.PostgresMemoryTables(database, value),
        lambda: postgres_tables.memory_totals(database, value, "nodestro:"),
    ):
        with pytest.raises(ValueError, match="Invalid tenant id"):
            build()


# ---- T-GUARD-2: the table prefix ------------------------------------------------------------------------------------

BAD_PREFIXES = {
    "injection": "x; DROP",
    "upper case": "A_",
    "no underscore": "a",
    "non-ASCII": "ä_",
    "double quote": 'a"_',
    "single quote": "a'_",
    "33 characters": "a" * 32 + "_",
    "empty": "",
    "starts with a digit": "1a_",
    "starts with underscore": "_a_",
    "space": "a b_",
    "trailing newline": "a_\n",
    "not a string": None,
}


# MUP-ONA T-GUARD-2
@pytest.mark.parametrize("prefix", list(BAD_PREFIXES.values()), ids=list(BAD_PREFIXES))
def test_a_bad_prefix_is_refused(prefix: Any) -> None:
    spy = SpySource()
    with pytest.raises(ValueError, match="prefix"):
        check_prefix(prefix)
    with pytest.raises(ValueError, match="prefix"):
        render("chunks_count", prefix)
    with pytest.raises(ValueError, match="prefix"):
        PostgresLayout(prefix)
    for kind in (Executor, PostgresIndex, PostgresFileStore, PostgresMemory):
        with pytest.raises(TypeError, match="PostgresLayout"):
            kind(spy, tenant_id=TENANT, layout=prefix)  # a prefix string is not a layout
    assert spy.opened == []


@pytest.mark.parametrize("prefix", ["nodestro_", "t_", "a_", "a1_b_", "a" * 31 + "_"])
def test_a_good_prefix_names_the_tables(prefix: str) -> None:
    # Rendering uses psycopg.sql (the postgres extra); the desktop profile runs this suite without it.
    pytest.importorskip("psycopg")
    assert PostgresLayout(prefix).table("chunks") == f"{prefix}chunks"
    text = render("vectors_store", prefix).as_string(None)
    assert f'"{prefix}chunk_vectors"' in text and f'"{prefix}chunks"' in text


def test_jsonb_text_is_the_document_stores_encoding() -> None:
    """Orkexa's backends keep their own copy of ``storage_postgres._encode`` (taken over from Nodestro in B5)."""
    pytest.importorskip("psycopg")
    from nodestro_runtime.storage_postgres import _encode

    for text in ['{"a": 1}', '{"a": "x\\u0000y", "\\u0000k": ["\\u0000"]}', '"plain"', "[]"]:
        assert jsonb_text(text) == _encode(text)[0]


# ---- Nodestro's adapter on a real server ---------------------------------------------------------------------------


@pytest.fixture
def database() -> Iterator[Any]:
    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    try:
        opened.migrate()
        yield opened
    finally:
        opened.close()
        postgres_support.drop_schema(schema)


def test_the_adapter_runs_each_mode_as_the_database_did(database: Any) -> None:
    from nodestro_runtime.storage_postgres import DatabaseSource

    source = DatabaseSource(database)
    assert isinstance(source, ConnectionSource)
    assert source.lock is database._lock
    executor = Executor(source, layout=LAYOUT, tenant_id=TENANT)
    key = {"tenant_id": TENANT, "namespace": "n"}
    assert executor.execute("memory_write", {**key, "path": "a", "content": "x"}).rowcount == 1
    with executor.transaction():
        assert database.in_transaction
        executor.execute("memory_write", {**key, "path": "b", "content": "y"})
        assert executor.execute("memory_files", {**key, "start": ""}).rows == [("a", 1), ("b", 1)]
    with pytest.raises(RuntimeError), executor.transaction():
        executor.execute("memory_clear", key)
        raise RuntimeError("roll back")
    assert len(executor.execute("memory_files", {**key, "start": ""}).rows) == 2
    with database.transaction():  # a transaction of the database: the executor joins it
        executor.execute("memory_clear", key)
        assert executor.execute("memory_files", {**key, "start": ""}).rows == []
    with pytest.raises(ValueError), source.transaction("bulk"):  # type: ignore[arg-type]
        pass


def test_the_tenant_setting_is_off_by_default_and_transaction_local(database: Any) -> None:
    from nodestro_runtime.storage_postgres import DatabaseSource

    probe = "SELECT current_setting('app.tenant_id', true)"
    with DatabaseSource(database).transaction("read", tenant_id=TENANT) as connection:
        assert connection.execute(probe).fetchall() in ([(None,)], [("",)])
    hooked = DatabaseSource(database, tenant_setting="app.tenant_id")
    for mode in ("read", "write", "transaction"):
        with hooked.transaction(mode, tenant_id=TENANT) as connection:  # type: ignore[arg-type]
            assert connection.execute(probe).fetchall() == [(TENANT,)]
    with DatabaseSource(database).transaction("transaction") as connection:
        assert connection.execute(probe).fetchall() in ([(None,)], [("",)])
    with pytest.raises(ValueError):
        DatabaseSource(database, tenant_setting="x'; DROP")
