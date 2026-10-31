"""The :class:`~nodestro_runtime.storage.DocumentStore` on PostgreSQL, for the server profile (ADR-0002, ADR-0003).

:class:`PostgresDatabase` owns one connection to a PostgreSQL database (optionally a schema in it) and its schema
migrations; :meth:`PostgresDatabase.store` gives the documents of one tenant as a :class:`PostgresStore`. Every row
carries ``tenant_id`` and every query filters on it, so one tenant's store never reads or changes another tenant's
documents.

Needs the optional dependency ``nodestro-runtime[postgres]`` (psycopg 3). The desktop never imports this module.

Semantics follow the SQLite store exactly (``tests/test_storage_contract.py`` runs the same cases on both): one
connection serves every thread of a process, an ``RLock`` serializes its use, every write is atomic, and
:meth:`PostgresDatabase.transaction` groups writes (reentrant, all-or-nothing, other threads wait). Several
processes may use the same database; PostgreSQL's row locks keep their writes consistent.

Migrations (forward only) are the numbered SQL files in ``nodestro_runtime/migrations/postgres``. They run under a
PostgreSQL advisory lock, so several processes starting at once apply each step exactly once, and are recorded in
``nodestro_schema_migrations``. A database written by a newer release is refused (``storage.schema_too_new``). Before
a database that already has a schema is migrated, ``before_migration`` must write a backup (operators: ``pg_dump``);
a fresh database needs none.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from importlib import resources
import json
import re
import threading
from typing import Any

from pydantic import BaseModel

from .problems import NodestroError
from .storage import BeforeMigration, ModelT

try:  # optional: nodestro-runtime[postgres]
    import psycopg
    from psycopg import sql
except ImportError:  # pragma: no cover - exercised only without the extra
    psycopg = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]

_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: Tenant ids: lower-case letters, digits, ``-`` and ``_``; at most 63 characters. The desktop's tenant is ``local``.
TENANT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_SCHEMA = re.compile(r"[a-z_][a-z0-9_]{0,62}")
#: The advisory lock key of the migration runner (any constant; ``nodestro`` in ASCII).
MIGRATION_LOCK = 0x6E6F646573747230
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
    """One PostgreSQL connection shared by the stores of every tenant, with its schema migrations.

    ``conninfo`` is a libpq connection string or URL (``postgresql://user@host/db``); credentials come from it or
    from the usual ``PG*`` variables and ``~/.pgpass``, never from Nodestro's settings. ``schema`` puts every table
    into that schema (created by :meth:`migrate` when missing); without it the server's ``search_path`` applies.
    """

    def __init__(self, conninfo: str, *, schema: str | None = None, connect_timeout: int = 10) -> None:
        _require_driver()
        if schema is not None and not _SCHEMA.fullmatch(schema):
            raise ValueError(f"Invalid schema name {schema!r}")
        self.schema = schema
        self._lock = threading.RLock()
        self._depth = 0
        self._closed = False
        self._connection = psycopg.connect(conninfo, autocommit=True, connect_timeout=connect_timeout)
        if schema is not None:
            self._connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))

    # ---- connection and transactions -------------------------------------------------------

    def close(self) -> None:
        """Close the connection; closing twice is harmless."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._connection.close()

    @property
    def closed(self) -> bool:
        return self._closed

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group writes into one transaction: all of them are stored, or none. Reentrant; other threads wait."""
        with self._transaction():
            yield

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self._connection
                finally:
                    self._depth -= 1
                return
            self._connection.execute("BEGIN")
            self._depth = 1
            try:
                yield self._connection
            except BaseException:
                self._depth = 0
                self._connection.execute("ROLLBACK")
                raise
            self._depth = 0
            self._connection.execute("COMMIT")

    def _query(self, query: str, params: tuple[Any, ...] = ()) -> builtins.list[tuple[Any, ...]]:
        with self._lock:
            return self._connection.execute(query, params).fetchall()

    def _write(self, query: str, params: tuple[Any, ...]) -> int:
        with self._transaction() as db:
            return int(db.execute(query, params).rowcount)

    # ---- tenants -----------------------------------------------------------------------------

    def store(self, tenant_id: str) -> PostgresStore:
        """The documents of ``tenant_id`` (validated against :data:`TENANT_ID`)."""
        return PostgresStore(self, tenant_id)

    # ---- schema versions and migrations ----------------------------------------------------------

    @property
    def schema_version(self) -> int:
        """The newest applied migration (0: no Nodestro tables yet)."""
        with self._lock:
            exists = self._connection.execute("SELECT to_regclass('nodestro_schema_migrations') IS NOT NULL").fetchone()
            if not exists or not exists[0]:
                return 0
            row = self._connection.execute(
                "SELECT coalesce(max(version), 0) FROM nodestro_schema_migrations"
            ).fetchone()
        return int(row[0]) if row else 0

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
            if self._depth:
                raise RuntimeError("migrate() cannot run inside a transaction")
            with self._transaction() as db:
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
            "SELECT " + _JSON + " FROM nodestro_documents WHERE tenant_id = %s AND collection = %s"
            " ORDER BY updated_at DESC, id"
        )
        params: tuple[Any, ...] = (self.tenant_id, collection)
        if limit is not None:
            query += " LIMIT %s"
            params = (*params, limit)
        return [model.model_validate_json(row[0]) for row in self.database._query(query, params)]

    def find(self, collection: str, field: str, values: Iterable[str], model: type[ModelT]) -> builtins.list[ModelT]:
        """Documents whose top-level ``field`` has one of ``values``, in update order."""
        if not _FIELD.fullmatch(field):
            raise ValueError(f"Invalid field name {field!r}")
        self._check()
        wanted = builtins.list(dict.fromkeys(values))
        if not wanted:
            return []
        rows = self.database._query(
            "SELECT " + _JSON + " FROM nodestro_documents WHERE tenant_id = %s AND collection = %s"
            " AND jsonb_typeof(data -> %s) = 'string' AND data ->> %s = ANY(%s) ORDER BY updated_at, id",
            (self.tenant_id, collection, field, field, wanted),
        )
        return [model.model_validate_json(row[0]) for row in rows]

    def items(self, collection: str, model: type[ModelT]) -> builtins.list[tuple[str, ModelT]]:
        """``(id, document)`` pairs of a collection, in id order."""
        self._check()
        rows = self.database._query(
            "SELECT id, " + _JSON + " FROM nodestro_documents WHERE tenant_id = %s AND collection = %s ORDER BY id",
            (self.tenant_id, collection),
        )
        return [(str(row[0]), model.model_validate_json(row[1])) for row in rows]

    def list_prefix(self, collection: str, prefix: str, model: type[ModelT]) -> builtins.list[ModelT]:
        """Documents whose id starts with ``prefix`` (literally), in id order."""
        self._check()
        rows = self.database._query(
            "SELECT " + _JSON + " FROM nodestro_documents WHERE tenant_id = %s AND collection = %s"
            " AND starts_with(id, %s) ORDER BY id",
            (self.tenant_id, collection, prefix),
        )
        return [model.model_validate_json(row[0]) for row in rows]

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
    "MIGRATION_LOCK",
    "POSTGRES_SCHEMA_VERSION",
    "TENANT_ID",
    "PostgresDatabase",
    "PostgresStep",
    "PostgresStore",
    "postgres_migrations",
]
