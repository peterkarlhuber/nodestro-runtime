"""A small document store on SQLite: one JSON document per (collection, id).

Workflows, agent templates, runs and settings are stored as Pydantic models.
The library's ``SQLiteMemory``, ``SQLiteFileStore`` and ``SQLiteIndex`` use
their own tables in the same database file.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


def _like(prefix: str) -> str:
    """A LIKE pattern (escape character ``\\``) for ids starting with ``prefix``."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Store:
    """Thread-safe JSON documents in SQLite, grouped into collections."""

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        self._lock = threading.RLock()
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
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def put(self, collection: str, key: str, model: BaseModel) -> None:
        data = model.model_dump_json()
        with self._transaction() as db:
            db.execute(
                "INSERT INTO nodestro_documents (collection, id, data) VALUES (?, ?, ?)"
                " ON CONFLICT (collection, id) DO UPDATE SET data = excluded.data,"
                " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (collection, key, data),
            )

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
