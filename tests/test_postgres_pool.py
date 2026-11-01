"""The connection pool of :class:`PostgresDatabase` (backend audit C-01, A-03, C-14; needs ``NODESTRO_TEST_POSTGRES``).

A dropped connection (a server restart, a failover, an idle timeout of a proxy) must not stop the process for good; a
slow read of one tenant must not hold up the reads of every other tenant; every statement has a time limit; the
session-level advisory locks (the scheduler leader, the price sync) notice a lost connection.
"""

from __future__ import annotations

from collections.abc import Iterator
import threading
import time

import postgres_support
from pydantic import BaseModel, ConfigDict
import pytest

from nodestro_runtime.problems import NodestroError

pytest.importorskip("psycopg")

import psycopg

from nodestro_runtime.jobs import LeaderLock
from nodestro_runtime.storage_postgres import POSTGRES_SCHEMA_VERSION, PostgresDatabase


@pytest.fixture
def schema() -> Iterator[str]:
    postgres_support.require_postgres()
    name = postgres_support.new_schema()
    database = PostgresDatabase(postgres_support.DSN, schema=name)
    database.migrate()
    database.close()
    yield name
    postgres_support.drop_schema(name)


@pytest.fixture
def database(schema: str) -> Iterator[PostgresDatabase]:
    opened = PostgresDatabase(postgres_support.DSN, schema=schema, pool_size=4)
    yield opened
    opened.close()


class _Any(BaseModel):
    model_config = ConfigDict(extra="allow")


def _terminate(pids: set[int]) -> None:
    with psycopg.connect(postgres_support.DSN, autocommit=True) as admin:
        for pid in pids:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))


def test_a_dropped_connection_is_replaced(database: PostgresDatabase) -> None:
    store = database.store("acme")
    store.put_raw("x", "1", {"a": 1})
    pids = {row[0] for row in database._query("SELECT pg_backend_pid()")}
    _terminate(pids)
    time.sleep(0.2)
    # Before the pool, every later call failed with "the connection is closed" until the process was restarted.
    assert store.get_raw("x", "1") == {"a": 1}
    store.put_raw("x", "2", {"a": 2})
    with store.transaction():
        store.put_raw("x", "3", {"a": 3})
    assert sorted(key for key, _ in store.items("x", _Any)) == ["1", "2", "3"]
    assert database.ping()


def test_a_write_on_a_connection_dropped_while_idle_succeeds(
    database: PostgresDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writes are not retried (they may have been applied), so an idle connection is checked before it is used."""
    import nodestro_runtime.storage_postgres as storage_postgres

    store = database.store("acme")
    store.put_raw("x", "1", {"a": 1})
    pids = {row[0] for row in database._query("SELECT pg_backend_pid()")}
    monkeypatch.setattr(storage_postgres, "CHECK_AFTER_IDLE", 0.0)
    _terminate(pids)
    time.sleep(0.2)
    store.put_raw("x", "2", {"a": 2})
    assert store.get_raw("x", "2") == {"a": 2}


def test_a_connection_in_steady_use_is_not_checked(database: PostgresDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection used again within ``CHECK_AFTER_IDLE`` skips the check (one round trip less per query)."""
    from psycopg_pool import ConnectionPool

    checks: list[object] = []
    monkeypatch.setattr(ConnectionPool, "check_connection", staticmethod(checks.append))
    store = database.store("acme")
    for index in range(20):
        store.put_raw("x", str(index), {"a": index})
        store.get_raw("x", str(index))
    assert checks == []


def test_every_pooled_connection_can_be_dropped(database: PostgresDatabase) -> None:
    store = database.store("acme")
    store.put_raw("x", "1", {"a": 1})
    pids = set()
    with database._pool.connection() as one, database._pool.connection() as two, database._pool.connection() as three:
        for connection in (one, two, three):
            pids.add(connection.info.backend_pid)
    _terminate(pids)
    for _ in range(5):
        assert store.get_raw("x", "1") == {"a": 1}
    assert {row[0] for row in database._query("SELECT pg_backend_pid()")}.isdisjoint(pids)


def test_a_failed_transaction_on_a_lost_connection_leaves_the_database_usable(database: PostgresDatabase) -> None:
    store = database.store("acme")
    with pytest.raises(psycopg.OperationalError), store.transaction():
        store.put_raw("x", "lost", {"a": 1})
        _terminate({row[0] for row in database._query("SELECT pg_backend_pid()")})
        store.put_raw("x", "lost2", {"a": 2})
    assert store.get_raw("x", "lost") is None  # rolled back with the connection
    store.put_raw("x", "after", {"a": 3})
    assert store.get_raw("x", "after") == {"a": 3}


def test_reads_of_other_threads_do_not_wait_for_a_slow_read(database: PostgresDatabase) -> None:
    database.store("quiet").put_raw("x", "1", {"a": 1})
    started = threading.Event()

    def slow() -> None:
        started.set()
        database._query("SELECT pg_sleep(1.5)")

    thread = threading.Thread(target=slow)
    thread.start()
    started.wait()
    time.sleep(0.1)
    began = time.monotonic()
    assert database.store("quiet").get_raw("x", "1") == {"a": 1}
    waited = time.monotonic() - began
    thread.join()
    # One shared connection behind one lock made this read wait for the whole 1.5 s.
    assert waited < 0.75


def test_reads_do_not_wait_for_another_threads_transaction(database: PostgresDatabase) -> None:
    store = database.store("acme")
    store.put_raw("x", "1", {"a": 1})
    inside, done = threading.Event(), threading.Event()

    def writer() -> None:
        with store.transaction():
            store.put_raw("x", "1", {"a": 2})
            inside.set()
            done.wait(5)

    thread = threading.Thread(target=writer)
    thread.start()
    inside.wait(5)
    try:
        assert store.get_raw("x", "1") == {"a": 1}  # committed data, at once
    finally:
        done.set()
        thread.join()
    assert store.get_raw("x", "1") == {"a": 2}


def test_statements_have_a_time_limit(schema: str) -> None:
    database = PostgresDatabase(postgres_support.DSN, schema=schema, statement_timeout=0.3)
    try:
        assert database._query("SHOW statement_timeout") == [("300ms",)]
        assert database._query("SHOW idle_in_transaction_session_timeout") == [("5min",)]
        with pytest.raises(psycopg.errors.QueryCanceled):
            database._query("SELECT pg_sleep(2)")
        assert database.ping()  # the connection stays usable
    finally:
        database.close()


def test_the_defaults_and_the_environment(schema: str, monkeypatch: pytest.MonkeyPatch) -> None:
    database = PostgresDatabase(postgres_support.DSN, schema=schema)
    try:
        assert (database.pool_size, database.statement_timeout, database.idle_in_transaction_timeout) == (
            8,
            30.0,
            300.0,
        )
        assert database._query("SHOW statement_timeout") == [("30s",)]
    finally:
        database.close()
    monkeypatch.setenv("NODESTRO_DATABASE_POOL_SIZE", "3")
    monkeypatch.setenv("NODESTRO_DATABASE_STATEMENT_TIMEOUT", "0")
    monkeypatch.setenv("NODESTRO_DATABASE_IDLE_IN_TRANSACTION_TIMEOUT", "60")
    database = PostgresDatabase(postgres_support.DSN, schema=schema)
    try:
        assert database.pool_size == 3
        assert database._query("SHOW statement_timeout") == [("0",)]
        assert database._query("SHOW idle_in_transaction_session_timeout") == [("1min",)]
    finally:
        database.close()
    monkeypatch.setenv("NODESTRO_DATABASE_POOL_SIZE", "none")
    with pytest.raises(NodestroError) as caught:
        PostgresDatabase(postgres_support.DSN, schema=schema)
    assert caught.value.code == "storage.settings_invalid"


def test_an_unreachable_database_fails_at_once() -> None:
    postgres_support.require_postgres()
    began = time.monotonic()
    with pytest.raises(psycopg.OperationalError):
        PostgresDatabase("host=127.0.0.1 port=1 dbname=none connect_timeout=2")
    assert time.monotonic() - began < 5


def test_ping(database: PostgresDatabase) -> None:
    assert database.ping()
    database.close()
    assert not database.ping()


def test_the_leader_lock_is_lost_with_its_connection_and_taken_again(schema: str) -> None:
    first = PostgresDatabase(postgres_support.DSN, schema=schema)
    second = PostgresDatabase(postgres_support.DSN, schema=schema)
    try:
        leader, other = LeaderLock(first, key=4242), LeaderLock(second, key=4242)
        assert leader.acquire() and leader.acquire()
        assert not other.acquire()
        # The leader's session ends (a restart of the connection): the lock is free, and the old leader knows it.
        _terminate({first._session.info.backend_pid})
        time.sleep(0.2)
        assert other.acquire()
        assert not leader.acquire()  # it no longer believes it leads
        other.release()
        assert leader.acquire()
        leader.release()
        assert other.acquire()
        other.release()
    finally:
        first.close()
        second.close()


def test_the_session_lock_counts_like_postgresql(database: PostgresDatabase, schema: str) -> None:
    other = PostgresDatabase(postgres_support.DSN, schema=schema)
    try:
        assert database.try_session_lock(77) and database.try_session_lock(77)
        database.release_session_lock(77)
        assert database.holds_session_lock(77)
        assert not other.try_session_lock(77)
        database.release_session_lock(77)
        assert not database.holds_session_lock(77)
        assert other.try_session_lock(77)
        other.release_session_lock(77)
    finally:
        other.close()


def test_migrate_has_no_statement_time_limit(schema: str) -> None:
    database = PostgresDatabase(postgres_support.DSN, schema=schema, statement_timeout=0.2)
    try:
        current = POSTGRES_SCHEMA_VERSION
        slow = {current + 1: ("slow.sql", "SELECT pg_sleep(0.5)")}
        steps = {**{version: ("x", "SELECT 1") for version in range(1, current + 1)}, **slow}
        assert database.migrate(migrations=steps, before_migration=lambda *_: None) == current
        assert database.schema_version == current + 1
    finally:
        database.close()
