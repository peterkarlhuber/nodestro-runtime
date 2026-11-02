"""Which sandbox runs which code on this computer, and why (ADR-0006, ADR-0009).

The ``sandbox`` setting has four modes (Docker-first, ADR-0006):

* ``auto`` (the default when nothing was chosen): untrusted (AI-generated) code runs in a Docker container once Docker
  passed its preflight and self-test, otherwise it is refused with Docker's reason. The built-in Linux sandbox is never
  chosen automatically, also not when Docker is missing. Code a person wrote runs in a host subprocess, as before.
* ``linux`` (chosen explicitly, Linux only): untrusted code runs in the built-in Linux sandbox where this computer
  passed its checks, otherwise it is refused; never in Docker. Third-party libraries are not available to untrusted
  code in this mode (:meth:`nodestro_runtime.python_envs.PythonEnvironments.backend`).
* ``subprocess`` (chosen explicitly, for trusted use only): untrusted code is always refused; only code a person
  wrote runs, in a host subprocess. It is never a fallback for AI-generated code.
* ``docker``: every tool runs in a container.

Server and Nodestro Local Docker always run tool code in containers; the setting does not apply there.

Orkexa reports what each backend enforces (:class:`orkexa.dynamic.SandboxCapabilities`) and whether it can run
here (:meth:`orkexa.dynamic.Sandbox.probe`); this module turns that into the report the studio shows. Whether a
backend may run untrusted code is :class:`~nodestro_runtime.sandbox_policy.SandboxPolicy`'s decision alone:
only containers (preflight and in-container self-test passed, :mod:`nodestro_runtime.docker_status`) and the
built-in Linux sandbox (desktop, tested systems, self-test passed) are released for it. Docker's status comes from a
background check, so the report never waits for the daemon (state ``checking`` until the first check finished).
Orkexa's macOS (Seatbelt) sandbox was removed: macOS reports ``macos-seatbelt`` as ``unavailable`` (``removed``);
Windows has no native sandbox. A backend that is merely available is never released because of its name or
technology.

The operating system is always the one of the computer the runtime runs on, not the browser's.
"""

from __future__ import annotations

import platform
import sys
from typing import Literal

from .builtin_sandbox import LinuxSandbox
from .docker_status import DockerStatus, approved_status, unavailable_status
from .sandbox_policy import SandboxPolicy
from .schemas import SandboxBackendStatus, SandboxSelection

BackendState = Literal["approved", "experimental", "unavailable", "not-isolating", "not-applicable", "checking"]
SandboxMode = Literal["auto", "subprocess", "linux", "docker"]
MODES: tuple[SandboxMode, ...] = ("auto", "subprocess", "linux", "docker")


def runtime_platform() -> tuple[str, str]:
    """The runtime's operating system (``linux``, ``macos``, ``windows`` or ``sys.platform``) and CPU."""
    system = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    if sys.platform.startswith("linux"):
        system = "linux"
    machine = platform.machine().lower()
    return system, {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)


def _native(policy: SandboxPolicy) -> SandboxBackendStatus:
    """The native sandbox of this operating system, as the policy sees it."""
    system, _ = runtime_platform()
    desktop = policy.profile == "desktop"
    if system == "linux":
        probe = LinuxSandbox.probe()
        caps = LinuxSandbox().capabilities()
        state: BackendState
        if not desktop:
            state, reason = "not-applicable", "server"
        elif policy.builtin_available():
            state, reason = "approved", ""
        else:
            state, reason = "unavailable", probe.reason
        return SandboxBackendStatus(
            name="linux-builtin",
            available=probe.available,
            state=state,
            reason=reason,
            detail=probe.detail,
            isolation=caps.isolation,
            maturity=caps.maturity,
            memory=caps.memory,
            notes=list(caps.notes),
        )
    if system == "macos":
        # Orkexa's Seatbelt sandbox was removed (not releasable): reported, never probed, never used.
        return SandboxBackendStatus(
            name="macos-seatbelt",
            state="unavailable" if desktop else "not-applicable",
            reason="removed" if desktop else "server",
            detail="The macOS sandbox was removed; AI-generated code runs in Docker on macOS",
        )
    return SandboxBackendStatus(
        name="windows-native" if system == "windows" else f"{system}-native",
        state="unavailable",
        reason="not-implemented",
        detail=f"Nodestro has no native sandbox for {system} yet",
    )


def docker_status_of(docker: DockerStatus | bool) -> DockerStatus:
    """The Docker status, also from the earlier ``docker_available`` flag (``True`` counts as proven)."""
    if isinstance(docker, DockerStatus):
        return docker
    if docker:
        return approved_status("Docker is available")
    return unavailable_status("docker-unreachable", "Docker is not reachable")


def _docker_argument(docker: DockerStatus | bool | None, docker_available: bool | None) -> DockerStatus:
    if docker is None:
        docker = bool(docker_available)
    return docker_status_of(docker)


def backends(
    policy: SandboxPolicy, docker: DockerStatus | bool | None = None, *, docker_available: bool | None = None
) -> list[SandboxBackendStatus]:
    """Every backend of this computer: the native one of its operating system, Docker and the subprocess.

    ``docker`` is the Docker sandbox's status (:meth:`nodestro_runtime.workspace.Workspace.docker_status`); a bool
    (or ``docker_available``) is still understood: ``True`` counts as passed preflight and self-test."""
    status = _docker_argument(docker, docker_available)
    desktop = policy.profile == "desktop"
    return [
        _native(policy),
        status.backend(),
        SandboxBackendStatus(
            name="subprocess",
            available=desktop,
            state="not-isolating" if desktop else "not-applicable",
            reason="" if desktop else "server",
            isolation="none",
            memory="kernel" if sys.platform.startswith("linux") else "none",
            notes=["Not a security boundary: runs only code a person wrote, as the user, with the user's files."],
        ),
    ]


def select(
    policy: SandboxPolicy,
    mode: SandboxMode,
    docker: DockerStatus | bool | None = None,
    *,
    docker_available: bool | None = None,
) -> SandboxSelection:
    """Where untrusted and user-authored code runs now, or why untrusted code is refused.

    Docker counts only when its preflight and self-test passed (state ``approved``); in Docker mode, on a server and
    in Nodestro Local Docker everything is refused until then, with the Docker backend's reason."""
    status = _docker_argument(docker, docker_available)
    system, machine = runtime_platform()

    def result(untrusted: str, user_authored: str, reason: str = "", detail: str = "") -> SandboxSelection:
        return SandboxSelection(
            mode=mode,
            profile=policy.profile,
            os=system,
            arch=machine,
            untrusted=untrusted,
            user_authored=user_authored,
            blocked=not untrusted,
            reason=reason,
            detail=detail,
        )

    if policy.profile != "desktop" or mode == "docker":
        if status.approved:
            return result("docker", "docker")
        where = "Docker is selected" if policy.profile == "desktop" else "Tool code runs only in Docker here"
        if status.state == "checking":
            detail = f"{where}; its preflight and self-test are still running, so no tool code runs yet"
        else:
            detail = f"{where} but the sandbox is not available ({status.detail}); no tool code runs until it passes"
        return result("", "", status.reason or "docker-unreachable", detail)
    if mode == "auto":
        # Docker-first: no automatic fallback to the built-in Linux sandbox or to the host.
        if status.approved:
            return result("docker", "subprocess")
        if status.state == "checking":
            detail = (
                "Automatic mode runs AI-generated code in Docker; its preflight and self-test are still running, so "
                "AI-generated code is refused until they passed."
            )
        else:
            detail = (
                f"Automatic mode runs AI-generated code only in Docker, which is not available ({status.detail}); "
                "AI-generated code is refused. To run it, start Docker."
            )
        return result("", "subprocess", status.reason or "docker-unreachable", detail)
    if mode == "linux":
        if policy.builtin_available():
            return result(
                "linux-builtin",
                "subprocess",
                detail="The built-in Linux sandbox was chosen; AI-generated code gets no third-party libraries here.",
            )
        native = _native(policy)
        detail = (
            f"The built-in sandbox is not available here ({native.reason}: {native.detail})"
            if system == "linux"
            else f"Nodestro has no native sandbox for {system}"
        )
        return result(
            "",
            "subprocess",
            "no-isolation",
            f"{detail}; AI-generated code is refused. To run it, choose Automatic and start Docker.",
        )
    return result(
        "",
        "subprocess",
        "no-isolation",
        "The Subprocess mode runs only code a person wrote; AI-generated code is refused. To run it, choose Automatic "
        "and start Docker.",
    )


__all__ = ["MODES", "SandboxMode", "backends", "docker_status_of", "runtime_platform", "select"]
