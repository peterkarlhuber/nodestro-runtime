"""The built-in Linux sandbox: runs tool code isolated without Docker, Bubblewrap or any other installed tool.

Each call starts the interpreter that runs the tool (Nodestro's own or a Python environment's) as a new process
that confines itself before it reads any tool code (:mod:`nodestro_runtime.sandbox_confine`):

- **Landlock** (Linux 6.7+, ABI 4): read-only access to the interpreter, the system's shared libraries and the
  call's working directory; write access only to the call's private ``tmp`` directory; nothing else of the file
  system, so no home directory, no Nodestro data directory, no other call's files and no ``/proc`` of other
  processes; no TCP. With ABI 6 (Linux 6.12) also no abstract UNIX sockets and no signals out of the sandbox.
- **seccomp**: no new processes and no ``execve``, no sockets of any kind (no network, loopback, DNS, D-Bus or
  display server), no ``ptrace``, no System V IPC, no signals, priority, affinity or limit changes aimed at another
  process, no namespaces, mounts, keyrings, BPF, ``io_uring`` and other kernel interfaces.
- **Resource limits**: address space, CPU time, file size, open files, processes (also threads); the host adds a
  wall-clock timeout and a limit for the total size of ``tmp``, and kills the process on timeout or when the run
  of the call is cancelled (:mod:`nodestro_runtime.sandbox_cancel`).
- **An empty environment**: no API keys, tokens or other variables of the Nodestro process.

It needs no user namespaces, so Ubuntu's AppArmor restriction of unprivileged user namespaces does not affect it,
and no setuid helper. :meth:`LinuxSandbox.status` checks the computer once and says why the sandbox is not
available; the policy (:mod:`nodestro_runtime.sandbox_policy`) counts it as isolating only on the Linux systems on
which the security and compatibility tests passed (:data:`TESTED_SYSTEMS`). Every run checks the confinement again
from the inside; a run whose confinement fails never executes tool code.

Limits (see ADR-0006): it shares the kernel with the computer (a kernel bug can break any Linux sandbox), file
metadata outside the allowed paths (``stat``, existence of a guessed path) is not hidden, the CPU's timing and cache
side channels remain, and the ``tmp`` size limit is checked by polling, so a run can exceed it briefly before it is
killed.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import time
from typing import ClassVar

import orkexa
from orkexa.dynamic import BoundedOutput, Sandbox, communicate_bounded, response_limit
from orkexa.dynamic.errors import SandboxError

from . import sandbox_cancel, sandbox_confine

#: Linux distributions (``ID`` and ``VERSION_ID`` of ``/etc/os-release``) on which every security and
#: compatibility test of the built-in sandbox passed on x86-64, with the distribution's own kernel and AppArmor.
TESTED_SYSTEMS: frozenset[tuple[str, str]] = frozenset({("ubuntu", "24.04"), ("debian", "13")})

#: Most files and folders a call may create in its ``tmp`` (an empty file uses no blocks, but an inode).
MAX_TMP_ENTRIES = 10_000
_RUNNER = Path(orkexa.__file__).resolve().parent / "dynamic" / "_sandbox_runner.py"
_CONFINE = Path(sandbox_confine.__file__).resolve()
#: Variables every sandbox process gets; nothing is inherited from Nodestro's environment.
BASE_ENV = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}  # fmt: skip

_PING = (
    "def ping(text: str) -> str:\n"
    '    """Echo the text.\n\n    Args:\n        text: Any text.\n\n    Returns:\n        The same text.\n    """\n'
    "    return text\n"
)


def os_release(path: str = "/etc/os-release") -> tuple[str, str]:
    """``ID`` and ``VERSION_ID`` of the running distribution ("", "" if unknown)."""
    values: dict[str, str] = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    except OSError:
        return "", ""
    return values.get("ID", ""), values.get("VERSION_ID", "")


@dataclass(frozen=True)
class SandboxStatus:
    """Whether the built-in sandbox can isolate tool code on this computer.

    ``reason`` is "" when ``available``; otherwise one of ``platform`` (not Linux x86-64), ``untested-system`` (a
    distribution the tests have not covered), ``landlock`` / ``seccomp`` (kernel feature missing or disabled),
    ``root`` (Nodestro runs as root), ``paths`` (the interpreter lives in a protected directory), ``verify``
    (the confinement did not hold) or
    ``failed`` (the self-test did not run)."""

    available: bool
    reason: str
    detail: str
    system: str
    landlock_abi: int


_STATUS: SandboxStatus | None = None
_STATUS_LOCK = threading.Lock()


class LinuxSandbox(Sandbox):
    """Runs Orkexa's sandbox runner confined by Landlock, seccomp and resource limits (module docstring).

    Args:
        timeout: Wall-clock limit per call in seconds.
        memory_mb: Address-space limit.
        tmp_mb: Limit for the total size of the call's ``tmp`` directory (and for one file).
        python: Interpreter, e.g. a Python environment's; defaults to Nodestro's own.
        env: Extra variables (never secrets).
        read: Extra directories the code may read (e.g. a Python environment).
        protected: Paths that must never become readable (Nodestro's data directory); the home directory and the
            temp root are always protected.
    """

    isolated: ClassVar[bool] = True

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        memory_mb: int = 1024,
        max_output_chars: int = 65_536,
        tmp_mb: int = 64,
        python: str | None = None,
        env: dict[str, str] | None = None,
        read: tuple[str, ...] = (),
        protected: tuple[str, ...] = (),
    ) -> None:
        super().__init__(timeout=timeout, memory_mb=memory_mb, max_output_chars=max_output_chars)
        self.tmp_mb = tmp_mb
        self.python = python or sys.executable
        self.env = {**(env or {}), **BASE_ENV}
        self.read = tuple(read)
        self.protected = tuple(protected)
        #: The calls running now; :meth:`cancel` kills them.
        self._processes: set[subprocess.Popen[bytes]] = set()
        self._lock = threading.Lock()
        #: Tests only: require a newer Landlock ABI than the kernel has, to check that the run is refused.
        self._min_landlock_abi = 0

    # --- availability --------------------------------------------------------------------------------------------

    @staticmethod
    def status(*, refresh: bool = False) -> SandboxStatus:
        """Check this computer once (cached): platform, tested distribution, kernel features and a confined
        self-test call."""
        global _STATUS
        with _STATUS_LOCK:
            if _STATUS is None or refresh:
                _STATUS = _check()
            return _STATUS

    @staticmethod
    def system_supported() -> bool:
        """Whether this is one of the tested distributions (does not check the kernel)."""
        return platform.machine() == "x86_64" and sys.platform.startswith("linux") and os_release() in TESTED_SYSTEMS

    # --- runs ----------------------------------------------------------------------------------------------------

    def cancel(self) -> None:
        """Kill every call this sandbox is running, of every run that uses it.

        Cancelling one run stops only that run's call (:mod:`nodestro_runtime.sandbox_cancel`)."""
        with self._lock:
            for process in self._processes:
                _kill(process)

    def _execute(self, workdir: Path, request: str) -> str:
        tmp = workdir / "tmp"
        tmp.mkdir(mode=0o700)
        probe = workdir.parent / f"{workdir.name}.probe"
        probe.write_bytes(b"outside")
        config = {
            "parent": os.getpid(),
            "runner": str(_RUNNER),
            # Orkexa's runner and ``orkexa.tools`` (also for a Python environment's interpreter, which lacks them).
            "read": [str(_RUNNER.parents[1]), *self.read],
            "write": [str(tmp)],
            "forbidden": [str(Path.home()), tempfile.gettempdir(), *self.protected],
            "probe_file": str(probe),
            "min_landlock_abi": self._min_landlock_abi,
            "limits": {
                "memory_bytes": self.memory_mb * 1024 * 1024,
                "cpu_seconds": int(self.timeout) + 1,
                "file_bytes": self.tmp_mb * 1024 * 1024,
            },
        }
        env = {**self.env, "TMPDIR": str(tmp), "HOME": str(tmp)}
        try:
            process = subprocess.Popen(
                [self.python, "-I", "-B", str(_CONFINE), json.dumps(config)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workdir,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            probe.unlink(missing_ok=True)
            raise SandboxError(f"Cannot start the sandbox interpreter {self.python}: {exc}") from exc
        with self._lock:
            self._processes.add(process)
        result: dict[str, BoundedOutput] = {}
        max_stdout = response_limit(self.max_output_chars)

        def communicate() -> None:
            # Bounded: the confined code can write to the inherited stdout directly, and the child's memory
            # limit does not cover what this (host) process buffers.
            result["output"] = communicate_bounded(
                process, request.encode("utf-8"), max_stdout=max_stdout, on_overflow=lambda: _kill(process)
            )

        reader = threading.Thread(target=communicate, daemon=True)
        reader.start()
        deadline = time.monotonic() + self.timeout
        limit = self.tmp_mb * 1024 * 1024
        problem = ""
        try:
            while reader.is_alive():
                reader.join(0.05)
                if not reader.is_alive():
                    break
                if sandbox_cancel.requested():
                    # Only this call: the sandbox is shared with other runs (see :mod:`.sandbox_cancel`).
                    problem = "The run was cancelled; the call was stopped"
                elif time.monotonic() > deadline:
                    problem = f"Timed out after {self.timeout:g} seconds"
                else:
                    size, entries = _usage(tmp)
                    if size > limit:
                        problem = f"The temporary files exceeded the limit of {self.tmp_mb} MB"
                    elif entries > MAX_TMP_ENTRIES:
                        problem = f"The temporary files exceeded the limit of {MAX_TMP_ENTRIES} files"
                if problem:
                    _kill(process)
                    reader.join(5)
                    break
        finally:
            _kill(process)  # no-op if it has exited; the process cannot have children
            with self._lock:
                self._processes.discard(process)
            probe.unlink(missing_ok=True)
        output = result.get("output")
        if not problem and output is not None and output.overflow:
            problem = f"The sandbox wrote more than {max_stdout} bytes of output; the run was stopped"
        if problem:
            raise SandboxError(problem)
        out, err = (output.stdout, output.stderr) if output is not None else (b"", b"")
        stderr = err.decode("utf-8", "replace")
        if process.returncode == sandbox_confine.EXIT_UNAVAILABLE and sandbox_confine.MARKER in stderr:
            detail = stderr.split(sandbox_confine.MARKER, 1)[1].strip().splitlines()[0]
            raise SandboxError(f"The built-in sandbox could not confine the run, nothing was executed ({detail})")
        if process.returncode is not None and process.returncode < 0 and not out.strip():
            raise SandboxError(f"Sandbox process was killed by signal {-process.returncode}")
        if not out.strip():
            raise SandboxError(f"Sandbox exited with code {process.returncode}: {stderr[-1000:]}")
        return out.decode("utf-8", "replace")


def _kill(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, 9)
        except OSError:
            process.kill()
        process.wait()


def _usage(directory: Path) -> tuple[int, int]:
    """Total size (allocated blocks) and number of entries below ``directory``, without following links."""
    total = count = 0
    stack = [str(directory)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    count += 1
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_blocks * 512
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count


def _check() -> SandboxStatus:
    system = " ".join(os_release())
    if not sys.platform.startswith("linux") or platform.machine() != "x86_64":
        return SandboxStatus(False, "platform", f"needs Linux x86-64, this is {sys.platform}", system, 0)
    abi = sandbox_confine.landlock_abi()
    if not LinuxSandbox.system_supported():
        return SandboxStatus(
            False,
            "untested-system",
            f"the built-in sandbox is tested on Ubuntu 24.04 and Debian 13, this is {system or 'unknown'}",
            system,
            abi,
        )
    try:
        result = LinuxSandbox(timeout=30.0).call(_PING, "ping", {"text": "ok"})
    except Exception as exc:
        text = str(exc)
        reason = "failed"
        for code in ("landlock", "seccomp", "paths", "verify", "platform", "limits", "root"):
            if f"({code}:" in text:
                reason = code
        return SandboxStatus(False, reason, text, system, abi)
    if result != "ok":
        return SandboxStatus(False, "failed", f"unexpected self-test result {result!r}", system, abi)
    return SandboxStatus(True, "", "", system, abi)


def measure(sandbox: Sandbox, calls: int = 10) -> dict[str, float]:
    """Wall-clock times of trivial tool calls in milliseconds: the first call and the mean and worst of ``calls``
    further calls."""
    started = time.perf_counter()
    sandbox.call(_PING, "ping", {"text": "x"})
    first = (time.perf_counter() - started) * 1000
    times: list[float] = []
    for _ in range(calls):
        started = time.perf_counter()
        sandbox.call(_PING, "ping", {"text": "x"})
        times.append((time.perf_counter() - started) * 1000)
    return {"first_ms": first, "mean_ms": sum(times) / len(times), "max_ms": max(times)}


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - manual tool
    """``python -m nodestro_runtime.builtin_sandbox``: print this computer's status and timings."""
    del argv
    status = LinuxSandbox.status()
    print(json.dumps(status.__dict__, indent=2))
    if status.available:
        print(json.dumps(measure(LinuxSandbox()), indent=2))
    return 0 if status.available else 1


__all__ = ["TESTED_SYSTEMS", "LinuxSandbox", "SandboxStatus", "measure", "os_release"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
