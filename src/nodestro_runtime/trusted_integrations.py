"""Nodestro's own integrations: the only entry points imported into the host process (N-22, review F1).

A distribution's name is its own statement, so it never decides trust by itself. An entry point is imported only when

1. it is one of :data:`TRUSTED_ENTRY_POINTS`: the distribution's normalized name, the entry point's name and its exact
   ``module:attribute``;
2. its distribution is installed as a wheel installs it: a ``<name>-<version>.dist-info`` folder whose name matches
   the metadata, inside the installation directory of its files;
3. every Python file of the pinned top-level package is listed in that distribution's ``RECORD`` with a SHA-256 hash,
   lies at its own path (no symbolic link on the way), and has exactly that hash now; the pinned module and the
   package's ``__init__.py`` are among them;

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


def verify(entry: metadata.EntryPoint) -> tuple[str, Path, dict[str, bytes]]:
    """``(top-level package, its directory, {file path: verified source})`` for a trusted ``entry``; raises
    :class:`UntrustedError` (see the module documentation). Nothing is imported."""
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
    module = entry.value.split(":")[0]
    top = module.split(".")[0]
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


def load(entry: metadata.EntryPoint) -> Any:
    """Import a trusted ``entry`` from its verified files (:func:`verify`) and return its value."""
    top, package, sources = verify(entry)
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


__all__ = ["TRUSTED_DISTRIBUTIONS", "TRUSTED_ENTRY_POINTS", "UntrustedError", "claims_trusted", "load", "verify"]
