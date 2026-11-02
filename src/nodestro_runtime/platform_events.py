"""Hooks that mirror Nodestro's audit events and AI usage into a platform backend (Axentra in the server profile).

Nodestro's own records stay the source of truth: the approval audit trail (``tool_approval_audit``), the MCP event
log and the usage ledger (:mod:`~nodestro_runtime.usage_log`) are written exactly as before. When a
:class:`PlatformEvents` sink is attached to the store (``store.platform_events``; only a PostgreSQL store of a server
with ``NODESTRO_AXENTRA_AUDIT`` / ``NODESTRO_AXENTRA_USAGE`` switched on has one), the record and its mirror are
written in one transaction on one connection: both commit, or neither does. A sink that fails rolls back only its own
savepoint and logs the failure; the Nodestro record is kept, and ``nodestro-server axentra reconcile`` writes what
is missing (idempotently) later. This module never imports Axentra.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class PlatformEvents(Protocol):
    """A sink for mirrored records; called inside the transaction that stores the Nodestro record."""

    def audit(
        self,
        tenant_id: str,
        *,
        event_id: str,
        at: datetime,
        action: str,
        actor: str | None,
        target_type: str,
        target_id: str,
        outcome: str,
        details: dict[str, Any],
    ) -> None: ...

    def usage(self, tenant_id: str, entry: Any) -> None: ...


def platform_events(store: object) -> PlatformEvents | None:
    """The sink attached to ``store`` (``None``: nothing is mirrored, e.g. the file store of the desktop app)."""
    return getattr(store, "platform_events", None)


__all__ = ["PlatformEvents", "platform_events"]
