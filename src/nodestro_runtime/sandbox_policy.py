"""Which code may run in which sandbox (ADR-0006).

Every piece of tool code belongs to a trust class:

* ``trusted``: code shipped with Nodestro (built-in tools) and installed integrations. It runs in process.
* ``user-authored``: code a person wrote in the tool editor, with recorded provenance
  (:mod:`nodestro_runtime.tool_provenance`).
* ``untrusted``: all AI-generated code and its tests (also after a person approved it), tool drafts, and code
  whose provenance is unknown, e.g. tools stored before provenance was recorded or restored from a backup.

The policy, not the call site, decides whether a sandbox may run a class. Only a container sandbox
(:class:`orkexa.dynamic.DockerSandbox`) counts as isolating. Untrusted code never runs in a host subprocess:
without an isolating sandbox it is refused with ``sandbox.isolation_required``. No setting, environment variable
or confirmation changes that. In the server profile every class except ``trusted`` needs isolation, and a host
subprocess sandbox cannot be created at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from orkexa.dynamic import DockerSandbox, Sandbox

from .problems import NodestroError
from .profile import DEFAULT_PROFILE, ProfileKind

TrustClass = Literal["trusted", "user-authored", "untrusted"]
TRUST_CLASSES: tuple[TrustClass, ...] = get_args(TrustClass)

Isolation = Literal["none", "container"]


def isolation_of(sandbox: Sandbox) -> Isolation:
    """How well a sandbox separates the code it runs from this computer.

    Only Orkexa's ``DockerSandbox`` isolates (no network, read-only, no capabilities, unprivileged user, limits);
    a subclass keeps those flags. Every other sandbox, including a subprocess with a custom interpreter, runs on
    the host as the user."""
    return "container" if isinstance(sandbox, DockerSandbox) else "none"


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

    def allows(self, trust: TrustClass, sandbox: Sandbox) -> bool:
        return not self.needs_isolation(trust) or isolation_of(sandbox) == "container"

    def require(self, trust: TrustClass, sandbox: Sandbox, *, name: str = "") -> None:
        """Raise ``sandbox.isolation_required`` unless ``sandbox`` may run code of class ``trust``."""
        if self.allows(trust, sandbox):
            return
        label = f"{name!r} " if name else ""
        raise NodestroError(
            "sandbox.isolation_required",
            f"The tool code {label}is {trust} and runs only in an isolating sandbox (Docker); "
            "it is not run on this computer without isolation",
            name=name,
            trust=trust,
        )

    def check_construction(self, isolation: Isolation) -> None:
        """Raise ``sandbox.unavailable`` for a sandbox kind the profile does not allow at all."""
        if self.profile != "desktop" and isolation != "container":
            raise NodestroError(
                "sandbox.unavailable",
                "The server profile runs tool code only in containers; a host subprocess sandbox is not available",
            )


__all__ = ["TRUST_CLASSES", "Isolation", "SandboxPolicy", "TrustClass", "isolation_of"]
