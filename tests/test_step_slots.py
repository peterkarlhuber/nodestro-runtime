"""The working and waiting slots of the steps with workflow tools of one depth (RT-096)."""

from __future__ import annotations

from collections.abc import Callable
import contextvars
import threading
import time
from typing import Any

import pytest

from nodestro_runtime import step_slots
from nodestro_runtime.step_slots import DepthSlots

STEP = ("run_a", "agent")
OTHER = ("run_b", "agent")


class Cancelled(Exception):
    pass


def never() -> None:
    return None


def eventually(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def in_thread(call: Callable[[], object]) -> threading.Thread:
    thread = threading.Thread(target=contextvars.copy_context().run, args=(call,), daemon=True)
    thread.start()
    return thread


def free(slots: DepthSlots) -> bool:
    return (slots.working, slots.waiting) == (0, 0)


def test_the_pool_has_a_bounded_number_of_threads() -> None:
    slots = DepthSlots(3)
    assert (slots.working_limit, slots.waiting_limit, slots.threads) == (3, 12, 15)
    assert DepthSlots(0).threads == 1 + step_slots.WAITING_PER_WORKING


def test_never_more_steps_work_than_working_slots() -> None:
    slots = DepthSlots(2)
    busy = 0
    most = 0
    lock = threading.Lock()

    def step() -> None:
        nonlocal busy, most
        with lock:
            busy += 1
            most = max(most, busy)
        time.sleep(0.02)
        with lock:
            busy -= 1

    threads = [
        threading.Thread(target=step_slots.run_in_slot, args=(slots, never, (f"run_{n}", "a"), step)) for n in range(8)
    ]
    for item in threads:
        item.start()
    for item in threads:
        item.join(10)
    assert most == 2 and free(slots)


def test_after_the_wait_the_step_waits_for_a_working_slot_in_turn() -> None:
    slots = DepthSlots(1, 1)
    first = slots.enter(never, STEP)
    first.call_started("c1", never)
    done = threading.Event()
    release = threading.Event()

    def waiting_call() -> None:
        with first.waiting_for_person(never):
            release.wait(5)
        done.set()

    thread = in_thread(waiting_call)
    eventually(lambda: first.holds == "waiting")
    second = slots.enter(never, OTHER)
    release.set()
    time.sleep(0.1)
    assert not done.is_set() and first.holds is None and slots.working == 1  # still the second step's slot
    second.leave()
    thread.join(5)
    assert done.is_set() and first.holds == "working"
    first.call_finished("c1")
    first.leave()
    assert free(slots)


def test_m1_a_step_parks_only_while_every_running_call_waits() -> None:
    """Review M1: a sibling call that still works keeps the step on its working slot; a call that starts while the
    step is parked takes a working slot first."""
    slots = DepthSlots(1, 1)
    step = slots.enter(never, STEP)
    step.call_started("waits", never)
    step.call_started("works", never)
    with step.waiting_for_person(never):
        assert step.holds == "working" and (slots.working, slots.waiting) == (1, 0)  # "works" still works
        step.call_finished("works")
        assert step.holds == "waiting" and (slots.working, slots.waiting) == (0, 1)
        other = slots.enter(never, OTHER)  # the depth's next step works meanwhile
        started: list[str] = []

        def third_call() -> None:
            step.call_started("third", never)
            started.append(str(step.holds))

        thread = in_thread(third_call)
        time.sleep(0.1)
        assert not started and slots.working == 1  # never beyond the limit: it waits for the slot
        other.leave()
        thread.join(5)
        assert started == ["working"] and (slots.working, slots.waiting) == (1, 0)
        step.call_finished("third")
        assert step.holds == "waiting"
    assert step.holds == "working" and (slots.working, slots.waiting) == (1, 0)
    step.call_finished("waits")
    step.leave()
    assert free(slots)


def test_with_all_waiting_slots_taken_a_waiting_step_keeps_its_slot_and_parks_later() -> None:
    slots = DepthSlots(2, 1)
    parked = slots.enter(never, OTHER)
    parked.calls_waiting = 1
    parked.park()
    assert parked.holds == "waiting"
    held = slots.enter(never, STEP)
    token = step_slots._STEP.set(held)
    try:
        with step_slots.waiting_for_person(never, STEP) as retry:
            # No waiting slot is free: the step keeps its working slot, never beyond a limit.
            assert held.holds == "working" and (slots.working, slots.waiting) == (1, 1)
            parked.leave()  # the parked step ends: its waiting slot is free
            retry()  # what the wait calls while it polls
            assert held.holds == "waiting" and (slots.working, slots.waiting) == (0, 1)
    finally:
        step_slots._STEP.reset(token)
    assert held.holds == "working" and (slots.working, slots.waiting) == (1, 0)
    held.leave()
    assert free(slots)


def test_parallel_calls_of_one_step_park_it_once_and_it_goes_on_after_the_first() -> None:
    slots = DepthSlots(1, 2)
    step = slots.enter(never, STEP)
    step.call_started("a", never)
    step.call_started("b", never)
    with step.waiting_for_person(never):
        with step.waiting_for_person(never):
            assert (step.calls_waiting, slots.waiting, slots.working) == (2, 1, 0)
        # One call works again (it returns its result): the step works, though the other call still waits.
        assert step.holds == "working" and (slots.working, slots.waiting) == (1, 0)
        step.call_finished("b")
        assert step.holds == "waiting"
    step.call_finished("a")
    assert step.holds == "working"
    step.leave()
    assert free(slots)


@pytest.mark.parametrize("working", [1, 2])
def test_two_waits_of_one_step_that_end_together_share_its_place_in_the_queue(working: int) -> None:
    """Re-review: both parallel calls of a parked step end their wait while every working slot is taken. The step
    waits once in the queue, the first call to get the slot takes it for the step, the other one returns too; the
    slot is counted once and given back once (before: a second queue entry, a call that waited forever for a slot
    its own step held, or a slot counted twice)."""
    slots = DepthSlots(working, 4)
    others = [slots.enter(never, (f"other_{n}", "a")) for n in range(working - 1)]
    step = slots.enter(never, STEP)
    step.call_started("a", never)
    step.call_started("b", never)
    waits = [step.waiting_for_person(never) for _ in "ab"]
    for item in waits:
        item.__enter__()
    assert step.holds == "waiting"
    blocker = slots.enter(never, OTHER)  # takes the freed working slot
    ended: list[str] = []

    def end(item: Any, name: str) -> None:
        item.__exit__(None, None, None)
        ended.append(name)

    threads = [in_thread(lambda item=item, name=name: end(item, name)) for item, name in zip(waits, "ab", strict=True)]
    time.sleep(0.3)  # both calls wait for the step's working slot
    assert not ended
    blocker.leave()
    for thread in threads:
        thread.join(5)
    assert sorted(ended) == ["a", "b"]
    assert step.holds == "working" and slots.working == working and not slots._queue
    step.call_finished("a")
    step.call_finished("b")
    step.leave()
    for other in others:
        other.leave()
    assert free(slots)


def test_m1_a_wait_that_fails_takes_a_working_slot_again() -> None:
    """Review M1: an error that is not the run's cancel (a store error, a timeout of the call) becomes a tool result
    and the agent goes on: on a working slot."""
    slots = DepthSlots(1, 1)
    step = slots.enter(never, STEP)
    step.call_started("a", never)
    with pytest.raises(OSError), step.waiting_for_person(never):
        assert step.holds == "waiting"
        raise OSError("store gone")
    assert step.holds == "working" and (slots.working, slots.waiting) == (1, 0)
    step.call_finished("a")
    step.leave()
    assert free(slots)


def test_a_cancelled_run_stops_waiting_for_a_slot_and_holds_none() -> None:
    slots = DepthSlots(1, 1)
    busy = slots.enter(never, OTHER)
    cancelled = threading.Event()

    def check() -> None:
        if cancelled.is_set():
            raise Cancelled

    errors: list[BaseException] = []

    def start() -> None:
        try:
            slots.enter(check, STEP)
        except Cancelled as exc:
            errors.append(exc)

    thread = in_thread(start)
    time.sleep(0.1)
    cancelled.set()
    thread.join(5)
    assert errors and slots.working == 1 and not slots._queue
    busy.leave()

    step = slots.enter(never, STEP)
    step.call_started("a", never)
    with pytest.raises(Cancelled), step.waiting_for_person(check):
        assert step.holds == "waiting"
        other = slots.enter(never, OTHER)  # takes the working slot meanwhile
        raise Cancelled
    # The wait ended by the cancel, and waiting for the slot again ends at once (check raises): nothing is held.
    assert step.holds is None and (slots.working, slots.waiting) == (1, 0) and not slots._queue
    other.leave()
    step.leave()
    assert free(slots)


def test_l1_steps_get_a_working_slot_first_come_first_served() -> None:
    slots = DepthSlots(1, 0)
    holder = slots.enter(never, ("run_0", "a"))
    order: list[int] = []
    threads = []
    for number in range(1, 4):

        def take(number: int = number) -> None:
            slot = slots.enter(never, (f"run_{number}", "a"))
            order.append(number)
            time.sleep(0.02)
            slot.leave()

        threads.append(in_thread(take))
        eventually(lambda count=number: len(slots._queue) == count)
    holder.leave()
    for thread in threads:
        thread.join(5)
    assert order == [1, 2, 3] and free(slots)


def test_h1_another_steps_slot_is_never_acted_on() -> None:
    """Review H1: a thread that inherited a step's slot (a child run's step) finds no slot of its own."""
    slots = DepthSlots(1, 1)
    parent = slots.enter(never, STEP)
    token = step_slots._STEP.set(parent)
    try:
        assert step_slots.current(STEP) is parent
        assert step_slots.current(("run_child", "agent")) is None
        with step_slots.waiting_for_person(never, ("run_child", "agent")) as retry:
            retry()
            assert parent.holds == "working" and parent.calls_waiting == 0
        step_slots.call_started(("run_child", "agent"), "c", never)
        assert parent.active == set()
        # Nothing below a step sees it at all.
        assert step_slots.without_slot(step_slots._STEP.get) is None
        assert step_slots._STEP.get() is parent
    finally:
        step_slots._STEP.reset(token)
    parent.leave()
    assert free(slots)


def test_a_step_without_workflow_tools_has_no_slot() -> None:
    assert step_slots.current(STEP) is None
    with step_slots.waiting_for_person(never, STEP) as retry:
        retry()
    step_slots.require_working(STEP, never)
    step_slots.call_started(STEP, "a", never)
    step_slots.call_finished(STEP, "a")
