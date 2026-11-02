"""In-app notifications of the server profile through Axentra's notify module (``NODESTRO_AXENTRA_NOTIFY=1``).

Off by default. When switched on, the worker tells the members who may act about two events, in their **in-app
inbox only** (Axentra channel ``inbox``: stored for the user, never handed to a delivery provider, so nothing leaves
the server; there is no e-mail or webhook channel here):

- a top-level run **failed** (also a run interrupted by the reaper, ``run.interrupted``);
- a tool call **waits for an approval**;
- a paused run **waits for a budget decision** (``status = paused`` with ``budget.pending_request_id``; runs without
  that field, i.e. stored before budget decisions existed, are simply not matched).

Recipients are the tenant's members whose role may run and decide (``owner``, ``admin``, ``editor``, ``operator``:
the roles with the ``run`` permission) and that have an Axentra user (linked by the identity backfill); others are
skipped and counted. The content holds ids, the workflow's name, the tool's name and a problem code: never call
arguments, model output, error messages or secrets.

**Idempotent per event.** Each message has the key ``nodestro:notify:<event>:<record id>:<Axentra user>``. A worker
that retries, resumes or crashes after enqueueing replays the key and Axentra returns the stored message; a replay
with other content (a renamed workflow) is refused by Axentra and counted as ``duplicate``, never sent again. The
worker finds events by sweeping the records changed since its last pass (:meth:`AxentraNotifications.sweep`), so
no execution path needs a hook and nothing is lost when a process stops: after a start it looks
:data:`LOOKBACK` back, and older events are not notified (switching the feature on does not flood inboxes).

Nodestro's high-frequency run events and their streams (SSE, ``nodestro_run_events``) are unchanged and not
notifications. This module imports Axentra only inside its methods (server profile, Python 3.11+).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from .axentra_metrics import count_operation

if TYPE_CHECKING:  # pragma: no cover
    from .axentra_platform import AxentraLink

logger = logging.getLogger(__name__)

#: Roles told about failed runs and pending approvals: those with the ``run`` permission (nodestro_api.identity).
NOTIFY_ROLES = ("owner", "admin", "editor", "operator")
#: How far back the first sweep after a start looks.
LOOKBACK = timedelta(hours=1)
#: The inbox templates Nodestro creates in product ``nodestro`` (key, category, variables, subject, text).
TEMPLATES: dict[str, dict[str, Any]] = {
    "nodestro.run_failed": {
        "category": "runs",
        "name": "Run failed",
        "variables": ["workflow", "run_id", "error_code"],
        "sample_data": {"workflow": "Invoice check", "run_id": "run_123", "error_code": "run.interrupted"},
        "variants": {
            "en": {
                "subject": "A run of {{ workflow }} failed",
                "text": "The run {{ run_id }} of the workflow {{ workflow }} failed ({{ error_code }}).",
            }
        },
    },
    "nodestro.approval_pending": {
        "category": "approvals",
        "name": "Tool call waits for approval",
        "variables": ["tool", "run_id"],
        "sample_data": {"tool": "send_mail", "run_id": "run_123"},
        "variants": {
            "en": {
                "subject": "{{ tool }} waits for your approval",
                "text": "A call of the tool {{ tool }} in the run {{ run_id }} waits for an approval.",
            }
        },
    },
    "nodestro.budget_pending": {
        "category": "approvals",
        "name": "Run waits for a budget decision",
        "variables": ["workflow", "run_id"],
        "sample_data": {"workflow": "Invoice check", "run_id": "run_123"},
        "variants": {
            "en": {
                "subject": "A run of {{ workflow }} waits for a budget decision",
                "text": "The run {{ run_id }} of the workflow {{ workflow }} waits for a decision on its budget.",
            }
        },
    },
}
EVENT_TEMPLATES = {
    "run_failed": "nodestro.run_failed",
    "approval_pending": "nodestro.approval_pending",
    "budget_pending": "nodestro.budget_pending",
}


def notification_key(event: str, record_id: str, axentra_user: str) -> str:
    """The idempotency key of one notification: one message per event, record and recipient, ever."""
    return f"nodestro:notify:{event}:{record_id}:{axentra_user}"


@dataclass
class NotifyReport:
    sent: int = 0
    replayed: int = 0
    duplicate: int = 0
    no_recipient: int = 0
    failed: int = 0
    events: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "replayed": self.replayed,
            "duplicate": self.duplicate,
            "no_recipient": self.no_recipient,
            "failed": self.failed,
            "events": dict(sorted(self.events.items())),
        }


class AxentraNotifications:
    """Inbox notifications for one server process (see the module documentation)."""

    def __init__(self, link: AxentraLink, *, lookback: timedelta = LOOKBACK) -> None:
        self.link = link
        self.lookback = lookback
        self._since: datetime | None = None
        self._ready = False

    @property
    def _notify(self) -> Any:
        notify = self.link.services.platform.notify
        if notify is None:
            from .problems import NodestroError

            raise NodestroError("axentra.notify_off", "Axentra's notify module is not switched on")
        return notify

    def ensure_templates(self) -> list[str]:
        """Create the inbox templates that are missing (idempotent, safe across processes); returns the created keys.

        A template of the same key on another channel (changed in Axentra's own admin interface) is refused with
        ``axentra.notify_channel``: Nodestro never sends anything but inbox messages."""
        from axentra.core.context import Principal
        from axentra.core.errors import Conflict, NotFound

        from .problems import NodestroError

        system = Principal.system("nodestro")
        product = self.link.services.product_id
        created = []
        for key, spec in TEMPLATES.items():
            try:
                existing = self._notify.get_template(system, product, key)
            except NotFound:
                existing = None
            if existing is None or existing.builtin:
                try:
                    self._notify.create_template(system, product, key=key, channel="inbox", **spec)
                    created.append(key)
                except Conflict:  # another process created it first
                    existing = self._notify.get_template(system, product, key)
            if existing is not None and existing.channel != "inbox":
                raise NodestroError(
                    "axentra.notify_channel",
                    f"The Axentra template {key} uses the {existing.channel} channel; Nodestro only sends inbox "
                    "notifications (set it back to inbox or delete it)",
                    template=key,
                )
        self._ready = True
        return created

    # -- recipients ----------------------------------------------------------------------------------------------

    def recipients(self, tenant_id: str) -> list[str]:
        """The Axentra users of the tenant's members that may act (:data:`NOTIFY_ROLES`), sorted."""
        rows = self.link.database._query(
            "SELECT l.axentra_id FROM nodestro_memberships m JOIN nodestro_axentra_ids l"
            " ON l.kind = 'user' AND l.nodestro_id = m.user_id WHERE m.tenant_id = %s AND m.role = ANY(%s)"
            " ORDER BY l.axentra_id",
            (tenant_id, list(NOTIFY_ROLES)),
        )
        return [str(row[0]) for row in rows]

    def _tenant(self, tenant_id: str) -> str | None:
        found = self.link.tenant(tenant_id)
        if found is not None:
            return found
        rows = self.link.database._query("SELECT name FROM nodestro_tenants WHERE id = %s", (tenant_id,))
        if not rows:
            return None
        from .axentra_platform import axentra_tenant_name

        return self.link.ensure_tenant(tenant_id, axentra_tenant_name(tenant_id, str(rows[0][0])))

    # -- sending -------------------------------------------------------------------------------------------------

    def notify(
        self, tenant_id: str, event: str, record_id: str, variables: dict[str, Any], report: NotifyReport
    ) -> None:
        """Enqueue the inbox message of one event for every recipient (idempotent per recipient)."""
        from axentra.core.errors import Conflict

        if not self._ready:
            self.ensure_templates()
        report.events[event] = report.events.get(event, 0) + 1
        ax_tenant = self._tenant(tenant_id)
        users = self.recipients(tenant_id) if ax_tenant is not None else []
        if not users:
            report.no_recipient += 1
            count_operation("notification", "no_recipient", kind=event)
            return
        for user in users:
            try:
                result = self._notify.enqueue_system(
                    self.link.services.product_id,
                    ax_tenant,
                    template_key=EVENT_TEMPLATES[event],
                    to_user_id=user,
                    variables=variables,
                    idempotency_key=notification_key(event, record_id, user),
                    reason="nodestro",
                )
            except Conflict:  # the same event with other content: already told, never twice
                report.duplicate += 1
                count_operation("notification", "duplicate", kind=event)
                continue
            except Exception:
                report.failed += 1
                count_operation("notification", "failed", kind=event)
                logger.exception("Could not notify about %s %s of tenant %s", event, record_id, tenant_id)
                continue
            if result.replayed:
                report.replayed += 1
                count_operation("notification", "replayed", kind=event)
            else:
                report.sent += 1
                count_operation("notification", "sent", kind=event)

    def sweep(self, *, since: datetime | None = None) -> NotifyReport:
        """Notify about failed runs and pending approvals changed since the last pass (first pass: :attr:`lookback`).

        Safe to run in several workers at once and to repeat: every message is keyed per event and recipient."""
        started = datetime.now(UTC)
        since = since or self._since or started - self.lookback
        report = NotifyReport()
        database = self.link.database
        for (tenant_id,) in database._query("SELECT id FROM nodestro_tenants ORDER BY id"):
            tenant_id = str(tenant_id)
            failed = database._query(
                "SELECT id, coalesce(data ->> 'workflow_name', ''), coalesce(data ->> 'error_code', 'run.failed')"
                " FROM nodestro_documents WHERE tenant_id = %s AND collection = 'runs' AND data ->> 'status' = 'failed'"
                " AND data ->> 'parent_run_id' IS NULL AND data ->> 'imported_at' IS NULL AND updated_at >= %s"
                " ORDER BY updated_at, id",
                (tenant_id, since),
            )
            for run_id, workflow, code in failed:
                variables = {"workflow": str(workflow)[:120], "run_id": str(run_id), "error_code": str(code)[:80]}
                self.notify(tenant_id, "run_failed", str(run_id), variables, report)
            pending = database._query(
                "SELECT data ->> 'id', coalesce(data ->> 'tool', ''), coalesce(data ->> 'run_id', '')"
                " FROM nodestro_documents WHERE tenant_id = %s AND collection = 'tool_approvals'"
                " AND data ->> 'status' = 'pending' AND updated_at >= %s ORDER BY updated_at, id",
                (tenant_id, since),
            )
            for request_id, tool, run_id in pending:
                variables = {"tool": str(tool)[:120], "run_id": str(run_id)}
                self.notify(tenant_id, "approval_pending", str(request_id), variables, report)
            budget = database._query(
                "SELECT data -> 'budget' ->> 'pending_request_id', id, coalesce(data ->> 'workflow_name', '')"
                " FROM nodestro_documents WHERE tenant_id = %s AND collection = 'runs' AND data ->> 'status' = 'paused'"
                " AND data -> 'budget' ->> 'pending_request_id' IS NOT NULL AND updated_at >= %s"
                " ORDER BY updated_at, id",
                (tenant_id, since),
            )
            for request_id, run_id, workflow in budget:
                variables = {"workflow": str(workflow)[:120], "run_id": str(run_id)}
                self.notify(tenant_id, "budget_pending", str(request_id), variables, report)
        if report.failed == 0:
            # The next pass starts a little before this one did: a record committed while this pass read is not
            # missed (the overlap is replayed, not sent again).
            self._since = started - timedelta(minutes=5)
        return report

    def purge(self) -> dict[str, int]:
        """Axentra's notification retention (content after ``notify.content_retention_days``); the worker's cleanup."""
        result: dict[str, int] = self._notify.purge_retention()
        return result


__all__ = [
    "EVENT_TEMPLATES",
    "LOOKBACK",
    "NOTIFY_ROLES",
    "TEMPLATES",
    "AxentraNotifications",
    "NotifyReport",
    "notification_key",
]
