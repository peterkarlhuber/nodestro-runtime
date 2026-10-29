"""Persistent memory spaces (``SQLiteMemory`` namespaces) that agents share or own."""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3
import threading

from orkexa import SQLiteMemory
from orkexa.memory import MEMORY_ROOT
from pydantic import BaseModel

from .problems import NodestroError

_SPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}")
_PREFIX = "nodestro:"


class MemoryFile(BaseModel):
    path: str
    size: int


class MemorySpaceInfo(BaseModel):
    name: str
    files: int
    size: int


class MemorySpace(SQLiteMemory):
    """A ``SQLiteMemory`` with read access for the memory browser."""

    def entries(self) -> list[MemoryFile]:
        with self._lock:
            files = self._files("")
        return [MemoryFile(path=f"{MEMORY_ROOT}/{key}", size=size) for key, size in sorted(files.items())]

    def read(self, path: str) -> str:
        key = path.removeprefix(MEMORY_ROOT).strip("/")
        with self._lock:
            text = self._read(key)
        if text is None:
            raise KeyError(path)
        return text

    def write(self, path: str, text: str) -> None:
        """Create or overwrite a file (the ``create`` command validates the path)."""
        self.create(path, text)

    def remove(self, path: str) -> None:
        self.delete(path)

    def clear(self) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM memory_files WHERE namespace = ?", (self.namespace,))


def check_space_name(name: str) -> str:
    name = name.strip()
    if not _SPACE.fullmatch(name):
        raise NodestroError(
            "memory.space_name", "A memory space needs a name of letters, digits, spaces, _ . -", name=name
        )
    return name


class MemorySpaces:
    """Opens memory spaces in the Nodestro database by name."""

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        self._open: dict[str, MemorySpace] = {}
        self._lock = threading.Lock()

    def open(self, name: str) -> MemorySpace:
        """The space ``name``; one shared connection per space (memory commands are locked)."""
        name = check_space_name(name)
        with self._lock:
            space = self._open.get(name)
            if space is None:
                space = self._open[name] = MemorySpace(self.database, namespace=_PREFIX + name)
            return space

    def close(self) -> None:
        with self._lock:
            for space in self._open.values():
                space.close()
            self._open.clear()

    def all(self) -> list[MemorySpaceInfo]:
        connection = sqlite3.connect(self.database, timeout=30)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS memory_files ("
                " namespace TEXT NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL,"
                " updated_at TEXT NOT NULL, PRIMARY KEY (namespace, path))"
            )
            rows = connection.execute(
                "SELECT namespace, count(*), sum(length(CAST(content AS BLOB))) FROM memory_files"
                " WHERE substr(namespace, 1, ?) = ? GROUP BY namespace ORDER BY namespace",
                (len(_PREFIX), _PREFIX),
            ).fetchall()
        finally:
            connection.close()
        return [
            MemorySpaceInfo(name=str(namespace)[len(_PREFIX) :], files=int(count), size=int(size or 0))
            for namespace, count, size in rows
        ]
