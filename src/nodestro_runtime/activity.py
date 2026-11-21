"""One activity log for every file, tool, workflow and agent action of a workspace (N-13).

Nodestro keeps several records of what happened, each for its own purpose: the approval audit trail
(:mod:`.tool_approvals`), the notes of started tool calls (:mod:`.tool_journal`), the MCP event log
(:mod:`.mcp.service`), the service connection events (:mod:`.connections`) and the runs themselves. They stay as they
are. The activity log adds one flat event per action, written at the same call sites, so a person can ask one place
"who did what, with which agent, to what, and how did it end" and export the answer.

What an event holds (:class:`ActivityEvent`): who (``actor``), the agent that acted (its name and step), the action,
the resource it concerned (a kind and an id), the outcome (``success``, ``failure`` or ``denied``, Axentra's audit
vocabulary) with an optional code (a problem code, a run or step status, a decision), the run or conversation it
belongs to and the id of the source record. It never holds contents: no tool arguments or their values, no prompts,
no outputs, no file contents, no tokens or secrets, and no free text of a source's ``details``. Codes and names are
checked against a narrow pattern; anything else is dropped rather than stored.

Resource identifiers are Nodestro ids and names (a run id, an input file id, a tool name, an MCP server id),
**never file paths or file names**. A path or a file name is not content, but it often names a person, a customer or
a project, and the log is exported and kept for its own retention period; the id still leads to the file's name for as
long as the file exists. Tool calls that take a path (the sandbox's file tools) only ever pass it as an argument, and
arguments are never stored.

Storage follows the other audit records: one document per event in the tenant's store (collection
``activity_log``, key ``<time>_<event id>``, so keys sort by time). Writing an event never fails the action it
describes: on PostgreSQL it is written in a savepoint, so a failed write rolls back only the event and never aborts
the caller's transaction; a failure is logged. Sources whose own record is already mirrored into the platform backend
(approval audit, MCP events, connection events; see :mod:`.platform_events`) are not mirrored a second time; every
other event is mirrored through the same sink, in the same savepoint as its document, as action
``nodestro.<action>``.

Retention (:func:`prune` with :func:`retention_days`): events older than :data:`RETENTION_ENV` days (default
:data:`DEFAULT_RETENTION_DAYS`; ``0`` keeps them without limit) are deleted. The desktop prunes when the workspace
opens; a server calls :func:`prune` from its housekeeping.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager
import csv
from datetime import datetime, timedelta
import io
import json
import logging
import os
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .platform_events import platform_events
from .schemas import new_id, now
from .storage import DocumentStore

logger = logging.getLogger(__name__)

COLLECTION = "activity_log"
#: Days an event is kept (a whole number; ``0``: no limit). Invalid values fall back to the default with a warning.
RETENTION_ENV = "NODESTRO_ACTIVITY_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 90

Source = Literal["tool_call", "tool_approval", "mcp", "connection", "workflow", "agent", "file"]
SOURCES: tuple[str, ...] = ("tool_call", "tool_approval", "mcp", "connection", "workflow", "agent", "file")
#: Sources whose own record is mirrored into the platform backend already: their activity events are not mirrored.
MIRRORED_BY_SOURCE: frozenset[str] = frozenset({"tool_approval", "mcp", "connection"})
#: Axentra's audit outcomes.
Outcome = Literal["success", "failure", "denied"]
ExportFormat = Literal["jsonl", "csv"]

#: The columns of an export, in order.
FIELDS: tuple[str, ...] = (
    "id",
    "at",
    "source",
    "action",
    "actor",
    "agent",
    "resource_type",
    "resource_id",
    "outcome",
    "code",
    "run_id",
    "node_id",
    "conversation_id",
    "source_event",
)

#: Identifiers, names and codes: printable, no line breaks, bounded. Anything else is not stored.
_TOKEN = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")


def _token(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if _TOKEN.match(text) else None


class ActivityEvent(BaseModel):
    """One action: who, with which agent, did what to which resource, and how it ended. Never contents or secrets."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: new_id("act"))
    at: datetime = Field(default_factory=now)
    source: Source
    #: ``<area>.<verb>``, e.g. ``tool.called``, ``workflow.finished``, ``file.uploaded``, ``mcp.server_added``.
    action: str
    #: Who: a principal id (``local`` on a desktop, a user id on a server), ``api:<key id>``, or ``system``.
    actor: str | None = None
    #: The agent that acted (its configured name), for tool calls and agent steps.
    agent: str | None = None
    #: What the action concerned: ``tool``, ``run``, ``workflow``, ``node``, ``input_file``, ``knowledge_file``,
    #: ``mcp_server``, ``mcp_tool``, ``service_connection``, ``tool_pre_grant``, ``folder_grant``.
    resource_type: str = ""
    #: The resource's Nodestro id or name (never a file path or file name).
    resource_id: str = ""
    outcome: Outcome = "success"
    #: A problem code, a run or step status, or a decision (``approved``); never free text.
    code: str | None = None
    run_id: str | None = None
    node_id: str | None = None
    conversation_id: str | None = None
    #: The id of the record this event was taken from (an approval audit event, an MCP or connection event).
    source_event: str | None = None

    @field_validator("actor", "agent", "run_id", "node_id", "conversation_id")
    @classmethod
    def _identifier(cls, value: str | None) -> str | None:
        return _token(value)

    @field_validator("resource_type", "resource_id")
    @classmethod
    def _resource(cls, value: str) -> str:
        return _token(value) or ""

    @field_validator("action", "source_event")
    @classmethod
    def _action(cls, value: str | None) -> str | None:
        if value is not None and not _CODE.match(value):
            raise ValueError("an action or record id is a short code")
        return value

    @field_validator("code")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        return value if value is not None and _CODE.match(value) else None

    @property
    def key(self) -> str:
        return f"{self.at.isoformat()}_{self.id}"


# ---- writing ------------------------------------------------------------------------------------------------------


def record(store: DocumentStore, event: ActivityEvent) -> ActivityEvent | None:
    """Store ``event`` (and mirror it, see the module docs); never raises: the action it describes goes on."""
    try:
        sink = None if event.source in MIRRORED_BY_SOURCE else platform_events(store)
        with _isolated(store):  # the mirror commits with the event (see .platform_events)
            store.put(COLLECTION, event.key, event)
            if sink is not None:
                sink.audit(str(getattr(store, "tenant_id", "")), **mirror_audit(event))
        return event
    except Exception:
        logger.exception("Could not store activity event %s", event.action)
        return None


def _isolated(store: DocumentStore) -> AbstractContextManager[Any]:
    """Where an event is written: a savepoint where the store has one (PostgreSQL), so a failed write rolls back only
    the event and never aborts the transaction of the action it describes; else the store's (reentrant) transaction.
    SQLite needs no savepoint: a failed statement there rolls back only itself, and the transaction goes on."""
    savepoint: Callable[[], AbstractContextManager[Any]] | None = getattr(store, "savepoint", None)
    return savepoint() if savepoint is not None else store.transaction()


def add(store: DocumentStore, source: Source, action: str, **fields: Any) -> ActivityEvent | None:
    """Build and :func:`record` one event; an event that cannot be built is logged and skipped."""
    try:
        event = ActivityEvent(source=source, action=action, **fields)
    except Exception:
        logger.exception("Could not build activity event %s", action)
        return None
    return record(store, event)


def mirror_audit(event: ActivityEvent) -> dict[str, Any]:
    """The arguments of :meth:`~.platform_events.PlatformEvents.audit` for ``event`` (also used by reconcile)."""
    details: dict[str, Any] = {"nodestro_event": event.id, "nodestro_actor": event.actor, "source": event.source}
    for name in ("agent", "code", "run_id", "node_id", "conversation_id"):
        value = getattr(event, name)
        if value is not None:
            details[name] = value
    return {
        "event_id": event.id,
        "at": event.at,
        "action": f"nodestro.{event.action}",
        "actor": event.actor,
        "target_type": event.resource_type,
        "target_id": event.resource_id,
        "outcome": event.outcome,
        "details": details,
    }


def outcome_of_status(status: str) -> Outcome:
    """A run's or step's final status as an outcome (``succeeded`` and ``skipped`` are a success)."""
    return "success" if status in ("succeeded", "skipped") else "failure"


# ---- reading and export -------------------------------------------------------------------------------------------


def query(
    store: DocumentStore,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    sources: Iterable[str] | None = None,
    actions: Iterable[str] | None = None,
    actor: str | None = None,
    agent: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    outcome: Outcome | None = None,
    run_id: str | None = None,
    conversation_id: str | None = None,
    before: str | None = None,
    limit: int | None = 200,
) -> list[ActivityEvent]:
    """Matching events, newest first. ``until`` is exclusive; ``before`` is the :attr:`~ActivityEvent.key` of the
    last event of the previous page (keyset paging); ``limit=None`` returns all (an export)."""
    if since is not None:
        found = store.list_since(COLLECTION, ActivityEvent, field="at", since=since)
    else:
        found = store.list(COLLECTION, ActivityEvent)
    wanted_sources = set(sources) if sources is not None else None
    wanted_actions = set(actions) if actions is not None else None
    exact: dict[str, str | None] = {
        "actor": actor,
        "agent": agent,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "outcome": outcome,
        "run_id": run_id,
        "conversation_id": conversation_id,
    }
    exact = {name: value for name, value in exact.items() if value is not None}

    def keep(event: ActivityEvent) -> bool:
        if until is not None and event.at >= until:
            return False
        if before is not None and event.key >= before:
            return False
        if wanted_sources is not None and event.source not in wanted_sources:
            return False
        if wanted_actions is not None and event.action not in wanted_actions:
            return False
        return all(getattr(event, name) == value for name, value in exact.items())

    selected = sorted((event for event in found if keep(event)), key=lambda event: event.key, reverse=True)
    return selected if limit is None else selected[:limit]


def export(events: Iterable[ActivityEvent], fmt: ExportFormat) -> Iterator[str]:
    """``events`` as JSON Lines (one object per line) or CSV (a header, then one row per event), line by line.

    CSV cells that a spreadsheet would read as a formula (``=``, ``+``, ``-``, ``@`` first) get a leading ``'``."""
    if fmt == "jsonl":
        for event in events:
            yield json.dumps(_row(event), ensure_ascii=False) + "\n"
        return
    if fmt != "csv":
        raise ValueError(f"unknown export format {fmt!r}")
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")

    def line(values: Iterable[str]) -> str:
        buffer.seek(0)
        buffer.truncate()
        writer.writerow(list(values))
        return buffer.getvalue()

    yield line(FIELDS)
    for event in events:
        row = _row(event)
        yield line(_cell(row[name]) for name in FIELDS)


def _row(event: ActivityEvent) -> dict[str, Any]:
    data = event.model_dump(mode="json")
    return {name: data[name] for name in FIELDS}


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


# ---- retention ----------------------------------------------------------------------------------------------------


def retention_days(environ: Mapping[str, str] | None = None) -> int | None:
    """Days to keep events (:data:`RETENTION_ENV`); ``None``: no limit."""
    text = (os.environ if environ is None else environ).get(RETENTION_ENV, "").strip()
    if not text:
        return DEFAULT_RETENTION_DAYS
    try:
        days = int(text)
    except ValueError:
        days = -1
    if days < 0:
        logger.warning(
            "%s must be a whole number of days (0: no limit); using %s", RETENTION_ENV, DEFAULT_RETENTION_DAYS
        )
        return DEFAULT_RETENTION_DAYS
    return days or None


def prune(store: DocumentStore, *, days: int | None, at: datetime | None = None) -> int:
    """Delete events older than ``days`` (``None``: keep all; usually :func:`retention_days`); returns how many were
    deleted."""
    if days is None:
        return 0
    cutoff = ((at or now()) - timedelta(days=days)).isoformat()
    removed = 0
    with store.transaction():
        for key, _ in store.items(COLLECTION, ActivityEvent):
            if key < cutoff:
                removed += store.delete(COLLECTION, key)
    return removed


__all__ = [
    "COLLECTION",
    "DEFAULT_RETENTION_DAYS",
    "FIELDS",
    "MIRRORED_BY_SOURCE",
    "RETENTION_ENV",
    "SOURCES",
    "ActivityEvent",
    "ExportFormat",
    "Outcome",
    "Source",
    "add",
    "export",
    "mirror_audit",
    "outcome_of_status",
    "prune",
    "query",
    "record",
    "retention_days",
]
