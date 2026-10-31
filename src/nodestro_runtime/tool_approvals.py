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
from typing import Any, Literal

from pydantic import BaseModel, Field

from .problems import NodestroError
from .schemas import new_id, now
from .storage import DocumentStore
from .tool_policy import PreGrantScope, ToolCapability, ToolPreGrant

COLLECTION = "tool_approvals"
#: The longest a call waits for a decision on a server (D10d; longer waits need an agent checkpoint).
DEFAULT_WAIT = timedelta(minutes=15)
MAX_WAIT = timedelta(minutes=15)
POLL_SECONDS = 1.0

ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "cancelled"]
FINAL: frozenset[str] = frozenset({"approved", "rejected", "expired", "cancelled"})


def arguments_hash(tool: str, arguments: dict[str, Any], *, version: int | None, definition_hash: str | None) -> str:
    """What an approval binds to: the tool, its version or MCP definition, and the exact arguments."""
    payload = {"tool": tool, "version": version, "definition_hash": definition_hash, "input": arguments}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def request_key(run_id: str, request_id: str) -> str:
    return f"{run_id}:{request_id}"


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
                self.close(run_id, request_id, "expired")
                continue
            if check is not None:
                try:
                    check()
                except BaseException:
                    self.close(run_id, request_id, "cancelled")
                    raise
            self.sleep(self.poll_seconds)

    # ---- the deciding side ----------------------------------------------------------------------------------------

    def get(self, run_id: str, request_id: str) -> ToolApprovalRequest | None:
        return self.store.get(COLLECTION, request_key(run_id, request_id), ToolApprovalRequest)

    def pending(self, run_id: str) -> list[ToolApprovalRequest]:
        return [item for item in self.of_run(run_id) if item.status == "pending"]

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
        (``tool.approval_decided``)."""
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
            self.close(run_id, request_id, "expired")
            current = self.get(run_id, request_id)
        assert current is not None
        self._refuse_final(current)
        raise AssertionError("unreachable: the request is final")  # pragma: no cover

    def close(self, run_id: str, request_id: str, status: Literal["expired", "cancelled"]) -> bool:
        """Mark a pending request ``expired`` or ``cancelled``; ``False`` when it was not pending any more."""
        found = self.get(run_id, request_id)
        if found is None or found.status != "pending":
            return False
        closed = found.model_copy(update={"status": status, "decided_at": self.clock()})
        return self.store.replace_if(COLLECTION, found.key, closed, field="status", expected="pending")

    def expire_run(self, run_id: str) -> int:
        """Expire every pending request of a run (the run stopped: interrupted, lost lease, crash, restart)."""
        return sum(self.close(run_id, item.id, "expired") for item in self.pending(run_id))

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

    The desktop uses one per process; a server worker supplies its own that also bounds its held runs (S-2a step 3).
    """

    def __init__(self, max_waiting: int = DESKTOP_MAX_WAITING) -> None:
        if max_waiting < 1:
            raise ValueError("max_waiting must be at least 1")
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


__all__ = [
    "COLLECTION",
    "DEFAULT_WAIT",
    "DESKTOP_MAX_WAITING",
    "ENABLE_ENV",
    "FINAL",
    "MAX_WAIT",
    "ApprovalStatus",
    "ToolApprovalRequest",
    "ToolApprovals",
    "ToolGate",
    "WaitingSlots",
    "approvals_enabled",
    "arguments_hash",
    "request_key",
]
