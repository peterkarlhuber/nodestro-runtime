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
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from importlib import metadata
import logging
from typing import Protocol, runtime_checkable

from orkexa import Tool

from .documents import OcrResult
from .problems import NodestroError

logger = logging.getLogger(__name__)

#: The entry point group installed integrations register in.
ENTRY_POINT_GROUP = "nodestro.integrations"
#: The version of this contract; it changes only when existing integrations would break.
CONTRACT = 1


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
    tools: Sequence[Tool] = field(default_factory=tuple)
    ocr: OcrEngine | None = None
    #: The contract version the integration was written for.
    contract: int = CONTRACT
    description: str = ""


class ExtensionError(NodestroError):
    """An integration that cannot be used (wrong contract version, duplicate tool names)."""


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
    """The integrations of one workspace, checked once."""

    def __init__(self, integrations: Iterable[Integration] = ()) -> None:
        self.integrations: list[Integration] = []
        names: dict[str, str] = {}
        for item in integrations:
            if item.contract != CONTRACT:
                raise ExtensionError(
                    "extension.contract",
                    f"Integration {item.name!r} was written for contract {item.contract}, "
                    f"the runtime offers {CONTRACT}",
                    name=item.name,
                    contract=item.contract,
                )
            for tool in item.tools:
                if tool.name is None:
                    raise ExtensionError("extension.tool_name", f"A tool of {item.name!r} has no name", name=item.name)
                if tool.name in names:
                    raise ExtensionError(
                        "extension.tool_duplicate",
                        f"Tool {tool.name!r} of {item.name!r} is already provided by {names[tool.name]!r}",
                        name=item.name,
                        tool=tool.name,
                    )
                names[tool.name] = item.name
            self.integrations.append(item)

    def tools(self) -> list[Tool]:
        return [tool for item in self.integrations for tool in item.tools]

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
                "tools": [tool.name for tool in item.tools],
                "ocr": item.ocr.name if item.ocr is not None else None,
            }
            for item in self.integrations
        ]
