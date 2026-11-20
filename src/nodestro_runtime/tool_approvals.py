"""Waiting for a person's decision about one tool call (S-2, decision D10; worker concept in nodestro-studio
``docs/evolution/s2-worker-concept.md``).

A request is a document of its own in the tenant's store (collection ``tool_approvals``, key ``<run id>:<request
id>``),
never part of the run document: runs are saved whole from memory, and such a save must not lose or overwrite a
decision. Every change of a request's status is a compare-and-set (:meth:`DocumentStore.replace_if`) from
``pending`` to ``approved``, ``rejected``, ``expired`` or ``cancelled``; a decision is also checked against the
deadline in that same atomic step, so a decision and an expiry that race have exactly one winner, also across
processes and database connections.

The run that asks waits in the agent's thread (:meth:`ToolApprovals.wait`), reading the stored request at a short
interval: the decision reaches it through the database, whichever process made it. Every question gets a new
request id (call ids of some providers repeat), and the request records the node, its loop iteration and the call
id. It is bound to the exact arguments and the tool version or MCP definition hash (:func:`arguments_hash`); it is
never matched to another call, and a request that is not ``pending`` any more never becomes ``pending`` again.

With :data:`CHECKPOINTS_ENV` switched on as well (M6b, default off), the run does not wait in a thread: the agent's
turn stops at the call (an orkexa turn checkpoint, :mod:`nodestro_runtime.tool_checkpoints`), the run pauses and
frees its worker, and the decision continues it. Such a request is ``checkpointed``. On resume only this request
can answer the call; an approved call runs at most once (``use`` goes from ``open`` to ``executed`` in one
compare-and-set), and only within :data:`EXECUTION_WINDOW` after the decision.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
import threading
import time
from typing import Any, Literal, TypeVar, cast

from pydantic import BaseModel, Field

from . import activity
from .mcp_access import UNATTENDED_TRIGGERS
from .platform_events import platform_events
from .problems import NodestroError
from .schemas import new_id, now
from .storage import DocumentStore
from .tool_policy import (
    MAX_UNATTENDED_GRANT,
    UNKNOWN,
    AccessAction,
    AccessContext,
    AccessMode,
    AccessResource,
    AccessScope,
    CallContext,
    Deny,
    DenyDuration,
    Grant,
    GrantDuration,
    PreGrantScope,
    ToolCapability,
    ToolFacts,
    ToolPreGrant,
    folder_parts,
)

COLLECTION = "tool_approvals"
#: The longest a call waits for a decision on a server (D10d; longer waits need an agent checkpoint).
DEFAULT_WAIT = timedelta(minutes=15)
MAX_WAIT = timedelta(minutes=15)
POLL_SECONDS = 1.0
#: How long after its approval a checkpointed call may still run; later it is stale and never runs (M6b).
EXECUTION_WINDOW = timedelta(minutes=15)

ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "cancelled"]
#: ``closed_because`` of the requests of a run that was stopped elsewhere.
INTERRUPTED = "run.interrupted"
#: ``closed_because`` of the requests of paused runs found while the checkpoint switch is off (M6b).
SWITCH_OFF = "switch_off"
#: ``closed_because`` of a pending request whose tool was classified differently while it waited (re-review R-1): the
#: approval would no longer cover the call, so the request is closed and the call is refused (``tool.approval_stale``).
RECLASSIFIED = "tool.reclassified"
#: What became of an approved checkpointed call: ``open`` (not run yet), ``executed`` (marked right before it ran),
#: ``stale`` (too late, or the call or the rules changed), ``closed`` (switch off, cancel, the run is gone).
CallUse = Literal["open", "executed", "stale", "closed"]
FINAL: frozenset[str] = frozenset({"approved", "rejected", "expired", "cancelled"})


def arguments_hash(tool: str, arguments: dict[str, Any], *, version: int | None, definition_hash: str | None) -> str:
    """What an approval binds to: the tool, its version or MCP definition, and the exact arguments."""
    payload = {"tool": tool, "version": version, "definition_hash": definition_hash, "input": arguments}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def request_key(run_id: str, request_id: str) -> str:
    return f"{run_id}:{request_id}"


def reclassified(asked: ToolCapability, current: ToolCapability) -> bool:
    """Whether a tool's classification changed in a way that matters for an approval (re-review R-1): what it may do
    (``access``) or how far it reaches (``reach``), in either direction. Who stated it (``source``) does not matter."""
    return asked.access != current.access or asked.reach != current.reach


# ---- the call an approval or an allow verdict covers ----------------------------------------------------------


@dataclass(frozen=True)
class CallBinding:
    """What the gate let a call run as (re-review R-1): the tool, its classification and, for MCP tools, the approved
    definition hash, at the moment of the gate's last check.

    The run engine binds it in the tool call's own context right before the call runs; an MCP tool checks it again
    against the stored tool right before the request is sent (:meth:`~nodestro_runtime.mcp.service.McpService.call`),
    so a reclassification between the gate and the call refuses the call instead of running it under the old
    decision."""

    tool: str
    capability: ToolCapability
    definition_hash: str | None = None


_BINDING: ContextVar[CallBinding | None] = ContextVar("nodestro_tool_call_binding", default=None)


def bind_call(binding: CallBinding | None) -> None:
    """Bind (or clear) what the current tool call may run as; set for every call before it runs."""
    _BINDING.set(binding)


def bound_call(tool: str) -> CallBinding | None:
    """The binding of the current call if it is about ``tool``; ``None`` otherwise."""
    found = _BINDING.get()
    return found if found is not None and found.tool == tool else None


# ---- the audit trail ------------------------------------------------------------------------------------------

AUDIT = "tool_approval_audit"
#: The newest audit events kept per tenant (older ones are dropped when the trail is read).
MAX_AUDIT = 5000

AuditKind = Literal[
    "decided",
    "decision_refused",
    "pre_granted",
    "pre_grant_revoked",
    "legacy_stamped",
    # M6b: a turn stopped at the call (checkpoint), the paused run went on, the approved call ran, an approval
    # could not be used any more, a call whose result was lost in a crash, requests closed because the switch is off.
    "suspended",
    "resumed",
    "executed",
    "stale",
    "result_lost",
    "switch_off",
    # R-4 (ADR-0006): a person adopted AI-generated code as their own, a refused adoption, an AI change ended one.
    "tool_adopted",
    "tool_adoption_refused",
    "tool_adoption_revoked",
    # N-06: a person stored or revoked a grant or a deny of the access model v2.
    "access_granted",
    "access_grant_revoked",
    "access_denied",
    "access_deny_revoked",
]


class ToolApprovalEvent(BaseModel):
    """One entry of a tenant's approval audit trail: who decided, granted or revoked what, when; and every refused
    decision with its problem code. Never holds the call's arguments (only their hash)."""

    id: str = Field(default_factory=lambda: new_id("tae"))
    at: datetime = Field(default_factory=now)
    kind: AuditKind
    actor: str
    tool: str | None = None
    run_id: str | None = None
    request_id: str | None = None
    pre_grant_id: str | None = None
    #: ``approved`` / ``rejected`` for decisions; the problem code for refused ones.
    outcome: str | None = None
    arguments_hash: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


def record_audit(store: DocumentStore, event: ToolApprovalEvent) -> ToolApprovalEvent:
    """Store ``event``; with a platform sink (:mod:`.platform_events`) its mirror commits in the same transaction.
    The activity log (:mod:`.activity`) gets one event for it."""
    sink = platform_events(store)
    if sink is None:
        store.put(AUDIT, f"{event.at.isoformat()}_{event.id}", event)
        _activity(store, event)
        return event
    with store.transaction():
        store.put(AUDIT, f"{event.at.isoformat()}_{event.id}", event)
        sink.audit(
            str(getattr(store, "tenant_id", "")),
            event_id=event.id,
            at=event.at,
            action=f"nodestro.tool_approval.{event.kind}",
            actor=event.actor,
            target_type=audit_target(event)[0],
            target_id=audit_target(event)[1],
            outcome="denied" if event.kind in REFUSALS else "success",
            details=audit_details(event),
        )
    _activity(store, event)
    return event


def _activity(store: DocumentStore, event: ToolApprovalEvent) -> None:
    """The activity event of an audit event: ids, the tool, the result; never the arguments or their hash."""
    target_type, target_id = audit_target(event)
    if event.tool and target_type == "run":
        target_type, target_id = "tool", event.tool
    activity.add(
        store,
        "tool_approval",
        f"tool_approval.{event.kind}",
        at=event.at,
        actor=event.actor,
        resource_type=target_type,
        resource_id=target_id,
        outcome="denied" if event.kind in REFUSALS else "success",
        code=event.outcome,
        run_id=event.run_id,
        source_event=event.id,
    )


#: Audit kinds of a refused action (mirrored with the outcome ``denied``).
REFUSALS = frozenset({"decision_refused", "tool_adoption_refused"})


def audit_target(event: ToolApprovalEvent) -> tuple[str, str]:
    """What the platform mirror names as the event's target: the run, the tool of an adoption or the pre-grant."""
    if event.run_id:
        return "run", event.run_id
    if event.kind.startswith("tool_adopt"):
        return "tool", event.tool or ""
    if event.kind.startswith("access_"):
        kind = "access_deny" if event.kind.startswith("access_deny") else "access_grant"
        return kind, str(event.details.get("record_id", ""))
    return "tool_pre_grant", event.pre_grant_id or ""


def audit_details(event: ToolApprovalEvent) -> dict[str, Any]:
    """What the platform mirror of ``event`` keeps: ids, the tool, the result and the arguments' hash (never the
    arguments themselves); ``details`` are Nodestro's own, already free of secrets and arguments."""
    found: dict[str, Any] = {"nodestro_event": event.id, "nodestro_actor": event.actor}
    for name in ("tool", "run_id", "request_id", "pre_grant_id", "outcome", "arguments_hash"):
        value = getattr(event, name)
        if value is not None:
            found[name] = value
    if event.details:
        found["details"] = event.details
    return found


def audit_events(store: DocumentStore, *, limit: int = 200) -> list[ToolApprovalEvent]:
    """The newest audit events first."""
    items = store.items(AUDIT, ToolApprovalEvent)
    items.sort(key=lambda pair: pair[0], reverse=True)
    for key, _ in items[MAX_AUDIT:]:
        store.delete(AUDIT, key)
    return [event for _, event in items[:limit]]


class ToolApprovalRequest(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tap"))
    run_id: str
    node_id: str
    #: The node's loop iterations (``NodeRun.iteration``; Adaptive Orchestration loops run a node again).
    iteration: list[int] = Field(default_factory=list)
    call_id: str
    tool: str
    #: The arguments exactly as they will be sent (shown to the person; never written to logs).
    input: dict[str, Any] = Field(default_factory=dict)
    capability: ToolCapability
    version: int | None = None
    definition_hash: str | None = None
    #: :func:`arguments_hash` of the above; a decision must name it.
    arguments_hash: str
    status: ApprovalStatus = "pending"
    requested_at: datetime = Field(default_factory=now)
    deadline: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None
    #: Why a request was closed without a decision: ``deadline``, ``cancelled`` or ``run.interrupted`` (the run was
    #: stopped elsewhere, e.g. by the reaper: the waiting run must not go on either).
    closed_because: str | None = None
    #: The run paused at this call (an agent checkpoint) instead of waiting in a thread (M6b).
    checkpointed: bool = False
    #: Whether an approved checkpointed call ran (:data:`CallUse`); changed only by compare-and-set from ``open``.
    use: CallUse = "open"
    executed_at: datetime | None = None

    @property
    def key(self) -> str:
        return request_key(self.run_id, self.id)


class ToolApprovals:
    """Store-backed tool approval requests of one tenant."""

    def __init__(
        self,
        store: DocumentStore,
        *,
        wait: timedelta = DEFAULT_WAIT,
        poll_seconds: float = POLL_SECONDS,
        clock: Callable[[], datetime] = now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not timedelta(0) < wait <= MAX_WAIT:
            raise ValueError(f"the wait for a tool approval must be more than 0 and at most {MAX_WAIT}")
        self.store = store
        self.wait_for = wait
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.sleep = sleep

    # ---- the asking side ------------------------------------------------------------------------------------------

    def request(
        self,
        *,
        run_id: str,
        node_id: str,
        call_id: str,
        tool: str,
        iteration: Sequence[int] = (),
        arguments: dict[str, Any],
        capability: ToolCapability,
        version: int | None = None,
        definition_hash: str | None = None,
        checkpointed: bool = False,
    ) -> ToolApprovalRequest:
        """Store a new pending request with a new id (never replacing another one)."""
        asked = self.clock()
        found = ToolApprovalRequest(
            run_id=run_id,
            node_id=node_id,
            iteration=list(iteration),
            call_id=call_id,
            tool=tool,
            input=arguments,
            capability=capability,
            version=version,
            definition_hash=definition_hash,
            arguments_hash=arguments_hash(tool, arguments, version=version, definition_hash=definition_hash),
            requested_at=asked,
            deadline=asked + self.wait_for,
            checkpointed=checkpointed,
        )
        self.store.put(COLLECTION, found.key, found)
        return found

    def wait(self, run_id: str, request_id: str, *, check: Callable[[], None] | None = None) -> ToolApprovalRequest:
        """Wait until the request is decided or its deadline passes; return the final stored request.

        ``check`` is called between reads and may raise (a cancelled run): the request then becomes ``cancelled``
        (unless a decision won the race) and the exception propagates."""
        while True:
            found = self.get(run_id, request_id)
            if found is None:
                raise NodestroError("tool.approval_not_found", "The approval request is gone", request_id=request_id)
            if found.status != "pending":
                return found
            if self.clock() >= found.deadline:
                self.close(run_id, request_id, "expired", because="deadline")
                continue
            if check is not None:
                try:
                    check()
                except BaseException:
                    self.close(run_id, request_id, "cancelled", because="cancelled")
                    raise
            self.sleep(self.poll_seconds)

    # ---- the deciding side ----------------------------------------------------------------------------------------

    def get(self, run_id: str, request_id: str) -> ToolApprovalRequest | None:
        return self.store.get(COLLECTION, request_key(run_id, request_id), ToolApprovalRequest)

    def pending(self, run_id: str) -> list[ToolApprovalRequest]:
        return [item for item in self.of_run(run_id) if item.status == "pending"]

    def open(self) -> list[ToolApprovalRequest]:
        """Every pending request of the tenant, oldest first (the approval inbox; also after a restart)."""
        found = [item for _, item in self.store.items(COLLECTION, ToolApprovalRequest) if item.status == "pending"]
        return sorted(found, key=lambda item: (item.requested_at, item.id))

    def of_run(self, run_id: str) -> list[ToolApprovalRequest]:
        """The requests of a run, oldest first."""
        found = self.store.list_prefix(COLLECTION, f"{run_id}:", ToolApprovalRequest)
        return sorted(found, key=lambda item: (item.requested_at, item.id))

    def decide(
        self,
        run_id: str,
        request_id: str,
        *,
        approved: bool,
        by: str,
        arguments: str,
        comment: str | None = None,
        current: ToolCapability | None = None,
    ) -> ToolApprovalRequest:
        """Approve or reject a pending request. ``arguments`` is the :func:`arguments_hash` the person was shown: a
        decision about other arguments or another tool definition is refused (``tool.approval_stale``), and so is a
        decision after the deadline (``tool.approval_expired``) or about a request that was decided already
        (``tool.approval_decided``). Every decision and every refused one is written to the audit trail.

        ``current`` is the tool's classification now. If it differs from the one the request was asked with
        (:func:`reclassified`), an approval is refused (``tool.approval_stale``) and the request is closed
        (:data:`RECLASSIFIED`): the person was shown another classification (re-review R-1). Rejecting stays
        possible."""
        try:
            if approved and current is not None:
                self._refuse_reclassified(run_id, request_id, current)
            decided = self._decide(run_id, request_id, approved=approved, by=by, arguments=arguments, comment=comment)
        except NodestroError as problem:
            self._audit("decision_refused", by, run_id, request_id, arguments, outcome=problem.code)
            raise
        self._audit("decided", by, run_id, request_id, arguments, outcome=decided.status, tool=decided.tool)
        return decided

    def _refuse_reclassified(self, run_id: str, request_id: str, current: ToolCapability) -> None:
        found = self.get(run_id, request_id)
        if found is None or found.status != "pending" or not reclassified(found.capability, current):
            return
        if self.close(run_id, request_id, "expired", because=RECLASSIFIED):
            self.audit("stale", found, outcome="reclassified", access=current.access)
        raise NodestroError(
            "tool.approval_stale",
            "The tool was classified differently since this call was shown; it is not made",
            request_id=request_id,
        )

    def invalidate_tool(self, tool: str, current: ToolCapability) -> int:
        """Invalidate what a reclassification of ``tool`` makes stale (re-review R-1): pending requests asked with
        another classification are closed (:data:`RECLASSIFIED`), and approved checkpointed calls that did not run yet
        are marked ``stale``. A call waiting in a thread refuses itself (the engine compares the classification before
        the call runs). Returns how many requests changed; every change is audited."""
        changed = 0
        for _key, item in self.store.items(COLLECTION, ToolApprovalRequest):
            if item.tool != tool or not reclassified(item.capability, current):
                continue
            if item.status == "pending":
                if self.close(item.run_id, item.id, "expired", because=RECLASSIFIED):
                    self.audit("stale", item, outcome="reclassified", access=current.access)
                    changed += 1
            elif (
                item.status == "approved"
                and item.checkpointed
                and item.use == "open"
                and self.use(item, "stale") is not None
            ):
                self.audit("stale", item, outcome="reclassified", access=current.access)
                changed += 1
        return changed

    def _audit(
        self,
        kind: AuditKind,
        actor: str,
        run_id: str,
        request_id: str,
        arguments: str,
        *,
        outcome: str,
        tool: str | None = None,
    ) -> None:
        if tool is None:
            found = self.get(run_id, request_id)
            tool = found.tool if found is not None else None
        event = ToolApprovalEvent(
            at=self.clock(),
            kind=kind,
            actor=actor,
            tool=tool,
            run_id=run_id,
            request_id=request_id,
            outcome=outcome,
            arguments_hash=arguments,
        )
        record_audit(self.store, event)

    def _decide(
        self,
        run_id: str,
        request_id: str,
        *,
        approved: bool,
        by: str,
        arguments: str,
        comment: str | None,
    ) -> ToolApprovalRequest:
        found = self.get(run_id, request_id)
        if found is None:
            raise NodestroError("tool.approval_not_found", "There is no such approval request", request_id=request_id)
        if found.arguments_hash != arguments:
            raise NodestroError(
                "tool.approval_stale", "The call changed since it was shown; decide again", request_id=request_id
            )
        self._refuse_final(found)
        decided = found.model_copy(
            update={
                "status": "approved" if approved else "rejected",
                "decided_by": by,
                "decided_at": self.clock(),
                "comment": comment,
            }
        )
        if self.store.replace_if(
            COLLECTION, found.key, decided, field="status", expected="pending", live_until="deadline"
        ):
            return decided
        # Lost the race: either somebody decided first, or the deadline passed (by the store's clock).
        current = self.get(run_id, request_id)
        if current is not None and current.status == "pending":
            self.close(run_id, request_id, "expired", because="deadline")
            current = self.get(run_id, request_id)
        assert current is not None
        self._refuse_final(current)
        raise AssertionError("unreachable: the request is final")  # pragma: no cover

    def close(
        self, run_id: str, request_id: str, status: Literal["expired", "cancelled"], *, because: str | None = None
    ) -> bool:
        """Mark a pending request ``expired`` or ``cancelled``; ``False`` when it was not pending any more."""
        found = self.get(run_id, request_id)
        if found is None or found.status != "pending":
            return False
        closed = found.model_copy(update={"status": status, "decided_at": self.clock(), "closed_because": because})
        return self.store.replace_if(COLLECTION, found.key, closed, field="status", expected="pending")

    def use(self, found: ToolApprovalRequest, use: CallUse) -> ToolApprovalRequest | None:
        """Mark what became of an approved checkpointed call, once: a compare-and-set from ``open``. ``None`` when
        another process marked it first (the call must then not run here)."""
        marked = found.model_copy(update={"use": use, "executed_at": self.clock() if use == "executed" else None})
        if self.store.replace_if(COLLECTION, found.key, marked, field="use", expected="open"):
            return marked
        return None

    def matching(
        self, run_id: str, *, node_id: str, iteration: Sequence[int], call_id: str
    ) -> ToolApprovalRequest | None:
        """The newest checkpointed request of exactly this call (run, node, loop iteration, call id); ``None``
        when there is none. A request of another iteration or turn never matches."""
        found = [
            item
            for item in self.of_run(run_id)
            if item.checkpointed
            and item.node_id == node_id
            and item.iteration == list(iteration)
            and item.call_id == call_id
        ]
        return found[-1] if found else None

    def close_checkpointed(self, run_id: str, *, because: str) -> int:
        """Close what a paused run's checkpoint still holds: pending requests become ``cancelled`` and approved
        calls that did not run are marked ``closed`` (they never run). Returns how many changed."""
        changed = 0
        for item in self.of_run(run_id):
            if not item.checkpointed:
                continue
            if item.status == "pending":
                changed += self.close(run_id, item.id, "cancelled", because=because)
            elif item.status == "approved" and item.use == "open":
                closed = item.model_copy(update={"use": "closed", "closed_because": because})
                changed += self.store.replace_if(COLLECTION, item.key, closed, field="use", expected="open")
        return changed

    def overdue(self) -> list[ToolApprovalRequest]:
        """Pending checkpointed requests whose deadline passed (the deadline sweep closes them)."""
        at = self.clock()
        return [item for item in self.open() if item.checkpointed and item.deadline <= at]

    def audit(
        self,
        kind: AuditKind,
        found: ToolApprovalRequest,
        *,
        actor: str = "nodestro",
        outcome: str | None = None,
        **details: Any,
    ) -> None:
        """An audit event about one request: never its arguments, only their hash."""
        record_audit(
            self.store,
            ToolApprovalEvent(
                at=self.clock(),
                kind=kind,
                actor=actor,
                tool=found.tool,
                run_id=found.run_id,
                request_id=found.id,
                outcome=outcome,
                arguments_hash=found.arguments_hash,
                details=details,
            ),
        )

    def expire_run(self, run_id: str) -> int:
        """Expire every pending request of a run (the run stopped: interrupted, lost lease, crash, restart)."""
        return sum(self.close(run_id, item.id, "expired", because=INTERRUPTED) for item in self.pending(run_id))

    @staticmethod
    def _refuse_final(found: ToolApprovalRequest) -> None:
        if found.status == "expired" and found.closed_because == RECLASSIFIED:
            raise NodestroError(
                "tool.approval_stale",
                "The tool was classified differently since this call was shown; it is not made",
                request_id=found.id,
            )
        if found.status == "expired":
            raise NodestroError("tool.approval_expired", "The approval request expired", request_id=found.id)
        if found.status != "pending":
            raise NodestroError(
                "tool.approval_decided",
                f"The approval request is {found.status} already",
                request_id=found.id,
                status=found.status,
            )


# ---- the capacity for waiting runs and the switch ------------------------------------------------------------

#: Switches per-call approvals on (``1``). Off by default: activating them is a separate, explicit step after the
#: security acceptance (D10); while off, runs behave exactly as before S-2.
ENABLE_ENV = "NODESTRO_TOOL_APPROVALS"
#: Switches the access model v2 on (``1``; N-06, default off): stored grants and denies and the global safety limits
#: take part in every tool call's decision. Only effective together with :data:`ENABLE_ENV`; activating it needs the
#: owner's gate G-2. While off, the gate decides exactly as before.
ACCESS_ENV = "NODESTRO_ACCESS_GRANTS"
#: Pauses runs at a call that waits for a decision instead of waiting in a thread (``1``; M6b, default off). Only
#: effective together with :data:`ENABLE_ENV`; activation is a separate decision like the approvals themselves.
CHECKPOINTS_ENV = "NODESTRO_TOOL_APPROVAL_CHECKPOINTS"
#: How many runs of one tenant may be paused at a tool call at the same time (``NODESTRO_TOOL_APPROVAL_MAX_PAUSED``).
DEFAULT_MAX_PAUSED = 20
MAX_PAUSED_ENV = "NODESTRO_TOOL_APPROVAL_MAX_PAUSED"
#: How many runs of one process may wait for an approval at the same time (desktop default 3, D10 concept §2.4).
DESKTOP_MAX_WAITING = 3


def approvals_enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "").strip() == "1"


def access_grants_enabled() -> bool:
    """Whether the access model v2 takes part in the gate (both switches on)."""
    return approvals_enabled() and os.environ.get(ACCESS_ENV, "").strip() == "1"


def checkpoints_enabled() -> bool:
    """Whether calls that wait for a decision pause their run (both switches on)."""
    return approvals_enabled() and os.environ.get(CHECKPOINTS_ENV, "").strip() == "1"


def max_paused() -> int:
    """The per-tenant cap on runs paused at a tool call (:data:`DEFAULT_MAX_PAUSED` unless configured)."""
    try:
        return max(0, int(os.environ.get(MAX_PAUSED_ENV, "") or DEFAULT_MAX_PAUSED))
    except ValueError:
        return DEFAULT_MAX_PAUSED


class WaitingSlots:
    """How many runs may wait for a tool approval at the same time; a run that cannot wait is denied at once
    (``tool.approval_capacity``), never queued. Counted per run: parallel calls of one run share its slot.

    The desktop uses one per process; a server worker (:class:`~nodestro_runtime.jobs.RunWorker`) installs its own
    into every tenant's gate, so the limit holds per worker across tenants. ``0`` lets no run wait.
    """

    def __init__(self, max_waiting: int = DESKTOP_MAX_WAITING) -> None:
        if max_waiting < 0:
            raise ValueError("max_waiting must not be negative")
        self.max_waiting = max_waiting
        self._waiting: dict[str, int] = {}
        self._lock = threading.Lock()

    def enter(self, run_id: str) -> bool:
        with self._lock:
            if run_id in self._waiting:
                self._waiting[run_id] += 1
                return True
            if len(self._waiting) >= self.max_waiting:
                return False
            self._waiting[run_id] = 1
            return True

    def leave(self, run_id: str) -> None:
        with self._lock:
            left = self._waiting.get(run_id, 0) - 1
            if left > 0:
                self._waiting[run_id] = left
            else:
                self._waiting.pop(run_id, None)

    @property
    def waiting(self) -> int:
        with self._lock:
            return len(self._waiting)


class ToolGate:
    """Everything the engine needs to apply the per-call rules in one tenant (set on ``RunManager.tool_gate``)."""

    def __init__(
        self,
        approvals: ToolApprovals,
        *,
        slots: WaitingSlots | None = None,
        pre_grants: Callable[[PreGrantScope, str], Sequence[ToolPreGrant]] = lambda _scope, _ref: (),
        mcp_unattended: Callable[[], bool] = lambda: False,
        checkpoints: bool = False,
        max_paused: int = DEFAULT_MAX_PAUSED,
        access: Callable[[], tuple[Sequence[Grant], Sequence[Deny]]] | None = None,
        offline: Callable[[], bool] = lambda: False,
        protected_folders: Sequence[str] = (),
    ) -> None:
        self.approvals = approvals
        #: Pause the run at a call that waits (M6b) instead of waiting in the agent's thread.
        self.checkpoints = checkpoints
        #: At most this many runs of the tenant paused at a tool call; above it a call is denied at once.
        self.max_paused = max_paused
        self.slots = slots or WaitingSlots()
        #: The pre-grants stored for one unattended scope (schedule, batch, test series); none until they are stored.
        self.pre_grants = pre_grants
        #: ADR-0007 D-7 (read-only MCP tools unattended), only after the MCP security acceptance.
        self.mcp_unattended = mcp_unattended
        #: N-06: the stored grants and denies (:meth:`AccessRules.current`); ``None`` while the switch is off.
        self.access = access
        #: The strict offline setting (a safety limit of the access model v2).
        self.offline = offline
        #: Nodestro's own data folder: no folder grant opens it.
        self.protected_folders = tuple(protected_folders)

    def call_context(
        self,
        *,
        run_id: str,
        node_id: str,
        trigger: str,
        trigger_ref: str | None,
        agent: str | None = None,
        conversation_id: str | None = None,
    ) -> CallContext:
        """The situation of a call in a run started by ``trigger`` (the root run's): the one place that says
        whether a person can answer and which pre-grants count. The engine's gate and the warning before an
        unattended start (:mod:`nodestro_runtime.tool_preflight`) both use it, so they cannot disagree."""
        scope: tuple[PreGrantScope, str] | None = None
        if trigger in ("schedule", "batch", "benchmark") and trigger_ref:
            scope = (cast(PreGrantScope, trigger), trigger_ref)
        return CallContext(
            run_id=run_id,
            node_id=node_id,
            trigger=trigger,
            attended=trigger not in UNATTENDED_TRIGGERS,
            scope=scope,
            pre_grants=tuple(self.pre_grants(*scope)) if scope is not None else (),
            mcp_unattended=self.mcp_unattended(),
            access=self._access(agent, conversation_id),
        )

    def _access(self, agent: str | None, conversation_id: str | None) -> AccessContext | None:
        """Read at every call, so a revoked grant or a new deny applies at the next call."""
        if self.access is None:
            return None
        grants, denies = self.access()
        return AccessContext(
            grants=tuple(grants),
            denies=tuple(denies),
            agent=agent,
            conversation_id=conversation_id,
            offline=self.offline(),
            protected_folders=self.protected_folders,
        )


def tool_facts(tools: Any, name: str) -> tuple[ToolCapability, int | None, str, str | None]:
    """Capability, version, kind and MCP definition hash of the tool ``name`` in a tool registry; a tool that is
    not there counts as an unknown own tool (:data:`~nodestro_runtime.tool_policy.UNKNOWN`)."""
    try:
        info = tools.get(name)
    except KeyError:
        return UNKNOWN, None, "custom", None
    return info.capability, info.version, info.kind, info.definition_hash


# ---- pre-grants (D10c) ------------------------------------------------------------------------------------------

PRE_GRANTS = "tool_pre_grants"


class ToolPreGrants:
    """A person's stored advance approvals for unattended scopes (schedule, batch, test series) of one tenant.

    Narrow (one tool, one version, one scope), traceable (who, when), revocable (checked at every call, so a
    revocation applies at the next call) and enforced in the runtime (:func:`~nodestro_runtime.tool_policy.decide`).
    Refused for ``destructive`` and MCP tools and for tools without a version."""

    def __init__(self, store: DocumentStore, *, clock: Callable[[], datetime] = now) -> None:
        self.store = store
        self.clock = clock

    def grant(
        self,
        *,
        tool: str,
        kind: str,
        capability: ToolCapability,
        version: int | None,
        scope: PreGrantScope,
        scope_id: str,
        by: str,
        expires_at: datetime | None = None,
    ) -> ToolPreGrant:
        if kind == "mcp":
            raise NodestroError("tool.pre_grant_mcp", "MCP tools cannot be approved in advance", tool=tool)
        if capability.access == "destructive":
            raise NodestroError(
                "tool.pre_grant_destructive", "Destructive tools cannot be approved in advance", tool=tool
            )
        if version is None:
            raise NodestroError("tool.pre_grant_version", "Only a tool version can be approved in advance", tool=tool)
        at = self.clock()
        if expires_at is not None and expires_at <= at:
            raise NodestroError("tool.pre_grant_expiry", "The expiry lies in the past", tool=tool)
        found = ToolPreGrant(
            id=new_id("tpg"),
            tool=tool,
            version=version,
            scope=scope,
            scope_id=scope_id,
            granted_by=by,
            granted_at=at,
            expires_at=expires_at,
        )
        self.store.put(PRE_GRANTS, f"{scope}:{scope_id}:{found.id}", found)
        record_audit(
            self.store,
            ToolApprovalEvent(
                at=at,
                kind="pre_granted",
                actor=by,
                tool=tool,
                pre_grant_id=found.id,
                details={"scope": scope, "scope_id": scope_id, "version": version, "expires_at": expires_at},
            ),
        )
        return found

    def all(self) -> list[ToolPreGrant]:
        """Every pre-grant of the tenant (revoked and expired ones included), newest first."""
        found = [item for _, item in self.store.items(PRE_GRANTS, ToolPreGrant)]
        return sorted(found, key=lambda item: (item.granted_at, item.id), reverse=True)

    def of_scope(self, scope: PreGrantScope, scope_id: str) -> list[ToolPreGrant]:
        """Every pre-grant stored for the scope, revoked and expired ones included (``decide`` filters them)."""
        return self.store.list_prefix(PRE_GRANTS, f"{scope}:{scope_id}:", ToolPreGrant)

    def revoke(self, scope: PreGrantScope, scope_id: str, grant_id: str, *, by: str) -> ToolPreGrant:
        key = f"{scope}:{scope_id}:{grant_id}"
        found = self.store.get(PRE_GRANTS, key, ToolPreGrant)
        if found is None:
            raise NodestroError("tool.pre_grant_not_found", "There is no such pre-grant", id=grant_id)
        if found.revoked_at is not None:
            return found
        revoked = found.model_copy(update={"revoked_at": self.clock(), "revoked_by": by})
        self.store.put(PRE_GRANTS, key, revoked)
        record_audit(
            self.store,
            ToolApprovalEvent(
                at=revoked.revoked_at or self.clock(),
                kind="pre_grant_revoked",
                actor=by,
                tool=found.tool,
                pre_grant_id=found.id,
                details={"scope": scope, "scope_id": scope_id},
            ),
        )
        return revoked


# ---- N-06: grants and denies of the access model v2 --------------------------------------------------------------

GRANTS = "access_grants"
DENIES = "access_denies"
_Record = TypeVar("_Record", Grant, Deny)
#: The run's own tools (agent memory, input files, knowledge proposals, skills): they never pass the per-call gate
#: (:func:`~nodestro_runtime.agent_builder.run_own_tool`), so a deny on them is refused instead of silently ignored.
RUN_OWN_TOOLS = frozenset({"memory", "input_files", "propose_knowledge", "load_skill", "read_skill_file"})
#: Principals that are not a person: API keys (``api:<id>``) and the runtime itself.
_NOT_PERSONS = frozenset({"system", "local-system"})


@dataclass(frozen=True)
class Person:
    """The person who writes a grant or a deny: the only principal :class:`AccessRules` accepts.

    The api builds it from a signed-in user (``local`` on a desktop). Machine principals (``system``, API keys
    ``api:<id>``, anything with a ``:``) are refused, and no tool, model or agent context builds one: a grant is never
    written by a model (spec 7.1)."""

    id: str

    def __post_init__(self) -> None:
        if not self.id or ":" in self.id or self.id in _NOT_PERSONS or len(self.id) > 200 or not self.id.isprintable():
            raise NodestroError("access.person_required", "Only a person can change grants and denies")


class AccessRules:
    """The grants and denies of one tenant (collections ``access_grants`` and ``access_denies``, key = record id).

    Every change is written by a :class:`Person`, outside any tool call, and audited (:func:`record_audit`, which
    also writes the activity log). Records are never deleted: a revocation sets ``revoked_at``, and the gate reads the
    records at every call (:meth:`current`), so a change applies at the next call."""

    def __init__(self, store: DocumentStore, *, clock: Callable[[], datetime] = now) -> None:
        self.store = store
        self.clock = clock

    def grant(
        self,
        *,
        resource: AccessResource,
        actions: Sequence[AccessAction],
        duration: GrantDuration,
        by: Person,
        scope: AccessScope = "all",
        agents: Sequence[str] = (),
        mode: AccessMode = "supervised",
        expires_at: datetime | None = None,
        conversation_id: str | None = None,
        tool: ToolFacts | None = None,
    ) -> Grant:
        """Store a grant. A tool grant names the tool as it is now (``tool``, :meth:`ToolFacts.of`); it ends with a
        new version, another classification or another MCP definition."""
        at = self._check_writer(by)
        _check_resource(resource)
        if (resource.type == "tool") != (tool is not None):
            raise NodestroError(
                "access.tool_facts_required", "A tool grant names the tool's version and classification, and only it"
            )
        if "delete" in actions:
            raise NodestroError(
                "access.grant_destructive", "Deleting for good needs a confirmation each time; it cannot be granted"
            )
        _check_expiry(duration, expires_at, at)
        if (duration == "conversation") != (conversation_id is not None):
            raise NodestroError(
                "access.conversation_required", "A grant for one conversation names that conversation, and only it"
            )
        if mode == "unattended" and set(actions) != {"read"}:
            if duration not in ("until", "once"):
                raise NodestroError(
                    "access.unattended_expiry",
                    "An unattended grant for more than reading needs an expiry date",
                    duration=duration,
                )
            if scope == "all":
                raise NodestroError(
                    "access.unattended_scope", "An unattended grant for more than reading names its agents"
                )
            if expires_at is not None and expires_at - at > MAX_UNATTENDED_GRANT:
                raise NodestroError(
                    "access.unattended_too_long",
                    "An unattended grant for more than reading lasts at most the maximum",
                    days=MAX_UNATTENDED_GRANT.days,
                )
        found = _build(
            Grant,
            id=new_id("agr"),
            resource=resource,
            actions=tuple(dict.fromkeys(actions)),
            scope=scope,
            agents=tuple(agents),
            duration=duration,
            mode=mode,
            expires_at=expires_at,
            conversation_id=conversation_id,
            created_at=at,
            tool=tool,
            granted_by=by.id,
        )
        self.store.put(GRANTS, found.id, found)
        self._audit("access_granted", found, by=by.id, at=at)
        return found

    def deny(
        self,
        *,
        resource: AccessResource,
        actions: Sequence[AccessAction],
        by: Person,
        scope: AccessScope = "all",
        agents: Sequence[str] = (),
        mode: AccessMode | None = None,
        duration: DenyDuration = "until_revoked",
        expires_at: datetime | None = None,
    ) -> Deny:
        at = self._check_writer(by)
        _check_resource(resource)
        if resource.type == "tool" and resource.id in RUN_OWN_TOOLS:
            # The run's own tools never pass the per-call gate, so such a deny would look effective and not be.
            raise NodestroError(
                "access.deny_not_enforced", "This tool belongs to the run and cannot be blocked", tool=resource.id
            )
        _check_expiry(duration, expires_at, at)
        found = _build(
            Deny,
            id=new_id("adn"),
            resource=resource,
            actions=tuple(dict.fromkeys(actions)),
            scope=scope,
            agents=tuple(agents),
            duration=duration,
            mode=mode,
            expires_at=expires_at,
            created_at=at,
            denied_by=by.id,
        )
        self.store.put(DENIES, found.id, found)
        self._audit("access_denied", found, by=by.id, at=at)
        return found

    def revoke_grant(self, grant_id: str, *, by: Person) -> Grant:
        return self._revoke(GRANTS, Grant, grant_id, by=by, kind="access_grant_revoked")

    def revoke_deny(self, deny_id: str, *, by: Person) -> Deny:
        return self._revoke(DENIES, Deny, deny_id, by=by, kind="access_deny_revoked")

    def use(self, grant_id: str) -> bool:
        """Mark an active ``once`` grant used: ``True`` for the one caller that may act on it. One compare-and-set
        from ``active``, like a revocation, so a revoked or expired grant is never used and a revocation is never
        lost."""
        found = self.store.get(GRANTS, grant_id, Grant)
        if found is None or found.duration != "once" or not found.active_at(self.clock()):
            return False
        used = found.model_copy(update={"status": "used"})
        return self.store.replace_if(GRANTS, grant_id, used, field="status", expected="active")

    def grants(self) -> list[Grant]:
        """Every grant (revoked, expired and used ones included), newest first."""
        found = self.store.list(GRANTS, Grant)
        return sorted(found, key=lambda item: (item.created_at, item.id), reverse=True)

    def denies(self) -> list[Deny]:
        """Every deny (revoked and expired ones included), newest first."""
        found = self.store.list(DENIES, Deny)
        return sorted(found, key=lambda item: (item.created_at, item.id), reverse=True)

    def current(self) -> tuple[list[Grant], list[Deny]]:
        """What the gate evaluates (:class:`ToolGate` ``access``); :func:`~.tool_policy.evaluate` skips revoked and
        expired records."""
        return self.grants(), self.denies()

    def _check_writer(self, by: Person) -> datetime:
        if not isinstance(by, Person):
            raise NodestroError("access.person_required", "Only a person can change grants and denies")
        if _BINDING.get() is not None:
            # Code that runs inside a tool call acts for a model: it never changes grants or denies.
            raise NodestroError("access.in_tool_call", "Grants and denies cannot be changed from a tool call")
        return self.clock()

    def _revoke(self, collection: str, model: type[_Record], record_id: str, *, by: Person, kind: AuditKind) -> _Record:
        at = self._check_writer(by)
        found = self.store.get(collection, record_id, model)
        if found is None:
            raise NodestroError("access.not_found", "There is no such grant or deny", id=record_id)
        if found.status != "active":
            return found  # revoked already, or a once grant used before
        revoked = found.model_copy(update={"status": "revoked", "revoked_at": at, "revoked_by": by.id})
        if not self.store.replace_if(collection, record_id, revoked, field="status", expected="active"):
            raw = self.store.get_raw(collection, record_id)
            if not isinstance(raw, dict) or "status" in raw:
                return self.store.get(collection, record_id, model) or found  # used or revoked in the meantime
            # A record stored before ``status`` existed: the compare-and-set cannot see it. Nothing can use such a
            # record (``use`` compares ``status`` too), so writing the revocation directly loses no use.
            self.store.put(collection, record_id, revoked)
        self._audit(kind, revoked, by=by.id, at=at)
        return revoked

    def _audit(self, kind: AuditKind, record: Grant | Deny, *, by: str, at: datetime) -> None:
        """One audit event per change. It names the resource type, never a folder path (paths can name people)."""
        details: dict[str, Any] = {
            "record_id": record.id,
            "resource_type": record.resource.type,
            "actions": list(record.actions),
            "scope": record.scope,
            "duration": record.duration,
            "mode": record.mode,
            "expires_at": record.expires_at,
        }
        if record.resource.type != "folder":
            details["resource_id"] = record.resource.id
        tool = record.resource.id if record.resource.type == "tool" else None
        record_audit(self.store, ToolApprovalEvent(at=at, kind=kind, actor=by, tool=tool, details=details))


def _check_resource(resource: AccessResource) -> None:
    if resource.type != "folder":
        return
    try:
        folder_parts(resource.id)
    except ValueError:
        raise NodestroError("access.folder_path", "A folder is named by its absolute path") from None


def _check_expiry(duration: str, expires_at: datetime | None, at: datetime) -> None:
    if expires_at is not None and expires_at.tzinfo is None:
        raise NodestroError("access.expiry", "An expiry date needs a time zone")
    if (duration == "until") != (expires_at is not None):
        raise NodestroError("access.expiry", "An expiry date belongs to the duration until a date, and only to it")
    if expires_at is not None and expires_at <= at:
        raise NodestroError("access.expiry", "The expiry lies in the past")


def _build(model: type[_Record], **values: Any) -> _Record:
    try:
        return model(**values)
    except ValueError:
        # The message of a validation error can quote the input (a folder path): it is not passed on.
        raise NodestroError("access.invalid", "The grant or deny is not valid") from None


__all__ = [
    "ACCESS_ENV",
    "AUDIT",
    "CHECKPOINTS_ENV",
    "COLLECTION",
    "DEFAULT_MAX_PAUSED",
    "DEFAULT_WAIT",
    "DENIES",
    "DESKTOP_MAX_WAITING",
    "ENABLE_ENV",
    "EXECUTION_WINDOW",
    "FINAL",
    "GRANTS",
    "INTERRUPTED",
    "MAX_PAUSED_ENV",
    "MAX_WAIT",
    "PRE_GRANTS",
    "REFUSALS",
    "RUN_OWN_TOOLS",
    "SWITCH_OFF",
    "AccessRules",
    "ApprovalStatus",
    "AuditKind",
    "CallUse",
    "Person",
    "ToolApprovalEvent",
    "ToolApprovalRequest",
    "ToolApprovals",
    "ToolGate",
    "ToolPreGrants",
    "WaitingSlots",
    "access_grants_enabled",
    "approvals_enabled",
    "arguments_hash",
    "audit_events",
    "audit_target",
    "checkpoints_enabled",
    "max_paused",
    "record_audit",
    "request_key",
    "tool_facts",
]
