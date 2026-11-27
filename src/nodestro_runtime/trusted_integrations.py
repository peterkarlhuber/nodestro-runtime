"""Nodestro's own integrations: the only entry points imported into the host process (N-22, review F1).

A distribution's name is its own statement, so it never decides trust by itself. An entry point is imported only when

1. it is one of :data:`TRUSTED_ENTRY_POINTS`: the distribution's normalized name, the entry point's name and its exact
   ``module:attribute``;
2. its distribution is installed as a wheel installs it: a ``<name>-<version>.dist-info`` folder whose name matches
   the metadata, inside the installation directory of its files;
3. every Python file of the pinned top-level package is listed in that distribution's ``RECORD`` with a SHA-256 hash,
   lies at its own path (no symbolic link on the way), and has exactly that hash now; the pinned module and the
   package's ``__init__.py`` are among them;
4. it is installed in the same directory as the running ``nodestro_runtime`` package (:func:`installation_home`): a
   copy anywhere else on ``sys.path`` (``PYTHONPATH``, the user's site-packages, the working directory, a ``.pth``
   entry), however self-consistent, is refused (residual risk R1). Replacing the copy next to the runtime needs the
   same write access as replacing the runtime itself;
5. the import path is closed (:func:`environment_problems`, owner decision on R1): the user's site-packages is not on
   it, no entry lies in the data directory, no entry or parent directory is writable by another user or a foreign
   group, and when this process cannot write its own installation, it cannot write any entry of the import path
   either. Where these permissions cannot be read (Windows ACLs) the host import is refused;
6. no other distribution installed in that directory has put files into the trusted package or its metadata (its
   ``RECORD`` lists them, also from a wheel's ``.data`` folder), no legacy installation is there, and no ``.pth``
   file with unknown code runs at interpreter start (:func:`installation_problems`, review H1);

and the import itself runs from exactly those verified bytes (:class:`_VerifiedFinder`, first on ``sys.meta_path``):
no cached bytecode, no other copy of the package on ``sys.path``, no file the ``RECORD`` does not list. Anything that
claims a trusted name and fails a check is never imported (:class:`UntrustedError`; the caller reports it as
``invalid``). An editable install lists no package files in its ``RECORD`` and is therefore not trusted.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
import hashlib
import importlib
from importlib import metadata
import importlib.abc
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import re
import site
import stat
import sys
import threading
from types import ModuleType
from typing import Any

#: (normalized distribution name, entry point name) -> the exact ``module:attribute`` it must name.
TRUSTED_ENTRY_POINTS: dict[tuple[str, str], str] = {
    ("nodestro-integrations", "tesseract-ocr"): "nodestro_integrations.ocr:integration",
}
#: The distribution names that may only come from the checks above.
TRUSTED_DISTRIBUTIONS: frozenset[str] = frozenset(name for name, _entry in TRUSTED_ENTRY_POINTS)
_MAX_FILE_BYTES = 4 * 1024 * 1024
_FINDERS: dict[str, _VerifiedFinder] = {}
_FINDERS_LOCK = threading.Lock()


class UntrustedError(Exception):
    """An entry point that claims to be Nodestro's own and fails a check; it is never imported."""


def normalized(name: object) -> str:
    return re.sub(r"[-_.]+", "-", str(name or "")).lower()


def claims_trusted(entry: metadata.EntryPoint) -> bool:
    """Whether ``entry``'s distribution calls itself by a trusted name (only a claim; see :func:`verify`)."""
    dist = getattr(entry, "dist", None)
    return dist is not None and normalized(dist.metadata["Name"]) in TRUSTED_DISTRIBUTIONS


def _hash(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def _read(path: Path) -> bytes:
    if os.path.realpath(path) != str(path):
        raise UntrustedError(f"{path.name} is reached through a symbolic link")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
            raise UntrustedError(f"{path.name} is not a regular file of a plausible size")
        return handle.read(_MAX_FILE_BYTES + 1)


def installation_home() -> Path | None:
    """The directory the running ``nodestro_runtime`` package was imported from (its ``site-packages``); the only
    place a trusted distribution is accepted from. ``None`` when it cannot be told."""
    import nodestro_runtime

    location = getattr(nodestro_runtime, "__file__", None)
    return Path(os.path.realpath(location)).parent.parent if location else None


def _same(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first)) == os.path.normcase(str(second))


def _inside(path: Path, folder: Path) -> bool:
    return _same(path, folder) or os.path.normcase(str(path)).startswith(os.path.normcase(str(folder)) + os.sep)


#: Whether the permissions of the import path can be read as POSIX modes and owners (not on Windows: ACLs).
_POSIX_MODES = os.name == "posix"


def _ids() -> tuple[int, int]:
    """The effective user and group id of this process."""
    return os.geteuid(), os.getegid()


def _status(path: Path) -> os.stat_result:
    return os.stat(path)


def _writable(path: Path) -> bool:
    return os.access(path, os.W_OK)


def _existing(path: Path) -> Path:
    """``path`` or its nearest existing parent (whoever can write that one can create ``path``)."""
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _unsafe(folder: Path, *, own: bool) -> str:
    """Why another user could change ``folder`` (empty: nobody but root and this user). ``own``: ``folder`` is the
    entry itself, not a parent (a parent writable by everyone with the sticky bit, like ``/tmp``, is fine)."""
    euid, egid = _ids()
    info = _status(folder)
    if info.st_uid not in (0, euid):
        return f"{folder} belongs to another user"
    if info.st_mode & stat.S_IWOTH and (own or not info.st_mode & stat.S_ISVTX):
        return f"{folder} is writable by every user"
    if info.st_mode & stat.S_IWGRP and info.st_gid not in (0, egid):  # also for a folder of root (review M1)
        return f"{folder} is writable by a group of other users"
    return ""


def environment_problems(
    home: Path, *, data_dir: str | Path | None = None, path: Sequence[str] | None = None
) -> list[str]:
    """Why the import path ``path`` (default ``sys.path``) of a process whose runtime lives in ``home`` is not closed
    against users and processes that may not change the installation (owner decision on R1; empty: closed).

    - the user's site-packages is on it (every ``pip install --user`` and its ``.pth`` files reach the host);
    - an entry, or ``home``, lies in ``data_dir`` (where the application builds environments and stores files);
    - an entry (an empty or relative one is the working directory) or one of its parents belongs to another user than
      root and this user, is writable by every user, or by a group other than root's or this user's own;
    - this process cannot write ``home`` (a sealed installation, like the container images) but can write an entry;
    - the permissions cannot be read on this system (Windows: ACLs), so nothing of the above is proven.
    """
    entries = list(sys.path if path is None else path)
    cwd = Path(os.getcwd())
    folders: list[Path] = []
    holders: list[Path] = []  # the folders that hold a symbolic link on the way to an entry (review L1)
    for entry in [str(home), *entries]:
        written = Path(os.path.normpath(cwd / entry if entry else cwd))
        folder = Path(os.path.realpath(written))
        if not any(_same(folder, seen) for seen in folders):
            folders.append(folder)
        if not _same(folder, written):
            for parent in written.parents:
                holder = Path(os.path.realpath(parent))
                if not any(_same(holder, seen) for seen in holders):
                    holders.append(holder)
    problems: list[str] = []
    if site.ENABLE_USER_SITE:
        user_site = Path(os.path.realpath(site.getusersitepackages()))
        if any(_same(folder, user_site) for folder in folders):
            problems.append(f"the user's site-packages {user_site} is on the import path")
    if data_dir is not None:
        data = Path(os.path.realpath(data_dir))
        problems += [f"{folder} lies in the data directory" for folder in folders if _inside(folder, data)]
    if not _POSIX_MODES:
        problems.append("the permissions of the import path cannot be checked on this system")
        return problems
    sealed = not _writable(_existing(home))
    for folder in folders:
        existing = _existing(folder)
        if sealed and _writable(existing):
            problems.append(f"{existing} is writable by this process, its installation {home} is not")
            continue
        for index, parent in enumerate([existing, *existing.parents]):
            reason = _unsafe(parent, own=index == 0)
            if reason:
                problems.append(reason)
                break
    for holder in holders:  # whoever can change the link can point the entry elsewhere
        existing = _existing(holder)
        for parent in [existing, *existing.parents]:
            reason = _unsafe(parent, own=False)
            if reason:
                problems.append(reason)
                break
    return list(dict.fromkeys(problems))


#: ``.pth`` files whose code is known exactly (SHA-256 of the whole file), from well-known installation tooling:
#: setuptools' ``distutils-precedence.pth`` (the same bytes in setuptools 70.0, 75.8 and 80.9, checked; Debian names it
#: ``zz-distutils-precedence.pth``). It only selects setuptools' copy of ``distutils`` when that is imported.
KNOWN_PTH_CODE: frozenset[str] = frozenset(
    {
        "2638ce9e2500e572a5e0de7faed6661eb569d1b696fcba07b0dd223da5f5d224",
    }
)
_PTH_CODE = re.compile(rb"^import[ \t]", re.MULTILINE)


def _record_paths(info: Path) -> list[str] | None:
    """The paths a ``.dist-info`` folder's ``RECORD`` lists (``None``: no readable ``RECORD``)."""
    import csv

    try:
        text = _read(info / "RECORD").decode("utf-8")
    except (OSError, UntrustedError, UnicodeDecodeError):
        return None
    return [row[0] for row in csv.reader(text.splitlines()) if row and row[0]]


def installation_problems(home: Path, *, own: str, top: str, site_dirs: Sequence[str] | None = None) -> list[str]:
    """Why the files of the trusted distribution in ``home`` may not be its own (review H1; empty: none).

    ``own`` is its ``.dist-info`` folder, ``top`` its top-level package. Every installer records the files it put
    in place in its distribution's ``RECORD``, also those a wheel brings in its ``.data/purelib`` folder; so

    - another ``.dist-info`` in ``home`` whose ``RECORD`` lists a path in ``top`` or in a ``.dist-info`` of the
      trusted name (it replaced or added to the trusted files), or that has no readable ``RECORD``;
    - a legacy installation in ``home`` (``.egg-info``, ``.egg``, ``.egg-link``), whose files cannot be told;
    - a ``.pth`` file with code (a line starting with ``import``, run by every interpreter start) in ``home`` or a
      site directory (``site_dirs``, default :func:`site.getsitepackages`) whose bytes are not in
      :data:`KNOWN_PTH_CODE`; a ``.pth`` line that only names a folder puts it on ``sys.path``, which
      :func:`environment_problems` checks.
    """
    trusted = {normalized(name) for name in TRUSTED_DISTRIBUTIONS}
    guarded = [home / top]
    problems: list[str] = []
    try:
        children = sorted(home.iterdir())
    except OSError as exc:
        return [f"cannot list {home}: {exc.strerror}"]
    for child in children:
        name = child.name
        if name.endswith(".dist-info") and normalized(name.split("-", 1)[0]) in trusted:
            guarded.append(child)
            if name != own:
                problems.append(f"{home} holds another metadata folder of a trusted name, {name}")
    for child in children:
        name = child.name
        if name.endswith((".egg-info", ".egg", ".egg-link")):
            problems.append(f"{name} in {home} is a legacy installation whose files cannot be told")
        if not name.endswith(".dist-info") or any(child == folder for folder in guarded):
            continue
        listed = _record_paths(child)
        if listed is None:
            problems.append(f"{name} in {home} has no readable RECORD")
            continue
        for item in listed:
            target = os.path.normpath(home / item)
            if any(_inside(Path(target), folder) for folder in guarded):
                problems.append(f"{name.removesuffix('.dist-info')} also installed {item}")
                break
    dirs = [str(home), *(site.getsitepackages() if site_dirs is None else site_dirs)]
    for folder in dict.fromkeys(os.path.realpath(entry) for entry in dirs):
        try:
            pth_files = sorted(Path(folder).glob("*.pth"))
        except OSError:
            continue
        for pth in pth_files:
            try:
                data = _read(Path(os.path.realpath(pth)))
            except (OSError, UntrustedError):
                problems.append(f"{pth} cannot be read")
                continue
            if _PTH_CODE.search(data) and hashlib.sha256(data).hexdigest() not in KNOWN_PTH_CODE:
                problems.append(f"{pth} runs code at every interpreter start")
    return list(dict.fromkeys(problems))


def verify(entry: metadata.EntryPoint, *, data_dir: str | Path | None = None) -> tuple[str, Path, dict[str, bytes]]:
    """``(top-level package, its directory, {file path: verified source})`` for a trusted ``entry``; raises
    :class:`UntrustedError` (see the module documentation). Nothing is imported. ``data_dir``: the workspace's data
    directory, which no entry of the import path may lie in."""
    dist = getattr(entry, "dist", None)
    if dist is None:
        raise UntrustedError("the entry point has no distribution")
    name = normalized(dist.metadata["Name"])
    if TRUSTED_ENTRY_POINTS.get((name, entry.name)) != entry.value:
        raise UntrustedError(f"{entry.name} = {entry.value} is not an entry point of {name}")
    folder = getattr(dist, "_path", None)
    expected = f"{name.replace('-', '_')}-{dist.version}.dist-info"
    if folder is None or Path(folder).name != expected:
        raise UntrustedError(f"the distribution's metadata folder is not {expected}")
    base = Path(str(dist.locate_file(""))).resolve()
    if Path(folder).resolve().parent != base:
        raise UntrustedError("the metadata folder is not in the installation directory")
    home = installation_home()
    if home is None or not _same(base, home):
        raise UntrustedError(f"it is not installed next to nodestro_runtime ({home}) but in {base}")
    problems = environment_problems(home, data_dir=data_dir)
    if problems:
        raise UntrustedError("the import path is not closed: " + "; ".join(problems[:3]))
    module = entry.value.split(":")[0]
    top = module.split(".")[0]
    problems = installation_problems(home, own=expected, top=top)
    if problems:
        raise UntrustedError("its installation is not its own: " + "; ".join(problems[:3]))
    sources: dict[str, bytes] = {}
    for item in dist.files or ():
        parts = item.parts
        if not parts or parts[0] != top or not parts[-1].endswith(".py"):
            continue
        if any(part in ("", ".", "..") for part in parts):
            raise UntrustedError(f"unexpected file name {item}")
        if item.hash is None or item.hash.mode != "sha256":
            raise UntrustedError(f"{item} has no SHA-256 hash in RECORD")
        path = base.joinpath(*parts)
        try:
            data = _read(path)
        except OSError as exc:
            raise UntrustedError(f"cannot read {item}: {exc.strerror}") from None
        if _hash(data) != item.hash.value:
            raise UntrustedError(f"{item} does not match its hash in RECORD")
        sources[str(path)] = data
    package = base / top
    target = base.joinpath(*module.split("."))
    if str(package / "__init__.py") not in sources or not (
        str(target.with_suffix(".py")) in sources or str(target / "__init__.py") in sources
    ):
        raise UntrustedError(f"RECORD does not list {module} with a hash (an editable install is not trusted)")
    return top, package, sources


class _VerifiedLoader(importlib.machinery.SourceFileLoader):
    """Compiles a module from its verified bytes; never reads cached bytecode or the file again."""

    def __init__(self, fullname: str, path: str, source: bytes) -> None:
        super().__init__(fullname, path)
        self._source = source

    def get_code(self, fullname: str) -> Any:
        return compile(self._source, self.path, "exec", dont_inherit=True)


class _VerifiedFinder(importlib.abc.MetaPathFinder):
    """Serves the modules of one verified package from their verified bytes and refuses every other module of it."""

    def __init__(self, top: str, package: Path, sources: dict[str, bytes]) -> None:
        self.top = top
        self.package = package
        self.sources = sources

    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname != self.top and not fullname.startswith(self.top + "."):
            return None
        location = self.package.joinpath(*fullname.split(".")[1:])
        for candidate, is_package in ((location / "__init__.py", True), (location.with_suffix(".py"), False)):
            source = self.sources.get(str(candidate))
            if source is not None:
                return importlib.util.spec_from_file_location(
                    fullname,
                    candidate,
                    loader=_VerifiedLoader(fullname, str(candidate), source),
                    submodule_search_locations=[str(location)] if is_package else None,
                )
        raise ModuleNotFoundError(f"{fullname} is not a verified file of {self.top}", name=fullname)


def load(entry: metadata.EntryPoint, *, data_dir: str | Path | None = None) -> Any:
    """Import a trusted ``entry`` from its verified files (:func:`verify`) and return its value."""
    top, package, sources = verify(entry, data_dir=data_dir)
    with _FINDERS_LOCK:
        finder = _FINDERS.get(top)
        if finder is None or finder.package != package:
            if finder is not None:
                raise UntrustedError(f"{top} was already verified at another location")
            finder = _FINDERS[top] = _VerifiedFinder(top, package, sources)
            sys.meta_path.insert(0, finder)
        else:
            finder.sources = sources
    loaded = sys.modules.get(top)
    if loaded is not None and os.path.realpath(str(getattr(loaded, "__file__", ""))) != str(package / "__init__.py"):
        raise UntrustedError(f"another {top} is already imported")
    module, _, attribute = entry.value.partition(":")
    value: Any = importlib.import_module(module)
    for part in attribute.split("."):
        value = getattr(value, part)
    return value


__all__ = [
    "TRUSTED_DISTRIBUTIONS",
    "TRUSTED_ENTRY_POINTS",
    "UntrustedError",
    "claims_trusted",
    "environment_problems",
    "installation_home",
    "installation_problems",
    "load",
    "verify",
]
