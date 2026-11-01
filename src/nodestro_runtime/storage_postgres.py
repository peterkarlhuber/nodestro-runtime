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
``nodestro_schema_migrations``. A database written by a newer release is refused (``storage.schema_too_new``). Before
a database that already has a schema is migrated, ``before_migration`` must write a backup (operators: ``pg_dump``);
a fresh database needs none.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from importlib import resources
import json
import os
import re
import threading
import time
from typing import Any
import weakref

from pydantic import BaseModel

from .problems import NodestroError
from .storage import BeforeMigration, DamagedDocuments, ModelT

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


class PostgresDatabase:
    """A pool of PostgreSQL connections shared by the stores of every tenant, with its schema migrations.

    ``conninfo`` is a libpq connection string or URL (``postgresql://user@host/db``); credentials come from it or
    from the usual ``PG*`` variables and ``~/.pgpass``, never from Nodestro's settings. ``schema`` puts every table
    into that schema (created by :meth:`migrate` when missing); without it the server's ``search_path`` applies.

    The pool holds at most ``pool_size`` connections (default ``NODESTRO_DATABASE_POOL_SIZE``, else 8). A
    connection that sat idle in the pool for more than :data:`CHECK_AFTER_IDLE` seconds is checked before it is
    handed out, so a connection the server dropped (a restart, a failover, an idle timeout of a proxy) is replaced
    instead of failing the next call. A read outside a transaction that meets a lost connection anyway is retried on
    another one; a write is not repeated (it may have been applied) and fails. The pool discards lost connections.
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
        """A pooled connection; notes when it goes back (no ``reset`` callback: that returns through a worker)."""
        with self._pool.connection(timeout=timeout) as connection:
            try:
                yield connection
            finally:
                self._returned[connection] = time.monotonic()

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

    def _query(self, query: str, params: tuple[Any, ...] = ()) -> builtins.list[tuple[Any, ...]]:
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

    def _write(self, query: str, params: tuple[Any, ...]) -> int:
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
                if self.schema is not None:
                    db.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
                db.execute(
                    "CREATE TABLE IF NOT EXISTS nodestro_schema_migrations ("
                    " version integer PRIMARY KEY, name text NOT NULL,"
                    " applied_at timestamptz NOT NULL DEFAULT now())"
                )
                current = self.schema_version
                if current == target:
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
                                "INSERT INTO nodestro_schema_migrations (version, name) VALUES (%s, %s)", (step, name)
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

    def require_current_schema(self) -> None:
        """Refuse to serve a database that is not at :data:`POSTGRES_SCHEMA_VERSION` (``storage.schema_mismatch``)."""
        found = self.schema_version
        if found != POSTGRES_SCHEMA_VERSION:
            raise NodestroError(
                "storage.schema_mismatch",
                f"The PostgreSQL database has schema version {found}; this release needs {POSTGRES_SCHEMA_VERSION}. "
                "Run the migration first",
                found=found,
                supported=POSTGRES_SCHEMA_VERSION,
            )


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
    "PostgresDatabase",
    "PostgresStep",
    "PostgresStore",
    "postgres_migrations",
]
