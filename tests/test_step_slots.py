"""The working and waiting slots of the steps with workflow tools of one depth (RT-096)."""

from __future__ import annotations

import threading
import time

import pytest

from nodestro_runtime import step_slots
from nodestro_runtime.step_slots import DepthSlots


class Cancelled(Exception):
    pass


def never() -> None:
    return None


def eventually(condition: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():  # type: ignore[operator]
        assert time.monotonic() < deadline
        time.sleep(0.01)


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

    threads = [threading.Thread(target=step_slots.run_in_slot, args=(slots, never, step)) for _ in range(8)]
    for item in threads:
        item.start()
    for item in threads:
        item.join(10)
    assert most == 2 and (slots.working, slots.waiting) == (0, 0)


def test_after_the_wait_the_step_waits_for_a_working_slot() -> None:
    slots = DepthSlots(1, 1)
    first = slots.enter(never)
    done = threading.Event()
    release = threading.Event()

    def waiting_step() -> None:
        with first.waiting_for_person(never):
            release.wait(5)
        done.set()

    thread = threading.Thread(target=waiting_step)
    thread.start()
    eventually(lambda: first.holds == "waiting")
    second = slots.enter(never)
    release.set()
    time.sleep(0.1)
    assert not done.is_set() and first.holds is None and slots.working == 1  # still the second step's slot
    second.leave()
    thread.join(5)
    assert done.is_set() and first.holds == "working"
    first.leave()
    assert (slots.working, slots.waiting) == (0, 0)


def test_with_all_waiting_slots_taken_a_waiting_step_keeps_its_slot_and_parks_later() -> None:
    slots = DepthSlots(2, 1)
    parked = slots.enter(never)
    held = slots.enter(never)
    parked.calls_waiting = 1
    parked.park()
    assert parked.holds == "waiting"
    token = step_slots._STEP.set(held)
    try:
        with step_slots.waiting_for_person(never) as retry:
            # No waiting slot is free: the step keeps its working slot, never beyond a limit.
            assert held.holds == "working" and (slots.working, slots.waiting) == (1, 1)
            parked.leave()  # the parked step ends: its waiting slot is free
            retry()  # what the wait calls while it polls
            assert held.holds == "waiting" and (slots.working, slots.waiting) == (0, 1)
    finally:
        step_slots._STEP.reset(token)
    assert held.holds == "working" and (slots.working, slots.waiting) == (1, 0)
    held.leave()
    assert (slots.working, slots.waiting) == (0, 0)


def test_parallel_calls_of_one_step_park_it_once_and_it_goes_on_after_the_last() -> None:
    slots = DepthSlots(1, 2)
    step = slots.enter(never)
    with step.waiting_for_person(never):
        with step.waiting_for_person(never):
            assert (step.calls_waiting, slots.waiting, slots.working) == (2, 1, 0)
        assert step.holds == "waiting"  # one call still waits
    assert step.holds == "working" and (slots.working, slots.waiting) == (1, 0)
    step.leave()


def test_a_cancelled_run_stops_waiting_for_a_slot_and_holds_none() -> None:
    slots = DepthSlots(1, 1)
    busy = slots.enter(never)
    cancelled = threading.Event()

    def check() -> None:
        if cancelled.is_set():
            raise Cancelled

    errors: list[BaseException] = []

    def start() -> None:
        try:
            slots.enter(check)
        except Cancelled as exc:
            errors.append(exc)

    thread = threading.Thread(target=start)
    thread.start()
    time.sleep(0.1)
    cancelled.set()
    thread.join(5)
    assert errors and slots.working == 1
    with busy.waiting_for_person(never):
        pass
    with pytest.raises(Cancelled), busy.waiting_for_person(check):
        raise Cancelled
    assert busy.holds is None and (slots.working, slots.waiting) == (0, 0)  # a cancelled step unwinds without one
    busy.leave()
    assert (slots.working, slots.waiting) == (0, 0)


def test_a_step_without_workflow_tools_has_no_slot() -> None:
    assert step_slots.current() is None
    with step_slots.waiting_for_person(never) as retry:
        retry()
