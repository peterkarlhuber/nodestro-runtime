"""Security and compatibility tests of the built-in Linux sandbox (ADR-0006).

The security probes (:mod:`nodestro_runtime.sandbox_probes`) run tool code that attacks the sandbox from the inside:
files outside the call, the data directory, keys and the parent environment, the network, new and foreign
processes, kernel interfaces, limits, cancel, timeout, cleanup and manipulated paths. A probe passes only if its
code **ran** in the sandbox and every attempt was refused; a sandbox that cannot start fails the probe.

The tests need Linux x86-64 with Landlock ABI 4 or newer on a tested distribution; elsewhere they are skipped,
unless ``NODESTRO_REQUIRE_SANDBOX=1`` (set in CI and in the system tests) turns the skip into a failure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import textwrap
import threading

from orkexa.dynamic import DockerSandbox, SubprocessSandbox
from orkexa.errors import ToolExecutionError
import pytest

from nodestro_runtime import builtin_sandbox, sandbox_confine, sandbox_probes
from nodestro_runtime.builtin_sandbox import TESTED_SYSTEMS, LinuxSandbox, SandboxStatus, measure
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.sandbox_policy import SandboxPolicy, isolation_of
from nodestro_runtime.tool_library import ToolLibrary

pytestmark = pytest.mark.real_sandbox_status  # this module checks the computer's real sandbox (see conftest.py)

STATUS = LinuxSandbox.status()
REQUIRED = os.environ.get("NODESTRO_REQUIRE_SANDBOX") == "1"

needs_sandbox = pytest.mark.skipif(
    not STATUS.available and not REQUIRED,
    reason=f"built-in sandbox not available here ({STATUS.reason}: {STATUS.detail}); "
    "NODESTRO_REQUIRE_SANDBOX=1 makes this a failure",
)


@pytest.fixture(scope="module")
def canaries() -> object:
    if not STATUS.available:
        pytest.fail(f"NODESTRO_REQUIRE_SANDBOX=1 but the built-in sandbox is not available: {STATUS}")
    with tempfile.TemporaryDirectory(prefix="nodestro-probes-") as directory:
        planted = sandbox_probes._Canaries(Path(directory))
        yield planted
        problems = planted.close()
        assert not problems, problems


@pytest.fixture(scope="module")
def sandbox() -> LinuxSandbox:
    if not STATUS.available:
        pytest.fail(f"NODESTRO_REQUIRE_SANDBOX=1 but the built-in sandbox is not available: {STATUS}")
    return LinuxSandbox(timeout=20.0, memory_mb=512, tmp_mb=16)


def tool(body: str, arg: str = "target") -> str:
    return (
        f"def probe({arg}: str) -> str:\n"
        '    """Probe.\n\n    Args:\n        target: Input.\n\n    Returns:\n        Output.\n    """\n'
        + textwrap.indent(textwrap.dedent(body).strip(), "    ")
        + "\n"
    )


# --- security probes ---------------------------------------------------------------------------------------------


@needs_sandbox
@pytest.mark.parametrize("probe", sandbox_probes.PROBES, ids=[p.name for p in sandbox_probes.PROBES])
def test_attack_is_refused(probe: sandbox_probes.Probe, canaries: sandbox_probes._Canaries) -> None:
    result = sandbox_probes.run_attack(probe, canaries)
    assert not result.error, result.error  # the probe ran inside the sandbox
    allowed = {k: v for k, v in result.detail.items() if v.startswith("ALLOWED") and k not in probe.allowed}
    assert result.passed, allowed or result.detail


@needs_sandbox
def test_limits_timeout_cancel_and_cleanup(canaries: sandbox_probes._Canaries) -> None:
    results = sandbox_probes.host_probes(canaries)
    failed = {r.name: (r.error, r.detail) for r in results if not r.passed}
    if os.geteuid() == 0:  # pragma: no cover - a root user is not limited by RLIMIT_NPROC (and is refused anyway)
        failed.pop("process-limit", None)
    assert not failed, failed


@needs_sandbox
def test_weakened_confinement_never_runs_tool_code(canaries: sandbox_probes._Canaries) -> None:
    [result] = sandbox_probes.kernel_probe_refuses_untested(canaries)
    assert result.passed, result.detail


@needs_sandbox
def test_an_older_kernel_is_refused_before_the_code_runs(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    weak = LinuxSandbox(timeout=20.0)
    weak._min_landlock_abi = 99  # as if the kernel's Landlock were older than required
    with pytest.raises(ToolExecutionError, match=r"could not confine the run.*landlock"):
        weak.call(tool(f"open({str(marker)!r}, 'w').write('x')\nreturn 'ran'"), "probe", {"target": ""})
    assert not marker.exists()


def test_platform_check_reports_missing_kernel_features(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_confine.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(sandbox_confine.sys, "platform", "linux")
    monkeypatch.setattr(sandbox_confine.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(sandbox_confine.os, "getegid", lambda: 1000)
    monkeypatch.setattr(sandbox_confine, "landlock_abi", lambda: 0)
    with pytest.raises(sandbox_confine.Unavailable, match="Landlock not enabled"):
        sandbox_confine._check_platform()
    monkeypatch.setattr(sandbox_confine, "landlock_abi", lambda: 3)
    with pytest.raises(sandbox_confine.Unavailable, match="ABI 3, the sandbox needs ABI 4"):
        sandbox_confine._check_platform()
    monkeypatch.setattr(sandbox_confine.os, "geteuid", lambda: 0)
    with pytest.raises(sandbox_confine.Unavailable, match="root"):
        sandbox_confine._check_platform()


def test_a_protected_directory_is_never_granted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_confine.sys, "path", ["/"])
    with pytest.raises(sandbox_confine.Unavailable, match="contains the protected path"):
        sandbox_confine.interpreter_paths([str(Path.home())])


def test_an_untested_distribution_is_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builtin_sandbox, "os_release", lambda path="": ("fedora", "41"))
    monkeypatch.setattr(builtin_sandbox.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(builtin_sandbox.sys, "platform", "linux")
    status = builtin_sandbox._check()
    assert not status.available and status.reason == "untested-system"
    assert "Ubuntu 24.04 and Debian 13" in status.detail


def test_os_release_is_parsed(tmp_path: Path) -> None:
    release = tmp_path / "os-release"
    release.write_text('NAME="Debian GNU/Linux"\nVERSION_ID="13"\nID=debian\n', encoding="utf-8")
    assert builtin_sandbox.os_release(str(release)) == ("debian", "13")
    assert builtin_sandbox.os_release(str(tmp_path / "missing")) == ("", "")
    assert ("ubuntu", "24.04") in TESTED_SYSTEMS and ("debian", "13") in TESTED_SYSTEMS


def test_the_seccomp_program_is_well_formed() -> None:
    program = sandbox_confine.seccomp_program(4242)
    assert len(program) % 8 == 0 and 0 < len(program) // 8 < 4096
    # every jump stays inside the program
    for index in range(len(program) // 8):
        code, jt, jf, _k = sandbox_confine.struct.unpack("<HBBI", program[index * 8 : index * 8 + 8])
        if code in (sandbox_confine._JEQ_K, sandbox_confine._JGE_K, sandbox_confine._JSET_K):
            assert index + 1 + max(jt, jf) < len(program) // 8


# --- compatibility -----------------------------------------------------------------------------------------------


@needs_sandbox
def test_standard_library_modules_tools_use(sandbox: LinuxSandbox) -> None:
    body = """
        import csv, datetime, decimal, io, json, os, re, sqlite3, zipfile, zoneinfo
        rows = list(csv.reader(io.StringIO('a,b\\n1,2')))
        path = os.path.join(os.environ['TMPDIR'], 'daten-ü.db')
        db = sqlite3.connect(path)
        db.execute('create table t (x)')
        db.execute('insert into t values (?)', (target,))
        db.commit()
        archive_path = os.path.join(os.environ['TMPDIR'], 'archiv.zip')
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('ä.txt', target)
        with zipfile.ZipFile(archive_path) as archive:
            unpacked = archive.read('ä.txt').decode()
        total = decimal.Decimal('0.1') + decimal.Decimal('0.2')
        day = datetime.datetime(2026, 10, 31, 12, tzinfo=zoneinfo.ZoneInfo('Europe/Berlin')).isoformat()
        return json.dumps([rows, db.execute('select x from t').fetchone()[0], unpacked, str(total), day,
                           re.sub('[aeiou]', '', target)])
    """
    expected = [[["a", "b"], ["1", "2"]], "Grüße", "Grüße", "0.3", "2026-10-31T12:00:00+01:00", "Grüß"]
    assert json.loads(sandbox.call(tool(body), "probe", {"target": "Grüße"})) == expected


@needs_sandbox
def test_validation_with_tests_and_non_ascii_paths(sandbox: LinuxSandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    code = (
        "def greet(name: str) -> str:\n"
        '    """Greet someone.\n\n    Args:\n        name: Who.\n\n    Returns:\n        The greeting.\n    """\n'
        "    return f'Grüß dich, {name}!'\n"
    )
    tests = "from tool import greet\n\n\ndef test_grüße():\n    assert greet('Zoë') == 'Grüß dich, Zoë!'\n"
    with tempfile.TemporaryDirectory(prefix="nodestro-äöü-") as directory:
        monkeypatch.setattr(tempfile, "tempdir", directory)
        result = sandbox.validate(code, tests, "greet")
    assert result.ok, result.feedback()
    assert [t.ok for t in result.tests] == [True]


@needs_sandbox
def test_repeated_and_parallel_calls(sandbox: LinuxSandbox) -> None:
    code = tool("import hashlib\nreturn hashlib.sha256(target.encode()).hexdigest()[:8]")
    results: dict[int, str] = {}

    def call(number: int) -> None:
        results[number] = sandbox.call(code, "probe", {"target": str(number)})

    threads = [threading.Thread(target=call, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert len(results) == 8 and len(set(results.values())) == 8
    for _ in range(5):
        assert sandbox.call(code, "probe", {"target": "1"}) == results[1]


@needs_sandbox
def test_a_tool_error_is_reported_and_the_next_call_works(sandbox: LinuxSandbox) -> None:
    with pytest.raises(ToolExecutionError, match="ZeroDivisionError"):
        sandbox.call(tool("return str(1 / 0)"), "probe", {"target": ""})
    assert sandbox.call(tool("return target"), "probe", {"target": "ok"}) == "ok"


@needs_sandbox
def test_output_past_the_runner_is_bounded_in_the_host(sandbox: LinuxSandbox) -> None:
    # The memory limit covers the confined child, not what the host reads from its stdout: reading stops at the
    # response limit and the call is stopped (audit finding B-01).
    flood = """
    import os
    block = b"x" * 1048576
    for _ in range(256):
        os.write(1, block)
    return target
    """
    with pytest.raises(ToolExecutionError, match="bytes of output; the run was stopped"):
        sandbox.call(tool(flood), "probe", {"target": ""})
    assert sandbox.call(tool("return target"), "probe", {"target": "ok"}) == "ok"


@needs_sandbox
def test_start_up_overhead_is_measured(sandbox: LinuxSandbox) -> None:
    timings = measure(sandbox, calls=5)
    host = measure(SubprocessSandbox(acknowledge_unsafe=True), calls=5)  # trusted ping code only
    assert timings["mean_ms"] < 3000
    print(f"built-in sandbox {timings['mean_ms']:.0f} ms per call, host subprocess {host['mean_ms']:.0f} ms")


# --- policy ------------------------------------------------------------------------------------------------------

_DOWN = SandboxStatus(False, "landlock", "Landlock not enabled in this kernel", "ubuntu 24.04", 0)
_UP = SandboxStatus(True, "", "", "ubuntu 24.04", 4)


def test_isolating_only_where_the_checks_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = LinuxSandbox()
    monkeypatch.setattr(LinuxSandbox, "status", staticmethod(lambda refresh=False: _DOWN))
    assert isolation_of(sandbox) == "none"
    assert not SandboxPolicy("desktop").allows("untrusted", sandbox)
    monkeypatch.setattr(LinuxSandbox, "status", staticmethod(lambda refresh=False: _UP))
    assert isolation_of(sandbox) == "kernel"
    assert SandboxPolicy("desktop").allows("untrusted", sandbox)
    # A server keeps its tenants' code in containers.
    assert not SandboxPolicy("server").allows("untrusted", sandbox)
    assert not SandboxPolicy("server").allows("user-authored", sandbox)
    assert SandboxPolicy("server").allows("untrusted", DockerSandbox())


def test_untrusted_code_is_routed_to_the_builtin_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(LinuxSandbox, "status", staticmethod(lambda refresh=False: _UP))
    library = ToolLibrary(tmp_path / "tools", policy=SandboxPolicy("desktop"))
    host = library.registry.sandbox
    assert isinstance(host, SubprocessSandbox)
    routed = library._require("untrusted", host, "x")
    assert isinstance(routed, LinuxSandbox) and routed.python == host.python
    assert str(tmp_path) in routed.protected
    assert library._require("user-authored", host, "x") is host  # a person's own code keeps the configured sandbox
    environment = SubprocessSandbox(python="/opt/env/bin/python", timeout=60.0, memory_mb=2048, acknowledge_unsafe=True)
    routed = library._require("untrusted", environment, "x")
    assert isinstance(routed, LinuxSandbox) and routed.python == "/opt/env/bin/python" and routed.memory_mb == 2048


def test_without_the_builtin_sandbox_untrusted_code_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(LinuxSandbox, "status", staticmethod(lambda refresh=False: _DOWN))
    library = ToolLibrary(tmp_path / "tools", policy=SandboxPolicy("desktop"))
    with pytest.raises(NodestroError) as caught:
        library._require("untrusted", library.registry.sandbox, "x")
    assert caught.value.code == "sandbox.isolation_required"
    assert caught.value.params["reason"] == "landlock"
    assert "Landlock not enabled" in str(caught.value)
    # The host subprocess is never used instead.
    tests = "from tool import probe\n\n\ndef test_probe():\n    assert probe('a') == 'a'\n"
    result = library.check(tool("return target"), tests, "probe", trust="untrusted")
    assert not result.ok and result.errors[0].code == "sandbox.isolation_required"


# --- the desktop workspace -----------------------------------------------------------------------------------------


@needs_sandbox
def test_generated_tools_run_in_the_builtin_sandbox_and_never_on_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
    from nodestro_runtime.workspace import Workspace

    def host(*_args: object) -> str:
        raise AssertionError("untrusted code reached the host subprocess")

    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        assert workspace.sandbox_policy.profile == "desktop"
        (tmp_path / "data" / "secret.txt").write_text("secret", encoding="utf-8")
        monkeypatch.setattr(SubprocessSandbox, "_execute", host)
        saved, info = workspace.tools.save_custom(
            CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, "celsius_to_fahrenheit", requirement="Convert temperatures"
        )
        assert info is not None and info.trust == "untrusted", saved
        assert workspace.tools.run("celsius_to_fahrenheit", {"celsius": 100}) == ("212.0", False)
        # The static check refuses open(); the sandbox the policy picked must hold where a check misses a way.
        routed = workspace.tools._require("untrusted", workspace.tools.registry.sandbox, "probe")
        assert isinstance(routed, LinuxSandbox)
        reader = tool(f"return open({str(tmp_path / 'data' / 'secret.txt')!r}).read()")
        with pytest.raises(ToolExecutionError, match="PermissionError"):
            routed.call(reader, "probe", {"target": ""})
    finally:
        workspace.close()
