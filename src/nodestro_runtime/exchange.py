"""Versioned exchange formats with one import check (N-14): MCP services, service connections, knowledge collections.

Every exchange file names its format and version in one string, ``<format>/<version>`` (as ``nodestro-agent/1`` of
agent templates does):

``nodestro-mcp/1``         JSON: the configuration of MCP services (address or stdio command, kind of sign-in, limits);
                           in :mod:`nodestro_runtime.mcp.exchange`, with the MCP client.
``nodestro-connection/1``  JSON: service connections (type, provider, name, description, settings).
``nodestro-collection/1``  ZIP: one knowledge collection: ``manifest.json`` (name, retrieval settings, the embedding
                           identity of the index it came from, one entry per document with size and SHA-256) and the
                           documents' stored bytes under ``documents/<nnnn>``.
``nodestro-workflow/1``    JSON: a workflow export; the workflow import checks the version (nodestro-api).

Secrets never travel. The MCP and connection formats have no field for one, and an export is checked before it is
written (:func:`ensure_no_secrets`): a field with a secret-like name, an address with credentials or a token in it, a
value shaped like a well-known key, or any secret the workspace holds for one of the exported entries refuses the
export (``exchange.secret_detected``, naming the fields, never the values).

An import is untrusted content. One check serves every format (:func:`read_json`, :func:`open_collection`): the size
limit first, then the format and version (a newer version than this runtime knows is refused with
``exchange.version_newer``, an unknown format with ``exchange.format_invalid``, another format with
``exchange.format_mismatch``), then the content against strict models (unknown fields are refused,
``exchange.content_invalid``); a collection's ZIP additionally gets the path, entry count, ZIP bomb and checksum
checks. An import always has a preview (``dry_run``) that runs every rule of adding the entries by hand and stores
nothing; applying it stores all entries or none.

What an import never does:

- attach a secret: an imported MCP service or connection is a new entry (new id) without a token or secret, so no
  secret of this workspace is bound to it; the report lists what has to be entered (``secrets_to_reenter``, the labels
  of ``nodestro-backup``: ``mcp:<id>``, ``connection:<id>``);
- switch anything on: imported MCP services are stored switched off, with no tool approved (discovery and approval
  happen here again); a stdio server still needs the Docker sandbox (``sandbox.isolation_required``) and the approval
  of its packages; the workspace's settings (retrieval mode, embedding model, OCR) are never changed;
- take vectors: a collection's documents are indexed again by this workspace (with its own embedding model when the
  semantic index is on), so the embedding identity in the manifest only tells whether it matches.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import re
import stat
from typing import IO, TYPE_CHECKING, Any, Literal, TypeVar
import zipfile
import zlib

from orkexa.embeddings import EmbeddingIdentity
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import __version__
from .connections import ConnectionIn
from .net_address import url_secret_problem
from .problems import NodestroError
from .providers import has_credentials
from .schemas import now

if TYPE_CHECKING:
    from .connections import ConnectionService, ServiceConnection
    from .knowledge import KnowledgeBase

#: The newest version of each format this runtime reads and writes.
FORMATS: dict[str, int] = {
    "nodestro-mcp": 1,
    "nodestro-connection": 1,
    "nodestro-collection": 1,
    "nodestro-workflow": 1,
}
_FORMAT = re.compile(r"^(nodestro-[a-z]{1,30})/([0-9]{1,6})$")

MIB = 1024 * 1024
#: The largest JSON exchange file (MCP services, connections).
MAX_JSON_BYTES = 1_000_000
#: Entries of one JSON exchange file.
MAX_ITEMS = 100
#: The largest collection file, its unpacked size, its documents and one document (the knowledge upload limit).
MAX_COLLECTION_BYTES = 256 * MIB
MAX_UNPACKED_BYTES = 512 * MIB
MAX_DOCUMENTS = 500
MAX_DOCUMENT_BYTES = 50 * MIB
MAX_MANIFEST_BYTES = 2 * MIB
#: An entry larger than 1 MiB may expand at most this many times (a "ZIP bomb" expands far more).
MAX_RATIO = 100
_RATIO_FLOOR = MIB
_DOCUMENT_PATH = re.compile(r"^documents/[0-9]{4}$")
#: Field names that hold a secret.
_SECRET_KEY = re.compile(r"(secret|token|passw|api_?key|apikey|authorization|credential|private_?key|sealed)", re.I)
#: Values shaped like well-known keys and tokens.
_SECRET_SHAPE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|-----BEGIN [A-Z ]*PRIVATE KEY-----|bearer\s+[A-Za-z0-9._~+/=-]{20,})",
    re.I,
)


class ExchangeError(NodestroError):
    """A refused export or import (``exchange.*``, or the code of the rule that refused an entry)."""


# ---- the shared import check ----------------------------------------------------------------------------------------


def format_version(value: object, expected: str) -> int:
    """The version of an exchange file whose ``format`` is ``value``, which must be ``expected`` in a version this
    runtime knows."""
    newest = FORMATS[expected]
    found = _FORMAT.fullmatch(value) if isinstance(value, str) else None
    if found is None or int(found.group(2)) < 1:
        raise ExchangeError(
            "exchange.format_invalid",
            f"The file is not a Nodestro exchange file ({expected}/{newest})",
            expected=f"{expected}/{newest}",
        )
    name, version = found.group(1), int(found.group(2))
    if name != expected:
        raise ExchangeError(
            "exchange.format_mismatch", f"The file holds {name}, not {expected}", found=name, expected=expected
        )
    if version > newest:
        raise ExchangeError(
            "exchange.version_newer",
            f"{name}/{version} is newer than this installation reads ({name}/{newest})",
            version=version,
            supported=newest,
        )
    return version


def read_json(raw: bytes, expected: str, *, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    """The checked JSON object of an exchange file: size, JSON, format and version (not yet the content)."""
    if len(raw) > max_bytes:
        raise ExchangeError(
            "exchange.too_large", f"An exchange file may have at most {max_bytes} bytes", limit=max_bytes
        )
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError):
        raise ExchangeError("exchange.invalid_json", "The file is not valid JSON") from None
    if not isinstance(document, dict):
        raise ExchangeError("exchange.format_invalid", "The file is not a Nodestro exchange file", expected=expected)
    format_version(document.get("format"), expected)
    return document


def _problems(exc: ValidationError) -> list[dict[str, str]]:
    """Where and why the content is invalid, never the values (they are untrusted and could be secrets)."""
    return [
        {"field": ".".join(str(part) for part in error.get("loc", ())) or "file", "reason": str(error.get("type"))}
        for error in exc.errors(include_input=False, include_url=False)[:20]
    ]


_M = TypeVar("_M", bound=BaseModel)


def parse(model: type[_M], document: dict[str, Any]) -> _M:
    """``document`` as ``model``: its content checked, unknown fields refused (``exchange.content_invalid``)."""
    try:
        return model.model_validate(document)
    except ValidationError as exc:
        raise ExchangeError(
            "exchange.content_invalid", "The content of the file is not valid", problems=_problems(exc)
        ) from None


# ---- the secret check of an export ----------------------------------------------------------------------------------


def secret_findings(document: Any, known: Iterable[str] = ()) -> list[str]:
    """Where ``document`` holds something secret-like: a non-empty field with a secret's name, an address with
    credentials or a key in it, a value shaped like a well-known key, or one of the ``known`` secrets (also inside a
    longer text). The places are field paths (``servers[0].url``); a known secret found is ``(known secret)``."""
    found: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                where = f"{path}.{key}" if path else str(key)
                if _SECRET_KEY.search(str(key)) and item not in (None, "", [], {}, False):
                    found.append(where)
                walk(item, where)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        elif isinstance(value, str) and (
            _SECRET_SHAPE.search(value)
            or ("://" in value and (has_credentials(value) or url_secret_problem(value) is not None))
        ):
            found.append(path)

    walk(document, "")
    text = json.dumps(document, ensure_ascii=False)
    for secret in known:
        if len(secret) >= 4 and (secret in text or json.dumps(secret, ensure_ascii=False)[1:-1] in text):
            found.append("(known secret)")
            break
    return sorted(set(found))


def ensure_no_secrets(document: Any, known: Iterable[str] = ()) -> None:
    """Refuse an export that would hold a secret (``exchange.secret_detected``, naming the places only)."""
    places = secret_findings(document, known)
    if places:
        raise ExchangeError(
            "exchange.secret_detected",
            "The export would contain a secret, so it was not written",
            places=places[:20],
        )


def secret_values(value: str | None) -> list[str]:
    """A stored secret and, for a stored JSON document (OAuth credentials), every longer text in it."""
    if not value:
        return []
    values = [value]
    try:
        parsed = json.loads(value)
    except ValueError:
        return values

    def walk(item: Any) -> Iterator[str]:
        if isinstance(item, str) and len(item) >= 8:
            yield item
        elif isinstance(item, dict):
            for inner in item.values():
                yield from walk(inner)
        elif isinstance(item, list):
            for inner in item:
                yield from walk(inner)

    return values + list(walk(parsed))


# ---- reports ---------------------------------------------------------------------------------------------------------


class ImportItem(BaseModel):
    """One entry of an import: ``ready`` (the preview found no problem), ``refused`` (``code`` and ``params`` say why;
    the same as adding it by hand would answer), ``created`` (``id`` is the new entry's) or ``exists`` (a document
    whose content this workspace has already, ``id``: that file; it is left as it is)."""

    kind: Literal["mcp_server", "connection", "document"]
    name: str
    status: Literal["ready", "refused", "created", "exists"] = "ready"
    code: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None
    #: Whether a secret (token, sign-in, connection secret) has to be entered before the entry can be used.
    needs_secret: bool = False


class EmbeddingMatch(BaseModel):
    """The embedding identity of a collection's source index and of this workspace's (``None``: no vectors)."""

    package: dict[str, Any] | None = None
    workspace: dict[str, Any] | None = None
    #: Both have vectors of the same model build; the documents are embedded here again either way.
    matches: bool = False


class ImportReport(BaseModel):
    format: str
    dry_run: bool
    items: list[ImportItem] = Field(default_factory=list)
    #: After applying: the secrets to enter (``mcp:<id>``, ``connection:<id>``, as ``nodestro-backup`` names them).
    secrets_to_reenter: list[str] = Field(default_factory=list)
    #: What an import never takes over (``enabled``, ``tool_approvals``, ``secrets``, ``vectors``, ``provenance``).
    left_out: list[str] = Field(default_factory=list)
    #: A collection import: the collection the documents go to, whether it has files already, the embedding check.
    collection: str | None = None
    collection_exists: bool = False
    embedding: EmbeddingMatch | None = None


def refuse_item(item: ImportItem, exc: NodestroError) -> None:
    # The params of an error answer: the documented parameters of the code (never a secret).
    item.status, item.code, item.params = "refused", exc.code, dict(exc.params)


def require_no_refusals(report: ImportReport) -> None:
    refused = [item for item in report.items if item.status == "refused"]
    if refused:
        raise ExchangeError(
            "exchange.items_refused",
            f"{len(refused)} entries of the file cannot be imported; the preview names them",
            refused=len(refused),
            first=refused[0].code or "",
        )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Envelope(StrictModel):
    format: str
    created_at: datetime | None = None
    producer: str = Field(default="", max_length=200)


def envelope(name: str) -> dict[str, Any]:
    return {
        "format": f"{name}/{FORMATS[name]}",
        "created_at": now().isoformat(),
        "producer": f"nodestro-runtime {__version__}",
    }


# ---- nodestro-connection/1 -------------------------------------------------------------------------------------------


class ConnectionEntry(StrictModel):
    """A connection's configuration: never its id, secret, reachable hosts (derived again on import) or check."""

    type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    name: str = Field(min_length=1, max_length=80)
    provider: str = Field(default="generic", pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    description: str = Field(default="", max_length=500)
    settings: dict[str, Any] = Field(default_factory=dict)


class ConnectionPackage(Envelope):
    connections: list[ConnectionEntry] = Field(max_length=MAX_ITEMS)


def connection_entry(item: ServiceConnection) -> ConnectionEntry:
    return ConnectionEntry(
        type=item.type, name=item.name, provider=item.provider, description=item.description, settings=item.settings
    )


def export_connections(service: ConnectionService, connection_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """``nodestro-connection/1`` of the workspace's connections (``connection_ids``: only these), checked for
    secrets."""
    wanted = None if connection_ids is None else set(connection_ids)
    items = [item for item in service.connections() if wanted is None or item.id in wanted]
    if wanted is not None and len(items) != len(wanted):
        missing = sorted(wanted - {item.id for item in items})[0]
        raise ExchangeError("connection.not_found", f"Connection not found: {missing}", id=missing)
    document = {
        **envelope("nodestro-connection"),
        "connections": [connection_entry(item).model_dump(mode="json") for item in items],
    }
    # The secrets themselves (in memory only) and their sealed form: neither may be in the file.
    known = [value for item in items for value in secret_values(service._secret(item))]
    known += [item.secret_sealed for item in items if item.secret_sealed]
    ensure_no_secrets(document, known)
    return document


def _connection_input(entry: ConnectionEntry) -> ConnectionIn:
    return ConnectionIn(**entry.model_dump())


def import_connections(
    service: ConnectionService, raw: bytes, *, dry_run: bool, actor: str | None = None
) -> ImportReport:
    """Preview (``dry_run``) or apply a ``nodestro-connection/1`` file: every connection is checked by the rules of
    creating one by hand (type, provider and mode, settings, address rules, unique name); applying creates all of
    them without secrets, or none."""
    package = parse(ConnectionPackage, read_json(raw, "nodestro-connection"))
    report = ImportReport(format=package.format, dry_run=dry_run, left_out=["secrets", "checks"])
    names: set[str] = set()
    for entry in package.connections:
        item = ImportItem(kind="connection", name=entry.name)
        try:
            kind = service.check_new(_connection_input(entry))
            item.needs_secret = kind.needs_secret(entry.provider)
            if entry.name.strip().lower() in names:
                raise ExchangeError(
                    "connection.name_taken", f"A connection named {entry.name!r} exists already", name=entry.name
                )
        except NodestroError as exc:
            refuse_item(item, exc)
        names.add(entry.name.strip().lower())
        report.items.append(item)
    if dry_run:
        return report
    require_no_refusals(report)
    created: list[str] = []
    try:
        for item, entry in zip(report.items, package.connections, strict=True):
            connection = service.create(_connection_input(entry), actor=actor)
            created.append(connection.id)
            item.status, item.id = "created", connection.id
    except BaseException:
        for connection_id in created:
            service.delete(connection_id, actor=actor, force=True)
        raise
    report.secrets_to_reenter = [f"connection:{item.id}" for item in report.items if item.needs_secret]
    return report


# ---- nodestro-collection/1 -------------------------------------------------------------------------------------------


class CollectionDocument(StrictModel):
    path: str = Field(pattern=_DOCUMENT_PATH.pattern)
    name: str = Field(min_length=1, max_length=500)
    media_type: str = Field(min_length=3, max_length=200, pattern=r"^[A-Za-z0-9.+_-]+/[A-Za-z0-9.+_-]+$")
    description: str = Field(default="", max_length=20_000)
    size: int = Field(ge=0, le=MAX_DOCUMENT_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: The document was converted to Markdown on upload (its stored bytes are that text).
    source_format: Literal["docx", "xlsx"] | None = None
    notes: dict[str, str] = Field(default_factory=dict, max_length=20)


class CollectionSettings(StrictModel):
    """The retrieval settings of the workspace the collection came from (informative: an import never changes the
    settings of this workspace)."""

    retrieval_mode: Literal["fts", "vector", "hybrid"] = "fts"
    embedding_model: str | None = Field(default=None, max_length=200)


class EmbeddingEntry(StrictModel):
    provider: str = Field(max_length=100)
    model: str = Field(max_length=200)
    dimensions: int = Field(ge=1, le=100_000)
    digest: str = Field(default="", max_length=200)
    normalized: bool = True
    document_instruction: str = Field(default="", max_length=2000)
    query_instruction: str = Field(default="", max_length=2000)
    document_prefix: str = Field(default="", max_length=200)
    query_prefix: str = Field(default="", max_length=200)


class CollectionManifest(Envelope):
    name: str = Field(min_length=1, max_length=120)
    settings: CollectionSettings = Field(default_factory=CollectionSettings)
    embedding: EmbeddingEntry | None = None
    documents: list[CollectionDocument] = Field(max_length=MAX_DOCUMENTS)


def _collection_files(knowledge: KnowledgeBase, name: str) -> list[Any]:
    wanted = name.strip()
    return [entry for entry in knowledge.store.list() if entry.metadata.get("collection", "") == wanted and wanted]


def _active_identity(knowledge: KnowledgeBase) -> dict[str, Any] | None:
    try:
        active = knowledge.index_status().active
        return EmbeddingIdentity.from_dict(active).to_dict() if active else None
    except (KeyError, TypeError, ValueError):
        return None


def export_collection(knowledge: KnowledgeBase, name: str, target: IO[bytes]) -> CollectionManifest:
    """Write ``nodestro-collection/1`` of the collection ``name`` to ``target`` (a binary file)."""
    files = _collection_files(knowledge, name)
    if not files:
        raise ExchangeError("exchange.collection_not_found", f"There is no collection {name!r}", name=name[:120])
    if len(files) > MAX_DOCUMENTS:
        raise ExchangeError("exchange.too_many_entries", "The collection has too many documents", limit=MAX_DOCUMENTS)
    mode, model = knowledge.retrieval()
    documents: list[CollectionDocument] = []
    total = 0
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for number, entry in enumerate(sorted(files, key=lambda item: (item.name.casefold(), item.id)), start=1):
            data = knowledge.store.get_bytes(entry.id)
            total += len(data)
            if len(data) > MAX_DOCUMENT_BYTES or total > MAX_UNPACKED_BYTES:
                raise ExchangeError(
                    "exchange.too_large", "The collection is too large to export", limit=MAX_UNPACKED_BYTES
                )
            source_format = entry.metadata.get("source_format") or None
            document = CollectionDocument(
                path=f"documents/{number:04d}",
                name=entry.name,
                media_type=entry.media_type,
                description=entry.description,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                source_format=source_format if source_format in ("docx", "xlsx") else None,
                notes={key[5:]: value for key, value in entry.metadata.items() if key.startswith("note_")},
            )
            archive.writestr(document.path, data)
            documents.append(document)
        identity = _active_identity(knowledge)
        manifest = CollectionManifest(
            **envelope("nodestro-collection"),
            name=name.strip(),
            settings=CollectionSettings(retrieval_mode=mode, embedding_model=model),
            embedding=EmbeddingEntry(**identity) if identity else None,
            documents=documents,
        )
        archive.writestr("manifest.json", manifest.model_dump_json(indent=2))
    return manifest


def _declared_entries(source: IO[bytes], size: int) -> int | None:
    """The entry count the end record of a ZIP file declares (read before zipfile indexes every entry)."""
    tail = min(size, 65_557)
    source.seek(size - tail)
    data = source.read(tail)
    at = data.rfind(b"PK\x05\x06")
    if at < 0 or len(data) < at + 12:
        return None
    return int.from_bytes(data[at + 10 : at + 12], "little")


@contextmanager
def _reading(name: str) -> Iterator[None]:
    """A damaged entry (wrong CRC, a lying size, a cut-off stream) is a refusal, not an internal error."""
    try:
        yield
    except (zipfile.BadZipFile, zlib.error, EOFError):
        raise ExchangeError("exchange.checksum", f"{name} is damaged", name=name) from None
    except (RuntimeError, NotImplementedError):
        raise ExchangeError("exchange.zip_invalid", f"{name} cannot be read", name=name) from None


def _read(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    with _reading(name), archive.open(name) as handle:
        # One byte more than allowed: a lying header cannot make this read past the limit.
        return handle.read(limit + 1)


def open_collection(source: IO[bytes]) -> tuple[zipfile.ZipFile, CollectionManifest]:
    """Check a ``nodestro-collection/1`` file completely (size, entries, paths, ZIP bomb, format and version, content,
    the files against the manifest) and return the open archive with its manifest. The documents' checksums are
    checked by :func:`read_document`."""
    size = source.seek(0, 2)
    if size > MAX_COLLECTION_BYTES:
        raise ExchangeError("exchange.too_large", "A collection file is too large", limit=MAX_COLLECTION_BYTES)
    declared = _declared_entries(source, size)
    if declared is not None and declared > MAX_DOCUMENTS + 1:
        raise ExchangeError("exchange.too_many_entries", "The file has too many entries", limit=MAX_DOCUMENTS + 1)
    source.seek(0)
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, ValueError, OSError, EOFError):
        raise ExchangeError("exchange.zip_invalid", "The file is not a readable ZIP file") from None
    try:
        manifest = _check_archive(archive)
    except BaseException:
        archive.close()
        raise
    return archive, manifest


def _check_archive(archive: zipfile.ZipFile) -> CollectionManifest:
    entries = archive.infolist()
    if len(entries) > MAX_DOCUMENTS + 1:
        raise ExchangeError("exchange.too_many_entries", "The file has too many entries", limit=MAX_DOCUMENTS + 1)
    names: dict[str, zipfile.ZipInfo] = {}
    unpacked = 0
    for info in entries:
        name = info.orig_filename
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode) or (mode and stat.S_IFMT(mode) and not stat.S_ISREG(mode)):
            raise ExchangeError(
                "exchange.unsafe_path", f"{name!r} is not a regular file", name=name[:200], reason="link"
            )
        if name in names:
            raise ExchangeError(
                "exchange.unsafe_path", f"{name!r} is in the file twice", name=name[:200], reason="duplicate"
            )
        if name != "manifest.json" and not _DOCUMENT_PATH.fullmatch(name):
            if name.startswith("/") or "\\" in name or ":" in name or ".." in name.split("/"):
                raise ExchangeError(
                    "exchange.unsafe_path", f"Unsafe path in the file: {name!r}", name=name[:200], reason="path"
                )
            raise ExchangeError("exchange.unexpected_file", f"Unexpected entry in the file: {name!r}", name=name[:200])
        if info.flag_bits & 0x1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ExchangeError("exchange.zip_invalid", f"{name} cannot be read", name=name)
        if info.file_size > _RATIO_FLOOR and info.file_size > max(info.compress_size, 1) * MAX_RATIO:
            raise ExchangeError(
                "exchange.zip_bomb", f"{name} expands more than {MAX_RATIO} times", name=name, limit=MAX_RATIO
            )
        unpacked += info.file_size
        if unpacked > MAX_UNPACKED_BYTES:
            raise ExchangeError("exchange.too_large", "The unpacked file is too large", limit=MAX_UNPACKED_BYTES)
        names[name] = info
    if "manifest.json" not in names:
        raise ExchangeError("exchange.incomplete", "The file has no manifest")
    raw = _read(archive, "manifest.json", MAX_MANIFEST_BYTES)
    manifest = parse(CollectionManifest, read_json(raw, "nodestro-collection", max_bytes=MAX_MANIFEST_BYTES))
    listed = [document.path for document in manifest.documents]
    if len(set(listed)) != len(listed) or set(listed) != set(names) - {"manifest.json"}:
        raise ExchangeError("exchange.incomplete", "The files in the file do not match its manifest")
    for document in manifest.documents:
        if names[document.path].file_size != document.size:
            raise ExchangeError("exchange.checksum", f"{document.path} is damaged", name=document.path)
    return manifest


def read_document(archive: zipfile.ZipFile, document: CollectionDocument) -> bytes:
    """A document's bytes, checked against its size and SHA-256 in the manifest."""
    data = _read(archive, document.path, document.size)
    if len(data) != document.size or hashlib.sha256(data).hexdigest() != document.sha256:
        raise ExchangeError("exchange.checksum", f"{document.path} is damaged (checksum mismatch)", name=document.path)
    return data


def import_collection(
    knowledge: KnowledgeBase,
    source: IO[bytes],
    *,
    dry_run: bool,
    collection: str | None = None,
    actor: str | None = None,
) -> ImportReport:
    """Preview (``dry_run``) or apply a ``nodestro-collection/1`` file: into a new collection (``collection``, else
    the manifest's name; one that has files already is refused, ``exchange.collection_exists``). Every document is
    checked against the manifest first; applying stores all of them with the upload's rules (conversion, PDF limits,
    indexing by this workspace's own model) or none."""
    archive, manifest = open_collection(source)
    with archive:
        target = (collection if collection is not None else manifest.name).strip()
        if not target or len(target) > 120:
            raise ExchangeError("exchange.collection_name_invalid", "A collection's name has 1 to 120 characters")
        workspace = _active_identity(knowledge)
        package = manifest.embedding.model_dump() if manifest.embedding is not None else None
        report = ImportReport(
            format=manifest.format,
            dry_run=dry_run,
            left_out=["vectors", "provenance"],
            collection=target,
            collection_exists=bool(_collection_files(knowledge, target)),
            embedding=EmbeddingMatch(
                package=package,
                workspace=workspace,
                matches=package is not None
                and workspace is not None
                and EmbeddingIdentity.from_dict(package).key == EmbeddingIdentity.from_dict(workspace).key,
            ),
        )
        # The file store keeps one entry per content: storing a document it has already would rename and move that
        # file (and undoing a failed import would delete it), so such a document is left out and named.
        present = {entry.sha256: entry.id for entry in knowledge.store.list()}
        contents: dict[int, bytes] = {}
        for number, document in enumerate(manifest.documents):
            item = ImportItem(kind="document", name=document.name)
            try:
                data = read_document(archive, document)
            except NodestroError as exc:
                refuse_item(item, exc)
            else:
                if document.sha256 in present:
                    item.status, item.id = "exists", present[document.sha256] or None
                else:
                    contents[number] = data
                    present[document.sha256] = ""
            report.items.append(item)
        if dry_run:
            return report
        require_no_refusals(report)
        if report.collection_exists:
            raise ExchangeError(
                "exchange.collection_exists", f"The collection {target!r} has files already", name=target
            )
        known = {entry.id for entry in knowledge.store.list()}
        added: list[str] = []
        try:
            for number, data in contents.items():
                item, document = report.items[number], manifest.documents[number]
                stored = knowledge.add(
                    data,
                    name=document.name,
                    description=document.description,
                    media_type=document.media_type,
                    collection=target,
                    extra={f"note_{key}": value for key, value in document.notes.items()},
                    actor=actor,
                    converted_from=document.source_format,
                )
                if stored.id in known:  # converted on upload into a content this workspace has already
                    item.status, item.id = "exists", stored.id
                    continue
                known.add(stored.id)
                added.append(stored.id)
                item.status, item.id = "created", stored.id
        except BaseException:
            for file_id in added:
                knowledge.delete(file_id, actor=actor)
            raise
        return report
