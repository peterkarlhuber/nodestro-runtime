"""Files uploaded as workflow inputs: kept for the runs that use them, never knowledge by themselves.

A file chosen in the run dialog, a test case, a schedule or sent to a published API is an
*input file*: it belongs to the runs (and test cases, schedules, batches) that reference it
and is not searchable knowledge. Taking a file over into knowledge is a separate,
confirmed step (:mod:`nodestro_runtime.adoption`).

Storage reuses what exists:

- the bytes live in an ``orkexa`` :class:`~orkexa.LocalFileStore` below
  ``<data dir>/inputs`` (content-addressed, so identical content is stored once, atomic
  writes, owner-only permissions) and not in the SQLite database;
- the metadata (name, format, size, checks, structure) is a document per upload in the
  collection ``input_files``; ids look like ``in_<12 hex>``;
- what Nodestro reads from a file (:func:`nodestro_runtime.file_formats.extract`) is computed once
  per content and kept next to the bytes, so every agent, sub-workflow and later run of
  the same file reuses it without reading the file again.

Runs reference input files by id in their form values and ``file_ids``. A file stays as
long as a run, test case, schedule or batch references it; a paused run therefore finds
its files after a restart. :meth:`InputFiles.cleanup` removes files nobody references
(after a grace period for files that were uploaded but never used) and, on request, the
input files of finished runs older than a number of days. Files of queued, running or
paused runs, of test cases and of schedules are never removed.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import Any, Literal, Protocol
import uuid

from orkexa import LocalFileStore, StoredFile
from pydantic import BaseModel, Field

from . import documents
from .file_formats import (
    MAX_UPLOAD_MB,
    ArchiveEntry,
    Extracted,
    FileFormatError,
    detect,
    extract,
    extract_member,
)
from .forms import FileRef
from .storage import Store

COLLECTION = "input_files"
PREFIX = "in_"
INPUT_ID = re.compile(r"^in_[0-9a-f]{12}$")
_REFERENCE = re.compile(r"in_[0-9a-f]{12}")
#: Collections whose documents reference input files.
_HOLDERS = ("runs", "benchmark_suites", "schedules", "batches")
#: Uploads that no run uses are kept this long (the run dialog may still be open).
GRACE = timedelta(hours=24)
_CACHE = 32

Origin = Literal["studio", "api", "test", "schedule", "batch"]


class FileStoreLike(Protocol):
    def __contains__(self, file_id: object) -> bool: ...

    def info(self, file_id: str) -> StoredFile: ...

    def list(self, query: str | None = None) -> list[StoredFile]: ...


def is_input_id(value: object) -> bool:
    return isinstance(value, str) and bool(INPUT_ID.match(value))


class InputFile(BaseModel):
    """One uploaded input file (its metadata; the bytes are in the blob store)."""

    id: str
    #: The id of the bytes in the blob store (identical content shares one blob).
    blob: str
    name: str
    format: str
    group: str
    media_type: str
    size: int
    sha256: str
    created_at: datetime
    origin: Origin = "studio"
    #: ``local`` or ``api:<key id>``.
    uploaded_by: str = "local"
    #: Pages, sheets, rows, columns, width, height, entries ... (see :class:`~nodestro_runtime.file_formats.Extracted`).
    info: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    #: A short description of the structure (the directory tree of an archive).
    outline: str = ""
    #: Length of the readable text.
    chars: int = 0
    #: ZIP archives: the entries.
    entries: list[ArchiveEntry] = Field(default_factory=list)
    #: The blob with the cached extraction (JSON).
    extract_blob: str | None = None
    #: Knowledge files made from this file (or entries of it) by a confirmed adoption.
    knowledge_ids: list[str] = Field(default_factory=list)


class InputFileOut(BaseModel):
    """An input file for the interface (without the internal blob ids)."""

    id: str
    name: str
    format: str
    group: str
    media_type: str
    size: int
    created_at: datetime
    origin: str
    info: dict[str, Any]
    notes: list[str]
    outline: str
    chars: int
    entries: list[ArchiveEntry]
    knowledge_ids: list[str]

    @classmethod
    def of(cls, item: InputFile) -> InputFileOut:
        return cls(**item.model_dump(exclude={"blob", "sha256", "extract_blob", "uploaded_by"}))


class StorageInfo(BaseModel):
    files: int = 0
    bytes: int = 0
    #: Files that no run, test case, schedule or batch uses.
    unreferenced: int = 0
    #: Files of runs that are queued, running or paused (never removed).
    in_use: int = 0


class CleanupResult(BaseModel):
    removed: int = 0
    freed_bytes: int = 0
    kept_in_use: int = 0


class _Blobs(LocalFileStore):
    """The bytes of input files; any format (the library's ``add`` accepts only what Claude reads)."""

    def put(self, data: bytes, *, name: str, media_type: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            if self._entry(digest[:16]) is None:
                self._save(
                    StoredFile(
                        id=digest[:16],
                        name=name,
                        media_type=media_type,
                        size=len(data),
                        sha256=digest,
                        created_at=datetime.now(timezone.utc),
                    ),
                    data,
                )
        return digest[:16]


class InputFiles:
    """Upload, read, reference and clean up the input files of runs."""

    def __init__(
        self,
        store: Store,
        root: Path,
        *,
        ocr: Callable[[], documents.OcrRunner | None] = lambda: None,
    ) -> None:
        """``ocr`` returns the OCR for scanned PDFs when it is switched on (``None``: off)."""
        self.store = store
        self.root = root
        self.blobs = _Blobs(root)
        self.ocr = ocr
        self._lock = threading.RLock()
        self._extracted: OrderedDict[str, Extracted] = OrderedDict()
        self._members: OrderedDict[tuple[str, str], tuple[ArchiveEntry, Extracted, bytes]] = OrderedDict()

    # ---- upload ------------------------------------------------------------------------

    def add(
        self,
        data: bytes,
        *,
        name: str,
        media_type: str | None = None,
        origin: Origin = "studio",
        uploaded_by: str = "local",
        limit_mb: int | None = None,
    ) -> InputFile:
        """Check, read and store an upload; raises :class:`~nodestro_runtime.file_formats.FileFormatError`."""
        name = _clean_name(name)
        limit = min(limit_mb or MAX_UPLOAD_MB, MAX_UPLOAD_MB)
        if len(data) > limit * 1024 * 1024:
            raise FileFormatError(
                "input.too_large", f"{name} is larger than {limit} MB", name=name, limit_mb=limit, size=len(data)
            )
        spec = detect(name, data)
        extracted = extract(name, data, spec=spec, ocr=self.ocr())
        blob = self.blobs.put(data, name=name, media_type=spec.media_type)
        cached = self.blobs.put(
            extracted.model_dump_json().encode("utf-8"), name=f"{blob}.extract.json", media_type="application/json"
        )
        item = InputFile(
            id=f"{PREFIX}{uuid.uuid4().hex[:12]}",
            blob=blob,
            name=name,
            format=spec.key,
            group=spec.group,
            media_type=spec.media_type if spec.key != "code" else (media_type or "text/plain"),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            created_at=datetime.now(timezone.utc),
            origin=origin,
            uploaded_by=uploaded_by,
            info=extracted.info,
            notes=extracted.notes,
            outline=extracted.outline,
            chars=extracted.chars,
            entries=extracted.entries,
            extract_blob=cached,
        )
        self.store.put(COLLECTION, item.id, item)
        with self._lock:
            self._remember(self._extracted, item.id, extracted)
        return item

    # ---- reading -----------------------------------------------------------------------

    def get(self, file_id: str) -> InputFile:
        found = self.store.get(COLLECTION, file_id, InputFile) if is_input_id(file_id) else None
        if found is None:
            raise KeyError(file_id)
        return found

    def find(self, file_id: str) -> InputFile | None:
        try:
            return self.get(file_id)
        except KeyError:
            return None

    def __contains__(self, file_id: object) -> bool:
        return isinstance(file_id, str) and self.find(file_id) is not None

    def data(self, file_id: str) -> bytes:
        return self.blobs.get_bytes(self.get(file_id).blob)

    def extracted(self, file_id: str) -> Extracted:
        """What Nodestro read from the file (computed once per content, then reused)."""
        with self._lock:
            if file_id in self._extracted:
                self._extracted.move_to_end(file_id)
                return self._extracted[file_id]
        item = self.get(file_id)
        result: Extracted | None = None
        if item.extract_blob and item.extract_blob in self.blobs:
            try:
                result = Extracted.model_validate_json(self.blobs.get_bytes(item.extract_blob))
            except ValueError:
                result = None
        if result is None:
            data = self.blobs.get_bytes(item.blob)
            result = extract(item.name, data, ocr=self.ocr())
        with self._lock:
            self._remember(self._extracted, file_id, result)
        return result

    def member(self, file_id: str, path: str) -> tuple[ArchiveEntry, Extracted, bytes]:
        """One entry of an archive, read like a file of its own (cached while in use)."""
        key = (file_id, path.strip().strip("/"))
        with self._lock:
            if key in self._members:
                self._members.move_to_end(key)
                return self._members[key]
        item = self.get(file_id)
        if item.format != "zip":
            raise FileFormatError("input.not_archive", f"{item.name} is not a ZIP archive", name=item.name)
        found = extract_member(self.blobs.get_bytes(item.blob), key[1], item.name)
        with self._lock:
            self._remember(self._members, key, found)
        return found

    @staticmethod
    def _remember(cache: OrderedDict[Any, Any], key: Any, value: Any) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > _CACHE:
            cache.popitem(last=False)

    def names(self, ids: list[str]) -> dict[str, str]:
        found = {}
        for file_id in ids:
            item = self.find(file_id)
            if item is not None:
                found[file_id] = item.name
        return found

    def all(self) -> list[InputFile]:
        return self.store.list(COLLECTION, InputFile, limit=100_000)

    def link_knowledge(self, file_id: str, knowledge_id: str) -> None:
        item = self.find(file_id)
        if item is not None and knowledge_id not in item.knowledge_ids:
            item.knowledge_ids.append(knowledge_id)
            self.store.put(COLLECTION, item.id, item)

    def forget(self) -> None:
        """Drop cached extractions (after a restore replaced the files)."""
        with self._lock:
            self._extracted.clear()
            self._members.clear()

    # ---- references and cleanup ---------------------------------------------------------

    def references(self) -> dict[str, set[str]]:
        """Input file id -> what references it: ``run:<status>``, ``suite``, ``schedule``, ``batch:<status>``."""
        found: dict[str, set[str]] = {}
        connection = self.store._connection
        marks = ", ".join("?" for _ in _HOLDERS)
        with self.store._lock:
            rows = connection.execute(
                f"SELECT collection, data FROM nodestro_documents WHERE collection IN ({marks})"
                " AND data LIKE '%in\\_%' ESCAPE '\\'",
                _HOLDERS,
            ).fetchall()
        for collection, raw in rows:
            ids = set(_REFERENCE.findall(raw))
            if not ids:
                continue
            holder = {"benchmark_suites": "suite", "schedules": "schedule"}.get(collection, collection)
            if collection in ("runs", "batches"):
                try:
                    status = json.loads(raw).get("status", "")
                except ValueError:
                    status = ""
                holder = f"{'run' if collection == 'runs' else 'batch'}:{status}"
            for file_id in ids:
                found.setdefault(file_id, set()).add(holder)
        return found

    @staticmethod
    def _protected(holders: set[str]) -> bool:
        """Files of active runs and batches, test cases and schedules are never removed."""
        active = {"run:queued", "run:running", "run:paused", "batch:running", "batch:paused", "batch:interrupted"}
        return bool(holders & (active | {"suite", "schedule"}))

    def storage(self) -> StorageInfo:
        references = self.references()
        files = self.all()
        return StorageInfo(
            files=len(files),
            bytes=sum(item.size for item in files),
            unreferenced=sum(1 for item in files if item.id not in references),
            in_use=sum(1 for item in files if self._protected(references.get(item.id, set()))),
        )

    def cleanup(self, *, older_than_days: int | None = None, at: datetime | None = None) -> CleanupResult:
        """Remove files nobody uses (after :data:`GRACE`) and, with ``older_than_days``, the files of
        finished runs created before then. Files of active runs, test cases and schedules stay."""
        moment = at or datetime.now(timezone.utc)
        references = self.references()
        cutoff = moment - timedelta(days=older_than_days) if older_than_days is not None else None
        result = CleanupResult()
        for item in self.all():
            holders = references.get(item.id, set())
            if self._protected(holders):
                result.kept_in_use += 1
                continue
            if holders:
                if cutoff is None or item.created_at > cutoff:
                    continue
            elif item.created_at > moment - GRACE:
                continue
            self.delete(item.id, force=True)
            result.removed += 1
            result.freed_bytes += item.size
        return result

    def release(self, ids: list[str]) -> int:
        """Remove the given files when nothing references them any more (after a run was deleted)."""
        references = self.references()
        removed = 0
        for file_id in dict.fromkeys(ids):
            if is_input_id(file_id) and file_id not in references and file_id in self:
                self.delete(file_id, force=True)
                removed += 1
        return removed

    def delete(self, file_id: str, *, force: bool = False) -> None:
        """Delete an input file; without ``force`` only when nothing references it."""
        item = self.get(file_id)
        if not force and file_id in self.references():
            from .problems import NodestroError

            raise NodestroError("input.in_use", f"{item.name} is used by a run, test case or schedule", name=item.name)
        self.store.delete(COLLECTION, file_id)
        with self._lock:
            self._extracted.pop(file_id, None)
            for key in [key for key in self._members if key[0] == file_id]:
                self._members.pop(key, None)
        remaining = {other.blob for other in self.all()} | {other.extract_blob for other in self.all()}
        for blob in (item.blob, item.extract_blob):
            if blob and blob not in remaining and blob in self.blobs:
                self.blobs.delete(blob)


def _clean_name(name: str) -> str:
    """The file name without control characters; a relative path from a folder upload is kept
    (``Belege/2024/a.pdf``), absolute parts and ``..`` never."""
    text = "".join(char for char in name.replace("\\", "/") if ord(char) >= 32)
    parts = [part.strip() for part in text.split("/") if part.strip() not in ("", ".", "..")]
    return "/".join(parts)[-240:].lstrip("/") or "upload"


class FileCatalog(Mapping[str, FileRef]):
    """Every file a run can reference: input files (``in_…``) and knowledge files, looked up lazily."""

    def __init__(self, inputs: InputFiles | None, knowledge: FileStoreLike) -> None:
        self.inputs = inputs
        self.knowledge = knowledge

    def __getitem__(self, file_id: str) -> FileRef:
        if is_input_id(file_id):
            item = self.inputs.find(file_id) if self.inputs is not None else None
            if item is None:
                raise KeyError(file_id)
            return FileRef(name=item.name, format=item.format, size=item.size, source="input")
        if file_id not in self.knowledge:
            raise KeyError(file_id)
        entry = self.knowledge.info(file_id)
        from .file_formats import format_of_name

        spec = format_of_name(entry.name)
        return FileRef(name=entry.name, format=spec.key if spec else None, size=entry.size, source="knowledge")

    def __contains__(self, file_id: object) -> bool:
        if not isinstance(file_id, str):
            return False
        if is_input_id(file_id):
            return self.inputs is not None and file_id in self.inputs
        return file_id in self.knowledge

    def __iter__(self) -> Iterator[str]:
        yield from (entry.id for entry in self.knowledge.list())
        if self.inputs is not None:
            yield from (item.id for item in self.inputs.all())

    def __len__(self) -> int:
        return sum(1 for _ in self)
