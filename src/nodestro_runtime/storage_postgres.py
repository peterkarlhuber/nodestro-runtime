"""The :class:`~nodestro_runtime.storage.DocumentStore` on PostgreSQL, for the server profile (ADR-0002, ADR-0003).

:class:`PostgresDatabase` owns a pool of connections to a PostgreSQL database (optionally a schema in it) and its schema
migrations; :meth:`PostgresDatabase.store` gives the documents of one tenant as a :class:`PostgresStore`. Every row
carries ``tenant_id`` and every query filters on it, so one tenant's store never reads or changes another tenant's
documents.

Needs the optional dependency ``nodestro-runtime[postgres]`` (psycopg 3). The desktop never imports this module.

Semantics follow the SQLite store (``tests/test_storage_contract.py`` runs the same cases on both): every write is
atomic, and :meth:`PostgresDatabase.transaction` groups writes (reentrant, all-or-nothing; the transactions of one
process run one after the other, each on the calling thread's own pooled connection). Reads run in parallel on pooled
connections and see committed data. Several processes may use the same database; PostgreSQL's row locks keep their
writes consistent, and :meth:`PostgresStore.lock` serializes read-modify-write steps across processes.

Migrations (forward only) are the numbered SQL files in ``nodestro_runtime/migrations/postgres``. They run under a
PostgreSQL advisory lock, so several processes starting at once apply each step exactly once, and are recorded in
``nodestro_schema_migrations`` with the SHA-256 checksum of each file. A database written by a newer release is
refused (``storage.schema_too_new``), and so is a shipped migration whose file differs from the one that was applied
(``storage.migration_changed``: never edit an applied migration, add a new one); rows written before checksums were
recorded are adopted with the checksum of the unchanged file the first time this release migrates. Before a database
that already has a schema is migrated, ``before_migration`` must write a backup (operators: ``pg_dump``); a fresh
database needs none.

Database roles (server profile): migrations run as the **owner** role of the tables; ``api`` and ``worker`` run as a
**runtime** role with data privileges only (:func:`grant_runtime_role`, :meth:`PostgresDatabase.role_info`), so the
row-level security of Axentra's tables in the same schema applies to them and they cannot change the schema.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import datetime
import hashlib
from importlib import resources
import json
import os
import re
import select
import threading
import time
from typing import TYPE_CHECKING, Any
import weakref

from pydantic import BaseModel

from .problems import NodestroError
from .storage import PAGED_COLLECTIONS, BeforeMigration, DamagedDocuments, ModelT, cursor_time

if TYPE_CHECKING:
    from psycopg.abc import Params, Query

try:  # optional: nodestro-runtime[postgres]
    import psycopg
    from psycopg import sql
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover - exercised only without the extra
    psycopg = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]
    ConnectionPool = None  # type: ignore[assignment,misc]

_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: Tenant ids: lower-case letters, digits, ``-`` and ``_``; at most 63 characters. The desktop's tenant is ``local``.
TENANT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_SCHEMA = re.compile(r"[a-z_][a-z0-9_]{0,62}")
#: The advisory lock key of the migration runner (any constant; ``nodestro`` in ASCII).
MIGRATION_LOCK = 0x6E6F646573747230
#: The advisory lock class of :meth:`PostgresStore.lock` (two-key form, so it never meets the one-key locks above).
DOCUMENT_LOCK_CLASS = 0x6E6F6400
#: Defaults of the connection pool and its time limits (seconds); the environment and the constructor override them.
DEFAULT_POOL_SIZE = 8
DEFAULT_STATEMENT_TIMEOUT = 30.0
DEFAULT_IDLE_IN_TRANSACTION_TIMEOUT = 300.0
#: A pooled connection idle for longer than this (seconds) is checked before it is handed out again; a connection
#: in steady use is not, which saves a round trip on every query. A read that still meets a lost connection is
#: retried on another one (reads are safe to repeat; writes are not, and fail).
CHECK_AFTER_IDLE = 2.0
#: A migration step: SQL text (several statements allowed) or a function that runs on the connection.
PostgresStep = str | Callable[[Any], None]


_NUL_ESCAPE = "\\u0000"


def _without_nul(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, dict):
        return {_without_nul(key): _without_nul(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_nul(item) for item in value]
    return value


def _encode(text: str) -> tuple[str, str | None]:
    """``(data, raw)`` for the table: jsonb cannot hold U+0000, so such a document keeps its exact JSON in ``raw``."""
    if _NUL_ESCAPE not in text:
        return text, None
    return json.dumps(_without_nul(json.loads(text))), text


#: The exact JSON text of a row.
_JSON = "coalesce(raw, data::text)"
#: The creation order of :meth:`PostgresStore.list_page` (RT2-05): the document's ``created_at`` as a time, ``NULL``
#: when it is missing or not a time; the expression of the indexes of migration 0009, which a query must repeat.
_CREATED_ORDER = "nodestro_created_order(data ->> 'created_at')"


def _setting(given: float | None, variable: str, default: float, *, minimum: float) -> Any:
    """``given``, else the environment variable ``variable``, else ``default``; a whole number when ``default`` is."""
    kind = int if isinstance(default, int) else float
    if given is None:
        text = os.environ.get(variable, "").strip()
        if not text:
            return default
        try:
            given = kind(text)
        except ValueError:
            given = None
        if given is None or given < minimum:
            raise NodestroError(
                "storage.settings_invalid",
                f"{variable} must be a {'whole ' if kind is int else ''}number of at least {minimum:g}, not {text!r}",
                variable=variable,
            )
    if given < minimum:
        raise ValueError(f"{variable}: at least {minimum:g}")
    return kind(given)


def _require_driver() -> None:
    if psycopg is None:
        raise NodestroError(
            "storage.postgres_unavailable",
            "PostgreSQL storage needs the optional dependency: pip install 'nodestro-runtime[postgres]'",
        )


def postgres_migrations() -> dict[int, tuple[str, str]]:
    """The migrations shipped with this release: version -> (file name, SQL), from ``migrations/postgres``."""
    found: dict[int, tuple[str, str]] = {}
    for entry in resources.files("nodestro_runtime.migrations").joinpath("postgres").iterdir():
        match = re.fullmatch(r"(\d{4})_[a-z0-9_]+\.sql", entry.name)
        if match:
            found[int(match.group(1))] = (entry.name, entry.read_text(encoding="utf-8"))
    return dict(sorted(found.items()))


#: The schema version this release reads and writes on PostgreSQL (the newest shipped migration).
POSTGRES_SCHEMA_VERSION = max(postgres_migrations())


def migration_checksum(step: PostgresStep) -> str | None:
    """The SHA-256 of a migration's SQL text (as recorded in ``nodestro_schema_migrations.checksum``); ``None`` for a
    step given as a function (tests), which cannot be compared."""
    return hashlib.sha256(step.encode("utf-8")).hexdigest() if isinstance(step, str) else None


#: Data privileges of the runtime role (never DDL), see :func:`grant_runtime_role`.
RUNTIME_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"
#: Bookkeeping tables of the migration runners (Nodestro's and Axentra's): read-only for the runtime role.
SCHEMA_VERSION_TABLES = ("nodestro_schema_migrations", "axentra_schema_migrations")


def _server_spoke(connection: Any) -> bool:
    """Whether data or the end of the stream waits on the socket of an idle connection (no round trip)."""
    try:
        fd = connection.pgconn.socket
    except Exception:  # no socket any more: certainly not usable
        return True
    try:
        if hasattr(select, "poll"):
            poller = select.poll()
            poller.register(fd, select.POLLIN | select.POLLERR | select.POLLHUP)
            return bool(poller.poll(0))
        readable, _, failed = select.select([fd], [], [fd], 0)
        return bool(readable or failed)
    except (OSError, ValueError):
        return True


def _still_usable(connection: Any) -> bool:
    """Whether a connection answers an empty statement (one round trip)."""
    try:
        ConnectionPool.check_connection(connection)
    except Exception:
        return False
    return True


class PostgresDatabase:
    """A pool of PostgreSQL connections shared by the stores of every tenant, with its schema migrations.

    ``conninfo`` is a libpq connection string or URL (``postgresql://user@host/db``); credentials come from it or
    from the usual ``PG*`` variables and ``~/.pgpass``, never from Nodestro's settings. ``schema`` puts every table
    into that schema (created by :meth:`migrate` when missing); without it the server's ``search_path`` applies.

    The pool holds at most ``pool_size`` connections (default ``NODESTRO_DATABASE_POOL_SIZE``, else 8). A
    connection that sat idle in the pool for more than :data:`CHECK_AFTER_IDLE` seconds is checked before it is
    handed out, so a connection the server dropped (a restart, a failover, an idle timeout of a proxy) is replaced
    instead of failing the next call; so is, on every checkout, a connection the server closed or spoke on while it
    sat idle, which shows on its socket without a round trip (RT2-03). A read outside a transaction that meets a lost
    connection anyway is retried on another one; a write is not repeated (it may have been applied) and fails. The
    pool discards lost connections.
    ``statement_timeout`` (seconds, default ``NODESTRO_DATABASE_STATEMENT_TIMEOUT``, else 30; 0: none) bounds every
    statement and ``idle_in_transaction_timeout`` (``NODESTRO_DATABASE_IDLE_IN_TRANSACTION_TIMEOUT``, else 300) every
    transaction left idle. Session-level advisory locks (:meth:`try_session_lock`) use one connection of their own.
    """

    def __init__(
        self,
        conninfo: str,
        *,
        schema: str | None = None,
        connect_timeout: int = 10,
        pool_size: int | None = None,
        statement_timeout: float | None = None,
        idle_in_transaction_timeout: float | None = None,
    ) -> None:
        _require_driver()
        if schema is not None and not _SCHEMA.fullmatch(schema):
            raise ValueError(f"Invalid schema name {schema!r}")
        self.schema = schema
        self.pool_size = _setting(pool_size, "NODESTRO_DATABASE_POOL_SIZE", DEFAULT_POOL_SIZE, minimum=1)
        self.statement_timeout = _setting(
            statement_timeout, "NODESTRO_DATABASE_STATEMENT_TIMEOUT", DEFAULT_STATEMENT_TIMEOUT, minimum=0
        )
        self.idle_in_transaction_timeout = _setting(
            idle_in_transaction_timeout,
            "NODESTRO_DATABASE_IDLE_IN_TRANSACTION_TIMEOUT",
            DEFAULT_IDLE_IN_TRANSACTION_TIMEOUT,
            minimum=0,
        )
        #: Mirrors audit events and AI usage into a platform backend (:mod:`~nodestro_runtime.platform_events`);
        #: ``None``: nothing is mirrored. A server sets it when ``NODESTRO_AXENTRA_AUDIT`` or ``..._USAGE`` is on.
        self.platform_events: Any = None
        self._conninfo = conninfo
        self._connect_kwargs: dict[str, Any] = {"autocommit": True, "connect_timeout": connect_timeout}
        #: Serializes the transactions (and so the writes) of this process, as with one connection: a thread that
        #: writes waits until another thread's transaction ends. Reads do not wait; each takes a pooled connection.
        self._lock = threading.RLock()
        #: The calling thread's transaction: its connection and nesting depth.
        self._local = threading.local()
        self._closed = False
        #: When each pooled connection went back into the pool (``time.monotonic()``).
        self._returned: weakref.WeakKeyDictionary[Any, float] = weakref.WeakKeyDictionary()
        # A first connection outside the pool reports a wrong address or password at once (the pool would retry).
        probe = psycopg.connect(conninfo, **self._connect_kwargs)
        try:
            self._configure(probe)
        finally:
            probe.close()
        self._pool = ConnectionPool(
            conninfo,
            kwargs=self._connect_kwargs,
            min_size=1,
            max_size=self.pool_size,
            open=False,
            configure=self._configure,
            check=self._check_if_idle,
            name="nodestro",
        )
        try:
            self._pool.open(wait=True, timeout=max(float(connect_timeout), 1.0))
        except BaseException:
            self._pool.close()
            raise
        #: The connection that holds session-level advisory locks (key -> how often this process took it).
        self._session_guard = threading.Lock()
        self._session: Any = None
        self._session_held: dict[int, int] = {}

    def _configure(self, connection: Any) -> None:
        """Settings of every new connection: the schema and the time limits (one round trip)."""
        connection.execute(
            "SELECT set_config('statement_timeout', %s, false),"
            " set_config('idle_in_transaction_session_timeout', %s, false)"
            + (", set_config('search_path', %s, false)" if self.schema is not None else ""),
            (
                f"{int(self.statement_timeout * 1000)}",
                f"{int(self.idle_in_transaction_timeout * 1000)}",
                *((f'"{self.schema}"',) if self.schema is not None else ()),
            ),
        )

    @contextmanager
    def _connection(self, timeout: float | None = None) -> Iterator[Any]:
        """A pooled connection; notes when it goes back (no ``reset`` callback: that returns through a worker).

        An idle connection in the pool has no statement in flight, so the server has nothing to say on it; data (or
        the end of the stream) waiting on its socket means the server closed it (a restart, a failover,
        ``pg_terminate_backend``) or sent a notice. That costs no round trip to find out, so it is looked at on every
        checkout, and such a connection is checked and, when it is broken, closed (the pool discards it) before
        another one is taken, without the pool's back-off between failed checks. Nothing was sent on it yet, so this
        never repeats a statement: a write must never meet such a connection, because a failed write is not repeated
        (RT2-03). After ``pool_size`` broken connections the next one is handed out as it is."""
        attempts = self.pool_size + 1
        for attempt in range(1, attempts + 1):
            with self._pool.connection(timeout=timeout) as connection:
                if attempt < attempts and _server_spoke(connection) and not _still_usable(connection):
                    connection.close()
                    continue
                try:
                    yield connection
                finally:
                    self._returned[connection] = time.monotonic()
                return

    def _check_if_idle(self, connection: Any) -> None:
        """Check a connection that sat idle (raises when it is broken; the pool then opens another one)."""
        returned = self._returned.get(connection)
        if returned is not None and time.monotonic() - returned > CHECK_AFTER_IDLE:
            ConnectionPool.check_connection(connection)

    # ---- connections and transactions ------------------------------------------------------

    def close(self) -> None:
        """Close every connection; closing twice is harmless."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        with self._session_guard:
            if self._session is not None:
                with suppress(Exception):
                    self._session.close()
                self._session = None
                self._session_held.clear()
        self._pool.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def ping(self, *, timeout: float = 5.0) -> bool:
        """Whether the database answers a trivial query within ``timeout`` seconds (a health check; never raises)."""
        if self._closed:
            return False
        try:
            with self._connection(timeout=timeout) as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception:
            return False
        return True

    @property
    def in_transaction(self) -> bool:
        """Whether the calling thread is inside :meth:`transaction`."""
        return bool(getattr(self._local, "depth", 0))

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group writes into one transaction: all of them are stored, or none. Reentrant; other threads wait."""
        with self._transaction():
            yield

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        state = self._local
        if getattr(state, "depth", 0):
            state.depth += 1
            try:
                yield state.connection
            finally:
                state.depth -= 1
            return
        with self._lock:
            if self._closed:
                raise RuntimeError("The database is closed")
            with self._connection() as connection:
                connection.execute("BEGIN")
                state.connection, state.depth = connection, 1
                try:
                    yield connection
                except BaseException:
                    state.connection, state.depth = None, 0
                    with suppress(psycopg.Error):  # a lost connection: the pool discards it
                        connection.execute("ROLLBACK")
                    raise
                state.connection, state.depth = None, 0
                connection.execute("COMMIT")

    def _query(self, query: Query, params: Params = ()) -> builtins.list[tuple[Any, ...]]:
        """The rows of a read. Outside a transaction a lost connection is replaced and the read repeated (RT2-03),
        so a statement that writes never goes through here (:meth:`_write`, :meth:`_write_returning`), except one
        that is safe to repeat, as listed in ``tests/test_query_writes.py`` with its reason (JQ-3)."""
        current: Any = getattr(self._local, "connection", None)
        if current is not None:  # inside this thread's transaction: read its own writes
            rows: builtins.list[tuple[Any, ...]] = current.execute(query, params).fetchall()
            return rows
        attempts = self.pool_size + 1  # every pooled connection may have been dropped at once
        for attempt in range(1, attempts + 1):
            if self._closed:
                raise RuntimeError("The database is closed")
            connection: Any = None
            try:
                with self._connection() as connection:
                    rows = connection.execute(query, params).fetchall()
                return rows
            except psycopg.OperationalError:
                # A lost connection (not a statement timeout, which leaves the connection usable): try another one.
                if connection is None or not connection.broken or attempt == attempts:
                    raise
        raise AssertionError("unreachable")

    def _write_returning(
        self, query: Query, params: Params = (), *, if_lost: tuple[Query, Params] | None = None
    ) -> builtins.list[tuple[Any, ...]]:
        """The rows of one writing statement with ``RETURNING``, run as :meth:`_write` runs a write: in the calling
        thread's transaction, or else in autocommit, serialized with this process's transactions and never repeated
        (RT2-03). A lost connection may have lost the reply of a statement the server applied and committed, so
        running it again could apply it twice or answer for a second change (JQ-3).

        ``if_lost`` is a read (statement, parameters) that finds what the write did when its connection was lost
        outside a transaction: its rows stand for the lost reply; no rows mean the write cannot be shown to have
        been applied, and the error of the lost connection is raised. Any other error is raised as it is."""
        if getattr(self._local, "depth", 0):
            rows: builtins.list[tuple[Any, ...]] = self._local.connection.execute(query, params).fetchall()
            return rows
        with self._lock:
            if self._closed:
                raise RuntimeError("The database is closed")
            connection: Any = None
            try:
                with self._connection() as connection:
                    rows = connection.execute(query, params).fetchall()
                return rows
            except psycopg.OperationalError as exc:
                # A statement timeout leaves the connection usable and the statement rolled back: nothing to find.
                if if_lost is None or connection is None or not connection.broken:
                    raise
                lost = exc
        found = self._query(*if_lost)  # a read: retried on another connection if this one is lost too
        if not found:
            raise lost
        return found

    def _write(self, query: Query, params: Params) -> int:
        if getattr(self._local, "depth", 0):
            return int(self._local.connection.execute(query, params).rowcount)
        # One statement is atomic on its own: run it in autocommit (no BEGIN/COMMIT round trips), still serialized
        # with the transactions of this process.
        with self._lock:
            if self._closed:
                raise RuntimeError("The database is closed")
            with self._connection() as connection:
                return int(connection.execute(query, params).rowcount)

    # ---- session-level advisory locks ------------------------------------------------------------

    def _session_connection(self) -> Any:
        """The live connection of the session locks (called under ``_session_guard``); a lost one is replaced, and
        the locks it held are gone with it."""
        if self._session is not None:
            try:
                self._session.execute("SELECT 1")
            except psycopg.Error:
                with suppress(Exception):
                    self._session.close()
                self._session = None
                self._session_held.clear()
        if self._session is None:
            if self._closed:
                raise RuntimeError("The database is closed")
            connection = psycopg.connect(self._conninfo, **self._connect_kwargs)
            try:
                self._configure(connection)
            except BaseException:
                connection.close()
                raise
            self._session = connection
        return self._session

    def try_session_lock(self, key: int) -> bool:
        """Take the session-level advisory lock ``key`` unless another process holds it (``False``).

        Held until :meth:`release_session_lock` or until the connection of the locks ends (with the process, or
        when the server drops it: then :meth:`holds_session_lock` says ``False`` and a later call takes it again,
        if it is still free). Taking it again in this process counts, as in PostgreSQL."""
        with self._session_guard:
            connection = self._session_connection()
            if self._session_held.get(key):
                self._session_held[key] += 1
                return True
            row = connection.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()
            if not row or not row[0]:
                return False
            self._session_held[key] = 1
            return True

    def holds_session_lock(self, key: int) -> bool:
        """Whether this process still holds the session lock ``key`` (checks that its connection is alive)."""
        with self._session_guard:
            if not self._session_held.get(key) or self._closed:
                return False
            try:
                self._session_connection()
            except Exception:
                return False
            return bool(self._session_held.get(key))

    def release_session_lock(self, key: int) -> None:
        """Give up one hold of the session lock ``key`` (the lock is free when the last one is released)."""
        with self._session_guard:
            count = self._session_held.get(key, 0)
            if count > 1:
                self._session_held[key] = count - 1
                return
            if not count:
                return
            del self._session_held[key]
            if self._session is not None:
                with suppress(psycopg.Error):
                    self._session.execute("SELECT pg_advisory_unlock(%s)", (key,))

    # ---- tenants -----------------------------------------------------------------------------

    def store(self, tenant_id: str) -> PostgresStore:
        """The documents of ``tenant_id`` (validated against :data:`TENANT_ID`)."""
        return PostgresStore(self, tenant_id)

    # ---- schema versions and migrations ----------------------------------------------------------

    @property
    def schema_version(self) -> int:
        """The newest applied migration (0: no Nodestro tables yet)."""
        exists = self._query("SELECT to_regclass('nodestro_schema_migrations') IS NOT NULL")
        if not exists or not exists[0][0]:
            return 0
        row = self._query("SELECT coalesce(max(version), 0) FROM nodestro_schema_migrations")
        return int(row[0][0]) if row else 0

    def migrate(
        self,
        *,
        before_migration: BeforeMigration | None = None,
        migrations: Mapping[int, tuple[str, PostgresStep]] | None = None,
    ) -> int:
        """Apply every missing migration; returns the version the database had.

        Runs in one transaction under an advisory lock: concurrent callers wait, then find nothing to do. A
        database of a newer release is refused (``storage.schema_too_new``) and a gap in the steps
        (``storage.migration_missing``) before anything is written. A database that already has a schema needs
        ``before_migration(current, target)`` to write a backup first (``storage.backup_required``,
        ``storage.backup_failed``); a failing step (``storage.migration_failed``) leaves the database unchanged.
        """
        steps: Mapping[int, tuple[str, PostgresStep]] = postgres_migrations() if migrations is None else migrations
        target = max(steps) if steps else 0
        with self._lock:
            if self.in_transaction:
                raise RuntimeError("migrate() cannot run inside a transaction")
            with self._transaction() as db:
                # Building an index of a large table may take longer than the statement timeout of normal work.
                db.execute("SET LOCAL statement_timeout = 0")
                # The lock first: CREATE ... IF NOT EXISTS is not safe against a concurrent creator.
                db.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
                if (
                    self.schema is not None
                    and not self._query("SELECT to_regnamespace(%s) IS NOT NULL", (self.schema,))[0][0]
                ):
                    # Only when missing: CREATE SCHEMA IF NOT EXISTS needs the CREATE privilege on the database even
                    # when the schema exists, which an owner role that was given only its schema does not have.
                    db.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
                db.execute(
                    "CREATE TABLE IF NOT EXISTS nodestro_schema_migrations ("
                    " version integer PRIMARY KEY, name text NOT NULL,"
                    " applied_at timestamptz NOT NULL DEFAULT now(), checksum text)"
                )
                # A table of a release before checksums gets its column only after the backup (OPS2-01): ALTER TABLE
                # holds an ACCESS EXCLUSIVE lock until this transaction ends, and the backup (pg_dump on another
                # connection) would wait for it while this transaction waits for the backup. Before the backup this
                # transaction takes no lock that a reader waits for; the applied migrations are checked (by name
                # without the column) without writing anything.
                checksums = self._has_checksums()
                current = self.schema_version
                self._verify_applied(steps, adopt=False)
                if current == target:
                    self._adopt_checksums(db, steps, checksums)
                    return current
                if current > target:
                    raise NodestroError(
                        "storage.schema_too_new",
                        f"The PostgreSQL database has schema version {current}; this Nodestro release reads "
                        f"version {target}. Use the newer release, or restore a backup made with this one",
                        found=current,
                        supported=target,
                    )
                missing = [version for version in range(current + 1, target + 1) if version not in steps]
                if missing:
                    raise NodestroError(
                        "storage.migration_missing",
                        f"No migration to schema version {missing[0]}",
                        found=current,
                        supported=target,
                    )
                if current > 0:
                    if before_migration is None:
                        raise NodestroError(
                            "storage.backup_required",
                            "A schema migration of a database with data needs a backup first; none was configured",
                            found=current,
                            supported=target,
                        )
                    try:
                        before_migration(current, target)
                    except Exception as exc:
                        raise NodestroError(
                            "storage.backup_failed",
                            f"The backup before the schema migration failed, so nothing was changed: {exc}",
                            found=current,
                            supported=target,
                        ) from exc
                self._adopt_checksums(db, steps, checksums)
                step = current
                try:
                    with db.transaction():  # a savepoint: the failure below is reported, then everything rolls back
                        for step in range(current + 1, target + 1):
                            name, change = steps[step]
                            if isinstance(change, str):
                                db.execute(change)
                            else:
                                change(db)
                            db.execute(
                                "INSERT INTO nodestro_schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                                (step, name, migration_checksum(change)),
                            )
                        if current > 0:
                            # Fresh statistics for the changed indexes: without them the planner may pick a wrong
                            # index until autovacuum gets to the table.
                            db.execute("ANALYZE nodestro_documents")
                except Exception as exc:
                    raise NodestroError(
                        "storage.migration_failed",
                        f"The schema migration to version {step} failed; the database is unchanged: {exc}",
                        found=current,
                        supported=target,
                        step=step,
                    ) from exc
                return current

    def _adopt_checksums(self, db: Any, steps: Mapping[int, tuple[str, PostgresStep]], checksums: bool) -> None:
        """Inside :meth:`migrate`, after the backup: give a schema table of a release before checksums its column and
        record the checksums of the migrations applied without them."""
        if not checksums:
            db.execute("ALTER TABLE nodestro_schema_migrations ADD COLUMN IF NOT EXISTS checksum text")
        self._verify_applied(steps, adopt=True)

    def _has_checksums(self) -> bool:
        rows = self._query(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('nodestro_schema_migrations')"
            " AND attname = 'checksum' AND NOT attisdropped)"
        )
        return bool(rows and rows[0][0])

    def _verify_applied(self, steps: Mapping[int, tuple[str, PostgresStep]], *, adopt: bool) -> None:
        """Refuse applied migrations whose shipped file differs (``storage.migration_changed``); with ``adopt``
        (under the migration lock), record the checksum of rows written before checksums were kept, after checking
        that the file name is the one that was applied. A table of a release before checksums has only the names
        to compare (and nothing is adopted until :meth:`migrate` added the column)."""
        query = "SELECT version, name, checksum FROM nodestro_schema_migrations ORDER BY version"
        if not self._has_checksums():
            exists = self._query("SELECT to_regclass('nodestro_schema_migrations') IS NOT NULL")
            if not exists or not exists[0][0]:
                return
            query = "SELECT version, name, NULL::text FROM nodestro_schema_migrations ORDER BY version"
            adopt = False
        changed: builtins.list[str] = []
        adopted: builtins.list[tuple[str, int]] = []
        for version, name, checksum in self._query(query):
            if version not in steps:
                continue  # newer than this release: refused as storage.schema_too_new
            shipped_name, step = steps[version]
            shipped = migration_checksum(step)
            if shipped is None:
                continue
            if name != shipped_name or (checksum is not None and checksum != shipped):
                changed.append(f"{version:04d} ({name})")
            elif checksum is None:
                adopted.append((shipped, version))
        if changed:
            raise NodestroError(
                "storage.migration_changed",
                "Applied migrations differ from the files of this release: " + ", ".join(changed) + ". An applied "
                "migration is never edited; restore a backup made with the release that applied them",
                changed=changed,
            )
        if adopt and adopted:
            for checksum, version in adopted:
                self._write(
                    "UPDATE nodestro_schema_migrations SET checksum = %s WHERE version = %s AND checksum IS NULL",
                    (checksum, version),
                )

    def require_current_schema(self) -> None:
        """Refuse to serve a database that is not at :data:`POSTGRES_SCHEMA_VERSION` (``storage.schema_mismatch``) or
        whose applied migrations differ from this release's files (``storage.migration_changed``)."""
        found = self.schema_version
        if found != POSTGRES_SCHEMA_VERSION:
            raise NodestroError(
                "storage.schema_mismatch",
                f"The PostgreSQL database has schema version {found}; this release needs {POSTGRES_SCHEMA_VERSION}. "
                "Run the migration first",
                found=found,
                supported=POSTGRES_SCHEMA_VERSION,
            )
        self._verify_applied(postgres_migrations(), adopt=False)

    # ---- database roles ----------------------------------------------------------------------------

    def role_info(self) -> dict[str, Any]:
        """How row-level security applies to this connection's role: ``superuser`` and ``bypass_rls`` (ignore every
        policy), ``table_owner`` (owns, or may act as the owner of, a table of its schema: it could switch row-level
        security off, alter or drop the table) and ``role`` (its name)."""
        rows = self._query(
            "SELECT current_user::text, r.rolsuper, r.rolbypassrls,"
            " EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            "   WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p')"
            "   AND pg_has_role(current_user, c.relowner, 'MEMBER'))"
            " FROM pg_roles r WHERE r.rolname = current_user"
        )
        role, superuser, bypass, owner = rows[0] if rows else ("", False, False, False)
        return {"role": role, "superuser": bool(superuser), "bypass_rls": bool(bypass), "table_owner": bool(owner)}

    def shared_pool(self) -> SharedPool:
        """This database's pool for another library of the same process (Axentra: ``Database.on_pool``), so the
        process keeps one connection budget; see :class:`SharedPool`."""
        return SharedPool(self)


class SharedPool:
    """The pool of a :class:`PostgresDatabase`, lent to Axentra (``axentra.data.Database.on_pool``).

    Connections are the database's own (``autocommit``, tuple rows, its ``search_path`` and time limits); Axentra
    runs each of its transactions as an explicit block on one of them and sets only transaction-local settings, so a
    connection comes back with nothing of Axentra's. Lending never opens, resizes or closes the pool: the
    :class:`PostgresDatabase` does. Nodestro's own transactions are not affected (the process lock serializes only
    them, as before)."""

    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    @property
    def conninfo(self) -> str:
        return self._database._conninfo

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[Any]:
        if self._database.closed:
            raise RuntimeError("The database is closed")
        with self._database._connection(timeout=timeout) as connection:
            yield connection

    def get_stats(self) -> dict[str, int]:
        return dict(self._database._pool.get_stats())

    @property
    def max_size(self) -> int:
        return int(self._database._pool.max_size)


class _Rows:
    """The result of a statement a :class:`DatabaseSource` ran on its own: its rows, or how many rows it changed."""

    def __init__(self, rows: builtins.list[tuple[Any, ...]], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> builtins.list[tuple[Any, ...]]:
        return builtins.list(self._rows)


class _Reads:
    """``read`` mode outside a transaction: each statement is a read of :class:`PostgresDatabase` (a pooled
    connection, no lock, repeated on another connection when its connection was lost)."""

    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    def execute(self, query: Any, params: Any = None) -> _Rows:
        rows = self._database._query(query, () if params is None else params)
        return _Rows(rows, len(rows))


class _Writes:
    """``write`` mode outside a transaction: the statement is a write of :class:`PostgresDatabase` (autocommit,
    serialized with the process's transactions, never repeated)."""

    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    def execute(self, query: Any, params: Any = None) -> _Rows:
        return _Rows([], self._database._write(query, () if params is None else params))


class DatabaseSource:
    """A :class:`PostgresDatabase` as the ``ConnectionSource`` of Orkexa's PostgreSQL backends
    (:mod:`orkexa.backends.postgres.connection`; MUP-ONA B4, B6).

    The modes run as the database ran the same statements before: ``read`` as :meth:`PostgresDatabase._query` (no
    lock, retried on a lost connection), ``write`` as :meth:`PostgresDatabase._write` (autocommit under the process
    lock), ``transaction`` as :meth:`PostgresDatabase.transaction` (``BEGIN``/``COMMIT`` under the process lock). Inside
    a transaction of the calling thread every mode joins it, so Orkexa's stores and the document store of the same
    database share one transaction.

    ``tenant_setting`` is the hook for row-level security on Nodestro's tables (MUP-ONA A9, not used yet): with a name
    such as ``"app.tenant_id"`` every transaction for a tenant starts with ``set_config(<name>, <tenant>, true)``,
    and ``read`` and ``write`` open a transaction too, because the setting is transaction-local. ``None`` (default):
    nothing is set."""

    def __init__(self, database: PostgresDatabase, *, tenant_setting: str | None = None) -> None:
        if tenant_setting is not None and not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*", tenant_setting):
            raise ValueError(f"Invalid setting name {tenant_setting!r}")
        self.database = database
        self.tenant_setting = tenant_setting

    @property
    def lock(self) -> threading.RLock:
        """The lock that serializes the database's transactions and writes in this process; Orkexa's stores hold it
        for every command (their ``lock``), so a command never waits for it while holding a lock of its own."""
        return self.database._lock

    @contextmanager
    def transaction(self, mode: str = "transaction", *, tenant_id: str | None = None) -> Iterator[Any]:
        if mode not in ("read", "write", "transaction"):
            raise ValueError(f"Unknown transaction mode {mode!r}")
        setting = self.tenant_setting if tenant_id is not None else None
        if mode == "transaction" or setting is not None:
            with self.database._transaction() as connection:
                if setting is not None:
                    connection.execute("SELECT set_config(%s, %s, true)", (setting, tenant_id))
                yield connection
        elif mode == "read":
            yield _Reads(self.database)
        else:
            yield _Writes(self.database)


def grant_runtime_role(owner: PostgresDatabase, role: str) -> dict[str, Any]:
    """Give the runtime role ``role`` data access (no DDL) to every table of the schema, now and for the tables the
    owner creates later (default privileges): ``USAGE`` on the schema, ``SELECT, INSERT, UPDATE, DELETE`` on tables,
    ``USAGE, SELECT`` on sequences, ``EXECUTE`` on functions, and ``CONNECT`` on the database when the owner may grant
    it. Covers Nodestro's and Axentra's tables alike. Idempotent.

    ``owner`` is connected as the owner role (``NODESTRO_DATABASE_MIGRATION_URL``). Refused (``storage.role_unsafe``,
    nothing granted) when ``role`` is the owner itself, a superuser, has ``BYPASSRLS`` or owns tables of the schema:
    row-level security would not apply to it. Returns what applies to the role afterwards."""
    report = inspect_role(owner, role)
    problems = [
        reason
        for reason, flag in (
            ("is the owner role itself", role == report["owner"]),
            ("is a superuser", report["superuser"]),
            ("has BYPASSRLS", report["bypass_rls"]),
            (f"owns tables ({', '.join(report['owned_tables'][:5])})", bool(report["owned_tables"])),
        )
        if flag
    ]
    if problems:
        raise NodestroError(
            "storage.role_unsafe",
            f"The runtime role {role!r} {' and '.join(problems)}; row-level security would not apply to it",
            role=role,
        )
    target = sql.Identifier(role)
    schema = sql.Identifier(report["schema"])
    with owner._transaction() as connection:
        for statement in (
            "GRANT USAGE ON SCHEMA {schema} TO {role}",
            "GRANT " + RUNTIME_TABLE_PRIVILEGES + " ON ALL TABLES IN SCHEMA {schema} TO {role}",
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {role}",
            "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {schema} TO {role}",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT " + RUNTIME_TABLE_PRIVILEGES + " ON TABLES TO {role}",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT USAGE, SELECT ON SEQUENCES TO {role}",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT EXECUTE ON FUNCTIONS TO {role}",
        ):
            connection.execute(sql.SQL(statement).format(schema=schema, role=target))
        # The schema versions are the migration runners' (owner role): the runtime role reads them for the schema
        # gate and cannot fake them.
        for table in SCHEMA_VERSION_TABLES:
            if connection.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()[0]:
                connection.execute(
                    sql.SQL("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {} FROM {}").format(
                        sql.Identifier(table), target
                    )
                )
    if not report["connect"]:
        try:
            with owner._transaction() as connection:
                connection.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(report["database"]), target)
                )
        except psycopg.errors.InsufficientPrivilege:
            pass  # reported as connect: false; the database's owner grants it
    return inspect_role(owner, role)


def inspect_role(database: PostgresDatabase, role: str) -> dict[str, Any]:
    """How row-level security applies to ``role`` in ``database``'s schema (read-only): whether it is a superuser,
    has ``BYPASSRLS``, which tables it owns, whether it may connect and create objects; plus the connecting role
    (``owner``), the schema and the database. ``storage.role_missing`` when the role does not exist."""
    rows = database._query(
        "SELECT current_user::text, current_schema()::text, current_database()::text, r.rolsuper, r.rolbypassrls"
        " FROM pg_roles r WHERE r.rolname = %s",
        (role,),
    )
    if not rows:
        raise NodestroError("storage.role_missing", f"The database role {role!r} does not exist", role=role)
    owner, schema, dbname, superuser, bypass = rows[0]
    tables = database._query(
        "SELECT c.relname, pg_has_role(%s, c.relowner, 'MEMBER') FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') ORDER BY c.relname",
        (role,),
    )
    privileges = database._query(
        "SELECT has_database_privilege(%s, current_database(), 'CONNECT'),"
        " has_schema_privilege(%s, current_schema(), 'CREATE')",
        (role, role),
    )
    connect, create = privileges[0] if privileges else (False, False)
    owned = [str(name) for name, own in tables if own]
    return {
        "role": role,
        "owner": owner,
        "schema": schema,
        "database": dbname,
        "tables": len(tables),
        "superuser": bool(superuser),
        "bypass_rls": bool(bypass),
        "owned_tables": owned,
        "connect": bool(connect),
        "can_create": bool(create),
        "restricted": not (superuser or bypass or owned),
    }


def pgvector_status(database: PostgresDatabase) -> dict[str, Any]:
    """Whether pgvector ranks the knowledge vectors of ``database``'s schema, and what an operator does if not (RT-048).

    Migration ``0010`` installs pgvector only when it is available at that moment and the migrating role may install
    it; one installed later, or in a schema this database's ``search_path`` does not include, leaves the runtime
    ranking the vectors itself. Read-only; ``state`` is one of:

    - ``active``: the type ``vector`` is usable here; pgvector ranks the vectors.
    - ``other_schema``: the extension is installed in ``extension_schema``, which this connection does not see (a
      server with ``NODESTRO_DATABASE_SCHEMA`` sets its ``search_path`` to that schema only).
    - ``installable``: the server has pgvector (``available_version``) but this database does not use it.
    - ``unavailable``: the PostgreSQL server has no pgvector; it has to be installed on the server first.

    ``command`` is the SQL that fixes it (run by a superuser, or a role that may create the extension: pgvector is
    not a trusted extension), ``None`` when nothing is to be done or the package must be installed first; ``message``
    says it in English for operator output. Processes that already run keep ranking themselves until they restart
    (each knowledge index checks pgvector once, and again after ``PostgresIndex.refresh()``)."""
    rows = database._query(
        "SELECT to_regtype('vector') IS NOT NULL, current_schema()::text,"
        " (SELECT e.extversion::text FROM pg_extension e WHERE e.extname = 'vector'),"
        " (SELECT n.nspname::text FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace"
        "   WHERE e.extname = 'vector'),"
        " (SELECT a.default_version::text FROM pg_available_extensions a WHERE a.name = 'vector')"
    )
    usable, schema, version, extension_schema, available = rows[0] if rows else (False, None, None, None, None)
    target = '"' + str(schema or "public").replace('"', '""') + '"'
    command: str | None = None
    if usable:
        state = "active"
        message = (
            f"pgvector {version} ranks the knowledge vectors" if version else "pgvector ranks the knowledge vectors"
        )
    elif version is not None:
        state = "other_schema"
        command = f"ALTER EXTENSION vector SET SCHEMA {target};"
        message = (
            f"pgvector {version} is installed in schema {extension_schema!r}, which this database's search_path "
            f"({schema!r}) does not include: the runtime ranks the vectors itself. Move it ({command}), unless other "
            "applications of this database use it there, then restart the API and worker processes"
        )
    elif available is not None:
        state = "installable"
        command = f"CREATE EXTENSION vector SCHEMA {target};"
        message = (
            f"pgvector {available} is available on the PostgreSQL server but not installed in this database: the "
            f"runtime ranks the vectors itself. As a superuser run {command} then restart the API and worker processes"
        )
    else:
        state = "unavailable"
        message = (
            "The PostgreSQL server has no pgvector: the runtime ranks the vectors itself (semantic search works, but "
            "reads every candidate vector). Install the pgvector package on the server, then check again"
        )
    return {
        "state": state,
        "version": version,
        "available_version": available,
        "extension_schema": extension_schema,
        "schema": schema,
        "command": command,
        "message": message,
    }


class PostgresStore:
    """The JSON documents of one tenant in PostgreSQL (a :class:`~nodestro_runtime.storage.DocumentStore`).

    ``close`` closes this store only; the shared :class:`PostgresDatabase` stays open for the other tenants unless
    the store owns it (:meth:`connect`)."""

    def __init__(self, database: PostgresDatabase, tenant_id: str, *, owns_database: bool = False) -> None:
        if not TENANT_ID.fullmatch(tenant_id):
            raise ValueError(f"Invalid tenant id {tenant_id!r}")
        self.database = database
        self.tenant_id = tenant_id
        self._owns_database = owns_database
        self._closed = False
        #: Documents listings left out because they do not fit the model (C-10; jsonb always holds valid JSON).
        self.damaged = DamagedDocuments()

    @classmethod
    def connect(cls, conninfo: str, tenant_id: str, *, schema: str | None = None) -> PostgresStore:
        """A store with its own connection, closed with the store (scripts, a single-tenant server)."""
        return cls(PostgresDatabase(conninfo, schema=schema), tenant_id, owns_database=True)

    @property
    def closed(self) -> bool:
        return self._closed or self.database.closed

    def close(self) -> None:
        self._closed = True
        if self._owns_database:
            self.database.close()

    def transaction(self) -> Any:
        return self.database.transaction()

    @property
    def platform_events(self) -> Any:
        """The database's platform event sink (:mod:`~nodestro_runtime.platform_events`), ``None`` when off."""
        return self.database.platform_events

    def _check(self) -> None:
        if self._closed:
            raise RuntimeError("The store is closed")

    def lock(self, collection: str, key: str) -> None:
        """Inside :meth:`transaction`: wait until no other transaction (of any connection or process) holds the lock
        of ``(collection, key)``, then hold it until this transaction ends. For read-modify-write steps, such as
        numbering the next version of a workflow; the document need not exist."""
        self._check()
        if not self.database.in_transaction:
            raise RuntimeError("lock() needs a transaction")
        self.database._query(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (DOCUMENT_LOCK_CLASS, json.dumps([self.tenant_id, collection, key])),
        )

    def put(self, collection: str, key: str, model: BaseModel) -> None:
        self._check()
        self._put(collection, key, model.model_dump_json(), touch=True)

    def put_raw(self, collection: str, key: str, value: Any) -> None:
        self._check()
        self._put(collection, key, json.dumps(value), touch=False)

    def put_raw_if_absent(self, collection: str, key: str, value: Any) -> bool:
        """One ``INSERT ... ON CONFLICT DO NOTHING``: atomic for every connection; ``True`` when it inserted."""
        self._check()
        data, raw = _encode(json.dumps(value))
        inserted = self.database._write(
            "INSERT INTO nodestro_documents (tenant_id, collection, id, data, raw) VALUES (%s, %s, %s, %s::jsonb, %s)"
            " ON CONFLICT (tenant_id, collection, id) DO NOTHING",
            (self.tenant_id, collection, key, data, raw),
        )
        return inserted == 1

    def replace_if(
        self, collection: str, key: str, model: BaseModel, *, field: str, expected: str, live_until: str | None = None
    ) -> bool:
        """One conditional ``UPDATE``: atomic for every connection; ``live_until`` uses the database's clock."""
        self._check()
        data, raw = _encode(model.model_dump_json())
        query = (
            "UPDATE nodestro_documents SET data = %s::jsonb, raw = %s, updated_at = clock_timestamp()"
            " WHERE tenant_id = %s AND collection = %s AND id = %s AND data->>%s = %s"
        )
        params: tuple[Any, ...] = (data, raw, self.tenant_id, collection, key, field, expected)
        if live_until is not None:
            query += " AND (data->>%s)::timestamptz > clock_timestamp()"
            params += (live_until,)
        try:
            return self.database._write(query, params) == 1
        except psycopg.DataError:  # the time is missing or unreadable: not live
            return False

    def _put(self, collection: str, key: str, text: str, *, touch: bool) -> None:
        data, raw = _encode(text)
        self.database._write(
            "INSERT INTO nodestro_documents (tenant_id, collection, id, data, raw) VALUES (%s, %s, %s, %s::jsonb, %s)"
            " ON CONFLICT (tenant_id, collection, id) DO UPDATE SET data = excluded.data, raw = excluded.raw"
            + (", updated_at = clock_timestamp()" if touch else ""),
            (self.tenant_id, collection, key, data, raw),
        )

    def _one(self, collection: str, key: str) -> str | None:
        self._check()
        rows = self.database._query(
            "SELECT " + _JSON + " FROM nodestro_documents WHERE tenant_id = %s AND collection = %s AND id = %s",
            (self.tenant_id, collection, key),
        )
        return str(rows[0][0]) if rows else None

    def get_raw(self, collection: str, key: str) -> Any | None:
        raw = self._one(collection, key)
        return None if raw is None else json.loads(raw)

    def get(self, collection: str, key: str, model: type[ModelT]) -> ModelT | None:
        raw = self._one(collection, key)
        return None if raw is None else model.model_validate_json(raw)

    def list(self, collection: str, model: type[ModelT], *, limit: int | None = None) -> builtins.list[ModelT]:
        """Documents of a collection, most recently updated first."""
        self._check()
        query = (
            "SELECT id, " + _JSON + " FROM nodestro_documents WHERE tenant_id = %s AND collection = %s"
            " ORDER BY updated_at DESC, id"
        )
        params: tuple[Any, ...] = (self.tenant_id, collection)
        if limit is not None:
            query += " LIMIT %s"
            params = (*params, limit)
        rows = self.database._query(query, params)
        return [item for _key, item in self.damaged.parse(collection, rows, model)]

    def list_page(
        self,
        collection: str,
        model: type[ModelT],
        *,
        limit: int,
        before: tuple[datetime, str] | None = None,
        top_level: bool = False,
        summary: bool = False,
        since: datetime | None = None,
        equals: Mapping[str, str] | None = None,
    ) -> tuple[builtins.list[ModelT], bool]:
        """One keyset page of the tenant's documents by creation, newest first (see
        :class:`~nodestro_runtime.storage.DocumentStore`); indexed for ``runs`` (migration 0009), so a page costs
        ``limit + 1`` index entries, not the whole collection. Returns the page and whether more documents follow."""
        if collection not in PAGED_COLLECTIONS:
            raise ValueError(f"list_page has no index for the collection {collection!r}")
        if limit < 0:
            raise ValueError("limit must not be negative")
        self._check()
        # The collection is a literal, so the partial indexes of migration 0009 apply in every plan (also in a
        # generic plan of a prepared statement, which knows no parameter values).
        where = "tenant_id = %s AND collection = 'runs' AND " + _CREATED_ORDER + " IS NOT NULL"
        params: builtins.list[Any] = [self.tenant_id]
        if top_level:
            where += " AND (data ->> 'parent_run_id') IS NULL"
        if since is not None:
            where += " AND " + _CREATED_ORDER + " >= %s::timestamptz"
            params.append(cursor_time(since))
        if equals:
            if not all(_FIELD.fullmatch(name) for name in equals):
                raise ValueError(f"Invalid field names {sorted(equals)!r}")
            # Containment of {"field": "text"} holds exactly for a top-level string field with that value.
            where += " AND data @> %s::jsonb"
            params.append(json.dumps({name: str(value) for name, value in equals.items()}))
        if before is not None:
            moment, after = before
            where += " AND (" + _CREATED_ORDER + ", id) < (%s::timestamptz, %s)"
            params += [cursor_time(moment), after]
        data = _JSON
        if summary:
            data = (
                "CASE WHEN raw IS NULL THEN (SELECT jsonb_object_agg(entry.key, entry.value)"
                " FROM jsonb_each(data) AS entry WHERE entry.key = ANY(%s))::text ELSE raw END"
            )
            params = [builtins.list(model.model_fields), *params]
        rows = self.database._query(
            f"SELECT id, {data} FROM nodestro_documents WHERE {where} ORDER BY {_CREATED_ORDER} DESC, id DESC LIMIT %s",
            (*params, limit + 1),
        )
        more = len(rows) > limit
        return [item for _key, item in self.damaged.parse(collection, rows[:limit], model)], more

    def list_since(self, collection: str, model: type[ModelT], *, field: str, since: datetime) -> builtins.list[ModelT]:
        """Documents whose top-level time ``field`` is at or after ``since``, newest first (no index)."""
        if not _FIELD.fullmatch(field):
            raise ValueError(f"Invalid field name {field!r}")
        self._check()
        rows = self.database._query(
            "SELECT id, " + _JSON + " FROM nodestro_documents WHERE tenant_id = %s AND collection = %s"
            " AND nodestro_created_order(data ->> %s) >= %s::timestamptz"
            " ORDER BY nodestro_created_order(data ->> %s) DESC, id DESC",
            (self.tenant_id, collection, field, cursor_time(since), field),
        )
        return [item for _key, item in self.damaged.parse(collection, rows, model)]

    def find(self, collection: str, field: str, values: Iterable[str], model: type[ModelT]) -> builtins.list[ModelT]:
        """Documents whose top-level ``field`` has one of ``values``, in update order."""
        if not _FIELD.fullmatch(field):
            raise ValueError(f"Invalid field name {field!r}")
        self._check()
        wanted = builtins.list(dict.fromkeys(values))
        if not wanted:
            return []
        # The field is a literal (checked above), so the expression indexes of migrations 0001 and 0006 apply.
        rows = self.database._query(
            "SELECT id, " + _JSON + " FROM nodestro_documents WHERE tenant_id = %s AND collection = %s"
            f" AND jsonb_typeof(data -> '{field}') = 'string' AND data ->> '{field}' = ANY(%s) ORDER BY updated_at, id",
            (self.tenant_id, collection, wanted),
        )
        return [item for _key, item in self.damaged.parse(collection, rows, model)]

    def items(self, collection: str, model: type[ModelT]) -> builtins.list[tuple[str, ModelT]]:
        """``(id, document)`` pairs of a collection, in id order."""
        self._check()
        rows = self.database._query(
            "SELECT id, " + _JSON + " FROM nodestro_documents WHERE tenant_id = %s AND collection = %s ORDER BY id",
            (self.tenant_id, collection),
        )
        return self.damaged.parse(collection, rows, model)

    def list_prefix(self, collection: str, prefix: str, model: type[ModelT]) -> builtins.list[ModelT]:
        """Documents whose id starts with ``prefix`` (literally), in id order."""
        self._check()
        rows = self.database._query(
            "SELECT id, " + _JSON + " FROM nodestro_documents WHERE tenant_id = %s AND collection = %s"
            " AND starts_with(id, %s) ORDER BY id",
            (self.tenant_id, collection, prefix),
        )
        return [item for _key, item in self.damaged.parse(collection, rows, model)]

    def delete_prefix(self, collection: str, prefix: str) -> int:
        self._check()
        return self.database._write(
            "DELETE FROM nodestro_documents WHERE tenant_id = %s AND collection = %s AND starts_with(id, %s)",
            (self.tenant_id, collection, prefix),
        )

    def delete(self, collection: str, key: str) -> bool:
        self._check()
        return (
            self.database._write(
                "DELETE FROM nodestro_documents WHERE tenant_id = %s AND collection = %s AND id = %s",
                (self.tenant_id, collection, key),
            )
            > 0
        )

    def search_raw(self, collections: Iterable[str], text: str) -> builtins.list[tuple[str, str]]:
        """``(collection, JSON text)`` of the documents whose JSON contains ``text`` (ASCII letters without case)."""
        self._check()
        wanted = builtins.list(dict.fromkeys(collections))
        if not wanted:
            return []
        rows = self.database._query(
            f"SELECT collection, {_JSON} FROM nodestro_documents WHERE tenant_id = %s AND collection = ANY(%s)"
            f' AND strpos(lower({_JSON} COLLATE "C"), lower(%s COLLATE "C")) > 0',
            (self.tenant_id, wanted, text),
        )
        return [(str(row[0]), str(row[1])) for row in rows]


__all__ = [
    "DEFAULT_IDLE_IN_TRANSACTION_TIMEOUT",
    "DEFAULT_POOL_SIZE",
    "DEFAULT_STATEMENT_TIMEOUT",
    "DOCUMENT_LOCK_CLASS",
    "MIGRATION_LOCK",
    "POSTGRES_SCHEMA_VERSION",
    "TENANT_ID",
    "DatabaseSource",
    "PostgresDatabase",
    "PostgresStep",
    "PostgresStore",
    "SharedPool",
    "grant_runtime_role",
    "inspect_role",
    "migration_checksum",
    "postgres_migrations",
]
