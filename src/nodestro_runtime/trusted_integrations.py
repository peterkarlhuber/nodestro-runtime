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
6. no other distribution in a site directory or a folder of ``sys.path`` has put files into the trusted package or
   its metadata (its ``RECORD`` names them, compared by file identity), no legacy installation is there, and every
   ``.pth`` file of a site directory is a known one, and no file is named by two distributions' ``RECORD``s
   (:func:`installation_problems`, reviews H1, N1 to N3 and NEW-1);
7. no ``sitecustomize`` or ``usercustomize`` module that is not a known one is loaded or importable
   (:func:`startup_hook_problems`, review NEW-2);

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
    links: list[tuple[Path, Path]] = []  # (holding folder, link) of every link on the way to an entry (L1, N4)
    unresolved: list[Path] = []
    for entry in [str(home), *entries]:
        written = Path(os.path.join(cwd, entry)) if entry else cwd
        folder = Path(os.path.realpath(written))
        if not any(_same(folder, seen) for seen in folders):
            folders.append(folder)
        met, resolved = _links(written)
        links += [pair for pair in met if pair not in links]
        if not resolved:
            unresolved.append(written)
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
    problems += [f"{written} has too many symbolic links to be resolved" for written in unresolved]
    euid, _egid = _ids()
    for holder, link in links:  # whoever can change a link, or the folder holding it, can point the entry elsewhere
        if os.lstat(link).st_uid not in (0, euid):
            problems.append(f"the symbolic link {link} belongs to another user")
        for parent in [holder, *holder.parents]:
            reason = _unsafe(parent, own=False)
            if reason:
                problems.append(reason)
                break
    return list(dict.fromkeys(problems))


def _links(written: Path) -> tuple[list[tuple[Path, Path]], bool]:
    """Every symbolic link met while ``written`` is resolved hop by hop, as ``(the real folder holding it, the
    link)``, and whether the resolution ended (``False``: more than 40 links, a loop)."""
    met: list[tuple[Path, Path]] = []
    parts = list(written.parts)
    if not parts:
        return met, True
    current = Path(parts.pop(0))
    hops = 0
    while parts:
        part = parts.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        if not candidate.is_symlink():
            current = candidate
            continue
        hops += 1
        if hops > 40:
            return met, False
        met.append((current, candidate))
        target = Path(os.readlink(candidate))
        if target.is_absolute():
            current, parts = Path(target.anchor), [*target.parts[1:], *parts]
        else:
            parts = [*target.parts, *parts]
    return met, True


#: The only ``.pth`` files accepted in a site directory, by the SHA-256 of their exact bytes (review N1). The
#: interpreter reads every ``.pth`` file of a site directory at start (``site.addpackage``: decoded as UTF-8 with or
#: without a byte order mark and split by ``str.splitlines``, so a line can also end in ``\r``, ``\x0c``, U+0085 or
#: U+2028), runs each line that starts with ``import`` and puts every other line on ``sys.path``; its content is not
#: judged here, only its identity. The one entry is setuptools' ``distutils-precedence.pth`` (the same bytes in
#: setuptools 65.5, 68.2, 69.5, 70.0, 72.1, 75.8, 78.1, 80.9, 80.10 and 84.2, checked; 58.1 differs and is not
#: accepted; Debian names it ``zz-distutils-precedence.pth``), which only selects setuptools' copy of ``distutils``
#: when that is imported. The products' environments need no other one.
KNOWN_PTH: frozenset[str] = frozenset(
    {
        "2638ce9e2500e572a5e0de7faed6661eb569d1b696fcba07b0dd223da5f5d224",
    }
)


def _record_paths(info: Path) -> list[str] | None:
    """The paths a ``.dist-info`` folder's ``RECORD`` lists (``None``: no readable ``RECORD``)."""
    import csv

    try:
        text = _read(info / "RECORD").decode("utf-8")
    except (OSError, UntrustedError, UnicodeDecodeError):
        return None
    return [row[0] for row in csv.reader(text.splitlines()) if row and row[0]]


def _identity(path: Path) -> tuple[int, int] | None:
    """The file system identity of ``path`` (device and inode, links followed); ``None`` when it does not exist.
    Two names of one file (another case on a case-insensitive file system, a symbolic link) have one identity."""
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    return info.st_dev, info.st_ino


def _real_folders(paths: Sequence[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        real = Path(os.path.realpath(path))
        if real.is_dir() and not any(_same(real, seen) for seen in found):
            found.append(real)
    return found


def _scan_folders(home: Path) -> tuple[list[Path], list[Path]]:
    """``(site directories, every folder to scan)``: the site directories whose ``.pth`` files the interpreter reads
    (``home``, :func:`site.getsitepackages`, the user's when enabled), and those with every folder of ``sys.path``
    (purelib and platlib may differ, ``lib`` and ``lib64``). Real, existing folders only."""
    sites = [home, *(Path(folder) for folder in site.getsitepackages())]
    if site.ENABLE_USER_SITE:
        sites.append(Path(site.getusersitepackages()))
    cwd = Path(os.getcwd())
    entries = [Path(os.path.join(cwd, entry)) if entry else cwd for entry in sys.path]
    sites = _real_folders(sites)
    return sites, _real_folders([*sites, *entries])


def _runtime_package() -> Path | None:
    """The folder of the running ``nodestro_runtime`` package (real path)."""
    import nodestro_runtime

    location = getattr(nodestro_runtime, "__file__", None)
    return Path(os.path.realpath(location)).parent if location else None


def installation_problems(
    home: Path,
    *,
    own: str,
    top: str,
    sites: Sequence[Path] | None = None,
    folders: Sequence[Path] | None = None,
    runtime: Path | None = None,
) -> list[str]:
    """Why the files of the trusted distribution in ``home`` may not be its own (reviews H1, N1 to N3, NEW-1; empty:
    none).

    ``own`` is its ``.dist-info`` folder, ``top`` its top-level package; ``sites`` and ``folders`` default to
    :func:`_scan_folders`, ``runtime`` to the running ``nodestro_runtime`` package when it lies in ``home``. Every
    installer records the files it put in place in its distribution's ``RECORD``, also those a wheel brings in its
    ``.data`` folder (``purelib``, ``platlib``, ``data`` …, relative to the ``RECORD``'s folder, with ``..`` where they
    lie elsewhere). Paths are compared by file identity (device and inode), never by name: another case on a
    case-insensitive file system, a symbolic link or ``..`` reach the same file (N3). In every scanned folder:

    - one file named by the ``RECORD``s of two distributions is refused: the later install replaced the earlier one's
      file (NEW-1: any module the host imports, the runtime's, a dependency's or the integration's);
    - a ``RECORD`` that names a file in the trusted package or its metadata folder, or in the running runtime's
      package or its metadata folder, is refused unless it is that distribution's own;
    - a ``.dist-info`` without a readable ``RECORD``, and a legacy installation (``.egg-info``, ``.egg``,
      ``.egg-link``) whose files cannot be told, are refused; so is a second metadata folder of the trusted name or
      of the runtime next to the runtime;

    and every ``.pth`` file of a site directory must be one of :data:`KNOWN_PTH` (N1).

    What this cannot see: a replaced copy of this module itself (or of a module imported before it) does the checking,
    so it says what its author wants; files written without any installer. Both need write access to the runtime's
    environment, which R1 leaves to trusted people and processes (``docs/extension-isolation.md``).
    """
    if sites is None or folders is None:
        default_sites, default_folders = _scan_folders(home)
        sites = default_sites if sites is None else sites
        folders = default_folders if folders is None else folders
    if runtime is None:
        package = _runtime_package()
        runtime = package if package is not None and _identity(package.parent) == _identity(home) else None
    trusted = {normalized(name) for name in TRUSTED_DISTRIBUTIONS}
    problems: list[str] = []
    home_identity = _identity(home)
    runtime_infos = sorted(
        child.name
        for child in (home.iterdir() if home.is_dir() else ())
        if child.name.endswith(".dist-info") and normalized(child.name.split("-", 1)[0]) == "nodestro-runtime"
    )
    #: file system identity of a guarded folder -> the label of the one distribution that may name files in it.
    areas: dict[tuple[int, int], str] = {}

    def label(folder: Path, info: str) -> str:
        return f"{info.removesuffix('.dist-info')} in {folder}"

    for folder_path, owner in ((home / top, own), (home / own, own)):
        identity = _identity(folder_path)
        if identity is not None:
            areas[identity] = label(Path(home), owner)
    if runtime is not None:
        if len(runtime_infos) != 1:
            problems.append(f"{home} holds {len(runtime_infos)} metadata folders of nodestro-runtime, not one")
        for info in runtime_infos[:1]:
            for folder_path in (runtime, home / info):
                identity = _identity(folder_path)
                if identity is not None:
                    areas[identity] = label(Path(home), info)
    known: dict[str, tuple[int, int] | None] = {}

    def area_of(target: Path) -> str | None:
        for folder in target.parents:
            key = str(folder)
            if key not in known:
                known[key] = _identity(folder)
            found = known[key]
            if found is not None and found in areas:
                return areas[found]
        return None

    claims: dict[tuple[int, int], str] = {}
    scanned: set[tuple[int, int] | None] = set()
    for folder in [Path(home), *(Path(entry) for entry in folders)]:
        folder_identity = _identity(folder)
        if folder_identity in scanned:
            continue  # the same folder again, under another name
        scanned.add(folder_identity)
        try:
            children = sorted(folder.iterdir())
        except OSError as exc:
            problems.append(f"cannot list {folder}: {exc.strerror}")
            continue
        in_home = folder_identity == home_identity
        for child in children:
            name = child.name
            if name.endswith((".egg-info", ".egg", ".egg-link")):
                problems.append(f"{name} in {folder} is a legacy installation whose files cannot be told")
                continue
            if not name.endswith(".dist-info"):
                continue
            if in_home and normalized(name.split("-", 1)[0]) in trusted and name != own:
                problems.append(f"{home} holds another metadata folder of a trusted name, {name}")
                continue
            listed = _record_paths(child)
            if listed is None:
                problems.append(f"{name} in {folder} has no readable RECORD")
                continue
            mine = label(folder, name)
            for item in listed:
                target = Path(os.path.join(folder, item))
                area_owner = area_of(target)
                if area_owner is not None and area_owner != mine:
                    problems.append(f"{mine} also installed {item}")
                    break
                identity = _identity(target)
                if identity is None:
                    continue
                first = claims.setdefault(identity, mine)
                if first != mine:
                    problems.append(f"{mine} and {first} both installed {item}")
                    break
    for folder in dict.fromkeys(Path(entry) for entry in [home, *sites]):
        try:
            pth_files = sorted(path for path in folder.iterdir() if path.name.lower().endswith(".pth"))
        except OSError:
            continue
        for pth in pth_files:
            try:
                data = _read(Path(os.path.realpath(pth)))
            except (OSError, UntrustedError):
                problems.append(f"{pth} cannot be read")
                continue
            if hashlib.sha256(data).hexdigest() not in KNOWN_PTH:
                problems.append(f"{pth} is not a known .pth file (the interpreter reads it at every start)")
    return list(dict.fromkeys(problems))


#: The only ``sitecustomize`` and ``usercustomize`` modules accepted, by the SHA-256 of their exact bytes (review
#: NEW-2). ``site`` imports them from ``sys.path`` at every interpreter start. The products have none (the desktop's
#: python-build-standalone interpreter and the images' ``python:3.13-slim`` with a virtual environment), so the list
#: is empty.
KNOWN_STARTUP_MODULES: frozenset[str] = frozenset()
STARTUP_MODULES = ("sitecustomize", "usercustomize")


def startup_hook_problems(path: Sequence[str] | None = None, modules: dict[str, Any] | None = None) -> list[str]:
    """Why code may run at every interpreter start (review NEW-2; empty: none): a ``sitecustomize`` or
    ``usercustomize`` module that this process imported (``modules``, default :data:`sys.modules`) or that a search
    of ``path`` (default ``sys.path``) finds, whose bytes are not in :data:`KNOWN_STARTUP_MODULES`. A package or a
    module without a source file (a namespace folder, a compiled module) is refused, it cannot be compared."""
    import importlib.machinery

    loaded = sys.modules if modules is None else modules
    search = list(sys.path if path is None else path)
    problems: list[str] = []
    for name in STARTUP_MODULES:
        origins: list[str] = []
        module = loaded.get(name)
        if module is not None:
            origins.append(str(getattr(module, "__file__", None) or "(no file)"))
        try:
            spec = importlib.machinery.PathFinder.find_spec(name, search)
        except (ImportError, ValueError):
            spec = None
        if spec is not None:
            origins.append(str(spec.origin or "(a namespace folder)"))
        for origin in dict.fromkeys(origins):
            try:
                data = _read(Path(os.path.realpath(origin)))
            except (OSError, UntrustedError, ValueError):
                data = None
            if (
                data is None
                or not origin.endswith(".py")
                or hashlib.sha256(data).hexdigest() not in KNOWN_STARTUP_MODULES
            ):
                problems.append(f"{name} ({origin}) runs at every interpreter start")
    return problems


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
    if not isinstance(folder, (str, os.PathLike)):  # a zip archive or another kind of distribution (review N5)
        raise UntrustedError("it is not installed as files in a folder")
    if Path(folder).name != expected:
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
    problems = startup_hook_problems()
    if problems:
        raise UntrustedError("code runs at every interpreter start: " + "; ".join(problems[:3]))
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
    "startup_hook_problems",
    "verify",
]
