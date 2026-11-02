"""Confines the current process before it runs tool code: the first half of the built-in Linux sandbox.

**Runs inside the sandbox process, before any tool code.** :class:`nodestro_runtime.builtin_sandbox.LinuxSandbox`
starts a fresh interpreter with this file and a JSON configuration as its only argument::

    python -I -B sandbox_confine.py '{"runner": ..., "write": [...], "forbidden": [...], "limits": {...}}'

The file uses only the standard library and never imports Nodestro. In this order it:

1. checks that it runs on Linux x86-64 and that the kernel offers Landlock ABI 4 or newer and seccomp filters;
2. sets resource limits (memory, CPU time, file size, open files, no new processes, no core dumps);
3. collects the directories the interpreter needs (its prefix, standard library and ``sys.path``) and refuses to
   start if one of them is, or contains, a forbidden path (home directory, Nodestro data directory, temp root);
4. applies a Landlock ruleset: read-only access to the interpreter, the shared system libraries, time zone data and
   the call's working directory; write access only below the call's private ``tmp`` directory; no TCP; on kernels
   with Landlock ABI 6, no abstract UNIX sockets and no signals to processes outside the sandbox;
5. sets ``no_new_privs`` and installs a seccomp filter (:func:`seccomp_program`) that refuses new processes,
   ``execve``, every socket, IPC with other processes, signals and resource changes aimed at other processes,
   ``ptrace`` and kernel interfaces tools never need;
6. verifies the confinement from the inside (a file outside the allowed paths, a socket and a new process must all be
   refused) and only then runs Orkexa's sandbox runner, which reads the request from stdin.

If any step fails, the process prints ``nodestro-sandbox-error: <code>: <detail>`` on stderr and exits with
:data:`EXIT_UNAVAILABLE` before any tool code is read. Nothing is ever run without the confinement.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import platform
import runpy
import socket
import struct
import sys

EXIT_UNAVAILABLE = 97
MARKER = "nodestro-sandbox-error:"
#: The oldest Landlock ABI the sandbox accepts (Linux 6.7; Ubuntu 24.04 ships 6.8 with ABI 4).
MIN_LANDLOCK_ABI = 4

# --- Landlock (include/uapi/linux/landlock.h) --------------------------------------------------------------------

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1

FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13  # ABI 2
FS_TRUNCATE = 1 << 14  # ABI 3
FS_IOCTL_DEV = 1 << 15  # ABI 5
NET_BIND_TCP = 1 << 0  # ABI 4
NET_CONNECT_TCP = 1 << 1
SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
SCOPE_SIGNAL = 1 << 1

#: Rights that may be granted on a single file (the others only make sense on directories).
_FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV
_READ = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR


def handled_fs(abi: int) -> int:
    """Every file system right the kernel's Landlock ABI knows; all of them are denied unless a rule grants them."""
    rights = (1 << 13) - 1
    if abi >= 2:
        rights |= FS_REFER
    if abi >= 3:
        rights |= FS_TRUNCATE
    if abi >= 5:
        rights |= FS_IOCTL_DEV
    return rights


#: System directories with shared libraries and data the interpreter and compiled packages may load. None of them
#: holds user data or secrets on the supported distributions.
SYSTEM_READ = (
    "/lib",
    "/lib64",
    "/usr/lib",
    "/usr/lib64",
    "/usr/share/zoneinfo",
    "/etc/ld.so.cache",
    "/etc/localtime",
)
#: Device files: /dev/null may be written; the random sources only read.
DEVICES = (("/dev/null", FS_READ_FILE | FS_WRITE_FILE), ("/dev/urandom", FS_READ_FILE), ("/dev/random", FS_READ_FILE))

# --- seccomp (x86-64) --------------------------------------------------------------------------------------------

_AUDIT_ARCH_X86_64 = 0xC000003E
_X32_SYSCALL_BIT = 0x40000000
_CLONE_THREAD = 0x00010000
_TIOCSTI = 0x5412
_TIOCLINUX = 0x541C

_RET_ALLOW = 0x7FFF0000
_RET_KILL_PROCESS = 0x80000000
_RET_ERRNO = 0x00050000

_LD_W_ABS = 0x20
_JEQ_K = 0x15
_JGE_K = 0x35
_JSET_K = 0x45
_RET_K = 0x06

_OFFSET_NR = 0
_OFFSET_ARCH = 4
_OFFSET_ARG0 = 16  # low 32 bits on little-endian
_OFFSET_ARG1 = 24

#: x86-64 system calls refused with EPERM (numbers from asm/unistd_64.h).
DENIED: dict[str, int] = {
    "fork": 57,
    "vfork": 58,
    "execve": 59,
    "execveat": 322,
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
    "fanotify_init": 300,
    "name_to_handle_at": 303,
    "open_by_handle_at": 304,
    "setns": 308,
    "process_vm_readv": 310,
    "process_vm_writev": 311,
    "kcmp": 312,
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
    # Signals and scheduling aimed at a process by pid file descriptor.
    "pidfd_open": 434,
    "pidfd_send_signal": 424,
    "tkill": 200,
    "process_madvise": 440,
    "process_mrelease": 448,
    # Other processes of the same user: priorities, CPU affinity, memory placement, futex lists.
    "setpriority": 141,
    "sched_setparam": 142,
    "sched_setscheduler": 144,
    "sched_setaffinity": 203,
    "sched_setattr": 314,
    "ioprio_set": 251,
    "migrate_pages": 256,
    "move_pages": 279,
    "get_robust_list": 274,
    # System V IPC is shared by every process of the user (no IPC namespace).
    "shmget": 29,
    "shmat": 30,
    "shmctl": 31,
    "semget": 64,
    "semop": 65,
    "semctl": 66,
    "shmdt": 67,
    "msgget": 68,
    "msgsnd": 69,
    "msgrcv": 70,
    "msgctl": 71,
    "semtimedop": 220,
}
_CLONE = 56
_CLONE3 = 435
_SOCKET = 41
_IOCTL = 16
_KILL = 62
_TGKILL = 234
_RT_SIGQUEUEINFO = 129
_RT_TGSIGQUEUEINFO = 297
_PRLIMIT64 = 302


def _insn(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("<HBBI", code, jt, jf, k)


def seccomp_program(pid: int) -> bytes:
    """The classic-BPF seccomp program for the sandbox process ``pid`` (``struct sock_filter[]``).

    Signals (``kill``, ``tgkill``, ``rt_sigqueueinfo``, ``rt_tgsigqueueinfo``) and ``prlimit64`` are allowed only
    for ``pid`` itself (``prlimit64`` also for 0, "this process"), so the code cannot signal or re-limit other
    processes of the user."""
    eperm = _RET_ERRNO | errno.EPERM
    program = [
        _insn(_LD_W_ABS, 0, 0, _OFFSET_ARCH),
        _insn(_JEQ_K, 1, 0, _AUDIT_ARCH_X86_64),
        _insn(_RET_K, 0, 0, _RET_KILL_PROCESS),
        _insn(_LD_W_ABS, 0, 0, _OFFSET_NR),
        _insn(_JGE_K, 0, 1, _X32_SYSCALL_BIT),  # the x32 syscall table is not allowed at all
        _insn(_RET_K, 0, 0, _RET_KILL_PROCESS),
    ]
    for number in DENIED.values():
        program += [_insn(_JEQ_K, 0, 1, number), _insn(_RET_K, 0, 0, eperm)]
    # glibc falls back from clone3 (whose flags live in memory a filter cannot read) to clone.
    program += [_insn(_JEQ_K, 0, 1, _CLONE3), _insn(_RET_K, 0, 0, _RET_ERRNO | errno.ENOSYS)]
    # clone: threads only (CLONE_THREAD set); a new process is refused.
    program += [
        _insn(_JEQ_K, 0, 4, _CLONE),
        _insn(_LD_W_ABS, 0, 0, _OFFSET_ARG0),
        _insn(_JSET_K, 0, 1, _CLONE_THREAD),
        _insn(_RET_K, 0, 0, _RET_ALLOW),
        _insn(_RET_K, 0, 0, eperm),
    ]
    # No sockets of any family: no network, no loopback, no DNS, no D-Bus, X11 or other local services.
    program += [_insn(_JEQ_K, 0, 1, _SOCKET), _insn(_RET_K, 0, 0, _RET_ERRNO | errno.EAFNOSUPPORT)]
    # ioctl: no input injection into a terminal.
    program += [
        _insn(_JEQ_K, 0, 5, _IOCTL),
        _insn(_LD_W_ABS, 0, 0, _OFFSET_ARG1),
        _insn(_JEQ_K, 2, 0, _TIOCSTI),
        _insn(_JEQ_K, 1, 0, _TIOCLINUX),
        _insn(_RET_K, 0, 0, _RET_ALLOW),
        _insn(_RET_K, 0, 0, eperm),
    ]
    # Signals only to this process.
    for number in (_KILL, _TGKILL, _RT_SIGQUEUEINFO, _RT_TGSIGQUEUEINFO):
        program += [
            _insn(_JEQ_K, 0, 4, number),
            _insn(_LD_W_ABS, 0, 0, _OFFSET_ARG0),
            _insn(_JEQ_K, 0, 1, pid),
            _insn(_RET_K, 0, 0, _RET_ALLOW),
            _insn(_RET_K, 0, 0, eperm),
        ]
    # prlimit64: this process only (pid 0 or its own pid).
    program += [
        _insn(_JEQ_K, 0, 5, _PRLIMIT64),
        _insn(_LD_W_ABS, 0, 0, _OFFSET_ARG0),
        _insn(_JEQ_K, 1, 0, 0),
        _insn(_JEQ_K, 0, 1, pid),
        _insn(_RET_K, 0, 0, _RET_ALLOW),
        _insn(_RET_K, 0, 0, eperm),
    ]
    program.append(_insn(_RET_K, 0, 0, _RET_ALLOW))
    return b"".join(program)


# --- helpers -----------------------------------------------------------------------------------------------------


class Unavailable(Exception):
    """The confinement cannot be applied; ``code`` names the reason for the host."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


class _Libc:
    """The C library's ``syscall`` and ``prctl``, looked up on first use and only on Linux.

    The module is imported on every system (the host imports it to find its path and its constants), so nothing
    Linux-specific may run at import time: macOS has no ``prctl``, and on Windows ``ctypes.CDLL(None)`` fails."""

    def __init__(self) -> None:
        self._lib: ctypes.CDLL | None = None

    def _load(self) -> ctypes.CDLL:
        if self._lib is None:
            if not sys.platform.startswith("linux"):
                raise Unavailable("platform", f"needs Linux, this is {sys.platform}")
            lib = ctypes.CDLL(None, use_errno=True)
            lib.syscall.restype = ctypes.c_long
            lib.prctl.restype = ctypes.c_int
            lib.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
            self._lib = lib
        return self._lib

    def syscall(self, *args: object) -> int:
        return int(self._load().syscall(*args))

    def prctl(self, *args: int) -> int:
        return int(self._load().prctl(*args))


_libc = _Libc()

_PR_SET_PDEATHSIG = 1
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2


def landlock_abi() -> int:
    """The kernel's Landlock ABI version, or 0 if Landlock is not built in or not enabled (and on other systems)."""
    if not sys.platform.startswith("linux"):
        return 0
    result = _libc.syscall(_SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0), _LANDLOCK_CREATE_RULESET_VERSION)
    return int(result) if result > 0 else 0


def _check_platform(minimum: int = MIN_LANDLOCK_ABI) -> int:
    if not sys.platform.startswith("linux") or platform.machine() != "x86_64":
        raise Unavailable("platform", f"needs Linux x86-64, this is {sys.platform} {platform.machine()}")
    if os.geteuid() == 0 or os.getegid() == 0:
        raise Unavailable("root", "Nodestro runs as the administrator (root); start it as a normal user")
    abi = landlock_abi()
    if abi < minimum:
        detail = "not enabled in this kernel" if abi == 0 else f"ABI {abi}, the sandbox needs ABI {minimum}"
        raise Unavailable("landlock", f"Landlock {detail}")
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            seccomp = [line for line in status if line.startswith("Seccomp:")]
    except OSError as exc:
        raise Unavailable("seccomp", f"cannot read the process status: {exc}") from exc
    if not seccomp:
        raise Unavailable("seccomp", "this kernel has no seccomp support")
    return abi


def _set_limits(limits: dict[str, int]) -> None:
    import resource  # POSIX only; this runs after the platform check

    def limit(kind: int, value: int) -> None:
        try:
            resource.setrlimit(kind, (value, value))
        except (ValueError, OSError) as exc:
            raise Unavailable("limits", f"cannot set resource limit {kind}: {exc}") from exc

    limit(resource.RLIMIT_CORE, 0)
    limit(resource.RLIMIT_NPROC, 0)
    limit(resource.RLIMIT_NOFILE, int(limits.get("open_files", 256)))
    limit(resource.RLIMIT_FSIZE, int(limits["file_bytes"]))
    limit(resource.RLIMIT_AS, int(limits["memory_bytes"]))
    limit(resource.RLIMIT_CPU, int(limits["cpu_seconds"]))


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def interpreter_paths(forbidden: list[str]) -> list[str]:
    """The existing directories the interpreter loads code from, without a path below another one.

    Raises :class:`Unavailable` if one of them is or contains a forbidden path: granting it would expose the home
    directory, the data directory or other calls' working directories."""
    candidates = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix, os.path.dirname(sys.executable)}
    candidates.update(p for p in sys.path if p)
    candidates.add(os.path.dirname(os.path.realpath(sys.executable)))
    found: list[str] = []
    for candidate in candidates:
        real = os.path.realpath(candidate)
        if not os.path.isdir(real) or real in found:
            continue
        for path in forbidden:
            if _is_within(path, real):
                raise Unavailable("paths", f"the interpreter path {real} contains the protected path {path}")
        found.append(real)
    found.sort()
    return [p for p in found if not any(p != q and _is_within(p, q) for q in found)]


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]  # fmt: skip


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _apply_landlock(abi: int, read: list[str], write: list[str]) -> None:
    fs = handled_fs(abi)
    attr = _RulesetAttr(fs, NET_BIND_TCP | NET_CONNECT_TCP, 0)
    size = 16
    if abi >= 6:
        attr.scoped = SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL
        size = 24
    ruleset = _libc.syscall(_SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size), 0)
    if ruleset < 0:
        raise Unavailable("landlock", f"cannot create a ruleset: {os.strerror(ctypes.get_errno())}")
    try:
        # Write access without device nodes and sockets (a root user could otherwise create devices).
        writable = fs & ~(FS_MAKE_CHAR | FS_MAKE_BLOCK | FS_MAKE_SOCK)
        rules = [(p, _READ) for p in read] + [(p, writable) for p in write] + list(DEVICES)
        for path, rights in rules:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                continue  # e.g. /lib64 on a system without it
            try:
                if not os.path.isdir(path):
                    rights &= _FILE_RIGHTS
                rule = _PathBeneathAttr(rights & fs, fd)
                if _libc.syscall(_SYS_LANDLOCK_ADD_RULE, ruleset, _LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(rule), 0):
                    raise Unavailable("landlock", f"cannot add a rule for {path}: {os.strerror(ctypes.get_errno())}")
            finally:
                os.close(fd)
        if _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0):
            raise Unavailable("landlock", f"cannot set no_new_privs: {os.strerror(ctypes.get_errno())}")
        if _libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0):
            raise Unavailable("landlock", f"cannot restrict the process: {os.strerror(ctypes.get_errno())}")
    finally:
        os.close(ruleset)


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def _apply_seccomp(pid: int) -> None:
    program = seccomp_program(pid)
    buffer = ctypes.create_string_buffer(program, len(program))
    fprog = _SockFprog(len(program) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    if _libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.addressof(fprog), 0, 0):
        raise Unavailable("seccomp", f"cannot install the filter: {os.strerror(ctypes.get_errno())}")


def _verify(probe_file: str) -> None:
    """Prove from the inside that the confinement holds, so a run never counts as sandboxed when it is not."""
    try:
        with open(probe_file, "rb"):
            pass
    except PermissionError:
        pass
    else:
        raise Unavailable("verify", "a file outside the sandbox is still readable")
    try:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()
    except OSError:
        pass
    else:
        raise Unavailable("verify", "a network socket can still be created")
    try:
        child = os.fork()
    except OSError:
        pass
    else:  # pragma: no cover - only reached if the filter failed
        if child == 0:
            os._exit(0)
        raise Unavailable("verify", "a new process can still be started")


def confine(config: dict[str, object]) -> None:
    """Apply every step of the module docstring to this process; raise :class:`Unavailable` if one fails."""
    # A configuration can only ask for a newer ABI (the tests use that to simulate an older kernel), never an older.
    abi = _check_platform(max(MIN_LANDLOCK_ABI, int(config.get("min_landlock_abi", 0))))  # type: ignore[call-overload]
    pid = os.getpid()
    _libc.prctl(_PR_SET_PDEATHSIG, 9, 0, 0, 0)
    if os.getppid() != int(config["parent"]):  # type: ignore[call-overload]
        raise Unavailable("parent", "the Nodestro process that started the sandbox is gone")
    forbidden = [os.path.realpath(str(p)) for p in config.get("forbidden", [])]  # type: ignore[attr-defined]
    read = interpreter_paths(forbidden)
    read += [os.path.realpath(str(p)) for p in config.get("read", [])]  # type: ignore[attr-defined]
    read += [p for p in SYSTEM_READ if os.path.exists(p)]
    read.append(os.getcwd())
    write = [os.path.realpath(str(p)) for p in config.get("write", [])]  # type: ignore[attr-defined]
    for path in read + write:
        for protected in forbidden:
            if _is_within(protected, path) and path not in write:
                raise Unavailable("paths", f"the sandbox path {path} contains the protected path {protected}")
    _set_limits(config["limits"])  # type: ignore[arg-type]
    _apply_landlock(abi, read, write)
    _apply_seccomp(pid)
    _verify(str(config["probe_file"]))


def main(argv: list[str]) -> int:
    try:
        config = json.loads(argv[1])
        confine(config)
    except Unavailable as exc:
        print(f"{MARKER} {exc.code}: {exc}", file=sys.stderr, flush=True)
        return EXIT_UNAVAILABLE
    except Exception as exc:
        print(f"{MARKER} internal: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return EXIT_UNAVAILABLE
    sys.argv = [str(config["runner"])]
    runpy.run_path(str(config["runner"]), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
