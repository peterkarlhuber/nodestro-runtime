"""Docker-first (ADR-0006, security review DS-2 and DS-3).

* DS-2: untrusted (AI-generated) code's libraries never become a host virtual environment: no ``uv pip sync`` and no
  import check (``verify_imports``, whose interpreter runs the packages' ``.pth`` files) on the host. In ``auto`` mode
  they are a container image, built and checked in containers, refused with Docker's reason while Docker has not
  passed its checks; in ``subprocess`` and ``linux`` mode they are refused. A person's own code keeps host
  environments.
* DS-3: ``auto`` runs untrusted code in Docker only (never automatically in the built-in Linux sandbox), ``subprocess``
  never runs it, and the built-in Linux sandbox runs it only after ``linux`` was chosen, with Nodestro's own
  interpreter only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
import sys
from typing import Any

from orkexa.dynamic import SubprocessSandbox
import pytest

from nodestro_runtime import docker_status, python_envs, sandbox_selection
from nodestro_runtime.builtin_sandbox import LinuxSandbox, SandboxStatus
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.python_envs import PythonEnvironments
from nodestro_runtime.python_tools import EnvironmentGate, check_with_dependencies
from nodestro_runtime.sandbox_policy import SandboxPolicy
from nodestro_runtime.schemas import SettingsIn
from nodestro_runtime.testing import ContainerDouble, FakeUv, StaticDockerMonitor
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE, ToolLibrary
from nodestro_runtime.workspace import Workspace

DESKTOP = SandboxPolicy("desktop")
INDEX = {"tablib": [("1.0", [], "tablib\ndef show(text):\n    return 'T:' + text\n")]}
CODE = '''# /// script
# dependencies = ["tablib"]
# ///
import tablib


def show(text: str) -> str:
    """Show a text.

    Args:
        text: The text.

    Returns:
        The shown text.
    """
    return tablib.show(text)
'''
TESTS = "from tool import show\n\n\ndef test_show():\n    assert show('a') == 'T:a'\n"
_UP = SandboxStatus(True, "", "", "ubuntu 24.04", 4)


class _Images:
    """Stands in for :class:`~nodestro_runtime.container_images.ContainerImages`: records builds and import checks."""

    def __init__(self) -> None:
        self.built: dict[str, str] = {}
        self.builds: list[str] = []
        self.checked: list[tuple[str, list[str]]] = []
        self.removed: list[str] = []
        self.docker = "/usr/bin/docker"
        self.container = ContainerDouble()

    def tag(self, lock: str, python_version: str) -> str:
        return f"nodestro-env:{python_envs._sha(lock + python_version)[:12]}"

    def build(self, lock: str, python_version: str, *, log: Callable[[str], None] | None = None) -> str:
        tag = self.tag(lock, python_version)
        self.builds.append(tag)
        self.built[tag] = python_envs._sha(lock)
        return tag

    def missing_modules(self, tag: str, modules: list[str]) -> list[str]:
        self.checked.append((tag, sorted(modules)))
        return []

    def lock_of(self, tag: str) -> str | None:
        return self.built.get(tag)

    def sandbox(self, tag: str, **_options: Any) -> ContainerDouble:
        return self.container

    def remove(self, tag: str) -> None:
        self.removed.append(tag)
        self.built.pop(tag, None)


@pytest.fixture
def no_host_import_check(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Records every host import check (the interpreter of a host environment started on this computer)."""
    started: list[Path] = []
    original = python_envs.verify_imports

    def record(interpreter: Path, modules: Any) -> list[str]:
        started.append(interpreter)
        return original(interpreter, modules)

    monkeypatch.setattr(python_envs, "verify_imports", record)
    return started


def _monitor(monkeypatch: pytest.MonkeyPatch, approved: bool) -> StaticDockerMonitor:
    status = (
        docker_status.approved_status()
        if approved
        else docker_status.unavailable_status("docker-unreachable", "Cannot connect to the Docker daemon")
    )
    monitor = StaticDockerMonitor(status)
    monkeypatch.setattr(docker_status, "monitor_for", lambda *a, **k: monitor)
    return monitor


Make = Callable[..., tuple[Workspace, FakeUv, _Images]]


@pytest.fixture
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Make]:
    opened: list[Workspace] = []

    def build(*, docker: bool, mode: str = "auto") -> tuple[Workspace, FakeUv, _Images]:
        _monitor(monkeypatch, docker)
        uv = FakeUv(INDEX)
        workspace = Workspace(tmp_path / f"data{len(opened)}", python_runner=uv, integrations=[])
        opened.append(workspace)
        if mode != "auto":
            workspace.update_settings(SettingsIn(sandbox=mode))  # type: ignore[arg-type]
        images = _Images()
        workspace.python.images = images  # type: ignore[assignment]
        return workspace, uv, images

    yield build
    for workspace in opened:
        workspace.close()


class _WithTablib(ContainerDouble):
    """A container double whose "image" has ``tablib`` (validates a copy of the code with the module inlined)."""

    def validate(self, code: str, tests: str, function_name: str) -> Any:
        inlined = code.replace("import tablib\n", "class tablib:\n    show = staticmethod(lambda text: 'T:' + text)\n")
        return super().validate(inlined, tests, function_name)


def _generated(workspace: Workspace) -> None:
    """Store ``show`` as an AI-generated (untrusted) tool, checked in a container double."""
    tools = workspace.tools
    result, info = tools.save_custom(
        CODE,
        TESTS,
        "show",
        requirement="Show texts",
        sandbox=_WithTablib(),
        extra_imports=["tablib"],
    )
    assert result.ok and info is not None and info.trust == "untrusted", result


# ---- DS-2: where an environment is built ----------------------------------------------------------------------


def test_auto_builds_untrusted_libraries_as_a_container_image_never_on_the_host(
    make: Make, no_host_import_check: list[Path]
) -> None:
    workspace, uv, images = make(docker=True)
    python = workspace.python
    needed = python.needed_for_sources([("show", CODE, TESTS)])
    plan = python.plan("tool", "show", needed, trust="untrusted")
    assert plan.error is None and plan.lock
    env = python.approve(plan.id, wait=True)
    assert env.install.status == "installed", env.install
    # The image was built and its imports were checked in it; no host venv was created, synced or started.
    assert images.builds and images.checked == [(images.builds[0], ["pydantic", "tablib"])]
    assert uv.syncs == 0 and no_host_import_check == []
    assert not python.env_dir(plan.scope).exists()
    assert python.sandbox(plan.scope, trust="untrusted") is images.container
    assert python.isolation() == "container"


def test_a_tool_job_plan_stays_untrusted_whatever_the_code_looks_like(
    make: Make, no_host_import_check: list[Path]
) -> None:
    workspace, uv, images = make(docker=True)
    outcome = EnvironmentGate(workspace.python).prepare("show", CODE, TESTS)
    assert outcome.plan is not None, outcome
    # The code is unknown to the library (no draft, no stored tool), yet the job's plan is installed as an image.
    EnvironmentGate(workspace.python).install(str(outcome.plan["id"]), lambda _line: None)
    assert images.builds and uv.syncs == 0 and no_host_import_check == []


def test_auto_without_docker_refuses_untrusted_libraries_with_dockers_reason(
    make: Make, no_host_import_check: list[Path]
) -> None:
    workspace, uv, images = make(docker=False)
    python = workspace.python
    needed = python.needed_for_sources([("show", CODE, TESTS)])
    status = python.status("tool", "show", needed, trust="untrusted")
    assert status.state == "unsupported" and status.problem is not None
    assert status.problem.code == "sandbox.isolation_required"
    assert status.problem.params["reason"] == "docker-unreachable"
    plan = python.plan("tool", "show", needed, trust="untrusted")
    assert plan.error is not None and plan.error.code == "sandbox.isolation_required" and not plan.lock
    assert python.isolation() == "none"
    # A tool job is told to use the standard library: nothing is resolved, built or started.
    outcome = EnvironmentGate(python).prepare("show", CODE, TESTS)
    assert outcome.plan is None and outcome.feedback and "standard library" in outcome.feedback
    assert uv.syncs == 0 and images.builds == [] and no_host_import_check == []


def test_an_approved_image_is_never_rebuilt_on_the_host_when_docker_goes_away(
    make: Make, monkeypatch: pytest.MonkeyPatch, no_host_import_check: list[Path]
) -> None:
    workspace, uv, images = make(docker=True)
    python = workspace.python
    _generated(workspace)
    needed = python.needed_for_sources(python.tool_sources(["show"]))
    plan = python.plan("tool", "show", needed)  # no trust given: the stored tool is untrusted
    python.approve(plan.id, wait=True)
    assert images.builds and python.sandbox(plan.scope) is images.container
    images.built.clear()  # the image is gone
    workspace.docker_monitor.current = docker_status.unavailable_status("docker-selftest", "no seccomp")  # type: ignore[attr-defined]
    assert python.sandbox(plan.scope) is None
    env = python.install(plan.scope, ["tablib"])
    assert env.install.status == "failed" and env.install.error is not None
    assert env.install.error.code == "sandbox.isolation_required"
    assert env.install.error.params["reason"] == "docker-selftest"
    assert len(images.builds) == 1 and uv.syncs == 0 and no_host_import_check == []
    assert not python.env_dir(plan.scope).exists()


@pytest.mark.parametrize("mode", ["subprocess", "linux"])
def test_subprocess_and_linux_modes_refuse_untrusted_libraries(
    make: Make, monkeypatch: pytest.MonkeyPatch, no_host_import_check: list[Path], mode: str
) -> None:
    monkeypatch.setattr(LinuxSandbox, "status", staticmethod(lambda refresh=False: _UP))
    workspace, uv, images = make(docker=True, mode=mode)
    python = workspace.python
    assert python.isolation() == "none"
    needed = python.needed_for_sources([("show", CODE, TESTS)])
    status = python.status("tool", "show", needed, trust="untrusted")
    assert status.state == "unsupported" and status.problem is not None
    assert status.problem.code == "sandbox.libraries_need_isolation" and status.problem.params["mode"] == mode
    # Not even an older, already approved definition is installed for it.
    workspace.update_settings(SettingsIn(sandbox="auto"))
    plan = python.plan("tool", "show", needed, trust="untrusted")
    assert plan.lock
    workspace.update_settings(SettingsIn(sandbox=mode))  # type: ignore[arg-type]
    env = python.approve(plan.id, wait=True)
    assert env.install.status == "failed" and env.install.error is not None
    assert env.install.error.code == "sandbox.libraries_need_isolation"
    assert uv.syncs == 0 and images.builds == [] and no_host_import_check == []
    assert python.sandbox(plan.scope, trust="untrusted") is None
    # The tool editor reports it without approving, installing or running anything.
    result = check_with_dependencies(workspace.tools, python, CODE, TESTS, "show", trust="untrusted")
    assert not result.ok and result.errors[-1].code == "sandbox.libraries_need_isolation"
    assert uv.syncs == 0 and no_host_import_check == []


@pytest.mark.parametrize("mode", ["auto", "subprocess", "linux"])
def test_a_persons_own_code_keeps_a_host_environment(make: Make, no_host_import_check: list[Path], mode: str) -> None:
    workspace, uv, images = make(docker=False, mode=mode)
    python = workspace.python
    result = check_with_dependencies(workspace.tools, python, CODE, TESTS, "show")
    assert result.dependencies_pending  # approval first
    needed = python.needed_for_sources([("show", CODE, TESTS)])
    plan = python.plan("tool", "show", needed)  # the API's plan: the code decides (new code: user-authored)
    env = python.approve(plan.id, wait=True)
    assert env.install.status == "installed", env.install
    assert uv.syncs == 1 and len(no_host_import_check) == 1 and images.builds == []
    sandbox = python.sandbox(plan.scope, trust="user-authored")
    assert isinstance(sandbox, SubprocessSandbox) and sandbox.python != sys.executable
    assert check_with_dependencies(workspace.tools, python, CODE, TESTS, "show").ok


def test_code_copied_from_a_generated_tool_counts_as_untrusted(make: Make, no_host_import_check: list[Path]) -> None:
    workspace, uv, _images = make(docker=False)
    _generated(workspace)
    python = workspace.python
    # The same code under a new name, sent from the editor or the API: still AI-generated.
    needed = python.needed_for_sources([("copy", CODE, TESTS)])
    assert python.scope_trust(needed.requirements, needed.digests) == "untrusted"
    status = python.status("tool", "copy", needed)
    assert status.state == "unsupported" and status.problem is not None
    assert status.problem.code == "sandbox.isolation_required"
    assert uv.syncs == 0 and no_host_import_check == []


def test_a_host_environment_is_not_handed_out_once_untrusted_code_needs_it(
    make: Make, no_host_import_check: list[Path]
) -> None:
    workspace, uv, images = make(docker=True)
    python = workspace.python
    saved, info = workspace.tools.save_custom(
        CODE, TESTS, "show", sandbox=_WithTablib(), extra_imports=["tablib"], trust="user-authored"
    )
    assert info is not None and info.trust == "user-authored", saved
    needed = python.needed_for_sources(python.tool_sources(["show"]))
    plan = python.plan("workflow", "wf_1", needed)  # the workflow's only library user is a person's tool
    python.approve(plan.id, wait=True)
    assert uv.syncs == 1 and isinstance(python.sandbox(plan.scope), SubprocessSandbox)
    _generated(workspace)  # a new, AI-generated version of "show"
    assert python.sandbox(plan.scope) is None  # the host venv is never used for it
    assert python.workflow_sandbox("wf_1") is None
    status = python.status("workflow", "wf_1", python.needed_for_sources(python.tool_sources(["show"])))
    assert status.state == "install_needed"  # an image is built next time, never the host environment
    python.install(plan.scope, ["tablib"])
    assert images.builds and uv.syncs == 1 and len(no_host_import_check) == 1
    assert python.workflow_sandbox("wf_1") is images.container


def test_mcp_scopes_are_never_host_environments(make: Make) -> None:
    workspace, _uv, _images = make(docker=False, mode="subprocess")
    python = workspace.python
    assert python.backend("mcp", "user-authored") == "refused"
    assert python.backend("tool", "user-authored") == "venv"
    workspace.update_settings(SettingsIn(sandbox="auto"))
    assert python.backend("mcp", "user-authored") == "image"
    assert python.backend("workflow", "untrusted") == "image"
    workspace.update_settings(SettingsIn(sandbox="docker"))
    assert python.backend("tool", "user-authored") == "image"


def test_environments_without_a_docker_status_count_docker_as_unproven(tmp_path: Path) -> None:
    from nodestro_runtime.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "db.sqlite")
    try:
        python = PythonEnvironments(store, tmp_path, ToolLibrary(tmp_path / "tools"), sandbox_kind=lambda: "auto")
        assert python.isolation() == "none"
        refused = python._refusal("image")
        assert refused is not None and refused.params["reason"] == "docker-unreachable"
    finally:
        store.close()


# ---- DS-3: which sandbox runs untrusted code ------------------------------------------------------------------


def _builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LinuxSandbox, "status", staticmethod(lambda refresh=False: _UP))
    monkeypatch.setattr(sandbox_selection, "runtime_platform", lambda: ("linux", "x86_64"))


def test_auto_prefers_docker_over_the_builtin_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    _builtin(monkeypatch)
    selection = sandbox_selection.select(DESKTOP, "auto", docker_status.approved_status())
    assert selection.untrusted == "docker" and selection.user_authored == "subprocess" and not selection.blocked


def test_auto_without_docker_refuses_even_with_the_builtin_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    _builtin(monkeypatch)
    for status in (
        docker_status.unavailable_status("docker-unreachable", "no daemon"),
        docker_status.DockerStatus("checking", "docker-checking", "running"),
    ):
        selection = sandbox_selection.select(DESKTOP, "auto", status)
        assert selection.blocked and selection.untrusted == "" and selection.reason == status.reason
        assert selection.user_authored == "subprocess"


def test_subprocess_mode_never_runs_untrusted_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _builtin(monkeypatch)
    selection = sandbox_selection.select(DESKTOP, "subprocess", docker_status.approved_status())
    assert selection.blocked and selection.untrusted == "" and selection.reason == "no-isolation"
    assert "only code a person wrote" in selection.detail


def test_the_builtin_sandbox_runs_untrusted_code_only_when_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    _builtin(monkeypatch)
    chosen = sandbox_selection.select(DESKTOP, "linux", docker_status.approved_status())
    assert chosen.untrusted == "linux-builtin" and chosen.mode == "linux" and not chosen.blocked
    assert "third-party libraries" in chosen.detail
    monkeypatch.setattr(
        LinuxSandbox, "status", staticmethod(lambda refresh=False: SandboxStatus(False, "landlock", "off", "x", 0))
    )
    missing = sandbox_selection.select(DESKTOP, "linux", docker_status.approved_status())
    assert missing.blocked and missing.reason == "no-isolation" and missing.untrusted == ""  # never Docker instead


def _library(tmp_path: Path, mode: str | None, container: ContainerDouble | None = None) -> ToolLibrary:
    tools = ToolLibrary(tmp_path / "tools", policy=DESKTOP)
    tools.sandbox_mode = mode
    tools.container_fallback = (lambda: container) if mode == "auto" else None
    return tools


def test_the_library_routes_untrusted_code_by_the_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _builtin(monkeypatch)
    host = SubprocessSandbox(timeout=15.0, acknowledge_unsafe=True)
    container = ContainerDouble()
    # auto: Docker, never the built-in sandbox although it is available.
    assert _library(tmp_path, "auto", container)._require("untrusted", host, "x") is container
    # linux: the built-in sandbox.
    assert isinstance(_library(tmp_path, "linux")._require("untrusted", host, "x"), LinuxSandbox)
    # subprocess and a library without a mode: refused.
    for mode in ("subprocess", None):
        with pytest.raises(NodestroError) as caught:
            _library(tmp_path, mode)._require("untrusted", host, "x")
        assert caught.value.code == "sandbox.isolation_required"
    with pytest.raises(NodestroError) as caught:
        _library(tmp_path, "subprocess")._require("untrusted", host, "x")
    assert caught.value.params["reason"] == "subprocess-mode"
    # A person's own code keeps the host subprocess in every mode.
    for mode in ("auto", "subprocess", "linux"):
        assert _library(tmp_path, mode, container)._require("user-authored", host, "x") is host


def test_auto_refuses_with_dockers_reason_when_docker_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builtin(monkeypatch)
    tools = _library(tmp_path, "auto", None)  # the fallback has no container: Docker not approved
    tools.docker_status = lambda: docker_status.unavailable_status("docker-limits", "pids limit not enforced")
    with pytest.raises(NodestroError) as caught:
        tools._require("untrusted", SubprocessSandbox(timeout=15.0, acknowledge_unsafe=True), "x")
    assert caught.value.params["reason"] == "docker-limits"
    assert "pids limit not enforced" in str(caught.value) and "Docker" in str(caught.value)


def test_the_builtin_sandbox_never_starts_an_environments_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interpreter of a host environment runs its ``.pth`` files before the sandbox confines it."""
    _builtin(monkeypatch)
    environment = SubprocessSandbox(
        python=str(tmp_path / "env" / "bin" / "python"), timeout=60.0, memory_mb=2048, acknowledge_unsafe=True
    )
    routed = _library(tmp_path, "linux")._require("untrusted", environment, "x")
    assert isinstance(routed, LinuxSandbox) and routed.python == sys.executable and routed.memory_mb == 2048
    # auto: the container instead of the environment's interpreter.
    container = ContainerDouble()
    assert _library(tmp_path, "auto", container)._require("untrusted", environment, "x") is container


def test_the_workspace_follows_the_stored_mode(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    _builtin(monkeypatch)
    workspace, _uv, _images = make(docker=True)
    assert workspace.tools.sandbox_mode == "auto" and workspace.tools.container_fallback is not None
    _untrusted = workspace.tools.save_custom(
        CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, "celsius_to_fahrenheit", requirement="x", sandbox=ContainerDouble()
    )
    assert _untrusted[1] is not None
    try:  # Docker (the runtime image, refused until its own self-test passed), never the built-in sandbox
        routed = workspace.tools._require("untrusted", workspace.tools.registry.sandbox, "celsius_to_fahrenheit")
    except NodestroError as exc:
        assert exc.params["reason"] == "docker-selftest"
    else:
        assert not isinstance(routed, LinuxSandbox)
    settings = workspace.update_settings(SettingsIn(sandbox="linux"))
    assert settings.sandbox == "linux" and settings.sandbox_selection.untrusted == "linux-builtin"
    assert workspace.tools.sandbox_mode == "linux" and workspace.tools.container_fallback is None
    routed = workspace.tools._require("untrusted", workspace.tools.registry.sandbox, "celsius_to_fahrenheit")
    assert isinstance(routed, LinuxSandbox)
    settings = workspace.update_settings(SettingsIn(sandbox="subprocess"))
    assert settings.sandbox_selection.blocked and settings.sandbox_selection.untrusted == ""
    with pytest.raises(NodestroError) as caught:
        workspace.tools.run("celsius_to_fahrenheit", {"celsius": 100})
    assert caught.value.params["reason"] == "subprocess-mode"
