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
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
import hashlib
import json
import os
import threading
import time
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from .mcp_access import UNATTENDED_TRIGGERS
from .problems import NodestroError
from .schemas import new_id, now
from .storage import DocumentStore
from .tool_policy import UNKNOWN, CallContext, PreGrantScope, ToolCapability, ToolPreGrant

COLLECTION = "tool_approvals"
#: The longest a call waits for a decision on a server (D10d; longer waits need an agent checkpoint).
DEFAULT_WAIT = timedelta(minutes=15)
MAX_WAIT = timedelta(minutes=15)
POLL_SECONDS = 1.0

ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "cancelled"]
#: ``closed_because`` of the requests of a run that was stopped elsewhere.
INTERRUPTED = "run.interrupted"
FINAL: frozenset[str] = frozenset({"approved", "rejected", "expired", "cancelled"})


def arguments_hash(tool: str, arguments: dict[str, Any], *, version: int | None, definition_hash: str | None) -> str:
    """What an approval binds to: the tool, its version or MCP definition, and the exact arguments."""
    payload = {"tool": tool, "version": version, "definition_hash": definition_hash, "input": arguments}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def request_key(run_id: str, request_id: str) -> str:
    return f"{run_id}:{request_id}"


# ---- the audit trail ------------------------------------------------------------------------------------------

AUDIT = "tool_approval_audit"
#: The newest audit events kept per tenant (older ones are dropped when the trail is read).
MAX_AUDIT = 5000

AuditKind = Literal["decided", "decision_refused", "pre_granted", "pre_grant_revoked", "legacy_stamped"]


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
    store.put(AUDIT, f"{event.at.isoformat()}_{event.id}", event)
    return event


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
    ) -> ToolApprovalRequest:
        """Approve or reject a pending request. ``arguments`` is the :func:`arguments_hash` the person was shown: a
        decision about other arguments or another tool definition is refused (``tool.approval_stale``), and so is a
        decision after the deadline (``tool.approval_expired``) or about a request that was decided already
        (``tool.approval_decided``). Every decision and every refused one is written to the audit trail."""
        try:
            decided = self._decide(run_id, request_id, approved=approved, by=by, arguments=arguments, comment=comment)
        except NodestroError as problem:
            self._audit("decision_refused", by, run_id, request_id, arguments, outcome=problem.code)
            raise
        self._audit("decided", by, run_id, request_id, arguments, outcome=decided.status, tool=decided.tool)
        return decided

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

    def expire_run(self, run_id: str) -> int:
        """Expire every pending request of a run (the run stopped: interrupted, lost lease, crash, restart)."""
        return sum(self.close(run_id, item.id, "expired", because=INTERRUPTED) for item in self.pending(run_id))

    @staticmethod
    def _refuse_final(found: ToolApprovalRequest) -> None:
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
#: How many runs of one process may wait for an approval at the same time (desktop default 3, D10 concept §2.4).
DESKTOP_MAX_WAITING = 3


def approvals_enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "").strip() == "1"


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
    ) -> None:
        self.approvals = approvals
        self.slots = slots or WaitingSlots()
        #: The pre-grants stored for one unattended scope (schedule, batch, test series); none until they are stored.
        self.pre_grants = pre_grants
        #: ADR-0007 D-7 (read-only MCP tools unattended), only after the MCP security acceptance.
        self.mcp_unattended = mcp_unattended

    def call_context(self, *, run_id: str, node_id: str, trigger: str, trigger_ref: str | None) -> CallContext:
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


__all__ = [
    "AUDIT",
    "COLLECTION",
    "DEFAULT_WAIT",
    "DESKTOP_MAX_WAITING",
    "ENABLE_ENV",
    "FINAL",
    "INTERRUPTED",
    "MAX_WAIT",
    "PRE_GRANTS",
    "ApprovalStatus",
    "AuditKind",
    "ToolApprovalEvent",
    "ToolApprovalRequest",
    "ToolApprovals",
    "ToolGate",
    "ToolPreGrants",
    "WaitingSlots",
    "approvals_enabled",
    "arguments_hash",
    "audit_events",
    "record_audit",
    "request_key",
    "tool_facts",
]
