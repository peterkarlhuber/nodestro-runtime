"""Background knowledge: uploaded files, their search index and per-agent views.

All files live in one ``SQLiteFileStore``; their text is indexed in one
``SQLiteIndex`` (source = file id). The server passes other backends of the same Orkexa
interfaces as :class:`KnowledgeTables` (PostgreSQL:
:func:`nodestro_runtime.postgres_tables.knowledge_tables`). An agent sees only the files assigned to
it, through :class:`ScopedFileStore` and :class:`ScopedIndex`, because
``orkexa.Agent`` takes exactly one store and one index.

Files are grouped into *collections* (a name kept with each file, ``metadata["collection"]``;
empty: no collection). A collection is a label, not a second store: agents still get
exactly the files assigned to them, so new knowledge never changes what a workflow or a
running workflow sees. Files taken over from runs (:mod:`nodestro_runtime.adoption`) keep where
they came from in their metadata (``origin_*``).

Retrieval modes (local models, D9): ``fts`` full-text search (the default), ``vector`` semantic search with an
embedding model, ``hybrid`` both: the vector ranking plus each candidate's full-text evidence, the share of the
query's words it contains (:func:`orkexa.retrieval.fuse_hybrid`, the same on SQLite and PostgreSQL), so a full-text
hit on one accidental word of a query in another language no longer outranks the right vector hit. The workspace
setting chooses the mode and the embedding model; an agent may choose another mode (``KnowledgeConfig.retrieval``).
Vectors exist only after a person built the semantic index (:meth:`KnowledgeBase.start_reindex`); they belong to one
embedding model, and another model needs a reindex. An agent view (:class:`ScopedIndex`) limits a search to the
agent's files *before* ranking, in every mode.

Granted folders (N-12, :mod:`nodestro_runtime.folder_index`): the index may also hold the chunks of files of a granted
folder (source ``folder:<folder grant id>/<path>``, :data:`FOLDER_SOURCE`). They are no knowledge files: the file store
never holds them, and every knowledge search (an agent's view, :meth:`KnowledgeBase.search`) is limited to the
knowledge files, so folder text is found only through the folder tools, which check the folder grant at every call.
Folder text is embedded (and a folder search's query is) only when :attr:`KnowledgeBase.folder_embedding_allowed`
says so, whatever the switches: an embedding server on this computer, or one elsewhere while the provenance switch is
on and a grant allows it. Otherwise the chunks are stored without vectors, the search runs in full text, and a reindex
for a model that may not embed them removes them first (:attr:`KnowledgeBase.folder_reset`) and stores none until it
ended (N-12 review H-1).

Egress protection (N-08, only while its switch is on): every embedding of granted content asks a check first
(:attr:`KnowledgeBase.embedding_check`, :attr:`ScopedIndex.before_embed`), so knowledge text and a marked run's search
query reach an embedding model outside this computer only with a grant (``docs/content-provenance.md``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from pathlib import Path
import threading
from typing import Any, Literal, Protocol, cast

from orkexa import Chunk, FileStore, Hit, Index, SQLiteFileStore, SQLiteIndex, StoredFile
from orkexa.embeddings import EmbeddingIdentity, EmbeddingModel
from orkexa.local import OllamaError
from orkexa.retrieval import RetrievalError, RetrievalMode, split_text
from pydantic import BaseModel, Field

from . import activity, documents
from .extensions import OcrEngine
from .problems import NodestroError
from .schemas import KnowledgeConfig
from .sqlite_tables import KnowledgeChunks
from .storage import DocumentStore

logger = logging.getLogger(__name__)
_INDEXED_SUFFIXES = (".pdf", ".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml", ".html", ".xml")
#: The start of the index source of a granted folder's file (N-12): never a knowledge file's id.
FOLDER_SOURCE = "folder:"


@dataclass(frozen=True)
class _PdfText:
    """The text of an uploaded PDF, read once (:func:`documents.read_pdf`): its pages, ``None`` when the PDF opens but
    its text cannot be extracted."""

    pages: list[str] | None

    @property
    def has_text(self) -> bool:
        """Whether the PDF has a text layer (a scan without OCR has none)."""
        return any(page.strip() for page in self.pages or [])


def _is_pdf(name: str, media_type: str | None) -> bool:
    return Path(name).suffix.lower() == ".pdf" or (media_type or "").lower() == "application/pdf"


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


IndexState = Literal["off", "ready", "incomplete", "building", "reindex_required", "unavailable"]


def sqlite_vectors_missing() -> str | None:
    """Why a SQLite knowledge base cannot hold vectors here, else ``None``: sqlite-vec is not installed (it has no
    wheel for Windows on ARM)."""
    import importlib.util

    if importlib.util.find_spec("sqlite_vec") is None:
        return "sqlite-vec is not installed on this platform"
    return None


class KnowledgeIndexStatus(BaseModel):
    """The semantic index of the knowledge base: which model filled it and how complete it is."""

    #: The workspace's retrieval mode and embedding model (settings).
    mode: Literal["fts", "vector", "hybrid"] = "fts"
    embedding_model: str | None = None
    #: ``off`` (no vectors), ``ready``, ``incomplete`` (chunks without vectors, e.g. added while the model server was
    #: down), ``building`` (a reindex runs or was interrupted), ``reindex_required`` (the vectors belong to another
    #: model than the chosen one) or ``unavailable`` (this storage cannot hold vectors: no sqlite-vec on this platform,
    #: or a PostgreSQL schema without the vector tables; ``detail`` says why). A model server that cannot be reached
    #: is never ``unavailable``: new chunks then stay without vectors (``incomplete``).
    state: IndexState = "off"
    chunks: int = 0
    embedded: int = 0
    #: The embedding model (and digest, dimensions) the vectors belong to.
    active: dict[str, Any] | None = None
    building: dict[str, Any] | None = None
    building_embedded: int = 0
    job: ReindexJob | None = None
    #: PostgreSQL: whether pgvector ranks the vectors (``None`` on SQLite).
    pgvector: bool | None = None
    detail: str = ""


class ReindexQueue(Protocol):
    """Where a server queues reindex jobs (:class:`nodestro_runtime.jobs.QueuedReindex`)."""

    def start(self, model: str) -> None: ...

    def cancel(self) -> None: ...

    def current(self) -> ReindexJob | None: ...


class ReindexJob(BaseModel):
    state: Literal["running", "done", "failed", "cancelled"] = "running"
    model: str
    completed: int = 0
    total: int = 0
    error_code: str | None = None
    error: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None


KnowledgeIndexStatus.model_rebuild()


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


class _WithoutFolderText:
    """The embedding model of a reindex that may not embed granted folder text (N-12 review H-1b): it refuses a batch
    while the index holds any folder chunk, so no route that adds one during the reindex can send it to the model.
    Everything else is the model's own."""

    def __init__(self, model: EmbeddingModel, has_folder_chunks: Callable[[], bool]) -> None:
        self.model = model
        self._has_folder_chunks = has_folder_chunks

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._has_folder_chunks():
            raise RetrievalError(
                "vector_unavailable", "Granted folder text may not go to this embedding model; build the index again"
            )
        return list(self.model.embed_documents(texts))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.model, name)


class FolderIndexBusy(Exception):
    """A granted folder's chunks cannot be stored now (a reindex runs that may not embed them): try again later."""


def _address(url: str | None) -> str | None:
    """A model server's address without its OpenAI-compatible suffix (``/v1``), to compare the one an embedding model
    was built for with the one the settings name now."""
    return (url or "").strip().removesuffix("/").removesuffix("/v1").rstrip("/") or None


def _model_address(model: object, fallback: str | None) -> str | None:
    """The address ``model`` really sends to: an Ollama model's client (``base_url``); else the address it was built
    for (``fallback``)."""
    client = getattr(model, "client", None)
    found = getattr(client, "base_url", None)
    return _address(found) if isinstance(found, str) else fallback


class ScopedIndex(Index):
    """A read-only view of an index that searches only the chunks of ``file_ids``, ranked by ``mode``.

    The files limit the candidates *before* ranking (the index filters by source), so other files never take the
    places of the assigned ones, in full-text, vector and hybrid search alike."""

    def __init__(
        self,
        base: Index,
        file_ids: Iterable[str],
        mode: RetrievalMode | None = None,
        *,
        before_embed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(max_chars=base.max_chars, overlap=base.overlap)
        self.base = base
        self.file_ids = frozenset(file_ids)
        self.mode = mode
        #: N-08: called before a search that embeds its query (every mode but ``fts``); it raises to refuse it.
        self.before_embed = before_embed

    def add_chunks(self, chunks: Iterable[Chunk]) -> list[int]:
        raise PermissionError("This view is read-only")

    def delete(self, source: str) -> int:
        raise PermissionError("This view is read-only")

    def search(self, query: str, k: int = 5) -> list[Hit]:
        return self.retrieve(query, k)

    def retrieve(
        self,
        query: str,
        k: int = 5,
        *,
        mode: RetrievalMode | None = None,
        sources: Iterable[str] | None = None,
    ) -> list[Hit]:
        allowed = self.file_ids if sources is None else self.file_ids & set(sources)
        chosen = mode or self.mode
        if chosen != "fts" and self.before_embed is not None:
            self.before_embed()  # ``None`` (the index's default) may embed too
        return self.base.retrieve(query, k, mode=chosen, sources=sorted(allowed))


class ChunkAccess(Protocol):
    """What Nodestro reads from an index beside the Orkexa interface (``KnowledgeChunks`` on SQLite)."""

    def counts(self) -> dict[str, int]: ...

    def attach_file(self, source: str) -> None: ...

    def text(self, source: str) -> str: ...


@dataclass(frozen=True)
class KnowledgeTables:
    """The storage of a knowledge base: the file store, the search index and its chunk access."""

    store: FileStore
    index: Index
    chunks: ChunkAccess


class KnowledgeBase:
    """Upload, index, list and delete knowledge files."""

    def __init__(
        self,
        database: str | Path | KnowledgeTables,
        *,
        ocr: documents.OcrRunner | None = None,
        engine: Callable[[], OcrEngine | None] = lambda: None,
    ) -> None:
        """``engine`` returns the OCR engine of the installed integrations (``None``: no OCR);
        ``ocr`` replaces it with a fixed function (tests)."""
        self.store: FileStore
        self.index: Index
        self.chunks: ChunkAccess
        if isinstance(database, KnowledgeTables):
            self.store, self.index, self.chunks = database.store, database.index, database.chunks
        else:
            index = SQLiteIndex(database, namespace="nodestro")
            self.store, self.index, self.chunks = (
                SQLiteFileStore(database, namespace="nodestro"),
                index,
                KnowledgeChunks(index),
            )
        self._lock = threading.RLock()
        self.ocr_runner = ocr
        self.ocr_engine = engine
        #: Whether scanned PDFs are recognised on upload (a setting; off by default).
        self.ocr_enabled: Callable[[], bool] = lambda: False
        #: The workspace's retrieval mode and embedding model (settings; default full text, no model).
        self.retrieval: Callable[[], tuple[RetrievalMode, str | None]] = lambda: ("fts", None)
        #: The embedding model of a name (the workspace connects it to the local model server).
        self.embedding_model: Callable[[str], EmbeddingModel] | None = None
        #: N-08: raises (``access.*``) when granted content may not go to the embedding model at the given address
        #: (the one the model really sends to): the workspace refuses an address outside this computer without a grant
        #: while the provenance switch is on. ``None``: no check.
        self.embedding_check: Callable[[str | None], None] | None = None
        #: The address of the local model server a newly built embedding model uses now (the workspace's connection);
        #: a model built for another address is built again before it embeds anything (RT-094 review M-1).
        self.embedding_address: Callable[[], str | None] = lambda: None
        #: The address the index's embedding model sends to (``None``: no model, or one without an address).
        self._embeddings_address: str | None = None
        self._job: ReindexJob | None = None
        self._job_cancel: threading.Event | None = None
        #: On a server's API process: where reindex jobs are queued for a worker (:class:`ReindexQueue`, RT-046);
        #: ``None``: a reindex runs in a thread of this process (desktop, Local Docker, tests).
        self.reindex_queue: ReindexQueue | None = None
        #: The workspace's store, where uploads and deletions are logged (:mod:`nodestro_runtime.activity`);
        #: ``None``: not logged (a knowledge base outside a workspace).
        self.activity: DocumentStore | None = None
        #: Whether the index may hold chunks of granted folders (N-12: the desktop and Nodestro Local Docker, never a
        #: server): a search without files is then limited to the knowledge files.
        self.folder_sources = False
        #: N-12 (fail closed, whatever the switches): whether granted folder text may be embedded by the model at the
        #: given address (the one it really sends to). The workspace allows this computer, and a server elsewhere only
        #: while the provenance switch is on and a grant allows it. Default: only a model without an address.
        self.folder_embedding_allowed: Callable[[str | None], bool] = lambda address: not address
        #: Removes every folder chunk (the folder index rebuilds them later): called when a reindex starts for a model
        #: that may not embed folder text, since a reindex embeds every chunk of the index.
        self.folder_reset: Callable[[], object] = self._remove_folder_chunks
        #: Whether the running reindex's model may embed folder text: while it is False, no folder chunk is stored.
        self._reindex_folders = True

    def ocr_available(self) -> bool:
        return self.runner() is not None

    def runner(self) -> documents.OcrRunner | None:
        """The OCR function to use now (``None``: no OCR is available)."""
        if self.ocr_runner is not None:
            return self.ocr_runner
        engine = self.ocr_engine()
        return engine.recognize if engine is not None else None

    def close(self) -> None:
        for part in (self.store, self.index):
            close = getattr(part, "close", None)
            if close is not None:
                close()

    def _chunk_counts(self) -> dict[str, int]:
        return self.chunks.counts()

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
        actor: str | None = None,
    ) -> KnowledgeFile:
        """Store a file and index its text (``actor``: who uploaded it, for the activity log).

        PDF and text formats are indexed as they are; DOCX and XLSX are converted to
        Markdown first (see :mod:`nodestro_runtime.documents`); a scanned PDF is recognised by
        local OCR when that is switched on and installed. Images are only stored.
        ``collection`` groups the file; ``extra`` is kept as metadata (origin of adopted files).
        """
        metadata: dict[str, str] = dict(extra or {})
        if collection.strip():
            metadata["collection"] = collection.strip()[:120]
        converted = documents.convert(data, name)
        # A PDF's text is read before anything is stored and outside the lock, in a separate process within the PDF
        # limits (MUP-ONA A1): a PDF over a limit is refused (document.too_complex) and leaves nothing behind.
        pdf = _PdfText(documents.read_pdf(data, name)) if converted is None and _is_pdf(name, media_type) else None
        if converted is not None:
            data = converted.text.encode("utf-8")
            media_type = "text/markdown"
            metadata["source_format"] = converted.source_format
            metadata.update({f"note_{key}": value for key, value in converted.notes.items()})
        with self._lock:
            entry = self.store.add(data, name=name, description=description, media_type=media_type, metadata=metadata)
            try:
                self._index_with_fallback(lambda: self._index_new(entry, data, name, converted, pdf))
            except BaseException:
                # A file that cannot be indexed is not kept: no half-stored entry, no stray chunks.
                self._delete(entry.id)
                raise
        self._activity("file.uploaded", entry.id, actor)
        return self.get(entry.id)

    def _index_new(
        self,
        entry: StoredFile,
        data: bytes,
        name: str,
        converted: documents.Converted | None,
        pdf: _PdfText | None,
    ) -> None:
        if converted is not None:
            self.index.delete(entry.id)
            self.index.add(converted.text, source=entry.id, title=name, file_id=entry.id)
        elif entry.media_type == "application/pdf":
            if pdf is None:  # stored as a PDF although neither its name nor its declared type said so
                pdf = _PdfText(documents.read_pdf(data, name))
            # The chunks orkexa's Index.add_file makes of a PDF (one title and ``page`` per page), from the text read
            # in the limited process instead of a second, unlimited extraction in this one. A PDF whose text cannot
            # be extracted, or without a text layer, is stored but not searchable (see OCR).
            chunks = [
                Chunk(text=piece, source=entry.id, title=f"{name}, p. {number}", metadata={"page": str(number)})
                for number, page in enumerate(pdf.pages or [], start=1)
                if page.strip()
                for piece in split_text(page, max_chars=self.index.max_chars, overlap=self.index.overlap)
            ]
            self.index.delete(entry.id)
            if chunks:
                self.index.add_chunks(chunks)
            self._attach_file_id(entry.id)
        elif Path(name).suffix.lower() in _INDEXED_SUFFIXES or entry.media_type.startswith("text/"):
            with suppress(UnicodeDecodeError):
                self.index.delete(entry.id)
                self.index.add(data.decode("utf-8"), source=entry.id, title=name, file_id=entry.id)
        if entry.media_type == "application/pdf" and not (pdf is not None and pdf.has_text):
            self._mark(entry.id, scanned="yes")
            if self.ocr_enabled():
                self.recognise(entry.id)

    # ---- semantic index ----------------------------------------------------------------------------------------

    def _vectors(self) -> Any:
        """The vector state of the index (``None``: this index cannot hold vectors, also a SQLite index on a platform
        without sqlite-vec, which Orkexa needs to build and search vectors)."""
        status = getattr(self.index, "vector_status", None)
        if status is None or (isinstance(self.index, SQLiteIndex) and sqlite_vectors_missing()):
            return None
        return status()

    def _model(self, name: str | None = None) -> EmbeddingModel | None:
        chosen = name or self.retrieval()[1]
        if not chosen or self.embedding_model is None:
            return None
        return self.embedding_model(chosen)

    def _sync_embeddings(self) -> None:
        """Give the index the embedding model its vectors belong to (the active space, else the one being built), so
        new chunks get vectors and vector searches embed their query with the same model; ``None`` without vectors."""
        if not hasattr(self.index, "embeddings"):
            return
        vectors = self._vectors()
        target = None if vectors is None else (vectors.active or vectors.building)
        current = getattr(self.index, "embeddings", None)
        if target is None:
            setattr(self.index, "embeddings", None)  # noqa: B010 (an attribute of the vector-capable indexes only)
            return
        wanted = _address(self.embedding_address())
        if current is not None and self._embeddings_address == wanted:
            # Kept only while it still talks to the server the settings name (RT-094 review M-1: before, a model
            # built for a remote server kept sending there after the address was changed to this computer, while
            # the N-08 check read the new address and let granted content through).
            try:
                identity = current.identity()
            except (OllamaError, NodestroError):
                identity = None
            if identity is not None and identity.model == target.get("model"):
                return
        model = self._model(str(target.get("model")))
        self._embeddings_address = _model_address(model, wanted)
        setattr(self.index, "embeddings", model)  # noqa: B010

    def _check_embedding(self, address: str | None) -> None:
        """N-08: raise ``access.*`` when granted content may not be sent to the embedding model at ``address`` (the
        address the model that will embed it really uses)."""
        if self.embedding_check is not None:
            self.embedding_check(address)

    def _without_vectors(self, work: Callable[[], object]) -> None:
        model = getattr(self.index, "embeddings", None)
        setattr(self.index, "embeddings", None)  # noqa: B010
        try:
            work()
        finally:
            setattr(self.index, "embeddings", model)  # noqa: B010

    def _index_checked(self, work: Callable[[], object], allowed: Callable[[str | None], bool] | None = None) -> None:
        """Index text; when the embedding check refuses (N-08), index it without vectors: the text stays searchable
        in full text, the refusal is audited and the status shows ``incomplete`` until a permitted reindex.
        ``allowed``: the check to use instead (granted folder text, :attr:`folder_embedding_allowed`)."""
        if getattr(self.index, "embeddings", None) is not None:
            if allowed is not None:
                if allowed(self._embeddings_address):
                    work()
                else:
                    logger.info("Granted folder text is indexed without vectors (its embedding server is elsewhere)")
                    self._without_vectors(work)
                return
            try:
                self._check_embedding(self._embeddings_address)
            except NodestroError as exc:
                if not exc.code.startswith("access."):
                    raise
                logger.warning("New knowledge is indexed without vectors: %s", exc.code)
                self._without_vectors(work)
                return
        work()

    def _index_with_fallback(
        self, work: Callable[[], object], allowed: Callable[[str | None], bool] | None = None
    ) -> None:
        """Index new text; when the embedding model cannot be reached, index it without vectors (a later reindex
        adds them, the status shows ``incomplete``) instead of refusing the upload. ``allowed``: as for
        :meth:`_index_checked`."""
        self._sync_embeddings()
        try:
            self._index_checked(work, allowed)
        except (OllamaError, RetrievalError) as exc:
            # ``reindex_required``: the embedding model changed on the server (a pull or rebuild gave it another
            # digest), so its vectors would not match the index's; the status says ``reindex_required`` (RT3-08).
            if getattr(self.index, "embeddings", None) is None or (
                isinstance(exc, RetrievalError) and exc.code != "reindex_required"
            ):
                raise
            logger.warning("New knowledge is indexed without vectors: %s", exc)
            self._without_vectors(work)

    def index_status(self) -> KnowledgeIndexStatus:
        mode, model = self.retrieval()
        status = KnowledgeIndexStatus(mode=mode, embedding_model=model, job=self._current_job())
        pgvector = getattr(self.index, "pgvector", None)
        try:
            vectors = self._vectors()
            status.pgvector = pgvector() if pgvector is not None else None
        except Exception as exc:  # e.g. a PostgreSQL schema without migration 0010
            status.state, status.detail = "unavailable", str(exc)
            return status
        if vectors is None:
            status.state = "unavailable"
            if isinstance(self.index, SQLiteIndex):
                status.detail = sqlite_vectors_missing() or ""
            return status
        status.chunks, status.embedded = vectors.chunks, vectors.embedded
        status.active, status.building = vectors.active, vectors.building
        status.building_embedded = vectors.building_embedded
        if vectors.building is not None:
            status.state = "building"
        elif vectors.active is None:
            status.state = "off"
        elif model and (vectors.active.get("model") != model or self._model_changed(model, vectors.active)):
            status.state = "reindex_required"
        elif vectors.embedded < vectors.chunks:
            status.state = "incomplete"
        else:
            status.state = "ready"
        return status

    def _model_changed(self, name: str, active: dict[str, Any]) -> bool:
        """Whether the embedding model ``name`` is now another build than the one the active vectors belong to (a new
        digest after a pull or rebuild, RT3-08); ``False`` when the model cannot be asked."""
        try:
            model = self._model(name)
            return model is not None and model.identity().key != EmbeddingIdentity.from_dict(active).key
        except (OllamaError, NodestroError, ValueError, KeyError, TypeError):
            return False

    def _reindex_model(self, model_name: str | None) -> tuple[str, EmbeddingModel]:
        """The embedding model a reindex uses (``model_name`` or the setting's), checked before anything starts."""
        name = model_name or self.retrieval()[1]
        if not name:
            raise NodestroError("knowledge.embedding_model_missing", "Choose an embedding model first")
        if self._vectors() is None:
            raise NodestroError("knowledge.vectors_unavailable", "This knowledge base cannot hold vectors")
        wanted = _address(self.embedding_address())
        model = self._model(name)
        if model is None:
            raise NodestroError("knowledge.embedding_model_missing", "No embedding model is available")
        # N-08: a reindex sends every chunk of the knowledge to the model, at the address it was built for.
        self._check_embedding(_model_address(model, wanted))
        return name, model

    def _folders_embeddable(self, model: EmbeddingModel) -> bool:
        """Whether a reindex with ``model`` may embed granted folder text (N-12 review H-1), judged by the address the
        model sends to alone: also when the index holds no folder chunk yet, since a folder may be indexed while the
        reindex runs (review H-1b)."""
        return self.folder_embedding_allowed(_model_address(model, _address(self.embedding_address())))

    def _has_folder_chunks(self) -> bool:
        return any(source.startswith(FOLDER_SOURCE) for source in self._chunk_counts())

    def _remove_folder_chunks(self) -> None:
        with self._lock:
            self.remove_sources([source for source in self._chunk_counts() if source.startswith(FOLDER_SOURCE)])

    def start_reindex(self, model_name: str | None = None) -> KnowledgeIndexStatus:
        """Build (or complete, or switch) the semantic index with an embedding model, in the background.

        Searches keep using the current vectors until every chunk has a vector of the new model; then the index
        switches in one transaction. Cancelled or interrupted, starting it again continues where it stopped.

        With a :attr:`reindex_queue` (a server's API process, RT-046) the reindex is queued for a worker process
        instead of running in a thread here: at most one per tenant on the whole server, and its state comes from
        the queue (``knowledge.reindex_running`` while one is queued or running)."""
        name, model = self._reindex_model(model_name)
        if self.reindex_queue is not None:
            self.reindex_queue.start(name)
            return self.index_status()
        folders = self._folders_embeddable(model)
        with self._lock:
            if self._job is not None and self._job.state == "running":
                raise NodestroError("knowledge.reindex_running", "The semantic index is already being built")
            job = ReindexJob(model=name)
            cancel = threading.Event()
            self._job, self._job_cancel = job, cancel
            self._reindex_folders = folders
            if not folders:
                self.folder_reset()
        threading.Thread(
            target=self._build, args=(job, model, cancel, None), name="nodestro-reindex", daemon=True
        ).start()
        return self.index_status()

    def execute_reindex(
        self,
        model_name: str,
        *,
        cancel: threading.Event,
        progress: Callable[[int, int], None] | None = None,
    ) -> ReindexJob:
        """Run a reindex to its end in the calling thread and return its outcome (a server worker's job, RT-046);
        never raises for the reindex's own problems: they are the outcome's ``error_code`` and ``error``."""
        job = ReindexJob(model=model_name)
        try:
            _, model = self._reindex_model(model_name)
        except NodestroError as exc:
            job.state, job.error_code, job.error = "failed", exc.code, str(exc)
            job.finished_at = datetime.now(UTC)
            return job
        folders = self._folders_embeddable(model)
        with self._lock:
            self._reindex_folders = folders
            if not folders:
                self.folder_reset()
        self._build(job, model, cancel, progress)
        return job

    def _build(
        self,
        job: ReindexJob,
        model: EmbeddingModel,
        cancel: threading.Event,
        progress: Callable[[int, int], None] | None,
    ) -> None:
        """The reindex itself; its outcome is written to ``job``. While its model may not embed folder text, no
        folder chunk is stored (:meth:`replace_chunks`), and the model refuses any batch while a folder chunk exists
        (a last guard, :class:`_WithoutFolderText`)."""
        reindex = self.index.reindex  # type: ignore[attr-defined]
        if not self._reindex_folders:
            model = cast(EmbeddingModel, _WithoutFolderText(model, self._has_folder_chunks))

        def report(done: int, total: int) -> None:
            job.completed, job.total = done, total
            if progress is not None:
                progress(done, total)

        try:
            for attempt in range(3):  # chunks added meanwhile: embed them too
                try:
                    reindex(model, progress=report, cancel=cancel)
                    break
                except RetrievalError as exc:
                    # The last attempt's vector_incomplete fails the job: it has not switched (before, it said done).
                    if exc.code != "vector_incomplete" or attempt == 2:
                        raise
            job.state = "done"
        except RetrievalError as exc:
            job.state = "cancelled" if exc.code == "cancelled" else "failed"
            job.error_code, job.error = f"knowledge.{exc.code}", str(exc)
        except OllamaError as exc:
            job.state = "failed"
            job.error_code, job.error = f"local.{exc.code}", str(exc)
        except Exception as exc:  # reported on the job
            logger.exception("Building the semantic index failed")
            job.state = "failed"
            job.error_code, job.error = "knowledge.reindex_failed", str(exc)
        finally:
            with self._lock:
                self._reindex_folders = True
                if isinstance(getattr(self.index, "embeddings", None), _WithoutFolderText):
                    self.index.embeddings = self.index.embeddings.model  # type: ignore[attr-defined]
            job.finished_at = datetime.now(UTC)

    def _current_job(self) -> ReindexJob | None:
        """The latest reindex: the queue's on a server's API process, else this process's thread's."""
        if self.reindex_queue is not None:
            return self.reindex_queue.current()
        return self._job

    def reindexing(self) -> bool:
        """Whether this process builds the semantic index now (in its own thread)."""
        job = self._job
        return job is not None and job.state == "running"

    def cancel_reindex(self) -> KnowledgeIndexStatus:
        if self.reindex_queue is not None:
            self.reindex_queue.cancel()
        elif self._job_cancel is not None:
            self._job_cancel.set()
        return self.index_status()

    def drop_vectors(self) -> KnowledgeIndexStatus:
        """Remove the semantic index (full-text search stays)."""
        job = self._current_job()
        if job is not None and job.state == "running":
            raise NodestroError("knowledge.reindex_running", "Stop building the semantic index first")
        drop = getattr(self.index, "drop_vectors", None)
        if drop is not None:
            with self._lock:
                drop()
                self.index.embeddings = None  # type: ignore[attr-defined]
        return self.index_status()

    def effective_mode(self, config: KnowledgeConfig) -> RetrievalMode:
        return self.retrieval()[0] if config.retrieval == "default" else config.retrieval

    def check_mode(self, mode: RetrievalMode) -> None:
        """Refuse a vector or hybrid search the semantic index cannot serve (never a silent full-text fallback)."""
        if mode == "fts":
            return
        status = self.index_status()
        if status.state in ("off", "unavailable"):
            raise NodestroError(
                "knowledge.vectors_missing",
                f"{mode} search needs the semantic index: build it in the knowledge settings, or use full-text search",
                mode=mode,
            )
        if status.active is None:
            # A first semantic index is being built: until it is complete, vector search has nothing to use.
            raise NodestroError(
                "knowledge.index_building",
                f"{mode} search can be used once the semantic index is complete",
                mode=mode,
            )

    def _prepare_search(self, mode: RetrievalMode) -> None:
        """Check the mode can be served and give the index the model of its vectors. Until a reindex to another
        chosen model completes, searches use the current vectors (status ``reindex_required`` says so)."""
        if mode != "fts":
            self.check_mode(mode)
            self._sync_embeddings()

    def _attach_file_id(self, file_id: str) -> None:
        """Let every chunk of a file point to it (an agent can then load the whole file after a hit)."""
        self.chunks.attach_file(file_id)

    def restore_file(self, entry: StoredFile, data: bytes, text: str) -> None:
        """Store a file under its original id with its readable text (an exported project's knowledge)."""
        with self._lock:
            self.store._save(entry, data)
            self.index.delete(entry.id)
            if text.strip():
                self._index_checked(lambda: self.index.add(text, source=entry.id, title=entry.name, file_id=entry.id))

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
            self._index_with_fallback(
                lambda: self.index.add(text, source=file_id, title=entry.name, file_id=file_id, metadata={"ocr": "yes"})
            )
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
        return self.chunks.text(file_id)

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

    def delete(self, file_id: str, *, actor: str | None = None) -> None:
        self._delete(file_id)
        self._activity("file.deleted", file_id, actor)

    def _delete(self, file_id: str) -> None:
        with self._lock:
            self.store.delete(file_id)
            self.index.delete(file_id)

    def _activity(self, action: str, file_id: str, actor: str | None) -> None:
        """One activity event (:mod:`nodestro_runtime.activity`) with the file's id, never its name or text."""
        if self.activity is not None:
            activity.add(
                self.activity, "file", action, actor=actor, resource_type="knowledge_file", resource_id=file_id
            )

    def search(
        self,
        query: str,
        file_ids: Iterable[str] | None = None,
        k: int = 8,
        *,
        mode: RetrievalMode | None = None,
        granted: bool = False,
    ) -> list[Hit]:
        """Search the knowledge base (``mode``: default the workspace's), limited to ``file_ids`` before ranking.
        ``granted``: the query holds granted content (N-08), so a search that embeds it asks the embedding check
        first (``access.*`` when refused)."""
        chosen = mode or self.retrieval()[0]
        self._prepare_search(chosen)
        if granted and chosen != "fts":
            self._check_embedding(self._embeddings_address)
        # Only knowledge files: the chunks of a granted folder are found through the folder tools alone (N-12).
        if file_ids is not None:
            sources: list[str] | None = [item for item in file_ids if not item.startswith(FOLDER_SOURCE)]
        else:
            sources = [entry.id for entry in self.store.list()] if self.folder_sources else None
        try:
            return self.index.retrieve(query, k, mode=chosen, sources=sources)
        except RetrievalError as exc:
            if exc.code != "reindex_required":
                raise
            # The vectors belong to another model or build (e.g. a new digest after a pull): a reindex is needed.
            raise NodestroError("knowledge.reindex_required", str(exc)) from None

    # ---- granted folders (N-12, nodestro_runtime.folder_index) --------------------------------------------------

    def writing(self) -> AbstractContextManager[Any]:
        """The write lock of the index: a folder index checks its grant and stores a file's chunks under it, so a
        revocation that drops the folder's chunks under the same lock leaves nothing behind."""
        return self._lock

    def chunk_sources(self) -> list[str]:
        """Every source the index holds chunks of."""
        return list(self._chunk_counts())

    def remove_sources(self, sources: Iterable[str]) -> int:
        """Remove every chunk (and its vectors) of ``sources``; returns how many were removed."""
        with self._lock:
            return sum(self.index.delete(source) for source in sources)

    def replace_chunks(self, source: str, chunks: list[Chunk]) -> int:
        """Replace the chunks of one granted folder's file with ``chunks`` (embedded when the semantic index is on
        and :attr:`folder_embedding_allowed` allows the model's address, else without vectors); returns how many are
        stored. Refuses the id of a knowledge file. :class:`FolderIndexBusy` while a reindex runs whose model may not
        embed folder text (it would embed every chunk the index holds)."""
        if not source.startswith(FOLDER_SOURCE):
            raise ValueError("Only a granted folder's source can be replaced here")

        def work() -> None:
            self.index.delete(source)
            if chunks:
                self.index.add_chunks(chunks)

        with self._lock:
            if not self._reindex_folders:
                raise FolderIndexBusy("A reindex runs whose embedding model may not embed folder text")
            self._index_with_fallback(work, self.folder_embedding_allowed)
        return len(chunks)

    def search_sources(
        self,
        query: str,
        sources: list[str],
        k: int,
        *,
        before_embed: Callable[[str | None], None] | None = None,
    ) -> list[Hit]:
        """Search the chunks of a granted folder's ``sources`` in the workspace's retrieval mode, in full text while
        the semantic index cannot serve it, and while :attr:`folder_embedding_allowed` refuses the model's address
        (the query is then never embedded). ``before_embed`` is the run's check before the query is embedded (N-08):
        it raises to refuse."""
        if not sources:
            return []
        chosen = self.retrieval()[0]
        if chosen != "fts":
            try:
                self.check_mode(chosen)
            except NodestroError:
                chosen = "fts"
        if chosen != "fts":
            self._sync_embeddings()
            if not self.folder_embedding_allowed(self._embeddings_address):
                chosen = "fts"  # the query is never sent to an embedding server folder text may not go to
            elif before_embed is not None:
                before_embed(self._embeddings_address)
        try:
            return self.index.retrieve(query, k, mode=chosen, sources=sources)
        except RetrievalError as exc:
            if exc.code != "reindex_required":
                raise
            raise NodestroError("knowledge.reindex_required", str(exc)) from None

    # ---- per-agent views -----------------------------------------------------------

    def views(
        self, config: KnowledgeConfig, *, before_embed: Callable[[str | None], None] | None = None
    ) -> tuple[ScopedFileStore | None, ScopedIndex | None]:
        """The store and index an agent gets for ``config`` (``None`` where it gets none). ``before_embed``: the run's
        check before a search embeds its query (N-08, :attr:`ScopedIndex.before_embed`), given the address the
        embedding model sends to; the model is built again first when the server's address changed."""
        file_ids = [file_id for file_id in config.file_ids if file_id in self.store]
        if not file_ids or config.mode == "context":
            return None, None
        if config.mode == "on_demand":
            return ScopedFileStore(self.store, file_ids), None
        mode = self.effective_mode(config)
        self._prepare_search(mode)

        def prepared() -> None:
            self._sync_embeddings()
            if before_embed is not None:
                before_embed(self._embeddings_address)

        return None, ScopedIndex(self.index, file_ids, mode, before_embed=prepared)
