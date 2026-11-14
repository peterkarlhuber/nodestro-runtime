"""Internal notifications (NAL-A3): a local inbox, workflow rules that fill it, and the ``notify_user`` tool.

People learn about important run events without watching the studio, and an agent can leave a short internal
message. Everything stays inside Nodestro: there is no e-mail, webhook or push channel here (that is NAL-A4/A5).

- **Rules** (:class:`NotificationRules`, ``Workflow.notifications``): which events of a workflow's top-level runs
  notify, and whom (``members``: everyone who may read the workspace; ``starter``: the person who started the run).
  Events: ``run_finished``, ``run_failed``, ``approval_pending``, ``budget_pending``, ``quality_failed``. The default
  is ``run_failed`` and ``approval_pending`` (plan NAL-A3, 6).
- **Local inbox** (:class:`LocalInbox`): documents of the workspace's store (collection ``notifications``), so they
  are separated by tenant like every other record. Each event notifies **at most once**: the document's id is derived
  from its key ``nodestro:notify:<event>:<record>`` and written with ``put_raw_if_absent``, so a retry, a resume or a
  second worker never writes it twice. Messages are kept :data:`RETENTION` long.
- **Triggering** (:class:`RunNotifier`): a run listener of the engine, called when a run ends or pauses; it reads the
  stored run, never its arguments or outputs. Imported runs and runs of sub-workflows never notify.
- **Tool** ``notify_user(title, message, level)``: an internal message to the run's starter (or the members);
  ``write`` + ``internal``, no network, at most :data:`MAX_MESSAGES_PER_RUN` per run.

Content: event messages carry ids, the workflow's name and a problem code, never arguments, model output, error texts
or secrets. A ``notify_user`` message is model output: interfaces show it as plain text (never HTML) and say that it
came from an agent.

In the server profile with Axentra's inbox switched on (``NODESTRO_AXENTRA_NOTIFY=1``) the local inbox is off: the
server's Axentra sweep (:mod:`.axentra_notify`) stays the only inbox there, unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
import hashlib
import logging
import os
import threading
from typing import Any, Literal

from orkexa import Tool, ToolResultError
from pydantic import Field

from .schemas import (
    DEFAULT_NOTIFY_EVENTS,
    NOTIFY_EVENTS,
    NotificationEvent,
    NotificationRules,
    Run,
    _Model,
    now,
)

logger = logging.getLogger(__name__)

Level = Literal["info", "success", "warning", "error"]
LEVELS: tuple[Level, ...] = ("info", "success", "warning", "error")
#: A message's kind: one of :data:`EVENTS`, or ``message`` (from ``notify_user``).
Kind = Literal["run_finished", "run_failed", "approval_pending", "budget_pending", "quality_failed", "message"]

COLLECTION = "notifications"
#: How long messages are kept.
RETENTION = timedelta(days=90)
#: ``notify_user`` messages per run (across all steps).
MAX_MESSAGES_PER_RUN = 3
MAX_TITLE = 120
MAX_TEXT = 2000
#: Most people who can mark one message as read (a server's members); more are not recorded.
MAX_READERS = 500
#: The values of ``NODESTRO_AXENTRA_NOTIFY`` that switch Axentra's inbox on (as ``axentra_platform`` reads them).
_ON = frozenset({"1", "true", "yes", "on"})


class Notification(_Model):
    """One message of the local inbox."""

    id: str
    #: ``nodestro:notify:<event>:<record>``: one message per event and record, ever.
    key: str
    kind: Kind
    level: Level = "info"
    #: The title and text. Event messages leave them empty: interfaces write them from ``kind`` and ``params`` in
    #: their own language. A ``notify_user`` message has the agent's text (plain text, model output).
    title: str = ""
    text: str = ""
    params: dict[str, str] = Field(default_factory=dict)
    run_id: str | None = None
    workflow_id: str | None = None
    workflow_name: str = ""
    #: The one person it is for (``starter``); ``None``: every member who may read the workspace.
    user_id: str | None = None
    #: Who read it, and when (user id -> time; ``local`` on a desktop).
    read: dict[str, datetime] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)

    def for_user(self, user_id: str) -> bool:
        return self.user_id is None or self.user_id == user_id

    def read_at(self, user_id: str) -> datetime | None:
        return self.read.get(user_id)


def notification_key(event: str, record_id: str) -> str:
    return f"nodestro:notify:{event}:{record_id}"


def notification_id(key: str) -> str:
    """A message's id from its key: the same event always has the same id (the idempotency of the inbox)."""
    return "ntf_" + hashlib.sha256(key.encode()).hexdigest()[:24]


def local_inbox_enabled(*, server: bool, environ: Mapping[str, str] | None = None) -> bool:
    """Whether a workspace keeps the local inbox: always, except on a server whose Axentra inbox is on."""
    if not server:
        return True
    env = os.environ if environ is None else environ
    return env.get("NODESTRO_AXENTRA_NOTIFY", "").strip().lower() not in _ON


def _clean(text: str, limit: int) -> str:
    """Plain text without control characters (line breaks and tabs kept), cut to ``limit``."""
    kept = "".join(char for char in text if char in "\n\t" or char.isprintable())
    return kept.strip()[:limit]


class LocalInbox:
    """The local inbox of one workspace (one tenant's store); see the module documentation."""

    def __init__(self, store: Any, *, retention: timedelta = RETENTION) -> None:
        self.store = store
        self.retention = retention
        self._lock = threading.Lock()
        self._purged: datetime | None = None

    def add(self, notification: Notification) -> bool:
        """Store a message once; ``False`` when a message with its key exists (nothing changes)."""
        stored = self.store.put_raw_if_absent(COLLECTION, notification.id, notification.model_dump(mode="json"))
        self._purge_now_and_then()
        return bool(stored)

    def get(self, notification_id: str) -> Notification | None:
        found: Notification | None = self.store.get(COLLECTION, notification_id, Notification)
        return found

    def recent(self) -> list[Notification]:
        """Every message of the retention period, newest first (ties: higher id first)."""
        found: list[Notification] = self.store.list_since(
            COLLECTION, Notification, field="created_at", since=now() - self.retention
        )
        return found

    def for_user(self, user_id: str) -> list[Notification]:
        return [item for item in self.recent() if item.for_user(user_id)]

    def unread(self, user_id: str) -> int:
        return sum(1 for item in self.for_user(user_id) if item.read_at(user_id) is None)

    def mark_read(self, user_id: str, ids: list[str] | None = None) -> int:
        """Mark ``user_id``'s messages as read (``ids``: only these; unknown or foreign ids change nothing)."""
        wanted = set(ids) if ids else None
        marked = 0
        moment = now()
        with self.store.transaction():
            for item in self.for_user(user_id):
                if wanted is not None and item.id not in wanted:
                    continue
                if item.read_at(user_id) is not None or len(item.read) >= MAX_READERS:
                    continue
                self.store.lock(COLLECTION, item.id)
                current = self.get(item.id)
                if current is None or current.read_at(user_id) is not None:
                    continue
                current.read[user_id] = moment
                self.store.put_raw(COLLECTION, current.id, current.model_dump(mode="json"))
                marked += 1
        return marked

    def count_messages(self, run_id: str) -> int:
        return sum(1 for item in self.recent() if item.kind == "message" and item.run_id == run_id)

    def purge(self) -> int:
        """Delete the messages older than the retention period; returns how many."""
        cutoff = now() - self.retention
        removed = 0
        for key, item in self.store.items(COLLECTION, Notification):
            if item.created_at < cutoff and self.store.delete(COLLECTION, key):
                removed += 1
        return removed

    def _purge_now_and_then(self) -> None:
        """At most once a day per process: old messages go when new ones come."""
        with self._lock:
            moment = now()
            if self._purged is not None and moment - self._purged < timedelta(days=1):
                return
            self._purged = moment
        try:
            self.purge()
        except Exception:  # cleaning up is extra; the new message is stored
            logger.exception("Purging old notifications failed")


def _paused_events(run: Run) -> list[tuple[NotificationEvent, str, dict[str, str]]]:
    """The waits of a paused run: ``(event, record id, params)`` per waiting step or budget request."""
    found: list[tuple[NotificationEvent, str, dict[str, str]]] = []
    budget = run.budget.pending_request_id if run.budget is not None else None
    if budget:
        found.append(("budget_pending", budget, {}))
    for node_id, state in sorted(run.nodes.items()):
        if state.status == "awaiting_approval":
            # A step in a loop can wait again in a later round: each round is its own event.
            record = f"{run.id}:{node_id}:{len(state.earlier)}"
            found.append(("approval_pending", record, {"node": node_id[:80]}))
        elif state.status == "awaiting_budget" and not budget:
            found.append(("budget_pending", f"{run.id}:{node_id}:{len(state.earlier)}", {}))
    return found


def run_events(run: Run) -> list[tuple[NotificationEvent, str, dict[str, str]]]:
    """The notifiable events of a stored run in its current state: ``(event, record id, params)``."""
    if run.parent_run_id is not None or run.imported_at is not None:
        return []
    if run.status == "paused":
        return _paused_events(run)
    found: list[tuple[NotificationEvent, str, dict[str, str]]] = []
    if run.status == "failed":
        found.append(("run_failed", run.id, {"error_code": (run.error_code or "run.failed")[:80]}))
    if run.status in ("succeeded", "failed", "cancelled"):
        found.append(("run_finished", run.id, {"status": run.status}))
    if any(state.evaluation is not None and not state.evaluation.passed for state in run.nodes.values()):
        found.append(("quality_failed", run.id, {}))
    return found


_LEVELS: dict[str, Level] = {
    "run_failed": "error",
    "approval_pending": "warning",
    "budget_pending": "warning",
    "quality_failed": "warning",
}


class RunNotifier:
    """Writes the messages of runs into a :class:`LocalInbox` (an engine run listener, and the ``notify_user`` sink)."""

    def __init__(self, inbox: LocalInbox) -> None:
        self.inbox = inbox
        self._lock = threading.Lock()

    def __call__(self, run: Run) -> None:
        """Called when ``run`` ended or paused: notify its events that its workflow's rules name."""
        rules = run.workflow.notifications
        for event, record, params in run_events(run):
            if event not in rules.events:
                continue
            if event == "run_finished" and "run_failed" in rules.events and run.status == "failed":
                continue  # one message for one failed run
            key = notification_key(event, record)
            level: Level = _LEVELS.get(event, "success" if run.status == "succeeded" else "info")
            self.inbox.add(
                Notification(
                    id=notification_id(key),
                    key=key,
                    kind=event,
                    level=level,
                    params=params,
                    run_id=run.id,
                    workflow_id=run.workflow_id,
                    workflow_name=_clean(run.workflow_name, MAX_TITLE),
                    user_id=run.started_by if rules.recipients == "starter" and run.started_by else None,
                )
            )

    def message(self, run: Run, node_id: str | None, title: str, text: str, level: str) -> str:
        """A ``notify_user`` message of ``run``; refused (``ValueError``) after :data:`MAX_MESSAGES_PER_RUN`."""
        level = level if level in LEVELS else "info"
        title = _clean(title, MAX_TITLE)
        text = _clean(text, MAX_TEXT)
        if not title:
            raise ValueError("notify.empty_title")
        with self._lock:
            count = self.inbox.count_messages(run.id)
            if count >= MAX_MESSAGES_PER_RUN:
                raise ValueError("notify.limit")
            key = notification_key("message", f"{run.id}:{count + 1}")
            item = Notification(
                id=notification_id(key),
                key=key,
                kind="message",
                level=level,
                title=title,
                text=text,
                params={"node": node_id[:80]} if node_id else {},
                run_id=run.id,
                workflow_id=run.workflow_id,
                workflow_name=_clean(run.workflow_name, MAX_TITLE),
                user_id=run.started_by or None,
            )
            self.inbox.add(item)
        return item.id


@Tool
def notify_user(title: str, message: str, level: Literal["info", "success", "warning", "error"] = "info") -> str:
    """Leave a short internal message for the person who started this run, in Nodestro's inbox.

    Use it for something a person should notice later (a result that needs a look, a problem you could not solve).
    Nothing is sent outside Nodestro. At most three messages per run.

    Args:
        title: A short title, at most 120 characters.
        message: The message as plain text, at most 2000 characters (no HTML; links are shown as text).
        level: How important it is: info, success, warning or error.

    Returns:
        A confirmation with the message's id.
    """
    from .tool_spec import current_tool_context

    context = current_tool_context()
    sink = context.services.notify if context is not None else None
    if sink is None:
        raise ToolResultError("Error: internal messages are not available here (notify.unavailable)")
    if level not in ("info", "success", "warning", "error"):
        level = "info"
    try:
        message_id = sink(title, message, level)
    except ValueError as exc:
        code = str(exc)
        if code == "notify.limit":
            raise ToolResultError(
                f"Error: this run has left {MAX_MESSAGES_PER_RUN} messages already (notify.limit); answer without "
                "another one"
            ) from None
        raise ToolResultError("Error: the message needs a title (notify.empty_title)") from None
    return f"Message {message_id} left in the inbox."


#: The notification tools, as the tool library offers them.
NOTIFY_TOOLS: tuple[Tool, ...] = (notify_user,)


__all__ = [
    "COLLECTION",
    "DEFAULT_NOTIFY_EVENTS",
    "LEVELS",
    "MAX_MESSAGES_PER_RUN",
    "NOTIFY_EVENTS",
    "NOTIFY_TOOLS",
    "RETENTION",
    "LocalInbox",
    "Notification",
    "NotificationEvent",
    "NotificationRules",
    "RunNotifier",
    "local_inbox_enabled",
    "notification_id",
    "notification_key",
    "notify_user",
    "run_events",
]
