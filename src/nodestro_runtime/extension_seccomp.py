"""Nodestro's own seccomp profile for the containers of extension tools (N-22, G-6 item 20).

The profile (``extension_seccomp.json`` next to this module) is the default profile of Docker (Moby, ``moby/profiles``
``seccomp/v0.1.0``, Apache-2.0; :data:`SOURCE`) made stricter by :func:`derive`:

- every rule that applies only to a container with a capability is dropped (sandbox containers have none, so those
  rules never applied; without them the profile shows what is allowed);
- the system calls in :data:`EXTRA_BLOCKED` are removed from every rule that allows them: tracing and reading other
  processes, executing code that is not a file (``memfd_create``, ``execveat``), changing the execution domain, NUMA
  memory placement and file handles. Python and the extension runner need none of them.

It is passed as ``--security-opt seccomp=<file>`` (the docker CLI sends the file's content to the daemon, also to
Docker Desktop's VM and through Nodestro Local Docker's sandbox gateway, which accepts exactly this profile), on top of
Orkexa's sandbox shape, and its effect is proven before any extension container runs: :data:`PROBE_SCRIPT` runs in a
container of the same shape, without package code, and every call in :data:`PROBES` must be refused
(:func:`check_probes`). ``scripts/extension_seccomp.py`` derives the file again from a new Moby profile.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: The Moby profile the file was derived from.
SOURCE = "https://raw.githubusercontent.com/moby/profiles/seccomp/v0.1.0/seccomp/default.json"
#: The profile file shipped with the runtime.
PROFILE_PATH = Path(__file__).with_name("extension_seccomp.json")
#: System calls no rule of the profile allows (beyond what Docker's default already refuses).
EXTRA_BLOCKED: tuple[str, ...] = (
    "execveat",
    "get_mempolicy",
    "kcmp",
    "mbind",
    "memfd_create",
    "memfd_secret",
    "migrate_pages",
    "name_to_handle_at",
    "personality",
    "pidfd_getfd",
    "process_madvise",
    "process_vm_readv",
    "process_vm_writev",
    "ptrace",
    "set_mempolicy",
    "set_mempolicy_home_node",
    "userfaultfd",
)


def derive(moby: dict[str, Any]) -> dict[str, Any]:
    """The extension profile from Moby's default profile ``moby`` (see the module documentation)."""
    if moby.get("defaultAction") != "SCMP_ACT_ERRNO":
        raise ValueError("the source profile must refuse every system call it does not list")
    rules = []
    for rule in moby.get("syscalls", []):
        if (rule.get("includes") or {}).get("caps"):
            continue
        rule = dict(rule)
        if rule.get("action") == "SCMP_ACT_ALLOW":
            rule["names"] = [name for name in rule["names"] if name not in EXTRA_BLOCKED]
            if not rule["names"]:
                continue
        rules.append(rule)
    return {**moby, "syscalls": rules}


def profile() -> dict[str, Any]:
    """The shipped profile."""
    data: dict[str, Any] = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    return data


def security_option() -> str:
    """The ``--security-opt`` value that applies the profile."""
    return f"seccomp={PROFILE_PATH}"


def is_profile(option: object) -> bool:
    """Whether a container's security option ``option`` (``seccomp=<JSON>``, as the docker CLI sends it) is exactly
    the shipped profile."""
    if not isinstance(option, str) or not option.startswith("seccomp="):
        return False
    try:
        return bool(json.loads(option[len("seccomp=") :]) == profile())
    except ValueError:
        return False


#: The system calls the probe makes, with arguments the kernel itself accepts for an unprivileged process (so a
#: refusal is the profile's): ``(name, x86-64 number, arm64 number, arguments)``.
PROBES: tuple[tuple[str, int, int, tuple[int, ...]], ...] = (
    ("ptrace", 101, 117, (0, 0, 0, 0)),  # PTRACE_TRACEME
    ("process_vm_readv", 310, 270, (0, 0, 0, 0, 0, 0)),  # pid filled in: this process, nothing to read
    ("memfd_create", 319, 279, (0, 0)),  # name filled in
    ("execveat", 322, 281, (0xFFFFFF9C, 0, 0, 0, 0x1000)),  # AT_FDCWD, "" filled in, AT_EMPTY_PATH: no file
    ("personality", 135, 92, (0xFFFFFFFF,)),  # only reads the current persona
    ("kcmp", 312, 272, (0, 0, 0, 0, 0)),  # pids filled in
)

#: Runs in a container of the extension shape (``python -I -B -c``); prints one JSON object: name -> ``refused`` or
#: ``allowed``.
PROBE_SCRIPT = """\
import ctypes, errno, json, os, platform
probes = json.loads(PROBES)
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
machine = platform.machine()
column = {'x86_64': 1, 'amd64': 1, 'aarch64': 2, 'arm64': 2}.get(machine)
result = {}
if column is None:
    result['unsupported'] = machine
else:
    name = ctypes.c_char_p(b'nodestro-probe')
    empty = ctypes.c_char_p(b'')
    for probe in probes:
        args = list(probe[3])
        if probe[0] in ('process_vm_readv', 'kcmp'):
            args[0] = os.getpid()
            if probe[0] == 'kcmp':
                args[1] = os.getpid()
        args = [ctypes.c_long(value) for value in args]
        if probe[0] == 'memfd_create':
            args[0] = name
        if probe[0] == 'execveat':
            args[1] = empty
        ctypes.set_errno(0)
        value = libc.syscall(ctypes.c_long(probe[column]), *args)
        code = ctypes.get_errno()
        result[probe[0]] = 'refused' if value == -1 and code in (errno.EPERM, errno.ENOSYS) else 'allowed'
print(json.dumps(result))
"""


def probe_command() -> list[str]:
    """The container command of the probe."""
    script = "PROBES = " + repr(json.dumps(PROBES)) + "\n" + PROBE_SCRIPT
    return ["python", "-I", "-B", "-c", script]


def check_probes(output: str) -> list[str]:
    """What the probe's output shows (empty: every call in :data:`PROBES` was refused)."""
    lines = output.strip().splitlines()
    try:
        found = json.loads(lines[-1]) if lines else None
    except ValueError:
        found = None
    if not isinstance(found, dict):
        return ["the seccomp probe printed no result"]
    if "unsupported" in found:
        return [f"no seccomp probes for the architecture {found['unsupported']}"]
    allowed = [name for name, *_rest in PROBES if found.get(name) != "refused"]
    return [f"the extension seccomp profile is not applied: {', '.join(allowed)} not refused"] if allowed else []


__all__ = [
    "EXTRA_BLOCKED",
    "PROBES",
    "PROBE_SCRIPT",
    "PROFILE_PATH",
    "SOURCE",
    "check_probes",
    "derive",
    "is_profile",
    "probe_command",
    "profile",
    "security_option",
]
