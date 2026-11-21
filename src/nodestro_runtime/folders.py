"""Granted folders that agents read (N-10): :class:`FolderGrant` and the reader of the folder tools.

A person grants a folder for reading (:mod:`nodestro_runtime.folder_grants`, the only writer). The grant is an
access grant of the access model v2 (N-06, :class:`~nodestro_runtime.tool_policy.Grant`, resource ``folder``,
action ``read``) plus what only a folder needs: a name the agents use, include and exclude patterns, limits and
the identity of the folder when it was granted. Duration, expiry, scope, mode and revocation are the access
grant's; there is no second approval logic.

The tools ``list_folder``, ``read_file`` and ``search_files`` (:mod:`nodestro_runtime.folder_tools`) read through
:class:`FolderReader`. Every call

- evaluates the access model for the exact folder or file (:func:`~nodestro_runtime.tool_policy.evaluate` with the
  folder grant's own access grant and every deny), so a revocation, an expiry or a deny on a subfolder applies at the
  next call, and the global safety limits (system folders, Nodestro's data folder) always hold;
- opens the folder through :class:`orkexa.fs.SafeRoot` (no links, no ``..``, no absolute paths, no hard links, no
  special files, blocked credential folders, the data folder protected by name and identity, the folder's identity
  as granted);
- writes one event to the activity log (N-13): source ``file``, action ``folder.listed``, ``folder.read`` or
  ``folder.searched``, resource ``folder_grant`` with the grant's id, the outcome and the refusal code. The log never
  names a path or a file.

Everything is off unless ``NODESTRO_FOLDER_GRANTS=1`` (:data:`FOLDERS_ENV`), and the tools run only on the desktop
and in Nodestro Local Docker, never in the server profile (``tool_spec.LIST_FOLDER_SPEC``). See ``docs/folders.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import os
import time
from typing import Any, Literal

from orkexa.fs import PDF_SUFFIXES, FsError, SafeRoot, TextBudget
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from . import activity
from .storage import DocumentStore
from .tool_policy import (
    AccessDecision,
    AccessMode,
    AccessRequest,
    AccessResource,
    Deny,
    Grant,
    evaluate,
)

#: The switch of folder grants and the folder tools. Off unless set to ``1``; activating it is an owner decision
#: after the independent review of N-10.
FOLDERS_ENV = "NODESTRO_FOLDER_GRANTS"
#: The collection of :class:`FolderGrant` documents (key = id).
COLLECTION = "folder_grants"
#: The largest file a grant may allow (``orkexa.fs.DEFAULT_MAX_FILE_BYTES``).
MAX_FILE_BYTES = 50 * 1024 * 1024

Operation = Literal["list", "read", "search"]
_ACTIONS: dict[str, str] = {"list": "folder.listed", "read": "folder.read", "search": "folder.searched"}


def folders_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether folder grants are switched on (``NODESTRO_FOLDER_GRANTS=1``)."""
    return (environ if environ is not None else os.environ).get(FOLDERS_ENV, "").strip() == "1"


class FolderLimits(BaseModel):
    """What one call of the folder tools may read. The defaults are the limits a grant gets unless the person sets
    lower or higher ones within the bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The largest file ``read_file`` reads and ``search_files`` searches (bytes).
    max_file_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=MAX_FILE_BYTES)
    #: The most entries one listing reads (hidden ones count too).
    max_entries: int = Field(default=2000, ge=1, le=10_000)
    #: The most files one search reads.
    max_search_files: int = Field(default=2000, ge=1, le=20_000)
    #: The most matches one search returns.
    max_results: int = Field(default=50, ge=1, le=500)
    #: The most characters one ``read_file`` call returns (the next call continues at ``offset``).
    max_chars: int = Field(default=50_000, ge=1000, le=500_000)
    #: PDF budgets: pages, seconds and memory of the isolated extraction (``orkexa.fs.TextBudget``).
    pdf_pages: int = Field(default=200, ge=1, le=500)
    pdf_seconds: float = Field(default=20.0, gt=0, le=60)
    pdf_memory_mb: int = Field(default=256, ge=64, le=1024)


class FolderGrant(BaseModel):
    """A folder granted for reading (see the module documentation). Never deleted: it ends with its access grant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    #: The name the agents use (``list_folder(folder=...)``); unique among the active grants, compared case-folded.
    name: str = Field(min_length=1, max_length=80)
    #: The canonical folder (links resolved; on Windows the final path).
    path: str = Field(min_length=1, max_length=4096)
    #: ``(device, inode)`` or ``(volume, file index)`` of the folder when it was granted: a folder that replaced it
    #: is refused (``fs.root_changed``).
    identity: tuple[int, int]
    #: Folder grants are read-only.
    access: Literal["read"] = "read"
    include: tuple[str, ...] = Field(default=(), max_length=100)
    exclude: tuple[str, ...] = Field(default=(), max_length=100)
    limits: FolderLimits = FolderLimits()
    #: The person asked for a search index of the folder. Stored only: indexing granted folders is a later step.
    index: bool = False
    #: The access grant (N-06) that holds duration, expiry, scope, mode and revocation.
    grant_id: str
    created_by: str
    created_at: AwareDatetime


@dataclass(frozen=True)
class FolderCaller:
    """Who reads, as the access model and the activity log name it."""

    #: ``<workflow id>/<node id>`` (:func:`~nodestro_runtime.tool_policy.agent_ref`), ``None`` outside a run.
    agent: str | None = None
    #: The agent's configured name, for the activity log.
    agent_name: str | None = None
    mode: AccessMode = "supervised"
    #: The conversation a ``conversation`` grant names (the conversation of an answer, else the top-level run).
    conversation_id: str | None = None
    offline: bool = False
    actor: str | None = None
    run_id: str | None = None
    node_id: str | None = None


class FolderError(Exception):
    """A refused or failed folder call: ``code`` is an ``orkexa.fs`` code, an ``access.*`` code or a ``folder.*``
    code; the message never names an absolute path."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text_of(data: bytes, shown: str) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:8192]:
        raise FsError("fs.unsupported_type", "The file is binary, not text", path=shown)
    return data.decode("utf-8-sig", errors="replace")


@dataclass(frozen=True)
class FileText:
    """The text of one file as ``read_file`` returns it."""

    path: str
    kind: Literal["text", "pdf"]
    size: int
    text: str
    pages: int | None = None


class FolderReader:
    """The folder tools' access for one caller: lists, reads and searches granted folders (module documentation).

    ``records`` returns the access grants and denies active now (read and handed in by the
    workspace: this module never writes records). ``protected`` is Nodestro's data folder. ``on_read`` is told the
    folder name, grant id and relative path of every file read, so the run can name its sources."""

    def __init__(
        self,
        store: DocumentStore,
        *,
        records: Callable[[], tuple[Sequence[Grant], Sequence[Deny]]],
        protected: str,
        caller: FolderCaller,
        clock: Callable[[], datetime],
        on_read: Callable[[FolderGrant, FileText], None] | None = None,
    ) -> None:
        self.store = store
        self.records = records
        self.protected = protected
        self.caller = caller
        self.clock = clock
        self.on_read = on_read

    # ---- what the caller may use ----------------------------------------------------------------------------

    def folders(self) -> list[FolderGrant]:
        """The folders this caller may read now, by name (the paths stay with the person)."""
        grants, denies = self.records()
        active = {grant.id: grant for grant in grants}
        found: dict[str, FolderGrant] = {}
        for folder in self.store.list(COLLECTION, FolderGrant):
            own = active.get(folder.grant_id)
            if own is not None and self._decide(folder, folder.path, own, denies).allowed:
                found.setdefault(folder.name.casefold(), folder)
        return sorted(found.values(), key=lambda item: item.name.casefold())

    def _decide(self, folder: FolderGrant, target: str, own: Grant, denies: Sequence[Deny]) -> AccessDecision:
        try:
            request = AccessRequest(
                resource=AccessResource(type="folder", id=target),
                action="read",
                effect="read_internal",
                agent=self.caller.agent,
                mode=self.caller.mode,
                conversation_id=self.caller.conversation_id,
                offline=self.caller.offline,
                protected_folders=(self.protected,),
            )
        except ValueError:
            return AccessDecision(allowed=False, kind="safety_limit", source="limit:unresolvable_path")
        # Only the folder grant's own access grant opens it (its scope, mode and expiry), every deny holds.
        return evaluate(request, grants=[own], denies=denies, at=self.clock())

    def _check(self, folder: FolderGrant, target: str) -> None:
        """Refuse unless the access model allows reading ``target`` (an absolute path) now."""
        grants, denies = self.records()
        own = next((grant for grant in grants if grant.id == folder.grant_id), None)
        if own is None:
            raise FolderError("access.missing_grant", "The folder is no longer granted (revoked or expired)")
        found = self._decide(folder, target, own, denies)
        if not found.allowed:
            raise FolderError(found.code or "access.missing_grant", _REFUSALS.get(found.kind or "", _REFUSED))

    def _find(self, name: str) -> FolderGrant:
        """The folder grant of ``name``: the active one, else the newest that ended (its calls are then refused as
        no longer granted, not as unknown)."""
        wanted = name.strip().casefold()
        grants, _ = self.records()
        active = {grant.id for grant in grants}
        named = [folder for folder in self.store.list(COLLECTION, FolderGrant) if folder.name.casefold() == wanted]
        named.sort(key=lambda folder: (folder.grant_id in active, folder.created_at, folder.id), reverse=True)
        if not named:
            raise FolderError("folder.unknown", "No granted folder has this name; call list_folder without a folder")
        return named[0]

    def _root(self, folder: FolderGrant) -> SafeRoot:
        limits = folder.limits
        return SafeRoot(
            folder.path,
            include=folder.include,
            exclude=folder.exclude,
            protected=[self.protected],
            max_file_bytes=limits.max_file_bytes,
            max_entries=limits.max_entries,
            expect_identity=folder.identity,
        )

    @contextmanager
    def _call(self, operation: Operation) -> Iterator[list[FolderGrant | None]]:
        """One logged call: yields a slot for the grant (filled once it is found) and logs the outcome."""
        slot: list[FolderGrant | None] = [None]
        outcome: activity.Outcome = "success"
        code: str | None = None
        try:
            yield slot
        except (FolderError, FsError) as exc:
            outcome, code = "denied", exc.code
            raise
        except Exception:
            outcome, code = "failure", "folder.failed"
            raise
        finally:
            found, caller = slot[0], self.caller
            activity.add(
                self.store,
                "file",
                _ACTIONS[operation],
                actor=caller.actor,
                agent=caller.agent_name,
                resource_type="folder_grant",
                resource_id=found.id if found is not None else "",
                outcome=outcome,
                code=code,
                run_id=caller.run_id,
                node_id=caller.node_id,
                conversation_id=caller.conversation_id,
            )

    def _open(self, slot: list[FolderGrant | None], name: str) -> tuple[FolderGrant, SafeRoot]:
        folder = self._find(name)
        slot[0] = folder
        self._check(folder, folder.path)
        return folder, self._root(folder)

    def _allowed_below(self, folder: FolderGrant) -> Callable[[str], bool]:
        """Whether a folder below the grant may be read now (a deny on it is left out of listings and searches)."""
        grants, denies = self.records()
        own = next((grant for grant in grants if grant.id == folder.grant_id), None)
        return lambda target: own is not None and self._decide(folder, target, own, denies).allowed

    # ---- the three operations -------------------------------------------------------------------------------

    def list(self, name: str, path: str = "") -> tuple[FolderGrant, list[dict[str, Any]]]:
        """The entries of a folder below the grant (``""``: the granted folder), each with ``name``, ``path``,
        ``kind`` (``file``, ``dir``, ``link``, ``other``) and ``size``. Subfolders a deny blocks are left out."""
        with self._call("list") as slot:
            folder, root = self._open(slot, name)
            parts = root.check(path, kind="dir")
            self._check(folder, _join(root, parts))
            allowed = self._allowed_below(folder)
            entries = [
                {"name": entry.display, "path": entry.path, "kind": entry.kind, "size": entry.size}
                for entry in root.list(path)
                if entry.kind != "dir" or allowed(_join(root, entry.path))
            ]
            return folder, entries

    def read(self, name: str, path: str) -> tuple[FolderGrant, FileText]:
        """The text of a file (text or PDF) below the grant, within the grant's limits."""
        with self._call("read") as slot:
            folder, root = self._open(slot, name)
            parts = root.check(path, kind="file")
            self._check(folder, _join(root, parts))
            found = _read(root, "/".join(parts), folder.limits)
            self._check(folder, _join(root, parts))  # revoked while a long PDF was read: nothing is returned
            if self.on_read is not None:
                self.on_read(folder, found)
            return folder, found

    def search(self, name: str, query: str, path: str = "") -> tuple[FolderGrant, dict[str, Any]]:
        """Files below ``path`` whose name or text contains ``query`` (case-insensitive): ``matches`` with
        ``path``, ``line`` and ``snippet`` (``None`` for a match by name), ``files_searched`` and ``truncated``.
        PDFs are found by name only. A search stops at the grant's limits and after :data:`SEARCH_SECONDS`."""
        needle = query.strip().casefold()
        with self._call("search") as slot:
            folder, root = self._open(slot, name)
            if not needle or len(needle) > 200:
                raise FolderError("folder.invalid_query", "Search for 1 to 200 characters")
            limits = folder.limits
            start = "/".join(root.check(path, kind="dir"))
            self._check(folder, _join(root, start))
            matches: list[dict[str, Any]] = []
            searched = 0
            pending = [start]
            deadline = time.monotonic() + SEARCH_SECONDS
            truncated = False
            while pending and not truncated:
                self._check(folder, folder.path)  # a revocation stops a long search at the next folder
                allowed = self._allowed_below(folder)
                for entry in root.list(pending.pop(0)):
                    if entry.kind == "dir":
                        if allowed(_join(root, entry.path)):
                            pending.append(entry.path)
                        continue
                    if entry.kind != "file":
                        continue  # links are never followed, special files never read
                    if needle in entry.name.casefold():
                        matches.append({"path": entry.path, "line": None, "snippet": None})
                    elif entry.size <= limits.max_file_bytes and not _is_pdf(entry.path):
                        searched += 1
                        try:
                            text = _text_of(root.read_bytes(entry.path), entry.path)
                        except FsError:
                            continue  # hard links, binary files, files that changed meanwhile: not searched
                        hit = _first_line(text, needle)
                        if hit is not None:
                            matches.append({"path": entry.path, "line": hit[0], "snippet": hit[1]})
                    if (
                        len(matches) >= limits.max_results
                        or searched >= limits.max_search_files
                        or time.monotonic() > deadline
                    ):
                        truncated = True
                        break
            return folder, {"matches": matches, "files_searched": searched, "truncated": truncated}


#: The longest one search may take (seconds); it then returns what it found as ``truncated``.
SEARCH_SECONDS = 30.0
_REFUSED = "Reading this folder is not allowed"
_REFUSALS = {
    "missing_grant": "The folder is not granted to this agent here (or the grant has ended)",
    "denied": "A person blocked reading this folder",
    "safety_limit": "System folders and Nodestro's own data are never open to agents",
}


def _join(root: SafeRoot, parts: Sequence[str] | str) -> str:
    names = parts.split("/") if isinstance(parts, str) else list(parts)
    return os.path.join(root.path, *[name for name in names if name])


def _is_pdf(path: str) -> bool:
    return os.path.splitext(path.casefold())[1] in PDF_SUFFIXES


def _read(root: SafeRoot, path: str, limits: FolderLimits) -> FileText:
    if _is_pdf(path):
        budget = TextBudget(
            max_bytes=limits.max_file_bytes,
            max_chars=5_000_000,
            max_pages=limits.pdf_pages,
            timeout=limits.pdf_seconds,
            max_memory_mb=limits.pdf_memory_mb,
        )
        found = root.extract_text(path, budget=budget)
        return FileText(found.path, "pdf", found.size, found.text, len(found.pages or ()))
    data = root.read_bytes(path)
    return FileText(path, "text", len(data), _text_of(data, path))


def _first_line(text: str, needle: str) -> tuple[int, str] | None:
    for number, line in enumerate(text.splitlines(), 1):
        at = line.casefold().find(needle)
        if at >= 0:
            start = max(0, at - 80)
            return number, line[start : at + len(needle) + 80].strip()
    return None


__all__ = [
    "COLLECTION",
    "FOLDERS_ENV",
    "MAX_FILE_BYTES",
    "SEARCH_SECONDS",
    "FileText",
    "FolderCaller",
    "FolderError",
    "FolderGrant",
    "FolderLimits",
    "FolderReader",
    "folders_enabled",
]
