"""Background knowledge: uploaded files, their search index and per-agent views.

All files live in one ``SQLiteFileStore``; their text is indexed in one
``SQLiteIndex`` (source = file id). An agent sees only the files assigned to
it, through :class:`ScopedFileStore` and :class:`ScopedIndex`, because
``orkexa.Agent`` takes exactly one store and one index.

Files are grouped into *collections* (a name kept with each file, ``metadata["collection"]``;
empty: no collection). A collection is a label, not a second store: agents still get
exactly the files assigned to them, so new knowledge never changes what a workflow or a
running workflow sees. Files taken over from runs (:mod:`nodestro_runtime.adoption`) keep where
they came from in their metadata (``origin_*``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
import tempfile
import threading

from orkexa import Chunk, FileStore, Hit, Index, SQLiteFileStore, SQLiteIndex, StoredFile
from pydantic import BaseModel

from . import documents
from .extensions import OcrEngine
from .schemas import KnowledgeConfig

_INDEXED_SUFFIXES = (".pdf", ".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml", ".html", ".xml")


class KnowledgeFile(BaseModel):
    id: str
    name: str
    media_type: str
    kind: str
    size: int
    description: str
    created_at: datetime
    chunks: int
    #: ``docx``/``xlsx`` when the file was converted to Markdown on upload.
    source_format: str | None = None
    #: ``yes`` for a PDF without a text layer (a scan).
    scanned: bool = False
    #: OCR result of a scan: ``done``, ``unavailable`` or ``failed`` (``None``: no OCR).
    ocr: str | None = None
    ocr_uncertain: int = 0
    ocr_words: int = 0
    notes: dict[str, str] = {}
    #: The collection the file belongs to (empty: none).
    collection: str = ""
    #: Where it came from: ``upload`` (knowledge page), ``input_file``, ``result_file``, ``agent_result`` or
    #: ``proposal`` (taken over from a run after a person confirmed it).
    origin: str = "upload"
    #: The workflow, step, run, source files, time and kind of content of a file taken over from a run.
    provenance: dict[str, str] = {}
    #: Whether its text is in the search index (images and scans without OCR are only stored).
    searchable: bool = False


class CollectionInfo(BaseModel):
    name: str
    files: int


class ScopedFileStore(FileStore):
    """A read-only view of a store that shows only ``file_ids``."""

    def __init__(self, base: FileStore, file_ids: Iterable[str]) -> None:
        super().__init__(
            files_api=base.files_api,
            max_image_bytes=base.max_image_bytes,
            max_document_bytes=base.max_document_bytes,
        )
        self.base = base
        self.file_ids = frozenset(file_ids)

    def _save(self, entry: StoredFile, data: bytes | None) -> None:
        if entry.id not in self.file_ids:
            raise PermissionError("This view is read-only")
        self.base._save(entry, data)  # only metadata such as api_file_id

    def _entry(self, file_id: str) -> StoredFile | None:
        return self.base._entry(file_id) if file_id in self.file_ids else None

    def _data(self, file_id: str) -> bytes | None:
        return self.base._data(file_id) if file_id in self.file_ids else None

    def _entries(self) -> list[StoredFile]:
        return [entry for entry in self.base._entries() if entry.id in self.file_ids]

    def _remove(self, file_id: str) -> None:
        raise PermissionError("This view is read-only")


class ScopedIndex(Index):
    """A read-only view of an index that returns only chunks of ``file_ids``."""

    def __init__(self, base: Index, file_ids: Iterable[str]) -> None:
        super().__init__(max_chars=base.max_chars, overlap=base.overlap)
        self.base = base
        self.file_ids = frozenset(file_ids)

    def add_chunks(self, chunks: Iterable[Chunk]) -> list[int]:
        raise PermissionError("This view is read-only")

    def delete(self, source: str) -> int:
        raise PermissionError("This view is read-only")

    def search(self, query: str, k: int = 5) -> list[Hit]:
        # Over-fetch from the shared index, then keep the assigned files only.
        hits = self.base.search(query, min(max(k * 20, 50), 500))
        return [hit for hit in hits if hit.source in self.file_ids][:k]


class KnowledgeBase:
    """Upload, index, list and delete knowledge files."""

    def __init__(
        self,
        database: str | Path,
        *,
        ocr: documents.OcrRunner | None = None,
        engine: Callable[[], OcrEngine | None] = lambda: None,
    ) -> None:
        """``engine`` returns the OCR engine of the installed integrations (``None``: no OCR);
        ``ocr`` replaces it with a fixed function (tests)."""
        self.store = SQLiteFileStore(database, namespace="nodestro")
        self.index = SQLiteIndex(database, namespace="nodestro")
        self._lock = threading.RLock()
        self.ocr_runner = ocr
        self.ocr_engine = engine
        #: Whether scanned PDFs are recognised on upload (a setting; off by default).
        self.ocr_enabled: Callable[[], bool] = lambda: False

    def ocr_available(self) -> bool:
        return self.runner() is not None

    def runner(self) -> documents.OcrRunner | None:
        """The OCR function to use now (``None``: no OCR is available)."""
        if self.ocr_runner is not None:
            return self.ocr_runner
        engine = self.ocr_engine()
        return engine.recognize if engine is not None else None

    def close(self) -> None:
        self.store.close()
        self.index.close()

    def _chunk_counts(self) -> dict[str, int]:
        connection = self.index._connection
        with self.index._lock:
            rows = connection.execute(
                "SELECT source, count(*) FROM index_chunks WHERE namespace = ? GROUP BY source",
                (self.index.namespace,),
            ).fetchall()
        return {str(source): int(count) for source, count in rows}

    def _describe(self, entry: StoredFile, counts: dict[str, int]) -> KnowledgeFile:
        meta = entry.metadata
        return KnowledgeFile(
            id=entry.id,
            name=entry.name,
            media_type=entry.media_type,
            kind=entry.kind,
            size=entry.size,
            description=entry.description,
            created_at=entry.created_at,
            chunks=counts.get(entry.id, 0),
            source_format=meta.get("source_format") or None,
            scanned=meta.get("scanned") == "yes",
            ocr=meta.get("ocr") or None,
            ocr_uncertain=int(meta.get("ocr_uncertain") or 0),
            ocr_words=int(meta.get("ocr_words") or 0),
            notes={key[5:]: value for key, value in meta.items() if key.startswith("note_")},
            collection=meta.get("collection", ""),
            origin=meta.get("origin", "upload"),
            provenance={key[7:]: value for key, value in meta.items() if key.startswith("origin_")},
            searchable=counts.get(entry.id, 0) > 0,
        )

    def files(self, query: str | None = None) -> list[KnowledgeFile]:
        counts = self._chunk_counts()
        return [self._describe(entry, counts) for entry in self.store.list(query)]

    def get(self, file_id: str) -> KnowledgeFile:
        return self._describe(self.store.info(file_id), self._chunk_counts())

    def collections(self) -> list[CollectionInfo]:
        counts: dict[str, int] = {}
        for entry in self.store.list():
            name = entry.metadata.get("collection", "")
            if name:
                counts[name] = counts.get(name, 0) + 1
        return [
            CollectionInfo(name=name, files=count)
            for name, count in sorted(counts.items(), key=lambda i: i[0].casefold())
        ]

    def add(
        self,
        data: bytes,
        *,
        name: str,
        description: str = "",
        media_type: str | None = None,
        collection: str = "",
        extra: dict[str, str] | None = None,
    ) -> KnowledgeFile:
        """Store a file and index its text.

        PDF and text formats are indexed as they are; DOCX and XLSX are converted to
        Markdown first (see :mod:`nodestro_runtime.documents`); a scanned PDF is recognised by
        local OCR when that is switched on and installed. Images are only stored.
        ``collection`` groups the file; ``extra`` is kept as metadata (origin of adopted files).
        """
        metadata: dict[str, str] = dict(extra or {})
        if collection.strip():
            metadata["collection"] = collection.strip()[:120]
        converted = documents.convert(data, name)
        if converted is not None:
            data = converted.text.encode("utf-8")
            media_type = "text/markdown"
            metadata["source_format"] = converted.source_format
            metadata.update({f"note_{key}": value for key, value in converted.notes.items()})
        with self._lock:
            entry = self.store.add(data, name=name, description=description, media_type=media_type, metadata=metadata)
            if converted is not None:
                self.index.delete(entry.id)
                self.index.add(converted.text, source=entry.id, title=name, file_id=entry.id)
            elif entry.media_type == "application/pdf":
                with tempfile.TemporaryDirectory(prefix="nodestro-upload-") as directory:
                    path = Path(directory) / "upload.pdf"
                    path.write_bytes(data)
                    # A PDF without a text layer is stored but not searchable (see OCR).
                    with suppress(ValueError, ImportError):
                        self.index.add_file(path, source=entry.id, title=name)
                    self._attach_file_id(entry.id)
            elif Path(name).suffix.lower() in _INDEXED_SUFFIXES or entry.media_type.startswith("text/"):
                with suppress(UnicodeDecodeError):
                    self.index.delete(entry.id)
                    self.index.add(data.decode("utf-8"), source=entry.id, title=name, file_id=entry.id)
            if entry.media_type == "application/pdf" and not documents.pdf_has_text(data):
                self._mark(entry.id, scanned="yes")
                if self.ocr_enabled():
                    self.recognise(entry.id)
        return self.get(entry.id)

    def _attach_file_id(self, file_id: str) -> None:
        """Let every chunk of a file point to it (an agent can then load the whole file after a hit)."""
        with self.index._lock:
            self.index._connection.execute(
                "UPDATE index_chunks SET file_id = ? WHERE namespace = ? AND source = ?",
                (file_id, self.index.namespace, file_id),
            )

    def restore_file(self, entry: StoredFile, data: bytes, text: str) -> None:
        """Store a file under its original id with its readable text (an exported project's knowledge)."""
        with self._lock:
            self.store._save(entry, data)
            self.index.delete(entry.id)
            if text.strip():
                self.index.add(text, source=entry.id, title=entry.name, file_id=entry.id)

    def _mark(self, file_id: str, **values: str) -> None:
        entry = self.store.info(file_id)
        with self.store._lock:
            self.store._save(entry.model_copy(update={"metadata": {**entry.metadata, **values}}), None)

    def recognise(self, file_id: str) -> KnowledgeFile:
        """Index the OCR text of a scanned PDF; uncertain words are marked and counted."""
        entry = self.store.info(file_id)
        if entry.media_type != "application/pdf":
            raise documents.DocumentError("document.not_pdf", f"{entry.name} is not a PDF", name=entry.name)
        runner = self.runner()
        if runner is None:
            self._mark(file_id, ocr="unavailable")
            return self.get(file_id)
        try:
            result = runner(self.store.get_bytes(file_id), entry.name)
        except documents.DocumentError as error:
            self._mark(file_id, ocr="failed", ocr_error=error.code)
            return self.get(file_id)
        with self._lock:
            self.index.delete(file_id)
            text = f"{documents.ocr_intro(result)}\n\n{result.text}"
            self.index.add(text, source=file_id, title=entry.name, file_id=file_id, metadata={"ocr": "yes"})
        self._mark(
            file_id,
            ocr="done",
            ocr_uncertain=str(result.uncertain),
            ocr_words=str(result.words),
            ocr_language=result.language,
        )
        return self.get(file_id)

    def text_of(self, file_id: str) -> str:
        """The readable text of a file: converted Markdown, OCR text or the indexed text."""
        entry = self.store.info(file_id)
        if entry.media_type.startswith("text/"):
            return self.store.get_bytes(file_id).decode("utf-8", errors="replace")
        connection = self.index._connection
        with self.index._lock:
            rows = connection.execute(
                "SELECT text FROM index_chunks WHERE namespace = ? AND source = ? ORDER BY id",
                (self.index.namespace, file_id),
            ).fetchall()
        return "\n".join(str(row[0]) for row in rows)

    def update(self, file_id: str, *, description: str, collection: str | None = None) -> KnowledgeFile:
        entry = self.store.info(file_id)
        metadata = dict(entry.metadata)
        if collection is not None:
            if collection.strip():
                metadata["collection"] = collection.strip()[:120]
            else:
                metadata.pop("collection", None)
        with self.store._lock:
            self.store._save(entry.model_copy(update={"description": description, "metadata": metadata}), None)
        return self.get(file_id)

    def delete(self, file_id: str) -> None:
        with self._lock:
            self.store.delete(file_id)
            self.index.delete(file_id)

    def search(self, query: str, file_ids: Iterable[str] | None = None, k: int = 8) -> list[Hit]:
        index: Index = self.index if file_ids is None else ScopedIndex(self.index, file_ids)
        return index.search(query, k)

    # ---- per-agent views -----------------------------------------------------------

    def views(self, config: KnowledgeConfig) -> tuple[ScopedFileStore | None, ScopedIndex | None]:
        """The store and index an agent gets for ``config`` (``None`` where it gets none)."""
        file_ids = [file_id for file_id in config.file_ids if file_id in self.store]
        if not file_ids or config.mode == "context":
            return None, None
        if config.mode == "on_demand":
            return ScopedFileStore(self.store, file_ids), None
        return None, ScopedIndex(self.index, file_ids)
