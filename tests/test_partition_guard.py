"""The partition guard and the table prefix of the generic PostgreSQL part (MUP-ONA B4: T-GUARD-1, T-GUARD-2).

* **T-GUARD-1** the executor (:class:`nodestro_runtime.pg_generic._sql.Executor`) refuses a partitioned statement whose
  ``tenant_id`` is missing, ``None``, empty, whitespace, not a string, 10,000 characters long or another tenant with
  ``RetrievalError("partition.missing")`` -- for every partitioned catalog entry, and through the index, file store and
  memory -- and a spy source proves that no transaction was opened (no database access).
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

from orkexa.retrieval import RetrievalError
import postgres_support
import pytest

from nodestro_runtime.pg_generic import PostgresFileStore, PostgresIndex, PostgresMemory
from nodestro_runtime.pg_generic._sql import CATALOG, MAX_TENANT_CHARS, Executor, check_prefix, jsonb_text, render

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
}


# MUP-ONA T-GUARD-1
@pytest.mark.parametrize("value", list(REFUSED.values()), ids=list(REFUSED))
@pytest.mark.parametrize("name", PARTITIONED)
def test_the_guard_refuses_an_unbound_tenant_before_any_database_access(name: str, value: Any) -> None:
    spy = SpySource()
    executor = Executor(spy, prefix="nodestro_", tenant_id=TENANT)
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
        Executor(spy, prefix="nodestro_", tenant_id=TENANT).execute(name, params)
    assert raised.value.code == "partition.missing"
    assert spy.opened == []


def test_the_guard_also_holds_inside_a_transaction() -> None:
    spy = SpySource()
    executor = Executor(spy, prefix="nodestro_", tenant_id=TENANT)
    with executor.transaction(), pytest.raises(RetrievalError):
        executor.execute("chunks_count", _params("chunks_count", tenant_id="tenant-b"))
    assert spy.opened == [("transaction", TENANT)]  # the block's own transaction; the statement never ran


def test_the_instance_tenant_must_pass_the_guard_too() -> None:
    for value in REFUSED.values():
        if value in ("tenant-b", f" {TENANT}"):
            continue  # valid tenants of their own
        with pytest.raises(ValueError):
            Executor(SpySource(), prefix="nodestro_", tenant_id=value)


def test_only_catalog_names_with_their_parameters_run() -> None:
    spy = SpySource()
    executor = Executor(spy, prefix="nodestro_", tenant_id=TENANT)
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
        PostgresIndex(source, TENANT, prefix="nodestro_", namespace="n"),
        PostgresFileStore(source, TENANT, prefix="nodestro_", namespace="n"),
        PostgresMemory(source, TENANT, prefix="nodestro_", namespace="n"),
    ]


@pytest.mark.parametrize("value", ["tenant-b", "", None, " ", "a" * 10_000])
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
        Executor(spy, prefix=prefix, tenant_id=TENANT)
    for kind in (PostgresIndex, PostgresFileStore, PostgresMemory):
        with pytest.raises(ValueError, match="prefix"):
            kind(spy, TENANT, prefix=prefix)
    assert spy.opened == []


@pytest.mark.parametrize("prefix", ["nodestro_", "t_", "a_", "a1_b_", "a" * 31 + "_"])
def test_a_good_prefix_names_the_tables(prefix: str) -> None:
    # Rendering uses psycopg.sql (the postgres extra); the desktop profile runs this suite without it.
    pytest.importorskip("psycopg")
    text = render("vectors_store", prefix).as_string(None)
    assert f'"{prefix}chunk_vectors"' in text and f'"{prefix}chunks"' in text


def test_jsonb_text_is_the_document_stores_encoding() -> None:
    """The generic part keeps its own copy of ``storage_postgres._encode`` (it imports nothing of Nodestro)."""
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
    from nodestro_runtime.pg_generic import ConnectionSource
    from nodestro_runtime.storage_postgres import DatabaseSource

    source = DatabaseSource(database)
    assert isinstance(source, ConnectionSource)
    assert source.lock is database._lock
    executor = Executor(source, prefix="nodestro_", tenant_id=TENANT)
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
