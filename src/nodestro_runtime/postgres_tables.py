"""Knowledge files, their search index and memory spaces on PostgreSQL (ADR-0002, server profile).

The Orkexa backends Nodestro uses on the desktop (``SQLiteFileStore``, ``SQLiteIndex``, ``SQLiteMemory``) keep
their data in SQLite tables. These classes implement the same Orkexa interfaces (``FileStore``, ``Index``,
``MemoryBackend``) on the tables of migration ``0002`` for one tenant and one Orkexa namespace, on the connection
and lock of a :class:`~nodestro_runtime.storage_postgres.PostgresDatabase`. Every query filters on ``tenant_id``.

Search: the words of a chunk's title and text (letters and digits; lower case, diacritics removed, as SQLite's
``unicode61 remove_diacritics 2`` tokenizer does) are kept in ``search_text`` and indexed as a ``tsvector``. A query
matches any of its words or word prefixes, like :class:`orkexa.SQLiteIndex`; results are ranked by ``ts_rank``
instead of BM25, so the order of equally good hits can differ from the desktop. Vector search is not available on
PostgreSQL yet.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
import json
import re
from typing import TYPE_CHECKING, Any
import unicodedata

from orkexa import Chunk, FileStore, Hit, Index, MemoryToolError, StoredFile
from orkexa.memory import MemoryBackend

from .knowledge import KnowledgeTables
from .memory_spaces import BrowsableMemory
from .storage_postgres import TENANT_ID, PostgresDatabase, _encode

if TYPE_CHECKING:
    from .server_settings import SecretBox
    from .workspace import WorkspaceStorage

#: Words as SQLite's unicode61 tokenizer sees them: runs of letters and digits.
_WORD = re.compile(r"[^\W_]+")
#: The rank that reciprocal-rank fusion adds to (as in ``orkexa.SQLiteIndex``).
_RRF_K = 60


def _check(tenant_id: str, namespace: str) -> None:
    if not TENANT_ID.fullmatch(tenant_id):
        raise ValueError(f"Invalid tenant id {tenant_id!r}")
    if not namespace:
        raise ValueError("namespace must not be empty")


def search_words(text: str) -> builtins.list[str]:
    """The words of ``text`` for the search index: lower case, without diacritics."""
    folded = "".join(char for char in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(char))
    return _WORD.findall(folded)


def _text(value: str) -> str:
    """PostgreSQL text cannot hold U+0000; the index keeps U+FFFD in its place."""
    return value.replace("\x00", "\ufffd")


def _tsquery(query: str) -> str:
    """Any word or word prefix of ``query`` (``refund`` finds ``refunds``); empty: nothing to search for."""
    words = dict.fromkeys(search_words(query))
    return " | ".join(f"'{word}':*" for word in words)


class PostgresFileStore(FileStore):
    """An Orkexa :class:`~orkexa.FileStore` of one tenant and namespace (table ``nodestro_files``)."""

    def __init__(
        self,
        database: PostgresDatabase,
        tenant_id: str,
        *,
        namespace: str = "default",
        files_api: Any | None = None,
        max_image_bytes: int = 5_000_000,
        max_document_bytes: int = 20_000_000,
    ) -> None:
        super().__init__(files_api=files_api, max_image_bytes=max_image_bytes, max_document_bytes=max_document_bytes)
        _check(tenant_id, namespace)
        self.database = database
        self.tenant_id = tenant_id
        self.namespace = namespace
        self._lock = database._lock  # one lock for every user of the connection

    def close(self) -> None:
        """Nothing to do: the connection belongs to the database."""

    def _save(self, entry: StoredFile, data: bytes | None) -> None:
        if data is None:
            self.database._write(
                "UPDATE nodestro_files SET entry = %s::jsonb WHERE tenant_id = %s AND namespace = %s AND id = %s",
                (_encode(entry.model_dump_json())[0], self.tenant_id, self.namespace, entry.id),
            )
        else:
            self.database._write(
                "INSERT INTO nodestro_files (tenant_id, namespace, id, entry, data) VALUES (%s, %s, %s, %s::jsonb, %s)"
                " ON CONFLICT (tenant_id, namespace, id) DO UPDATE SET entry = excluded.entry, data = excluded.data",
                (self.tenant_id, self.namespace, entry.id, _encode(entry.model_dump_json())[0], data),
            )

    def _entry(self, file_id: str) -> StoredFile | None:
        rows = self.database._query(
            "SELECT entry::text FROM nodestro_files WHERE tenant_id = %s AND namespace = %s AND id = %s",
            (self.tenant_id, self.namespace, file_id),
        )
        return StoredFile.model_validate_json(rows[0][0]) if rows else None

    def _data(self, file_id: str) -> bytes | None:
        rows = self.database._query(
            "SELECT data FROM nodestro_files WHERE tenant_id = %s AND namespace = %s AND id = %s",
            (self.tenant_id, self.namespace, file_id),
        )
        return bytes(rows[0][0]) if rows else None

    def _entries(self) -> builtins.list[StoredFile]:
        rows = self.database._query(
            "SELECT entry::text FROM nodestro_files WHERE tenant_id = %s AND namespace = %s",
            (self.tenant_id, self.namespace),
        )
        return [StoredFile.model_validate_json(row[0]) for row in rows]

    def _remove(self, file_id: str) -> None:
        self.database._write(
            "DELETE FROM nodestro_files WHERE tenant_id = %s AND namespace = %s AND id = %s",
            (self.tenant_id, self.namespace, file_id),
        )


class PostgresIndex(Index):
    """An Orkexa :class:`~orkexa.Index` of one tenant and namespace with full-text search (``nodestro_chunks``).

    Also gives what Nodestro reads beside the Orkexa interface (``KnowledgeChunks`` on SQLite): chunk counts per
    source, pointing a source's chunks to its file, and a source's indexed text."""

    def __init__(
        self,
        database: PostgresDatabase,
        tenant_id: str,
        *,
        namespace: str = "default",
        max_chars: int = 1500,
        overlap: int = 200,
    ) -> None:
        super().__init__(max_chars=max_chars, overlap=overlap)
        _check(tenant_id, namespace)
        self.database = database
        self.tenant_id = tenant_id
        self.namespace = namespace
        self._lock = database._lock

    def close(self) -> None:
        """Nothing to do: the connection belongs to the database."""

    def __len__(self) -> int:
        rows = self.database._query(
            "SELECT count(*) FROM nodestro_chunks WHERE tenant_id = %s AND namespace = %s",
            (self.tenant_id, self.namespace),
        )
        return int(rows[0][0])

    def sources(self) -> builtins.list[str]:
        rows = self.database._query(
            "SELECT DISTINCT source FROM nodestro_chunks WHERE tenant_id = %s AND namespace = %s ORDER BY source",
            (self.tenant_id, self.namespace),
        )
        return [str(row[0]) for row in rows]

    def add_chunks(self, chunks: Iterable[Chunk]) -> builtins.list[int]:
        kept = [chunk for chunk in chunks if chunk.text.strip()]
        ids: builtins.list[int] = []
        if not kept:
            return ids
        with self.database._transaction() as db:
            for chunk in kept:
                row = db.execute(
                    "INSERT INTO nodestro_chunks"
                    " (tenant_id, namespace, source, title, file_id, metadata, text, search_text)"
                    " VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s) RETURNING id",
                    (
                        self.tenant_id,
                        self.namespace,
                        _text(chunk.source),
                        None if chunk.title is None else _text(chunk.title),
                        chunk.file_id,
                        _encode(json.dumps(chunk.metadata))[0],
                        _text(chunk.text),
                        " ".join(search_words(f"{chunk.text} {chunk.title or ''}")),
                    ),
                ).fetchone()
                ids.append(int(row[0]))
        return ids

    def delete(self, source: str) -> int:
        return self.database._write(
            "DELETE FROM nodestro_chunks WHERE tenant_id = %s AND namespace = %s AND source = %s",
            (self.tenant_id, self.namespace, source),
        )

    def search(self, query: str, k: int = 5) -> builtins.list[Hit]:
        if k < 1:
            raise ValueError("k must be positive")
        expression = _tsquery(query)
        if not expression:
            return []
        rows = self.database._query(
            "SELECT id, source, title, file_id, metadata::text, text FROM nodestro_chunks,"
            " to_tsquery('simple', %s) AS query"
            " WHERE tenant_id = %s AND namespace = %s AND search @@ query"
            " ORDER BY ts_rank(search, query, 1) DESC, id LIMIT %s",
            (expression, self.tenant_id, self.namespace, k),
        )
        return [
            Hit(
                id=int(chunk_id),
                score=1.0 / (_RRF_K + rank + 1),
                source=source,
                title=title,
                file_id=file_id,
                metadata=json.loads(metadata),
                text=text,
            )
            for rank, (chunk_id, source, title, file_id, metadata, text) in enumerate(rows)
        ]

    # ---- what KnowledgeChunks does on SQLite -------------------------------------------------------

    def counts(self) -> dict[str, int]:
        """Number of chunks per source."""
        rows = self.database._query(
            "SELECT source, count(*) FROM nodestro_chunks WHERE tenant_id = %s AND namespace = %s GROUP BY source",
            (self.tenant_id, self.namespace),
        )
        return {str(source): int(count) for source, count in rows}

    def attach_file(self, source: str) -> None:
        """Let every chunk of ``source`` point to the file with the same id."""
        self.database._write(
            "UPDATE nodestro_chunks SET file_id = %s WHERE tenant_id = %s AND namespace = %s AND source = %s",
            (source, self.tenant_id, self.namespace, source),
        )

    def text(self, source: str) -> str:
        """The indexed text of ``source``: its chunks in the order they were added, one per line."""
        rows = self.database._query(
            "SELECT text FROM nodestro_chunks WHERE tenant_id = %s AND namespace = %s AND source = %s ORDER BY id",
            (self.tenant_id, self.namespace, source),
        )
        return "\n".join(str(row[0]) for row in rows)


class PostgresMemory(MemoryBackend):
    """An Orkexa memory (``/memories``) of one tenant and namespace (table ``nodestro_memory_files``).

    Every command runs in one transaction, as with ``SQLiteMemory``."""

    def __init__(
        self,
        database: PostgresDatabase,
        tenant_id: str,
        *,
        namespace: str = "default",
        max_file_chars: int = 100_000,
        max_view_chars: int = 16_000,
    ) -> None:
        super().__init__(max_file_chars=max_file_chars, max_view_chars=max_view_chars)
        _check(tenant_id, namespace)
        self.database = database
        self.tenant_id = tenant_id
        self.namespace = namespace
        self._lock = database._lock

    def close(self) -> None:
        """Nothing to do: the connection belongs to the database."""

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self.database._transaction():
            yield

    def _read(self, key: str) -> str | None:
        rows = self.database._query(
            "SELECT content FROM nodestro_memory_files WHERE tenant_id = %s AND namespace = %s AND path = %s",
            (self.tenant_id, self.namespace, key),
        )
        return str(rows[0][0]) if rows else None

    def _write(self, key: str, text: str) -> None:
        if "\x00" in text or "\x00" in key:
            raise MemoryToolError("Error: memory files cannot contain the character U+0000")
        self.database._write(
            "INSERT INTO nodestro_memory_files (tenant_id, namespace, path, content) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (tenant_id, namespace, path) DO UPDATE SET content = excluded.content,"
            " updated_at = clock_timestamp()",
            (self.tenant_id, self.namespace, key, text),
        )

    def _files(self, prefix: str) -> dict[str, int]:
        start = f"{prefix}/" if prefix else ""
        rows = self.database._query(
            "SELECT path, octet_length(content) FROM nodestro_memory_files"
            " WHERE tenant_id = %s AND namespace = %s AND starts_with(path, %s)",
            (self.tenant_id, self.namespace, start),
        )
        return {str(path): int(size) for path, size in rows}

    def _delete(self, key: str) -> None:
        self.database._write(
            "DELETE FROM nodestro_memory_files WHERE tenant_id = %s AND namespace = %s"
            " AND (path = %s OR starts_with(path, %s))",
            (self.tenant_id, self.namespace, key, key + "/"),
        )

    def _rename(self, old: str, new: str) -> None:
        self.database._write(
            "UPDATE nodestro_memory_files SET path = %s || substr(path, %s), updated_at = clock_timestamp()"
            " WHERE tenant_id = %s AND namespace = %s AND (path = %s OR starts_with(path, %s))",
            (new, len(old) + 1, self.tenant_id, self.namespace, old, old + "/"),
        )

    def clear(self) -> None:
        """Delete every file of this memory."""
        self.database._write(
            "DELETE FROM nodestro_memory_files WHERE tenant_id = %s AND namespace = %s",
            (self.tenant_id, self.namespace),
        )


class PostgresMemorySpace(PostgresMemory, BrowsableMemory):
    """A memory space of the memory browser on PostgreSQL."""


class PostgresMemoryTables:
    """The memory spaces of one tenant (:class:`~nodestro_runtime.memory_spaces.MemoryTables`)."""

    def __init__(self, database: PostgresDatabase, tenant_id: str) -> None:
        _check(tenant_id, "-")
        self.database = database
        self.tenant_id = tenant_id

    def open_space(self, namespace: str) -> BrowsableMemory:
        return PostgresMemorySpace(self.database, self.tenant_id, namespace=namespace)

    def totals(self, prefix: str) -> builtins.list[tuple[str, int, int]]:
        return memory_totals(self.database, self.tenant_id, prefix)


def knowledge_tables(database: PostgresDatabase, tenant_id: str, *, namespace: str = "nodestro") -> KnowledgeTables:
    """The knowledge storage of one tenant for :class:`~nodestro_runtime.knowledge.KnowledgeBase`."""
    index = PostgresIndex(database, tenant_id, namespace=namespace)
    return KnowledgeTables(PostgresFileStore(database, tenant_id, namespace=namespace), index, index)


def tenant_storage(
    database: PostgresDatabase,
    tenant_id: str,
    secrets: SecretBox | None = None,
    endpoints: tuple[str, ...] = (),
) -> WorkspaceStorage:
    """Everything a :class:`~nodestro_runtime.workspace.Workspace` stores, for one tenant of ``database``.

    ``secrets`` encrypts the tenant's provider keys at rest (a server always passes its box); ``endpoints`` are the
    model endpoints the operator allows (none by default)."""
    from .workspace import WorkspaceStorage

    return WorkspaceStorage(
        tenant_id=tenant_id,
        store=database.store(tenant_id),
        knowledge=knowledge_tables(database, tenant_id),
        memory=PostgresMemoryTables(database, tenant_id),
        secrets=secrets,
        endpoints=tuple(endpoints),
    )


def memory_totals(database: PostgresDatabase, tenant_id: str, prefix: str) -> builtins.list[tuple[str, int, int]]:
    """``(namespace, files, bytes)`` of every memory namespace of the tenant starting with ``prefix``."""
    _check(tenant_id, "-")
    rows = database._query(
        "SELECT namespace, count(*), sum(octet_length(content)) FROM nodestro_memory_files"
        " WHERE tenant_id = %s AND starts_with(namespace, %s) GROUP BY namespace ORDER BY namespace",
        (tenant_id, prefix),
    )
    return [(str(namespace), int(count), int(size or 0)) for namespace, count, size in rows]


__all__ = [
    "PostgresFileStore",
    "PostgresIndex",
    "PostgresMemory",
    "PostgresMemorySpace",
    "PostgresMemoryTables",
    "knowledge_tables",
    "memory_totals",
    "search_words",
    "tenant_storage",
]
