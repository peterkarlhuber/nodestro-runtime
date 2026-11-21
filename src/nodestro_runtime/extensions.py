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

Tools of *installed* integrations are locked (security review 2026-11 (phase 0R), finding S1; owner decision
E-0R-2 A): such code would run in the host process, which sees the environment, the unlocked vault and the whole
file system, and the time
limit does not stop it. Foreign code may only run in the Docker sandbox, and that path does not exist yet (N-22), so
:data:`EXTENSION_TOOLS_ENV` cannot unlock them either: it is the switch N-22 will honour, and today it only changes the
reason a locked tool reports. The integrations' OCR engines are not affected. Integrations passed explicitly
(``Workspace(integrations=[...])``) are the embedding application's own code and keep their tools. A capability that
an installed integration states for its tools is never taken as given: it counts as :data:`~nodestro_runtime.
tool_policy.UNKNOWN` (finding S3), so a tool cannot pass the approval gate by calling itself read-only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from importlib import metadata
import logging
import os
from typing import Literal, Protocol, runtime_checkable

from orkexa import Tool

from .documents import OcrResult
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
#: The switch for the tools of installed integrations (N-22). Off by default; on, it still unlocks nothing until the
#: Docker sandbox path for extension tools exists (owner decision E-0R-2 A: no host-process fallback).
EXTENSION_TOOLS_ENV = "NODESTRO_EXTENSION_TOOLS"

#: Why the tools of installed integrations are locked: ``off`` (the switch is off) or ``isolation_unavailable`` (the
#: switch is on, but no isolated way to run them exists yet).
LockReason = Literal["off", "isolation_unavailable"]


def extension_tools_lock() -> LockReason:
    """Why tools of installed integrations are locked; every value locks them today (see the module docs)."""
    requested = os.environ.get(EXTENSION_TOOLS_ENV, "").strip() == "1"
    return "isolation_unavailable" if requested else "off"


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


def _load(entry: metadata.EntryPoint) -> Integration:
    value = entry.load()
    if callable(value) and not isinstance(value, Integration):
        value = value()
    if not isinstance(value, Integration):
        raise ExtensionError(
            "extension.invalid",
            f"Entry point {entry.name!r} does not provide an Integration",
            name=entry.name,
        )
    return value


def discover(
    entry_points: Callable[[], Iterable[metadata.EntryPoint]] | None = None,
) -> list[Integration]:
    """The integrations installed in this environment, sorted by name.

    A broken integration is logged and left out: an optional extension never stops the
    runtime from starting.
    """
    found: list[Integration] = []
    listed = entry_points() if entry_points is not None else metadata.entry_points(group=ENTRY_POINT_GROUP)
    for entry in listed:
        try:
            found.append(_load(entry))
        except Exception:
            logger.exception("Integration %r could not be loaded", entry.name)
    return sorted(found, key=lambda item: item.name)


class Extensions:
    """The integrations of one workspace, checked once.

    With ``strict`` (the default) an integration that does not fit raises :class:`ExtensionError`.
    Without it, such an integration is logged and left out, so an optional installed extension
    never stops the runtime. ``reserved`` names tools the runtime already has (the built-in tools).
    ``installed``: the integrations came from entry points (:func:`discover`); their tools are locked (see the module
    documentation) and listed in :attr:`locked` with the reason.
    """

    def __init__(
        self,
        integrations: Iterable[Integration] = (),
        *,
        strict: bool = True,
        reserved: Iterable[str] = (),
        installed: bool = False,
    ) -> None:
        self.integrations: list[Integration] = []
        self.installed = installed
        #: Tool name -> why it is locked (installed integrations only).
        self.locked: dict[str, LockReason] = {}
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
                    "Tool %r of the installed integration %r is locked (%s): foreign tools run only in the Docker "
                    "sandbox, which extension tools cannot use yet",
                    name,
                    owner,
                    reason,
                )

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
        """Name, version and contributions of every integration (for diagnostics)."""
        return [
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
