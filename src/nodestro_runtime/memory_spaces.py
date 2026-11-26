"""Persistent memory spaces (Orkexa memory namespaces) that agents share or own.

The desktop keeps them in ``SQLiteMemory``; the server passes :class:`MemoryTables` for PostgreSQL
(:class:`nodestro_runtime.postgres_tables.PostgresMemoryTables`).

Owners (RT-089c): on a server every space belongs to one member, recorded in the tenant's store (:data:`OWNERS`).
Its first writer owns it: the member who writes its first file, or the person a run acts for when one of its steps
first opens it (:meth:`MemorySpaces.open_for`). Only the owner lists, reads and writes it, and only the owner's runs
and agent tests use it; nobody else learns that it exists (``memory.space_not_found``). A space that already had
files before owners were recorded has none and fails closed: no member's run or agent test uses it, and only the
server's operator gives it an owner (:meth:`MemorySpaces.assign`, audited by the caller); nothing assigns one by
itself and nothing is deleted. The desktop's single person (``owner`` ``None``, or a workspace without
:attr:`MemorySpaces.owners`) sees every space as before.
"""

from __future__ import annotations

from abc import abstractmethod
from datetime import UTC, datetime
from pathlib import Path
import re
import threading
from typing import Literal, Protocol

from orkexa import SQLiteMemory
from orkexa.memory import MEMORY_ROOT, MemoryBackend
from pydantic import BaseModel, Field

from .problems import NodestroError
from .sqlite_tables import clear_memory, memory_totals
from .storage import DocumentStore

#: A valid memory space name (the HTTP API checks names with it too).
SPACE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}")
_SPACE = SPACE_PATTERN  # the name before it was public
_PREFIX = "nodestro:"


class MemoryFile(BaseModel):
    path: str
    size: int


class MemorySpaceInfo(BaseModel):
    name: str
    files: int
    size: int


#: The collection of the spaces' owners (RT-089c): space name -> :class:`SpaceOwner`.
OWNERS = "memory_space_owners"
#: Whom a caller that names nobody acts for (RT-089c): it owns no space, so it uses none where there are owners
#: (the same value as ``conversations.NO_OWNER``).
NOBODY = "<no owner>"


class SpaceOwner(BaseModel):
    """Who owns a memory space (RT-089c)."""

    space: str
    owner: str
    #: ``created``: its first writer; ``assigned``: the operator gave a space without an owner its owner.
    how: Literal["created", "assigned"]
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def space_not_found(name: str) -> NodestroError:
    """The answer for a space the caller may not use: the same as for one that does not exist."""
    return NodestroError("memory.space_not_found", f"There is no memory space {name}", name=name)


class BrowsableMemory(MemoryBackend):
    """A memory with read access for the memory browser (any Orkexa storage)."""

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

    @abstractmethod
    def clear(self) -> None:
        """Delete every file of the space."""

    def close(self) -> None:
        """Release the storage (nothing for a shared connection)."""


class MemorySpace(BrowsableMemory, SQLiteMemory):
    """A ``SQLiteMemory`` with read access for the memory browser."""

    def clear(self) -> None:
        clear_memory(self)

    def close(self) -> None:
        SQLiteMemory.close(self)


class MemoryTables(Protocol):
    """Where memory spaces live: opens a namespace and sums up every namespace with a prefix."""

    def open_space(self, namespace: str) -> BrowsableMemory: ...

    def totals(self, prefix: str) -> list[tuple[str, int, int]]: ...


class _SQLiteMemoryTables:
    def __init__(self, database: str) -> None:
        self.database = database

    def open_space(self, namespace: str) -> BrowsableMemory:
        return MemorySpace(self.database, namespace=namespace)

    def totals(self, prefix: str) -> list[tuple[str, int, int]]:
        return memory_totals(self.database, prefix)


def check_space_name(name: str) -> str:
    name = name.strip()
    if not _SPACE.fullmatch(name):
        raise NodestroError(
            "memory.space_name", "A memory space needs a name of letters, digits, spaces, _ . -", name=name
        )
    return name


class MemorySpaces:
    """Opens memory spaces in the Nodestro database by name."""

    def __init__(self, database: str | Path | MemoryTables, *, owners: DocumentStore | None = None) -> None:
        self.tables: MemoryTables = _SQLiteMemoryTables(str(database)) if isinstance(database, str | Path) else database
        self._open: dict[str, BrowsableMemory] = {}
        self._lock = threading.Lock()
        #: RT-089c: where the owners are recorded (a server tenant's store); ``None``: no owners (the desktop's single
        #: person uses every space).
        self.owners = owners

    def open(self, name: str) -> BrowsableMemory:
        """The space ``name``; one shared connection per space (memory commands are locked)."""
        name = check_space_name(name)
        with self._lock:
            space = self._open.get(name)
            if space is None:
                space = self._open[name] = self.tables.open_space(_PREFIX + name)
            return space

    def close(self) -> None:
        with self._lock:
            for space in self._open.values():
                space.close()
            self._open.clear()

    def all(self) -> list[MemorySpaceInfo]:
        return [
            MemorySpaceInfo(name=namespace[len(_PREFIX) :], files=count, size=size)
            for namespace, count, size in self.tables.totals(_PREFIX)
        ]

    # ---- owners (RT-089c) ----------------------------------------------------------------------------------------

    def owner(self, name: str) -> str | None:
        """The recorded owner of the space ``name`` (``None``: none recorded, or no owners here)."""
        if self.owners is None:
            return None
        found = self.owners.get(OWNERS, check_space_name(name), SpaceOwner)
        return found.owner if found is not None else None

    def may_use(self, name: str, owner: str | None) -> bool:
        """Whether ``owner`` may list, read and write the space ``name``: its recorded owner, or anybody where there are
        no owners. ``owner`` ``None`` is the desktop's single person, who uses every space; any other value is a user id
        (or a principal without one, which owns nothing)."""
        if self.owners is None or owner is None:
            return True
        return self.owner(name) == owner

    def visible(self, owner: str | None) -> list[MemorySpaceInfo]:
        """The spaces ``owner`` may use (:meth:`may_use`); with owners, one read of them all."""
        found = self.all()
        if self.owners is None or owner is None:
            return found
        mine = {item.space for item in self.owners.list(OWNERS, SpaceOwner) if item.owner == owner}
        return [item for item in found if item.name in mine]

    def ownerless(self) -> list[MemorySpaceInfo]:
        """The spaces with files but without an owner (only where there are owners): nobody but the operator's
        assignment (:meth:`assign`) makes them usable again."""
        if self.owners is None:
            return []
        owned = {item.space for item in self.owners.list(OWNERS, SpaceOwner)}
        return [item for item in self.all() if item.name not in owned]

    def open_for(self, name: str, owner: str | None) -> BrowsableMemory:
        """The space ``name`` for ``owner`` (see :meth:`may_use`), who writes to it or whose run uses it. A space
        without an owner and without files is new: ``owner`` becomes its owner (a user id only). Anything else
        answers ``memory.space_not_found``, also a space that has files but no owner (fail closed)."""
        name = check_space_name(name)
        if self.owners is None or owner is None:
            return self.open(name)
        store = self.owners
        found = store.get(OWNERS, name, SpaceOwner)
        if found is None and _is_user(owner) and not self.open(name).entries():
            # Read before the transaction: the space's rows may live in the same database (SQLite). Every writer
            # where there are owners claims first, so a new space gets its files only after its owner is recorded.
            with store.transaction():
                store.lock(OWNERS, name)
                found = store.get(OWNERS, name, SpaceOwner)
                if found is None:
                    found = SpaceOwner(space=name, owner=owner, how="created")
                    store.put(OWNERS, name, found)
        if found is None or found.owner != owner:
            raise space_not_found(name)
        return self.open(name)

    def assign(self, name: str, owner: str) -> bool:
        """Give the space ``name`` the owner ``owner`` where it has none (the operator's assignment; the caller checks
        that ``owner`` is a member and audits it). ``False``: it has an owner already, which is never replaced."""
        if self.owners is None or not _is_user(owner):
            raise ValueError("An owner is a user id of a workspace with owners")
        name = check_space_name(name)
        store = self.owners
        with store.transaction():
            store.lock(OWNERS, name)
            if store.get(OWNERS, name, SpaceOwner) is not None:
                return False
            store.put(OWNERS, name, SpaceOwner(space=name, owner=owner, how="assigned"))
        return True


def _is_user(owner: str) -> bool:
    """A user id can own a space; a principal without one (``conversations.NO_OWNER``, which holds a space and angle
    brackets) cannot."""
    return bool(owner) and not any(char in owner for char in " <>")
