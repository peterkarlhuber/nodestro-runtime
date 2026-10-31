"""Prototype of a built-in sandbox for Linux (plan PR-13, ADR-0006 point 7): bubblewrap plus a seccomp filter.

**A prototype, not an isolating sandbox.** :func:`nodestro_runtime.sandbox_policy.isolation_of` keeps classifying
it as ``"none"``, so untrusted code is still refused with it. It exists so the acceptance tests of ADR-0006 can be run
against a candidate and its gaps can be measured (``python -m nodestro_runtime.builtin_sandbox`` prints the report).
A candidate becomes isolating only for an operating system on which every security and compatibility test passed,
including on a clean machine with the desktop bundle; that decision is a separate, reviewed change.

What one call gets:

- new user, PID, network, IPC, UTS and cgroup namespaces (``bwrap --unshare-all``): no network interface except an
  unconfigured loopback, no view of the host's processes, no capabilities, user ``nobody``;
- a root file system built from scratch: only the interpreter, its standard library and site-packages, the shared
  libraries they link against (found with ``ldd``), Orkexa's runner, the call's files (read-only) and a small
  ``/tmp`` (tmpfs with a size limit). No home directory, data directory, ``/etc`` or other programs;
- an empty environment (``--clearenv``);
- a seccomp filter (x86-64 only) that refuses new processes (``fork``, ``vfork``, ``clone`` without
  ``CLONE_THREAD``), sockets other than ``AF_UNIX``, ``ptrace``, mounts, namespaces, keyrings, ``bpf``,
  ``perf_event_open``, ``userfaultfd``, ``io_uring`` and other kernel interfaces tools never need;
- Orkexa's runner limits (CPU time, address space, file size, no core dumps) and the host's wall-clock timeout;
  the process tree dies with the call (``--die-with-parent`` and the PID namespace).

Known gaps (see the report): no Landlock rules, no cgroup limits for memory or processes beyond the rlimits, timing
and cache side channels of the shared CPU, and distributions that restrict unprivileged user namespaces (Ubuntu 24.04
with AppArmor) need a profile for ``bwrap``.
"""

from __future__ import annotations

from collections.abc import Iterable
import errno
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import sysconfig
import time
from typing import ClassVar

import orkexa
from orkexa.dynamic import Sandbox
from orkexa.dynamic.errors import SandboxError

#: Where Orkexa's package (with the sandbox runner) appears inside the sandbox.
PACKAGE_TARGET = "/opt/orkexa/orkexa"
_PACKAGE_DIR = Path(orkexa.__file__).resolve().parent

# --- seccomp -----------------------------------------------------------------------------------------------------

_AUDIT_ARCH_X86_64 = 0xC000003E
_X32_SYSCALL_BIT = 0x40000000
_CLONE_THREAD = 0x00010000
_AF_UNIX = 1

_RET_ALLOW = 0x7FFF0000
_RET_KILL_PROCESS = 0x80000000
_RET_ERRNO = 0x00050000

_LD_W_ABS = 0x20  # BPF_LD | BPF_W | BPF_ABS
_JEQ_K = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_JGE_K = 0x35  # BPF_JMP | BPF_JGE | BPF_K
_JSET_K = 0x45  # BPF_JMP | BPF_JSET | BPF_K
_RET_K = 0x06  # BPF_RET | BPF_K

# struct seccomp_data: int nr; __u32 arch; __u64 instruction_pointer; __u64 args[6];
_OFFSET_NR = 0
_OFFSET_ARCH = 4
_OFFSET_ARG0 = 16  # low 32 bits on little-endian

#: x86-64 system calls refused with EPERM (numbers from asm/unistd_64.h).
_DENIED_X86_64: dict[str, int] = {
    "fork": 57,
    "vfork": 58,
    "ptrace": 101,
    "syslog": 103,
    "uselib": 134,
    "personality": 135,
    "vhangup": 153,
    "pivot_root": 155,
    "adjtimex": 159,
    "chroot": 161,
    "acct": 163,
    "settimeofday": 164,
    "mount": 165,
    "umount2": 166,
    "swapon": 167,
    "swapoff": 168,
    "reboot": 169,
    "iopl": 172,
    "ioperm": 173,
    "init_module": 175,
    "delete_module": 176,
    "quotactl": 179,
    "lookup_dcookie": 212,
    "clock_settime": 227,
    "kexec_load": 246,
    "add_key": 248,
    "request_key": 249,
    "keyctl": 250,
    "unshare": 272,
    "perf_event_open": 298,
    "name_to_handle_at": 303,
    "open_by_handle_at": 304,
    "setns": 308,
    "process_vm_readv": 310,
    "process_vm_writev": 311,
    "finit_module": 313,
    "kexec_file_load": 320,
    "bpf": 321,
    "userfaultfd": 323,
    "io_uring_setup": 425,
    "io_uring_enter": 426,
    "io_uring_register": 427,
    "open_tree": 428,
    "move_mount": 429,
    "fsopen": 430,
    "fsconfig": 431,
    "fsmount": 432,
    "fspick": 433,
    "pidfd_getfd": 438,
    "mount_setattr": 442,
}
_CLONE_X86_64 = 56
_CLONE3_X86_64 = 435
_SOCKET_X86_64 = 41


def _instruction(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("<HBBI", code, jt, jf, k)


def seccomp_filter(machine: str | None = None) -> bytes:
    """The compiled classic-BPF seccomp program (``struct sock_filter[]``) for ``bwrap --seccomp``.

    Raises :class:`SandboxError` on an architecture the prototype has no tested syscall table for."""
    machine = machine or platform.machine()
    if machine not in ("x86_64", "AMD64"):
        raise SandboxError(f"The built-in sandbox prototype has no seccomp filter for {machine}")
    eperm = _RET_ERRNO | errno.EPERM
    program = [
        _instruction(_LD_W_ABS, 0, 0, _OFFSET_ARCH),
        _instruction(_JEQ_K, 1, 0, _AUDIT_ARCH_X86_64),
        _instruction(_RET_K, 0, 0, _RET_KILL_PROCESS),
        _instruction(_LD_W_ABS, 0, 0, _OFFSET_NR),
        # x32 system calls (a second syscall table) are not allowed at all.
        _instruction(_JGE_K, 0, 1, _X32_SYSCALL_BIT),
        _instruction(_RET_K, 0, 0, _RET_KILL_PROCESS),
    ]
    for number in _DENIED_X86_64.values():
        program += [_instruction(_JEQ_K, 0, 1, number), _instruction(_RET_K, 0, 0, eperm)]
    # glibc falls back from clone3 (whose flags live in memory a filter cannot read) to clone.
    program += [_instruction(_JEQ_K, 0, 1, _CLONE3_X86_64), _instruction(_RET_K, 0, 0, _RET_ERRNO | errno.ENOSYS)]
    # clone: threads only (CLONE_THREAD set); a new process is refused.
    program += [
        _instruction(_JEQ_K, 0, 4, _CLONE_X86_64),
        _instruction(_LD_W_ABS, 0, 0, _OFFSET_ARG0),
        _instruction(_JSET_K, 0, 1, _CLONE_THREAD),
        _instruction(_RET_K, 0, 0, _RET_ALLOW),
        _instruction(_RET_K, 0, 0, eperm),
    ]
    # socket: AF_UNIX only (there is no network in the namespace anyway).
    program += [
        _instruction(_JEQ_K, 0, 4, _SOCKET_X86_64),
        _instruction(_LD_W_ABS, 0, 0, _OFFSET_ARG0),
        _instruction(_JEQ_K, 0, 1, _AF_UNIX),
        _instruction(_RET_K, 0, 0, _RET_ALLOW),
        _instruction(_RET_K, 0, 0, _RET_ERRNO | errno.EAFNOSUPPORT),
    ]
    program.append(_instruction(_RET_K, 0, 0, _RET_ALLOW))
    return b"".join(program)


_PING = (
    "def ping(text: str) -> str:\n"
    '    """Echo the text.\n\n    Args:\n        text: Any text.\n\n    Returns:\n        The same text.\n    """\n'
    "    return text\n"
)


# --- file system -------------------------------------------------------------------------------------------------


def _ldd(paths: Iterable[Path], ldd: str) -> set[Path]:
    """Shared libraries (and the dynamic loader) the ELF files in ``paths`` need, as absolute paths."""
    found: set[Path] = set()
    batch = [str(p) for p in paths]
    for start in range(0, len(batch), 200):
        completed = subprocess.run(
            [ldd, *batch[start : start + 200]], capture_output=True, text=True, check=False, env={"LC_ALL": "C"}
        )
        for line in completed.stdout.splitlines():
            line = line.strip()
            if line.endswith(":") or "not found" in line:  # a file header of a multi-file listing
                continue
            target = line.split("=>", 1)[1].strip() if "=>" in line else line
            target = target.split(" (", 1)[0]
            if target.startswith("/"):
                found.add(Path(target))
    return found


def _python_dirs() -> list[Path]:
    """The standard library and site-packages of this interpreter (each existing directory once)."""
    paths = sysconfig.get_paths()
    dirs: list[Path] = []
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        value = paths.get(key)
        if value and Path(value).is_dir() and Path(value) not in dirs:
            dirs.append(Path(value))
    return dirs


class BubblewrapSandbox(Sandbox):
    """Runs Orkexa's sandbox runner under ``bwrap`` with the root file system, namespaces and filter described in
    the module docstring. Not classified as isolating (prototype)."""

    #: Not a security boundary until the prototype has passed every acceptance test (ADR-0006 point 7).
    isolated: ClassVar[bool] = False

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        memory_mb: int = 512,
        max_output_chars: int = 65_536,
        tmp_mb: int = 16,
        bwrap: str | None = None,
        ldd: str | None = None,
    ) -> None:
        super().__init__(timeout=timeout, memory_mb=memory_mb, max_output_chars=max_output_chars)
        self.tmp_mb = tmp_mb
        self.bwrap = bwrap or shutil.which("bwrap") or "bwrap"
        self.ldd = ldd or shutil.which("ldd") or "ldd"
        self._filter = seccomp_filter()
        self._binds: list[str] | None = None

    @staticmethod
    def available(bwrap: str | None = None) -> bool:
        """Whether this computer can run the prototype (Linux x86-64, ``bwrap`` and ``ldd`` present, user
        namespaces allowed)."""
        if not sys.platform.startswith("linux") or platform.machine() != "x86_64":
            return False
        if shutil.which(bwrap or "bwrap") is None or shutil.which("ldd") is None:
            return False
        try:
            return BubblewrapSandbox(bwrap=bwrap, timeout=20.0).call(_PING, "ping", {"text": "ok"}) == "ok"
        except Exception:  # any failure means the prototype cannot run here
            return False

    def _bind_arguments(self) -> list[str]:
        if self._binds is not None:
            return self._binds
        executable = Path(sys.executable)
        real_executable = executable.resolve()
        python_dirs = _python_dirs()
        elf_files = [real_executable]
        for directory in python_dirs:
            elf_files += [p for p in directory.rglob("*.so*") if p.is_file()]
        libraries = _ldd(elf_files, self.ldd)
        if not libraries:
            raise SandboxError(f"Cannot list the shared libraries of {real_executable} with {self.ldd}")
        args: list[str] = []
        for library in sorted(libraries):
            args += ["--ro-bind", str(library.resolve()), str(library)]
        for directory in python_dirs:
            args += ["--ro-bind", str(directory.resolve()), str(directory)]
        args += ["--ro-bind", str(real_executable), str(executable)]
        venv_config = Path(sys.prefix) / "pyvenv.cfg"
        if sys.prefix != sys.base_prefix and venv_config.is_file():
            args += ["--ro-bind", str(venv_config.resolve()), str(venv_config)]
        args += ["--ro-bind", str(_PACKAGE_DIR), PACKAGE_TARGET]
        self._binds = args
        return args

    def command(self, workdir: Path, seccomp_fd: int) -> list[str]:
        """The ``bwrap`` command line for one run; the caller passes the filter on ``seccomp_fd``."""
        return [
            self.bwrap,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--cap-drop",
            "ALL",
            "--uid",
            "65534",
            "--gid",
            "65534",
            "--hostname",
            "sandbox",
            *self._bind_arguments(),
            "--ro-bind",
            str(workdir),
            "/work",
            "--size",
            str(self.tmp_mb * 1024 * 1024),
            "--tmpfs",
            "/tmp",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--setenv",
            "HOME",
            "/tmp",
            "--chdir",
            "/work",
            "--seccomp",
            str(seccomp_fd),
            sys.executable,
            "-I",
            "-B",
        ]

    def _execute(self, workdir: Path, request: str) -> str:
        read_fd, write_fd = os.pipe()
        try:
            # The filter is small (well below the pipe buffer), so writing it before bwrap starts cannot block.
            os.write(write_fd, self._filter)
            os.close(write_fd)
            write_fd = -1
            command = [*self.command(workdir, read_fd), f"{PACKAGE_TARGET}/dynamic/_sandbox_runner.py"]
            completed = subprocess.run(
                command,
                input=request,
                capture_output=True,
                text=True,
                env={},
                timeout=self.timeout,
                check=False,
                pass_fds=(read_fd,),
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"Timed out after {self.timeout:g} seconds") from exc
        except OSError as exc:
            raise SandboxError(f"Cannot start bwrap: {exc}") from exc
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
        if not completed.stdout.strip():
            raise SandboxError(f"Sandbox exited with code {completed.returncode}: {completed.stderr[-1000:]}")
        return completed.stdout


# --- report ------------------------------------------------------------------------------------------------------


def measure(sandbox: Sandbox, calls: int = 10) -> dict[str, float]:
    """Wall-clock times of trivial tool calls in milliseconds: the first call (with one-time setup such as listing
    the shared libraries) and the mean and worst of ``calls`` further calls."""
    started = time.perf_counter()
    sandbox.call(_PING, "ping", {"text": "x"})
    first = (time.perf_counter() - started) * 1000
    times: list[float] = []
    for _ in range(calls):
        started = time.perf_counter()
        sandbox.call(_PING, "ping", {"text": "x"})
        times.append((time.perf_counter() - started) * 1000)
    return {"first_ms": first, "mean_ms": sum(times) / len(times), "max_ms": max(times)}


def _report() -> str:  # pragma: no cover - manual tool, exercised by the tests through ``measure``
    """Timings of this computer (the host subprocess comparison is printed by the tests, which may create one)."""
    lines = ["| Check | Result |", "| --- | --- |"]
    available = BubblewrapSandbox.available()
    lines.append(f"| available on this computer | {'yes' if available else 'no'} |")
    if available:
        bwrap = measure(BubblewrapSandbox())
        lines.append(f"| first call, bubblewrap (one-time setup) | {bwrap['first_ms']:.0f} ms |")
        lines.append(f"| per call, bubblewrap (mean / max) | {bwrap['mean_ms']:.0f} ms / {bwrap['max_ms']:.0f} ms |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - manual tool
    del argv
    print(_report())
    return 0


__all__ = ["BubblewrapSandbox", "measure", "seccomp_filter"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
