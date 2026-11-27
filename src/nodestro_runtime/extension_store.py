"""The data-only extension store ``<data>/extensions`` (RT-098, owner decision 2026-11-27; see
``docs/extension-isolation.md``).

Foreign extension packages are never installed into the host Python environment, never put on ``sys.path`` and never
imported in the host process. They come only from this store, as files, and their code runs only in the Docker sandbox
(:mod:`.extension_sandbox`). The store accepts a pure-Python wheel through :meth:`ExtensionStore.install` (the only
way in; the HTTP API's admin routes call it) and keeps it unpacked, read-only, in ``<data>/extensions/<sha256>/`` (the
wheel's SHA-256). ``index.json`` next to the packages lists the installed packages and the revoked hashes; a package
counts only while the index lists it.

Intake (:func:`inspect_wheel`, nothing written before it passed): the size and the SHA-256 the administrator expects,
a stated origin, zip safety (no absolute, ``..``, backslash or duplicate path, no symbolic link, device file or other
non-regular member, no encryption, only stored or deflated members whose real size matches), limits on entries, file
size and total size, exactly one ``.dist-info`` whose ``RECORD`` lists every member with its SHA-256 and size, a pure
``py3-none-any`` wheel, no ``.data`` part, no ``.pth`` file, no start-up module and no native code, metadata that
matches the folder name, entry points only in :data:`~.extensions.ENTRY_POINT_GROUP` naming modules of the wheel, and
no dependency the sandbox image does not provide (dependency images are G-6 item 21, not built).

Atomic: the files are written to ``.staging/<random>/``, made read-only, renamed to ``<sha256>/`` and only then listed
in the index (one ``os.replace``, the commit point). A crash before the commit leaves a staging folder or a folder the
index does not list; neither is ever valid, and both are removed the next time the store is opened.

Removal and revocation commit the index first, then delete the folder. A revoked hash is never installed again. Every
call of a store tool checks first that its package is still installed with the hash it was verified with
(:meth:`ExtensionStore.check`); the files the sandbox gets are the bytes verified when the package was listed, never a
fresh read. Install, update, removal, revocation and refused intake are written to the activity log (N-13).
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
import configparser
import contextlib
import csv
from dataclasses import dataclass
from email.parser import BytesParser
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import threading
from typing import Any
import unicodedata
import uuid
import zipfile

from . import activity
from .extension_sandbox import (
    _ENTRY,
    MAX_FILE_BYTES,
    MAX_PACKAGE_BYTES,
    MAX_PACKAGE_FILES,
    ExtensionPackage,
    PackageError,
)
from .extensions import ENTRY_POINT_GROUP
from .problems import NodestroError
from .schemas import now
from .storage import DocumentStore

logger = logging.getLogger(__name__)

#: The store's folder below the data directory.
STORE_DIR = "extensions"
INDEX = "index.json"
STAGING = ".staging"
TRASH = ".trash"
#: Bounds of an upload.
MAX_WHEEL_BYTES = MAX_PACKAGE_BYTES
MAX_ENTRIES = 2 * MAX_PACKAGE_FILES
MAX_UNPACKED_BYTES = MAX_PACKAGE_BYTES
MAX_ENTRY_POINTS = 16
MAX_ORIGIN_CHARS = 500
#: Dependencies the sandbox provides itself (the contract module stands in for these); any other one needs a
#: dependency image (G-6 item 21, not built), so the wheel is refused.
PROVIDED = frozenset({"nodestro-runtime", "orkexa", "pydantic", "typing-extensions"})
#: Members never accepted: code a Python start runs, and native code.
_REFUSED_SUFFIXES = (".pth", ".so", ".pyd", ".dll", ".dylib", ".exe", ".egg-link")
_STARTUP_MODULES = ("sitecustomize", "usercustomize")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIST_INFO = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._]*[A-Za-z0-9])?)-(?P<version>[A-Za-z0-9.!+_]+)\.dist-info$"
)
_REQUIREMENT = re.compile(r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_EXTRA = re.compile(r"\bextra\s*==")
_POINT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")
_PURE_TAGS = frozenset({"py3-none-any", "py2.py3-none-any"})
_INDEX_VERSION = 1


class StoreError(NodestroError):
    """An upload, removal or revocation the store refuses (code ``extension_store.*``)."""


def _refused(reason: str, message: str) -> StoreError:
    return StoreError("extension_store.refused", f"The extension package was refused: {message}", reason=reason)


def normalized(name: str) -> str:
    """A distribution name as PEP 503 compares it."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Inspected:
    """A wheel that passed every intake check (nothing written yet)."""

    sha256: str
    distribution: str
    version: str
    #: ``(entry point name, module:attribute)``.
    entries: tuple[tuple[str, str], ...]
    #: Member path -> content (every member, ``RECORD`` included).
    files: dict[str, bytes]
    record_sha256: str


def _check_member_name(name: str) -> tuple[str, ...]:
    if not name or len(name) > 512 or "\\" in name or "\x00" in name or _CONTROL.search(name):
        raise _refused("unsafe_path", f"unsafe member name {name[:80]!r}")
    parts = tuple(name.rstrip("/").split("/"))
    if name.startswith("/") or ":" in parts[0] or any(part in ("", ".", "..") or len(part) > 255 for part in parts):
        raise _refused("unsafe_path", f"unsafe member name {name[:80]!r}")
    return parts


def _record_hash(value: str) -> str:
    """``sha256=<urlsafe base64>`` of a ``RECORD`` row as hex (empty when it is not a SHA-256)."""
    algorithm, _, digest = value.partition("=")
    if algorithm != "sha256" or not digest:
        return ""
    try:
        return base64.urlsafe_b64decode(digest + "=" * (-len(digest) % 4)).hex()
    except (ValueError, TypeError):
        return ""


def parse_record(data: bytes, info: str) -> dict[str, tuple[str, int]]:
    """``RECORD`` as ``path -> (sha256 hex, size)``; every row but ``RECORD``'s own needs both."""
    try:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    except (UnicodeDecodeError, csv.Error):
        raise _refused("record", "RECORD cannot be read") from None
    found: dict[str, tuple[str, int]] = {}
    for row in rows:
        if not row:
            continue
        if len(row) != 3:
            raise _refused("record", "a RECORD row does not have three fields")
        path, digest, size = row
        _check_member_name(path)
        if path in found:
            raise _refused("record", f"RECORD lists {path[:80]!r} twice")
        if path == f"{info}/RECORD":
            found[path] = ("", -1)
            continue
        hexed = _record_hash(digest)
        if not hexed or not size.isdigit():
            raise _refused("record", f"RECORD has no SHA-256 and size for {path[:80]!r}")
        found[path] = (hexed, int(size))
    return found


def _read_members(archive: zipfile.ZipFile) -> dict[str, bytes]:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise _refused("too_large", f"more than {MAX_ENTRIES} members")
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    total = 0
    for info in infos:
        parts = _check_member_name(info.filename)
        folded = unicodedata.normalize("NFC", "/".join(parts)).casefold()
        if folded in seen:
            raise _refused("unsafe_path", f"{info.filename[:80]!r} is in the archive twice")
        seen.add(folded)
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if info.is_dir():
            if kind not in (0, stat.S_IFDIR):
                raise _refused("unsafe_member", f"{info.filename[:80]!r} is not a plain folder")
            continue
        if kind not in (0, stat.S_IFREG):
            raise _refused("unsafe_member", f"{info.filename[:80]!r} is a link, a device or another special file")
        if info.flag_bits & 0x1:
            raise _refused("unsafe_member", f"{info.filename[:80]!r} is encrypted")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise _refused("unsafe_member", f"{info.filename[:80]!r} uses an unsupported compression")
        if info.file_size > MAX_FILE_BYTES:
            raise _refused("too_large", f"{info.filename[:80]!r} is larger than {MAX_FILE_BYTES} bytes")
        with archive.open(info) as handle:
            content = handle.read(MAX_FILE_BYTES + 1)
        if len(content) != info.file_size:
            raise _refused("unsafe_member", f"{info.filename[:80]!r} is not the size it states")
        total += len(content)
        if total > MAX_UNPACKED_BYTES:
            raise _refused("too_large", f"more than {MAX_UNPACKED_BYTES} bytes unpacked")
        files["/".join(parts)] = content
    return files


def _metadata_checks(files: dict[str, bytes], info: str, name: str, version: str) -> None:
    wheel = BytesParser().parsebytes(files.get(f"{info}/WHEEL", b""))
    if (wheel.get("Root-Is-Purelib") or "").strip().lower() != "true":
        raise _refused("not_pure", "only pure-Python wheels are accepted")
    tags = [tag.strip() for tag in wheel.get_all("Tag") or []]
    if not tags or any(tag not in _PURE_TAGS for tag in tags):
        raise _refused("not_pure", "only py3-none-any wheels are accepted")
    if f"{info}/METADATA" not in files:
        raise _refused("manifest", "the wheel has no METADATA")
    meta = BytesParser().parsebytes(files[f"{info}/METADATA"])
    if normalized(meta.get("Name") or "") != normalized(name) or (meta.get("Version") or "").strip() != version:
        raise _refused("manifest", "METADATA does not match the .dist-info folder")
    for requirement in meta.get_all("Requires-Dist") or []:
        match = _REQUIREMENT.match(requirement)
        if not match:
            raise _refused("dependency", f"unreadable dependency {requirement[:80]!r}")
        _spec, _, marker = requirement.partition(";")
        if _EXTRA.search(marker):
            continue  # an optional extra is never installed
        if normalized(match["name"]) not in PROVIDED:
            raise _refused(
                "dependency",
                f"it needs {match['name'][:80]!r}; packages with their own dependencies need a dependency image, "
                "which is not built yet (G-6 item 21)",
            )


def _entry_points(files: dict[str, bytes], info: str) -> tuple[tuple[str, str], ...]:
    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",), strict=True)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    try:
        parser.read_string(files.get(f"{info}/entry_points.txt", b"").decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error):
        raise _refused("manifest", "entry_points.txt cannot be read") from None
    extra = [section for section in parser.sections() if section != ENTRY_POINT_GROUP]
    if extra:
        raise _refused("manifest", f"only {ENTRY_POINT_GROUP} entry points are accepted, not {extra[0][:80]!r}")
    if not parser.has_section(ENTRY_POINT_GROUP):
        raise _refused("manifest", f"the wheel declares no {ENTRY_POINT_GROUP} entry point")
    entries = tuple(sorted((key, value.strip()) for key, value in parser.items(ENTRY_POINT_GROUP)))
    if not entries or len(entries) > MAX_ENTRY_POINTS:
        raise _refused("manifest", f"between 1 and {MAX_ENTRY_POINTS} entry points are accepted")
    for key, value in entries:
        if not _POINT_NAME.match(key) or not _ENTRY.match(value):
            raise _refused("manifest", f"invalid entry point {key[:40]!r} = {value[:80]!r}")
        top = value.split(":")[0].split(".")[0]
        if f"{top}/__init__.py" not in files and f"{top}.py" not in files:
            raise _refused("manifest", f"the entry point {key[:40]!r} names a module the wheel does not contain")
    return entries


def inspect_wheel(data: bytes, *, expected_sha256: str) -> Inspected:
    """Every intake check of a wheel (see the module documentation); raises :class:`StoreError`."""
    if len(data) > MAX_WHEEL_BYTES:
        raise _refused("too_large", f"the wheel is larger than {MAX_WHEEL_BYTES} bytes")
    digest = hashlib.sha256(data).hexdigest()
    if not _SHA256.match(expected_sha256 or "") or digest != expected_sha256:
        raise _refused("hash_mismatch", "the wheel's SHA-256 is not the expected one")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = _read_members(archive)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, NotImplementedError, RuntimeError, OSError):
        raise _refused("archive", "the file is not a readable wheel") from None
    tops = {path.split("/")[0] for path in files}
    infos = [top for top in tops if top.endswith(".dist-info")]
    if len(infos) != 1 or not (named := _DIST_INFO.match(infos[0])):
        raise _refused("manifest", "the wheel must have exactly one <name>-<version>.dist-info folder")
    info = infos[0]
    if any(top.endswith(".data") for top in tops):
        raise _refused("data_part", "wheels with a .data part (scripts, headers, data) are not accepted")
    for path in files:
        base = path.rsplit("/", 1)[-1]
        if base.lower().endswith(_REFUSED_SUFFIXES) or (
            path.count("/") == 0 and base.rsplit(".", 1)[0] in _STARTUP_MODULES
        ):
            raise _refused("unsafe_member", f"{path[:80]!r} would run code at start or is native code")
    if f"{info}/RECORD" not in files:
        raise _refused("record", "the wheel has no RECORD")
    record = parse_record(files[f"{info}/RECORD"], info)
    if set(record) != set(files):
        raise _refused("record", "RECORD does not list exactly the wheel's members")
    for path, (hexed, size) in record.items():
        if size >= 0 and (hashlib.sha256(files[path]).hexdigest() != hexed or len(files[path]) != size):
            raise _refused("hash_mismatch", f"{path[:80]!r} does not match its RECORD hash")
    _metadata_checks(files, info, named["name"], named["version"])
    entries = _entry_points(files, info)
    meta = BytesParser().parsebytes(files[f"{info}/METADATA"])
    return Inspected(
        sha256=digest,
        distribution=str(meta.get("Name")).strip(),
        version=named["version"],
        entries=entries,
        files=files,
        record_sha256=hashlib.sha256(files[f"{info}/RECORD"]).hexdigest(),
    )


def _origin(text: str) -> str:
    value = (text or "").strip()
    if not value or len(value) > MAX_ORIGIN_CHARS or _CONTROL.search(value):
        raise _refused("origin", f"state where the package comes from (1 to {MAX_ORIGIN_CHARS} printable characters)")
    return value


def _writable(path: Path) -> None:
    """Make a read-only package tree deletable again (never follows a link)."""
    for folder in [path, *(Path(root) / name for root, dirs, _files in os.walk(path) for name in dirs)]:
        if not folder.is_symlink():
            with contextlib.suppress(OSError):
                os.chmod(folder, 0o700)


def _delete(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.exists():
        _writable(path)
        shutil.rmtree(path, ignore_errors=True)


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    with contextlib.suppress(OSError):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _read_regular(path: Path, limit: int) -> bytes:
    """A regular file, never through a symbolic link, with one name only (no hard link), at most ``limit`` bytes."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise PackageError(f"{path.name} is not a plain file of at most {limit} bytes")
        return handle.read(limit + 1)


class ExtensionStore:
    """The store of one data directory (see the module documentation). ``documents``: where intake, removal and
    revocation are audited (the workspace's store; ``None``: only logged)."""

    def __init__(self, root: str | Path, *, documents: DocumentStore | None = None) -> None:
        self.root = Path(root)
        self.documents = documents
        self._lock = threading.RLock()

    # ---- the index ------------------------------------------------------------------------------------------------

    def _index(self) -> dict[str, Any]:
        path = self.root / INDEX
        try:
            data = json.loads(_read_regular(path, 4 * 1024 * 1024))
        except FileNotFoundError:
            return {"version": _INDEX_VERSION, "packages": {}, "revoked": {}}
        except (OSError, ValueError, PackageError) as exc:
            raise StoreError(
                "extension_store.unreadable", f"The extension store's index cannot be read: {exc}"
            ) from None
        if not isinstance(data, dict) or data.get("version") != _INDEX_VERSION:
            raise StoreError("extension_store.unreadable", "The extension store's index has an unknown format")
        packages, revoked = data.get("packages"), data.get("revoked")
        if not isinstance(packages, dict) or not isinstance(revoked, dict):
            raise StoreError("extension_store.unreadable", "The extension store's index has an unknown format")
        return {"version": _INDEX_VERSION, "packages": packages, "revoked": revoked}

    def _commit(self, index: dict[str, Any]) -> None:
        """Write the index in one ``os.replace`` (the commit point of every change)."""
        temporary = self.root / f".{INDEX}.{uuid.uuid4().hex}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(index, handle, indent=1, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.root / INDEX)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        _fsync_dir(self.root)

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for folder in (self.root, self.root / STAGING, self.root / TRASH):
            if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
                raise StoreError("extension_store.unreadable", "The extension store is not a plain folder")

    def _audit(
        self, action: str, actor: str, sha256: str, *, outcome: activity.Outcome = "success", code: str | None = None
    ) -> None:
        logger.info("Extension store: %s %s by %s (%s)", action, sha256, actor, code or outcome)
        if self.documents is not None:
            activity.add(
                self.documents,
                "extension",
                action,
                actor=actor,
                resource_type="extension_package",
                resource_id=sha256,
                outcome=outcome,
                code=code,
            )

    # ---- maintenance ------------------------------------------------------------------------------------------------

    def clean(self) -> None:
        """Remove what an interrupted change left: staging and trash folders, and package folders the index does not
        list (a crash between the rename and the commit). None of them was ever valid."""
        if not self.root.is_dir():
            return
        with self._lock:
            listed = set(self._index()["packages"])
            for child in self.root.iterdir():
                if child.name in (STAGING, TRASH):
                    for item in list(child.iterdir()) if child.is_dir() and not child.is_symlink() else []:
                        _delete(item)
                elif child.name == INDEX:
                    continue
                elif child.name.startswith(f".{INDEX}."):
                    child.unlink(missing_ok=True)
                elif child.name not in listed:
                    logger.warning("Extension store: %s is not listed in the index and is removed", child.name)
                    _delete(child)

    # ---- the controlled interfaces ----------------------------------------------------------------------------------

    def install(
        self, data: bytes, *, sha256: str, origin: str, actor: str, replaces: str | None = None
    ) -> dict[str, Any]:
        """Take ``data`` (a wheel) into the store after every intake check, atomically; ``sha256``: the hash the
        administrator expects; ``origin``: where it comes from; ``replaces``: the installed package of the same
        distribution this one updates (removed in the same commit). Returns the package's entry."""
        try:
            origin_text = _origin(origin)
            inspected = inspect_wheel(data, expected_sha256=(sha256 or "").strip().lower())
            with self._lock:
                self._ensure_root()
                index = self._index()
                if inspected.sha256 in index["revoked"]:
                    raise _refused("revoked", "this package was revoked and is never installed again")
                if inspected.sha256 in index["packages"]:
                    raise _refused("exists", "this package is installed already")
                if replaces is not None:
                    old = index["packages"].get(replaces)
                    if old is None:
                        raise StoreError("extension_store.not_found", "The package to update is not installed")
                    if normalized(str(old.get("distribution", ""))) != normalized(inspected.distribution):
                        raise _refused("exists", "an update must be of the same distribution")
                for key, entry in index["packages"].items():
                    other = str(entry.get("distribution", "")) if isinstance(entry, dict) else ""
                    if key != replaces and normalized(other) == normalized(inspected.distribution):
                        raise _refused("exists", "this distribution is installed already; update it instead")
                entry = self._place(inspected, origin_text, actor, replaces)
                index["packages"][inspected.sha256] = entry
                if replaces is not None:
                    index["packages"].pop(replaces, None)
                self._commit(index)
                if replaces is not None:
                    self._discard(replaces)
        except StoreError as exc:
            self._audit(
                "extension_package.refused",
                actor,
                (sha256 or "")[:64],
                outcome="denied",
                code=str(exc.params.get("reason") or exc.code),
            )
            raise
        self._audit("extension_package.updated" if replaces else "extension_package.installed", actor, inspected.sha256)
        return {"sha256": inspected.sha256, **entry}

    def _place(self, inspected: Inspected, origin: str, actor: str, replaces: str | None) -> dict[str, Any]:
        """Write ``inspected`` read-only to ``<root>/<sha256>`` through a staging folder; returns its index entry."""
        staging_root = self.root / STAGING
        staging_root.mkdir(mode=0o700, exist_ok=True)
        staging = staging_root / uuid.uuid4().hex
        staging.mkdir(mode=0o700)
        try:
            folders: set[Path] = {staging}
            for path, content in inspected.files.items():
                target = staging.joinpath(*path.split("/"))
                for parent in reversed(target.relative_to(staging).parents[:-1]):
                    folder = staging / parent
                    if folder not in folders:
                        folder.mkdir(mode=0o700)
                        folders.add(folder)
                flags = (
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                )
                descriptor = os.open(target, flags, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(target, 0o444)
            for folder in sorted(folders, key=lambda item: len(item.parts), reverse=True):
                _fsync_dir(folder)
                os.chmod(folder, 0o555)
            os.rename(staging, self.root / inspected.sha256)
            _fsync_dir(self.root)
        except BaseException:
            _delete(staging)
            raise
        return {
            "distribution": inspected.distribution,
            "version": inspected.version,
            "entries": [list(item) for item in inspected.entries],
            "record_sha256": inspected.record_sha256,
            "origin": origin,
            "installed_at": now().isoformat(),
            "installed_by": actor,
            "replaces": replaces,
            "files": len(inspected.files),
            "bytes": sum(len(item) for item in inspected.files.values()),
        }

    def _discard(self, sha256: str) -> None:
        """Delete an uncommitted or removed package folder (moved aside first, so no half-deleted folder stays)."""
        folder = self.root / sha256
        if not folder.exists():
            return
        trash = self.root / TRASH
        trash.mkdir(mode=0o700, exist_ok=True)
        aside = trash / uuid.uuid4().hex
        try:
            os.rename(folder, aside)
        except OSError:
            aside = folder
        _delete(aside)

    def remove(self, sha256: str, *, actor: str) -> None:
        """Remove an installed package (it may be installed again later)."""
        with self._lock:
            index = self._index()
            if sha256 not in index["packages"]:
                raise StoreError("extension_store.not_found", "The extension package is not installed")
            index["packages"].pop(sha256)
            self._commit(index)
            self._discard(sha256)
        self._audit("extension_package.removed", actor, sha256)

    def revoke(self, sha256: str, *, actor: str) -> None:
        """Revoke a hash: removed when installed, and never installed again."""
        sha256 = (sha256 or "").strip().lower()
        if not _SHA256.match(sha256):
            raise _refused("hash_mismatch", "a revocation names the package's SHA-256")
        with self._lock:
            self._ensure_root()
            index = self._index()
            index["packages"].pop(sha256, None)
            index["revoked"][sha256] = {"revoked_at": now().isoformat(), "revoked_by": actor}
            self._commit(index)
            self._discard(sha256)
        self._audit("extension_package.revoked", actor, sha256)

    def listing(self) -> dict[str, Any]:
        """The installed packages (with their index entries) and the revoked hashes."""
        if not self.root.is_dir():
            return {"packages": [], "revoked": []}
        with self._lock:
            index = self._index()
        return {
            "packages": [{"sha256": key, **value} for key, value in sorted(index["packages"].items())],
            "revoked": [{"sha256": key, **value} for key, value in sorted(index["revoked"].items())],
        }

    # ---- what the sandbox gets ---------------------------------------------------------------------------------------

    def check(self, sha256: str, record_sha256: str) -> None:
        """Raise :class:`~.extension_sandbox.PackageError` unless ``sha256`` is still installed with the ``RECORD`` it
        was verified with and is not revoked (before every call of its tools)."""
        try:
            index = self._index()
        except StoreError as exc:
            raise PackageError(str(exc)) from None
        if sha256 in index["revoked"]:
            raise PackageError("the extension package was revoked")
        entry = index["packages"].get(sha256)
        if entry is None or entry.get("record_sha256") != record_sha256:
            raise PackageError("the extension package is no longer installed")

    def verified_files(self, sha256: str, entry: dict[str, Any], top: str) -> dict[str, str]:
        """The Python files of module ``top`` of an installed package, after checking the whole folder against its
        ``RECORD`` (whose own hash the index keeps): no link, no hard link, no file missing or added, every hash."""
        self.check(sha256, str(entry.get("record_sha256")))
        base = self.root / sha256
        if base.is_symlink() or not base.is_dir():
            raise PackageError("the package folder is missing")
        present: set[str] = set()
        for path in _walk(base):
            present.add(path)
        infos = [path for path in present if path.count("/") == 1 and path.split("/")[0].endswith(".dist-info")]
        record_path = next((path for path in infos if path.endswith("/RECORD")), "")
        if not record_path:
            raise PackageError("the package has no RECORD")
        record_data = _read_regular(base / record_path, 4 * 1024 * 1024)
        if hashlib.sha256(record_data).hexdigest() != entry.get("record_sha256"):
            raise PackageError("the package's RECORD was changed after intake")
        try:
            record = parse_record(record_data, record_path.split("/")[0])
        except StoreError as exc:
            raise PackageError(str(exc)) from None
        if set(record) != present:
            raise PackageError("files were added to or removed from the package after intake")
        found: dict[str, str] = {}
        total = 0
        for path, (hexed, size) in sorted(record.items()):
            if size < 0:
                continue
            content = _read_regular(base.joinpath(*path.split("/")), MAX_FILE_BYTES)
            if hashlib.sha256(content).hexdigest() != hexed or len(content) != size:
                raise PackageError(f"{path[:80]} was changed after intake")
            parts = path.split("/")
            if path.endswith(".py") and (parts[0] == top or parts == [f"{top}.py"]):
                total += len(content)
                if len(found) >= MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
                    raise PackageError("the package is larger than the sandbox accepts")
                try:
                    found[path] = content.decode("utf-8")
                except UnicodeDecodeError:
                    raise PackageError(f"{path[:80]} is not UTF-8") from None
        if not found:
            raise PackageError(f"the package has no Python file of {top!r}")
        return found

    def packages(self) -> list[ExtensionPackage]:
        """Every entry point of every installed package, for :class:`~.extensions.Extensions` (nothing is imported;
        the files are read and verified only when the sandbox needs them). An unreadable index lists none."""
        if not self.root.is_dir():
            return []
        try:
            self.clean()
            index = self._index()
        except (StoreError, OSError) as exc:
            logger.error("The extension store cannot be read; no package of it is used: %s", exc)
            return []
        found: list[ExtensionPackage] = []
        for sha256, entry in sorted(index["packages"].items()):
            if not _SHA256.match(sha256) or not isinstance(entry, dict):
                continue
            for name, value in entry.get("entries") or []:
                found.append(self._package(sha256, entry, str(name), str(value)))
        return found

    def _package(self, sha256: str, entry: dict[str, Any], name: str, value: str) -> ExtensionPackage:
        top = value.split(":")[0].split(".")[0]
        record_sha256 = str(entry.get("record_sha256"))
        return ExtensionPackage(
            name=name,
            entry=value,
            distribution=str(entry.get("distribution")),
            version=str(entry.get("version")),
            read_files=lambda: self.verified_files(sha256, entry, top),
            sha256=sha256,
            check=lambda: self.check(sha256, record_sha256),
        )


def _walk(base: Path) -> Iterator[str]:
    """Every file below ``base`` as a ``/`` path; raises :class:`PackageError` at a link or special file."""
    stack: list[tuple[Path, str]] = [(base, "")]
    while stack:
        folder, prefix = stack.pop()
        with os.scandir(folder) as entries:
            for item in entries:
                relative = f"{prefix}{item.name}"
                if item.is_symlink():
                    raise PackageError(f"{relative[:80]} is a symbolic link")
                if item.is_dir(follow_symlinks=False):
                    stack.append((Path(item.path), relative + "/"))
                elif item.is_file(follow_symlinks=False):
                    yield relative
                else:
                    raise PackageError(f"{relative[:80]} is not a regular file")


__all__ = [
    "ENTRY_POINT_GROUP",
    "INDEX",
    "MAX_WHEEL_BYTES",
    "PROVIDED",
    "STORE_DIR",
    "ExtensionStore",
    "Inspected",
    "StoreError",
    "inspect_wheel",
    "normalized",
    "parse_record",
]
