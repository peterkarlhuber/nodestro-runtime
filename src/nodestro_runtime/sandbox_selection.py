"""Which sandbox runs which code on this computer, and why (ADR-0006, ADR-0009).

The ``sandbox`` setting has three modes:

* ``auto`` (the default when nothing was chosen): untrusted code runs in the built-in Linux sandbox where this
  computer passed its checks, otherwise in a Docker container if Docker is running, otherwise it is refused.
  Code a person wrote runs in a host subprocess, as before.
* ``subprocess`` (chosen explicitly, kept from earlier releases): like ``auto``, but never Docker: untrusted
  code runs only in the built-in Linux sandbox, otherwise it is refused.
* ``docker``: every tool runs in a container.

Server and Nodestro Local Docker always run tool code in containers; the setting does not apply there.

Orkexa reports what each backend enforces (:class:`orkexa.dynamic.SandboxCapabilities`) and whether it can run
here (:meth:`orkexa.dynamic.Sandbox.probe`); this module turns that into the report the studio shows. Whether a
backend may run untrusted code is :class:`~nodestro_runtime.sandbox_policy.SandboxPolicy`'s decision alone:
only containers and the built-in Linux sandbox (desktop, tested systems, self-test passed) are released for it.
The macOS sandbox of Orkexa is reported as *experimental* and never used; Windows has no native sandbox yet.
A backend that is merely available is never released because of its name or technology.

The operating system is always the one of the computer the runtime runs on, not the browser's.
"""

from __future__ import annotations

import platform
import sys
from typing import Literal

from orkexa.dynamic import DockerSandbox, MacOSSandbox

from .builtin_sandbox import LinuxSandbox
from .sandbox_policy import SandboxPolicy
from .schemas import SandboxBackendStatus, SandboxSelection

BackendState = Literal["approved", "experimental", "unavailable", "not-isolating", "not-applicable"]
SandboxMode = Literal["auto", "subprocess", "docker"]
MODES: tuple[SandboxMode, ...] = ("auto", "subprocess", "docker")


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
        caps = MacOSSandbox().capabilities()
        if not desktop:
            return SandboxBackendStatus(
                name="macos-seatbelt", state="not-applicable", reason="server", maturity=caps.maturity
            )
        probe = MacOSSandbox.probe()
        return SandboxBackendStatus(
            name="macos-seatbelt",
            available=probe.available,
            # Implemented and self-tested, but not reviewed and not released: never used for any code.
            state="experimental" if probe.available else "unavailable",
            reason="not-released" if probe.available else probe.reason,
            detail=probe.detail,
            isolation=caps.isolation,
            maturity=caps.maturity,
            memory=caps.memory,
            notes=list(caps.notes),
        )
    return SandboxBackendStatus(
        name="windows-native" if system == "windows" else f"{system}-native",
        state="unavailable",
        reason="not-implemented",
        detail=f"Nodestro has no native sandbox for {system} yet",
    )


def backends(policy: SandboxPolicy, docker_available: bool) -> list[SandboxBackendStatus]:
    """Every backend of this computer: the native one of its operating system, Docker and the subprocess."""
    docker = DockerSandbox().capabilities()
    desktop = policy.profile == "desktop"
    return [
        _native(policy),
        SandboxBackendStatus(
            name="docker",
            available=docker_available,
            state="approved" if docker_available else "unavailable",
            reason="" if docker_available else "unreachable",
            isolation=docker.isolation,
            maturity=docker.maturity,
            memory=docker.memory,
            notes=list(docker.notes),
        ),
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


def select(policy: SandboxPolicy, mode: SandboxMode, docker_available: bool) -> SandboxSelection:
    """Where untrusted and user-authored code runs now, or why untrusted code is refused."""
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
        if docker_available or policy.profile != "desktop":
            return result("docker", "docker")
        detail = "Docker is selected but not reachable; no tool code runs until it is started"
        return result("", "", "docker-unreachable", detail)
    if policy.builtin_available():
        return result("linux-builtin", "subprocess")
    if mode == "auto" and docker_available:
        return result("docker", "subprocess")
    native = _native(policy)
    detail = (
        f"The built-in sandbox is not available here ({native.reason}: {native.detail})"
        if system == "linux"
        else f"Nodestro has no released native sandbox for {system}"
    )
    hint = "start Docker" if mode == "auto" else "choose Automatic or Docker and start Docker"
    return result("", "subprocess", "no-isolation", f"{detail}; AI-generated code is refused. To run it, {hint}.")


__all__ = ["MODES", "SandboxMode", "backends", "runtime_platform", "select"]
