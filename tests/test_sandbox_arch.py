"""The built-in Linux sandbox fails closed on every architecture other than x86-64 (docs/sandbox-arm64.md).

The seccomp filter and the probes use x86-64 system call numbers. On AArch64 (and any other architecture) the
sandbox must therefore never be available: the host's status check, the confinement script's own platform check
and the seccomp filter's architecture check each refuse it on their own. These tests check all three, the filter
with a small classic-BPF interpreter over every system call number, and on a real non-x86-64 machine (the CI job
on ``ubuntu-24.04-arm``) the real status and the real confinement script.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys

from orkexa.dynamic.errors import SandboxError
import pytest

from nodestro_runtime import builtin_sandbox, sandbox_confine, sandbox_probes
from nodestro_runtime.builtin_sandbox import LinuxSandbox, SandboxStatus

pytestmark = pytest.mark.real_sandbox_status  # the computer's real sandbox status (see conftest.py)

PID = 4242
ALLOW = sandbox_confine._RET_ALLOW
KILL = sandbox_confine._RET_KILL_PROCESS
EPERM = sandbox_confine._RET_ERRNO | errno.EPERM

#: AUDIT_ARCH_* values (include/uapi/linux/audit.h) of architectures the filter must kill.
OTHER_ARCHES = {
    "aarch64": 0xC00000B7,
    "arm": 0x40000028,
    "i386": 0x40000003,
    "aarch64-big-endian": 0x800000B7,
    "riscv64": 0xC00000F3,
    "ppc64le": 0xC0000015,
    "s390x": 0x80000016,
}
#: Dangerous AArch64 system calls (asm-generic/unistd.h): clone, clone3, execve, ptrace, socket, process_vm_readv,
#: bpf, perf_event_open, userfaultfd, keyctl, mount, unshare, personality, io_uring_setup.
AARCH64_DANGEROUS = (220, 435, 221, 117, 198, 270, 280, 241, 282, 219, 40, 97, 92, 425)


def run_filter(program: bytes, nr: int, arch: int, args: tuple[int, ...] = (0, 0, 0, 0, 0, 0)) -> int:
    """Evaluate the classic-BPF ``program`` on ``struct seccomp_data`` as the kernel would; returns the action."""
    padded = (*args, 0, 0, 0, 0, 0, 0)[:6]
    data = struct.pack("<iIQ6Q", nr, arch, 0, *[a & 0xFFFFFFFFFFFFFFFF for a in padded])
    insns = [struct.unpack("<HBBI", program[i : i + 8]) for i in range(0, len(program), 8)]
    acc, pc = 0, 0
    while True:
        code, jt, jf, k = insns[pc]
        if code == sandbox_confine._LD_W_ABS:
            acc = struct.unpack_from("<I", data, k)[0]
            pc += 1
        elif code == sandbox_confine._RET_K:
            return k
        elif code in (sandbox_confine._JEQ_K, sandbox_confine._JGE_K, sandbox_confine._JSET_K):
            taken = {
                sandbox_confine._JEQ_K: acc == k,
                sandbox_confine._JGE_K: acc >= k,
                sandbox_confine._JSET_K: bool(acc & k),
            }[code]
            pc += 1 + (jt if taken else jf)
        else:  # pragma: no cover - the program uses no other instruction
            raise AssertionError(f"unknown BPF instruction {code:#x}")


@pytest.mark.parametrize("arch", sorted(OTHER_ARCHES))
def test_the_filter_kills_every_system_call_of_another_architecture(arch: str) -> None:
    program = sandbox_confine.seccomp_program(PID)
    value = OTHER_ARCHES[arch]
    for nr in [*range(0, 1100), *AARCH64_DANGEROUS, 0x40000000 | 39, 0x7FFFFFFF, -1]:
        assert run_filter(program, nr, value) == KILL, (arch, nr)


def test_the_filter_on_x86_64_denies_its_table_and_kills_x32() -> None:
    program = sandbox_confine.seccomp_program(PID)
    x86 = sandbox_confine._AUDIT_ARCH_X86_64
    for name, nr in sandbox_confine.DENIED.items():
        assert run_filter(program, nr, x86) == EPERM, name
    assert run_filter(program, sandbox_confine._CLONE3, x86) == sandbox_confine._RET_ERRNO | errno.ENOSYS
    assert run_filter(program, sandbox_confine._SOCKET, x86) == sandbox_confine._RET_ERRNO | errno.EAFNOSUPPORT
    assert run_filter(program, sandbox_confine._CLONE, x86, (17,)) == EPERM  # fork-like clone (SIGCHLD)
    assert run_filter(program, sandbox_confine._CLONE, x86, (0x3D0F00,)) == ALLOW  # a thread (CLONE_THREAD set)
    assert run_filter(program, sandbox_confine._KILL, x86, (PID,)) == ALLOW
    assert run_filter(program, sandbox_confine._KILL, x86, (1,)) == EPERM
    assert run_filter(program, 39, x86) == ALLOW  # getpid
    for nr in (0x40000000 | 39, 0x40000000 | 59, 0x7FFFFFFF, -1):  # x32 and anything above it
        assert run_filter(program, nr, x86) == KILL, nr


def test_the_x86_64_numbers_match_the_kernel_headers() -> None:
    header = Path("/usr/include/x86_64-linux-gnu/asm/unistd_64.h")
    if not header.exists():
        header = Path("/usr/include/asm/unistd_64.h")
    if not header.exists():
        pytest.skip("no x86-64 kernel headers on this machine")
    numbers = {m[0]: int(m[1]) for m in re.findall(r"#define __NR_(\w+)\s+(\d+)", header.read_text())}
    expected = dict(sandbox_confine.DENIED)
    expected.update(clone=sandbox_confine._CLONE, clone3=sandbox_confine._CLONE3, socket=sandbox_confine._SOCKET,
                    ioctl=sandbox_confine._IOCTL, kill=sandbox_confine._KILL, tgkill=sandbox_confine._TGKILL,
                    rt_sigqueueinfo=sandbox_confine._RT_SIGQUEUEINFO, prlimit64=sandbox_confine._PRLIMIT64,
                    rt_tgsigqueueinfo=sandbox_confine._RT_TGSIGQUEUEINFO,
                    landlock_create_ruleset=sandbox_confine._SYS_LANDLOCK_CREATE_RULESET,
                    landlock_add_rule=sandbox_confine._SYS_LANDLOCK_ADD_RULE,
                    landlock_restrict_self=sandbox_confine._SYS_LANDLOCK_RESTRICT_SELF)  # fmt: skip
    wrong = {name: (nr, numbers.get(name)) for name, nr in expected.items() if numbers.get(name) != nr}
    assert not wrong, wrong


def test_aarch64_is_refused_by_the_host_and_by_the_confinement(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (builtin_sandbox, sandbox_confine):
        monkeypatch.setattr(module.platform, "machine", lambda: "aarch64")
        monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(builtin_sandbox, "os_release", lambda path="": ("ubuntu", "24.04"))
    status = builtin_sandbox._check()
    assert not status.available and status.reason == "platform"
    assert not LinuxSandbox.system_supported()
    with pytest.raises(sandbox_confine.Unavailable, match="needs Linux x86-64"):
        sandbox_confine._check_platform()


def test_the_probes_refuse_to_run_on_another_architecture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LinuxSandbox, "status", staticmethod(lambda refresh=False: SandboxStatus(True, "", "", "", 4)))
    monkeypatch.setattr(sandbox_probes.platform, "machine", lambda: "aarch64")
    with pytest.raises(SandboxError, match="written for x86-64"):
        sandbox_probes.run_all()


@pytest.mark.skipif(platform.machine() == "x86_64", reason="checks the real behavior on a non-x86-64 machine")
def test_the_real_machine_fails_closed(tmp_path: Path) -> None:  # pragma: no cover - runs on ubuntu-24.04-arm
    status = LinuxSandbox.status(refresh=True)
    assert not status.available and status.reason == "platform", status
    # The confinement script on its own (as if the host check were skipped) refuses before any code runs.
    marker = tmp_path / "ran"
    runner = tmp_path / "runner.py"
    runner.write_text(f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8")
    config = {"parent": 0, "runner": str(runner), "write": [], "forbidden": [], "probe_file": str(tmp_path / "p"),
              "limits": {"memory_bytes": 1 << 30, "cpu_seconds": 10, "file_bytes": 1 << 20}}  # fmt: skip
    done = subprocess.run([sys.executable, "-I", "-B", str(sandbox_confine.__file__), json.dumps(config)],
                          capture_output=True, text=True, timeout=60)  # fmt: skip
    assert done.returncode == sandbox_confine.EXIT_UNAVAILABLE, done
    assert f"{sandbox_confine.MARKER} platform" in done.stderr
    assert not marker.exists()
