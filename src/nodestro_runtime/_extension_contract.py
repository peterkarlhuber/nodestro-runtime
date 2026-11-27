"""The extension contract *inside* the sandbox container (N-22). Never imported in the host process.

The runtime itself is not installed in the container. This module stands in for the parts of
:mod:`nodestro_runtime.extensions`, :mod:`nodestro_runtime.tool_spec` and :mod:`nodestro_runtime.tool_policy` that an
integration imports to describe itself: ``Integration``, ``ToolSpec`` and ``ToolCapability`` as plain values. The
runner registers it under those three module names. Only the standard library is used; it runs on Python 3.10+.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ENTRY_POINT_GROUP = "nodestro.integrations"
CONTRACT = 2
SUPPORTED_CONTRACTS = frozenset({1, 2})


class ExtensionError(Exception):
    """An integration that cannot be used."""


class _Value:
    """Keeps whatever fields it is given; the host reads only the few it needs."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)

    def model_copy(self, *, update: dict[str, Any] | None = None) -> Any:
        return type(self)(**{**self.__dict__, **(update or {})})


class ToolSpec(_Value):
    """A tool's catalog entry, as an integration states it (only ``capability`` and ``timeout_seconds`` are read)."""


class ToolCapability(_Value):
    """A capability statement (only ``access`` and ``reach`` are read, and only to raise the floor)."""


UNKNOWN = ToolCapability(access="write", reach="external", source="default")


@dataclass(frozen=True)
class Integration:
    """What one integration contributes (the same fields as in the runtime)."""

    name: str
    version: str = "0"
    tools: Any = field(default_factory=tuple)
    ocr: Any = None
    contract: int = CONTRACT
    description: str = ""
