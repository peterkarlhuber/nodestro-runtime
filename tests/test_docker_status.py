"""The Docker sandbox's status: checked in the background, approved only after preflight and self-test (R2)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
import sys
import threading
import time
from typing import Any

from orkexa.dynamic import DockerSandbox, SandboxProbe
from orkexa.dynamic.docker import DockerHost, DockerProbeError
import pytest

from nodestro_runtime import docker_status, sandbox_selection
from nodestro_runtime.docker_status import ROOTFUL_NOTE, DockerMonitor, DockerStatus
from nodestro_runtime.sandbox_policy import SandboxPolicy, isolation_of
from nodestro_runtime.schemas import SandboxBackendStatus, Settings
from nodestro_runtime.workspace import Workspace

DOCKER = sys.executable  # an absolute path that exists: the CLI check passes, nothing is run with it

HOST = DockerHost(
    engine="docker",
    os_type="linux",
    arch="x86_64",
    rootless=False,
    cgroup_version="2",
    cgroup_driver="systemd",
    memory_limit=True,
    pids_limit=True,
    cpu_cfs_quota=True,
    cpu_cfs_period=True,
    seccomp_profile="builtin",
    apparmor=True,
    selinux=False,
    daemon_id="daemon-1",
    server_version="27.4.0",
)


class FakeSandbox:
    """Stands in for the runtime-image sandbox: counts self-tests and answers with ``probe``."""

    def __init__(self, probe: SandboxProbe | None = None) -> None:
        self.probe = probe or SandboxProbe("docker", True, facts=(("image_id", "sha256:" + "ab" * 32),))
        self.self_tests = 0

    def self_test(self) -> SandboxProbe:
        self.self_tests += 1
        return self.probe


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def monitor(
    sandbox: FakeSandbox,
    *,
    host: DockerHost | Exception = HOST,
    clock: Clock | None = None,
    inspected: list[int] | None = None,
    delay: threading.Event | None = None,
) -> DockerMonitor:
    def inspect(docker: str, *, env: Any = None) -> DockerHost:
        if delay is not None:
            delay.wait(10)
        if inspected is not None:
            inspected.append(1)
        if isinstance(host, Exception):
            raise host
        return host

    return DockerMonitor(
        lambda: sandbox,  # type: ignore[arg-type,return-value]
        docker=DOCKER,
        environ={"PATH": "/usr/bin"},
        inspect=inspect,
        clock=clock or Clock(),
    )


def test_the_first_status_is_checking_and_never_waits_for_the_daemon() -> None:
    gate = threading.Event()
    checked = monitor(FakeSandbox(), delay=gate)
    began = time.monotonic()
    first = checked.status()
    assert time.monotonic() - began < 1  # a daemon that does not answer never holds up a settings read
    assert (first.state, first.reason) == ("checking", "docker-checking")
    assert first.backend().state == "checking" and not first.backend().available
    gate.set()
    assert checked.wait(10).approved


def test_approved_only_after_preflight_and_self_test_and_proves_the_daemon() -> None:
    sandbox = FakeSandbox()
    checked = monitor(sandbox)
    status = checked.check()
    assert status.approved and sandbox.self_tests == 1
    assert [(c.name, c.ok) for c in status.checks] == [
        ("cli", True),
        ("daemon", True),
        ("preflight", True),
        ("image", True),
        ("self-test", True),
    ]
    row = status.backend()
    assert (row.engine, row.rootless, row.cgroup_version) == ("docker", False, "2")
    assert row.tested_at is not None and row.checks[-1].name == "self-test"
    assert docker_status.daemon_proven(DOCKER, {"PATH": "/usr/bin"})
    assert isolation_of(DockerSandbox(docker=DOCKER, environ={"PATH": "/usr/bin"})) == "container"


def test_the_self_test_is_kept_per_daemon_fingerprint_and_the_preflight_repeats() -> None:
    sandbox, clock, inspected = FakeSandbox(), Clock(), []
    checked = monitor(sandbox, clock=clock, inspected=inspected)
    checked.status()
    checked.wait(10)
    assert checked.status().approved and len(inspected) == 1  # within the preflight's TTL: no new check
    clock.now += docker_status.PREFLIGHT_TTL
    checked.status()
    assert checked.wait(10).approved and len(inspected) == 2 and sandbox.self_tests == 1
    checked._inspect = lambda docker, env=None: replace(HOST, server_version="28.0.0")  # another daemon
    clock.now += docker_status.PREFLIGHT_TTL
    checked.status()
    assert checked.wait(10).approved and sandbox.self_tests == 2
    checked.status(refresh=True)
    assert checked.wait(10).approved and sandbox.self_tests == 3  # asked for: the self-test runs again


def test_a_failed_self_test_is_retried_later_or_on_request() -> None:
    sandbox, clock = FakeSandbox(SandboxProbe("docker", False, "self-test", "CapEff is not 0")), Clock()
    checked = monitor(sandbox, clock=clock)
    status = checked.check()
    assert (status.state, status.reason) == ("unavailable", "docker-selftest") and "CapEff" in status.detail
    assert status.tested_at is not None and not docker_status.daemon_proven(DOCKER, {"PATH": "/usr/bin"})
    assert checked.check().reason == "docker-selftest" and sandbox.self_tests == 1
    clock.now += docker_status.RETRY_FAILED
    sandbox.probe = SandboxProbe("docker", True)
    assert checked.check().approved and sandbox.self_tests == 2


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"os_type": "windows"}, "docker-os-type"),
        ({"memory_limit": False}, "docker-limits"),
        ({"seccomp_profile": None}, "docker-seccomp"),
        ({"seccomp_profile": "unconfined"}, "docker-seccomp"),
        ({"rootless": True, "cgroup_version": "1"}, "docker-rootless-cgroup"),
    ],
)
def test_preflight_refusals_have_their_reason(change: dict[str, Any], reason: str) -> None:
    sandbox = FakeSandbox()
    docker_status.record_proof(DOCKER, {"PATH": "/usr/bin"}, SandboxProbe("docker", True))
    status = monitor(sandbox, host=replace(HOST, **change)).check()
    assert (status.state, status.reason) == ("unavailable", reason) and sandbox.self_tests == 0
    assert not docker_status.daemon_proven(DOCKER, {"PATH": "/usr/bin"})  # a refused daemon loses its proof
    assert docker_status.last_reason(DOCKER, {"PATH": "/usr/bin"}) == reason


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (DockerProbeError("unreachable", "Cannot connect"), "docker-unreachable"),
        (DockerProbeError("malformed", "no JSON"), "docker-unreachable"),
        (DockerProbeError("missing", "not installed"), "docker-missing"),
    ],
)
def test_daemon_problems_have_their_reason(error: Exception, reason: str) -> None:
    assert monitor(FakeSandbox(), host=error).check().reason == reason


def test_a_missing_cli_and_a_missing_image(tmp_path: Path) -> None:
    missing = DockerMonitor(lambda: FakeSandbox(), docker="docker", environ={"PATH": str(tmp_path)})  # type: ignore[arg-type,return-value]
    status = missing.check()
    assert status.reason == "docker-missing" and status.checks[0].name == "cli"
    image = FakeSandbox(SandboxProbe("docker", False, "image", "The sandbox image is not available"))
    found = monitor(image).check()
    assert found.reason == "docker-image" and found.checks[-1].name == "image"


def test_a_broken_check_reports_instead_of_raising() -> None:
    class Broken(FakeSandbox):
        def self_test(self) -> SandboxProbe:
            raise RuntimeError("boom")

    checked = monitor(Broken())
    checked.status()
    status = checked.wait(10)
    assert status.state == "unavailable" and "boom" in status.detail


def test_a_rootful_daemon_on_linux_carries_the_root_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker_status.sys, "platform", "linux")
    rootful = monitor(FakeSandbox()).check()
    assert ROOTFUL_NOTE in rootful.detail and ROOTFUL_NOTE in rootful.backend().notes
    rootless = monitor(FakeSandbox(), host=replace(HOST, rootless=True)).check()
    assert ROOTFUL_NOTE not in rootless.detail and rootless.backend().rootless is True
    desktop = monitor(FakeSandbox(), host=replace(HOST, engine="docker-desktop")).check()
    assert ROOTFUL_NOTE not in desktop.detail  # the daemon runs in Docker Desktop's virtual machine


def test_the_reaper_removes_all_own_containers_at_start_then_expired_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, bool]] = []
    done = threading.Event()

    def reap(docker: str, owner: str, *, remove_all: bool = False, env: Any = None) -> int:
        calls.append((owner, remove_all))
        if len(calls) >= 3:
            done.set()
        return 1

    monkeypatch.setattr(docker_status, "reap", reap)
    checked = monitor(FakeSandbox())
    checked.reap_interval = 0.05
    checked.start_reaper(remove_all_first=True)
    checked.start_reaper(remove_all_first=True)  # once per monitor
    assert done.wait(10)
    checked.close()
    assert calls[0] == ("nodestro", True) and all(call == ("nodestro", False) for call in calls[1:])


def test_a_reaper_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    from orkexa.dynamic import SandboxError

    def refuse(*args: Any, **kwargs: Any) -> int:
        raise SandboxError("GET /containers/json is not available through the gateway")

    monkeypatch.setattr(docker_status, "reap", refuse)
    assert monitor(FakeSandbox()).reap_once(remove_all=True) == 0


@pytest.mark.real_docker_status
def test_one_monitor_per_daemon_is_shared() -> None:
    make: Callable[[], Any] = FakeSandbox
    first = docker_status.monitor_for(make, environ={"DOCKER_HOST": "unix:///a.sock"})
    assert docker_status.monitor_for(make, environ={"DOCKER_HOST": "unix:///a.sock"}) is first
    assert docker_status.monitor_for(make, environ={"DOCKER_HOST": "unix:///b.sock"}) is not first
    docker_status.reset_monitors()
    assert docker_status.monitor_for(make, environ={"DOCKER_HOST": "unix:///a.sock"}) is not first


def test_a_missing_proof_asks_the_daemon_s_monitor_to_check(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[bool] = []

    class Recording(DockerMonitor):
        def status(self, *, refresh: bool = False, wait: float = 0.0) -> DockerStatus:
            asked.append(refresh)
            return docker_status.CHECKING

    key = docker_status.daemon_key("docker", {"DOCKER_HOST": "unix:///c.sock"})
    docker_status._MONITORS[key] = Recording(lambda: FakeSandbox())  # type: ignore[arg-type,return-value]
    assert not docker_status.daemon_proven("docker", {"DOCKER_HOST": "unix:///c.sock"}) and asked == [False]


def test_the_statuses_become_the_report_s_fields() -> None:
    row = DockerStatus("unavailable", "docker-limits", "no pids", host=HOST).backend()
    assert isinstance(row, SandboxBackendStatus)
    data = row.model_dump(mode="json")
    assert {"engine", "rootless", "cgroup_version", "checks", "tested_at"} <= set(data)
    assert SandboxBackendStatus.model_validate({"name": "docker"}).checks == []  # additive: old reports still read


# ---- the workspace --------------------------------------------------------------------------------------------


@pytest.mark.real_docker_status
def test_the_settings_never_wait_for_docker_info(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    created: list[DockerMonitor] = []

    def slow_monitor(sandbox: Callable[[], DockerSandbox], **kwargs: Any) -> DockerMonitor:
        assert callable(sandbox)  # the workspace's runtime-image sandbox; the test proves with a stand-in
        found = monitor(FakeSandbox(), delay=gate)
        created.append(found)
        return found

    monkeypatch.setattr(docker_status, "monitor_for", slow_monitor)
    monkeypatch.setattr(DockerMonitor, "start_reaper", lambda self, remove_all_first: None)
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        began = time.monotonic()
        settings: Settings = workspace.settings()
        assert time.monotonic() - began < 2
        docker = next(b for b in settings.sandbox_backends if b.name == "docker")
        assert (docker.state, docker.reason) == ("checking", "docker-checking")
        assert not settings.docker_available and workspace.docker_status().state == "checking"
        gate.set()
        assert created[0].wait(10).approved
        assert workspace.docker_status().state == "approved" and workspace.settings().docker_available
        assert workspace.docker_status(refresh=True, wait=10).state == "approved"
    finally:
        gate.set()
        workspace.close()


def test_the_workspace_starts_the_reaper_only_where_it_owns_the_containers(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        assert workspace.docker_monitor.reaps == [True]  # type: ignore[attr-defined]
        assert workspace.docker_status().reason == "docker-missing"
        selection = sandbox_selection.select(SandboxPolicy("desktop"), "docker", workspace.docker_monitor.status())
        assert selection.blocked and selection.reason == "docker-missing"
    finally:
        workspace.close()
