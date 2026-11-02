"""Local MCP servers over stdio, only in an isolated container (WP-8, D-5, ADR-0006).

The container is replaced by ``fake_docker.py`` here (it records the ``docker run`` flags and runs the server's module
locally); ``test_a_real_container`` runs the server in Nodestro's real sandbox image (NODESTRO_DOCKER_TESTS=1).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any

from orkexa.dynamic import SandboxError, SandboxProbe
from pydantic import ValidationError
import pytest

from nodestro_runtime.container_images import MCP_OWNER, StreamedSandbox, sandbox_run_args
from nodestro_runtime.mcp.hub import McpHub
from nodestro_runtime.mcp.models import McpServerIn, StdioCommand
from nodestro_runtime.mcp.netguard import EgressPolicy
from nodestro_runtime.mcp.service import McpService
from nodestro_runtime.mcp.stdio import LIFETIME, MAX_LINE_BYTES, launch, proof_sandbox, prove, relay
from nodestro_runtime.mcp.vault import TokenVault
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.python_envs import needed_for_requirements
from nodestro_runtime.sandbox_gateway import NAME
from nodestro_runtime.storage import SQLiteStore

TESTS = Path(__file__).resolve().parent
IMAGE = "nodestro-env:" + "0" * 32
#: The id the (stand-in) self-test resolves IMAGE to; containers run it, never the tag.
IMAGE_ID = "sha256:" + "ab" * 32


class FakeProof:
    """Stands in for ``stdio.prove`` (preflight and in-container self-test): records the calls, returns the id."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.refuse: str | None = None

    def __call__(self, image: str, **options: Any) -> str:
        self.calls.append({"image": image, **options})
        if self.refuse is not None:
            raise NodestroError("sandbox.isolation_required", "refused", reason=self.refuse)
        return IMAGE_ID


class FakeImages:
    #: An installation's own owner (``docker_status.installation_owner``).
    owner = "nodestro-0123456789ab"

    def __init__(self, docker: str, log: Path) -> None:
        self.docker = docker
        self.log = log

    def _cli_environment(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", ""),
            "FAKE_DOCKER_LOG": str(self.log),
            "PYTHONPATH": os.pathsep.join([str(TESTS), *sys.path]),
        }


class FakePython:
    """The parts of PythonEnvironments the MCP service uses."""

    def __init__(self, images: FakeImages, *, containers: bool = True, ready: bool = True) -> None:
        self.images = images
        self._containers = containers
        self.ready = ready
        self.forgotten: list[str] = []
        self.planned: list[tuple[str, str, Any]] = []

    def containers(self) -> bool:
        return self._containers

    def image(self, scope: str) -> str | None:
        return IMAGE if self._containers and self.ready else None

    def forget(self, scope: str) -> None:
        self.forgotten.append(scope)

    def plan(self, kind: str, ref: str, needed: Any, *, label: str = "") -> Any:
        self.planned.append((kind, ref, needed))
        return {"kind": kind, "ref": ref}

    def status(self, kind: str, ref: str, needed: Any, *, label: str = "") -> Any:
        class Status:
            state = "ready" if self.ready else "approval_needed"

        return Status()


@pytest.fixture
def docker(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "docker.log"
    script = tmp_path / "docker"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{TESTS / "fake_docker.py"}" "$@"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), log


@pytest.fixture
def service(tmp_path: Path, docker: tuple[str, Path]) -> Iterator[McpService]:
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.migrate(before_migration=None)
    hub = McpHub(EgressPolicy())
    svc = McpService(store, hub, TokenVault(None, "local"))
    svc.python = FakePython(FakeImages(*docker))
    svc.stdio_proof = FakeProof()
    yield svc
    hub.close()
    store.close()


def stdio_input(**extra: Any) -> McpServerIn:
    command = {"requirements": ["nodestro-test-server==1.0"], "module": "mcp_stdio_server", **extra.pop("cmd", {})}
    return McpServerIn(name="Local Catalog", transport="stdio", stdio=StdioCommand(**command), **extra)


def code(call: Any) -> str:
    with pytest.raises(NodestroError) as caught:
        call()
    return caught.value.code


def docker_calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


# ---- configuration ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "requirement",
    [
        "pkg @ https://example.com/pkg.whl",
        "-e .",
        "--index-url https://evil.example/simple",
        "../local/path",
        "/abs/path",
        "pkg; os_name == 'nt'",
        "git+https://example.com/repo",
        "",
    ],
)
def test_only_plain_package_requirements_are_accepted(requirement: str) -> None:
    with pytest.raises(ValidationError):
        StdioCommand(requirements=[requirement], module="server")


@pytest.mark.parametrize("module", ["os;rm", "-c", "a/b", "1abc", "a..b", "a b"])
def test_the_module_must_be_a_module_path(module: str) -> None:
    with pytest.raises(ValidationError):
        StdioCommand(requirements=["pkg"], module=module)


def test_arguments_are_short_and_printable() -> None:
    with pytest.raises(ValidationError):
        StdioCommand(requirements=["pkg"], module="server", args=["a\nb"])
    with pytest.raises(ValidationError):
        StdioCommand(requirements=["pkg"], module="server", args=["x" * 201])
    assert StdioCommand(requirements=["mcp-server-time[cli]>=1,<3"], module="mcp_server_time", args=["--tz", "UTC"])


def test_requirements_become_an_approvable_need() -> None:
    needed = needed_for_requirements(["mcp-server-time[cli]>=1,<3", "tzdata"], "local-catalog")
    assert [item.line() for item in needed.requirements] == ["mcp-server-time[cli]<3,>=1", "tzdata"]
    assert needed.requirements[0].required_by == ["local-catalog"]
    assert not needed.blocking
    assert needed_for_requirements(["pkg @ https://x.example/p.whl"], "x").blocking


def test_a_stdio_server_needs_the_container_sandbox(service: McpService) -> None:
    service.python.__init__(service.python.images, containers=False)
    assert code(lambda: service.add(stdio_input())) == "sandbox.isolation_required"
    service.python = None
    assert code(lambda: service.add(stdio_input())) == "sandbox.isolation_required"


def test_a_stdio_server_takes_no_credentials_and_no_address(service: McpService) -> None:
    assert code(lambda: service.add(stdio_input(auth="bearer", token="secret"))) == "mcp.stdio_invalid"
    assert code(lambda: service.add(stdio_input(auth="oauth"))) == "mcp.stdio_invalid"
    assert code(lambda: service.add(stdio_input(allow_loopback=True))) == "mcp.stdio_invalid"
    assert code(lambda: service.add(McpServerIn(name="x", transport="stdio"))) == "mcp.stdio_invalid"
    command = StdioCommand(requirements=["pkg"], module="server")
    assert code(lambda: service.add(McpServerIn(name="x", url="https://a.example/mcp", stdio=command))) == (
        "mcp.stdio_invalid"
    )
    server = service.add(stdio_input(url="https://ignored.example/mcp"))
    assert server.url == "stdio:mcp_stdio_server"
    assert service.view(server).environment_state == "ready"


def test_packages_are_planned_through_the_python_environments(service: McpService) -> None:
    server = service.add(stdio_input())
    assert service.plan_environment(server.id) == {"kind": "mcp", "ref": server.id}
    _kind, _ref, needed = service.python.planned[-1]
    assert [item.line() for item in needed.requirements] == ["nodestro-test-server==1.0"]


def test_without_approved_packages_nothing_starts(service: McpService, docker: tuple[str, Path]) -> None:
    service.python.ready = False
    server = service.add(stdio_input())
    assert service.view(server).environment_state == "approval_needed"
    assert code(lambda: service.discover(server.id)) == "mcp.environment_not_ready"
    assert docker_calls(docker[1]) == []


# ---- the container command --------------------------------------------------------------------------------


def test_the_container_has_the_sandbox_shape() -> None:
    started = launch(image=IMAGE_ID, module="pkg.server", args=["--tz", "UTC"], memory_mb=256, docker="docker")
    run = started.args[started.args.index("--") + 1 :]
    image = run.index(IMAGE_ID)
    assert IMAGE not in run
    expected = sandbox_run_args(
        docker="docker",
        container_name=started.container,
        image=IMAGE_ID,
        memory_mb=256,
        cpus=1.0,
        pids_limit=64,
        owner=MCP_OWNER,
        lifetime_s=LIFETIME,
    )

    def stable(args: list[str]) -> list[str]:  # the deadline label is the time of the call
        return [item for item in args if not item.startswith("org.orkexa.deadline=")]

    assert stable(run[: image + 1]) == stable(expected)
    assert f"org.orkexa.sandbox={MCP_OWNER}" in run  # never removed by the tool containers' reaper
    assert run[image + 1 :] == ["python", "-I", "-B", "-m", "pkg.server", "--tz", "UTC"]
    flags = " ".join(run)
    for flag in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--user 65534:65534",
        "--pull never",
        "--memory 256m",
        "--memory-swap 256m",
    ):
        assert flag in flags
    assert "-v" not in run and "--volume" not in run and "--mount" not in run and "--privileged" not in run
    # The sandbox gateway accepts only its own container names.
    assert NAME.fullmatch(started.container)
    # The tool sandbox uses the same shape.
    sandbox = StreamedSandbox(image=IMAGE, docker="docker", memory_mb=256)
    assert (
        stable(sandbox.command(Path("/tmp"), "nodestro-sandbox-x")[:-5][1:])
        == stable(
            sandbox_run_args(
                "docker", "nodestro-sandbox-x", IMAGE, memory_mb=256, cpus=sandbox.cpus, pids_limit=sandbox.pids_limit
            )
        )[1:]
    )


def test_the_relay_ends_an_overlong_line_and_removes_the_container(tmp_path: Path, docker: tuple[str, Path]) -> None:
    script, log = docker
    os.environ["FAKE_DOCKER_LOG"] = str(log)
    try:
        out = io.BytesIO()
        flood = [sys.executable, "-c", "import sys\nwhile True: sys.stdout.write('x' * 65536); sys.stdout.flush()"]
        result = relay(
            flood,
            max_line=1_000_000,
            container="nodestro-sandbox-mcp-abc",
            docker=script,
            stdin=io.BytesIO(b""),
            stdout=out,
        )
        assert result == 3
        assert len(out.getvalue()) <= 1_000_000 + 65536
        assert ["rm", "-f", "nodestro-sandbox-mcp-abc"] in docker_calls(log)
        out = io.BytesIO()
        echo = [sys.executable, "-c", "import sys\nfor line in sys.stdin: sys.stdout.write(line)"]
        assert relay(echo, max_line=100, container="", docker=script, stdin=io.BytesIO(b"a\nb\n"), stdout=out) == 0
        assert out.getvalue() == b"a\nb\n"
    finally:
        os.environ.pop("FAKE_DOCKER_LOG", None)
    assert MAX_LINE_BYTES >= 1024 * 1024


# ---- end to end through the hub ---------------------------------------------------------------------------


def test_discovery_approval_and_calls_run_in_the_container(service: McpService, docker: tuple[str, Path]) -> None:
    server = service.add(stdio_input(cmd={"args": ["--name", "x"]}), actor="admin")
    stored, discovery = asyncio.run(service.adiscover(server.id, actor="admin"))
    assert stored.server_name == "nodestro-stdio"
    assert discovery.protocol_version
    assert {tool.name for tool in service.tools(server.id)} >= {"search", "delete_page"}
    assert service.usable() == []
    service.decide(
        server.id,
        "search",
        approve=True,
        expected_hash=service.tool(server.id, "search").hash,
        access="read",
        actor="admin",
    )
    _server, _tool, result = service.call("mcp__local-catalog__search", {"query": "q"})
    assert result.text == "results for q"
    calls = docker_calls(docker[1])
    runs = [call for call in calls if call[0] == "run"]
    assert len(runs) == 2  # one container per connection
    for call in runs:
        assert call[call.index("--network") + 1] == "none"
        assert call[-3:] == ["mcp_stdio_server", "--name", "x"]
        # The installation's MCP owner: never the tool containers' owner, so their reaper leaves it alone.
        assert "org.orkexa.sandbox=nodestro-mcp-0123456789ab" in call
        name = call[call.index("--name") + 1]
        assert ["rm", "-f", name] in calls  # removed after the connection
    # Changing a write tool stays blocked without the per-call approval gate (as for HTTP servers).
    service.decide(
        server.id,
        "delete_page",
        approve=True,
        expected_hash=service.tool(server.id, "delete_page").hash,
        access="write",
        actor="admin",
    )
    assert code(lambda: service.check("mcp__local-catalog__delete_page")) == "mcp.write_not_supported"


def test_a_server_flooding_stdout_fails_with_a_stable_code(service: McpService, docker: tuple[str, Path]) -> None:
    server = service.add(stdio_input(cmd={"args": ["--flood"]}, timeout_seconds=20))
    assert code(lambda: service.discover(server.id)) in ("mcp.stdio_failed", "mcp.protocol_error")
    assert service.server(server.id).last_error_code


def test_a_changed_command_needs_new_approvals(service: McpService) -> None:
    server = service.add(stdio_input())
    service.discover(server.id)
    service.decide(
        server.id,
        "search",
        approve=True,
        expected_hash=service.tool(server.id, "search").hash,
        access="read",
        actor="admin",
    )
    service.update(server.id, stdio_input(cmd={"args": ["--other"]}))
    assert service.tool(server.id, "search").status == "changed"
    service.remove(server.id)
    assert service.python.forgotten == [f"mcp:{server.id}"]


# ---- the proof before every start (DS-4) ----------------------------------------------------------------


def test_every_start_is_proven_and_runs_the_image_by_its_id(service: McpService, docker: tuple[str, Path]) -> None:
    server = service.add(stdio_input(cmd={"memory_mb": 384}))
    service.discover(server.id)
    service.discover(server.id)
    runs = [call for call in docker_calls(docker[1]) if call[0] == "run"]
    proofs = service.stdio_proof.calls  # type: ignore[attr-defined]
    assert len(runs) == len(proofs) == 2  # one proof per container start
    for proof in proofs:
        # The approved image's tag is checked in exactly the container's shape and owner.
        assert proof["image"] == IMAGE and proof["memory_mb"] == 384
        assert proof["owner"] == "nodestro-mcp-0123456789ab" and proof["docker"] == docker[0]
    for call in runs:
        assert IMAGE_ID in call and IMAGE not in call  # the checked id, never the tag


@pytest.mark.parametrize("reason", ["docker-selftest", "docker-limits", "docker-seccomp", "docker-unreachable"])
def test_a_daemon_that_fails_the_proof_starts_no_container(
    service: McpService, docker: tuple[str, Path], reason: str
) -> None:
    server = service.add(stdio_input())
    service.stdio_proof.refuse = reason  # type: ignore[attr-defined]
    with pytest.raises(NodestroError) as caught:
        service.discover(server.id)
    assert caught.value.code == "sandbox.isolation_required" and caught.value.params["reason"] == reason
    assert docker_calls(docker[1]) == []


def test_a_tag_is_never_launched() -> None:
    assert code(lambda: launch(image=IMAGE, module="pkg.server", args=[], memory_mb=256)) == (
        "sandbox.isolation_required"
    )


class ProofSandbox:
    """Stands in for the self-test sandbox of ``stdio.prove``."""

    def __init__(self, probe: SandboxProbe | Exception) -> None:
        self.probe = probe
        self.made: list[tuple[str, dict[str, Any]]] = []
        self.tested: list[str | None] = []

    def __call__(self, image: str, **options: Any) -> ProofSandbox:
        self.made.append((image, options))
        return self

    def self_test(self, *, image: str | None = None) -> SandboxProbe:
        self.tested.append(image)
        if isinstance(self.probe, Exception):
            raise self.probe
        return self.probe


@pytest.mark.parametrize(
    ("probe", "reason"),
    [
        (SandboxProbe("docker", False, "limits", "the daemon does not enforce memory limits"), "docker-limits"),
        (SandboxProbe("docker", False, "seccomp", "seccomp unconfined"), "docker-seccomp"),
        (SandboxProbe("docker", False, "unreachable", "no daemon"), "docker-unreachable"),
        (SandboxProbe("docker", False, "self-test", "the container is not on cgroup v2"), "docker-selftest"),
        (SandboxProbe("docker", False, "image", "not built"), "docker-image"),
        (SandboxError("the self-test container did not run"), "docker-selftest"),
        (SandboxProbe("docker", True), "docker-image"),  # passed, but without the id it checked
    ],
)
def test_prove_refuses_without_preflight_and_self_test(probe: SandboxProbe | Exception, reason: str) -> None:
    fake = ProofSandbox(probe)
    with pytest.raises(NodestroError) as caught:
        prove(IMAGE, memory_mb=256, docker="docker", docker_env={"PATH": "/usr/bin"}, sandbox=fake)
    assert caught.value.code == "sandbox.isolation_required" and caught.value.params["reason"] == reason
    assert fake.tested == [IMAGE]


def test_prove_returns_the_id_the_self_test_checked() -> None:
    fake = ProofSandbox(SandboxProbe("docker", True, facts=(("image_id", IMAGE_ID),)))
    assert (
        prove(IMAGE, memory_mb=300, docker="dk", docker_env={"PATH": "/p"}, owner="nodestro-mcp-x", sandbox=fake)
        == IMAGE_ID
    )
    assert fake.made == [
        (IMAGE, {"memory_mb": 300, "docker": "dk", "docker_env": {"PATH": "/p"}, "owner": "nodestro-mcp-x"})
    ]


def test_the_self_test_checks_the_shape_the_container_gets() -> None:
    from orkexa.dynamic.docker import run_args

    checker = proof_sandbox(IMAGE_ID, memory_mb=256, docker="docker", docker_env={"PATH": "/p"}, owner=MCP_OWNER)
    assert checker.owner == MCP_OWNER and checker.environ == {"PATH": "/p"}
    started = launch(image=IMAGE_ID, module="pkg.server", args=[], memory_mb=256, docker="docker")
    run = started.args[started.args.index("--") + 1 :]
    run = run[: run.index(IMAGE_ID) + 1]

    def stable(args: list[str]) -> list[str]:
        return [item for item in args if not item.startswith("org.orkexa.deadline=")]

    # The self-test's container shape (Orkexa keys its result by it) is the stdio container's, the deadline aside.
    shape = run_args("docker", started.container, IMAGE_ID, checker.limits(), owner=checker.owner)
    assert stable(shape) == stable(run)


def test_prove_runs_orkexa_s_preflight_and_self_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real gate: a daemon without enforced limits (preflight) or on cgroup v1 (self-test) is refused, and no
    MCP container is ever started; only the self-test container runs."""
    from dataclasses import replace

    from orkexa.dynamic.docker import DockerHost, ImageInfo
    import orkexa.dynamic.sandbox as orkexa_sandbox

    from nodestro_runtime import docker_status

    host = DockerHost(
        engine="docker",
        os_type="linux",
        arch="x86_64",
        rootless=False,
        cgroup_version="1",
        cgroup_driver="cgroupfs",
        memory_limit=True,
        pids_limit=True,
        cpu_cfs_quota=True,
        cpu_cfs_period=True,
        seccomp_profile="builtin",
        apparmor=True,
        selinux=False,
        daemon_id="daemon-mcp",
        server_version="27.4.0",
    )
    current = {"host": replace(host, memory_limit=False)}
    started: list[list[str]] = []

    def run_bounded(args: list[str], data: bytes, **_: Any) -> tuple[int, Any]:
        started.append(args)
        report = json.dumps({"version": 1, "cgroup": {"version": "1"}}).encode()

        class Output:
            stdout = report + b"\n"
            stderr = b""

        return 0, Output()

    monkeypatch.setattr(orkexa_sandbox, "inspect_host", lambda docker, env=None: current["host"])
    monkeypatch.setattr(
        orkexa_sandbox,
        "inspect_image",
        lambda docker, ref, env=None: ImageInfo(id=IMAGE_ID, repo_digests=(), env_names=()),
    )
    monkeypatch.setattr(orkexa_sandbox, "run_bounded", run_bounded)
    StreamedSandbox.clear_self_test_cache()
    try:
        with pytest.raises(NodestroError) as caught:
            prove(IMAGE, memory_mb=256, docker=sys.executable, docker_env={"PATH": os.environ.get("PATH", "")})
        assert caught.value.params["reason"] == "docker-limits" and started == []  # refused before any container
        current["host"] = host  # limits reported, but cgroup v1: the self-test shows they are not verifiable
        with pytest.raises(NodestroError) as caught:
            prove(IMAGE, memory_mb=256, docker=sys.executable, docker_env={"PATH": os.environ.get("PATH", "")})
        assert caught.value.params["reason"] == "docker-selftest"
        assert len(started) == 1 and any(item.startswith("orkexa-selftest-") for item in started[0])
        assert f"org.orkexa.sandbox={MCP_OWNER}" in started[0] and "256m" in started[0]
    finally:
        StreamedSandbox.clear_self_test_cache()
        docker_status.clear_proofs()


def test_without_a_launcher_the_hub_refuses(tmp_path: Path) -> None:
    hub = McpHub(EgressPolicy())
    try:
        from nodestro_runtime.mcp.models import McpServer

        server = McpServer(
            slug="x",
            name="x",
            url="stdio:server",
            transport="stdio",
            stdio=StdioCommand(requirements=["pkg"], module="server"),
        )
        assert code(lambda: hub.discover(server, None)) == "sandbox.isolation_required"
    finally:
        hub.close()


# ---- a real container through the sandbox gateway (NODESTRO_DOCKER_TESTS=1) --------------------------------------


@pytest.fixture
def real_gateway(tmp_path: Path) -> Iterator[dict[str, str]]:
    from test_sandbox_gateway import real_gateway as gateway_fixture

    yield from gateway_fixture.__wrapped__(tmp_path)  # type: ignore[attr-defined]


def test_a_real_third_party_server_runs_in_the_sandbox_container(tmp_path: Path, real_gateway: dict[str, str]) -> None:
    """``mcp-server-time`` from PyPI, hash-locked, in Nodestro's sandbox image, started through the gateway (which
    refuses every container that is not of the sandbox's shape: no network, read-only, nobody, limits)."""
    from nodestro_runtime.container_images import ContainerImages
    from nodestro_runtime.python_envs import UvRunner, current_python_version, find_uv

    uv = find_uv()
    assert uv is not None
    python = current_python_version()
    lock = UvRunner(tmp_path / "uv-cache", uv).compile(
        ["mcp-server-time==2025.9.25", "mcp>=1.10,<2", "pydantic>=2.6,<3"],
        python=python,
        previous="",
        log=lambda _l: None,
        builds=False,
    )
    images = ContainerImages(environ=real_gateway)
    tag = images.build(lock, python)
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.migrate(before_migration=None)
    hub = McpHub(EgressPolicy())
    try:
        svc = McpService(store, hub, TokenVault(None, "local"))
        fake = FakePython(images)  # type: ignore[arg-type]
        fake.image = lambda _scope: tag  # type: ignore[method-assign,assignment]
        svc.python = fake
        server = svc.add(
            McpServerIn(
                name="Time",
                transport="stdio",
                stdio=StdioCommand(
                    requirements=["mcp-server-time==2025.9.25", "mcp>=1.10,<2"],
                    module="mcp_server_time",
                    args=["--local-timezone", "UTC"],
                ),
                timeout_seconds=60,
            )
        )
        stored, discovery = svc.discover(server.id)
        assert stored.server_name and "time" in stored.server_name.lower()
        names = {tool.name for tool in discovery.tools}
        assert "get_current_time" in names
        svc.decide(
            server.id,
            "get_current_time",
            approve=True,
            expected_hash=svc.tool(server.id, "get_current_time").hash,
            access="read",
            actor="admin",
        )
        _server, _tool, result = svc.call("mcp__time__get_current_time", {"timezone": "UTC"})
        assert "UTC" in result.text and not result.is_error
        # Nothing is left running.
        listed = subprocess.run(
            ["docker", "ps", "-q", "--filter", "name=nodestro-sandbox-mcp-"], capture_output=True, text=True, timeout=30
        )
        assert listed.stdout.strip() == ""
    finally:
        hub.close()
        store.close()
        images.remove(tag)
