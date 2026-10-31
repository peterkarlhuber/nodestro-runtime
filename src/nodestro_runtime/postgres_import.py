"""Move the data of a desktop database into one tenant of a PostgreSQL server database (ADR-0002).

:func:`import_desktop_database` copies a desktop's ``nodestro.db`` (documents, knowledge files and their search
index, memory spaces) into an empty tenant, in one transaction: either everything arrives or nothing. The source
is opened read-only; use a closed desktop's database or a backup copy of it.

What is not copied:

- ``tool_provenance``: a server cannot verify where the desktop's records came from, so every imported tool is
  untrusted (like after restoring a backup) and runs only in an isolating sandbox.
- Files beside the database (``tools``, ``python``, ``inputs`` in the data directory): the server keeps them
  elsewhere; :attr:`ImportReport.not_imported` names them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any

from pydantic import BaseModel

from .postgres_tables import search_words
from .problems import NodestroError
from .storage import SCHEMA_VERSION
from .storage_postgres import TENANT_ID, PostgresDatabase, _encode

#: Document collections that are never imported (see the module documentation).
SKIPPED_COLLECTIONS = frozenset({"tool_provenance"})
#: Directories of a desktop data directory whose files stay behind.
DATA_DIRECTORIES = ("tools", "python", "inputs")
_TENANT_TABLES = ("nodestro_documents", "nodestro_files", "nodestro_chunks", "nodestro_memory_files")
_BATCH = 500


class ImportReport(BaseModel):
    tenant_id: str
    documents: int = 0
    files: int = 0
    chunks: int = 0
    memory_files: int = 0
    #: Documents left out on purpose, by collection.
    skipped: dict[str, int] = {}
    #: What stays behind and why (shown to the operator).
    not_imported: list[str] = []


def _text(value: str) -> str:
    return value.replace("\x00", "�")


def _batches(cursor: sqlite3.Cursor) -> Iterator[list[Any]]:
    while rows := cursor.fetchmany(_BATCH):
        yield rows


def _has_table(source: sqlite3.Connection, name: str) -> bool:
    return (
        source.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None
    )


def import_desktop_database(source: str | Path, database: PostgresDatabase, tenant_id: str) -> ImportReport:
    """Copy the desktop database ``source`` into the empty tenant ``tenant_id``; returns what was copied.

    Refuses (and changes nothing) when the tenant already has data (``storage.import_target_not_empty``), when the
    source is not a Nodestro database of this release (``storage.import_source``), or when the server database is
    not at the current schema (``storage.schema_mismatch``)."""
    if not TENANT_ID.fullmatch(tenant_id):
        raise ValueError(f"Invalid tenant id {tenant_id!r}")
    path = Path(source)
    if not path.is_file():
        raise NodestroError("storage.import_source", f"{path} is not a file", path=str(path))
    database.require_current_schema()
    report = ImportReport(tenant_id=tenant_id)
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise NodestroError("storage.import_source", f"Cannot open {path}: {exc}", path=str(path)) from exc
    with closing(connection) as sqlite:
        try:
            sqlite.execute("BEGIN")  # one consistent read of the whole source
            version = int(sqlite.execute("PRAGMA user_version").fetchone()[0])
            has_documents = _has_table(sqlite, "nodestro_documents")
        except sqlite3.DatabaseError as exc:
            raise NodestroError(
                "storage.import_source", f"{path} is not a Nodestro database: {exc}", path=str(path)
            ) from exc
        if not has_documents or version != SCHEMA_VERSION:
            raise NodestroError(
                "storage.import_source",
                f"{path} is not a Nodestro database of this release (schema version {version}, needed "
                f"{SCHEMA_VERSION}); open it with this desktop release first",
                path=str(path),
                found=version,
                supported=SCHEMA_VERSION,
            )
        with database._transaction() as db:
            for table in _TENANT_TABLES:
                if db.execute(f"SELECT 1 FROM {table} WHERE tenant_id = %s LIMIT 1", (tenant_id,)).fetchone():
                    raise NodestroError(
                        "storage.import_target_not_empty",
                        f"Tenant {tenant_id} already has data; import only into an empty tenant",
                        tenant=tenant_id,
                    )
            _copy_documents(sqlite, db, report)
            _copy_files(sqlite, db, report)
            _copy_chunks(sqlite, db, report)
            _copy_memory(sqlite, db, report)
    data_dir = path.parent
    for name in DATA_DIRECTORIES:
        folder = data_dir / name
        if folder.is_dir() and any(folder.iterdir()):
            report.not_imported.append(f"{name}/: files beside the database are not part of the import")
    if report.skipped.get("tool_provenance"):
        report.not_imported.append("tool_provenance: imported tools are untrusted and need an isolating sandbox")
    return report


def _copy_documents(sqlite: sqlite3.Connection, db: Any, report: ImportReport) -> None:
    cursor = sqlite.execute("SELECT collection, id, data, updated_at FROM nodestro_documents ORDER BY collection, id")
    for rows in _batches(cursor):
        values = []
        for collection, key, data, updated_at in rows:
            if collection in SKIPPED_COLLECTIONS:
                report.skipped[collection] = report.skipped.get(collection, 0) + 1
                continue
            encoded, raw = _encode(data)
            values.append((report.tenant_id, collection, key, encoded, raw, updated_at))
        with db.cursor() as out:
            out.executemany(
                "INSERT INTO nodestro_documents (tenant_id, collection, id, data, raw, updated_at)"
                " VALUES (%s, %s, %s, %s::jsonb, %s, %s::timestamptz)",
                values,
            )
        report.documents += len(values)


def _copy_files(sqlite: sqlite3.Connection, db: Any, report: ImportReport) -> None:
    if not _has_table(sqlite, "stored_files"):
        return
    cursor = sqlite.execute("SELECT namespace, id, entry, data FROM stored_files ORDER BY namespace, id")
    for rows in _batches(cursor):
        with db.cursor() as out:
            out.executemany(
                "INSERT INTO nodestro_files (tenant_id, namespace, id, entry, data) VALUES (%s, %s, %s, %s::jsonb, %s)",
                [(report.tenant_id, ns, key, _encode(entry)[0], bytes(data)) for ns, key, entry, data in rows],
            )
        report.files += len(rows)


def _copy_chunks(sqlite: sqlite3.Connection, db: Any, report: ImportReport) -> None:
    if not _has_table(sqlite, "index_chunks"):
        return
    cursor = sqlite.execute("SELECT namespace, source, title, file_id, metadata, text FROM index_chunks ORDER BY id")
    for rows in _batches(cursor):
        with db.cursor() as out:
            out.executemany(
                "INSERT INTO nodestro_chunks"
                " (tenant_id, namespace, source, title, file_id, metadata, text, search_text)"
                " VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                [
                    (
                        report.tenant_id,
                        ns,
                        _text(source),
                        None if title is None else _text(title),
                        file_id,
                        _encode(metadata or json.dumps({}))[0],
                        _text(text),
                        " ".join(search_words(f"{text} {title or ''}")),
                    )
                    for ns, source, title, file_id, metadata, text in rows
                ],
            )
        report.chunks += len(rows)


def _copy_memory(sqlite: sqlite3.Connection, db: Any, report: ImportReport) -> None:
    if not _has_table(sqlite, "memory_files"):
        return
    cursor = sqlite.execute("SELECT namespace, path, content, updated_at FROM memory_files ORDER BY namespace, path")
    for rows in _batches(cursor):
        with db.cursor() as out:
            out.executemany(
                "INSERT INTO nodestro_memory_files (tenant_id, namespace, path, content, updated_at)"
                " VALUES (%s, %s, %s, %s, %s::timestamptz)",
                [(report.tenant_id, ns, _text(p), _text(content), updated) for ns, p, content, updated in rows],
            )
        report.memory_files += len(rows)


__all__ = ["DATA_DIRECTORIES", "SKIPPED_COLLECTIONS", "ImportReport", "import_desktop_database"]
