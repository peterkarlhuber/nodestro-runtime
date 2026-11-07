"""The Docker sandbox's status: checked in the background, approved only after preflight and self-test (R2)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
import re
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


BUSY = SandboxProbe("docker", False, "image-busy", "Nodestro sandbox gateway: Another image build is running")


def test_a_gateway_busy_with_another_build_keeps_checking_with_a_bounded_backoff() -> None:
    """LD-020: the first sandbox image build of a restarted Studio meets the build of the previous one."""
    sandbox, clock = FakeSandbox(BUSY), Clock()
    checked = monitor(sandbox, clock=clock)
    waits = []
    for _ in range(7):
        status = checked.check()
        assert (status.state, status.reason) == ("checking", "docker-checking")
        assert status.checks[-1].name == "image" and not status.checks[-1].ok
        assert not status.backend().available and status.backend().state == "checking"
        assert checked.check().state == "checking"  # before the next try: no new self-test
        wait = checked._busy_retry_at - clock.now
        waits.append(wait)
        clock.now += wait
    assert waits == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0] and sandbox.self_tests == 7
    assert not docker_status.daemon_proven(DOCKER, {"PATH": "/usr/bin"})
    sandbox.probe = SandboxProbe("docker", True, facts=(("image_id", "sha256:" + "ab" * 32),))
    assert checked.check().approved and sandbox.self_tests == 8
    assert checked._busy_since is None and checked._busy_count == 0  # the next refusal starts at 5 s again


def test_a_real_image_failure_after_a_busy_gateway_is_docker_image() -> None:
    sandbox, clock = FakeSandbox(BUSY), Clock()
    checked = monitor(sandbox, clock=clock)
    assert checked.check().state == "checking"
    clock.now += docker_status.BUSY_RETRY_FIRST
    sandbox.probe = SandboxProbe("docker", False, "image", "Building the container image failed: exit code 1")
    status = checked.check()
    assert (status.state, status.reason) == ("unavailable", "docker-image") and "exit code 1" in status.detail
    clock.now += docker_status.BUSY_RETRY_MAX  # a real failure waits for the usual retry again
    assert checked.check().reason == "docker-image" and sandbox.self_tests == 2


def test_a_gateway_that_stays_busy_is_reported_as_an_image_problem_in_the_end() -> None:
    sandbox, clock = FakeSandbox(BUSY), Clock()
    checked = monitor(sandbox, clock=clock)
    assert checked.check().state == "checking"
    clock.now += docker_status.BUSY_LIMIT
    status = checked.check()
    assert (status.state, status.reason) == ("unavailable", "docker-image") and "Another image build" in status.detail
    assert sandbox.self_tests == 2  # tried once more (still busy), then given up on
    clock.now += docker_status.BUSY_RETRY_MAX
    assert checked.check().reason == "docker-image" and sandbox.self_tests == 2  # now the usual retry interval
    clock.now += docker_status.RETRY_FAILED
    assert checked.check().reason == "docker-image" and sandbox.self_tests == 3


def test_a_busy_gateway_schedules_the_next_check_by_itself() -> None:
    scheduled: list[tuple[float, Callable[[], object]]] = []
    clock = Clock()
    checked = DockerMonitor(
        lambda: FakeSandbox(BUSY),  # type: ignore[arg-type,return-value]
        docker=DOCKER,
        environ={"PATH": "/usr/bin"},
        inspect=lambda docker, env=None: HOST,
        clock=clock,
        schedule=lambda seconds, function: scheduled.append((seconds, function)),
    )
    checked.status()
    assert checked.wait(10).state == "checking"
    assert [seconds for seconds, _ in scheduled] == [docker_status.BUSY_RETRY_FIRST]
    assert scheduled[0][1] == checked.status
    clock.now += docker_status.BUSY_RETRY_FIRST
    scheduled[0][1]()  # the timer fires: the next check runs and schedules the one after
    assert checked.wait(10).state == "checking" and [seconds for seconds, _ in scheduled] == [5.0, 10.0]
    approved = DockerMonitor(
        lambda: FakeSandbox(),  # type: ignore[arg-type,return-value]
        docker=DOCKER,
        environ={"PATH": "/usr/bin"},
        inspect=lambda docker, env=None: HOST,
        clock=Clock(),
        schedule=lambda seconds, function: scheduled.append((seconds, function)),
    )
    approved.status()
    assert approved.wait(10).approved and len(scheduled) == 2  # nothing to wait for: nothing scheduled


def test_a_busy_gateway_never_revokes_a_daemon_s_proof() -> None:
    environ = {"PATH": "/usr/bin"}
    docker_status.record_proof(DOCKER, environ, SandboxProbe("docker", True))
    docker_status.record_proof(DOCKER, environ, BUSY)
    assert docker_status.daemon_proven(DOCKER, environ)
    assert docker_status.reason_of("image-busy") == "docker-checking"


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
    monkeypatch.setattr(DockerMonitor, "reap_mcp_once", lambda self, started_before=None: 0)
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


class FakeContainers:
    """Stands in for ``list_sandbox_containers`` and ``docker rm -f`` of the MCP reaper."""

    def __init__(self, containers: list[Any]) -> None:
        self.containers = {container.id: container for container in containers}
        self.listed: list[str] = []
        self.removed: list[str] = []

    def list(self, docker: str, owner: str, *, env: Any = None) -> list[Any]:
        self.listed.append(owner)
        return [container for container in self.containers.values() if container.owner == owner]

    def remove(self, _monitor: DockerMonitor, container_id: str) -> bool:
        self.removed.append(container_id)
        self.containers.pop(container_id, None)
        return True


def _mcp_containers(now: float) -> list[Any]:
    from orkexa.dynamic.docker import SandboxContainer

    lifetime = docker_status.MCP_LIFETIME
    mine = "nodestro-mcp-0123456789ab"
    return [
        # Started a minute before this process (its connection's process is gone): left behind.
        SandboxContainer("left", "nodestro-sandbox-mcp-left", mine, now - 60 + lifetime, True),
        # Past its deadline.
        SandboxContainer("expired", "nodestro-sandbox-mcp-expired", mine, now - 10, True),
        # Without a valid deadline.
        SandboxContainer("broken", "nodestro-sandbox-mcp-broken", mine, None, False),
        # Started by this process after its reaper began.
        SandboxContainer("fresh", "nodestro-sandbox-mcp-fresh", mine, now + 5 + lifetime, True),
        # Another installation's on the same daemon (another owner): never even listed.
        SandboxContainer("other", "nodestro-sandbox-mcp-other", "nodestro-mcp-ba9876543210", now - 10, True),
    ]


def test_left_behind_mcp_containers_are_reaped_at_the_start_then_expired_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = time.time()
    fake = FakeContainers(_mcp_containers(now))
    monkeypatch.setattr(docker_status, "list_sandbox_containers", fake.list)
    monkeypatch.setattr(DockerMonitor, "_remove", lambda self, container_id: fake.remove(self, container_id))
    monkeypatch.setattr(docker_status, "reap", lambda *args, **kwargs: 0)
    checked = monitor(FakeSandbox())
    checked.owner = "nodestro-0123456789ab"
    assert checked.reap_mcp_once(started_before=now) == 3
    assert sorted(fake.removed) == ["broken", "expired", "left"]
    assert set(fake.listed) == {"nodestro-mcp-0123456789ab"}  # only the installation's own MCP owner
    assert set(fake.containers) == {"fresh", "other"}
    # Later runs remove only containers past their deadline: a running connection's container stays.
    assert checked.reap_mcp_once() == 0 and set(fake.containers) == {"fresh", "other"}
    fake.containers["fresh"] = replace(fake.containers["fresh"], deadline=time.time() - 1)
    assert checked.reap_mcp_once() == 1 and set(fake.containers) == {"other"}


def test_the_reaper_thread_reaps_mcp_containers_at_start_and_periodically(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float | None] = []
    done = threading.Event()

    def reap_mcp_once(self: DockerMonitor, *, started_before: float | None = None) -> int:
        calls.append(started_before)
        if len(calls) >= 3:
            done.set()
        return 0

    monkeypatch.setattr(docker_status, "reap", lambda *args, **kwargs: 0)
    monkeypatch.setattr(DockerMonitor, "reap_mcp_once", reap_mcp_once)
    began = time.time()
    checked = monitor(FakeSandbox())
    checked.reap_interval = 0.05
    checked.start_reaper(remove_all_first=True)
    assert done.wait(10)
    checked.close()
    assert calls[0] is not None and began <= calls[0] <= time.time()
    assert all(call is None for call in calls[1:])
    # A server's processes (which share the daemon and owner) reap only expired MCP containers, also at the start.
    calls.clear()
    done.clear()
    shared = monitor(FakeSandbox())
    shared.reap_interval = 0.05
    shared.start_reaper(remove_all_first=False)
    assert done.wait(10)
    shared.close()
    assert all(call is None for call in calls)


def test_a_server_s_processes_reap_only_expired_mcp_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    from orkexa.dynamic.docker import SandboxContainer

    now = time.time()
    shared = "nodestro-mcp"
    fake = FakeContainers(
        [
            SandboxContainer("running", "a", shared, now - 60 + docker_status.MCP_LIFETIME, True),
            SandboxContainer("expired", "b", shared, now - 1, True),
        ]
    )
    monkeypatch.setattr(docker_status, "list_sandbox_containers", fake.list)
    monkeypatch.setattr(DockerMonitor, "_remove", lambda self, container_id: fake.remove(self, container_id))
    assert monitor(FakeSandbox()).reap_mcp_once() == 1
    assert fake.removed == ["expired"] and fake.listed == [shared]


def test_the_mcp_reaper_is_quiet_without_docker_or_an_mcp_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    from orkexa.dynamic import SandboxError

    def refuse(*args: Any, **kwargs: Any) -> list[Any]:
        raise SandboxError("GET /containers/json is not available through the gateway")

    monkeypatch.setattr(docker_status, "list_sandbox_containers", refuse)
    assert monitor(FakeSandbox()).reap_mcp_once(started_before=time.time()) == 0
    foreign = monitor(FakeSandbox())
    foreign.owner = "orkexa"  # not one of Nodestro's owners: it has no MCP owner
    assert foreign.mcp_container_owner() is None and foreign.reap_mcp_once() == 0


def test_an_mcp_container_is_removed_with_the_daemon_s_cli_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    seen: list[tuple[list[str], Any]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append((args, kwargs.get("env")))
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(docker_status.subprocess, "run", run)
    assert monitor(FakeSandbox())._remove("abc") is True
    assert seen == [([DOCKER, "rm", "-f", "abc"], {"PATH": "/usr/bin"})]


def test_stdio_containers_carry_the_deadline_the_mcp_reaper_assumes() -> None:
    from nodestro_runtime.mcp import stdio

    assert stdio.LIFETIME == docker_status.MCP_LIFETIME


@pytest.mark.real_docker_status
def test_one_monitor_per_daemon_is_shared() -> None:
    make: Callable[[], Any] = FakeSandbox
    first = docker_status.monitor_for(make, environ={"DOCKER_HOST": "unix:///a.sock"})
    assert docker_status.monitor_for(make, environ={"DOCKER_HOST": "unix:///a.sock"}) is first
    assert docker_status.monitor_for(make, environ={"DOCKER_HOST": "unix:///b.sock"}) is not first
    # One per installation on a daemon: its reaper removes only its own containers.
    other = docker_status.monitor_for(make, environ={"DOCKER_HOST": "unix:///a.sock"}, owner="nodestro-0123456789ab")
    assert other is not first and other.owner == "nodestro-0123456789ab"  # type: ignore[attr-defined]
    docker_status.reset_monitors()
    assert docker_status.monitor_for(make, environ={"DOCKER_HOST": "unix:///a.sock"}) is not first


def test_each_installation_has_its_own_container_owner(tmp_path: Path) -> None:
    from orkexa.dynamic.docker import ContainerLimits, run_args

    first = docker_status.installation_owner(tmp_path / "a", "id-1")
    assert re.fullmatch(r"nodestro-[0-9a-f]{12}", first)
    assert docker_status.installation_owner(tmp_path / "a", "id-1") == first  # stable across starts
    assert docker_status.installation_owner(tmp_path / "x" / ".." / "a", "id-1") == first  # the resolved path
    assert docker_status.installation_owner(tmp_path / "b", "id-1") != first
    # Two Nodestro Local Docker containers both keep their data in /data: their installation ids tell them apart.
    assert docker_status.installation_owner("/data", "id-1") != docker_status.installation_owner("/data", "id-2")
    limits = ContainerLimits(memory_mb=256, cpus=1.0, pids=64)
    run_args("docker", "nodestro-sandbox-x", "img", limits, owner=first)  # Orkexa accepts it
    mcp = docker_status.mcp_owner(first)
    assert mcp == f"nodestro-mcp-{first.removeprefix('nodestro-')}" and mcp != first
    assert docker_status.mcp_owner(docker_status.OWNER) == "nodestro-mcp"
    with pytest.raises(ValueError):
        docker_status.mcp_owner(mcp)
    with pytest.raises(ValueError):
        docker_status.mcp_owner("orkexa")


def test_a_missing_proof_asks_the_daemon_s_monitor_to_check(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[bool] = []

    class Recording(DockerMonitor):
        def status(self, *, refresh: bool = False, wait: float = 0.0) -> DockerStatus:
            asked.append(refresh)
            return docker_status.CHECKING

    key = docker_status.daemon_key("docker", {"DOCKER_HOST": "unix:///c.sock"})
    docker_status._MONITORS[(key, docker_status.OWNER)] = Recording(lambda: FakeSandbox())  # type: ignore[arg-type,return-value]
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


@pytest.mark.real_docker_status
def test_two_installations_on_one_daemon_never_reap_each_other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        DockerMonitor, "start_reaper", lambda self, remove_all_first: started.append((self.owner, remove_all_first))
    )
    monkeypatch.setattr(DockerMonitor, "status", lambda self, refresh=False, wait=0.0: docker_status.CHECKING)
    first = Workspace(tmp_path / "one", integrations=[])
    second = Workspace(tmp_path / "two", integrations=[])
    try:
        owners = (first.sandbox_owner, second.sandbox_owner)
        assert owners[0] != owners[1] and all(re.fullmatch(r"nodestro-[0-9a-f]{12}", item) for item in owners)
        # Each removes all of its own containers at the start, and only its own.
        assert started == [(owners[0], True), (owners[1], True)]
        assert first.docker_monitor is not second.docker_monitor
        # The self-test, the tool calls and the environments' containers all carry the installation's owner.
        assert first._runtime_sandbox().owner == owners[0]
        assert first.python.runtime_sandbox().owner == owners[0]
        assert first.python.images.sandbox("nodestro-env:x").owner == owners[0]
    finally:
        first.close()
        second.close()
    again = Workspace(tmp_path / "one", integrations=[])
    try:
        assert again.sandbox_owner == owners[0]  # the same installation keeps its owner
    finally:
        again.close()


def test_the_workspace_starts_the_reaper_only_where_it_owns_the_containers(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        assert workspace.docker_monitor.reaps == [True]  # type: ignore[attr-defined]
        assert workspace.docker_status().reason == "docker-missing"
        selection = sandbox_selection.select(SandboxPolicy("desktop"), "docker", workspace.docker_monitor.status())
        assert selection.blocked and selection.reason == "docker-missing"
    finally:
        workspace.close()


def test_a_server_tenant_shares_the_server_s_owner_and_reaps_only_expired_containers(tmp_path: Path) -> None:
    from bedrock_support import tenant_storage

    workspace = Workspace(tmp_path / "tenant", integrations=[], storage=tenant_storage(tmp_path / "db", "alpha"))
    try:
        # A server's API and worker processes share the daemon: never all containers at the start.
        assert workspace.sandbox_owner == docker_status.OWNER
        assert workspace.docker_monitor.reaps == [False]  # type: ignore[attr-defined]
        assert workspace.python.runtime_sandbox().owner == docker_status.OWNER
    finally:
        workspace.close()


def test_the_mcp_lifetime_covers_the_longest_connection() -> None:
    from nodestro_runtime.mcp.models import McpServer

    longest = McpServer.model_fields["timeout_seconds"].metadata
    maximum = next(m.le for m in longest if getattr(m, "le", None) is not None)
    # A connection is capped at twice the server's timeout; a minute covers container start and removal.
    assert 2 * maximum + 60 <= docker_status.MCP_LIFETIME
