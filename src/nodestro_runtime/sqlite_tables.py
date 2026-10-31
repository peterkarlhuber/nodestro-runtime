"""The SQL Nodestro runs on Orkexa's SQLite tables, in one place.

Orkexa's ``SQLiteIndex`` (tables ``index_chunks``, ``index_fts``) and ``SQLiteMemory`` (``memory_files``) have no
methods for a few things Nodestro needs: chunk counts per file, pointing a file's chunks to the file, a file's
indexed text, the totals of every memory space and clearing one space. These functions do them with the same
statements as before, on the connection and lock of the Orkexa object, so they run in its transactions and
never write beside it. Only this module and :mod:`nodestro_runtime.storage` touch a database connection
directly (``tests/test_storage_contract.py`` checks that); a second storage backend implements these
operations for its own tables.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from orkexa import SQLiteIndex, SQLiteMemory

#: The table ``SQLiteMemory`` creates on first use (created here too, so totals work on a new database).
_MEMORY_TABLE = (
    "CREATE TABLE IF NOT EXISTS memory_files ("
    " namespace TEXT NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL,"
    " updated_at TEXT NOT NULL, PRIMARY KEY (namespace, path))"
)


class KnowledgeChunks:
    """Read and adjust the chunks of one ``SQLiteIndex`` namespace by source (a knowledge file id)."""

    def __init__(self, index: SQLiteIndex) -> None:
        self.index = index

    def _connection(self) -> Any:
        return self.index._connection  # Orkexa's connection; used under the index's own lock

    def counts(self) -> dict[str, int]:
        """Number of chunks per source."""
        with self.index._lock:
            rows = (
                self._connection()
                .execute(
                    "SELECT source, count(*) FROM index_chunks WHERE namespace = ? GROUP BY source",
                    (self.index.namespace,),
                )
                .fetchall()
            )
        return {str(source): int(count) for source, count in rows}

    def attach_file(self, source: str) -> None:
        """Let every chunk of ``source`` point to the file with the same id (``file_id``)."""
        with self.index._lock:
            self._connection().execute(
                "UPDATE index_chunks SET file_id = ? WHERE namespace = ? AND source = ?",
                (source, self.index.namespace, source),
            )

    def text(self, source: str) -> str:
        """The indexed text of ``source``: its chunks in the order they were added, one per line."""
        with self.index._lock:
            rows = (
                self._connection()
                .execute(
                    "SELECT text FROM index_chunks WHERE namespace = ? AND source = ? ORDER BY id",
                    (self.index.namespace, source),
                )
                .fetchall()
            )
        return "\n".join(str(row[0]) for row in rows)


def clear_memory(memory: SQLiteMemory) -> None:
    """Delete every file of the memory's namespace."""
    with memory._lock:
        memory._connection.execute("DELETE FROM memory_files WHERE namespace = ?", (memory.namespace,))


def memory_totals(database: str, prefix: str) -> list[tuple[str, int, int]]:
    """``(namespace, files, bytes)`` of every memory namespace starting with ``prefix``, by namespace.

    Uses a short-lived connection of its own (the spaces may not be open)."""
    connection = sqlite3.connect(database, timeout=30)
    try:
        connection.execute(_MEMORY_TABLE)
        rows = connection.execute(
            "SELECT namespace, count(*), sum(length(CAST(content AS BLOB))) FROM memory_files"
            " WHERE substr(namespace, 1, ?) = ? GROUP BY namespace ORDER BY namespace",
            (len(prefix), prefix),
        ).fetchall()
    finally:
        connection.close()
    return [(str(namespace), int(count), int(size or 0)) for namespace, count, size in rows]


__all__ = ["KnowledgeChunks", "clear_memory", "memory_totals"]
