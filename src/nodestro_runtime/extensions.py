"""The extension contract: how optional integrations plug into the runtime.

An integration is a plain :class:`Integration` value. It can contribute

- ``tools``: trusted, hand-written :class:`orkexa.Tool` objects that run in the host process
  like the built-in tools (they appear in the tool library as ``builtin``), and
- ``ocr``: an :class:`OcrEngine` that recognises the text of scanned PDFs for knowledge
  files and run inputs.

Integrations reach a :class:`~nodestro_runtime.workspace.Workspace` in one of two ways:

1. explicitly, ``Workspace(data_dir, integrations=[...])`` (dependency injection; tests and
   embedding applications), or
2. by installation: a distribution declares an entry point in the group
   :data:`ENTRY_POINT_GROUP` that names an :class:`Integration` (or a function without
   arguments returning one); :func:`discover` loads them. The studio installs
   ``nodestro-integrations`` this way.

Every integration states the contract version it was written for (:data:`CONTRACT`); one
for another version is refused with a clear message instead of failing later. Nothing here
starts a service or opens a network connection: an integration is code the runtime calls.

Only Nodestro's own distribution (:data:`TRUSTED_DISTRIBUTIONS`) is imported into the host process, for its OCR
engine; its tools stay locked (``isolation_unavailable``): they would run in the host process, which sees the
environment, the unlocked vault and the whole file system, and the time limit does not stop them. Every other entry
point is never imported (N-22; security review 2026-11 (phase 0R), findings S1 to S3; owner decision E-0R-2 A):
:func:`find_packages` reads it as data, and its tools run only in the Docker sandbox
(:mod:`nodestro_runtime.extension_sandbox`), and only while :data:`EXTENSION_TOOLS_ENV` is ``1`` (off by default) and
the daemon passed its checks. Otherwise they are locked with the reason ``off`` or ``not_secured`` ("locked, not
sufficiently secured"); there is no host-process fallback. Integrations passed explicitly
(``Workspace(integrations=[...])``) are the embedding application's own code and keep their tools. A capability that an
installed integration states for its tools is never taken as given: it counts at least as :data:`~nodestro_runtime.
tool_policy.UNKNOWN` (finding S3) and can only be raised, so a tool cannot pass the approval gate by calling itself
read-only. See ``docs/extension-isolation.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
import logging
import os
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from orkexa import Tool

from . import trusted_integrations
from .documents import OcrResult
from .extension_sandbox import (
    ExtensionPackage,
    ExtensionSandbox,
    ExtensionTool,
    PackageError,
    SandboxUnavailable,
    extension_spec,
    package_of,
)
from .problems import NodestroError
from .tool_policy import UNKNOWN
from .tool_spec import ToolSpec

logger = logging.getLogger(__name__)

#: The entry point group installed integrations register in.
ENTRY_POINT_GROUP = "nodestro.integrations"
#: The version of this contract; it changes only when existing integrations would break.
#: Version 2 (NAL-A1, ADR-0001) lets an integration hand in a :class:`~nodestro_runtime.tool_spec.ToolSpec` with each
#: tool: ``tools`` may hold ``(spec, tool)`` pairs. Integrations written for version 1 keep working unchanged.
CONTRACT = 2
#: Every contract version the runtime still accepts.
SUPPORTED_CONTRACTS: frozenset[int] = frozenset({1, 2})
#: The switch for the tools of installed integrations (N-22), a development switch: off by default in every profile.
#: On, the tools of foreign packages run in the Docker sandbox once the daemon passed its checks; no tool class is
#: enabled by default before the security evidence G-6 (``docs/extension-isolation.md``).
EXTENSION_TOOLS_ENV = "NODESTRO_EXTENSION_TOOLS"
#: The distributions whose entry points may be imported into the host process (Nodestro's own; for their OCR
#: engines), and only after the checks of :mod:`nodestro_runtime.trusted_integrations`.
TRUSTED_DISTRIBUTIONS: frozenset[str] = trusted_integrations.TRUSTED_DISTRIBUTIONS
TRUSTED_DISTRIBUTIONS_TEXT = ", ".join(sorted(TRUSTED_DISTRIBUTIONS))
#: What a locked package or tool shows.
LOCKED_MESSAGE = "locked, not sufficiently secured"

#: Why tools of installed integrations are locked: ``off`` (the switch is off), ``isolation_unavailable`` (a tool of
#: the trusted distribution, which is imported into the host and has no isolated path), ``not_secured`` (the switch
#: is on, but no Docker sandbox passed its checks) or ``invalid`` (the package could not be listed in the sandbox).
LockReason = Literal["off", "isolation_unavailable", "not_secured", "invalid"]


def extension_tools_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether :data:`EXTENSION_TOOLS_ENV` is ``1``."""
    return (environ if environ is not None else os.environ).get(EXTENSION_TOOLS_ENV, "").strip() == "1"


def extension_tools_lock() -> LockReason:
    """Why tools of the trusted distribution's integrations are locked (they are imported into the host process)."""
    return "isolation_unavailable" if extension_tools_enabled() else "off"


@runtime_checkable
class OcrEngine(Protocol):
    """Recognises the text of a scanned PDF (local programs, never a paid service by default)."""

    name: str

    def available(self) -> bool:
        """Whether the engine can run now (e.g. its programs are installed)."""
        ...

    def recognize(self, data: bytes, name: str) -> OcrResult:
        """The recognised text; raises :class:`~nodestro_runtime.documents.DocumentError` on failure."""
        ...


@dataclass(frozen=True)
class Integration:
    """What one optional integration contributes."""

    name: str
    version: str = "0"
    #: Tools, or (contract 2) ``(spec, tool)`` pairs. A spec's capability always counts as the integration author's
    #: statement (``source="author"``), never as Nodestro's.
    tools: Sequence[Tool | tuple[ToolSpec, Tool]] = field(default_factory=tuple)
    ocr: OcrEngine | None = None
    #: The contract version the integration was written for.
    contract: int = CONTRACT
    description: str = ""


class ExtensionError(NodestroError):
    """An integration that cannot be used (wrong contract version, duplicate tool names)."""


def _split(item: Integration, *, installed: bool = False) -> list[tuple[ToolSpec | None, Tool]]:
    """The tools of ``item`` with their specs (``None``: no spec, contract 1). ``installed``: the integration came from
    an entry point, so its stated capability is replaced by :data:`~nodestro_runtime.tool_policy.UNKNOWN`."""
    pairs: list[tuple[ToolSpec | None, Tool]] = []
    for entry in item.tools:
        if isinstance(entry, tuple):
            if item.contract < 2 or len(entry) != 2:
                raise ExtensionError(
                    "extension.invalid",
                    f"Integration {item.name!r} hands in a tool spec without contract 2",
                    name=item.name,
                )
            spec, tool = entry
            if not isinstance(spec, ToolSpec) or not isinstance(tool, Tool) or spec.id != tool.name:
                raise ExtensionError(
                    "extension.tool_spec", f"A tool spec of {item.name!r} does not match its tool", name=item.name
                )
            author = UNKNOWN if installed else spec.capability.model_copy(update={"source": "author"})
            pairs.append((spec.model_copy(update={"capability": author}), tool))
        else:
            pairs.append((None, entry))
    return pairs


def _check(item: Integration, names: dict[str, str], reserved: set[str]) -> list[str]:
    """The tool names of ``item``; raises when it cannot be used next to the integrations before it."""
    if item.contract not in SUPPORTED_CONTRACTS:
        raise ExtensionError(
            "extension.contract",
            f"Integration {item.name!r} was written for contract {item.contract}, the runtime offers {CONTRACT}",
            name=item.name,
            contract=item.contract,
        )
    found: list[str] = []
    for _spec, tool in _split(item):
        if tool.name is None:
            raise ExtensionError("extension.tool_name", f"A tool of {item.name!r} has no name", name=item.name)
        owner = names.get(tool.name) or ("the runtime" if tool.name in reserved else None)
        if owner is not None or tool.name in found:
            raise ExtensionError(
                "extension.tool_duplicate",
                f"Tool {tool.name!r} of {item.name!r} is already provided by {owner or item.name!r}",
                name=item.name,
                tool=tool.name,
            )
        found.append(tool.name)
    return found


def _load(entry: metadata.EntryPoint, data_dir: str | Path | None) -> Integration:
    value = trusted_integrations.load(entry, data_dir=data_dir)
    if callable(value) and not isinstance(value, Integration):
        value = value()
    if not isinstance(value, Integration):
        raise ExtensionError(
            "extension.invalid",
            f"Entry point {entry.name!r} does not provide an Integration",
            name=entry.name,
        )
    return value


def _listed(entry_points: Callable[[], Iterable[metadata.EntryPoint]] | None) -> list[metadata.EntryPoint]:
    """Every entry point of the group, of every installed distribution (``metadata.entry_points()`` would keep only
    the first distribution of each normalized name on ``sys.path``, so an earlier copy could hide another)."""
    if entry_points is not None:
        return list(entry_points())
    return [
        entry for dist in metadata.distributions() for entry in dist.entry_points if entry.group == ENTRY_POINT_GROUP
    ]


def _rejected(entry: metadata.EntryPoint, data_dir: str | Path | None) -> str:
    """Why ``entry``, which claims a trusted distribution name, may not be imported (empty: it passed every check)."""
    try:
        trusted_integrations.verify(entry, data_dir=data_dir)
    except trusted_integrations.UntrustedError as exc:
        return f"claims to be {TRUSTED_DISTRIBUTIONS_TEXT} but {exc}"
    return ""


def discover(
    entry_points: Callable[[], Iterable[metadata.EntryPoint]] | None = None,
    *,
    data_dir: str | Path | None = None,
) -> list[Integration]:
    """The integrations of Nodestro's own distribution installed in this environment, sorted by name. Only entry
    points that pass :func:`nodestro_runtime.trusted_integrations.verify` are imported, from their verified files
    (installed next to the runtime, on a closed import path; ``data_dir``: the workspace's data directory, which no
    entry of the import path may lie in); every other entry point is left to :func:`find_packages` and never imported.

    A broken integration is logged and left out: an optional extension never stops the
    runtime from starting.
    """
    found: list[Integration] = []
    seen: set[tuple[str, str]] = set()
    for entry in _listed(entry_points):
        if not trusted_integrations.claims_trusted(entry) or _rejected(entry, data_dir):
            continue
        key = (trusted_integrations.normalized(entry.dist.metadata["Name"]) if entry.dist else "", entry.name)
        if key in seen:
            continue
        seen.add(key)
        try:
            found.append(_load(entry, data_dir))
        except Exception:
            logger.exception("Integration %r could not be loaded", entry.name)
    return sorted(found, key=lambda item: item.name)


def find_packages(
    entry_points: Callable[[], Iterable[metadata.EntryPoint]] | None = None,
    *,
    data_dir: str | Path | None = None,
) -> list[ExtensionPackage]:
    """The installed integrations of every other distribution, as data: nothing of them is imported (N-22). An entry
    point that claims Nodestro's own distribution name and fails its checks is listed too, as ``rejected``."""
    found: list[ExtensionPackage] = []
    for entry in _listed(entry_points):
        if not trusted_integrations.claims_trusted(entry):
            found.append(package_of(entry))
        elif reason := _rejected(entry, data_dir):
            logger.warning("The installed integration %r is never loaded: %s", entry.name, reason)
            found.append(package_of(entry, rejected=reason))
    return sorted(found, key=lambda item: item.key)


class Extensions:
    """The integrations of one workspace, checked once.

    With ``strict`` (the default) an integration that does not fit raises :class:`ExtensionError`.
    Without it, such an integration is logged and left out, so an optional installed extension
    never stops the runtime. ``reserved`` names tools the runtime already has (the built-in tools).
    ``installed``: the integrations came from entry points (:func:`discover`); their tools are locked (see the module
    documentation) and listed in :attr:`locked` with the reason.

    ``packages`` are the foreign installed integrations (:func:`find_packages`): never imported, locked (``off``) until
    :meth:`load_packages` lists their tools in the Docker sandbox; :meth:`packages_status` says how each one stands.
    """

    def __init__(
        self,
        integrations: Iterable[Integration] = (),
        *,
        strict: bool = True,
        reserved: Iterable[str] = (),
        installed: bool = False,
        packages: Iterable[ExtensionPackage] = (),
    ) -> None:
        self.integrations: list[Integration] = []
        self.installed = installed
        #: Tool name -> why it is locked (installed integrations only).
        self.locked: dict[str, LockReason] = {}
        self.packages: list[ExtensionPackage] = list(packages)
        #: Package key -> (``None`` when its tools run in the sandbox, else why they are locked, a detail).
        self._package_state: dict[tuple[str, str, str], tuple[LockReason | None, str]] = {}
        keys = [package.key for package in self.packages]
        for package in self.packages:
            if package.rejected:
                self._package_state[package.key] = ("invalid", package.rejected)
            elif keys.count(package.key) > 1:  # two copies that cannot be told apart: neither is used
                self._package_state[package.key] = ("invalid", "installed more than once")
            else:
                self._package_state[package.key] = ("off", "")
        #: Package key -> the names of its tools offered from the sandbox.
        self._package_tools: dict[tuple[str, str, str], list[str]] = {}
        taken = set(reserved)
        names: dict[str, str] = {}
        for item in integrations:
            try:
                tools = _check(item, names, taken)
            except ExtensionError:
                if strict:
                    raise
                logger.exception("Integration %r is left out", item.name)
                continue
            names.update(dict.fromkeys(tools, item.name))
            self.integrations.append(item)
        if installed:
            reason = extension_tools_lock()
            self.locked = dict.fromkeys(names, reason)
            for name, owner in names.items():
                logger.warning(
                    "Tool %r of the installed integration %r is locked (%s): it is imported into the host process, "
                    "and tools of installed integrations run only in the Docker sandbox",
                    name,
                    owner,
                    reason,
                )

    def load_packages(
        self, sandbox: ExtensionSandbox, reserved: Iterable[str], environ: Mapping[str, str] | None = None
    ) -> list[tuple[ToolSpec, ExtensionTool]]:
        """The tools of :attr:`packages` as listed in the Docker sandbox, with their specs (:func:`~nodestro_runtime.
        extension_sandbox.extension_spec`); none while the switch is off (no container starts) or while the sandbox
        has not passed its checks (``not_secured``: nothing runs anywhere else). A package that cannot be listed, or
        whose tool names clash with ``reserved`` or another package's, is locked (``invalid``)."""
        usable = [package for package in self.packages if self._package_state[package.key][0] != "invalid"]
        if not usable:
            return []
        if not extension_tools_enabled(environ):
            return []
        try:
            sandbox.ready()
        except Exception as exc:  # SandboxUnavailable, or an image that cannot be built
            detail = str(exc)[:500]
            logger.warning("Tools of installed integrations stay locked (%s): %s", LOCKED_MESSAGE, detail)
            for package in usable:
                self._package_state[package.key] = ("not_secured", detail)
            return []
        taken = set(reserved) | set(self.locked)
        found: list[tuple[ToolSpec, ExtensionTool]] = []
        for package in usable:
            try:
                described = sandbox.describe(package)
                names = [str(item["name"]) for item in described]
                clash = sorted({name for name in names if name in taken or names.count(name) > 1})
                if clash:
                    raise PackageError(f"tool names already taken: {', '.join(clash)}")
            except SandboxUnavailable as exc:
                self._package_state[package.key] = ("not_secured", str(exc)[:500])
                continue
            except Exception as exc:  # the package's files or answers
                logger.warning("The installed integration %r is locked: %s", package.name, exc)
                self._package_state[package.key] = ("invalid", str(exc)[:500])
                continue
            taken.update(names)
            for item in described:
                spec = extension_spec(str(item["name"]), item["stated"])
                found.append((spec, ExtensionTool(package, item, spec, sandbox)))
            self._package_state[package.key] = (None, "")
            self._package_tools[package.key] = names
        return found

    def packages_status(self) -> list[dict[str, object]]:
        """Each foreign installed integration: name, distribution, version, ``state`` (``sandboxed`` or ``locked``),
        ``reason`` and ``message`` (when locked), ``detail`` and its tools offered from the sandbox."""
        result: list[dict[str, object]] = []
        for package in self.packages:
            reason, detail = self._package_state.get(package.key, ("off", ""))
            result.append(
                {
                    "name": package.name,
                    "distribution": package.distribution,
                    "version": package.version,
                    "state": "locked" if reason else "sandboxed",
                    "reason": reason,
                    "message": LOCKED_MESSAGE if reason else None,
                    "detail": detail or None,
                    "tools": list(self._package_tools.get(package.key, [])),
                }
            )
        return result

    def _pairs(self) -> list[tuple[ToolSpec | None, Tool]]:
        """The usable tools with their specs: none of an installed integration (they are locked)."""
        if self.installed:
            return []
        return [pair for item in self.integrations for pair in _split(item)]

    def tools(self) -> list[Tool]:
        return [tool for _spec, tool in self._pairs()]

    def specs(self) -> dict[str, ToolSpec]:
        """The specs integrations handed in (contract 2), by tool name; their capability is the author's."""
        return {tool.name: spec for spec, tool in self._pairs() if spec and tool.name}

    def ocr(self) -> OcrEngine | None:
        """The first OCR engine that can run now (``None``: OCR unavailable)."""
        for item in self.integrations:
            if item.ocr is not None and item.ocr.available():
                return item.ocr
        return None

    def summary(self) -> list[dict[str, object]]:
        """Name, version and contributions of every integration (for diagnostics), then the foreign installed
        integrations (:meth:`packages_status`, with ``"sandbox": True``)."""
        found: list[dict[str, object]] = [
            {
                "name": item.name,
                "version": item.version,
                "tools": [tool.name for _spec, tool in _split(item)],
                "locked": {
                    name: reason
                    for _spec, tool in _split(item)
                    if (name := tool.name) and (reason := self.locked.get(name))
                },
                "contract": item.contract,
                "ocr": item.ocr.name if item.ocr is not None else None,
            }
            for item in self.integrations
        ]
        return found + [{**status, "sandbox": True} for status in self.packages_status()]
