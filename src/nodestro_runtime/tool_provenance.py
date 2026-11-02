"""Where the code of a stored tool came from.

Only one fact is recorded: which versions of a tool a person wrote in the tool editor. Everything without such a
record is ``untrusted`` (:mod:`nodestro_runtime.sandbox_policy`): AI-generated tools and drafts, tools stored before
this record existed, and tools restored from a backup (the record is cleared on restore, because a backup can come
from anywhere).

The class belongs to a tool's whole history: a tool is ``user-authored`` only while *every* version under its name
(also deleted ones) was written in the editor. Editing an AI-generated tool, or deleting it and writing a new one
under the same name, never makes it ``user-authored``; neither does approving it.

The one exception is an explicit *adoption* (ADR-0006, decision R-4): on the desktop, the local owner or an
administrator may confirm, after seeing the code, the tests and a warning, that they take over one version of an
AI-generated tool as their own. The record names who adopted which version, when, and the
:func:`content_hash` of exactly the code and tests that were shown. It counts only while that version still has that
content and every later version was written in the editor; any later version without such a record (an AI change)
ends it (:meth:`nodestro_runtime.tool_library.ToolLibrary.adoption`).
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import threading
from typing import Protocol

from pydantic import BaseModel, Field

from .storage import DocumentStore

_COLLECTION = "tool_provenance"
_ADOPTIONS = "tool_adoptions"


def code_digest(code: str) -> str:
    """A fingerprint of tool code that ignores line endings and trailing whitespace."""
    lines = [line.rstrip() for line in code.replace("\r\n", "\n").replace("\r", "\n").strip().split("\n")]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def content_hash(code: str, tests: str) -> str:
    """The fingerprint an adoption is bound to: the tool's code and its tests (both are AI-generated, and both run
    without isolation once the tool is adopted), each normalized like :func:`code_digest`."""
    return hashlib.sha256(f"{code_digest(code)}\n{code_digest(tests)}".encode()).hexdigest()


class ToolAdoption(BaseModel):
    """A person's confirmed adoption of one version of an AI-generated tool as their own code (ADR-0006, R-4)."""

    name: str
    version: int
    #: :func:`content_hash` of the code and tests of ``version`` as they were shown when the person confirmed.
    content_hash: str
    #: Who confirmed (the principal's id; ``local`` for the desktop's owner) and in which role (``owner``/``admin``).
    actor: str
    role: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AuthoredVersions(BaseModel):
    """The versions of one tool a person wrote in the editor."""

    name: str
    versions: list[int] = Field(default_factory=list)


class ToolProvenance(Protocol):
    def authored(self, name: str) -> set[int]:
        """Versions of ``name`` written in the tool editor."""
        ...

    def record_authored(self, name: str, version: int) -> None: ...

    def adoption(self, name: str) -> ToolAdoption | None:
        """The stored adoption of ``name`` (whether it still counts is the library's decision)."""
        ...

    def record_adoption(self, adoption: ToolAdoption) -> None: ...

    def drop_adoption(self, name: str) -> None: ...

    def clear(self) -> None:
        """Forget every record, adoptions too (after a restore: the restored tools become ``untrusted``)."""
        ...


class MemoryProvenance:
    """Records kept in memory (a library without a database, e.g. in tests)."""

    def __init__(self) -> None:
        self._authored: dict[str, set[int]] = {}
        self._adoptions: dict[str, ToolAdoption] = {}
        self._lock = threading.Lock()

    def authored(self, name: str) -> set[int]:
        with self._lock:
            return set(self._authored.get(name, set()))

    def record_authored(self, name: str, version: int) -> None:
        with self._lock:
            self._authored.setdefault(name, set()).add(version)

    def adoption(self, name: str) -> ToolAdoption | None:
        with self._lock:
            found = self._adoptions.get(name)
            return found.model_copy() if found is not None else None

    def record_adoption(self, adoption: ToolAdoption) -> None:
        with self._lock:
            self._adoptions[adoption.name] = adoption.model_copy()

    def drop_adoption(self, name: str) -> None:
        with self._lock:
            self._adoptions.pop(name, None)

    def clear(self) -> None:
        with self._lock:
            self._authored.clear()
            self._adoptions.clear()


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

    def adoption(self, name: str) -> ToolAdoption | None:
        return self.store.get(_ADOPTIONS, name, ToolAdoption)

    def record_adoption(self, adoption: ToolAdoption) -> None:
        with self._lock:
            self.store.put(_ADOPTIONS, adoption.name, adoption)

    def drop_adoption(self, name: str) -> None:
        with self._lock:
            if self.store.get(_ADOPTIONS, name, ToolAdoption) is not None:
                self.store.delete(_ADOPTIONS, name)

    def clear(self) -> None:
        with self._lock:
            for record in self.store.list(_COLLECTION, AuthoredVersions):
                self.store.delete(_COLLECTION, record.name)
            for adoption in self.store.list(_ADOPTIONS, ToolAdoption):
                self.store.delete(_ADOPTIONS, adoption.name)


__all__ = [
    "AuthoredVersions",
    "MemoryProvenance",
    "StoredProvenance",
    "ToolAdoption",
    "ToolProvenance",
    "code_digest",
    "content_hash",
]
