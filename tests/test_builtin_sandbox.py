"""Acceptance tests of the built-in sandbox prototype (ADR-0006, "Sandbox prototype acceptance tests (PR-13)").

Every security probe is tool code that tries something from inside the sandbox and reports what happened; the test
requires the attempt to fail. The tests need Linux x86-64 with ``bwrap`` and unprivileged user namespaces; without
them they are skipped, unless ``NODESTRO_REQUIRE_BWRAP=1`` (set in CI) turns the skip into a failure.

Passing tests do not make the prototype isolating: the policy keeps classifying it as ``"none"`` (last test).
"""

from __future__ import annotations

from collections.abc import Iterator
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import textwrap
import time

from orkexa.dynamic import SubprocessSandbox
from orkexa.dynamic.errors import SandboxError
from orkexa.errors import ToolExecutionError
import pytest

from nodestro_runtime.builtin_sandbox import BubblewrapSandbox, measure, seccomp_filter
from nodestro_runtime.sandbox_policy import SandboxPolicy, isolation_of

AVAILABLE = BubblewrapSandbox.available()
REQUIRED = os.environ.get("NODESTRO_REQUIRE_BWRAP") == "1"

needs_bwrap = pytest.mark.skipif(
    not AVAILABLE and not REQUIRED,
    reason="needs Linux x86-64 with bwrap and user namespaces (NODESTRO_REQUIRE_BWRAP=1 makes this a failure)",
)


@pytest.fixture(scope="module")
def sandbox() -> BubblewrapSandbox:
    if not AVAILABLE:
        pytest.fail("NODESTRO_REQUIRE_BWRAP=1 but the bubblewrap sandbox cannot run here")
    return BubblewrapSandbox(timeout=10.0, memory_mb=256, tmp_mb=8)


def probe(body: str) -> str:
    """Tool code whose body tries something; it returns ``BLOCKED: …`` when an exception stopped it."""
    indented = textwrap.indent(textwrap.dedent(body).strip(), "        ")
    return (
        "def probe(target: str) -> str:\n"
        '    """Try something the sandbox must prevent.\n\n'
        "    Args:\n        target: What to try it on.\n\n"
        '    Returns:\n        What happened.\n    """\n'
        "    try:\n"
        f"{indented}\n"
        "    except BaseException as exc:  # noqa: BLE001\n"
        "        return 'BLOCKED: ' + type(exc).__name__ + ': ' + str(exc)\n"
    )


def run(sandbox: BubblewrapSandbox, body: str, target: str = "") -> str:
    return sandbox.call(probe(body), "probe", {"target": target})


@pytest.fixture
def host_secret() -> Iterator[Path]:
    """A file of the user's, outside the sandbox (as the data directory or home directory would be)."""
    with tempfile.TemporaryDirectory(prefix="nodestro-host-") as directory:
        path = Path(directory) / "provider-key.txt"
        path.write_text("sk-host-secret", encoding="utf-8")
        yield path


# --- file system -------------------------------------------------------------------------------------------------


@needs_bwrap
def test_files_outside_the_call_cannot_be_read(sandbox: BubblewrapSandbox, host_secret: Path) -> None:
    result = run(sandbox, "return 'READ: ' + open(target).read()", str(host_secret))
    assert result.startswith("BLOCKED: FileNotFoundError"), result
    home = run(sandbox, "import os\nreturn 'LISTED: ' + str(os.listdir(os.path.expanduser(target)))", str(Path.home()))
    assert home.startswith("BLOCKED"), home


@needs_bwrap
def test_only_the_interpreter_and_its_libraries_are_visible(sandbox: BubblewrapSandbox) -> None:
    result = run(sandbox, "import os\nreturn 'ROOT: ' + ' '.join(sorted(os.listdir('/')))")
    visible = set(result.removeprefix("ROOT: ").split())
    assert not visible & {"etc", "root", "var", "srv", "mnt", "media", "boot", "sys", "run"}, result
    for path in ("/bin/sh", "/usr/bin/env", "/etc/passwd", "/etc/hosts"):
        assert run(sandbox, "return 'FOUND: ' + str(__import__('os').stat(target))", path).startswith("BLOCKED"), path


@needs_bwrap
def test_files_outside_the_scratch_directory_cannot_be_written(sandbox: BubblewrapSandbox, host_secret: Path) -> None:
    for target in ("/work/tool.py", "/work/new.txt", str(host_secret), str(host_secret.parent / "dropped.txt")):
        result = run(sandbox, "open(target, 'w').write('x')\nreturn 'WROTE'", target)
        assert result.startswith("BLOCKED"), (target, result)
    assert host_secret.read_text(encoding="utf-8") == "sk-host-secret"
    assert not (host_secret.parent / "dropped.txt").exists()


@needs_bwrap
def test_the_scratch_directory_is_private_to_the_call(sandbox: BubblewrapSandbox) -> None:
    marker = f"nodestro-marker-{os.getpid()}"
    assert run(sandbox, "open('/tmp/' + target, 'w').write('x')\nreturn 'WROTE'", marker) == "WROTE"
    assert not (Path(tempfile.gettempdir()) / marker).exists()
    assert run(sandbox, "return 'FOUND: ' + open('/tmp/' + target).read()", marker).startswith("BLOCKED")


# --- network -----------------------------------------------------------------------------------------------------


@needs_bwrap
def test_no_tcp_connection_not_even_to_loopback(sandbox: BubblewrapSandbox) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:  # stands in for the studio API
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        result = run(
            sandbox,
            "import socket\nsocket.create_connection(('127.0.0.1', int(target)), timeout=2)\nreturn 'CONNECTED'",
            str(port),
        )
    assert result.startswith("BLOCKED"), result
    connect = "import socket\nsocket.create_connection((target, 443), timeout=2)\nreturn 'CONNECTED'"
    outside = run(sandbox, connect, "1.1.1.1")
    assert outside.startswith("BLOCKED"), outside


@needs_bwrap
def test_no_udp_and_no_name_resolution(sandbox: BubblewrapSandbox) -> None:
    udp = run(
        sandbox,
        "import socket\ns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "s.sendto(b'x', (target, 53))\nreturn 'SENT'",
        "8.8.8.8",
    )
    assert udp.startswith("BLOCKED"), udp
    dns = run(sandbox, "import socket\nreturn 'RESOLVED: ' + str(socket.getaddrinfo(target, 443))", "example.com")
    assert dns.startswith("BLOCKED"), dns


# --- processes ---------------------------------------------------------------------------------------------------


@needs_bwrap
def test_no_other_programs_and_no_child_processes(sandbox: BubblewrapSandbox) -> None:
    for body in (
        "import subprocess\n"
        "return 'RAN: ' + subprocess.run(['/bin/sh', '-c', 'id'], capture_output=True, text=True).stdout",
        "import os, sys\npid = os.fork()\nif pid == 0:\n    os._exit(0)\nreturn 'FORKED'",
        "import subprocess, sys\nreturn 'RAN: ' + str(subprocess.run([sys.executable, '-c', 'pass']).returncode)",
        "import multiprocessing\np = multiprocessing.Process(target=print)\np.start()\np.join()\nreturn 'STARTED'",
    ):
        result = run(sandbox, body)
        assert result.startswith("BLOCKED"), (body, result)


@needs_bwrap
def test_threads_still_work(sandbox: BubblewrapSandbox) -> None:
    body = (
        "import threading\nout = []\nt = threading.Thread(target=lambda: out.append(target))\nt.start()\nt.join()\n"
        "return 'THREAD: ' + out[0]"
    )
    assert run(sandbox, body, "ok") == "THREAD: ok"


@needs_bwrap
def test_the_host_processes_are_invisible_and_nothing_survives_the_call(sandbox: BubblewrapSandbox) -> None:
    result = run(sandbox, "import os\nreturn 'PIDS: ' + ' '.join(p for p in os.listdir('/proc') if p.isdigit())")
    assert len(result.removeprefix("PIDS: ").split()) <= 3, result
    run(sandbox, "import time\nreturn 'DONE'")
    leftovers = subprocess.run(["pgrep", "-f", "_sandbox_runner.py"], capture_output=True, text=True, check=False)
    assert leftovers.stdout.strip() == "", leftovers.stdout


@needs_bwrap
def test_kernel_interfaces_for_escapes_are_refused(sandbox: BubblewrapSandbox) -> None:
    body = """
        import ctypes, os
        libc = ctypes.CDLL(None, use_errno=True)
        calls = {'ptrace': (101, 16, 1, 0, 0), 'unshare': (272, 0x10000000), 'mount': (165, 0, 0, 0, 0, 0),
                 'bpf': (321, 0, 0, 0), 'keyctl': (250, 0, 0), 'userfaultfd': (323, 0), 'io_uring_setup': (425, 1, 0),
                 'setns': (308, 0, 0), 'perf_event_open': (298, 0, 0, 0, 0, 0)}
        allowed = []
        for name, args in calls.items():
            if libc.syscall(*args) != -1:
                allowed.append(name)
        return 'ALLOWED: ' + ' '.join(allowed) if allowed else 'BLOCKED: all'
    """
    assert run(sandbox, body) == "BLOCKED: all"


# --- secrets in the environment ----------------------------------------------------------------------------------


@needs_bwrap
def test_the_parent_environment_and_launch_secret_are_not_visible(
    sandbox: BubblewrapSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODESTRO_LAUNCH_SECRET", "launch-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-host")
    body = """
        import os
        seen = dict(os.environ)
        for pid in [p for p in os.listdir('/proc') if p.isdigit()]:
            try:
                seen[pid] = open(f'/proc/{pid}/environ', 'rb').read().decode(errors='replace')
            except OSError:
                pass
        return 'SEEN: ' + repr(seen)
    """
    result = run(sandbox, body)
    assert "launch-secret-value" not in result and "sk-ant-host" not in result, result


# --- resources ---------------------------------------------------------------------------------------------------


@needs_bwrap
def test_an_endless_loop_is_stopped() -> None:
    sandbox = BubblewrapSandbox(timeout=3.0)
    started = time.monotonic()
    with pytest.raises(ToolExecutionError):
        run(sandbox, "while True:\n    pass")
    assert time.monotonic() - started < 15


@needs_bwrap
def test_memory_beyond_the_limit_is_refused(sandbox: BubblewrapSandbox) -> None:
    result = run(sandbox, "block = bytearray(1024 * 1024 * 1024)\nreturn 'ALLOCATED'")
    assert result.startswith("BLOCKED: MemoryError"), result


@needs_bwrap
def test_disk_beyond_the_limit_is_refused(sandbox: BubblewrapSandbox) -> None:
    body = (
        "with open('/tmp/fill', 'wb') as f:\n    for _ in range(64):\n        f.write(b'x' * 1024 * 1024)\n"
        "return 'FILLED'"
    )
    result = run(sandbox, body)
    assert result.startswith("BLOCKED"), result


# --- compatibility -----------------------------------------------------------------------------------------------


@needs_bwrap
def test_standard_library_modules_tools_use(sandbox: BubblewrapSandbox) -> None:
    body = """
        import csv, datetime, decimal, io, json, re, sqlite3, zipfile
        rows = list(csv.reader(io.StringIO('a,b\\n1,2')))
        db = sqlite3.connect(':memory:')
        db.execute('create table t (x)')
        db.execute('insert into t values (?)', (target,))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('ä.txt', target)
        total = decimal.Decimal('0.1') + decimal.Decimal('0.2')
        day = datetime.date(2026, 10, 31).isoformat()
        return json.dumps([rows, db.execute('select x from t').fetchone()[0], len(buffer.getvalue()) > 0,
                           str(total), day, re.sub('[aeiou]', '', target)])
    """
    expected = [[["a", "b"], ["1", "2"]], "Grüße", True, "0.3", "2026-10-31", "Grüß"]
    assert json.loads(run(sandbox, body, "Grüße")) == expected


@needs_bwrap
def test_validation_with_tests_and_non_ascii_paths(sandbox: BubblewrapSandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    code = (
        "def greet(name: str) -> str:\n"
        '    """Greet someone.\n\n    Args:\n        name: Who.\n\n    Returns:\n        The greeting.\n    """\n'
        "    return f'Grüß dich, {name}!'\n"
    )
    tests = "from tool import greet\n\n\ndef test_greet():\n    assert greet('Zoë') == 'Grüß dich, Zoë!'\n"
    with tempfile.TemporaryDirectory(prefix="nodestro-äöü-") as directory:
        monkeypatch.setattr(tempfile, "tempdir", directory)
        result = sandbox.validate(code, tests, "greet")
    assert result.ok, result.summary() if hasattr(result, "summary") else result
    assert [t.ok for t in result.tests] == [True]


@needs_bwrap
def test_start_up_overhead_is_measured(sandbox: BubblewrapSandbox) -> None:
    timings = measure(sandbox, calls=3)
    host = measure(SubprocessSandbox(acknowledge_unsafe=True), calls=3)
    assert timings["mean_ms"] > 0 and host["mean_ms"] > 0
    print(f"bubblewrap {timings['mean_ms']:.0f} ms per call, host subprocess {host['mean_ms']:.0f} ms")


# --- classification ----------------------------------------------------------------------------------------------


def test_the_seccomp_program_is_well_formed() -> None:
    program = seccomp_filter("x86_64")
    assert len(program) % 8 == 0 and 0 < len(program) // 8 < 4096
    with pytest.raises(SandboxError):
        seccomp_filter("aarch64")


def test_the_prototype_is_not_classified_as_isolating() -> None:
    sandbox = BubblewrapSandbox.__new__(BubblewrapSandbox)  # no bwrap needed to classify
    assert isolation_of(sandbox) == "none"
    assert not SandboxPolicy("desktop").allows("untrusted", sandbox)
    assert BubblewrapSandbox.isolated is False
