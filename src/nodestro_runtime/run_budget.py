"""Run budgets that a person can raise while the run waits (BUDGET-1).

A run's budget is a limit of that run only. It starts as the workflow's budget, else the workspace's default budget
(:class:`~nodestro_runtime.schemas.RunBudget`); raising it never changes the workflow, its saved versions, the
settings or any other run.

When a model request of an *attended* run (started by a person: ``manual`` or ``resume``) does not fit a raisable
limit, the run does not fail:

1. The request is not sent. Orkexa's tracker refuses it before anything is billed: the limit is used up, or the
   request's most expensive outcome (its reservation, ``orkexa.core.cost_bound``) does not fit the rest of the limit.
2. :class:`RunBudgetGate` opens **one** :class:`BudgetRequest` for the whole top-level run (collection
   ``budget_requests``) and raises :class:`orkexa.RequestSuspended`: the agent's turn ends with a request-stage turn
   checkpoint, the node waits as ``awaiting_budget``. While the request is pending, every further model request of
   the run (parallel agents, sub-workflows, teams) is held the same way; requests already in flight finish and are
   recorded. The run pauses once nothing executes any more; no thread or worker slot stays occupied.
3. A person ends the run or sets a higher limit for exactly the limit that refused the request (the run's own, or
   the own limit of a sub-workflow or team run). The decision is a compare-and-set on the request: exactly one
   decision wins, repeated or stale ones are refused. The run continues and every request is admitted again, so a
   limit that is still too small opens the next request; this repeats as often as needed.

Unattended runs (schedules, batches, test series, published APIs) keep the earlier behaviour: they fail with
``run.budget_exceeded``. Price problems (unknown, unreliable or stale prices, ``pricing.*``) are never budget
decisions: a higher limit cannot fix them. Money is :class:`~decimal.Decimal` throughout (JSON: strings).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
import os
import threading
from typing import TYPE_CHECKING, Any, Literal

from orkexa import RequestSuspended
from orkexa.dynamic import BudgetExceededError
from pydantic import BaseModel, Field

from .platform_events import platform_events
from .problems import NodestroError
from .schemas import BudgetRaise, Run, RunBudget, new_id, now

if TYPE_CHECKING:
    from .cost_tracking import PricedTracker
    from .storage import DocumentStore

COLLECTION = "budget_requests"
AUDIT = "budget_audit"
#: The newest audit events kept per tenant (older ones are dropped when the trail is read).
MAX_AUDIT = 5000
#: The operator's maximum of every raised run budget on a server (USD); a tenant administrator cannot lift it.
OPERATOR_CAP_ENV = "NODESTRO_MAX_RUN_BUDGET_USD"
DEFAULT_DECISION_HOURS = 24
MAX_DECISION_HOURS = 720
#: The highest budget anybody may enter (a typing error guard, not a policy).
MAX_AMOUNT = Decimal("1000000")
CENT = Decimal("0.01")
#: The reason a turn waits (``RequestSuspended.reason``, the turn checkpoint's ``reason``).
SUSPEND_REASON = "budget"

BudgetScope = Literal["run", "subworkflow", "team"]
BudgetRequestStatus = Literal["pending", "raised", "ended", "expired", "cancelled"]
BudgetReason = Literal["used_up", "request_too_large"]


# ---- amounts -------------------------------------------------------------------------------------------------------


def amount(value: Any) -> Decimal:
    """A USD amount from a decimal string or number: finite, not negative, at most :data:`MAX_AMOUNT`, at most two
    decimal places. Raises ``budget.limit_invalid``."""
    try:
        found = Decimal(str(value).strip()) if not isinstance(value, Decimal) else value
    except (InvalidOperation, ValueError):
        raise NodestroError("budget.limit_invalid", "The budget is not a valid amount", value=str(value)) from None
    if not found.is_finite() or found < 0 or found > MAX_AMOUNT:
        raise NodestroError("budget.limit_invalid", "The budget is not a valid amount", value=str(value))
    if found != found.quantize(CENT):
        raise NodestroError("budget.limit_invalid", "Budgets have at most two decimal places", value=str(value))
    return found.quantize(CENT)


def stored_amount(value: Any) -> Decimal | None:
    """A stored amount (settings, environment); ``None`` when empty or invalid."""
    if value is None or value == "":
        return None
    try:
        return amount(value)
    except NodestroError:
        return None


def to_decimal(value: float | Decimal | None) -> Decimal | None:
    """A float of the earlier models (``budget_usd``, costs) as an exact decimal of its shortest representation."""
    return None if value is None else Decimal(str(value))


def operator_cap(*, server: bool = True) -> Decimal | None:
    """The operator's maximum (``NODESTRO_MAX_RUN_BUDGET_USD``); it applies to every profile where it is set."""
    del server  # the operator sets it on servers; on a desktop it is the person's own choice, honoured as well
    return stored_amount(os.environ.get(OPERATOR_CAP_ENV))


def decision_hours(value: Any) -> int:
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DECISION_HOURS
    return min(max(hours, 1), MAX_DECISION_HOURS)


def effective_cap(*caps: Decimal | None) -> Decimal | None:
    """The lowest of the caps that are set."""
    found = [cap for cap in caps if cap is not None]
    return min(found) if found else None


def ceil_cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_CEILING)


def suggested_limit(limit: Decimal, spent: Decimal, reserved: Decimal, bound: Decimal | None) -> Decimal | None:
    """The smallest limit that admits the blocked request: spent + in flight + its most expensive outcome, rounded up
    to the cent and above the current limit. ``None`` when the request's cost has no upper bound. It is a minimum for
    the next request, never a forecast of what the rest of the run costs."""
    if bound is None:
        return None
    return max(ceil_cent(spent + reserved + bound), ceil_cent(limit) + CENT)


# ---- stored requests -----------------------------------------------------------------------------------------------


class BudgetBlock(BaseModel):
    """The limit that refused the request."""

    #: ``run``: the top-level run's own limit; ``subworkflow`` / ``team``: the own limit of a child run.
    scope: BudgetScope = "run"
    run_id: str
    #: The node of the top-level run the child run belongs to (``None`` for the run's own limit).
    node_id: str | None = None
    #: The child run's workflow name (``None`` for the run's own limit).
    label: str | None = None


class BlockedRequest(BaseModel):
    """A model request that waits: the one that was refused, and the ones held while the decision is pending."""

    run_id: str
    node_id: str
    agent: str
    model: str
    #: Its most expensive outcome in USD (``None``: no upper bound, e.g. no price, provider-native tools, images).
    max_cost_usd: Decimal | None = None
    bounded: bool = False
    #: The price catalog's status of the model when it waited (``verified``, ``stale``, ``unknown``, ...).
    price_status: str | None = None
    #: Whether this is the request that was refused (the others only wait for the decision).
    refused: bool = False


class BudgetRequest(BaseModel):
    """A budget decision of one top-level run: what blocked, the numbers when it was asked, and the decision."""

    id: str = Field(default_factory=lambda: new_id("bud"))
    run_id: str
    workflow_id: str
    workflow_name: str
    #: The store's tenant (``None`` on a desktop).
    tenant_id: str | None = None
    status: BudgetRequestStatus = "pending"
    #: ``used_up``: nothing is left; ``request_too_large``: the request's most expensive outcome does not fit.
    reason: BudgetReason = "used_up"
    attended: bool = True
    block: BudgetBlock
    #: The blocking limit, what was spent against it (known costs) and what was in flight when it was asked.
    limit_usd: Decimal
    spent_usd: Decimal
    reserved_usd: Decimal = Decimal(0)
    #: Some cost counted against the limit is unknown (no price): it is not zero, only not known.
    unknown_cost: bool = False
    requests: list[BlockedRequest] = Field(default_factory=list)
    suggested_limit_usd: Decimal | None = None
    created_at: datetime = Field(default_factory=now)
    deadline: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    #: The new limit (``raised``).
    new_limit_usd: Decimal | None = None
    #: The client's key of the decision: the same decision sent again is answered with this result.
    idempotency_key: str | None = None
    #: What followed the decision: ``continued`` (the run goes on) or ``ended``.
    outcome: Literal["continued", "ended"] | None = None
    #: Why a request closed without a decision (``deadline``, ``run.cancelled``, ``run.gone``, ...).
    closed_because: str | None = None

    @property
    def key(self) -> str:
        return request_key(self.run_id, self.id)

    @property
    def refused(self) -> BlockedRequest | None:
        return next((item for item in self.requests if item.refused), None)


def request_key(run_id: str, request_id: str) -> str:
    return f"{run_id}:{request_id}"


AuditKind = Literal["requested", "raised", "ended", "expired", "cancelled", "refused", "applied"]


class BudgetAuditEvent(BaseModel):
    """One entry of the tenant's budget audit trail (every decision, also refused ones, with the validation's code)."""

    id: str = Field(default_factory=lambda: new_id("bae"))
    at: datetime = Field(default_factory=now)
    kind: AuditKind
    actor: str
    run_id: str
    request_id: str | None = None
    previous_limit_usd: Decimal | None = None
    new_limit_usd: Decimal | None = None
    #: The validation's result: ``ok`` or the refusing problem code.
    result: str = "ok"
    #: ``continued``, ``ended``, ``waiting`` (decided while the run still executes elsewhere) or ``None``.
    outcome: str | None = None
    scope: BudgetScope | None = None


def record_audit(store: DocumentStore, event: BudgetAuditEvent) -> BudgetAuditEvent:
    """Store ``event``; with a platform sink (:mod:`.platform_events`) its mirror commits in the same transaction."""
    sink = platform_events(store)
    key = f"{event.at.isoformat()}_{event.id}"
    if sink is None:
        store.put(AUDIT, key, event)
        return event
    with store.transaction():
        store.put(AUDIT, key, event)
        sink.audit(
            str(getattr(store, "tenant_id", "")),
            event_id=event.id,
            at=event.at,
            action=f"nodestro.run_budget.{event.kind}",
            actor=event.actor,
            target_type="run",
            target_id=event.run_id,
            outcome="denied" if event.kind == "refused" else "success",
            details={
                key: str(value)
                for key, value in event.model_dump(exclude={"id", "at", "actor"}).items()
                if value is not None
            },
        )
    return event


def audit_events(store: DocumentStore, *, run_id: str | None = None, limit: int = 200) -> list[BudgetAuditEvent]:
    """The newest audit events first (of one run, or of the tenant)."""
    items = store.items(AUDIT, BudgetAuditEvent)
    items.sort(key=lambda pair: pair[0], reverse=True)
    for key, _ in items[MAX_AUDIT:]:
        store.delete(AUDIT, key)
    events = [event for _, event in items[:MAX_AUDIT]]
    if run_id is not None:
        events = [event for event in events if event.run_id == run_id]
    return events[:limit]


class BudgetRequests:
    """The budget requests of one tenant's store."""

    def __init__(self, store: DocumentStore, *, clock: Callable[[], datetime] = now) -> None:
        self.store = store
        self.clock = clock

    def get(self, run_id: str, request_id: str) -> BudgetRequest | None:
        found = self.store.get(COLLECTION, request_key(run_id, request_id), BudgetRequest)
        if found is None or found.tenant_id != getattr(self.store, "tenant_id", None) or found.run_id != run_id:
            return None
        return found

    def of_run(self, run_id: str) -> list[BudgetRequest]:
        """Every request of a top-level run, oldest first."""
        found = self.store.list_prefix(COLLECTION, f"{run_id}:", BudgetRequest)
        tenant = getattr(self.store, "tenant_id", None)
        return sorted((item for item in found if item.tenant_id == tenant), key=lambda item: item.created_at)

    def pending(self) -> list[BudgetRequest]:
        """Every pending request of the tenant, oldest first."""
        tenant = getattr(self.store, "tenant_id", None)
        found = [item for _, item in self.store.items(COLLECTION, BudgetRequest) if item.tenant_id == tenant]
        return sorted((item for item in found if item.status == "pending"), key=lambda item: item.created_at)

    def _stamped(self, request: BudgetRequest, **changes: Any) -> BudgetRequest:
        """``request`` with ``changes``, marked as this tenant's (also when it was built before it was stored)."""
        return request.model_copy(update={**changes, "tenant_id": getattr(self.store, "tenant_id", None)})

    def put(self, request: BudgetRequest) -> BudgetRequest:
        stamped = self._stamped(request)
        self.store.put(COLLECTION, stamped.key, stamped)
        return stamped

    def transition(self, found: BudgetRequest, status: BudgetRequestStatus, **changes: Any) -> BudgetRequest | None:
        """Move a pending request to ``status`` (compare-and-set on its status): ``None`` when another decision, a
        deadline or a cancellation came first."""
        if found.status != "pending":
            return None
        updated = self._stamped(found, status=status, **changes)
        if self.store.replace_if(COLLECTION, found.key, updated, field="status", expected="pending"):
            return updated
        return None

    def update(self, found: BudgetRequest, **changes: Any) -> BudgetRequest | None:
        """Change fields of a request that is still pending (numbers, waiting requests): compare-and-set on status."""
        updated = self._stamped(found, **changes)
        if self.store.replace_if(COLLECTION, found.key, updated, field="status", expected=found.status):
            return updated
        return None

    def close_run(self, run_id: str, status: BudgetRequestStatus, *, because: str) -> list[BudgetRequest]:
        """Close every pending request of a run without a decision (cancelled, gone, expired)."""
        closed = []
        for item in self.of_run(run_id):
            if item.status == "pending":
                done = self.transition(item, status, closed_because=because, decided_at=self.clock())
                if done is not None:
                    closed.append(done)
        return closed

    def recent(self, *, hours: int = MAX_DECISION_HOURS + 24) -> list[BudgetRequest]:
        """The tenant's requests of the last ``hours`` (pending or decided), oldest first."""
        tenant = getattr(self.store, "tenant_id", None)
        since = self.clock() - timedelta(hours=hours)
        found = [
            item
            for _, item in self.store.items(COLLECTION, BudgetRequest)
            if item.tenant_id == tenant and item.created_at >= since
        ]
        return sorted(found, key=lambda item: item.created_at)

    def overdue(self) -> list[BudgetRequest]:
        at = self.clock()
        return [item for item in self.pending() if item.deadline is not None and item.deadline <= at]


# ---- the run's budgets --------------------------------------------------------------------------------------------


def initial_budget(
    run: Run, *, workflow_budget: float | None, default_budget: float | None, child: BudgetScope | None = None
) -> RunBudget:
    """The budget a run starts with (BUDGET-1): its workflow's budget, else the default budget (top-level runs only:
    a sub-workflow without a budget has no limit of its own; its parent's applies)."""
    if workflow_budget is not None:
        if child is not None:
            source: Any = child
        else:
            source = "release" if run.release_id else "workflow"
        limit = to_decimal(workflow_budget)
    elif child is None and default_budget is not None:
        source, limit = "default", to_decimal(default_budget)
    else:
        source, limit = "none", None
    return RunBudget(source=source, initial_limit_usd=limit, limit_usd=limit)


def apply_raise(run: Run, request: BudgetRequest) -> bool:
    """Record an approved raise in the run whose limit it raises (once). Returns whether something changed."""
    budget = run.budget or RunBudget()
    if request.new_limit_usd is None or any(item.request_id == request.id for item in budget.raises):
        return False
    budget.raises.append(
        BudgetRaise(
            request_id=request.id,
            previous_limit_usd=budget.limit_usd if budget.limit_usd is not None else request.limit_usd,
            new_limit_usd=request.new_limit_usd,
            decided_by=request.decided_by or "",
            decided_at=request.decided_at or now(),
        )
    )
    budget.limit_usd = request.new_limit_usd
    run.budget = budget
    run.budget_usd = float(request.new_limit_usd)
    return True


def limit_of(run: Run) -> Decimal | None:
    """The limit that applies to ``run`` now."""
    if run.budget is not None:
        return run.budget.limit_usd
    return to_decimal(run.budget_usd)


# ---- holding requests while a run waits ----------------------------------------------------------------------------

#: Set while a model request of a run may stop its turn (every request of a run's agents, judges and orchestrators).
_SUSPENDABLE: ContextVar[bool] = ContextVar("nodestro_budget_suspendable", default=False)


@contextmanager
def suspendable() -> Iterator[None]:
    """Requests in this block may stop their turn for a budget decision (:class:`orkexa.RequestSuspended`)."""
    token = _SUSPENDABLE.set(True)
    try:
        yield
    finally:
        _SUSPENDABLE.reset(token)


def can_suspend() -> bool:
    return _SUSPENDABLE.get()


@dataclass
class TrackerScope:
    """Which run's limit a tracker holds: the top-level run's, or a child run's own limit."""

    run_id: str
    scope: BudgetScope = "run"
    node_id: str | None = None
    label: str | None = None
    #: The run's limit now (``None``: none) and its known cost before this execution: the tracker's limit is the
    #: rest (limit - offset).
    limit: Decimal | None = None
    offset: Decimal = Decimal(0)
    #: Some earlier cost of the run is unknown.
    unknown_before: bool = False


@dataclass
class RunBudgetGate:
    """The budget decisions of one execution of an attended top-level run, shared by the trackers of its child runs.

    ``on_request`` is called (in the requesting thread) when a request is opened or changed: the engine records the
    pending request in the run and announces it."""

    run_id: str
    workflow_id: str
    workflow_name: str
    requests: BudgetRequests
    deadline_hours: int = DEFAULT_DECISION_HOURS
    on_request: Callable[[BudgetRequest, bool], None] | None = None
    pending: BudgetRequest | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def hold(self, tracker: PricedTracker, *, agent: str, provider: str, model: str, amount: Decimal | None) -> None:
        """While a decision is pending, no request of the run is sent: it waits for the same decision."""
        with self.lock:
            found = self.pending
            if found is None:
                return
            if not can_suspend():
                raise BudgetExceededError(
                    f"The run waits for a budget decision; request of agent {agent!r} to {provider}:{model} not sent"
                )
            self._join(found, tracker, agent=agent, provider=provider, model=model, bound=amount, refused=False)
        self._suspend(found)

    def block(
        self,
        tracker: PricedTracker,
        exc: BudgetExceededError,
        *,
        agent: str,
        provider: str,
        model: str,
        amount: Decimal | None,
        requester: PricedTracker,
    ) -> None:
        """``exc`` refused a request because of ``tracker``'s limit: open the run's budget request (or join the
        pending one) and stop the turn. Raises ``exc`` itself where a turn cannot stop."""
        if not can_suspend():
            raise exc
        scope = tracker.budget_scope
        assert scope is not None
        created = False
        with self.lock:
            found = self.pending
            if found is None:
                limit = scope.limit if scope.limit is not None else scope.offset + (tracker.limit_usd or Decimal(0))
                spent = scope.offset + (getattr(exc, "spent_usd", None) or tracker.spent_usd)
                reserved = getattr(exc, "reserved_usd", None) or Decimal(0)
                bound = getattr(exc, "requested_usd", None)
                if bound is None:
                    bound = amount
                reason: BudgetReason = "request_too_large" if spent < limit else "used_up"
                found = BudgetRequest(
                    run_id=self.run_id,
                    workflow_id=self.workflow_id,
                    workflow_name=self.workflow_name,
                    reason=reason,
                    block=BudgetBlock(scope=scope.scope, run_id=scope.run_id, node_id=scope.node_id, label=scope.label),
                    limit_usd=limit,
                    spent_usd=spent,
                    reserved_usd=reserved,
                    unknown_cost=scope.unknown_before or bool(tracker.unpriced_records),
                    suggested_limit_usd=suggested_limit(limit, spent, reserved, bound),
                    deadline=now() + timedelta(hours=self.deadline_hours),
                )
                self.pending = found
                created = True
            self._join(found, requester, agent=agent, provider=provider, model=model, bound=amount, refused=created)
        self._suspend(found)

    def _join(
        self,
        found: BudgetRequest,
        tracker: PricedTracker,
        *,
        agent: str,
        provider: str,
        model: str,
        bound: Decimal | None,
        refused: bool,
    ) -> None:
        scope = tracker.budget_scope
        run_id = scope.run_id if scope is not None else self.run_id
        node_id = agent.split(":", 1)[0].split("/", 1)[0]
        if any(item.run_id == run_id and item.agent == agent for item in found.requests):
            return
        status = None
        lookup = getattr(tracker, "_status", None)
        if callable(lookup):
            try:
                status = str(lookup(provider, model)[0])
            except Exception:  # the price catalog is only shown here
                status = None
        found.requests.append(
            BlockedRequest(
                run_id=run_id,
                node_id=node_id,
                agent=agent,
                model=f"{provider}:{model}",
                max_cost_usd=bound,
                bounded=bound is not None,
                price_status=status,
                refused=refused,
            )
        )
        if self.on_request is not None:
            self.on_request(found, refused)

    @staticmethod
    def _suspend(found: BudgetRequest) -> None:
        raise RequestSuspended(SUSPEND_REASON, ref=found.id)

    def resolved(self) -> BudgetRequest | None:
        """The pending request as stored now, if somebody decided it (or it closed) meanwhile."""
        found = self.pending
        if found is None:
            return None
        stored = self.requests.get(found.run_id, found.id)
        if stored is None or stored.status == "pending":
            return None
        return stored


__all__ = [
    "AUDIT",
    "COLLECTION",
    "OPERATOR_CAP_ENV",
    "BlockedRequest",
    "BudgetAuditEvent",
    "BudgetBlock",
    "BudgetRequest",
    "BudgetRequests",
    "RunBudgetGate",
    "TrackerScope",
    "amount",
    "apply_raise",
    "audit_events",
    "can_suspend",
    "ceil_cent",
    "decision_hours",
    "effective_cap",
    "initial_budget",
    "limit_of",
    "operator_cap",
    "record_audit",
    "stored_amount",
    "suggested_limit",
    "suspendable",
    "to_decimal",
]
