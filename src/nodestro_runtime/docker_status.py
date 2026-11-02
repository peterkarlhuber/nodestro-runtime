"""Whether the Docker sandbox is proven on this computer, checked in the background (Docker-first plan R2).

Docker is the sandbox for AI-generated code wherever the built-in Linux sandbox is not released, so the runtime must
know whether the daemon behind the ``docker`` CLI really enforces the sandbox. Two of Orkexa's checks decide it:

* the **preflight** (:func:`orkexa.dynamic.docker.preflight`): the CLI exists, the daemon answers, runs Linux
  containers, enforces memory, process and CPU limits and filters system calls with seccomp. It is cheap
  (``docker info``) and repeated at most every :data:`PREFLIGHT_TTL` seconds;
* the **self-test** (:meth:`orkexa.dynamic.DockerSandbox.self_test`): one container of exactly the sandbox's shape in
  Nodestro's runtime image shows what the daemon applied. It runs once per daemon (its
  :func:`~orkexa.dynamic.docker.host_fingerprint`); a changed daemon or setting runs it again, a failed one is retried
  after :data:`RETRY_FAILED` seconds or on request.

Both run in a background thread (:class:`DockerMonitor`), never in the caller's: reading the settings never waits for
``docker info`` (a daemon that does not answer takes up to 10 s). Until the first check finished, the state is
``checking`` and nothing runs in Docker. Only when both checks passed is the Docker backend ``approved`` and a
container sandbox counts as isolating (:func:`daemon_proven`, used by
:func:`nodestro_runtime.sandbox_policy.isolation_of`); otherwise untrusted code is refused, and every container call
still checks again itself before it starts (Orkexa). The Studio starts without Docker; only the secure execution is
blocked, with the reason.

The monitor also removes containers this installation left behind (:func:`orkexa.dynamic.docker.reap`, only those
labelled ``org.orkexa.sandbox=<owner>``): at the start all of them (a desktop or Nodestro Local Docker, which own
their containers), then every :data:`REAP_INTERVAL` seconds those past their deadline. The owner names one
installation (:func:`installation_owner`: ``nodestro-<id>``), so two installations on one daemon (two desktop users,
a desktop and Nodestro Local Docker) never remove each other's containers. A server's API and worker processes share
the owner :data:`OWNER` and only ever remove expired containers.

The local MCP servers' containers carry the installation's MCP owner (:func:`mcp_owner`), which the tool reaper never
touches (they live as long as their connection, up to :data:`MCP_LIFETIME`). The same reaper thread removes them
separately (:meth:`DockerMonitor.reap_mcp_once`): every :data:`REAP_INTERVAL` seconds those past their deadline, and at
the start of a process that owns its containers also every one created before the reaper started. That process holds
the data directory's exclusive lock (:class:`~nodestro_runtime.workspace.DataDirectoryLock`) and the owner names that
data directory and installation, so the process that started such a container is gone; a container this process
starts later has a later deadline and is kept, and another installation's containers have another owner.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import contextlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Literal, Protocol

from orkexa.dynamic import DockerSandbox, SandboxError, SandboxProbe
from orkexa.dynamic.docker import (
    DockerHost,
    DockerProbeError,
    host_fingerprint,
    inspect_host,
    list_sandbox_containers,
    preflight,
    reap,
)

if TYPE_CHECKING:  # the schemas import the sandbox policy, which imports this module
    from .schemas import SandboxBackendStatus

logger = logging.getLogger(__name__)

#: The owner label (``org.orkexa.sandbox``) of a server's tool containers (shared by its API and worker processes,
#: which only ever reap expired ones) and the prefix of an installation's own owner (:func:`installation_owner`).
OWNER = "nodestro"
#: Orkexa's rule for an owner (:func:`orkexa.dynamic.docker.run_args` refuses any other).
_OWNER_RULE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
#: Seconds a preflight result is reused before ``docker info`` runs again.
PREFLIGHT_TTL = 5.0
#: Seconds before a failed self-test is tried again by itself (a passed one is kept per daemon fingerprint).
RETRY_FAILED = 300.0
#: Seconds between two runs of the reaper (containers past their deadline).
REAP_INTERVAL = 60.0
#: Seconds a passed check counts for :func:`daemon_proven` without being confirmed again.
PROOF_TTL = 600.0
#: Seconds a local MCP server's container may exist (its deadline label; it lives as long as its connection).
MCP_LIFETIME = 7 * 24 * 3600.0

DockerState = Literal["approved", "unavailable", "checking"]

#: Orkexa's reason codes, and the reasons of the Docker backend they become (``SandboxBackendStatus.reason``).
REASONS: dict[str, str] = {
    "missing": "docker-missing",
    "unreachable": "docker-unreachable",
    "malformed": "docker-unreachable",
    "limits": "docker-limits",
    "seccomp": "docker-seccomp",
    "rootless-cgroup": "docker-rootless-cgroup",
    "os-type": "docker-os-type",
    "image": "docker-image",
    "self-test": "docker-selftest",
}
#: Every reason the Docker backend can report (with state ``checking``: ``docker-checking``).
DOCKER_REASONS = (*dict.fromkeys(REASONS.values()), "docker-checking")

#: Shown with a rootful daemon on Linux: who may use its socket is root on this computer.
ROOTFUL_NOTE = (
    "This Docker daemon runs as root: membership in the docker group (or access to its socket) is equivalent to root "
    "access on this computer. Rootless Docker or Podman limit what a container escape could reach."
)


def installation_owner(data_dir: str | Path, installation: str = "") -> str:
    """The owner label of one installation's sandbox containers: ``nodestro-<12 hex digits>``.

    The digits are the start of the SHA-256 of the resolved data directory and the installation's random id
    (:attr:`nodestro_runtime.python_envs.PythonEnvironments.installation`), so two installations on one Docker daemon
    get different owners even when their data directories have the same path (two Nodestro Local Docker containers,
    each with its own ``/data`` volume). The reaper of one installation never sees the other's containers."""
    resolved = str(Path(data_dir).expanduser().resolve())
    digest = hashlib.sha256(f"{resolved}\0{installation}".encode()).hexdigest()[:12]
    return _checked_owner(f"{OWNER}-{digest}")


def mcp_owner(owner: str = OWNER) -> str:
    """The owner label of the local MCP servers' containers of the installation whose tool containers carry ``owner``
    (``nodestro-mcp`` for :data:`OWNER`, ``nodestro-mcp-<id>`` for ``nodestro-<id>``). It differs from every tool
    owner, so the tool containers' reaper never removes a running MCP server (they live as long as their
    connection)."""
    if owner == OWNER:
        return f"{OWNER}-mcp"
    prefix = f"{OWNER}-"
    if not owner.startswith(prefix) or owner.startswith(f"{OWNER}-mcp"):
        raise ValueError(f"Not a tool container owner: {owner!r}")
    return _checked_owner(f"{OWNER}-mcp-{owner[len(prefix) :]}")


def _checked_owner(owner: str) -> str:
    if not _OWNER_RULE.fullmatch(owner):
        raise ValueError(f"Invalid sandbox owner {owner!r}")
    return owner


def reason_of(code: str) -> str:
    """The Docker backend's reason for one of Orkexa's probe reasons (unknown ones count as ``docker-unreachable``)."""
    return REASONS.get(code, "docker-unreachable")


@dataclass(frozen=True)
class SandboxCheck:
    """One check behind the status (:class:`nodestro_runtime.schemas.SandboxCheck` in the report)."""

    name: str
    ok: bool = False
    detail: str = ""


@dataclass(frozen=True)
class DockerStatus:
    """The Docker sandbox on this computer, as the last background check found it."""

    state: DockerState
    #: One of :data:`DOCKER_REASONS`; empty when approved.
    reason: str = ""
    detail: str = ""
    host: DockerHost | None = None
    checks: tuple[SandboxCheck, ...] = ()
    #: When the self-test last ran (or its passed result was confirmed for this daemon).
    tested_at: datetime | None = None
    fingerprint: str = ""
    notes: tuple[str, ...] = field(default=())

    @property
    def approved(self) -> bool:
        return self.state == "approved"

    def backend(self) -> SandboxBackendStatus:
        """The Docker row of :func:`nodestro_runtime.sandbox_selection.backends`."""
        from .schemas import SandboxBackendStatus
        from .schemas import SandboxCheck as CheckModel

        caps = DockerSandbox(image="nodestro-runtime").capabilities()
        return SandboxBackendStatus(
            name="docker",
            available=self.approved,
            state=self.state,
            reason=self.reason,
            detail=self.detail,
            isolation=caps.isolation,
            maturity=caps.maturity,
            memory=caps.memory,
            notes=[*caps.notes, *self.notes],
            engine=self.host.engine if self.host is not None else "",
            rootless=self.host.rootless if self.host is not None else None,
            cgroup_version=self.host.cgroup_version if self.host is not None else None,
            checks=[CheckModel(name=item.name, ok=item.ok, detail=item.detail) for item in self.checks],
            tested_at=self.tested_at,
        )


CHECKING = DockerStatus(
    "checking",
    "docker-checking",
    "The Docker sandbox is being checked (preflight and self-test); nothing runs in it until the check passed",
)


def approved_status(detail: str = "Preflight and self-test passed") -> DockerStatus:
    """An approved status (tests, embedders that prove Docker themselves)."""
    return DockerStatus("approved", "", detail, checks=(SandboxCheck(name="self-test", ok=True),))


def unavailable_status(reason: str = "docker-missing", detail: str = "Docker is not installed") -> DockerStatus:
    return DockerStatus("unavailable", reason, detail)


# ---- proofs per daemon --------------------------------------------------------------------------------------------

_DaemonKey = tuple[str, str, str]
_PROOFS: dict[_DaemonKey, float] = {}
_LAST_REASON: dict[_DaemonKey, str] = {}
_PROOFS_LOCK = threading.Lock()


def daemon_key(docker: str, environ: Mapping[str, str] | None) -> _DaemonKey:
    """The daemon a docker CLI with this environment talks to (``DOCKER_HOST`` / ``DOCKER_CONTEXT``)."""
    source = os.environ if environ is None else environ
    return (docker, source.get("DOCKER_HOST", ""), source.get("DOCKER_CONTEXT", ""))


#: Orkexa reasons that are about one image, not the daemon: they never revoke a daemon's proof (the call of that image
#: is refused by Orkexa anyway).
_IMAGE_REASONS = frozenset({"image", "self-test"})


def record_proof(docker: str, environ: Mapping[str, str] | None, probe: SandboxProbe) -> None:
    """Remember a self-test result of this daemon: a pass proves it (for :data:`PROOF_TTL`); a failure of the daemon
    (CLI, reachability, preflight) revokes the proof, a failure of one image does not."""
    key = daemon_key(docker, environ)
    with _PROOFS_LOCK:
        if probe.available:
            _PROOFS[key] = time.monotonic()
            _LAST_REASON.pop(key, None)
        elif probe.reason not in _IMAGE_REASONS:
            _PROOFS.pop(key, None)
            _LAST_REASON[key] = reason_of(probe.reason)


def revoke_proof(docker: str, environ: Mapping[str, str] | None, reason: str) -> None:
    key = daemon_key(docker, environ)
    with _PROOFS_LOCK:
        _PROOFS.pop(key, None)
        _LAST_REASON[key] = reason


def daemon_proven(docker: str, environ: Mapping[str, str] | None) -> bool:
    """Whether this daemon passed the preflight and the self-test within :data:`PROOF_TTL` seconds. Never blocks;
    a registered :class:`DockerMonitor` of the daemon is asked to check again (in its thread) when the proof is
    missing or old."""
    key = daemon_key(docker, environ)
    with _PROOFS_LOCK:
        proven = key in _PROOFS and time.monotonic() - _PROOFS[key] < PROOF_TTL
        # Any monitor of this daemon (one per owner, i.e. per installation of the process) can check it again.
        monitor = next((found for (daemon, _owner), found in _MONITORS.items() if daemon == key), None)
    if not proven and monitor is not None:
        monitor.status()  # starts a check in the background if one is due
    return proven


def last_reason(docker: str, environ: Mapping[str, str] | None) -> str:
    """Why the daemon is not proven (``docker-checking`` while nothing is known yet)."""
    with _PROOFS_LOCK:
        return _LAST_REASON.get(daemon_key(docker, environ), "docker-checking")


def clear_proofs() -> None:
    """Forget every proof (tests; after the daemon's configuration was changed in place)."""
    with _PROOFS_LOCK:
        _PROOFS.clear()
        _LAST_REASON.clear()


# ---- the monitor --------------------------------------------------------------------------------------------------


class DockerMonitor:
    """Checks the Docker sandbox in a background thread and reaps Nodestro's left-over containers.

    ``sandbox`` builds the container sandbox whose self-test proves the daemon (the workspace's runtime-image
    sandbox: same docker CLI, environment and limits as the calls). :meth:`status` never blocks unless asked to
    wait."""

    def __init__(
        self,
        sandbox: Callable[[], DockerSandbox],
        *,
        docker: str = "docker",
        environ: Mapping[str, str] | None = None,
        owner: str = OWNER,
        preflight_ttl: float = PREFLIGHT_TTL,
        retry_failed: float = RETRY_FAILED,
        reap_interval: float = REAP_INTERVAL,
        inspect: Callable[..., DockerHost] = inspect_host,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sandbox = sandbox
        self.docker = docker
        self.environ = dict(environ) if environ is not None else None
        self.owner = owner
        self.preflight_ttl = preflight_ttl
        self.retry_failed = retry_failed
        self.reap_interval = reap_interval
        self._inspect = inspect
        self._clock = clock
        self._lock = threading.Lock()
        self._status: DockerStatus = CHECKING
        self._checked_at: float | None = None
        self._thread: threading.Thread | None = None
        self._done = threading.Event()
        #: Self-test results per host fingerprint: (monotonic time, probe, wall-clock time).
        self._self_tests: dict[str, tuple[float, SandboxProbe, datetime]] = {}
        self._force_self_test = False
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    # -- status --

    def status(self, *, refresh: bool = False, wait: float = 0.0) -> DockerStatus:
        """The last result; starts a check in the background when the preflight is older than ``preflight_ttl``
        (or with ``refresh``, which also runs the self-test again). ``wait`` seconds at most for that check."""
        with self._lock:
            due = refresh or self._checked_at is None or self._clock() - self._checked_at >= self.preflight_ttl
            if refresh:
                self._force_self_test = True
            running = self._thread is not None and self._thread.is_alive()
            if due and not running and not self._stop.is_set():
                self._done.clear()
                self._thread = threading.Thread(target=self._run, name="nodestro-docker-status", daemon=True)
                self._thread.start()
            thread = self._thread
        if wait > 0 and thread is not None:
            thread.join(wait)
        with self._lock:
            return self._status

    def wait(self, timeout: float | None = None) -> DockerStatus:
        """Wait for the running check (tests, a command line); returns the status."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self._status

    def _run(self) -> None:
        try:
            found = self.check()
        except Exception as exc:  # a broken check never stops the runtime; it reports why
            logger.exception("Checking the Docker sandbox failed")
            found = DockerStatus("unavailable", "docker-unreachable", f"Checking Docker failed: {exc}")
        with self._lock:
            self._status = found
            self._checked_at = self._clock()
        self._done.set()

    def check(self) -> DockerStatus:
        """One check in the calling thread: preflight, then the self-test unless this daemon passed it already."""
        checks: list[SandboxCheck] = []
        path = self.environ.get("PATH") if self.environ is not None else None
        found_cli = shutil.which(self.docker) if path is None else shutil.which(self.docker, path=path)
        if found_cli is None and not os.path.isabs(self.docker):
            detail = f"The {self.docker} command is not installed"
            checks.append(SandboxCheck(name="cli", ok=False, detail=detail))
            return self._refused("docker-missing", detail, checks)
        checks.append(SandboxCheck(name="cli", ok=True, detail=found_cli or self.docker))
        try:
            host = self._inspect(self.docker, env=self.environ)
        except DockerProbeError as exc:
            checks.append(SandboxCheck(name="daemon", ok=False, detail=exc.detail))
            return self._refused(reason_of(exc.reason), exc.detail, checks)
        checks.append(SandboxCheck(name="daemon", ok=True, detail=f"{host.engine} {host.server_version}".strip()))
        checked = preflight(host)
        checks.append(SandboxCheck(name="preflight", ok=checked.available, detail=checked.detail))
        notes = self._notes(host)
        if not checked.available:
            return self._refused(reason_of(checked.reason), checked.detail, checks, host, notes)
        fingerprint = host_fingerprint(host)
        with self._lock:
            remembered = self._self_tests.get(fingerprint)
            force = self._force_self_test
            self._force_self_test = False
        if (
            remembered is None
            or force
            or (not remembered[1].available and self._clock() - remembered[0] >= self.retry_failed)
        ):
            try:
                probe = self.sandbox().self_test()
            except SandboxError as exc:
                probe = SandboxProbe("docker", False, "self-test", str(exc))
            remembered = (self._clock(), probe, datetime.now(timezone.utc))
            with self._lock:
                self._self_tests = {fingerprint: remembered}  # only the current daemon matters
        _, probe, tested_at = remembered
        image = dict(probe.facts).get("image_id", "")
        if not probe.available:
            stage = "image" if probe.reason in ("image", "missing") else "self-test"
            checks.append(SandboxCheck(name=stage, ok=False, detail=probe.detail))
            reason = "docker-image" if stage == "image" else reason_of(probe.reason)
            return replace(self._refused(reason, probe.detail, checks, host, notes, fingerprint), tested_at=tested_at)
        checks.append(SandboxCheck(name="image", ok=True, detail=image))
        checks.append(SandboxCheck(name="self-test", ok=True, detail="A container of the sandbox's shape passed"))
        record_proof(self.docker, self.environ, probe)
        detail = (
            f"Preflight and self-test passed ({host.engine} {host.server_version}, "
            f"cgroup v{host.cgroup_version or '?'}{', rootless' if host.rootless else ''})"
        )
        if ROOTFUL_NOTE in notes:
            detail += f". {ROOTFUL_NOTE}"
        return DockerStatus("approved", "", detail, host, tuple(checks), tested_at, fingerprint, notes)

    def _refused(
        self,
        reason: str,
        detail: str,
        checks: list[SandboxCheck],
        host: DockerHost | None = None,
        notes: tuple[str, ...] = (),
        fingerprint: str = "",
    ) -> DockerStatus:
        revoke_proof(self.docker, self.environ, reason)
        return DockerStatus("unavailable", reason, detail, host, tuple(checks), None, fingerprint, notes)

    @staticmethod
    def _notes(host: DockerHost) -> tuple[str, ...]:
        """The rootful-daemon note on Linux (Docker Desktop, OrbStack and Colima keep the daemon in a VM)."""
        linux = sys.platform.startswith("linux")
        if linux and not host.rootless and host.engine in ("docker", "podman", "unknown"):
            return (ROOTFUL_NOTE,)
        return ()

    # -- reaper --

    def start_reaper(self, *, remove_all_first: bool) -> None:
        """Start the reaper thread once: first all of this owner's containers (``remove_all_first``: the process owns
        them, e.g. a desktop), then every ``reap_interval`` seconds those past their deadline. The local MCP servers'
        containers of the installation are reaped alongside (:meth:`reap_mcp_once`): at the start with
        ``remove_all_first`` every one started before this call, then those past their deadline."""
        with self._lock:
            if self._reaper is not None or self._stop.is_set():
                return
            # MCP containers whose deadline is earlier than one started now were started before this reaper.
            started = time.time() if remove_all_first else None
            self._reaper = threading.Thread(
                target=self._reap_loop, args=(remove_all_first, started), name="nodestro-docker-reaper", daemon=True
            )
            self._reaper.start()

    def reap_once(self, *, remove_all: bool = False) -> int:
        """Remove this owner's containers past their deadline (all with ``remove_all``); 0 when Docker cannot be
        asked (no daemon, or the sandbox gateway, which reaps its own containers)."""
        if shutil.which(self.docker) is None and not os.path.isabs(self.docker):
            return 0
        try:
            return reap(self.docker, self.owner, remove_all=remove_all, env=self.environ)
        except (SandboxError, ValueError, OSError) as exc:
            logger.debug("Reaping sandbox containers failed: %s", exc)
            return 0

    def mcp_container_owner(self) -> str | None:
        """The owner label of this installation's local MCP servers' containers (``None``: the tool owner is not one
        of Nodestro's, so there is none)."""
        try:
            return mcp_owner(self.owner)
        except ValueError:
            return None

    def reap_mcp_once(self, *, started_before: float | None = None) -> int:
        """Remove this installation's local MCP server containers past their deadline (or without a valid one), and
        with ``started_before`` (a time) also those started before it: their deadline is earlier than
        ``started_before + MCP_LIFETIME``. Only a process that holds the data directory's lock passes it (at its
        start), so no running connection owns such a container. 0 when Docker cannot be asked."""
        owner = self.mcp_container_owner()
        if owner is None or (shutil.which(self.docker) is None and not os.path.isabs(self.docker)):
            return 0
        try:
            containers = list_sandbox_containers(self.docker, owner, env=self.environ)
        except (SandboxError, ValueError, OSError) as exc:
            logger.debug("Listing the MCP server containers failed: %s", exc)
            return 0
        now = time.time()
        removed = 0
        for container in containers:
            expired = container.deadline is None or container.deadline <= now
            orphaned = (
                started_before is not None
                and container.deadline is not None
                and container.deadline < started_before + MCP_LIFETIME
            )
            if (expired or orphaned) and self._remove(container.id):
                removed += 1
        return removed

    def _remove(self, container_id: str) -> bool:
        try:
            done = subprocess.run(
                [self.docker, "rm", "-f", container_id],
                capture_output=True,
                check=False,
                timeout=30,
                env=self.environ,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("Removing the container %s failed: %s", container_id, exc)
            return False
        return done.returncode == 0

    def _reap_loop(self, remove_all_first: bool, started: float | None = None) -> None:
        removed = self.reap_once(remove_all=remove_all_first)
        if removed:
            logger.info("Removed %d sandbox container(s) left behind by an earlier run", removed)
        removed = self.reap_mcp_once(started_before=started)
        if removed:
            logger.info("Removed %d MCP server container(s) left behind or expired", removed)
        while not self._stop.wait(self.reap_interval):
            removed = self.reap_once()
            if removed:
                logger.info("Removed %d expired sandbox container(s)", removed)
            removed = self.reap_mcp_once()
            if removed:
                logger.info("Removed %d expired MCP server container(s)", removed)

    def close(self) -> None:
        """Stop the reaper (and further checks)."""
        self._stop.set()
        with contextlib.suppress(RuntimeError):
            if self._reaper is not None:
                self._reaper.join(1)


class DockerMonitorLike(Protocol):
    """What a workspace needs of a monitor (:class:`DockerMonitor`, or a test's stand-in)."""

    def status(self, *, refresh: bool = False, wait: float = 0.0) -> DockerStatus: ...

    def start_reaper(self, *, remove_all_first: bool) -> None: ...

    def close(self) -> None: ...


_MONITORS: dict[tuple[_DaemonKey, str], DockerMonitor] = {}


def monitor_for(
    sandbox: Callable[[], DockerSandbox],
    *,
    docker: str = "docker",
    environ: Mapping[str, str] | None = None,
    owner: str = OWNER,
) -> DockerMonitorLike:
    """The process's monitor of the daemon ``docker`` with ``environ`` reaches, for the containers of ``owner`` (one
    per daemon and owner, shared by every workspace of the process, e.g. a server's tenants); created with
    ``sandbox`` (whose containers carry ``owner``) on first use."""
    key = (daemon_key(docker, environ), owner)
    with _PROOFS_LOCK:
        monitor = _MONITORS.get(key)
        if monitor is None or monitor._stop.is_set():
            monitor = DockerMonitor(sandbox, docker=docker, environ=environ, owner=owner)
            _MONITORS[key] = monitor
        return monitor


def reset_monitors() -> None:
    """Stop and forget every monitor and proof (tests)."""
    with _PROOFS_LOCK:
        monitors = list(_MONITORS.values())
        _MONITORS.clear()
    for monitor in monitors:
        monitor.close()
    clear_proofs()


__all__ = [
    "CHECKING",
    "DOCKER_REASONS",
    "MCP_LIFETIME",
    "OWNER",
    "PREFLIGHT_TTL",
    "REAP_INTERVAL",
    "REASONS",
    "ROOTFUL_NOTE",
    "DockerMonitor",
    "DockerMonitorLike",
    "DockerStatus",
    "SandboxCheck",
    "approved_status",
    "clear_proofs",
    "daemon_proven",
    "installation_owner",
    "last_reason",
    "mcp_owner",
    "monitor_for",
    "reason_of",
    "record_proof",
    "reset_monitors",
    "revoke_proof",
    "unavailable_status",
]
