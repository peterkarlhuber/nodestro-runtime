"""Slots of the agent steps with workflow tools of one nesting depth (N-20, RT-096).

An agent step with workflow tools runs in a thread of its depth's pool, and a call of a workflow tool waits in that
thread while the workflow waits for a person (an approval step, a tool approval, a budget decision), up to
``WORKFLOW_TOOL_WAIT_SECONDS``. Such a waiting step does no work, so it must not keep a later step of the same depth
from running:

- at most ``working`` steps of a depth *work* at the same time (the configured run threads, unchanged);
- a step all of whose running tool calls wait for a person moves to one of ``waiting`` waiting slots and frees its
  working slot. As soon as one of its calls works again (its wait ended, or a new call starts) and before its next
  model request, it takes a working slot again; it waits for one in turn (first come, first served) like a new step;
- when all waiting slots are taken, a waiting step keeps its working slot (as before) and tries again while it
  waits, so no limit is ever exceeded.

The depth's pool has ``working + waiting`` threads, so the number of threads stays bounded. A slot belongs to exactly
one step (its run and node): only that step's own hooks and waits act on it. Child runs never see it (their
coroutines start without it, and every thread of an ordinary step runs without one), so a wait inside a child run
never moves its parent's slot. A step only ever waits for a slot of its own depth, and the steps it waits for (the
workflow's own steps) run one depth below or on the ordinary pool, so no step waits for a slot held by a step that
waits for it. Waiting for a slot ends when ``check`` raises (the run was cancelled or reached its time limit).
"""

from __future__ import annotations

import collections
from collections.abc import Callable, Iterator
import contextlib
from contextlib import contextmanager
import contextvars
import threading
from typing import Any, Literal, TypeVar

T = TypeVar("T")

#: How often a step that waits for a slot checks whether its run was cancelled (or reached its time limit).
POLL_SECONDS = 0.25
#: Waiting slots per working slot of a depth.
WAITING_PER_WORKING = 4

#: The slot of the step the current thread works for (set in the step's thread, copied into its tool threads).
_STEP: contextvars.ContextVar[StepSlot | None] = contextvars.ContextVar("nodestro_step_slot", default=None)

Owner = tuple[str, str]


class DepthSlots:
    """The working and waiting slots of one depth."""

    def __init__(self, working: int, waiting: int | None = None) -> None:
        self.working_limit = max(1, working)
        self.waiting_limit = max(0, self.working_limit * WAITING_PER_WORKING if waiting is None else waiting)
        self.working = 0
        self.waiting = 0
        self._changed = threading.Condition()
        #: Steps that wait for a working slot, in the order they asked (first come, first served).
        self._queue: collections.deque[StepSlot] = collections.deque()

    @property
    def threads(self) -> int:
        """The size of the depth's thread pool."""
        return self.working_limit + self.waiting_limit

    def enter(self, check: Callable[[], None], owner: Owner) -> StepSlot:
        """Wait for a working slot (``check`` may raise: a cancelled run) and return the step's slot."""
        slot = StepSlot(self, owner)
        slot.take_working(check)
        return slot


class StepSlot:
    """What one step holds: a working slot, a waiting slot or (only while it waits for a working slot, or while its
    cancelled run unwinds) nothing."""

    def __init__(self, slots: DepthSlots, owner: Owner) -> None:
        self.slots = slots
        #: The step's run and node.
        self.owner = owner
        self.holds: Literal["working", "waiting"] | None = None
        #: The step's tool calls that run right now (started and not finished), by id.
        self.active: set[str] = set()
        #: How many of them wait for a person right now.
        self.calls_waiting = 0

    def take_working(self, check: Callable[[], None]) -> None:
        """Hold a working slot: at once if the step holds one, else in turn once one is free."""
        slots = self.slots
        with slots._changed:
            if self.holds == "working":
                return
            if self.holds == "waiting":
                slots.waiting -= 1
                self.holds = None
            slots._queue.append(self)
            try:
                while slots._queue[0] is not self or slots.working >= slots.working_limit:
                    check()
                    slots._changed.wait(POLL_SECONDS)
                slots.working += 1
                self.holds = "working"
            finally:
                slots._queue.remove(self)
                slots._changed.notify_all()

    def _park_locked(self) -> None:
        slots = self.slots
        if (
            self.holds == "working"
            and self.calls_waiting > 0
            and self.calls_waiting >= len(self.active)
            and slots.waiting < slots.waiting_limit
        ):
            slots.working -= 1
            slots.waiting += 1
            self.holds = "waiting"
            slots._changed.notify_all()

    def park(self) -> None:
        """Move to a waiting slot if every running call of the step waits for a person and a waiting slot is free."""
        with self.slots._changed:
            self._park_locked()

    def call_started(self, call_id: str, check: Callable[[], None]) -> None:
        """A tool call of the step starts: it works, so the step holds a working slot."""
        with self.slots._changed:
            self.active.add(call_id)
        self.take_working(check)

    def call_finished(self, call_id: str) -> None:
        """A tool call of the step ended; if the others all wait for a person, the step may park."""
        with self.slots._changed:
            self.active.discard(call_id)
            self._park_locked()

    def leave(self) -> None:
        """Give back whatever the step holds (its end)."""
        slots = self.slots
        with slots._changed:
            if self.holds == "working":
                slots.working -= 1
            elif self.holds == "waiting":
                slots.waiting -= 1
            self.holds = None
            slots._changed.notify_all()

    @contextmanager
    def waiting_for_person(self, check: Callable[[], None]) -> Iterator[None]:
        """Around a call's wait for a person: the step may park meanwhile (see :meth:`park`, which the wait calls
        again while it polls); afterwards, however the wait ended, the call works again on a working slot. Only when
        ``check`` raises while it waits for that slot (the run was cancelled) does the original error propagate
        without a slot; the cancelled step does no more work, and its next model request would wait for one."""
        with self.slots._changed:
            self.calls_waiting += 1
            self._park_locked()
        try:
            yield
        except BaseException:
            self._went_on()
            with contextlib.suppress(BaseException):  # the run was cancelled: the original error propagates
                self.take_working(check)
            raise
        self._went_on()
        self.take_working(check)

    def _went_on(self) -> None:
        with self.slots._changed:
            self.calls_waiting -= 1


def current(owner: Owner) -> StepSlot | None:
    """The slot of the step ``owner`` (run id, node id) if the current thread works for it; ``None`` for any other
    step (a step without workflow tools, a step of a child run)."""
    slot = _STEP.get()
    return slot if slot is not None and slot.owner == owner else None


def run_in_slot(slots: DepthSlots, check: Callable[[], None], owner: Owner, call: Callable[[], T]) -> T:
    """Run a step in the current (pool) thread once it has a working slot; give the slot back at its end."""
    slot = slots.enter(check, owner)
    token = _STEP.set(slot)
    try:
        return call()
    finally:
        _STEP.reset(token)
        slot.leave()


def without_slot(call: Callable[..., T], /, *args: Any) -> T:
    """``call(*args)`` in a copy of the current context without any step's slot: for every thread of an ordinary
    step and for a child run's coroutine started from a step's thread, so nothing below a step acts on its slot."""
    context = contextvars.copy_context()
    context.run(_STEP.set, None)
    return context.run(call, *args)


@contextmanager
def waiting_for_person(check: Callable[[], None], owner: Owner) -> Iterator[Callable[[], None]]:
    """The slot of step ``owner`` around a wait for a person (:meth:`StepSlot.waiting_for_person`), or nothing for a
    step without one. Yields the function the wait calls while it polls."""
    slot = current(owner)
    if slot is None:
        yield lambda: None
        return
    with slot.waiting_for_person(check):
        yield slot.park


def call_started(owner: Owner, call_id: str, check: Callable[[], None]) -> None:
    slot = current(owner)
    if slot is not None:
        slot.call_started(call_id, check)


def call_finished(owner: Owner, call_id: str) -> None:
    slot = current(owner)
    if slot is not None:
        slot.call_finished(call_id)


def require_working(owner: Owner, check: Callable[[], None]) -> None:
    """Before the step's work (a model request): it holds a working slot, waiting for one if needed."""
    slot = current(owner)
    if slot is not None:
        slot.take_working(check)
