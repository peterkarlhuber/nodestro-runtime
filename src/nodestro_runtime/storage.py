"""Document storage: the :class:`DocumentStore` contract and its SQLite implementation.

Workflows, agent templates, runs and settings are stored as Pydantic models, one JSON document per
(collection, id). Every component depends on :class:`DocumentStore` only; :class:`SQLiteStore` (``Store``
for existing imports) keeps them in the table ``nodestro_documents`` of the data directory's database. The
library's ``SQLiteMemory``, ``SQLiteFileStore`` and ``SQLiteIndex`` use their own tables in the same database
file; the SQL that Nodestro runs on those tables is in :mod:`nodestro_runtime.sqlite_tables`.

Schema versions (SQLite): the database records its version in ``PRAGMA user_version``. Earlier releases used
version 0; version 1 adds the indexes of the list and find queries, version 2 the creation-order indexes of the
keyset run listing :meth:`SQLiteStore.list_page` (:data:`MIGRATIONS`). :meth:`SQLiteStore.migrate`
writes a backup first (the ``before_migration`` hook; a new, empty database needs none), runs every step in one
transaction, and refuses a database written by a newer release (``storage.schema_too_new``) instead of misreading
it.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
import dataclasses
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel
from tabrion import sqlite as tabrion_sqlite
from tabrion.errors import BackupFailed, BackupRequired, MigrationFailed, MigrationMissing, SchemaTooNew, UnsafePath

from .error_texts import failure_text
from .problems import NodestroError

ModelT = TypeVar("ModelT", bound=BaseModel)
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

logger = logging.getLogger(__name__)

#: The schema version this release reads and writes (``PRAGMA user_version``; earlier releases wrote 0).
SCHEMA_VERSION = 2
#: One migration step: changes the schema from version ``n - 1`` to ``n`` on the given connection, inside the
#: transaction :meth:`SQLiteStore.migrate` opened (it must not commit, nor run ``executescript``, which commits).
#: Foreign keys are on (:data:`SQLITE_PROFILE`), and ``PRAGMA foreign_keys = OFF`` has no effect inside a
#: transaction: a step that rebuilds a table (create, copy, drop, rename) must keep every reference valid at each
#: statement, or use ``PRAGMA defer_foreign_keys = ON``, which holds the checks until the commit.
Migration = Callable[[sqlite3.Connection], None]
#: The fields :meth:`SQLiteStore.find` looks up on hot paths (runs by status at start-up, runs of a publication).
INDEXED_FIELDS = ("status", "trigger_ref")


def _list_and_find_indexes(db: sqlite3.Connection) -> None:
    """Version 1: ``list`` reads the newest documents from an index instead of sorting the whole collection, and
    ``find`` on :data:`INDEXED_FIELDS` looks values up. Only documents with valid JSON are in the find indexes, so a
    damaged document never stops a write; the damaged ones have a small index of their own (``find`` reports them)."""
    db.execute(
        "CREATE INDEX IF NOT EXISTS nodestro_documents_updated ON nodestro_documents (collection, updated_at DESC, id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS nodestro_documents_damaged ON nodestro_documents (collection)"
        " WHERE NOT json_valid(data)"
    )
    for field in INDEXED_FIELDS:
        db.execute(
            f"CREATE INDEX IF NOT EXISTS nodestro_documents_{field} ON nodestro_documents"
            f" (collection, json_extract(data, '$.{field}')) WHERE json_valid(data)"
        )


#: The creation order of :meth:`SQLiteStore.list_page`: a document's top-level ``created_at`` as UTC text at
#: millisecond precision (``NULL`` when it is missing or not a time). Indexed by migration 2 for ``runs``; the query
#: must use exactly this expression for the index to apply.
#: Only text that starts with a date is converted: ``strftime`` of the word ``now`` is not deterministic, and SQLite
#: refuses a write whose indexed expression is not.
CREATED_ORDER = (
    "CASE WHEN json_extract(data, '$.created_at') GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"
    " THEN strftime('%Y-%m-%dT%H:%M:%f', json_extract(data, '$.created_at')) END"
)
#: The collections :meth:`DocumentStore.list_page` serves: those with its indexes (SQLite version 2, PostgreSQL
#: migration 0009). Another collection needs indexes of its own first; until then ``list_page`` refuses it.
PAGED_COLLECTIONS = ("runs",)


def _created_order_indexes(db: sqlite3.Connection) -> None:
    """Version 2 (RT2-05): the keyset run listing (:meth:`SQLiteStore.list_page`) reads one page of runs by creation
    from an index, of all runs or of the top-level runs only (sub-workflow and team runs can outnumber them by far),
    instead of reading every run. Partial indexes: only runs with valid JSON (a damaged document never stops a
    write). The SQL is written out (not built from :data:`CREATED_ORDER`) so this step never changes."""
    order = (
        "CASE WHEN json_extract(data, '$.created_at') GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"
        " THEN strftime('%Y-%m-%dT%H:%M:%f', json_extract(data, '$.created_at')) END"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS nodestro_documents_runs_created ON nodestro_documents"
        f" (collection, ({order}) DESC, id DESC) WHERE collection = 'runs' AND json_valid(data)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS nodestro_documents_runs_created_top ON nodestro_documents"
        f" (collection, ({order}) DESC, id DESC)"
        " WHERE collection = 'runs' AND json_valid(data) AND json_extract(data, '$.parent_run_id') IS NULL"
    )


#: The steps of this release, by the version they lead to.
MIGRATIONS: Mapping[int, Migration] = {1: _list_and_find_indexes, 2: _created_order_indexes}


def cursor_time(moment: datetime) -> str:
    """A cursor's time as ISO text for the stores' creation order (a time without zone counts as UTC)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


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
    return moment > datetime.now(UTC)


@runtime_checkable
class DocumentStore(Protocol):
    """JSON documents grouped into collections: the storage every Nodestro component uses.

    Contract (tested for every implementation in ``tests/test_storage_contract.py``):

    - ``put``/``put_raw`` insert or replace; ``put`` also marks the document as updated now, ``put_raw`` keeps the
      update time of an existing document. ``put_raw_if_absent`` inserts only when no document has the key, as one
      atomic step that is safe across threads, processes and database connections (reserving a key once, such as an
      ``Idempotency-Key``), and returns whether it inserted; an existing document is left as it is.
    - ``list_page`` is a keyset page by creation (RT2-05): documents with a readable top-level ``created_at``,
      newest first (ties: higher id first), strictly after the cursor ``before = (created_at, id)`` in that order
      (``id`` ``""``: everything older than the time), with ``top_level`` only those without ``parent_run_id``;
      at most ``limit`` of them and whether more follow. The order compares creation times at the store's
      precision (SQLite: milliseconds, PostgreSQL: microseconds), so a page continues exactly where the cursor of
      the previous one points. ``since`` keeps only documents created at or after that time; ``equals`` only those
      whose top-level fields have these string values (``{"workflow_id": ...}``), both in the query (RT-042).
      ``summary`` reads only the top-level fields ``model`` declares (PostgreSQL returns a
      document it keeps as exact text, one with a NUL character, whole). Only the collections of
      :data:`PAGED_COLLECTIONS` (``runs``) are served; ``list`` stays as it is.
    - ``list_since`` returns the documents of any collection whose top-level time ``field`` is at or after ``since``,
      newest first (ties: higher id first), filtered in the query and not in Python (RT-009; no index: for
      collections such as ``usage_log`` that are read by period).
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

    def put_raw_if_absent(self, collection: str, key: str, value: Any) -> bool: ...

    def replace_if(
        self, collection: str, key: str, model: BaseModel, *, field: str, expected: str, live_until: str | None = None
    ) -> bool: ...

    def get_raw(self, collection: str, key: str) -> Any | None: ...

    def get(self, collection: str, key: str, model: type[ModelT]) -> ModelT | None: ...

    def list(self, collection: str, model: type[ModelT], *, limit: int | None = None) -> list[ModelT]: ...

    def find(
        self, collection: str, field: str, values: Iterable[str], model: type[ModelT]
    ) -> builtins.list[ModelT]: ...

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
    ) -> tuple[builtins.list[ModelT], bool]: ...

    def list_since(
        self, collection: str, model: type[ModelT], *, field: str, since: datetime
    ) -> builtins.list[ModelT]: ...

    def items(self, collection: str, model: type[ModelT]) -> builtins.list[tuple[str, ModelT]]: ...

    def list_prefix(self, collection: str, prefix: str, model: type[ModelT]) -> builtins.list[ModelT]: ...

    def delete_prefix(self, collection: str, prefix: str) -> int: ...

    def delete(self, collection: str, key: str) -> bool: ...

    def search_raw(self, collections: Iterable[str], text: str) -> builtins.list[tuple[str, str]]: ...


class DamagedDocuments:
    """Documents a listing left out because they cannot be read (C-10): JSON that does not parse (a disk fault, a
    manual edit) or that does not fit the model (a newer release's data after a downgrade). One such document
    never stops a whole listing, the start-up recovery or a search; each one is logged once as a warning with its
    collection and id, and :attr:`found` keeps them for diagnostics. Reading it alone (``get``) still raises."""

    def __init__(self) -> None:
        #: ``(collection, id)`` -> why it cannot be read (the first line of the error).
        self.found: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self.found)

    def parse(self, collection: str, rows: Iterable[tuple[Any, Any]], model: type[ModelT]) -> list[tuple[str, ModelT]]:
        """``(id, document)`` of ``rows`` (``(id, JSON text)``) in their order, without the damaged ones."""
        parsed: list[tuple[str, ModelT]] = []
        for key, text in rows:
            try:
                parsed.append((str(key), model.model_validate_json(text)))
            except ValueError as exc:  # json and pydantic errors: the document is left out, never the listing
                self.report(collection, str(key), exc)
        return parsed

    def report(self, collection: str, key: str, error: BaseException) -> None:
        reason = (str(error).strip().splitlines() or [type(error).__name__])[0][:300]
        with self._lock:
            new = (collection, key) not in self.found
            self.found[(collection, key)] = reason
        if new:
            logger.warning("Damaged document %s/%s is left out of listings: %s", collection, key, reason)


#: How the desktop opens its SQLite database (TABRION MIG-NOD-6, ``tabrion.sqlite.connect``): WAL, ``synchronous=FULL``
#: (durability as before), busy timeout 30 s, foreign keys on (``PRAGMA foreign_key_check`` must stay empty),
#: ``trusted_schema`` off, ``secure_delete`` on. Before, ``secure_delete`` was the build's default and never set: on
#: with the SQLite of Linux distributions (Debian, Ubuntu), off with the SQLite of python.org, macOS and Windows and
#: in the desktop bundles; the profile now sets it on everywhere (a change on those platforms: deleted content is
#: overwritten). New files are created ``0600``; an existing file keeps its mode (MIG-NOD-9 tightens it), but must
#: belong to the running user and not be writable by group or others, and no symbolic link is followed
#: (``storage.unsafe_file``, :func:`open_sqlite`).
SQLITE_PROFILE = tabrion_sqlite.SQLiteProfile(
    journal_mode=tabrion_sqlite.JournalMode.WAL,
    synchronous=tabrion_sqlite.Synchronous.FULL,
    foreign_keys=True,
    busy_timeout=30.0,
    secure_delete=True,
)


def _detail(error: Any, name: str, default: int) -> int:
    """A whole-number detail of a Tabrion error (``found``, ``step``, ``missing``), else ``default``."""
    value = error.details.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


#: The import source of a server (``postgres_import``): a copied desktop database may belong to another user or have
#: a looser mode, which does not matter for a read-only copy; symbolic links stay refused.
SHARED_SOURCE_PROFILE = dataclasses.replace(SQLITE_PROFILE, allow_shared_file=True)

#: What a refused file is, and what the user does about it (``storage.unsafe_file``, parameter ``reason``).
_UNSAFE_FILE = {
    "sqlite.symlink": (
        "symlink",
        "is a symbolic link, or one of its -wal, -shm or -journal files is; Nodestro does not follow links. Move the "
        "real file into the data directory in place of the link",
    ),
    "sqlite.unsafe_mode": (
        "writable_by_others",
        "may be changed by other users (group or others have write permission). Remove that permission "
        "(chmod go-w on the file and its -wal and -shm files), then start again",
    ),
    "sqlite.unsafe_owner": (
        "other_owner",
        "belongs to another user. Give it to the user who runs Nodestro (chown), or run Nodestro as its owner",
    ),
}


_UNSAFE_PATH = ("unsafe_path", "is not a regular file this user may open. Check the path and its permissions")


def unsafe_advice(error: UnsafePath) -> tuple[str, str]:
    """``(reason, what the file is and what to do)`` for a file that ``tabrion.sqlite`` refused."""
    return _UNSAFE_FILE.get(error.code, _UNSAFE_PATH)


def unsafe_file(error: UnsafePath, database: str | Path) -> NodestroError:
    """The Nodestro problem for a database file that ``tabrion.sqlite`` refused (``storage.unsafe_file``)."""
    reason, advice = unsafe_advice(error)
    return NodestroError("storage.unsafe_file", f"The database {database} {advice}", reason=reason)


def open_sqlite(
    database: str | Path,
    *,
    read_only: bool = False,
    profile: tabrion_sqlite.SQLiteProfile = SQLITE_PROFILE,
    refusal: Callable[[UnsafePath], Exception] | None = None,
) -> sqlite3.Connection:
    """A connection to the desktop's SQLite file ``database`` under ``profile`` (default :data:`SQLITE_PROFILE`;
    autocommit, usable from every thread; the caller serializes its use). ``read_only`` never creates or changes the
    file. A file ``tabrion.sqlite`` refuses (a symbolic link, another owner, writable by others) raises
    ``storage.unsafe_file`` with what to do about it, or what ``refusal`` makes of the refusal."""
    try:
        return tabrion_sqlite.connect(database, profile=profile, read_only=read_only, check_same_thread=False)
    except UnsafePath as exc:
        raise (refusal(exc) if refusal is not None else unsafe_file(exc, database)) from None


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
        #: Documents that listings left out because they cannot be read (C-10).
        self.damaged = DamagedDocuments()
        self._connection = open_sqlite(self.database)
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
            with tabrion_sqlite.write_transaction(self._connection) as connection:
                self._depth = 1
                try:
                    yield connection
                finally:
                    self._depth = 0

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

    def put_raw_if_absent(self, collection: str, key: str, value: Any) -> bool:
        with self._transaction() as db:
            cursor = db.execute(
                "INSERT INTO nodestro_documents (collection, id, data) VALUES (?, ?, ?)"
                " ON CONFLICT (collection, id) DO NOTHING",
                (collection, key, json.dumps(value)),
            )
            return cursor.rowcount == 1

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
        query = "SELECT id, data FROM nodestro_documents WHERE collection = ? ORDER BY updated_at DESC, id"
        params: tuple[Any, ...] = (collection,)
        if limit is not None:
            query += " LIMIT ?"
            params = (collection, limit)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [item for _key, item in self.damaged.parse(collection, rows, model)]

    def find(self, collection: str, field: str, values: Iterable[str], model: type[ModelT]) -> builtins.list[ModelT]:
        """Documents whose top-level ``field`` has one of ``values``, however many the collection holds."""
        if not _FIELD.fullmatch(field):
            raise ValueError(f"Invalid field name {field!r}")
        wanted = list(dict.fromkeys(values))
        if not wanted:
            return []
        marks = ", ".join("?" for _ in wanted)
        # ``json_valid(data)`` lets the partial indexes of :data:`INDEXED_FIELDS` apply; ``+updated_at`` keeps SQLite
        # from preferring the list index for the order and scanning the whole collection instead of looking the
        # values up. Documents that are not valid JSON come from their own small index, to be reported (C-10).
        order = "+updated_at, id" if field in INDEXED_FIELDS else "updated_at, id"
        with self._lock:
            rows = self._connection.execute(
                f"SELECT id, data FROM nodestro_documents WHERE collection = ? AND json_valid(data)"
                f" AND json_extract(data, '$.{field}') IN ({marks}) ORDER BY {order}",
                (collection, *wanted),
            ).fetchall()
            rows += self._connection.execute(
                "SELECT id, data FROM nodestro_documents WHERE collection = ? AND NOT json_valid(data)", (collection,)
            ).fetchall()
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
        """One keyset page of a collection of :data:`PAGED_COLLECTIONS` by creation, newest first (see
        :class:`DocumentStore`); indexed by schema version 2, so a page costs ``limit + 1`` index entries, not the
        whole collection. Returns the page and whether more documents follow."""
        if collection not in PAGED_COLLECTIONS:
            raise ValueError(f"list_page has no index for the collection {collection!r}")
        if limit < 0:
            raise ValueError("limit must not be negative")
        # The collection is a literal, so the partial indexes of version 2 apply.
        where = ["collection = 'runs'", "json_valid(data)", f"{CREATED_ORDER} IS NOT NULL"]
        params: builtins.list[Any] = []
        if top_level:
            where.append("json_extract(data, '$.parent_run_id') IS NULL")
        if since is not None:
            where.append(f"{CREATED_ORDER} >= strftime('%Y-%m-%dT%H:%M:%f', ?)")
            params.append(cursor_time(since))
        for name, value in (equals or {}).items():
            if not _FIELD.fullmatch(name):
                raise ValueError(f"Invalid field name {name!r}")
            where.append(f"json_type(data, '$.{name}') = 'text' AND json_extract(data, '$.{name}') = ?")
            params.append(value)
        if before is not None:
            moment, after = before
            text = cursor_time(moment)
            # ``<=`` on the order alone lets SQLite range-scan the index; the rest picks the rows after the cursor.
            where.append(f"{CREATED_ORDER} <= strftime('%Y-%m-%dT%H:%M:%f', ?)")
            where.append(f"({CREATED_ORDER} < strftime('%Y-%m-%dT%H:%M:%f', ?) OR id < ?)")
            params += [text, text, after]
        data = "data"
        if summary:
            fields = builtins.list(model.model_fields)
            marks = ", ".join("?" for _ in fields)
            data = (
                "(SELECT json_group_object(entry.key, nodestro_documents.data -> entry.key)"
                f" FROM json_each(nodestro_documents.data) AS entry WHERE entry.key IN ({marks}))"
            )
            params = [*fields, *params]
        query = (
            f"SELECT id, {data} FROM nodestro_documents WHERE {' AND '.join(where)}"
            f" ORDER BY {CREATED_ORDER} DESC, id DESC LIMIT ?"
        )
        with self._lock:
            rows = self._connection.execute(query, (*params, limit + 1)).fetchall()
        more = len(rows) > limit
        return [item for _key, item in self.damaged.parse(collection, rows[:limit], model)], more

    def list_since(self, collection: str, model: type[ModelT], *, field: str, since: datetime) -> builtins.list[ModelT]:
        if not _FIELD.fullmatch(field):
            raise ValueError(f"Invalid field name {field!r}")
        moment = (
            f"CASE WHEN json_extract(data, '$.{field}') GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"
            f" THEN strftime('%Y-%m-%dT%H:%M:%f', json_extract(data, '$.{field}')) END"
        )
        query = (
            f"SELECT id, data FROM nodestro_documents WHERE collection = ? AND json_valid(data)"
            f" AND {moment} >= strftime('%Y-%m-%dT%H:%M:%f', ?) ORDER BY {moment} DESC, id DESC"
        )
        with self._lock:
            rows = self._connection.execute(query, (collection, cursor_time(since))).fetchall()
        return [item for _key, item in self.damaged.parse(collection, rows, model)]

    def items(self, collection: str, model: type[ModelT]) -> builtins.list[tuple[str, ModelT]]:
        """``(id, document)`` pairs of a collection, in id order."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, data FROM nodestro_documents WHERE collection = ? ORDER BY id", (collection,)
            ).fetchall()
        return self.damaged.parse(collection, rows, model)

    def list_prefix(self, collection: str, prefix: str, model: type[ModelT]) -> builtins.list[ModelT]:
        """Documents whose id starts with ``prefix``, in id order."""
        pattern = _like(prefix)
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, data FROM nodestro_documents WHERE collection = ? AND id LIKE ? ESCAPE '\\' ORDER BY id",
                (collection, pattern),
            ).fetchall()
        return [item for _key, item in self.damaged.parse(collection, rows, model)]

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
            versions = {"found": current, "supported": target}

            def backup(found: int, supported: int) -> None:
                assert before_migration is not None  # tabrion calls it only when one was given
                before_migration(found, supported)

            try:
                tabrion_sqlite.migrate(
                    self._connection,
                    migrations,
                    target=target,
                    backup=None if before_migration is None else backup,
                    is_fresh=lambda _connection: self._is_empty(),
                )
            except SchemaTooNew as exc:
                found = _detail(exc, "found", current)
                raise NodestroError(
                    "storage.schema_too_new",
                    f"The database {self.database} has schema version {found}; this Nodestro release reads "
                    f"version {target}. Use the newer release, or restore a backup made with this one",
                    found=found,
                    supported=target,
                ) from None
            except MigrationMissing as exc:
                raise NodestroError(
                    "storage.migration_missing",
                    f"No migration to schema version {_detail(exc, 'missing', current + 1)}",
                    found=_detail(exc, "found", current),
                    supported=target,
                ) from None
            except BackupRequired:
                raise NodestroError(
                    "storage.backup_required",
                    "A schema migration needs a backup first; none was configured",
                    **versions,
                ) from None
            except BackupFailed as exc:
                original = exc.original
                raise NodestroError(
                    "storage.backup_failed",
                    "The backup before the schema migration failed, so nothing was changed: "
                    f"{failure_text(original if original is not None else exc)}",
                    **versions,
                ) from original
            except MigrationFailed as exc:
                original = exc.original
                step = _detail(exc, "step", current)
                raise NodestroError(
                    "storage.migration_failed",
                    f"The schema migration to version {step} failed; the database is unchanged: "
                    f"{failure_text(original if original is not None else exc)}",
                    **versions,
                    step=step,
                ) from original
            except sqlite3.Error as exc:
                # BEGIN IMMEDIATE failed (busy for longer than the timeout, a read-only file): as before, a failed
                # migration that changed nothing.
                raise NodestroError(
                    "storage.migration_failed",
                    f"The schema migration to version {current} failed; the database is unchanged: {failure_text(exc)}",
                    **versions,
                    step=current,
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

        Writes of this store wait until the copy is complete; other connections may keep reading. The copy
        (:func:`tabrion.sqlite.backup`, TABRION MIG-NOD-6) is written to a ``0600`` temporary file next to ``target``,
        checked (``quick_check``), synced and renamed over ``target``; it is one self-contained file
        (``journal_mode=DELETE``), and a symbolic link as ``target`` is refused."""
        with self._lock:
            try:
                tabrion_sqlite.backup(self._connection, target, overwrite=True)
            except UnsafePath as exc:
                raise unsafe_file(exc, target) from None

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
    "DamagedDocuments",
    "DocumentStore",
    "Migration",
    "ModelT",
    "SQLiteStore",
    "Store",
]
