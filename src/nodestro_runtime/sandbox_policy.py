"""Which code may run in which sandbox (ADR-0006).

Every piece of tool code belongs to a trust class:

* ``trusted``: code shipped with Nodestro (built-in tools) and installed integrations. It runs in process.
* ``user-authored``: code a person wrote in the tool editor, with recorded provenance
  (:mod:`nodestro_runtime.tool_provenance`).
* ``untrusted``: all AI-generated code and its tests (also after a person approved it), tool drafts, and code
  whose provenance is unknown, e.g. tools stored before provenance was recorded or restored from a backup.

The policy, not the call site, decides whether a sandbox may run a class. Two sandboxes count as isolating:

* a container (:class:`orkexa.dynamic.DockerSandbox`), in every profile;
* the built-in Linux sandbox (:class:`nodestro_runtime.builtin_sandbox.LinuxSandbox`, Landlock and seccomp), in the
  desktop profile only, and only on a computer where its self-test passed and whose distribution is one of the
  tested systems (:data:`nodestro_runtime.builtin_sandbox.TESTED_SYSTEMS`).

Untrusted code never runs in a host subprocess: without an isolating sandbox it is refused with
``sandbox.isolation_required``, and the message says why the built-in sandbox is not available. No setting,
environment variable or confirmation changes that, and there is no fallback to a host subprocess. In the server
profile every class except ``trusted`` needs a container, and a host subprocess sandbox cannot be created at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from orkexa.dynamic import DockerSandbox, Sandbox

from .builtin_sandbox import LinuxSandbox
from .problems import NodestroError
from .profile import DEFAULT_PROFILE, ProfileKind

TrustClass = Literal["trusted", "user-authored", "untrusted"]
TRUST_CLASSES: tuple[TrustClass, ...] = get_args(TrustClass)

Isolation = Literal["none", "container", "kernel"]


def isolation_of(sandbox: Sandbox) -> Isolation:
    """How well a sandbox separates the code it runs from this computer.

    Orkexa's ``DockerSandbox`` isolates with a container (no network, read-only, no capabilities, unprivileged user,
    limits); a subclass keeps those flags. The built-in ``LinuxSandbox`` isolates with the kernel (``kernel``) when
    this computer passed its checks (:meth:`LinuxSandbox.status`). Every other sandbox, including a subprocess with a
    custom interpreter, runs on the host as the user."""
    if isinstance(sandbox, DockerSandbox):
        return "container"
    if isinstance(sandbox, LinuxSandbox) and LinuxSandbox.status().available:
        return "kernel"
    return "none"


@dataclass(frozen=True)
class SandboxPolicy:
    """The rules of one operating profile."""

    profile: ProfileKind = DEFAULT_PROFILE

    def needs_isolation(self, trust: TrustClass) -> bool:
        """Whether code of this class may only run in an isolating sandbox."""
        if trust not in TRUST_CLASSES:
            return True  # an unknown class is treated like untrusted code
        if trust == "trusted":
            return False
        return trust == "untrusted" or self.profile != "desktop"

    def accepts(self, isolation: Isolation) -> bool:
        """Whether this profile counts ``isolation`` as isolating: a container everywhere, the built-in Linux sandbox
        only on the desktop (a server keeps its tenants' code in containers)."""
        if isolation == "container":
            return True
        return isolation == "kernel" and self.profile == "desktop"

    def allows(self, trust: TrustClass, sandbox: Sandbox) -> bool:
        return not self.needs_isolation(trust) or self.accepts(isolation_of(sandbox))

    def builtin_available(self) -> bool:
        """Whether the built-in Linux sandbox may isolate code here (desktop profile, checks passed)."""
        return self.profile == "desktop" and LinuxSandbox.status().available

    def require(self, trust: TrustClass, sandbox: Sandbox, *, name: str = "") -> None:
        """Raise ``sandbox.isolation_required`` unless ``sandbox`` may run code of class ``trust``."""
        if self.allows(trust, sandbox):
            return
        label = f"{name!r} " if name else ""
        reason, detail = "", ""
        if self.profile == "desktop":
            status = LinuxSandbox.status()
            reason, detail = status.reason, status.detail
        why = f" The built-in Linux sandbox is not available here: {detail}." if detail else ""
        raise NodestroError(
            "sandbox.isolation_required",
            f"The tool code {label}is {trust} and runs only in an isolating sandbox (the built-in Linux sandbox or "
            f"Docker); it is not run on this computer without isolation.{why}",
            name=name,
            trust=trust,
            reason=reason,
        )

    def check_construction(self, isolation: Isolation) -> None:
        """Raise ``sandbox.unavailable`` for a sandbox kind the profile does not allow at all."""
        if self.profile != "desktop" and isolation != "container":
            raise NodestroError(
                "sandbox.unavailable",
                "The server profile runs tool code only in containers; a host subprocess sandbox is not available",
            )


__all__ = ["TRUST_CLASSES", "Isolation", "SandboxPolicy", "TrustClass", "isolation_of"]
