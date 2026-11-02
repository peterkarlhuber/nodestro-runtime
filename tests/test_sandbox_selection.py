"""Automatic sandbox selection (``auto``), its report, and that it never falls back to an unsafe backend."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

from orkexa.dynamic import MacOSSandbox, SubprocessSandbox
import pytest

from nodestro_runtime import sandbox_cancel, sandbox_selection
from nodestro_runtime.builtin_sandbox import LinuxSandbox, SandboxStatus
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.profile import RuntimeProfile
from nodestro_runtime.sandbox_policy import SandboxPolicy, isolation_of
from nodestro_runtime.schemas import SettingsIn
from nodestro_runtime.testing import ContainerDouble
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE, ToolLibrary
from nodestro_runtime.workspace import Workspace

NAME = "celsius_to_fahrenheit"
DESKTOP = SandboxPolicy("desktop")
SERVER = SandboxPolicy("server")


def _builtin(monkeypatch: pytest.MonkeyPatch, available: bool) -> None:
    reason = "" if available else "landlock"
    status = SandboxStatus(available=available, reason=reason, detail="test", system="test", landlock_abi=4)
    monkeypatch.setattr(LinuxSandbox, "status", staticmethod(lambda refresh=False: status))


def _on(monkeypatch: pytest.MonkeyPatch, system: str) -> None:
    monkeypatch.setattr(sandbox_selection, "runtime_platform", lambda: (system, "arm64"))


# ---- selection --------------------------------------------------------------------------


def test_linux_with_the_builtin_sandbox_uses_it_in_every_desktop_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "linux")
    _builtin(monkeypatch, True)
    for mode in ("auto", "subprocess"):
        selection = sandbox_selection.select(DESKTOP, mode, docker_available=True)
        assert selection.untrusted == "linux-builtin" and selection.user_authored == "subprocess"
        assert not selection.blocked


def test_auto_falls_back_to_docker_and_otherwise_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "macos")
    with_docker = sandbox_selection.select(DESKTOP, "auto", docker_available=True)
    assert with_docker.untrusted == "docker" and with_docker.user_authored == "subprocess"
    without = sandbox_selection.select(DESKTOP, "auto", docker_available=False)
    assert without.blocked and without.untrusted == "" and without.reason == "no-isolation"
    assert "start Docker" in without.detail
    assert without.os == "macos"


def test_an_explicit_subprocess_choice_never_uses_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "windows")
    selection = sandbox_selection.select(DESKTOP, "subprocess", docker_available=True)
    assert selection.blocked and selection.reason == "no-isolation"
    assert "choose Automatic or Docker" in selection.detail


def test_docker_mode_and_servers_run_everything_in_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "linux")
    _builtin(monkeypatch, True)
    docker = sandbox_selection.select(DESKTOP, "docker", docker_available=True)
    assert docker.untrusted == docker.user_authored == "docker"
    stopped = sandbox_selection.select(DESKTOP, "docker", docker_available=False)
    assert stopped.blocked and stopped.reason == "docker-unreachable" and stopped.user_authored == ""
    server = sandbox_selection.select(SERVER, "auto", docker_available=False)
    assert server.untrusted == "docker" and server.profile == "server"


def test_the_macos_sandbox_is_reported_but_never_released(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "macos")
    from orkexa.dynamic import SandboxProbe

    monkeypatch.setattr(MacOSSandbox, "probe", classmethod(lambda cls, refresh=False: SandboxProbe("macos", True)))
    native = sandbox_selection.backends(DESKTOP, docker_available=False)[0]
    assert native.name == "macos-seatbelt" and native.available
    assert native.state == "experimental" and native.reason == "not-released"
    # Even an available macOS sandbox is never chosen and never accepted by the policy.
    assert sandbox_selection.select(DESKTOP, "auto", docker_available=False).blocked
    sandbox = MacOSSandbox()
    assert isolation_of(sandbox) == "none"
    assert not DESKTOP.allows("untrusted", sandbox)


def test_windows_reports_no_native_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "windows")
    names = [(b.name, b.state, b.reason) for b in sandbox_selection.backends(DESKTOP, docker_available=True)]
    assert names == [
        ("windows-native", "unavailable", "not-implemented"),
        ("docker", "approved", ""),
        ("subprocess", "not-isolating", ""),
    ]


def test_the_runtime_platform_is_this_computer() -> None:
    system, machine = sandbox_selection.runtime_platform()
    expected = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    assert system == expected and machine


# ---- the tool library's auto fallback -------------------------------------------------------


def _untrusted(tools: ToolLibrary) -> None:
    container = ContainerDouble()
    result, info = tools.save_custom(
        CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, NAME, requirement="Convert temperatures", sandbox=container
    )
    assert result.ok and info is not None, result


def test_auto_runs_untrusted_code_in_the_fallback_container(tmp_path: Path) -> None:
    tools = ToolLibrary(tmp_path / "tools", sandbox=SubprocessSandbox(timeout=15.0, acknowledge_unsafe=True))
    _untrusted(tools)
    container = ContainerDouble()
    tools.container_fallback = lambda: container
    assert tools.run(NAME, {"celsius": 100}) == ("212.0", False)
    assert ("call", NAME) in container.calls


def test_no_fallback_without_docker_and_none_for_another_interpreter(tmp_path: Path) -> None:
    tools = ToolLibrary(tmp_path / "tools", sandbox=SubprocessSandbox(timeout=15.0, acknowledge_unsafe=True))
    _untrusted(tools)
    tools.container_fallback = lambda: None  # Docker not running
    with pytest.raises(NodestroError) as error:
        tools.run(NAME, {"celsius": 100})
    assert error.value.code == "sandbox.isolation_required"
    # A Python environment's interpreter has packages the container lacks: its code is never moved there.
    other = SubprocessSandbox(timeout=15.0, acknowledge_unsafe=True, python=str(tmp_path / "venv" / "python"))
    container = ContainerDouble()
    tools.container_fallback = lambda: container
    assert tools._fallback(other) is None


# ---- the workspace and stored settings ------------------------------------------------------


def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, docker: bool) -> Workspace:
    monkeypatch.setattr("orkexa.dynamic.DockerSandbox.available", staticmethod(lambda *a, **k: docker))
    return Workspace(tmp_path / "data", integrations=[])


def test_a_new_installation_starts_in_auto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path, monkeypatch, docker=False)
    try:
        settings = workspace.settings()
        assert settings.sandbox == "auto"
        assert settings.sandbox_selection is not None and settings.sandbox_selection.blocked
        assert [b.name for b in settings.sandbox_backends][1:] == ["docker", "subprocess"]
        assert workspace.tools.container_fallback is not None
    finally:
        workspace.close()


@pytest.mark.parametrize(
    ("stored", "expected", "fallback"),
    [("subprocess", "subprocess", False), ("docker", "docker", False), ("auto", "auto", True)],
)
def test_stored_choices_are_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: Any, expected: str, fallback: bool
) -> None:
    workspace = _workspace(tmp_path, monkeypatch, docker=True)
    try:
        workspace.update_settings(SettingsIn(sandbox=stored))
    finally:
        workspace.close()
    reopened = _workspace(tmp_path, monkeypatch, docker=True)
    try:
        assert reopened.settings().sandbox == expected
        assert (reopened.tools.container_fallback is not None) is fallback
    finally:
        reopened.close()


def test_containers_only_profiles_ignore_the_desktop_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orkexa.dynamic.DockerSandbox.available", staticmethod(lambda *a, **k: True))
    workspace = Workspace(profile=RuntimeProfile(kind="local-container", data_dir=tmp_path / "data"), integrations=[])
    try:
        assert workspace.sandbox_policy.profile == "local-container"
        assert workspace.settings().sandbox == "docker"
        assert workspace.tools.container_fallback is None
        with pytest.raises(NodestroError):
            workspace.update_settings(SettingsIn(sandbox="auto"))
    finally:
        workspace.close()


# ---- cancellation ----------------------------------------------------------------------


def test_the_runtime_cancel_check_is_orkexas() -> None:
    from orkexa.dynamic import cancel_requested, current_cancel

    token = sandbox_cancel.bind(lambda: True)
    try:
        assert current_cancel() is not None and cancel_requested()
        assert sandbox_cancel.requested()
    finally:
        sandbox_cancel.reset(token)
    assert not cancel_requested()
