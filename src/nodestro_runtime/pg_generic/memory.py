"""An Orkexa memory (``/memories``) of one tenant and namespace on PostgreSQL (table ``<prefix>memory_files``)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import threading
from typing import Any

from orkexa import MemoryToolError
from orkexa.memory import MemoryBackend

from ._sql import Executor, check_namespace, check_tenant
from .connection import ConnectionSource

__all__ = ["PostgresMemory"]


class PostgresMemory(MemoryBackend):
    """An Orkexa memory (``/memories``) of one tenant and namespace (table ``<prefix>memory_files``).

    Every command runs in one transaction of the source, as with ``SQLiteMemory``. ``lock`` as for
    :class:`~nodestro_runtime.pg_generic.files.PostgresFileStore`."""

    def __init__(
        self,
        source: ConnectionSource,
        tenant_id: str,
        *,
        prefix: str,
        namespace: str = "default",
        lock: threading.RLock | None = None,
        max_file_chars: int = 100_000,
        max_view_chars: int = 16_000,
    ) -> None:
        super().__init__(max_file_chars=max_file_chars, max_view_chars=max_view_chars)
        self.tenant_id = check_tenant(tenant_id)
        self.namespace = check_namespace(namespace)
        self.source = source
        self._sql = Executor(source, prefix=prefix, tenant_id=tenant_id)
        if lock is not None:
            self._lock = lock

    def _key(self, **params: Any) -> dict[str, Any]:
        return {"tenant_id": self.tenant_id, "namespace": self.namespace, **params}

    @property
    def capabilities(self) -> frozenset[str]:
        """``partitioned`` (by tenant) and ``concurrent_writers`` (:attr:`orkexa.memory.MemoryBackend.capabilities`):
        the commands of one process are serialized on the lock."""
        return frozenset({"partitioned", "concurrent_writers"})

    def close(self) -> None:
        """Nothing to do: the connections belong to the source."""

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._sql.transaction():
            yield

    def _read(self, key: str) -> str | None:
        rows = self._sql.execute("memory.read", self._key(path=key)).rows
        return str(rows[0][0]) if rows else None

    def _write(self, key: str, text: str) -> None:
        if "\x00" in text or "\x00" in key:
            raise MemoryToolError("Error: memory files cannot contain the character U+0000")
        self._sql.execute("memory.write", self._key(path=key, content=text))

    def _files(self, prefix: str) -> dict[str, int]:
        start = f"{prefix}/" if prefix else ""
        rows = self._sql.execute("memory.files", self._key(start=start)).rows
        return {str(path): int(size) for path, size in rows}

    def _delete(self, key: str) -> None:
        self._sql.execute("memory.delete", self._key(path=key, under=key + "/"))

    def _rename(self, old: str, new: str) -> None:
        self._sql.execute("memory.rename", self._key(new=new, cut=len(old) + 1, old=old, under=old + "/"))

    def clear(self) -> None:
        """Delete every file of this memory."""
        self._sql.execute("memory.clear", self._key())
