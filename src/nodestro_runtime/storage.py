"""Document storage: the :class:`DocumentStore` contract and its SQLite implementation.

Workflows, agent templates, runs and settings are stored as Pydantic models, one JSON document per
(collection, id). Every component depends on :class:`DocumentStore` only; :class:`SQLiteStore` (``Store``
for existing imports) keeps them in the table ``nodestro_documents`` of the data directory's database. The
library's ``SQLiteMemory``, ``SQLiteFileStore`` and ``SQLiteIndex`` use their own tables in the same database
file; the SQL that Nodestro runs on those tables is in :mod:`nodestro_runtime.sqlite_tables`.

Schema versions (SQLite): the database records its version in ``PRAGMA user_version``. Earlier releases used
version 0; version 1 adds the indexes of the list and find queries (:data:`MIGRATIONS`). :meth:`SQLiteStore.migrate`
writes a backup first (the ``before_migration`` hook; a new, empty database needs none), runs every step in one
transaction, and refuses a database written by a newer release (``storage.schema_too_new``) instead of misreading
it.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from .problems import NodestroError

ModelT = TypeVar("ModelT", bound=BaseModel)
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: The schema version this release reads and writes (``PRAGMA user_version``; earlier releases wrote 0).
SCHEMA_VERSION = 1
#: One migration step: changes the schema from version ``n - 1`` to ``n`` on the given connection, inside the
#: transaction :meth:`SQLiteStore.migrate` opened (it must not commit).
Migration = Callable[[sqlite3.Connection], None]
#: The fields :meth:`SQLiteStore.find` looks up on hot paths (runs by status at start-up, runs of a publication).
INDEXED_FIELDS = ("status", "trigger_ref")


def _list_and_find_indexes(db: sqlite3.Connection) -> None:
    """Version 1: ``list`` reads the newest documents from an index instead of sorting the whole collection, and
    ``find`` on :data:`INDEXED_FIELDS` looks values up. Only documents with valid JSON are in the find indexes, so a
    damaged document never stops a write."""
    db.execute(
        "CREATE INDEX IF NOT EXISTS nodestro_documents_updated ON nodestro_documents (collection, updated_at DESC, id)"
    )
    for field in INDEXED_FIELDS:
        db.execute(
            f"CREATE INDEX IF NOT EXISTS nodestro_documents_{field} ON nodestro_documents"
            f" (collection, json_extract(data, '$.{field}')) WHERE json_valid(data)"
        )


#: The steps of this release, by the version they lead to.
MIGRATIONS: Mapping[int, Migration] = {1: _list_and_find_indexes}
#: Called with (current version, target version) before a migration changes anything; writes a backup.
BeforeMigration = Callable[[int, int], None]


def _in_future(value: Any) -> bool:
    """Whether an ISO time stored in a document lies after now (a missing or unreadable time does not)."""
    if not isinstance(value, str):
        return False
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        return False
    return moment > datetime.now(timezone.utc)


@runtime_checkable
class DocumentStore(Protocol):
    """JSON documents grouped into collections: the storage every Nodestro component uses.

    Contract (tested for every implementation in ``tests/test_storage_contract.py``):

    - ``put``/``put_raw`` insert or replace; ``put`` also marks the document as updated now, ``put_raw`` keeps the
      update time of an existing document.
    - ``list`` returns the most recently updated first (ties in id order); ``find``, in update order, the
      documents whose top-level field has one of the values; ``items``, ``list_prefix`` in id order.
      ``list_prefix``/``delete_prefix`` treat the prefix literally (``%`` and ``_`` are ordinary characters).
    - ``search_raw`` returns ``(collection, JSON text)`` of the documents whose JSON text contains ``text``
      (ASCII letters compared without case), for scans that look into documents of several collections.
    - Every write is atomic. ``transaction`` groups writes: all of them are stored or none; it is reentrant,
      and other threads wait until it ends.
    - ``replace_if`` replaces a document only while its top-level ``field`` still has the value ``expected`` (and,
      with ``live_until``, while the time in that top-level field lies in the future by the store's clock), as one
      atomic step that is safe across threads, processes and database connections. It returns whether it did.
    - ``lock(collection, key)``, inside ``transaction``, makes every other transaction (of any thread, connection or
      process) that locks the same pair wait until this one ends: for read-modify-write steps such as numbering
      versions. Outside a transaction it raises ``RuntimeError``.
    - ``close`` is idempotent; ``closed`` says whether it was called.
    """

    @property
    def closed(self) -> bool: ...

    def close(self) -> None: ...

    def transaction(self) -> AbstractContextManager[None]: ...

    def lock(self, collection: str, key: str) -> None: ...

    def put(self, collection: str, key: str, model: BaseModel) -> None: ...

    def put_raw(self, collection: str, key: str, value: Any) -> None: ...

    def replace_if(
        self, collection: str, key: str, model: BaseModel, *, field: str, expected: str, live_until: str | None = None
    ) -> bool: ...

    def get_raw(self, collection: str, key: str) -> Any | None: ...

    def get(self, collection: str, key: str, model: type[ModelT]) -> ModelT | None: ...

    def list(self, collection: str, model: type[ModelT], *, limit: int | None = None) -> list[ModelT]: ...

    def find(
        self, collection: str, field: str, values: Iterable[str], model: type[ModelT]
    ) -> builtins.list[ModelT]: ...

    def items(self, collection: str, model: type[ModelT]) -> builtins.list[tuple[str, ModelT]]: ...

    def list_prefix(self, collection: str, prefix: str, model: type[ModelT]) -> builtins.list[ModelT]: ...

    def delete_prefix(self, collection: str, prefix: str) -> int: ...

    def delete(self, collection: str, key: str) -> bool: ...

    def search_raw(self, collections: Iterable[str], text: str) -> builtins.list[tuple[str, str]]: ...


def _like(prefix: str) -> str:
    """A LIKE pattern (escape character ``\\``) for ids starting with ``prefix``."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class SQLiteStore:
    """Thread-safe JSON documents in SQLite, grouped into collections (a :class:`DocumentStore`).

    One connection serves every thread; an ``RLock`` serializes its use. Each write is its own
    ``BEGIN IMMEDIATE`` transaction, and :meth:`transaction` groups several writes into one, so
    related documents (a workflow and its version) are stored together or not at all. The database is in
    WAL mode (except ``:memory:``), so other connections (Orkexa's tables, backups) can read while it writes.
    """

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        self._lock = threading.RLock()
        #: Nesting depth of :meth:`transaction` (only the thread holding ``_lock`` changes it).
        self._depth = 0
        self._closed = False
        self._connection = sqlite3.connect(self.database, isolation_level=None, check_same_thread=False, timeout=30)
        if self.database != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS nodestro_documents ("
            " collection TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,"
            " updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
            " PRIMARY KEY (collection, id))"
        )

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
        """Group writes into one transaction: all of them are stored, or none (also after a crash).

        Reentrant: a write inside the block joins it, and a nested block joins the outer one.
        Other threads wait until the block ends.
        """
        with self._transaction():
            yield

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self._connection
                finally:
                    self._depth -= 1
                return
            self._connection.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self._connection
            except BaseException:
                self._depth = 0
                self._connection.execute("ROLLBACK")
                raise
            self._depth = 0
            self._connection.execute("COMMIT")

    def lock(self, collection: str, key: str) -> None:
        """Nothing more to do inside :meth:`transaction`: ``BEGIN IMMEDIATE`` already holds the database's write lock
        for every connection and process, so no other transaction can run until this one ends."""
        with self._lock:
            if not self._depth:
                raise RuntimeError("lock() needs a transaction")

    def put(self, collection: str, key: str, model: BaseModel) -> None:
        data = model.model_dump_json()
        with self._transaction() as db:
            db.execute(
                "INSERT INTO nodestro_documents (collection, id, data) VALUES (?, ?, ?)"
                " ON CONFLICT (collection, id) DO UPDATE SET data = excluded.data,"
                " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (collection, key, data),
            )

    def replace_if(
        self, collection: str, key: str, model: BaseModel, *, field: str, expected: str, live_until: str | None = None
    ) -> bool:
        data = model.model_dump_json()
        # BEGIN IMMEDIATE holds the database's write lock, so the check and the write are one step for every
        # connection and process that uses this file.
        with self._transaction() as db:
            row = db.execute(
                "SELECT data FROM nodestro_documents WHERE collection = ? AND id = ?", (collection, key)
            ).fetchone()
            if row is None:
                return False
            current = json.loads(row[0])
            if not isinstance(current, dict) or current.get(field) != expected:
                return False
            if live_until is not None and not _in_future(current.get(live_until)):
                return False
            db.execute(
                "UPDATE nodestro_documents SET data = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                " WHERE collection = ? AND id = ?",
                (data, collection, key),
            )
            return True

    def put_raw(self, collection: str, key: str, value: Any) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO nodestro_documents (collection, id, data) VALUES (?, ?, ?)"
                " ON CONFLICT (collection, id) DO UPDATE SET data = excluded.data",
                (collection, key, json.dumps(value)),
            )

    def get_raw(self, collection: str, key: str) -> Any | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT data FROM nodestro_documents WHERE collection = ? AND id = ?", (collection, key)
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def get(self, collection: str, key: str, model: type[ModelT]) -> ModelT | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT data FROM nodestro_documents WHERE collection = ? AND id = ?", (collection, key)
            ).fetchone()
        return None if row is None else model.model_validate_json(row[0])

    def list(self, collection: str, model: type[ModelT], *, limit: int | None = None) -> list[ModelT]:
        """Documents of a collection, most recently updated first."""
        query = "SELECT data FROM nodestro_documents WHERE collection = ? ORDER BY updated_at DESC, id"
        params: tuple[Any, ...] = (collection,)
        if limit is not None:
            query += " LIMIT ?"
            params = (collection, limit)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [model.model_validate_json(row[0]) for row in rows]

    def find(self, collection: str, field: str, values: Iterable[str], model: type[ModelT]) -> builtins.list[ModelT]:
        """Documents whose top-level ``field`` has one of ``values``, however many the collection holds."""
        if not _FIELD.fullmatch(field):
            raise ValueError(f"Invalid field name {field!r}")
        wanted = list(dict.fromkeys(values))
        if not wanted:
            return []
        marks = ", ".join("?" for _ in wanted)
        # ``json_valid(data)`` lets the partial indexes of :data:`INDEXED_FIELDS` apply (a damaged document has no
        # field to match anyway); ``+updated_at`` keeps SQLite from preferring the list index for the order and
        # scanning the whole collection instead of looking the values up.
        order = "+updated_at, id" if field in INDEXED_FIELDS else "updated_at, id"
        with self._lock:
            rows = self._connection.execute(
                f"SELECT data FROM nodestro_documents WHERE collection = ? AND json_valid(data)"
                f" AND json_extract(data, '$.{field}') IN ({marks}) ORDER BY {order}",
                (collection, *wanted),
            ).fetchall()
        return [model.model_validate_json(row[0]) for row in rows]

    def items(self, collection: str, model: type[ModelT]) -> builtins.list[tuple[str, ModelT]]:
        """``(id, document)`` pairs of a collection, in id order."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, data FROM nodestro_documents WHERE collection = ? ORDER BY id", (collection,)
            ).fetchall()
        return [(str(row[0]), model.model_validate_json(row[1])) for row in rows]

    def list_prefix(self, collection: str, prefix: str, model: type[ModelT]) -> builtins.list[ModelT]:
        """Documents whose id starts with ``prefix``, in id order."""
        pattern = _like(prefix)
        with self._lock:
            rows = self._connection.execute(
                "SELECT data FROM nodestro_documents WHERE collection = ? AND id LIKE ? ESCAPE '\\' ORDER BY id",
                (collection, pattern),
            ).fetchall()
        return [model.model_validate_json(row[0]) for row in rows]

    def delete_prefix(self, collection: str, prefix: str) -> int:
        pattern = _like(prefix)
        with self._transaction() as db:
            cursor = db.execute(
                "DELETE FROM nodestro_documents WHERE collection = ? AND id LIKE ? ESCAPE '\\'", (collection, pattern)
            )
        return cursor.rowcount

    def delete(self, collection: str, key: str) -> bool:
        with self._transaction() as db:
            cursor = db.execute("DELETE FROM nodestro_documents WHERE collection = ? AND id = ?", (collection, key))
        return cursor.rowcount > 0

    def search_raw(self, collections: Iterable[str], text: str) -> builtins.list[tuple[str, str]]:
        """``(collection, JSON text)`` of the documents in ``collections`` whose JSON contains ``text``."""
        wanted = builtins.list(dict.fromkeys(collections))
        if not wanted:
            return []
        marks = ", ".join("?" for _ in wanted)
        pattern = "%" + _like(text)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT collection, data FROM nodestro_documents WHERE collection IN ({marks})"
                " AND data LIKE ? ESCAPE '\\'",
                (*wanted, pattern),
            ).fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]

    # ---- SQLite only: schema version, migrations, maintenance ---------------------------

    @property
    def schema_version(self) -> int:
        """The schema version recorded in the database (``PRAGMA user_version``)."""
        with self._lock:
            return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    def migrate(
        self,
        *,
        before_migration: BeforeMigration | None,
        migrations: Mapping[int, Migration] | None = None,
        target: int | None = None,
    ) -> int:
        """Bring the database to schema version ``target`` (default :data:`SCHEMA_VERSION`, with :data:`MIGRATIONS`);
        returns the version it had.

        Nothing is written when the database already has ``target``. A database of a newer release is refused
        (``storage.schema_too_new``) before anything is written. Otherwise ``before_migration(current, target)``
        writes a backup first (not for a new, empty database; a migration without one is refused with
        ``storage.backup_required``; a failing backup stops it with ``storage.backup_failed``), and every step
        runs in one transaction together with the new version number: a failing step
        (``storage.migration_failed``) leaves the database exactly as it was.
        """
        migrations = MIGRATIONS if migrations is None else migrations
        target = SCHEMA_VERSION if target is None else target
        with self._lock:
            if self._depth:
                raise RuntimeError("migrate() cannot run inside a transaction")
            current = self.schema_version
            if current == target:
                return current
            if current > target:
                raise NodestroError(
                    "storage.schema_too_new",
                    f"The database {self.database} has schema version {current}; this Nodestro release reads "
                    f"version {target}. Use the newer release, or restore a backup made with this one",
                    found=current,
                    supported=target,
                )
            missing = [version for version in range(current + 1, target + 1) if version not in migrations]
            if missing:
                raise NodestroError(
                    "storage.migration_missing",
                    f"No migration to schema version {missing[0]}",
                    found=current,
                    supported=target,
                )
            fresh = self._is_empty()
            if before_migration is None and not fresh:
                raise NodestroError(
                    "storage.backup_required",
                    "A schema migration needs a backup first; none was configured",
                    found=current,
                    supported=target,
                )
            try:
                if not fresh and before_migration is not None:
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
                with self._transaction() as db:
                    for step in range(current + 1, target + 1):
                        migrations[step](db)
                    db.execute(f"PRAGMA user_version = {int(target)}")
            except Exception as exc:
                raise NodestroError(
                    "storage.migration_failed",
                    f"The schema migration to version {step} failed; the database is unchanged: {exc}",
                    found=current,
                    supported=target,
                    step=step,
                ) from exc
            return current

    def _is_empty(self) -> bool:
        """Whether the database holds nothing yet (only this store's empty table): migrating it needs no backup."""
        tables = self._connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name <> 'nodestro_documents'"
            " AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        if tables:
            return False
        return self._connection.execute("SELECT 1 FROM nodestro_documents LIMIT 1").fetchone() is None

    def snapshot(self, target: str | Path) -> None:
        """Copy the whole database, consistently, to the SQLite file ``target`` (an online backup).

        Writes of this store wait until the copy is complete; other connections may keep reading."""
        destination = sqlite3.connect(str(target))
        try:
            with self._lock:
                self._connection.backup(destination)
        finally:
            destination.close()

    @contextmanager
    def maintenance(self) -> Iterator[sqlite3.Connection]:
        """The connection itself, with every other use of this store waiting until the block ends.

        For SQLite maintenance that has no document operation (restoring a backup into the open database);
        the caller manages transactions. Not available inside :meth:`transaction`."""
        with self._lock:
            if self._depth:
                raise RuntimeError("maintenance() cannot run inside a transaction")
            yield self._connection


#: The name of :class:`SQLiteStore` in Nodestro 0.1 and 0.2 (imports and type hints keep working).
Store = SQLiteStore

__all__ = [
    "INDEXED_FIELDS",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "BeforeMigration",
    "DocumentStore",
    "Migration",
    "ModelT",
    "SQLiteStore",
    "Store",
]
