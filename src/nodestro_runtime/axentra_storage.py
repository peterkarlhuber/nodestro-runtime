"""Input file bytes in Axentra's storage (server profile, ``NODESTRO_FILES_BACKEND=axentra``; see ``docs/AXENTRA.md``).

By default the bytes of input files live on the shared data volume below ``<data dir>/tenants/<id>/inputs``
(:class:`~nodestro_runtime.input_files.InputFiles`, an ``orkexa`` ``LocalFileStore``). With
``NODESTRO_FILES_BACKEND=axentra`` (needs ``NODESTRO_AXENTRA=on``) :class:`AxentraBlobs` stores them through Axentra's
:class:`axentra.storage.service.StorageService` instead (local directory or S3, chosen in Axentra's own
``[storage]`` settings of ``NODESTRO_AXENTRA_CONFIG``). What stays the same:

- **Ids.** Input files keep their ids (``in_<12 hex>``) and their documents; a blob keeps its id (the first 16 hex
  digits of the SHA-256). Only where the bytes are changes. Agent delivery, the studio and the REST API read through
  :class:`~nodestro_runtime.input_files.InputFiles` as before, in the API and in the worker processes alike.
- **Deduplication.** Identical content of one tenant is stored once (one Axentra file per blob).

What Axentra adds: per-tenant objects (``<product>/<axentra tenant>/<file>``, its row-level security), the size
limit and the tenant quotas (``storage.max_file_bytes``, ``default_quota_*``, per-tenant quotas), the content-type
policy of product ``nodestro`` (G-7: executable, HTML, SVG and script files are kept as *inert* attachments,
``application/octet-stream``; :meth:`AxentraFiles.ensure_policy` sets ``store_inert`` unless the product already has a
policy of its own, because Nodestro has always accepted code and markup files as inputs), an audit event per upload
and delete, and a SHA-256 per object. Every read is checked against the SHA-256 recorded here
(``input.integrity`` when it differs).

The mapping blob -> Axentra file is a document per blob in the tenant's collection :data:`COLLECTION` (no schema
change, tenant-isolated like every Nodestro document). A blob without a mapping is read from the volume: during the
transition (and for files imported from a desktop) the volume is the fallback.

Migration (``nodestro-server files migrate|verify|remove-source|export-volume``):

- :meth:`AxentraFiles.migrate` copies volume blobs into Axentra: per tenant, resumable and idempotent (a blob with a
  mapping is skipped), ``dry_run`` only counts. Each copy is read back and its SHA-256 compared before the mapping is
  recorded; the volume file is **never** deleted.
- :meth:`AxentraFiles.verify` reads every mapped object back, compares the SHA-256, records ``verified_at`` and
  reports missing or damaged objects, volume blobs not migrated yet and Axentra objects without a mapping.
- :meth:`AxentraFiles.remove_sources` deletes a migrated volume file only when ``NODESTRO_FILES_SOURCE_REMOVAL=1``
  (off by default), the object was verified by ``verify``, the fallback period
  (``NODESTRO_FILES_SOURCE_RETENTION_DAYS``, default 30) since its migration has passed, and a fresh read still
  matches the SHA-256.
- :meth:`AxentraFiles.export_to_volume` copies Axentra-only blobs back to the volume (the rollback to ``volume``).

A user's delete removes every copy (the Axentra object, the mapping and a volume copy), as the volume backend does.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from .file_formats import FileFormatError
from .input_files import _Blobs
from .problems import NodestroError

if TYPE_CHECKING:  # pragma: no cover
    from .axentra_platform import AxentraLink, AxentraServices
    from .storage_postgres import PostgresDatabase

logger = logging.getLogger(__name__)

#: The tenant collection that maps a blob id to its Axentra file.
COLLECTION = "file_objects"
#: The metadata key of an Axentra file that names its Nodestro blob.
BLOB_KEY = "nodestro_blob"
_BLOB = re.compile(r"^[0-9a-f]{16}$")
_MIB = 1024 * 1024


class FileObject(BaseModel):
    """Where the bytes of one blob of a tenant live in Axentra."""

    blob: str
    sha256: str
    size: int
    axentra_tenant: str
    #: ``None`` for empty content (Axentra stores no empty objects; nothing has to be read).
    axentra_file: str | None
    origin: Literal["upload", "migration"]
    #: Kept by Axentra's content-type policy as an inert attachment (served as ``application/octet-stream``).
    stored_inert: bool = False
    created_at: datetime
    #: The last successful ``files verify`` (the copy check of ``migrate`` does not count).
    verified_at: datetime | None = None
    #: When ``files remove-source`` deleted the volume copy.
    source_removed_at: datetime | None = None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FilesReport:
    """What a migration step did (ids and counts only, never content)."""

    dry_run: bool = False
    tenants: int = 0
    #: Blobs found on the volume (migrate, export: their source).
    found: int = 0
    copied: int = 0
    already: int = 0
    pending: int = 0
    bytes: int = 0
    verified: int = 0
    removed: int = 0
    kept: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)
    orphans: list[dict[str, str]] = field(default_factory=list)

    def fail(self, tenant: str, blob: str, reason: str) -> None:
        self.failed.append({"tenant": tenant, "blob": blob, "reason": reason})

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "tenants": self.tenants,
            "found": self.found,
            "copied": self.copied,
            "already": self.already,
            "pending": self.pending,
            "bytes": self.bytes,
            "verified": self.verified,
            "removed": self.removed,
            "kept": self.kept,
            "failed": list(self.failed),
            "orphans": list(self.orphans),
            "ok": not self.failed,
        }


class AxentraFiles:
    """Axentra's storage for the input files of every tenant of one server process (API, worker or a command).

    ``services`` must be started (:meth:`~nodestro_runtime.axentra_platform.AxentraServices.start`) with the
    ``storage`` module (``NODESTRO_FILES_BACKEND=axentra``)."""

    def __init__(
        self,
        services: AxentraServices,
        database: PostgresDatabase,
        *,
        link: AxentraLink | None = None,
        retention_days: int | None = None,
        source_removal: bool | None = None,
    ) -> None:
        from axentra.core.context import Principal

        from .axentra_platform import AxentraLink

        storage = services.platform.storage
        if storage is None:
            raise NodestroError(
                "profile.invalid", "NODESTRO_FILES_BACKEND=axentra needs Axentra's storage module (not configured)"
            )
        self.services = services
        self.database = database
        self.storage = storage
        self.link = link if link is not None else AxentraLink(services, database)
        config = services.config
        self.retention_days = config.files_source_retention_days if retention_days is None else retention_days
        self.source_removal = config.files_source_removal if source_removal is None else source_removal
        self.principal = Principal.system("nodestro.files")

    # ---- set-up ----------------------------------------------------------------------------------------

    def check(self) -> None:
        """Refuse a configuration that would lose files: a relative local directory depends on the working
        directory of each process (``profile.invalid``)."""
        cfg = self.storage.cfg
        if cfg.backend == "local" and not Path(cfg.local_path).is_absolute():
            raise NodestroError(
                "profile.invalid",
                "Axentra's storage.local_path must be an absolute directory shared by the API and the workers "
                f"(set it in NODESTRO_AXENTRA_CONFIG; found {cfg.local_path!r})",
            )

    def ensure_policy(self) -> str:
        """Product ``nodestro`` keeps blocked types (executables, HTML, SVG, scripts) as inert attachments unless
        the product has a policy of its own; returns the effective mode. Idempotent."""
        product_id = self.services.product_id
        policy = self.storage.content_policy(product_id)
        if policy.blocked_types_source == "installation" and policy.blocked_types != "store_inert":
            self.storage.set_policy(
                self.principal,
                product_id,
                blocked_types="store_inert",
                blocked_extensions=sorted(policy.blocked_extensions),
                blocked_content_types=sorted(policy.blocked_content_types),
                inert_extensions=sorted(policy.inert_extensions),
            )
            return "store_inert"
        return str(policy.blocked_types)

    def start(self) -> AxentraFiles:
        self.check()
        self.ensure_policy()
        return self

    # ---- per tenant ---------------------------------------------------------------------------------------

    def blobs(self, tenant_id: str, root: Path) -> AxentraBlobs:
        """The blob store of one tenant's input files (``root``: its volume directory, the fallback)."""
        return AxentraBlobs(self, tenant_id, root)

    def axentra_tenant(self, tenant_id: str) -> str:
        known = self.link.tenant(tenant_id)
        if known is not None:
            return known
        rows = self.database._query("SELECT name FROM nodestro_tenants WHERE id = %s", (tenant_id,))
        if not rows:
            raise NodestroError("identity.tenant_unknown", f"No tenant {tenant_id}", tenant=tenant_id)
        return self.link.ensure_tenant(tenant_id, str(rows[0][0]))

    def lookup(self, tenant_id: str, blob: str) -> FileObject | None:
        if not _BLOB.match(blob):
            return None
        return self.database.store(tenant_id).get(COLLECTION, blob, FileObject)

    def upload(self, tenant_id: str, data: bytes, *, blob: str, name: str, media_type: str, origin: str) -> FileObject:
        """Store ``data`` as a new Axentra file (not recorded yet; :meth:`record` does that)."""
        from axentra.core.errors import AxentraError, PayloadTooLarge, QuotaExceeded, UnsupportedMediaType

        digest = _sha256(data)
        ax_tenant = self.axentra_tenant(tenant_id)
        if not data:
            return FileObject(
                blob=blob,
                sha256=digest,
                size=0,
                axentra_tenant=ax_tenant,
                axentra_file=None,
                origin=origin,  # type: ignore[arg-type]
                created_at=_now(),
            )
        label = name.replace("\\", "/").rsplit("/", 1)[-1] or "upload"
        try:
            info = self.storage.upload(
                self.principal,
                self.services.product_id,
                ax_tenant,
                data,
                name=label,
                content_type=media_type,
                metadata={BLOB_KEY: blob, "nodestro_tenant": tenant_id},
            )
        except UnsupportedMediaType as exc:
            raise FileFormatError(
                "input.blocked_type", f"{label}: this file type is not accepted on this server ({exc})", name=name
            ) from None
        except PayloadTooLarge:
            limit = self.storage.max_file_bytes // _MIB
            raise FileFormatError(
                "input.too_large", f"{label} is larger than {limit} MB", name=name, limit_mb=limit, size=len(data)
            ) from None
        except QuotaExceeded as exc:
            raise FileFormatError("input.quota_exceeded", f"{label}: {exc}", name=name) from None
        except AxentraError as exc:
            raise NodestroError("files.unavailable", f"The file storage refused {label}: {exc}") from None
        if info.sha256 != digest or info.size != len(data):  # pragma: no cover - Axentra hashes while streaming
            self.discard(ax_tenant, info.id)
            raise NodestroError("input.integrity", f"{label} was stored damaged; nothing was kept", name=name)
        return FileObject(
            blob=blob,
            sha256=digest,
            size=len(data),
            axentra_tenant=ax_tenant,
            axentra_file=info.id,
            origin=origin,  # type: ignore[arg-type]
            stored_inert=info.stored_inert,
            created_at=_now(),
        )

    def read(self, tenant_id: str, item: FileObject) -> bytes:
        """The bytes of a mapped blob of ``tenant_id``, checked against its SHA-256."""
        from axentra.core.errors import NotFound

        if item.axentra_tenant != self.axentra_tenant(tenant_id):  # defense in depth: never another tenant's object
            raise NodestroError("input.integrity", "This file belongs to another tenant", blob=item.blob)
        if item.axentra_file is None:
            data = b""
        else:
            try:
                _, stream = self.storage.open(
                    self.principal, self.services.product_id, item.axentra_tenant, item.axentra_file
                )
                data = b"".join(stream)
            except NotFound:
                raise NodestroError(
                    "input.content_missing", "The content of this file is missing in the file storage", blob=item.blob
                ) from None
        if len(data) != item.size or _sha256(data) != item.sha256:
            logger.error("Input blob %s does not match its SHA-256 in the file storage", item.blob)
            raise NodestroError(
                "input.integrity", "The content of this file does not match its checksum", blob=item.blob
            )
        return data

    def discard(self, ax_tenant: str, file_id: str | None) -> None:
        """Delete one Axentra file (best effort: a failure is logged; ``verify`` reports what is left)."""
        from axentra.core.errors import NotFound

        if file_id is None:
            return
        try:
            self.storage.delete(self.principal, self.services.product_id, ax_tenant, file_id)
        except NotFound:
            pass
        except Exception:
            logger.exception("Deleting Axentra file %s failed; files verify reports it as an orphan", file_id)

    def record(self, tenant_id: str, item: FileObject, *, source: Path | None = None) -> FileObject:
        """Record ``item`` unless the blob is mapped already (then the new object is discarded and the existing
        mapping returned). With ``source`` (migration), only while that volume file still exists: a file deleted
        meanwhile is not resurrected."""
        store = self.database.store(tenant_id)
        winner: FileObject | None = None
        with store.transaction():
            store.lock(COLLECTION, item.blob)
            existing = store.get(COLLECTION, item.blob, FileObject)
            if existing is not None:
                winner = existing
            elif source is not None and not source.is_file():
                winner = None
            else:
                store.put(COLLECTION, item.blob, item)
                return item
        self.discard(item.axentra_tenant, item.axentra_file)
        if winner is None:
            raise KeyError(item.blob)
        if winner.sha256 != item.sha256:
            raise NodestroError("input.integrity", "Two contents share one blob id", blob=item.blob)
        return winner

    # ---- migration ----------------------------------------------------------------------------------------

    def _tenants(self, tenant: str | None) -> list[str]:
        rows = self.database._query("SELECT id FROM nodestro_tenants ORDER BY id")
        ids = [str(row[0]) for row in rows]
        if tenant is not None:
            if tenant not in ids:
                raise NodestroError("identity.tenant_unknown", f"No tenant {tenant}", tenant=tenant)
            return [tenant]
        return ids

    @staticmethod
    def _volume(data_root: Path, tenant_id: str) -> Path:
        return data_root / "tenants" / tenant_id / "inputs"

    @staticmethod
    def _volume_entries(root: Path) -> Iterator[tuple[str, str, str, str, Path]]:
        """``(blob, sha256, name, media type, path)`` of every blob in a volume catalog (read once)."""
        catalog = root / "catalog.json"
        if not catalog.is_file():
            return
        for blob, entry in json.loads(catalog.read_text(encoding="utf-8")).items():
            if _BLOB.match(blob):
                yield (
                    blob,
                    str(entry.get("sha256") or ""),
                    str(entry.get("name") or "upload"),
                    str(entry.get("media_type") or "application/octet-stream"),
                    root / "blobs" / blob,
                )

    def migrate(self, data_root: Path, *, tenant: str | None = None, dry_run: bool = False) -> FilesReport:
        """Copy every volume blob without a mapping into Axentra (see the module documentation)."""
        report = FilesReport(dry_run=dry_run)
        for tenant_id in self._tenants(tenant):
            report.tenants += 1
            store = self.database.store(tenant_id)
            for blob, digest, name, media_type, path in self._volume_entries(self._volume(data_root, tenant_id)):
                report.found += 1
                existing = store.get(COLLECTION, blob, FileObject)
                if existing is not None:
                    report.already += 1
                    if digest and existing.sha256 != digest:
                        report.fail(tenant_id, blob, "the mapped object has another SHA-256 than the volume file")
                    continue
                if not path.is_file() or path.is_symlink():
                    report.fail(tenant_id, blob, "the volume file is missing")
                    continue
                if dry_run:
                    report.pending += 1
                    report.bytes += path.stat().st_size
                    continue
                data = path.read_bytes()
                actual = _sha256(data)
                if actual[:16] != blob or (digest and actual != digest):
                    report.fail(tenant_id, blob, "the volume file does not match its SHA-256 (not copied)")
                    continue
                try:
                    item = self.upload(tenant_id, data, blob=blob, name=name, media_type=media_type, origin="migration")
                    self.read(tenant_id, item)  # read back and compare before the mapping makes it the source of truth
                except NodestroError as exc:
                    report.fail(tenant_id, blob, f"{exc.code}: {exc}")
                    continue
                try:
                    self.record(tenant_id, item, source=path)
                except KeyError:
                    continue  # deleted while it was copied
                report.copied += 1
                report.bytes += len(data)
        logger.info("Input file migration%s: %s", " (dry run)" if dry_run else "", report.as_dict())
        return report

    def verify(self, data_root: Path, *, tenant: str | None = None) -> FilesReport:
        """Read every mapped object back and compare its SHA-256 (records ``verified_at``); report volume blobs not
        migrated yet (``pending``) and Axentra objects without a mapping (``orphans``)."""
        report = FilesReport()
        for tenant_id in self._tenants(tenant):
            report.tenants += 1
            store = self.database.store(tenant_id)
            mapped: dict[str, FileObject] = {}
            for blob, item in store.items(COLLECTION, FileObject):
                mapped[blob] = item
                try:
                    self.read(tenant_id, item)
                except NodestroError as exc:
                    report.fail(tenant_id, blob, exc.code)
                    continue
                report.verified += 1
                report.bytes += item.size
                with store.transaction():
                    store.lock(COLLECTION, blob)
                    current = store.get(COLLECTION, blob, FileObject)
                    if current is not None and current.axentra_file == item.axentra_file:
                        store.put(COLLECTION, blob, current.model_copy(update={"verified_at": _now()}))
            for blob, *_ in self._volume_entries(self._volume(data_root, tenant_id)):
                report.found += 1
                if blob not in mapped:
                    report.pending += 1
            ax_tenant = self.link.tenant(tenant_id)
            if ax_tenant is not None:
                known = {item.axentra_file for item in mapped.values()}
                for info in self._axentra_files(ax_tenant):
                    if info.id not in known:
                        report.orphans.append(
                            {"tenant": tenant_id, "file": info.id, "blob": str(info.metadata.get(BLOB_KEY, ""))}
                        )
        logger.info("Input file verification: %s", report.as_dict())
        return report

    def _axentra_files(self, ax_tenant: str) -> Iterator[Any]:
        cursor: str | None = None
        while True:
            page = self.storage.list(
                self.principal,
                self.services.product_id,
                ax_tenant,
                sort="created_at",
                limit=200,
                cursor=cursor,
            )
            for info in page.items:
                if BLOB_KEY in info.metadata:
                    yield info
            cursor = page.next_cursor
            if cursor is None:
                return

    def remove_sources(
        self, data_root: Path, *, tenant: str | None = None, dry_run: bool = False, now: datetime | None = None
    ) -> FilesReport:
        """Delete migrated volume files whose fallback period has passed (see the module documentation)."""
        if not self.source_removal:
            raise NodestroError(
                "files.source_removal_off",
                "Removing migrated volume files is switched off (NODESTRO_FILES_SOURCE_REMOVAL=1 allows it)",
            )
        moment = now or _now()
        cutoff = moment - timedelta(days=self.retention_days)
        report = FilesReport(dry_run=dry_run)
        for tenant_id in self._tenants(tenant):
            report.tenants += 1
            store = self.database.store(tenant_id)
            root = self._volume(data_root, tenant_id)
            volume = _Blobs(root) if (root / "catalog.json").is_file() else None
            for blob, digest, _, _, _ in list(self._volume_entries(root)):
                report.found += 1
                item = store.get(COLLECTION, blob, FileObject)
                if item is None:
                    report.pending += 1
                    continue
                if item.origin != "migration" or item.verified_at is None or item.created_at > cutoff:
                    report.kept += 1
                    continue
                if digest and digest != item.sha256:
                    report.fail(tenant_id, blob, "the volume file and the mapped object differ")
                    continue
                try:
                    self.read(tenant_id, item)  # the copy must still be intact right now
                except NodestroError as exc:
                    report.fail(tenant_id, blob, exc.code)
                    continue
                if dry_run:
                    report.removed += 1
                    continue
                if volume is not None and blob in volume:
                    volume.delete(blob)
                with store.transaction():
                    store.lock(COLLECTION, blob)
                    current = store.get(COLLECTION, blob, FileObject)
                    if current is not None:
                        store.put(COLLECTION, blob, current.model_copy(update={"source_removed_at": moment}))
                report.removed += 1
        logger.info("Input file source removal%s: %s", " (dry run)" if dry_run else "", report.as_dict())
        return report

    def export_to_volume(self, data_root: Path, *, tenant: str | None = None, dry_run: bool = False) -> FilesReport:
        """Copy every mapped blob that is not on the volume back to it (before switching back to ``volume``).
        Idempotent; mappings and Axentra objects stay."""
        report = FilesReport(dry_run=dry_run)
        for tenant_id in self._tenants(tenant):
            report.tenants += 1
            store = self.database.store(tenant_id)
            items = store.items(COLLECTION, FileObject)
            if not items:
                continue
            volume = _Blobs(self._volume(data_root, tenant_id))
            for blob, item in items:
                report.found += 1
                if blob in volume:
                    report.already += 1
                    continue
                if dry_run:
                    report.pending += 1
                    report.bytes += item.size
                    continue
                try:
                    data = self.read(tenant_id, item)
                except NodestroError as exc:
                    report.fail(tenant_id, blob, exc.code)
                    continue
                volume.put(data, name=f"{blob}", media_type="application/octet-stream")
                report.copied += 1
                report.bytes += item.size
        logger.info("Input file export to the volume%s: %s", " (dry run)" if dry_run else "", report.as_dict())
        return report


class AxentraBlobs:
    """One tenant's input file bytes in Axentra, with the volume as the fallback for blobs without a mapping
    (the :class:`~nodestro_runtime.input_files.BlobStore` of :class:`~nodestro_runtime.input_files.InputFiles`)."""

    def __init__(self, files: AxentraFiles, tenant_id: str, root: Path) -> None:
        self.files = files
        self.tenant_id = tenant_id
        self.root = root
        self._volume: _Blobs | None = None

    @property
    def volume(self) -> _Blobs:
        if self._volume is None:
            self._volume = _Blobs(self.root)
        return self._volume

    def _on_volume(self, blob: str) -> bool:
        return (self.root / "catalog.json").is_file() and blob in self.volume

    def put(self, data: bytes, *, name: str, media_type: str) -> str:
        digest = _sha256(data)
        blob = digest[:16]
        existing = self.files.lookup(self.tenant_id, blob)
        if existing is not None:
            if existing.sha256 != digest:
                raise NodestroError("input.integrity", "Two contents share one blob id", blob=blob)
            return blob
        item = self.files.upload(self.tenant_id, data, blob=blob, name=name, media_type=media_type, origin="upload")
        self.files.record(self.tenant_id, item)
        return blob

    def get_bytes(self, file_id: str) -> bytes:
        item = self.files.lookup(self.tenant_id, file_id)
        if item is None:
            if not self._on_volume(file_id):
                raise KeyError(file_id)
            return self.volume.get_bytes(file_id)  # not migrated yet: the volume is the fallback
        return self.files.read(self.tenant_id, item)

    def __contains__(self, file_id: object) -> bool:
        if not isinstance(file_id, str) or not _BLOB.match(file_id):
            return False
        return self.files.lookup(self.tenant_id, file_id) is not None or self._on_volume(file_id)

    def inert(self, file_id: str) -> bool:
        item = self.files.lookup(self.tenant_id, file_id)
        return item is not None and item.stored_inert

    def delete(self, file_id: str) -> None:
        """Delete every copy of the blob: the mapping and the volume copy in one locked step, then the object."""
        store = self.files.database.store(self.tenant_id)
        with store.transaction():
            store.lock(COLLECTION, file_id)
            item = store.get(COLLECTION, file_id, FileObject)
            on_volume = self._on_volume(file_id)
            if item is None and not on_volume:
                raise KeyError(file_id)
            if item is not None:
                store.delete(COLLECTION, file_id)
            if on_volume:
                self.volume.delete(file_id)
        if item is not None:
            self.files.discard(item.axentra_tenant, item.axentra_file)


def open_files(services: AxentraServices, database: PostgresDatabase) -> AxentraFiles:
    """Axentra's storage for this process (``services`` started), checked and with Nodestro's content policy."""
    return AxentraFiles(services, database).start()


__all__ = ["COLLECTION", "AxentraBlobs", "AxentraFiles", "FileObject", "FilesReport", "open_files"]
