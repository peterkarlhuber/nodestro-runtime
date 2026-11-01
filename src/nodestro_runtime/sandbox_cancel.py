"""Stopping a running sandbox call when its run is cancelled (audit B-03).

A sandbox is shared by every run of a workflow (or of the whole tool library), so cancelling one run must not call
:meth:`nodestro_runtime.builtin_sandbox.LinuxSandbox.cancel`, which kills the calls of every run. Instead the engine
binds the run's cancel check to the agent's thread (:func:`bind`, a context variable that Orkexa copies into the
threads that run tool calls), and a sandbox that supports it polls :func:`requested` while one call runs:

- the built-in Linux sandbox kills the call's process group;
- the container sandboxes Nodestro builds (:class:`nodestro_runtime.container_images.StreamedSandbox`) remove the
  call's container (``docker rm -f``), which also ends the docker client.

Orkexa's own ``SubprocessSandbox`` and ``DockerSandbox`` do not poll it: their calls still end at the sandbox
timeout, and the agent stops at its next check after the call.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token

Check = Callable[[], bool]

_CHECK: ContextVar[Check | None] = ContextVar("nodestro_sandbox_cancel", default=None)


def bind(check: Check | None) -> Token[Check | None]:
    """Bind the cancel check of the run whose tools the current thread (and the threads it starts) runs."""
    return _CHECK.set(check)


def reset(token: Token[Check | None]) -> None:
    _CHECK.reset(token)


def current() -> Check | None:
    """The bound check, for a thread that watches a call on behalf of the caller (context variables are not
    inherited by new threads)."""
    return _CHECK.get()


def requested(check: Check | None = None) -> bool:
    """Whether the run of the current call was cancelled (``False`` outside a run)."""
    found = check if check is not None else _CHECK.get()
    if found is None:
        return False
    try:
        return bool(found())
    except Exception:  # a broken check must never stop a call by itself
        return False


__all__ = ["Check", "bind", "current", "requested", "reset"]
