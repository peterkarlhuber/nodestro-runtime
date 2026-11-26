"""Granted folders as a knowledge source (N-12): the search index of a folder grant with ``index`` set.

The knowledge base (:class:`~nodestro_runtime.knowledge.KnowledgeBase`) indexes the text and PDF files of such a
folder into its search index, one index source per file (``folder:<folder grant id>/<relative path>``,
:func:`source_of`), never into its file store: a folder's files are no knowledge files, they show in no knowledge
list, and a knowledge search never returns them (:attr:`KnowledgeBase.folder_sources`). Agents search the index only
through ``search_files`` (:class:`~nodestro_runtime.folders.FolderReader`), so every search is a call of the folder
tools: the access model decides for the caller at that moment, the run is marked as holding granted content (N-08)
and the activity log names the grant.

The rights stay on the :class:`~nodestro_runtime.folders.FolderGrant`:

- :meth:`FolderIndex.sync` builds the index (first index) and keeps it current: a file whose size, modification time
  or change time differs from the manifest is read again, and its chunks are replaced only when its SHA-256 changed;
  files that are gone, excluded, denied or no longer readable lose their chunks. Every file is read through
  :class:`orkexa.fs.SafeRoot` with the grant's patterns, limits, identity and the default excludes, PDFs in the
  isolated extraction with the grant's PDF budgets (pages, seconds, memory). The access grant is checked again
  inside the knowledge base's write lock before each file's chunks are stored, so a sync that runs while the grant
  is revoked stores nothing more.
- :meth:`FolderIndex.drop` removes a folder's chunks (with their vectors) and its manifest. A revocation through
  :class:`~nodestro_runtime.folder_grants.FolderGrants` or the access API drops the index at once
  (:meth:`FolderIndex.drop_ended`), an expiry at the indexer's next sweep; until then (and for a search already
  running) the folder tools refuse the folder and drop every hit of an ended grant, so a search over a revoked folder
  returns nothing.
- :meth:`FolderIndex.reconcile` runs at the start and then periodically (:class:`FolderIndexer`): it syncs every
  active grant with ``index`` and drops the index of every grant that ended, and every folder chunk without a grant.

Egress (N-08, RT-094, N-12 review H-1): folder text is granted content, fail closed whatever the switches. It is
embedded only when :attr:`KnowledgeBase.folder_embedding_allowed` allows the embedding model's address: a server on
this computer, or one elsewhere only while the provenance switch is on and a grant on ``cloud_provider`` ``ollama``
allows it. Otherwise the text is indexed without vectors (full-text search finds it), a folder search does not embed
its query, and a reindex for such a model removes every folder index first and stores no folder chunk until it ended
(the indexer rebuilds them, without vectors). A search that embeds its query asks the run's check as well
(``RunServices.embedding_check``).

Budgets (N-12 review L-1, L-2): at most :data:`MAX_FILE_CHUNKS` chunks of one file and :data:`MAX_FOLDER_CHUNKS`
chunks or :data:`MAX_FOLDER_BYTES` bytes of files of one folder are indexed; the rest is left out and the state says
``truncated``. A long sync sweeps for ended grants of other folders every :data:`SWEEP_SECONDS`.

Off unless ``NODESTRO_FOLDER_GRANTS=1``, and never in the server profile, like the folder tools. While the switch is
off, the workspace removes every folder index at its start (:meth:`FolderIndex.purge`). See ``docs/folders.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
import os
import posixpath
import threading
import time
from typing import Literal

from orkexa import Chunk
from orkexa.fs import FsError, SafeRoot, TextBudget
from orkexa.retrieval import split_text
from pydantic import AwareDatetime, BaseModel

from .folders import COLLECTION, FolderError, FolderGrant, _is_pdf, _text_of, safe_root
from .knowledge import FOLDER_SOURCE, FolderIndexBusy, KnowledgeBase
from .schemas import now
from .storage import DocumentStore
from .tool_policy import AccessDecision, AccessRequest, AccessResource, Deny, Grant, evaluate

logger = logging.getLogger(__name__)

#: One document per indexed file (key ``<folder grant id>/<hash of the path>``, :class:`FolderIndexFile`).
FILES = "folder_index_files"
#: One document per indexed folder grant (key = its id, :class:`FolderIndexState`).
STATES = "folder_index_state"
#: The file types a folder index reads: text formats and PDFs (others are left out, not refused).
INDEXED_SUFFIXES = (".pdf", ".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml", ".html", ".xml")
#: The most folders one sync keeps queued; deeper folders are then left out and the state says ``truncated``.
MAX_PENDING = 10_000
#: Seconds between two syncs of every indexed folder (:class:`FolderIndexer`).
SYNC_SECONDS = 300.0
#: Seconds between two sweeps for grants that ended (an expiry; a revocation drops at once).
SWEEP_SECONDS = 30.0
#: The most chunks one file adds to the index (the rest of it is left out; bounds the time the write lock is held).
MAX_FILE_CHUNKS = 400
#: The most chunks of one folder's files the index holds.
MAX_FOLDER_CHUNKS = 50_000
#: The most bytes of one folder's files the index reads (their sizes summed).
MAX_FOLDER_BYTES = 512 * 1024 * 1024
#: Codes that end a sync instead of skipping one subfolder: the granted folder itself changed or is gone.
_FATAL = frozenset({"fs.root_changed", "fs.protected_root", "fs.not_found", "fs.not_a_directory", "fs.link"})


def source_of(folder_id: str, path: str) -> str:
    """The index source of one file of a folder grant."""
    return f"{FOLDER_SOURCE}{folder_id}/{path}"


def _prefix(folder_id: str) -> str:
    return f"{FOLDER_SOURCE}{folder_id}/"


def _key(folder_id: str, path: str) -> str:
    return f"{folder_id}/{hashlib.sha256(path.encode('utf-8', 'surrogatepass')).hexdigest()[:40]}"


class FolderIndexFile(BaseModel):
    """One indexed file: what the next sync compares (size, times, content hash) and how many chunks it has."""

    folder_id: str
    path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str
    chunks: int
    indexed_at: AwareDatetime
    #: The file had more than :data:`MAX_FILE_CHUNKS` (or the folder's chunk budget left) chunks; the rest is left out.
    truncated: bool = False


class FolderIndexState(BaseModel):
    """The index of one folder grant: ``indexing`` until the first sync ends, then ``ready``; ``failed`` with a
    ``code`` when the folder could not be read at all (its chunks are then removed)."""

    folder_id: str
    state: Literal["indexing", "ready", "failed"]
    files: int = 0
    chunks: int = 0
    #: Files that could not be read (too large, binary, a PDF over its budget, hard links, changed meanwhile).
    skipped: int = 0
    #: The sync stopped at the grant's file limit, the folder queue's limit or the folder's chunk or byte budget, or a
    #: file was cut at :data:`MAX_FILE_CHUNKS`.
    truncated: bool = False
    code: str | None = None
    synced_at: AwareDatetime | None = None


@dataclass(frozen=True)
class FolderHit:
    """One hit of a folder index search: the file, the chunk's text and, for a PDF, its page."""

    path: str
    text: str
    score: float
    page: int | None = None


class FolderIndex:
    """The search indexes of a workspace's folder grants (module documentation).

    ``records`` returns the access grants and denies active now; ``protected`` is Nodestro's data folder."""

    def __init__(
        self,
        store: DocumentStore,
        knowledge: KnowledgeBase,
        *,
        records: Callable[[], tuple[Sequence[Grant], Sequence[Deny]]],
        protected: str,
        clock: Callable[[], datetime] = now,
        max_folder_chunks: int = MAX_FOLDER_CHUNKS,
        max_folder_bytes: int = MAX_FOLDER_BYTES,
        sweep_seconds: float = SWEEP_SECONDS,
    ) -> None:
        self.store = store
        self.knowledge = knowledge
        self.records = records
        self.protected = protected
        self.clock = clock
        self.max_folder_chunks = max_folder_chunks
        self.max_folder_bytes = max_folder_bytes
        self.sweep_seconds = sweep_seconds
        self._swept = time.monotonic()
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    # ---- rights -------------------------------------------------------------------------------------------------

    def _grant(self, folder: FolderGrant) -> tuple[Grant, Sequence[Deny]] | None:
        """The folder's access grant and the denies, when the grant holds now; ``None`` once it ended."""
        grants, denies = self.records()
        own = next((grant for grant in grants if grant.id == folder.grant_id), None)
        if own is None or not own.active_at(self.clock()):
            return None
        return own, denies

    def _decide(self, folder: FolderGrant, target: str, own: Grant, denies: Sequence[Deny]) -> AccessDecision:
        """Whether the index may hold ``target`` (an absolute path): the grant's folder, every deny that names no
        agent or any agent (fail closed), the global safety limits. Who may search it is decided per call."""
        try:
            request = AccessRequest(
                resource=AccessResource(type="folder", id=target),
                action="read",
                effect="read_internal",
                protected_folders=(self.protected,),
            )
        except ValueError:
            return AccessDecision(allowed=False, kind="safety_limit", source="limit:unresolvable_path")
        # The index serves the grant as a whole; its scope (which agents) is checked when an agent searches.
        whole = own.model_copy(update={"scope": "all", "agents": ()})
        return evaluate(request, grants=[whole], denies=denies, at=self.clock())

    def _active(self, folder: FolderGrant) -> bool:
        return self._grant(folder) is not None

    # ---- state --------------------------------------------------------------------------------------------------

    def files(self, folder_id: str) -> list[FolderIndexFile]:
        return self.store.list_prefix(FILES, f"{folder_id}/", FolderIndexFile)

    def state(self, folder_id: str) -> FolderIndexState | None:
        return self.store.get(STATES, folder_id, FolderIndexState)

    @contextmanager
    def _locked(self, folder_id: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(folder_id, threading.Lock())
        with lock:
            yield

    # ---- removing -----------------------------------------------------------------------------------------------

    def drop(self, folder_id: str) -> int:
        """Remove the index of a folder grant: its chunks (with their vectors), its manifest and its state. Returns
        the chunks removed. Does not wait for a sync of the folder: that one stores nothing more once the grant ended
        (it checks inside the same write lock)."""
        prefix = _prefix(folder_id)
        with self.knowledge.writing():
            sources = {source_of(folder_id, item.path) for item in self.files(folder_id)}
            sources.update(source for source in self.knowledge.chunk_sources() if source.startswith(prefix))
            removed = self.knowledge.remove_sources(sources)
            self.store.delete_prefix(FILES, f"{folder_id}/")
            self.store.delete(STATES, folder_id)
        if removed:
            logger.info("The index of a folder grant was removed (%d chunks)", removed)
        return removed

    def drop_ended(self) -> list[str]:
        """Drop the index of every folder grant whose grant ended (revoked or expired) or that has no ``index``, and
        every folder chunk without a folder grant. Returns the folder grant ids dropped."""
        folders = {folder.id: folder for folder in self.store.list(COLLECTION, FolderGrant)}
        indexed = {state.folder_id for state in self.store.list(STATES, FolderIndexState)}
        indexed.update(item.folder_id for item in self.store.list(FILES, FolderIndexFile))
        for source in self.knowledge.chunk_sources():
            if source.startswith(FOLDER_SOURCE):
                indexed.add(source[len(FOLDER_SOURCE) :].split("/", 1)[0])
        dropped = []
        for folder_id in sorted(indexed):
            folder = folders.get(folder_id)
            if folder is None or not folder.index or not self._active(folder):
                self.drop(folder_id)
                dropped.append(folder_id)
        return dropped

    def _sweep(self) -> None:
        """Drop the indexes of grants that ended, at most every ``sweep_seconds``: a long sync of one folder does not
        keep another folder's expired grant in the index (N-12 review L-2)."""
        if time.monotonic() - self._swept < self.sweep_seconds:
            return
        self._swept = time.monotonic()
        self.drop_ended()

    def purge(self) -> list[str]:
        """Drop every folder index (the switch is off: nothing of a granted folder stays in the index)."""
        indexed = {state.folder_id for state in self.store.list(STATES, FolderIndexState)}
        indexed.update(item.folder_id for item in self.store.list(FILES, FolderIndexFile))
        for source in self.knowledge.chunk_sources():
            if source.startswith(FOLDER_SOURCE):
                indexed.add(source[len(FOLDER_SOURCE) :].split("/", 1)[0])
        for folder_id in sorted(indexed):
            self.drop(folder_id)
        return sorted(indexed)

    # ---- building -----------------------------------------------------------------------------------------------

    def reconcile(self) -> None:
        """Drop the index of every ended grant, then sync every active grant with ``index`` (the start and the
        indexer's periodic run)."""
        self.drop_ended()
        for folder in self.store.list(COLLECTION, FolderGrant):
            if folder.index and self._active(folder):
                try:
                    self.sync(folder.id)
                except Exception:  # one folder never stops the others
                    logger.exception("Indexing a granted folder failed")

    def sync(self, folder_id: str) -> FolderIndexState | None:
        """Bring the index of one folder grant up to date (first index, changes, deletions); ``None`` when the
        folder is not indexed (no such grant, no ``index``, or the grant ended: its index is then dropped)."""
        folder = self.store.get(COLLECTION, folder_id, FolderGrant)
        if folder is None or not folder.index or not self._active(folder):
            self.drop(folder_id)
            return None
        with self._locked(folder_id):
            return self._sync(folder)

    def _sync(self, folder: FolderGrant) -> FolderIndexState | None:
        previous = self.state(folder.id)
        if previous is None or previous.state == "failed":
            self.store.put(STATES, folder.id, FolderIndexState(folder_id=folder.id, state="indexing"))
        try:
            found, truncated = self._walk(folder)
        except _Ended:
            self.drop(folder.id)
            return None
        except (FsError, FolderError) as exc:
            # The granted folder itself cannot be read (replaced, gone, now protected): nothing of it stays.
            self.drop(folder.id)
            failed = FolderIndexState(folder_id=folder.id, state="failed", code=exc.code, synced_at=self.clock())
            if self._active(folder):
                self.store.put(STATES, folder.id, failed)
            return failed
        known = {item.path: item for item in self.files(folder.id)}
        skipped = 0
        root = self._root(folder)
        kept: set[str] = set()
        spent_bytes = spent_chunks = 0
        for path, size in found.items():
            self._sweep()
            room = self.max_folder_chunks - spent_chunks
            spent_bytes += size
            if room <= 0 or spent_bytes > self.max_folder_bytes:
                truncated = True  # the folder's budget is spent: the other files are left out
                break
            try:
                stored, cut = self._index_file(folder, root, path, size, known.get(path), room)
            except _Ended:
                self.drop(folder.id)
                return None
            except FolderIndexBusy:
                # A reindex runs that may not embed folder text: the next sync continues.
                logger.info("A folder index waits for the semantic index to be built")
                return self.state(folder.id)
            if stored is None:
                skipped += 1
                continue
            kept.add(path)
            spent_chunks += stored
            truncated = truncated or cut
        for path in known.keys() - kept:  # gone, excluded, denied, unreadable or above a limit now
            self._remove_file(folder, path)
        files = self.files(folder.id)
        state = FolderIndexState(
            folder_id=folder.id,
            state="ready",
            files=sum(1 for item in files if item.chunks),
            chunks=sum(item.chunks for item in files),
            skipped=skipped,
            truncated=truncated,
            synced_at=self.clock(),
        )
        with self.knowledge.writing():
            ended = not self._active(folder)  # revoked while the last files were read
            if not ended:
                self.store.put(STATES, folder.id, state)
        if ended:
            self.drop(folder.id)
            return None
        return state

    def _root(self, folder: FolderGrant) -> SafeRoot:
        held = self._grant(folder)
        if held is None:
            raise _Ended
        own, denies = held

        def opened(names: tuple[str, ...]) -> None:
            # The names the file system stored for what is opened, not the spelling asked for (N-10 re-review M2).
            if names and not self._decide(folder, os.path.join(folder.path, *names), own, denies).allowed:
                raise FolderError("access.denied", "A person blocked reading this file")

        return safe_root(folder, self.protected, on_open=opened)

    def _walk(self, folder: FolderGrant) -> tuple[dict[str, int], bool]:
        """The files the index should hold now (relative path -> size): text and PDF files the grant's patterns,
        limits and the denies allow, at most the grant's ``max_search_files``."""
        root = self._root(folder)
        limit = folder.limits.max_search_files
        found: dict[str, int] = {}
        pending = [""]
        truncated = False
        while pending:
            current = pending.pop(0)
            self._sweep()
            held = self._grant(folder)
            if held is None:
                raise _Ended
            own, denies = held  # fresh records for every folder

            def allowed(relative: str, own: Grant = own, denies: Sequence[Deny] = denies) -> bool:
                return self._decide(folder, os.path.join(root.path, *relative.split("/")), own, denies).allowed

            if current and not allowed(current):
                continue
            try:
                entries = root.list(current)
            except FolderError:
                if not current:
                    raise
                continue
            except FsError as exc:
                if not current or exc.code in _FATAL:
                    raise
                continue  # one unreadable subfolder does not end the sync
            for entry in entries:
                if entry.kind == "dir":
                    if not allowed(entry.path):
                        continue
                    if len(pending) >= MAX_PENDING:
                        truncated = True
                    else:
                        pending.append(entry.path)
                    continue
                if entry.kind != "file" or entry.ambiguous:
                    continue  # links are never followed, special files never read
                if os.path.splitext(entry.name.casefold())[1] not in INDEXED_SUFFIXES:
                    continue
                if entry.size > folder.limits.max_file_bytes or not allowed(entry.path):
                    continue
                if len(found) >= limit:
                    truncated = True
                    return found, truncated
                found[entry.path] = entry.size
        return found, truncated

    def _index_file(
        self,
        folder: FolderGrant,
        root: SafeRoot,
        path: str,
        size: int,
        known: FolderIndexFile | None,
        room: int = MAX_FOLDER_CHUNKS,
    ) -> tuple[int | None, bool]:
        """Index one file when it changed, with at most ``room`` (and :data:`MAX_FILE_CHUNKS`) chunks. Returns the
        chunks it has and whether some were left out; ``None`` chunks when it could not be read (its old chunks are
        then removed)."""
        try:
            # A hint only, never used to read: the content is read through the path guard and its hash decides.
            info = os.stat(os.path.join(root.path, *path.split("/")), follow_symlinks=False)
        except OSError:
            self._remove_file(folder, path)
            return None, False
        if (
            known is not None
            and known.size == size == info.st_size
            and known.mtime_ns == info.st_mtime_ns
            and known.ctime_ns == info.st_ctime_ns
            and known.chunks <= room
        ):
            return known.chunks, known.truncated  # unchanged
        try:
            data = root.read_bytes(path)
            digest = hashlib.sha256(data).hexdigest()
            entry = FolderIndexFile(
                folder_id=folder.id,
                path=path,
                size=len(data),
                mtime_ns=info.st_mtime_ns,
                ctime_ns=info.st_ctime_ns,
                sha256=digest,
                chunks=known.chunks if known is not None else 0,
                indexed_at=self.clock(),
            )
            if known is not None and known.sha256 == digest and known.chunks <= room:
                entry = entry.model_copy(update={"truncated": known.truncated})
                with self.knowledge.writing():
                    if not self._active(folder):
                        raise _Ended
                    self.store.put(FILES, _key(folder.id, path), entry)  # touched, not changed
                return known.chunks, known.truncated
            chunks = self._chunks(folder, root, path, data)
        except (FsError, FolderError):
            self._remove_file(folder, path)  # binary, too large, a PDF over its budget, changed or denied meanwhile
            return None, False
        limit = min(MAX_FILE_CHUNKS, room)
        cut = len(chunks) > limit
        chunks = chunks[:limit]
        with self.knowledge.writing():
            # Checked inside the write lock a revocation's drop takes: once the grant ended, nothing more is stored.
            if not self._active(folder):
                raise _Ended
            stored = self.knowledge.replace_chunks(source_of(folder.id, path), chunks)
            self.store.put(FILES, _key(folder.id, path), entry.model_copy(update={"chunks": stored, "truncated": cut}))
        return stored, cut

    def _chunks(self, folder: FolderGrant, root: SafeRoot, path: str, data: bytes) -> list[Chunk]:
        source = source_of(folder.id, path)
        name = posixpath.basename(path)
        title = f"{folder.name}/{path}"
        size = self.knowledge.index.max_chars
        overlap = self.knowledge.index.overlap
        if _is_pdf(path):
            limits = folder.limits
            budget = TextBudget(
                max_bytes=limits.max_file_bytes,
                max_chars=5_000_000,
                max_pages=limits.pdf_pages,
                timeout=limits.pdf_seconds,
                max_memory_mb=limits.pdf_memory_mb,
            )
            found = root.extract_text(path, budget=budget)
            return [
                Chunk(text=piece, source=source, title=f"{title}, p. {number}", metadata={"page": str(number)})
                for number, page in enumerate(found.pages or (), start=1)
                if page.strip()
                for piece in split_text(page, max_chars=size, overlap=overlap)
            ]
        text = _text_of(data, name)
        return [
            Chunk(text=piece, source=source, title=title, metadata={})
            for piece in split_text(text, max_chars=size, overlap=overlap)
            if piece.strip()
        ]

    def _remove_file(self, folder: FolderGrant, path: str) -> None:
        with self.knowledge.writing():
            self.knowledge.remove_sources([source_of(folder.id, path)])
            self.store.delete(FILES, _key(folder.id, path))

    # ---- searching ----------------------------------------------------------------------------------------------

    def ready(self, folder: FolderGrant) -> bool:
        """Whether ``folder``'s index can answer a search (its first sync ended)."""
        state = self.state(folder.id)
        return folder.index and state is not None and state.state == "ready"

    def search(
        self,
        folder: FolderGrant,
        query: str,
        *,
        below: str = "",
        k: int = 50,
        before_embed: Callable[[str | None], None] | None = None,
    ) -> list[FolderHit] | None:
        """The best chunks of ``folder``'s files (below the relative folder ``below``) for ``query``, ranked like
        the knowledge search; ``None`` when the folder has no ready index. The caller checks the access model."""
        if not self.ready(folder) or not self._active(folder):
            return None
        prefix = f"{below}/" if below else ""
        sources = [
            source_of(folder.id, item.path)
            for item in self.files(folder.id)
            if item.chunks and item.path.startswith(prefix)
        ]
        hits = self.knowledge.search_sources(query, sources, k, before_embed=before_embed)
        start = len(_prefix(folder.id))
        found = []
        for hit in hits:
            if not hit.source.startswith(_prefix(folder.id)):
                continue
            page = (hit.metadata or {}).get("page")
            found.append(
                FolderHit(
                    path=hit.source[start:],
                    text=hit.text,
                    score=hit.score,
                    page=int(page) if page and str(page).isdigit() else None,
                )
            )
        return found


class _Ended(Exception):
    """The folder grant ended while its index was built: the sync stops and drops the index."""


class FolderIndexer:
    """Keeps the folder indexes current in a background thread: :meth:`FolderIndex.reconcile` at the start and every
    ``sync_seconds``, a sweep for ended grants every ``sweep_seconds``, and a sync of one folder when asked
    (:meth:`wake`, after a grant)."""

    def __init__(
        self, index: FolderIndex, *, sync_seconds: float = SYNC_SECONDS, sweep_seconds: float = SWEEP_SECONDS
    ) -> None:
        self.index = index
        self.sync_seconds = sync_seconds
        self.sweep_seconds = sweep_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._asked: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="nodestro-folder-index", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def wake(self, folder_id: str | None = None) -> None:
        """Sync ``folder_id`` soon (``None``: every folder)."""
        with self._lock:
            self._asked.add(folder_id or "*")
        self._wake.set()

    def _loop(self) -> None:
        next_sync = 0.0
        while not self._stop.is_set():
            try:
                if time.monotonic() >= next_sync:
                    self.index.reconcile()
                    next_sync = time.monotonic() + self.sync_seconds
                else:
                    self.index.drop_ended()
                with self._lock:
                    asked, self._asked = self._asked, set()
                if "*" in asked:
                    self.index.reconcile()
                else:
                    for folder_id in sorted(asked):
                        self.index.sync(folder_id)
            except Exception:  # the indexer never stops; a closed store ends it at the next stop()
                if self._stop.is_set():
                    return
                logger.exception("The folder indexer failed; it tries again later")
            self._wake.wait(self.sweep_seconds)
            self._wake.clear()


__all__ = [
    "FILES",
    "INDEXED_SUFFIXES",
    "MAX_FILE_CHUNKS",
    "MAX_FOLDER_BYTES",
    "MAX_FOLDER_CHUNKS",
    "STATES",
    "SWEEP_SECONDS",
    "SYNC_SECONDS",
    "FolderHit",
    "FolderIndex",
    "FolderIndexFile",
    "FolderIndexState",
    "FolderIndexer",
    "source_of",
]
