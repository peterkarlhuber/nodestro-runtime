"""Stopping a running sandbox call when its run is cancelled (audit B-03).

A sandbox is shared by every run of a workflow (or of the whole tool library), so cancelling one run must not call
:meth:`nodestro_runtime.builtin_sandbox.LinuxSandbox.cancel`, which kills the calls of every run. Instead the engine
binds the run's cancel check to the agent's thread (:func:`bind`, a context variable that Orkexa copies into the
threads that run tool calls), and a sandbox that supports it polls :func:`requested` while one call runs:

- the built-in Linux sandbox kills the call's process group;
- the container sandboxes Nodestro builds (:class:`nodestro_runtime.container_images.StreamedSandbox`) remove the
  call's container (``docker rm -f``), which also ends the docker client.

Orkexa's ``SubprocessSandbox`` and ``DockerSandbox`` poll the same check (the context variable is Orkexa's own,
:mod:`orkexa.dynamic.lifecycle`, ORK-001): they kill the call's process group, or remove its container.
"""

from __future__ import annotations

from contextvars import Token

from orkexa.dynamic.lifecycle import CancelCheck, bind_cancel, cancel_requested, current_cancel, reset_cancel

Check = CancelCheck


def bind(check: Check | None) -> Token[Check | None]:
    """Bind the cancel check of the run whose tools the current thread (and the threads it starts) runs."""
    return bind_cancel(check)


def reset(token: Token[Check | None]) -> None:
    reset_cancel(token)


def current() -> Check | None:
    """The bound check, for a thread that watches a call on behalf of the caller (context variables are not
    inherited by new threads)."""
    return current_cancel()


def requested(check: Check | None = None) -> bool:
    """Whether the run of the current call was cancelled (``False`` outside a run; a broken check never
    cancels)."""
    return cancel_requested(check)


__all__ = ["Check", "bind", "current", "requested", "reset"]
