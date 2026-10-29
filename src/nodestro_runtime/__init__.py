"""Nodestro Runtime: the workflow engine of Nodestro on top of the Orkexa agent library.

The runtime validates, runs, cancels and continues multi-agent workflows without an HTTP
server or an interface. The entry point is :class:`nodestro_runtime.workspace.Workspace`;
the shared data models are in :mod:`nodestro_runtime.schemas`, the extension contract for
optional integrations in :mod:`nodestro_runtime.extensions`.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("nodestro-runtime")
except PackageNotFoundError:  # pragma: no cover - a copy without package metadata (e.g. an export)
    __version__ = "0.0.0"

__all__ = ["__version__"]
