"""A worker whose lease renewal stalls on demand, for the tests that expire a live worker's lease and reap its run.

Expiring the lease with an ``UPDATE`` while the worker keeps renewing it is a race: a heartbeat (or an ``owns`` check
before a model or tool call) that is already on its way to the database renews the lease right after the expiry, the
reaper then finds no expired job and the test fails (``reap`` returned ``[]``; nodestro-runtime master 6b65b2e, pg-b
3.11, 2026-11-21). A real stall cannot renew: :class:`StalledRenewal` holds every renewal of one queue while the test
expires and reaps, and waits for a renewal in flight to end first. When the block ends, the held renewals reach the
database, find the job interrupted and the worker learns that its lease is lost, as after a real stall.
"""

from __future__ import annotations

from collections.abc import Callable
import threading
from types import TracebackType
from typing import Any

#: The JobQueue methods that renew a running job's lease (``owns`` goes through ``heartbeat``).
RENEWALS = ("heartbeat", "report")


class StalledRenewal:
    """Wraps the lease renewals of ``queue`` (the worker's own JobQueue); ``with stall:`` is the stalled heartbeat."""

    def __init__(self, queue: Any) -> None:
        self._gate = threading.Lock()
        for name in RENEWALS:
            setattr(queue, name, self._held(getattr(queue, name)))

    def _held(self, renew: Callable[..., Any]) -> Callable[..., Any]:
        def held(*args: Any, **kwargs: Any) -> Any:
            with self._gate:
                return renew(*args, **kwargs)

        return held

    def __enter__(self) -> StalledRenewal:
        if not self._gate.acquire(timeout=10):  # a renewal in flight ends within milliseconds
            raise AssertionError("a lease renewal did not end within 10 s")
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        self._gate.release()
