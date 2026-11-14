"""The connection pool of :class:`PostgresDatabase` (backend audit C-01, A-03, C-14; needs ``NODESTRO_TEST_POSTGRES``).

A dropped connection (a server restart, a failover, an idle timeout of a proxy) must not stop the process for good; a
slow read of one tenant must not hold up the reads of every other tenant; every statement has a time limit; the
session-level advisory locks (the scheduler leader, the price sync) notice a lost connection.
"""

from __future__ import annotations

from collections.abc import Iterator
import subprocess
import sys
import threading
import time

import postgres_support
from pydantic import BaseModel, ConfigDict
import pytest

from nodestro_runtime.problems import NodestroError

pytest.importorskip("psycopg")

import psycopg

from nodestro_runtime.jobs import LeaderLock
from nodestro_runtime.storage_postgres import POSTGRES_SCHEMA_VERSION, PostgresDatabase, postgres_migrations


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


def test_writes_right_after_every_pooled_connection_was_dropped_succeed(database: PostgresDatabase) -> None:
    """RT2-03: a connection the server closed while it sat in the pool is replaced before a write gets it, also within
    ``CHECK_AFTER_IDLE`` of its last use (writes are never repeated, so the check has to happen before)."""
    store = database.store("acme")
    store.put_raw("x", "0", {"a": 0})
    with (
        database._pool.connection() as one,
        database._pool.connection() as two,
        database._pool.connection() as three,
        database._pool.connection() as four,
    ):
        pids = {connection.info.backend_pid for connection in (one, two, three, four)}
    _terminate(pids)
    time.sleep(0.2)  # well within CHECK_AFTER_IDLE: the idle check alone would not run
    for index in range(1, 2 * database.pool_size + 1):
        store.put_raw("x", str(index), {"a": index})
    with store.transaction():
        store.put_raw("x", "in-transaction", {"a": -1})
    assert len(store.items("x", _Any)) == 2 * database.pool_size + 2
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


def _lock_holder(key: int) -> int:
    """The server process that holds the one-key advisory lock ``key`` (asked as the owner of the tests)."""
    with psycopg.connect(postgres_support.DSN, autocommit=True) as admin:
        row = admin.execute(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted"
            " AND classid = ((%s::bigint >> 32) & 4294967295)::oid AND objid = (%s::bigint & 4294967295)::oid"
            " AND objsubid = 1",
            (key, key),
        ).fetchone()
    assert row is not None, "nobody holds the lock"
    return int(row[0])


def test_the_session_locks_are_tabrions_session_lock(database: PostgresDatabase) -> None:
    """TABRION MIG-NOD-4: the session-level advisory locks are a ``tabrion.postgres.SessionLock``."""
    from tabrion.postgres import SessionLock

    assert isinstance(database._session_locks, SessionLock)
    assert database.try_session_lock(4711)
    try:
        assert database._session_locks.held_keys() == {4711}
        assert _lock_holder(4711) == database._session.info.backend_pid
    finally:
        database.release_session_lock(4711)
    assert database._session_locks.held_keys() == frozenset()


def test_a_second_session_cannot_take_a_held_lock(database: PostgresDatabase, schema: str) -> None:
    other = PostgresDatabase(postgres_support.DSN, schema=schema)
    try:
        assert not other.holds_session_lock(4712)
        assert database.try_session_lock(4712)
        assert not other.try_session_lock(4712)
        assert not other.holds_session_lock(4712)
        other.release_session_lock(4712)  # not held: nothing happens, the holder keeps it
        assert database.holds_session_lock(4712)
        assert not other.try_session_lock(4712)
        database.release_session_lock(4712)
        assert other.try_session_lock(4712)
        assert not database.try_session_lock(4712)
        other.release_session_lock(4712)
    finally:
        other.close()


def test_a_lost_session_lock_is_taken_again_by_a_later_call(database: PostgresDatabase, schema: str) -> None:
    """The public API since before Tabrion: after a lost connection, ``try_session_lock`` takes the lock again when it
    is free, also without asking :meth:`holds_session_lock` first (SessionLock alone would refuse until then)."""
    other = PostgresDatabase(postgres_support.DSN, schema=schema)
    try:
        assert database.try_session_lock(4713) and database.try_session_lock(4713)
        _terminate({_lock_holder(4713)})
        time.sleep(0.2)
        assert other.try_session_lock(4713)  # free again: the server ended the leader's session
        assert not database.try_session_lock(4713)  # the loss is noticed, the lock is not taken twice
        assert not database.holds_session_lock(4713)
        other.release_session_lock(4713)
        assert database.try_session_lock(4713)  # without holds_session_lock: taken again
        assert database.holds_session_lock(4713)
        database.release_session_lock(4713)
        assert not database.holds_session_lock(4713)
        assert other.try_session_lock(4713)
        other.release_session_lock(4713)
    finally:
        other.close()


def test_the_session_locks_after_close(schema: str) -> None:
    database = PostgresDatabase(postgres_support.DSN, schema=schema)
    assert database.try_session_lock(4714)
    database.close()
    assert not database.holds_session_lock(4714)
    database.release_session_lock(4714)  # harmless: the lock went with the connection
    with pytest.raises(RuntimeError, match="closed"):
        database.try_session_lock(4714)
    other = PostgresDatabase(postgres_support.DSN, schema=schema)
    try:
        assert other.try_session_lock(4714)
        other.release_session_lock(4714)
    finally:
        other.close()


_LEADER = """
import sys
from nodestro_runtime.jobs import LeaderLock
from nodestro_runtime.storage_postgres import PostgresDatabase

database = PostgresDatabase(sys.argv[1], schema=sys.argv[2])
leader = LeaderLock(database, key=int(sys.argv[3]))
print("leader" if leader.acquire() else "follower", flush=True)
sys.stdin.readline()
"""


def test_the_leader_changes_between_two_processes(database: PostgresDatabase, schema: str) -> None:
    """Leader change across processes: the leader process dies, and another process takes over."""
    key = 4715
    child = subprocess.Popen(
        [sys.executable, "-c", _LEADER, database._conninfo, schema, str(key)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "leader"
        follower = LeaderLock(database, key=key)
        assert not follower.acquire()
        child.kill()  # no release: the server ends the session when the process is gone
        child.wait(timeout=10)
        deadline = time.monotonic() + 10
        while not follower.acquire():
            assert time.monotonic() < deadline, "the lock of the dead leader was not freed"
            time.sleep(0.05)
        assert database.holds_session_lock(key)
        follower.release()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def _settings(connection: psycopg.Connection) -> dict[str, str]:
    return {str(name): str(value) for name, value, _ in connection.execute("SHOW ALL").fetchall()}


def test_every_connection_sets_only_the_time_limits_and_the_schema(
    schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TABRION MIG-NOD-8: ``_configure`` goes through ``tabrion.postgres.configure`` and, with the defaults, sets what
    it always set and nothing else (``SHOW ALL`` of a pooled and of the lock connection against a plain connection):
    no ``lock_timeout``, no ``application_name``, no ``TimeZone``."""
    for variable in ("NODESTRO_DATABASE_STATEMENT_TIMEOUT", "NODESTRO_DATABASE_IDLE_IN_TRANSACTION_TIMEOUT"):
        monkeypatch.delenv(variable, raising=False)
    database = PostgresDatabase(postgres_support.DSN, schema=schema, pool_size=1)
    try:
        with psycopg.connect(database._conninfo, autocommit=True) as plain:
            baseline = _settings(plain)
        expected = {
            "statement_timeout": "30s",
            "idle_in_transaction_session_timeout": "5min",
            "search_path": f'"{schema}"',
        }
        with database._connection() as pooled:
            shown = _settings(pooled)
        assert {k: v for k, v in shown.items() if baseline.get(k) != v} == expected
        assert database.try_session_lock(4716)
        try:
            shown = _settings(database._session)
        finally:
            database.release_session_lock(4716)
        assert {k: v for k, v in shown.items() if baseline.get(k) != v} == expected
    finally:
        database.close()


def test_configure_without_a_schema_leaves_the_search_path(schema: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NODESTRO_DATABASE_STATEMENT_TIMEOUT", raising=False)
    database = PostgresDatabase(postgres_support.DSN, statement_timeout=1.5, idle_in_transaction_timeout=0, pool_size=1)
    try:
        with psycopg.connect(database._conninfo, autocommit=True) as plain:
            baseline = _settings(plain)
        with database._connection() as pooled:
            shown = _settings(pooled)
        changed = {k: v for k, v in shown.items() if baseline.get(k) != v}
        assert changed == {"statement_timeout": "1500ms"} | (
            {}
            if baseline["idle_in_transaction_session_timeout"] == "0"
            else {"idle_in_transaction_session_timeout": "0"}
        )
    finally:
        database.close()


def test_migrate_has_no_statement_time_limit(schema: str) -> None:
    database = PostgresDatabase(postgres_support.DSN, schema=schema, statement_timeout=0.2)
    try:
        current = POSTGRES_SCHEMA_VERSION
        slow = {current + 1: ("slow.sql", "SELECT pg_sleep(0.5)")}
        steps = {**postgres_migrations(), **slow}
        assert database.migrate(migrations=steps, before_migration=lambda *_: None) == current
        assert database.schema_version == current + 1
    finally:
        database.close()
