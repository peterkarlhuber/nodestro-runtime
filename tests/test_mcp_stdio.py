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

from pydantic import ValidationError
import pytest

pytest.importorskip("mcp")

from nodestro_runtime.container_images import StreamedSandbox, sandbox_run_args
from nodestro_runtime.mcp.hub import McpHub
from nodestro_runtime.mcp.models import McpServerIn, StdioCommand
from nodestro_runtime.mcp.netguard import EgressPolicy
from nodestro_runtime.mcp.service import McpService
from nodestro_runtime.mcp.stdio import MAX_LINE_BYTES, launch, relay
from nodestro_runtime.mcp.vault import TokenVault
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.python_envs import needed_for_requirements
from nodestro_runtime.sandbox_gateway import NAME
from nodestro_runtime.storage import SQLiteStore

TESTS = Path(__file__).resolve().parent
IMAGE = "nodestro-env:" + "0" * 32


class FakeImages:
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
    started = launch(image=IMAGE, module="pkg.server", args=["--tz", "UTC"], memory_mb=256, docker="docker")
    run = started.args[started.args.index("--") + 1 :]
    image = run.index(IMAGE)
    assert run[: image + 1] == sandbox_run_args(
        docker="docker", container_name=started.container, image=IMAGE, memory_mb=256, cpus=1.0, pids_limit=64
    )
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
        sandbox.command(Path("/tmp"), "nodestro-sandbox-x")[:-5][1:]
        == sandbox_run_args(
            "docker", "nodestro-sandbox-x", IMAGE, memory_mb=256, cpus=sandbox.cpus, pids_limit=sandbox.pids_limit
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
    service.decide(server.id, "search", approve=True, access="read", actor="admin")
    _server, _tool, result = service.call("mcp__local-catalog__search", {"query": "q"})
    assert result.text == "results for q"
    calls = docker_calls(docker[1])
    runs = [call for call in calls if call[0] == "run"]
    assert len(runs) == 2  # one container per connection
    for call in runs:
        assert call[call.index("--network") + 1] == "none"
        assert call[-3:] == ["mcp_stdio_server", "--name", "x"]
        name = call[call.index("--name") + 1]
        assert ["rm", "-f", name] in calls  # removed after the connection
    # Changing a write tool stays blocked without the per-call approval gate (as for HTTP servers).
    service.decide(server.id, "delete_page", approve=True, access="write", actor="admin")
    assert code(lambda: service.check("mcp__local-catalog__delete_page")) == "mcp.write_not_supported"


def test_a_server_flooding_stdout_fails_with_a_stable_code(service: McpService, docker: tuple[str, Path]) -> None:
    server = service.add(stdio_input(cmd={"args": ["--flood"]}, timeout_seconds=20))
    assert code(lambda: service.discover(server.id)) in ("mcp.stdio_failed", "mcp.protocol_error")
    assert service.server(server.id).last_error_code


def test_a_changed_command_needs_new_approvals(service: McpService) -> None:
    server = service.add(stdio_input())
    service.discover(server.id)
    service.decide(server.id, "search", approve=True, access="read", actor="admin")
    service.update(server.id, stdio_input(cmd={"args": ["--other"]}))
    assert service.tool(server.id, "search").status == "changed"
    service.remove(server.id)
    assert service.python.forgotten == [f"mcp:{server.id}"]


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
        svc.decide(server.id, "get_current_time", approve=True, access="read", actor="admin")
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
