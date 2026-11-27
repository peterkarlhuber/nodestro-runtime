"""Slots of the agent steps with workflow tools of one nesting depth (N-20, RT-096).

An agent step with workflow tools runs in a thread of its depth's pool, and a call of a workflow tool waits in that
thread while the workflow waits for a person (an approval step, a tool approval, a budget decision), up to
``WORKFLOW_TOOL_WAIT_SECONDS``. Such a waiting step does no work, so it must not keep a later step of the same depth
from running:

- at most ``working`` steps of a depth *work* at the same time (the configured run threads, unchanged);
- while a call of a step waits for a person, the step moves to one of ``waiting`` waiting slots and frees its
  working slot; once no call of it waits any more, it takes a working slot again (and waits for one, like a new
  step);
- when all waiting slots are taken, a waiting step keeps its working slot (as before) and tries again while it
  waits, so the limit is never exceeded.

The depth's pool has ``working + waiting`` threads, so the number of threads stays bounded. A step only ever waits
for a slot of its own depth, and the steps it waits for (the workflow's own steps) run one depth below, so no step
waits for a slot held by a step that waits for it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import contextvars
import threading
from typing import Literal, TypeVar

T = TypeVar("T")

#: How often a step that waits for a slot checks whether its run was cancelled.
POLL_SECONDS = 0.25
#: Waiting slots per working slot of a depth.
WAITING_PER_WORKING = 4

#: The slot of the step the current thread works for (set in the step's thread, copied into its tool threads).
_STEP: contextvars.ContextVar[StepSlot | None] = contextvars.ContextVar("nodestro_step_slot", default=None)


class DepthSlots:
    """The working and waiting slots of one depth."""

    def __init__(self, working: int, waiting: int | None = None) -> None:
        self.working_limit = max(1, working)
        self.waiting_limit = max(0, self.working_limit * WAITING_PER_WORKING if waiting is None else waiting)
        self.working = 0
        self.waiting = 0
        self._changed = threading.Condition()

    @property
    def threads(self) -> int:
        """The size of the depth's thread pool."""
        return self.working_limit + self.waiting_limit

    def enter(self, check: Callable[[], None]) -> StepSlot:
        """Wait for a working slot (``check`` may raise: a cancelled run) and return the step's slot."""
        slot = StepSlot(self)
        slot.take_working(check)
        return slot


class StepSlot:
    """What one step holds: a working slot, a waiting slot or (only while it waits for a working slot) nothing."""

    def __init__(self, slots: DepthSlots) -> None:
        self.slots = slots
        self.holds: Literal["working", "waiting"] | None = None
        #: Calls of this step that wait for a person right now (parallel tool calls).
        self.calls_waiting = 0

    def take_working(self, check: Callable[[], None]) -> None:
        slots = self.slots
        with slots._changed:
            while self.holds is None:
                if slots.working < slots.working_limit:
                    slots.working += 1
                    self.holds = "working"
                    break
                check()
                slots._changed.wait(POLL_SECONDS)

    def park(self) -> None:
        """Move to a waiting slot if a call of the step waits for a person and a waiting slot is free."""
        slots = self.slots
        with slots._changed:
            if self.holds == "working" and self.calls_waiting > 0 and slots.waiting < slots.waiting_limit:
                slots.working -= 1
                slots.waiting += 1
                self.holds = "waiting"
                slots._changed.notify_all()

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
        again while it polls); afterwards it works again on a working slot."""
        slots = self.slots
        with slots._changed:
            self.calls_waiting += 1
        self.park()
        went_on = False
        try:
            yield
            went_on = True
        finally:
            with slots._changed:
                self.calls_waiting -= 1
                if self.calls_waiting == 0 and self.holds == "waiting":
                    slots.waiting -= 1
                    self.holds = None
                    slots._changed.notify_all()
            if went_on:
                self.take_working(check)
            # A cancelled run unwinds without a slot: none of its steps works any more.


def current() -> StepSlot | None:
    """The slot of the step the current thread works for, if it is a step with workflow tools."""
    return _STEP.get()


def run_in_slot(slots: DepthSlots, check: Callable[[], None], call: Callable[[], T]) -> T:
    """Run a step in the current (pool) thread once it has a working slot; give the slot back at its end."""
    slot = slots.enter(check)
    token = _STEP.set(slot)
    try:
        return call()
    finally:
        _STEP.reset(token)
        slot.leave()


@contextmanager
def waiting_for_person(check: Callable[[], None]) -> Iterator[Callable[[], None]]:
    """The current step's :meth:`StepSlot.waiting_for_person`, or nothing for a step without one. Yields the
    function the wait calls while it polls."""
    slot = current()
    if slot is None:
        yield lambda: None
        return
    with slot.waiting_for_person(check):
        yield slot.park
