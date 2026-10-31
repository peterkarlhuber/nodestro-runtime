"""Where the code of a stored tool came from.

Only one fact is recorded: which versions of a tool a person wrote in the tool editor. Everything without such a
record is ``untrusted`` (:mod:`nodestro_runtime.sandbox_policy`): AI-generated tools and drafts, tools stored before
this record existed, and tools restored from a backup (the record is cleared on restore, because a backup can come
from anywhere).

The class belongs to a tool's whole history: a tool is ``user-authored`` only while *every* version under its name
(also deleted ones) was written in the editor. Editing an AI-generated tool, or deleting it and writing a new one
under the same name, never makes it ``user-authored``; neither does approving it.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Protocol

from pydantic import BaseModel, Field

from .storage import DocumentStore

_COLLECTION = "tool_provenance"


def code_digest(code: str) -> str:
    """A fingerprint of tool code that ignores line endings and trailing whitespace."""
    lines = [line.rstrip() for line in code.replace("\r\n", "\n").replace("\r", "\n").strip().split("\n")]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


class AuthoredVersions(BaseModel):
    """The versions of one tool a person wrote in the editor."""

    name: str
    versions: list[int] = Field(default_factory=list)


class ToolProvenance(Protocol):
    def authored(self, name: str) -> set[int]:
        """Versions of ``name`` written in the tool editor."""
        ...

    def record_authored(self, name: str, version: int) -> None: ...

    def clear(self) -> None:
        """Forget every record (after a restore: the restored tools become ``untrusted``)."""
        ...


class MemoryProvenance:
    """Records kept in memory (a library without a database, e.g. in tests)."""

    def __init__(self) -> None:
        self._authored: dict[str, set[int]] = {}
        self._lock = threading.Lock()

    def authored(self, name: str) -> set[int]:
        with self._lock:
            return set(self._authored.get(name, set()))

    def record_authored(self, name: str, version: int) -> None:
        with self._lock:
            self._authored.setdefault(name, set()).add(version)

    def clear(self) -> None:
        with self._lock:
            self._authored.clear()


class StoredProvenance:
    """Records in the Nodestro database (collection ``tool_provenance``, one document per tool name)."""

    def __init__(self, store: DocumentStore) -> None:
        self.store = store
        self._lock = threading.Lock()

    def authored(self, name: str) -> set[int]:
        found = self.store.get(_COLLECTION, name, AuthoredVersions)
        return set(found.versions) if found is not None else set()

    def record_authored(self, name: str, version: int) -> None:
        with self._lock:
            found = self.store.get(_COLLECTION, name, AuthoredVersions) or AuthoredVersions(name=name)
            if version not in found.versions:
                found.versions.append(version)
            self.store.put(_COLLECTION, name, found)

    def clear(self) -> None:
        with self._lock:
            for record in self.store.list(_COLLECTION, AuthoredVersions):
                self.store.delete(_COLLECTION, record.name)


__all__ = ["AuthoredVersions", "MemoryProvenance", "StoredProvenance", "ToolProvenance", "code_digest"]
