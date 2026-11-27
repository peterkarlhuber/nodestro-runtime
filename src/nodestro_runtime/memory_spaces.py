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

Consents (RT-089d): owning a space is not enough for a workflow to use it. A workflow may be shared and changed by
other members, so a run (or an agent test) opens a person's space only after that person allowed exactly this content
to use it: a :class:`MemoryConsent` names the space, its owner, the workflow (or the tested agent configuration) and
the SHA-256 digest of its content (:mod:`nodestro_runtime.memory_consents`). Any change of the content changes the
digest, so the consent no longer applies to it; nothing grants a consent by itself, and the owner revokes one at any
time. Without a consent the step fails with ``memory.consent_required`` (fail closed). The desktop needs none.

Entries (N-24): every file of a space is an entry the owner sees, edits and deletes (:meth:`BrowsableMemory.entries`,
:meth:`~BrowsableMemory.write`, :meth:`~BrowsableMemory.remove`), with the time and origin of its last write from the
Orkexa backend (:meth:`orkexa.memory.MemoryBackend.list_entries`): ``tool`` when an agent's memory tool wrote it,
:data:`ORIGIN_USER` when a person wrote it by an explicit action (the HTTP API), ``None`` when unknown (written before
origins were kept). Only an explicit action of a person writes with :data:`ORIGIN_USER`; no model tool does. Each
entry and space names its ``scope``: today every space is one person's (``user``); ``project`` and ``agent`` are
reserved for spaces of a project or of one agent.
"""

from __future__ import annotations

from abc import abstractmethod
from datetime import UTC, datetime
import hashlib
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


#: Whose a memory space is (N-24): every space today is one person's (``user``); ``project`` and ``agent`` are reserved.
MemoryScope = Literal["user", "project", "agent"]
#: The origin of a file a person wrote by an explicit action (the HTTP API), never by a model tool (N-24).
ORIGIN_USER = "user"
#: The origin of a file that came with an imported project (the memory seed of a standalone workflow).
ORIGIN_IMPORT = "import"


class MemoryFile(BaseModel):
    """One entry of a memory space (N-24: with the time and origin of its last write)."""

    path: str
    size: int
    #: When it was last written; ``None`` when the storage keeps no time.
    updated_at: datetime | None = None
    #: Who wrote it last: ``tool`` (an agent's memory tool), ``user`` (a person, explicitly), ``None`` (unknown).
    origin: str | None = None
    scope: MemoryScope = "user"


class MemorySpaceInfo(BaseModel):
    name: str
    files: int
    size: int
    scope: MemoryScope = "user"


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


#: The collection of the owners' consents (RT-089d): :func:`consent_key` -> :class:`MemoryConsent`.
CONSENTS = "memory_space_consents"
#: What a consent allows to use a space: a saved workflow in one content, or one agent configuration in an agent test.
ConsentSubject = Literal["workflow", "agent"]


class MemoryConsent(BaseModel):
    """The owner of a memory space allows one content to use it (RT-089d): the workflow ``workflow_id`` (or, for an
    agent test, an agent configuration) whose content has the digest ``digest``. It applies to nothing else: a changed
    workflow has another digest and needs a new consent."""

    id: str
    space: str
    owner: str
    subject: ConsentSubject
    #: The workflow (``subject`` ``workflow``); ``None`` for an agent configuration.
    workflow_id: str | None = None
    #: The saved version the consent was given for and the workflow's name then (shown to the owner; not checked).
    workflow_version: int | None = None
    workflow_name: str = ""
    #: The SHA-256 digest of the content (:func:`nodestro_runtime.memory_consents.workflow_digest`, ``agent_digest``).
    digest: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def consent_key(space: str, subject: ConsentSubject, workflow_id: str | None, digest: str) -> str:
    """A consent's key: one per space, subject, workflow and digest (the same consent given twice is one)."""
    return f"{space}|{subject}|{workflow_id or ''}|{digest}"


def consent_id(key: str) -> str:
    return "mcn_" + hashlib.sha256(key.encode()).hexdigest()[:24]


def consent_required(
    name: str, *, workflow_id: str | None = None, workflow_name: str = "", subject: ConsentSubject = "workflow"
) -> NodestroError:
    """The answer for the owner's own space that this content may not use (RT-089d): only the owner gets it (any
    other caller gets :func:`space_not_found`), so it reveals nothing they do not know."""
    if subject == "agent":
        return NodestroError(
            "memory.consent_required",
            f"The memory space {name} may be used by this agent configuration only after you allowed it",
            space=name,
            workflow_id="",
            workflow="",
        )
    return NodestroError(
        "memory.consent_required",
        f"The memory space {name} may be used by the workflow {workflow_name or workflow_id} only after its owner "
        "allowed this version of it",
        space=name,
        workflow_id=workflow_id or "",
        workflow=workflow_name or workflow_id or "",
    )


def space_not_found(name: str) -> NodestroError:
    """The answer for a space the caller may not use: the same as for one that does not exist."""
    return NodestroError("memory.space_not_found", f"There is no memory space {name}", name=name)


class BrowsableMemory(MemoryBackend):
    """A memory with read access for the memory browser (any Orkexa storage)."""

    def entries(self) -> list[MemoryFile]:
        """Every file of the space, sorted by path, with the time and origin of its last write (N-24)."""
        return [
            MemoryFile(path=entry.path, size=entry.size, updated_at=entry.updated_at, origin=entry.origin)
            for entry in self.list_entries(MEMORY_ROOT)
        ]

    def read(self, path: str) -> str:
        key = path.removeprefix(MEMORY_ROOT).strip("/")
        with self._lock:
            text = self._read(key)
        if text is None:
            raise KeyError(path)
        return text

    def write(self, path: str, text: str, *, origin: str | None = ORIGIN_USER) -> None:
        """Create or overwrite a file (the ``create`` command validates the path); a person's explicit write by
        default (:data:`ORIGIN_USER`)."""
        self.create(path, text, origin=origin)

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

    # ---- consents (RT-089d) --------------------------------------------------------------------------------------

    def needs_consent(self, name: str, owner: str | None) -> bool:
        """Whether a use of the space ``name`` for ``owner`` must first find the owner's consent: only where there are
        owners, and only for a space that is ``owner``'s or that ``owner`` would create (no owner, no files). Any other
        use is refused as a space that does not exist (:meth:`open_for`) and reveals nothing."""
        if self.owners is None or owner is None or not _is_user(owner):
            return False
        name = check_space_name(name)
        recorded = self.owner(name)
        if recorded is not None:
            return recorded == owner
        return not self.open(name).entries()

    def consented(
        self, name: str, owner: str | None, subject: ConsentSubject, workflow_id: str | None, digest: str
    ) -> bool:
        """Whether ``owner`` allowed the content with ``digest`` (of the workflow ``workflow_id``, or an agent
        configuration) to use their space ``name``. Always true without owners (the desktop)."""
        if self.owners is None or owner is None:
            return True
        name = check_space_name(name)
        if not _is_user(owner) or self.owner(name) != owner:
            return False
        found = self.owners.get(CONSENTS, consent_key(name, subject, workflow_id, digest), MemoryConsent)
        return found is not None and found.owner == owner

    def grant(
        self,
        name: str,
        owner: str,
        subject: ConsentSubject,
        digest: str,
        *,
        workflow_id: str | None = None,
        workflow_version: int | None = None,
        workflow_name: str = "",
    ) -> MemoryConsent:
        """The owner's explicit consent (the caller audits it): ``owner`` must own the space ``name`` or create it now
        (as its first writer, :meth:`open_for`); anything else answers ``memory.space_not_found``."""
        if self.owners is None or not _is_user(owner):
            raise ValueError("A consent is given by a user who owns a space of a workspace with owners")
        if subject == "workflow" and not workflow_id:
            raise ValueError("A workflow's consent names the workflow")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("A consent names a SHA-256 digest")
        name = check_space_name(name)
        self.open_for(name, owner)  # claims a new space, refuses another person's
        key = consent_key(name, subject, workflow_id if subject == "workflow" else None, digest)
        consent = MemoryConsent(
            id=consent_id(key),
            space=name,
            owner=owner,
            subject=subject,
            workflow_id=workflow_id if subject == "workflow" else None,
            workflow_version=workflow_version,
            workflow_name=workflow_name[:200],
            digest=digest,
        )
        self.owners.put(CONSENTS, key, consent)
        return consent

    def consents(self, owner: str | None) -> list[MemoryConsent]:
        """The consents ``owner`` gave, oldest first (only for spaces they still own); none without owners."""
        if self.owners is None or owner is None:
            return []
        mine = {item.space for item in self.owners.list(OWNERS, SpaceOwner) if item.owner == owner}
        found = [
            item for item in self.owners.list(CONSENTS, MemoryConsent) if item.owner == owner and item.space in mine
        ]
        return sorted(found, key=lambda item: (item.at, item.id))

    def revoke(self, consent: str, owner: str) -> MemoryConsent | None:
        """Withdraw ``owner``'s consent with the id ``consent`` (another person's or an unknown id: ``None``). A run
        that already reads the space keeps its step; every later step and child run is refused."""
        if self.owners is None:
            return None
        for key, item in self.owners.items(CONSENTS, MemoryConsent):
            if item.id == consent and item.owner == owner:
                self.owners.delete(CONSENTS, key)
                return item
        return None

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
