"""Security probes of the built-in Linux sandbox: attacks that must fail, run from inside a real sandbox.

``python -m nodestro_runtime.sandbox_probes [--json]`` runs every probe on this computer and prints the results; the
exit status is 0 only if every probe passed. The test suite runs the same probes, and the desktop's system tests run
them with the packaged product on clean Ubuntu 24.04 and Debian 13 machines as a normal user.

Each probe is tool code that tries something the sandbox must prevent and returns what happened. A probe passes only
if the tool code **ran** inside the sandbox (it returned its report) and every attempt was refused: a probe whose
sandbox could not start fails, it never counts as a successful isolation test. Host-side probes (limits, timeout,
cancel, cleanup) check what the host saw.

The probes plant canaries first: a secret file in the home directory, a fake Nodestro data directory with a
``secrets.json``, an API key in Nodestro's own environment, a TCP server on the loopback interface and a UNIX socket
server. A probe fails if any canary is read or contacted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from orkexa.dynamic.errors import SandboxError

from .builtin_sandbox import LinuxSandbox, SandboxStatus, measure
from .sandbox_policy import SandboxPolicy

_HEADER = '''
import json, os, sys, socket, ctypes, errno, signal, subprocess, threading, resource


def attempt(results, name, action):
    try:
        value = action()
    except BaseException as exc:  # noqa: BLE001
        results[name] = "refused: " + type(exc).__name__ + ": " + str(exc)[:120]
    else:
        results[name] = "ALLOWED: " + repr(value)[:200]


def probe(context: str) -> str:
    """Run the probe.

    Args:
        context: JSON with the canaries.

    Returns:
        JSON with one entry per attempt.
    """
    c = json.loads(context)
    r = {}
    body(c, r)
    return json.dumps(r)
'''


@dataclass
class Probe:
    """One attack. ``body`` is the code of ``def body(c, r)``; ``c`` holds the canaries, ``r`` collects attempts."""

    name: str
    area: str
    body: str
    #: Attempts (keys of ``r``) that must succeed, e.g. writing below ``tmp``; all others must be refused.
    allowed: tuple[str, ...] = ()


@dataclass
class Result:
    name: str
    area: str
    passed: bool
    detail: dict[str, str] = field(default_factory=dict)
    error: str = ""


PROBES: list[Probe] = [
    # --- files ----------------------------------------------------------------------------------------------------
    Probe(
        "files-outside-workdir",
        "files",
        """
    attempt(r, "read /etc/passwd", lambda: open("/etc/passwd").read(20))
    attempt(r, "read /etc/shadow", lambda: open("/etc/shadow").read(20))
    attempt(r, "list /", lambda: os.listdir("/"))
    attempt(r, "list /home", lambda: os.listdir("/home"))
    attempt(r, "list home", lambda: os.listdir(c["home"]))
    attempt(r, "read home canary", lambda: open(c["home_canary"]).read())
    attempt(r, "write home", lambda: open(os.path.join(c["home"], "nodestro-probe-written"), "w").write("x"))
    attempt(r, "write /tmp", lambda: open("/tmp/nodestro-probe-written", "w").write("x"))
    attempt(r, "write /dev/shm", lambda: open("/dev/shm/nodestro-probe-written", "w").write("x"))
    attempt(r, "list workdir parent", lambda: os.listdir(".."))
    attempt(r, "write workdir", lambda: open("tool.py", "a").write("#"))
    attempt(r, "read /sys/class/net", lambda: os.listdir("/sys/class/net"))
    attempt(r, "read ssh keys", lambda: os.listdir(os.path.join(c["home"], ".ssh")))
    attempt(r, "write tmp", lambda: open(os.path.join(os.environ["TMPDIR"], "ok.txt"), "w").write("ok"))
""",
        allowed=("write tmp",),
    ),
    Probe(
        "data-directory",
        "files",
        """
    attempt(r, "list data dir", lambda: os.listdir(c["data_dir"]))
    attempt(r, "read secrets.json", lambda: open(os.path.join(c["data_dir"], "secrets.json")).read())
    attempt(r, "read workspace db", lambda: open(os.path.join(c["data_dir"], "workspace.db"), "rb").read(16))
    attempt(r, "read other workdir", lambda: open(os.path.join(c["other_workdir"], "tool.py")).read())
""",
    ),
    Probe(
        "links-and-renames",
        "paths",
        """
    tmp = os.environ["TMPDIR"]
    os.symlink(c["home_canary"], os.path.join(tmp, "link"))
    attempt(r, "read through symlink", lambda: open(os.path.join(tmp, "link")).read())
    attempt(r, "hard link home canary", lambda: os.link(c["home_canary"], os.path.join(tmp, "hard")))
    attempt(r, "rename tmp file out", lambda: (open(os.path.join(tmp, "f"), "w").close(),
                                               os.rename(os.path.join(tmp, "f"), os.path.join(c["home"], "moved"))))
    attempt(r, "open via /proc/self/root", lambda: open("/proc/self/root" + c["home_canary"]).read())
    attempt(r, "open dot-dot path", lambda: open(os.path.join(tmp, "..", "..", os.path.basename(c["other_workdir"]),
                                                              "tool.py")).read())
    attempt(r, "make device node", lambda: os.mknod(os.path.join(tmp, "dev"), 0o600 | 0o020000, os.makedev(1, 3)))
    attempt(r, "make fifo", lambda: os.mkfifo(os.path.join(tmp, "fifo")))
""",
        allowed=("make fifo",),
    ),
    Probe(
        "odd-file-names",
        "paths",
        """
    tmp = os.environ["TMPDIR"]
    names = ["ü-名前-😀.txt", "with\\nnewline", "-rf", " lead space", "a" * 255, "..hidden", "back\\\\slash"]
    for name in names:
        attempt(r, "create " + repr(name)[:30], lambda n=name: open(os.path.join(tmp, n), "w").write("x"))
    attempt(r, "create ../escape", lambda: open(os.path.join(tmp, "../escape"), "w").write("x"))
    attempt(r, "create /tmp/abs", lambda: open("/tmp/nodestro-abs", "w").write("x"))
    os.symlink(c["host_dir"], os.path.join(tmp, "host-link"))
    attempt(r, "write through dir symlink", lambda: open(os.path.join(tmp, "host-link", "planted"), "w").write("x"))
    attempt(r, "remove through dir symlink", lambda: os.unlink(os.path.join(tmp, "host-link", "keep.txt")))
""",
        allowed=tuple(
            "create " + repr(n)[:30]
            for n in ["ü-名前-😀.txt", "with\nnewline", "-rf", " lead space", "a" * 255, "..hidden", "back\\slash"]
        ),
    ),
    # --- secrets and environment -----------------------------------------------------------------------------------
    Probe(
        "environment-and-keys",
        "secrets",
        """
    env = dict(os.environ)
    leaked = [k for k in env if k not in ("HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONIOENCODING",
                                          "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")]
    if leaked or c["key_value"] in json.dumps(env):
        r["inherited variables"] = "ALLOWED: " + repr(leaked)
    attempt(r, "parent environ", lambda: open("/proc/%d/environ" % c["parent"], "rb").read())
    attempt(r, "parent cmdline", lambda: open("/proc/%d/cmdline" % c["parent"], "rb").read())
    attempt(r, "parent fds", lambda: os.listdir("/proc/%d/fd" % c["parent"]))
    attempt(r, "parent mem", lambda: open("/proc/%d/mem" % c["parent"], "rb").read(1))
    attempt(r, "list /proc", lambda: os.listdir("/proc"))
    attempt(r, "keyring", lambda: ctypes.CDLL(None, use_errno=True).syscall(250, 0, -3, 0, 0, 0) == 0 or
            (_ for _ in ()).throw(OSError(ctypes.get_errno(), "keyctl")))
""",
    ),
    # --- network --------------------------------------------------------------------------------------------------
    Probe(
        "network",
        "network",
        """
    attempt(r, "tcp socket", lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM))
    attempt(r, "udp socket", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
    attempt(r, "ipv6 socket", lambda: socket.socket(socket.AF_INET6, socket.SOCK_STREAM))
    attempt(r, "unix socket", lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
    attempt(r, "netlink socket", lambda: socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0))
    attempt(r, "packet socket", lambda: socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0))
    attempt(r, "dns lookup", lambda: socket.getaddrinfo("example.com", 443))
    attempt(r, "loopback service", lambda: socket.create_connection(("127.0.0.1", c["tcp_port"]), timeout=2))
    attempt(r, "local unix service", lambda: (lambda s: s.connect(c["unix_socket"]))(socket.socket(socket.AF_UNIX)))
    attempt(r, "abstract unix socket",
            lambda: (lambda s: s.connect("\\0nodestro-probe"))(socket.socket(socket.AF_UNIX)))
    attempt(r, "http", lambda: __import__("urllib.request").request.urlopen("http://127.0.0.1:%d/" % c["tcp_port"],
                                                                          timeout=2))
    attempt(r, "socketpair", lambda: socket.socketpair())
""",
        allowed=("socketpair",),
    ),
    # --- processes ------------------------------------------------------------------------------------------------
    Probe(
        "process-start",
        "processes",
        """
    attempt(r, "fork", lambda: os.fork())
    attempt(r, "subprocess", lambda: subprocess.run(["/bin/sh", "-c", "echo escaped"], capture_output=True))
    attempt(r, "posix_spawn", lambda: os.posix_spawn("/bin/true", ["true"], {}))
    attempt(r, "execv python", lambda: os.execv(sys.executable, [sys.executable, "-c", "print(1)"]))
    attempt(r, "raw clone", lambda: ctypes.CDLL(None, use_errno=True).syscall(56, 17, 0, 0, 0, 0) >= 0 and "cloned"
            or (_ for _ in ()).throw(OSError(ctypes.get_errno(), "clone")))
    attempt(r, "unshare user ns", lambda: os.unshare(0x10000000) if hasattr(os, "unshare") else
            (_ for _ in ()).throw(OSError("no unshare")))
    attempt(r, "ptrace parent", lambda: ctypes.CDLL(None, use_errno=True).syscall(101, 16, c["parent"], 0, 0) == 0 or
            (_ for _ in ()).throw(OSError(ctypes.get_errno(), "ptrace")))
    attempt(r, "multiprocessing", lambda: __import__("multiprocessing").get_context("fork").Process(
        target=print).start())
""",
    ),
    Probe(
        "foreign-processes",
        "processes",
        """
    attempt(r, "signal parent", lambda: os.kill(c["parent"], 0))
    attempt(r, "signal init", lambda: os.kill(1, 0))
    attempt(r, "signal all", lambda: os.kill(-1, 0))
    attempt(r, "signal group", lambda: os.killpg(c["parent_group"], 0))
    attempt(r, "renice parent", lambda: os.setpriority(os.PRIO_PROCESS, c["parent"], 19))
    attempt(r, "prlimit parent", lambda: resource.prlimit(c["parent"], resource.RLIMIT_NOFILE))
    attempt(r, "affinity parent", lambda: os.sched_setaffinity(c["parent"], {0}))
    attempt(r, "pidfd parent", lambda: os.pidfd_open(c["parent"]))
    attempt(r, "sysv shm", lambda: ctypes.CDLL(None, use_errno=True).syscall(29, 0, 4096, 0o1600) >= 0 and "shm"
            or (_ for _ in ()).throw(OSError(ctypes.get_errno(), "shmget")))
    attempt(r, "process_vm_readv", lambda: ctypes.CDLL(None, use_errno=True).syscall(310, c["parent"], 0, 0, 0, 0, 0)
            >= 0 or (_ for _ in ()).throw(OSError(ctypes.get_errno(), "process_vm_readv")))
    attempt(r, "signal self", lambda: os.kill(os.getpid(), 0))
""",
        allowed=("signal self",),
    ),
    Probe(
        "kernel-interfaces",
        "processes",
        """
    libc = ctypes.CDLL(None, use_errno=True)
    def call(nr, *args):
        res = libc.syscall(nr, *args)
        if res < 0:
            raise OSError(ctypes.get_errno(), "syscall %d" % nr)
        return res
    attempt(r, "bpf", lambda: call(321, 0, 0, 0))
    attempt(r, "perf_event_open", lambda: call(298, 0, 0, -1, -1, 0))
    attempt(r, "io_uring_setup", lambda: call(425, 1, 0))
    attempt(r, "userfaultfd", lambda: call(323, 0))
    attempt(r, "mount", lambda: call(165, b"none", b"/tmp", b"tmpfs", 0, 0))
    attempt(r, "setns", lambda: call(308, 0, 0))
    attempt(r, "open_by_handle_at", lambda: call(304, 0, 0, 0))
    attempt(r, "x32 syscall", lambda: "survived")  # an x32 call would kill the process; covered by the filter test
""",
        allowed=("x32 syscall",),
    ),
]


def _plain(body: str) -> str:
    return _HEADER + "\n\ndef body(c, r):\n" + body.strip("\n") + "\n"


class _Canaries:
    """Planted secrets and services the probes must not reach."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = Path.home()
        self.home_canary = self.home / f".nodestro-probe-{secrets.token_hex(4)}"
        self.home_canary.write_text("home secret", encoding="utf-8")
        self.data_dir = root / "data"
        self.data_dir.mkdir()
        (self.data_dir / "secrets.json").write_text('{"anthropic": "sk-ant-probe"}', encoding="utf-8")
        (self.data_dir / "workspace.db").write_bytes(b"SQLite format 3\0")
        self.other_workdir = Path(tempfile.mkdtemp(prefix="orkexa-sandbox-"))
        (self.other_workdir / "tool.py").write_text("SECRET = 1\n", encoding="utf-8")
        self.host_dir = root / "host"
        self.host_dir.mkdir()
        (self.host_dir / "keep.txt").write_text("keep", encoding="utf-8")
        self.key_value = "sk-ant-api03-" + secrets.token_hex(24)
        self.contacts: list[str] = []
        self.tcp = socket.create_server(("127.0.0.1", 0))
        self.unix_path = str(root / "service.sock")
        self.unix = socket.socket(socket.AF_UNIX)
        self.unix.bind(self.unix_path)
        self.unix.listen()
        for server in (self.tcp, self.unix):
            threading.Thread(target=self._accept, args=(server,), daemon=True).start()

    def _accept(self, server: socket.socket) -> None:
        try:
            while True:
                connection, _ = server.accept()
                self.contacts.append(str(server.getsockname()))
                connection.close()
        except OSError:
            return

    def context(self) -> dict[str, Any]:
        return {
            "home": str(self.home),
            "home_canary": str(self.home_canary),
            "data_dir": str(self.data_dir),
            "other_workdir": str(self.other_workdir),
            "host_dir": str(self.host_dir),
            "key_value": self.key_value,
            "parent": os.getpid(),
            "parent_group": os.getpgrp(),
            "tcp_port": self.tcp.getsockname()[1],
            "unix_socket": self.unix_path,
        }

    def close(self) -> list[str]:
        """Remove the canaries; return what was found tampered with."""
        problems = list((self.contacts and [f"service contacted: {c}" for c in self.contacts]) or [])
        if not (self.host_dir / "keep.txt").exists() or (self.host_dir / "planted").exists():
            problems.append("host directory changed through a symbolic link")
        for path in (self.home / "nodestro-probe-written", self.home / "moved", Path("/tmp/nodestro-probe-written"),
                     Path("/dev/shm/nodestro-probe-written"), Path("/tmp/nodestro-abs")):  # fmt: skip
            if path.exists():
                problems.append(f"file written outside the sandbox: {path}")
                path.unlink()
        self.tcp.close()
        self.unix.close()
        self.home_canary.unlink(missing_ok=True)
        for file in self.other_workdir.iterdir():
            file.unlink()
        self.other_workdir.rmdir()
        return problems


def _sandbox(canaries: _Canaries, **kwargs: Any) -> LinuxSandbox:
    return LinuxSandbox(protected=(str(canaries.data_dir),), **kwargs)


def run_attack(probe: Probe, canaries: _Canaries) -> Result:
    """Run one probe in a fresh sandbox; passed only if it ran and every attempt not in ``allowed`` was refused."""
    os.environ["NODESTRO_PROBE_KEY"] = canaries.key_value  # in Nodestro's environment, must not reach the code
    try:
        raw = _sandbox(canaries, timeout=30.0).call(_plain(probe.body), "probe", {"context": json.dumps(
            canaries.context())})  # fmt: skip
    except Exception as exc:
        return Result(probe.name, probe.area, False, error=f"the probe did not run in the sandbox: {exc}")
    finally:
        os.environ.pop("NODESTRO_PROBE_KEY", None)
    detail: dict[str, str] = json.loads(raw)
    passed = bool(detail)
    for attempt, outcome in detail.items():
        should_work = attempt in probe.allowed
        if outcome.startswith("ALLOWED") != should_work:
            passed = False
    missing = [a for a in probe.allowed if a not in detail]
    return Result(probe.name, probe.area, passed and not missing, detail)


def _expect_error(name: str, area: str, run: Callable[[], Any], words: tuple[str, ...], seconds: float) -> Result:
    started = time.monotonic()
    try:
        value = run()
    except Exception as exc:
        elapsed = time.monotonic() - started
        text = str(exc)
        ok = any(w in text for w in words) and "could not confine" not in text and elapsed < seconds
        return Result(name, area, ok, {"error": text[:300], "seconds": f"{elapsed:.1f}"})
    return Result(name, area, False, {"result": repr(value)[:300]}, error="the run was not stopped")


_TOOL = _HEADER.split("def probe", 1)[0]


def _tool(body: str, arg: str = "x") -> str:
    return (
        _TOOL
        + f"\n\ndef probe({arg}: str) -> str:\n"
        + '    """Probe.\n\n    Args:\n        x: Unused.\n\n    Returns:\n        Text.\n    """\n'
        + "\n".join("    " + line for line in body.strip("\n").splitlines())
        + "\n"
    )


def host_probes(canaries: _Canaries) -> list[Result]:
    """Limits, timeout, cancel and cleanup, as seen from the host."""
    results: list[Result] = []
    memory = _sandbox(canaries, timeout=20.0, memory_mb=256)
    results.append(_expect_error("memory-limit", "limits", lambda: memory.call(
        _tool("data = bytearray(1024 * 1024 * 1024)\nreturn str(len(data))"), "probe", {"x": ""}),
        ("MemoryError",), 20))  # fmt: skip
    cpu = _sandbox(canaries, timeout=3.0)
    results.append(_expect_error("cpu-timeout", "limits", lambda: cpu.call(
        _tool("while True:\n    pass"), "probe", {"x": ""}), ("Timed out",), 8))  # fmt: skip
    tmp = _sandbox(canaries, timeout=20.0, tmp_mb=8)
    results.append(_expect_error("tmp-limit-one-file", "limits", lambda: tmp.call(_tool(
        "with open(os.environ['TMPDIR'] + '/big', 'wb') as f:\n    f.write(b'x' * 20 * 1024 * 1024)\nreturn 'written'"),
        "probe", {"x": ""}), ("File size limit", "signal 25", "exceeded", "File too large"), 20))  # fmt: skip
    results.append(_expect_error("tmp-limit-many-files", "limits", lambda: tmp.call(_tool(
        "import time\nfor i in range(200):\n"
        "    open(os.environ['TMPDIR'] + '/f%d' % i, 'wb').write(b'x' * 1024 * 1024)\n"
        "    time.sleep(0.01)\nreturn 'written'"), "probe", {"x": ""}), ("exceeded",), 20))  # fmt: skip
    results.append(_expect_error("tmp-limit-file-count", "limits", lambda: tmp.call(_tool(
        "for i in range(200000):\n    open(os.environ['TMPDIR'] + '/e%d' % i, 'wb').close()\nreturn 'written'"),
        "probe", {"x": ""}), ("files",), 20))  # fmt: skip
    threads = _sandbox(canaries, timeout=20.0)
    results.append(_expect_error("process-limit", "limits", lambda: threads.call(_tool(
        "ts = [threading.Thread(target=lambda: None) for _ in range(5)]\n[t.start() for t in ts]\nreturn 'started'"),
        "probe", {"x": ""}), ("can't start new thread", "Resource temporarily unavailable"), 20))  # fmt: skip
    output = _sandbox(canaries, timeout=20.0, max_output_chars=1000)
    results.append(_expect_error("output-limit", "limits", lambda: output.call(
        _tool("return 'x' * 5000"), "probe", {"x": ""}), ("limit is 1000",), 20))  # fmt: skip
    # cancel: a call that sleeps forever, cancelled from another thread
    cancel = _sandbox(canaries, timeout=60.0)
    threading.Timer(1.5, cancel.cancel).start()
    results.append(_expect_error("cancel", "lifecycle", lambda: cancel.call(
        _tool("import time\ntime.sleep(600)\nreturn 'slept'"), "probe", {"x": ""}), ("signal 9", "killed", "exited"),
        10))  # fmt: skip
    leftovers = _sandbox_processes()
    workdirs = [p for p in Path(tempfile.gettempdir()).glob("orkexa-sandbox-*") if p != canaries.other_workdir]
    results.append(Result("cleanup", "lifecycle", not leftovers and not workdirs,
                          {"processes": repr(leftovers), "workdirs": repr([str(w) for w in workdirs])}))  # fmt: skip
    return results


def _sandbox_processes() -> list[int]:
    """Process ids of sandbox processes still alive (their command line names the confinement script)."""
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if b"sandbox_confine.py" in (entry / "cmdline").read_bytes():
                    found.append(int(entry.name))
            except OSError:
                continue
    return found


def kernel_probe_refuses_untested(canaries: _Canaries) -> list[Result]:
    """Fail closed: a run whose confinement cannot be verified never executes tool code."""
    marker = canaries.root / "executed"
    sandbox = LinuxSandbox(timeout=20.0, read=(str(canaries.root),))  # makes the probe file's parent readable
    code = _tool(f"open({str(marker)!r}, 'w').write('ran')\nreturn 'ran'")
    # The verification file lives next to the workdir in the temp root; granting read access to the temp root through
    # ``read`` must be refused because the temp root is protected.
    try:
        sandbox.read = (tempfile.gettempdir(),)
        sandbox.call(code, "probe", {"x": ""})
    except Exception as exc:
        ok = "could not confine" in str(exc) and not marker.exists()
        return [Result("fail-closed", "kernel", ok, {"error": str(exc)[:300]})]
    return [Result("fail-closed", "kernel", False, error="tool code ran although the confinement was weakened")]


# --- compatibility ---------------------------------------------------------------------------------------------------

_STDLIB = """
import csv, datetime, decimal, io, json, os, re, sqlite3, zipfile, zoneinfo, statistics, math, hashlib, base64
rows = list(csv.reader(io.StringIO('a,b\\n1,2')))
path = os.path.join(os.environ['TMPDIR'], 'daten-ü.db')
db = sqlite3.connect(path)
db.execute('create table t (x)')
db.execute('insert into t values (?)', (x,))
db.commit()
archive_path = os.path.join(os.environ['TMPDIR'], 'archiv.zip')
with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
    archive.writestr('ä.txt', x)
with zipfile.ZipFile(archive_path) as archive:
    unpacked = archive.read('ä.txt').decode()
total = decimal.Decimal('0.1') + decimal.Decimal('0.2')
day = datetime.datetime(2026, 10, 31, 12, tzinfo=zoneinfo.ZoneInfo('Europe/Berlin')).isoformat()
return json.dumps([rows, db.execute('select x from t').fetchone()[0], unpacked, str(total), day,
                   re.sub('[aeiou]', '', x), statistics.mean([1, 2, 3]), hashlib.sha256(b'a').hexdigest()[:6]])
"""
_STDLIB_EXPECTED = [[["a", "b"], ["1", "2"]], "Grüße 😀", "Grüße 😀", "0.3", "2026-10-31T12:00:00+01:00", "Grüß 😀", 2,
                    "ca9781"]  # fmt: skip

_NUMPY = """
import numpy, json
matrix = numpy.arange(12, dtype=float).reshape(3, 4)
return json.dumps([numpy.__version__, float(matrix.sum()), float(numpy.linalg.det(numpy.eye(3) * 2))])
"""


def _builtin_tools_source() -> str:
    """Nodestro's built-in tools (the ones the 24 examples use) as plain code for the sandbox."""
    from . import builtin_tools

    source = Path(builtin_tools.__file__).read_text(encoding="utf-8")
    source = source.replace("from orkexa import Tool, ToolResultError", "class ToolResultError(Exception):\n    pass")
    return source.replace("@Tool\n", "")


def _examples_tools_check() -> tuple[str, str]:
    """Tool code that runs the built-in tools' functions in the sandbox, and the result the host computes."""
    from . import builtin_tools

    calls = (
        "json.dumps([calculator('(17 + 4) * 3 / 7'), days_between(date(2026, 1, 1), date(2026, 10, 31)),"
        " text_statistics('Grüße aus Wien. Zwei Sätze!'), current_datetime('Europe/Vienna')[:4]])"
    )
    module = _builtin_tools_source()
    code = (
        "import json\n"
        + _TOOL_DEF.format(arg="x")
        + "    namespace = {}\n"
        + f"    exec(compile({module!r}, 'builtin_tools', 'exec'), namespace)\n"
        + "    namespace['json'] = json\n"
        + f"    return eval({calls!r}, namespace)\n"
    )
    from datetime import date

    def run(tool: Any, *args: Any) -> Any:
        return tool.function(*args)

    expected = json.dumps([
        run(builtin_tools.calculator, "(17 + 4) * 3 / 7"),
        run(builtin_tools.days_between, date(2026, 1, 1), date(2026, 10, 31)),
        run(builtin_tools.text_statistics, "Grüße aus Wien. Zwei Sätze!"),
        run(builtin_tools.current_datetime, "Europe/Vienna")[:4],
    ])  # fmt: skip
    return code, expected


_TOOL_DEF = (
    "def probe({arg}: str) -> str:\n"
    '    """Probe.\n\n    Args:\n        x: Input.\n\n    Returns:\n        Output.\n    """\n'
)


def compat_checks(env_python: str | None = None) -> list[Result]:
    """Compatibility: what tool code needs must work in the sandbox, with the isolation unchanged."""
    results: list[Result] = []
    sandbox = LinuxSandbox(timeout=30.0)

    def check(name: str, run: Callable[[], Any], expected: Any) -> None:
        try:
            value = run()
        except Exception as exc:
            results.append(Result(name, "compat", False, error=f"{type(exc).__name__}: {exc}"[:400]))
            return
        ok = value == expected
        results.append(Result(name, "compat", ok, {"result": repr(value)[:300], "expected": repr(expected)[:300]}))

    check("stdlib", lambda: json.loads(sandbox.call(_tool(_STDLIB), "probe", {"x": "Grüße 😀"})), _STDLIB_EXPECTED)
    code, expected = _examples_tools_check()
    check("example-tools", lambda: sandbox.call(code, "probe", {"x": ""}), expected)
    tool_code = _tool("return x[::-1]")
    tests = "from tool import probe\n\n\ndef test_umkehren():\n    assert probe('Grüße') == 'eßürG'\n"
    check("generated-tool-tests", lambda: [sandbox.validate(tool_code, tests, "probe").ok], [True])
    check("tool-error-then-next-call", lambda: _error_then_ok(sandbox), "ok")
    check("repeated-calls", lambda: [sandbox.call(tool_code, "probe", {"x": str(n)}) for n in range(10)],
          [str(n)[::-1] for n in range(10)])  # fmt: skip
    check("parallel-calls", lambda: _parallel(sandbox, tool_code, 8), [str(n)[::-1] for n in range(8)])
    slow = LinuxSandbox(timeout=2.0)
    check("timeout-then-next-call", lambda: _timeout_then_ok(slow), "ok")
    long = LinuxSandbox(timeout=60.0)
    check("long-call", lambda: long.call(_tool("import time\ntime.sleep(5)\nreturn 'done'"), "probe", {"x": ""}),
          "done")  # fmt: skip
    check("desktop-workspace", _workspace_check, ["untrusted", "212.0", "LinuxSandbox", "sandbox.isolation_required"])
    if env_python:
        env = LinuxSandbox(timeout=60.0, memory_mb=2048, python=env_python)
        result = Result("compiled-library", "compat", False)
        try:
            version, total, det = json.loads(env.call(_tool(_NUMPY), "probe", {"x": ""}))
            result = Result("compiled-library", "compat", total == 66.0 and abs(det - 8.0) < 1e-9,
                            {"numpy": version, "sum": str(total), "det": str(det)})  # fmt: skip
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"[:400]
        results.append(result)
    # The comparison is a bare interpreter start (fixed code, no tool code on the host).
    starts = []
    for _ in range(10):
        started = time.perf_counter()
        subprocess.run([sys.executable, "-I", "-B", "-c", "import json"], check=True, env={})
        starts.append((time.perf_counter() - started) * 1000)
    own = measure(LinuxSandbox(timeout=30.0), calls=10)
    results.append(Result("overhead", "performance", own["mean_ms"] < 2000, {
        "sandbox_first_ms": f"{own['first_ms']:.0f}", "sandbox_mean_ms": f"{own['mean_ms']:.0f}",
        "sandbox_max_ms": f"{own['max_ms']:.0f}",
        "interpreter_start_ms": f"{sum(starts) / len(starts):.0f}"}))  # fmt: skip
    return results


def _workspace_check() -> list[str]:
    """A desktop workspace: an AI-generated tool is tested and runs in the built-in sandbox; the host subprocess
    is never used for it (it would fail here), and without the built-in sandbox it is refused."""
    from orkexa.dynamic import SubprocessSandbox

    from .problems import NodestroError
    from .tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
    from .workspace import Workspace

    def host(*_args: object) -> str:
        raise AssertionError("untrusted code reached the host subprocess")

    original = SubprocessSandbox._execute
    SubprocessSandbox._execute = host  # type: ignore[method-assign, assignment]
    try:
        with tempfile.TemporaryDirectory(prefix="nodestro-workspace-") as directory:
            workspace = Workspace(Path(directory) / "data", integrations=[])
            try:
                tools = workspace.tools
                saved, info = tools.save_custom(
                    CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, "celsius_to_fahrenheit", requirement="Convert"
                )
                if info is None:
                    return [f"not saved: {saved.errors}"]
                result, _ = tools.run("celsius_to_fahrenheit", {"celsius": 100})
                routed = tools._require("untrusted", tools.registry.sandbox, "celsius_to_fahrenheit")
                down = SandboxPolicy("desktop")
                refused = ""
                original_status = LinuxSandbox.status
                LinuxSandbox.status = staticmethod(  # type: ignore[method-assign]
                    lambda refresh=False: SandboxStatus(False, "landlock", "simulated", "", 0)
                )
                try:
                    down.require("untrusted", routed, name="x")
                except NodestroError as exc:
                    refused = exc.code
                finally:
                    LinuxSandbox.status = original_status  # type: ignore[method-assign]
                return [info.trust, result, type(routed).__name__, refused]
            finally:
                workspace.close()
    finally:
        SubprocessSandbox._execute = original  # type: ignore[method-assign]


def _error_then_ok(sandbox: LinuxSandbox) -> str:
    try:
        sandbox.call(_tool("return str(1 / 0)"), "probe", {"x": ""})
    except Exception as exc:
        if "ZeroDivisionError" not in str(exc):
            raise
    else:
        raise AssertionError("the error was not reported")
    return sandbox.call(_tool("return 'ok'"), "probe", {"x": ""})


def _timeout_then_ok(sandbox: LinuxSandbox) -> str:
    try:
        sandbox.call(_tool("while True:\n    pass"), "probe", {"x": ""})
    except Exception as exc:
        if "Timed out" not in str(exc):
            raise
    else:
        raise AssertionError("the endless loop was not stopped")
    return sandbox.call(_tool("return 'ok'"), "probe", {"x": ""})


def _parallel(sandbox: LinuxSandbox, code: str, count: int) -> list[str]:
    results: dict[int, str] = {}

    def call(number: int) -> None:
        results[number] = sandbox.call(code, "probe", {"x": str(number)})

    threads = [threading.Thread(target=call, args=(n,)) for n in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    return [results.get(n, "") for n in range(count)]


def run_all() -> list[Result]:
    """Run every probe on this computer. Raises :class:`SandboxError` if the sandbox is not available."""
    status = LinuxSandbox.status()
    if not status.available:
        raise SandboxError(f"The built-in sandbox is not available here: {status.reason}: {status.detail}")
    with tempfile.TemporaryDirectory(prefix="nodestro-probes-") as directory:
        canaries = _Canaries(Path(directory))
        try:
            results = [run_attack(probe, canaries) for probe in PROBES]
            results += host_probes(canaries)
            results += kernel_probe_refuses_untested(canaries)
        finally:
            problems = canaries.close()
        results.append(Result("canaries", "secrets", not problems, {"problems": repr(problems)}))
    results.append(Result("normal-user", "system", os.geteuid() != 0, {"uid": str(os.geteuid())}))
    return results


def main(argv: list[str] | None = None) -> int:
    """``python -m nodestro_runtime.sandbox_probes [--json] [--env-python PATH]``: security probes and compatibility
    checks; ``--env-python`` is the interpreter of a Python environment with numpy (the compiled-library check)."""
    args = sys.argv[1:] if argv is None else argv
    env_python = args[args.index("--env-python") + 1] if "--env-python" in args else None
    status = LinuxSandbox.status()
    header = {"system": status.system, "landlock_abi": status.landlock_abi, "available": status.available,
              "reason": status.reason, "detail": status.detail, "python": sys.executable,
              "kernel": os.uname().release, "uid": os.geteuid()}  # fmt: skip
    if not status.available:
        print(json.dumps({"status": header, "results": []}, indent=2))
        return 2
    results = run_all() + compat_checks(env_python)
    if "--json" in args:
        print(json.dumps({"status": header, "results": [r.__dict__ for r in results]}, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(header, ensure_ascii=False))
        for result in results:
            print(f"{'PASS' if result.passed else 'FAIL'}  {result.area:11} {result.name}  {result.error}")
            if not result.passed or result.area == "performance" or result.name == "compiled-library":
                for key, value in result.detail.items():
                    print(f"        {key}: {value}")
    passed = sum(r.passed for r in results)
    print(f"{passed} of {len(results)} checks passed", file=sys.stderr)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
