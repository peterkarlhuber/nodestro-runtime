"""The sandbox gateway: a Docker API endpoint that only lets Nodestro start locked-down tool containers.

Nodestro runs untrusted tool code (all AI-generated tools and their tests, ADR-0006) only in containers. A server
gives its workers a separate Docker daemon for that; on one computer (the ``local-container`` profile, Nodestro Local
Docker) the only daemon is the host's, and access to its socket is root on the host. The Nodestro container therefore
never gets that socket. A separate gateway process holds it and offers Nodestro a socket of its own
(``DOCKER_HOST=unix:///run/nodestro-sandbox/docker.sock``) that speaks the Docker Engine API, but answers only the
handful of requests Nodestro's sandboxes need (:mod:`nodestro_runtime.container_images`), and only in their safe
form:

- **Containers** are created from the request's image, command and working directory only. Everything that decides
  isolation is set by the gateway, whatever the request says: no network, read-only root file system, every
  capability dropped, ``no-new-privileges``, user ``65534:65534`` (``nobody``), memory, CPU and process limits no
  higher than the gateway's maxima, two small ``noexec`` tmpfs (``/tmp``, ``/work``), no bind mounts, volumes,
  devices, ports, host namespaces, privileged mode or other runtimes. A request that asks for any of these is refused
  (HTTP 403 with the reason), so a misconfigured or compromised client cannot quietly get them. The image must be one
  Nodestro builds (``nodestro-env:<key>``), and nothing is ever pulled for a container.
- **Containers can only be used by name or ID if the gateway created them** (attach, start, wait, kill, remove,
  inspect). They carry the gateway's label; at start and every few seconds the gateway removes its containers that
  outlived :attr:`GatewayConfig.max_lifetime` (cleanup after crashes), and it refuses new ones beyond
  :attr:`GatewayConfig.max_containers`.
- **Images** can only be built from exactly the Dockerfile :mod:`nodestro_runtime.container_images` writes, with a
  lock file whose every package is pinned with hashes (no options such as other indexes), with a copy of the very
  Orkexa package the gateway runs, and under exactly the tag the runtime computes for that lock. The build downloads
  wheels (no package code runs: ``--only-binary``), installs them without network access to an index and runs no
  other command. Only ``nodestro-env:<key>`` images can be inspected or removed.
- **Everything else** (other images, volumes, networks, exec, the daemon's events, swarm, plugins, BuildKit sessions,
  registry credentials) is refused.

The gateway does not run tool code and needs no network: it talks to the client and the daemon over UNIX sockets.
It is part of the image of the product that uses it (Nodestro Local Docker starts it as its own container with the
host's Docker socket, without network, capabilities or a writable root file system).

What it does not change: the containers run on the host's Docker daemon, so their isolation is Docker's (namespaces,
cgroups, seccomp, AppArmor/SELinux where the host has them). A kernel or container runtime vulnerability can break
any container; rootless Docker limits the damage of such an escape to the user who runs Docker. See the Local Docker
security model for what this means per platform.

Usage: ``python -m nodestro_runtime.sandbox_gateway --listen /run/nodestro-sandbox/docker.sock``
(``--docker`` the daemon's socket, default ``/var/run/docker.sock``; limits from ``NODESTRO_SANDBOX_*``, see
:meth:`GatewayConfig.from_env`).
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
import contextlib
from dataclasses import dataclass, field
import hashlib
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import sys
import tarfile
import time
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit
import uuid

logger = logging.getLogger("nodestro.sandbox_gateway")

#: Label of every container the gateway creates; its value is the gateway's instance.
LABEL = "dev.nodestro.sandbox"
#: The images a client may use, inspect, build and remove (see container_images.IMAGE_REPOSITORY).
IMAGE = re.compile(r"^nodestro-env:[0-9a-f]{32}$")
#: Container names a client may choose (orkexa/nodestro sandboxes use these prefixes).
NAME = re.compile(r"^(?:nodestro|orkexa)-sandbox-[0-9a-z-]{1,64}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^/v\d+\.\d+(?=/)")
_HEAD_LIMIT = 64 * 1024
_JSON_LIMIT = 1024 * 1024
_BUILD_LIMIT = 64 * 1024 * 1024
#: The tmpfs mounts a sandbox container gets (the same as StreamedSandbox asks for).
TMPFS = {
    "/tmp": "rw,noexec,nosuid,size=16m",
    "/work": "rw,noexec,nosuid,size=16m,uid=65534,gid=65534,mode=0700",
}
_PROXY_ARGS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"})
_DEFAULTS: tuple[Any, ...] = (None, "", 0, False, [], {})


class Refused(Exception):
    """A request the gateway does not forward; the message goes back to the client (HTTP ``status``)."""

    def __init__(self, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class GatewayConfig:
    """The gateway's limits; a client can ask for less, never for more."""

    #: The daemon's socket (the host's ``/var/run/docker.sock``, or a rootless daemon's).
    docker: str = "/var/run/docker.sock"
    #: Names this gateway's containers (label value), so two installations never clean up each other's.
    instance: str = "default"
    max_containers: int = 4
    max_memory_mb: int = 2048
    max_cpus: float = 2.0
    max_pids: int = 256
    #: Seconds after which a container is killed and removed (a tool call times out much earlier).
    max_lifetime: float = 600.0
    #: The network of image builds (``default``; ``host`` only where the default network cannot reach the index).
    build_network: str = "default"
    #: Concurrent image builds.
    max_builds: int = 1
    #: Python versions (``major.minor``) whose ``python:<v>-slim`` base images may be built on.
    python_versions: tuple[str, ...] = field(default_factory=lambda: (f"{sys.version_info[0]}.{sys.version_info[1]}",))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> GatewayConfig:
        """Limits from ``NODESTRO_SANDBOX_INSTANCE``, ``_MAX_CONTAINERS``, ``_MAX_MEMORY_MB``, ``_MAX_CPUS``,
        ``_MAX_PIDS``, ``_MAX_LIFETIME`` and ``_BUILD_NETWORK``; a value that is not understood stops the gateway."""
        environ = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        numbers = {
            "max_containers": int,
            "max_memory_mb": int,
            "max_cpus": float,
            "max_pids": int,
            "max_lifetime": float,
        }
        for name, kind in numbers.items():
            raw = environ.get(f"NODESTRO_SANDBOX_{name.upper()}", "").strip()
            if raw:
                try:
                    value = kind(raw)
                except ValueError:
                    raise SystemExit(f"NODESTRO_SANDBOX_{name.upper()}={raw!r} is not a number") from None
                if value <= 0:
                    raise SystemExit(f"NODESTRO_SANDBOX_{name.upper()} must be positive")
                values[name] = value
        instance = environ.get("NODESTRO_SANDBOX_INSTANCE", "").strip()
        if instance:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", instance):
                raise SystemExit(f"NODESTRO_SANDBOX_INSTANCE={instance!r} is not a valid name")
            values["instance"] = instance
        network = environ.get("NODESTRO_SANDBOX_BUILD_NETWORK", "").strip()
        if network:
            if network not in ("default", "host"):
                raise SystemExit("NODESTRO_SANDBOX_BUILD_NETWORK must be default or host")
            values["build_network"] = network
        values.update(overrides)
        return cls(**values)


# ---- the policy (pure functions, unit tested) --------------------------------------------------------------------


def _is_default(value: Any) -> bool:
    """Whether a value is what the docker CLI sends for a setting nobody asked for."""
    if isinstance(value, dict) and set(value) <= {"Name", "MaximumRetryCount"} and value:
        return value.get("Name") in ("", "no") and not value.get("MaximumRetryCount")  # RestartPolicy
    if isinstance(value, dict) and set(value) == {"Type", "Config"}:
        return value["Type"] == "" and not value["Config"]  # LogConfig
    if isinstance(value, list) and len(value) == 2 and all(item == 0 for item in value):
        return True  # ConsoleSize
    return any(value == default and type(value) is type(default) for default in _DEFAULTS)


def _split(path: str) -> tuple[str, str]:
    """The path without the API version prefix, and the prefix (``/v1.51``)."""
    match = _VERSION.match(path)
    if match is None:
        return path, ""
    return path[match.end() :], match.group(0)


def plan_create(query: dict[str, str], body: dict[str, Any], config: GatewayConfig) -> tuple[dict[str, str], bytes]:
    """The container the gateway creates for a ``POST /containers/create`` request: its query and JSON body.

    Raises :class:`Refused` for anything outside the sandbox's shape."""
    if not isinstance(body, dict):
        raise Refused("The container configuration must be a JSON object")
    allowed_query = {"name", "platform"}
    for key, value in query.items():
        if key not in allowed_query:
            raise Refused(f"The query parameter {key!r} is not allowed")
        if key == "platform" and value not in ("", "linux", "linux/amd64", "linux/arm64"):
            raise Refused(f"The platform {value!r} is not allowed")
    name = query.get("name") or f"nodestro-sandbox-{uuid.uuid4().hex[:12]}"
    if not NAME.fullmatch(name):
        raise Refused(f"The container name {name!r} is not allowed")
    image = body.get("Image")
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise Refused(f"Only Nodestro's sandbox images (nodestro-env:<key>) can be run, not {image!r}")
    config_keys = {
        "Image",
        "Cmd",
        "Entrypoint",
        "WorkingDir",
        "Env",
        "User",
        "AttachStdin",
        "AttachStdout",
        "AttachStderr",
        "OpenStdin",
        "StdinOnce",
        "Tty",
        "Labels",
        "NetworkDisabled",
        "HostConfig",
        "NetworkingConfig",
        "Hostname",
        "Domainname",
        "ExposedPorts",
        "Volumes",
        "StopSignal",
        "StopTimeout",
        "OnBuild",
        "Shell",
        "Healthcheck",
        "ArgsEscaped",
        "MacAddress",
        "Platform",
    }
    for key, value in body.items():
        if key not in config_keys and not _is_default(value):
            raise Refused(f"The container setting {key!r} is not allowed")
    for key in ("Entrypoint", "ExposedPorts", "Volumes", "OnBuild", "Shell", "Healthcheck", "MacAddress", "Domainname"):
        if not _is_default(body.get(key)):
            raise Refused(f"The container setting {key!r} is not allowed")
    if body.get("Tty"):
        raise Refused("A sandbox container has no terminal")
    if body.get("WorkingDir") not in (None, "", "/work"):
        raise Refused("A sandbox container works in /work")
    if body.get("User") not in (None, "", "65534:65534", "65534"):
        raise Refused("A sandbox container runs as 65534:65534 (nobody)")
    networking = body.get("NetworkingConfig") or {}
    if not isinstance(networking, dict) or any(not _is_default(value) for value in networking.values()):
        raise Refused("A sandbox container has no network")
    command = body.get("Cmd") or []
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command) or not command:
        raise Refused("A sandbox container needs a command (a list of strings)")
    env = body.get("Env") or []
    if not isinstance(env, list) or not all(isinstance(item, str) for item in env) or len(env) > 64:
        raise Refused("The environment must be a short list of KEY=value strings")

    host = body.get("HostConfig") or {}
    if not isinstance(host, dict):
        raise Refused("HostConfig must be a JSON object")
    # Settings a sandbox request may carry; everything else must be absent or Docker's default.
    host_allowed = {
        "NetworkMode",
        "ReadonlyRootfs",
        "CapDrop",
        "SecurityOpt",
        "PidsLimit",
        "Memory",
        "MemorySwap",
        "NanoCpus",
        "Tmpfs",
        "AutoRemove",
        "ConsoleSize",
        "MemorySwappiness",
    }
    for key, value in host.items():
        if key not in host_allowed and not _is_default(value):
            raise Refused(f"The host setting {key!r} is not allowed in a sandbox container")
    for key in ("MaskedPaths", "ReadonlyPaths"):
        if host.get(key) is not None:  # an empty list would unmask /proc and /sys paths
            raise Refused(f"The host setting {key!r} is not allowed in a sandbox container")
    if host.get("MemorySwappiness") not in (None, -1):
        raise Refused("The host setting 'MemorySwappiness' is not allowed in a sandbox container")
    if host.get("NetworkMode") not in (None, "", "none"):
        raise Refused("A sandbox container has no network (NetworkMode none)")
    if any(not isinstance(item, str) or item != "no-new-privileges" for item in host.get("SecurityOpt") or []):
        raise Refused("Only the security option no-new-privileges is allowed")
    if any(item.upper() != "ALL" for item in host.get("CapDrop") or []):
        raise Refused("A sandbox container drops every capability")
    tmpfs = host.get("Tmpfs") or {}
    if not isinstance(tmpfs, dict) or not set(tmpfs) <= set(TMPFS):
        raise Refused(f"Only these temporary file systems are allowed: {', '.join(TMPFS)}")

    max_memory = config.max_memory_mb * 1024 * 1024
    memory = int(host.get("Memory") or max_memory)
    if memory <= 0 or memory > max_memory:
        raise Refused(f"A sandbox container gets at most {config.max_memory_mb} MB of memory")
    max_nano = int(config.max_cpus * 1e9)
    nano = int(host.get("NanoCpus") or max_nano)
    if nano <= 0 or nano > max_nano:
        raise Refused(f"A sandbox container gets at most {config.max_cpus:g} CPUs")
    pids = int(host.get("PidsLimit") or config.max_pids)
    if pids <= 0 or pids > config.max_pids:
        raise Refused(f"A sandbox container gets at most {config.max_pids} processes")

    created = {
        "Image": image,
        "Cmd": command,
        "WorkingDir": "/work",
        "Env": env,
        "User": "65534:65534",
        "AttachStdin": bool(body.get("AttachStdin")),
        "AttachStdout": bool(body.get("AttachStdout")),
        "AttachStderr": bool(body.get("AttachStderr")),
        "OpenStdin": bool(body.get("OpenStdin")),
        "StdinOnce": bool(body.get("StdinOnce")),
        "Tty": False,
        "NetworkDisabled": True,
        "Labels": {LABEL: config.instance},
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Privileged": False,
            # Stated rather than left to the daemon's defaults, which an administrator can change.
            "IpcMode": "private",
            "Binds": [],
            "Mounts": [],
            "Devices": [],
            "PidsLimit": pids,
            "Memory": memory,
            "MemorySwap": memory,
            "NanoCpus": nano,
            "Tmpfs": dict(TMPFS),
            "AutoRemove": True,
            "Init": False,
            "Ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 1024}],
            "LogConfig": {"Type": "none", "Config": {}},
        },
        "NetworkingConfig": {"EndpointsConfig": {}},
    }
    return {"name": name}, json.dumps(created).encode("utf-8")


def _lock_entries(lock: str) -> list[str]:
    """The logical lines of a lock file (continuations joined, comments dropped)."""
    entries: list[str] = []
    current = ""
    for raw in lock.splitlines():
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip() and not current:
            continue
        current += " " + line.strip().rstrip("\\").strip()
        if not line.endswith("\\"):
            if current.strip():
                entries.append(current.strip())
            current = ""
    if current.strip():
        entries.append(current.strip())
    return entries


_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]*\])?==[A-Za-z0-9.+!_-]+"
    r"(?:\s*;\s*[A-Za-z0-9_ .<>=!~'\"()]+?)?"
    r"(?:\s+--hash=sha256:[0-9a-f]{64})+$"
)


def check_lock(lock: str) -> None:
    """Every entry of a lock is ``name==version`` with SHA-256 hashes; no options, URLs or other indexes."""
    entries = _lock_entries(lock)
    if not entries:
        raise Refused("The lock file is empty")
    for entry in entries:
        if not _REQUIREMENT.fullmatch(entry):
            raise Refused(f"The lock file entry {entry[:120]!r} is not an exact version with hashes")


def _orkexa_files() -> dict[str, bytes]:
    from .container_images import _orkexa_dir, _orkexa_files

    root = _orkexa_dir()
    return {str(PurePosixPath("orkexa", *path.relative_to(root).parts)): path.read_bytes() for path in _orkexa_files()}


def plan_build(query: dict[str, str], context: bytes, config: GatewayConfig) -> dict[str, str]:
    """The query of an image build the gateway forwards; ``context`` is the build's tar archive.

    Raises :class:`Refused` unless the context is exactly what :class:`ContainerImages.build
    <nodestro_runtime.container_images.ContainerImages.build>` writes for a hashed lock and its tag."""
    from .container_images import _DOCKERFILE, LOCK_LABEL, ContainerImages

    ignored = {"rm", "forcerm", "q", "nocache", "pull", "dockerfile", "version", "networkmode", "buildargs"}
    harmless_defaults = {
        "cachefrom",
        "cgroupparent",
        "cpuperiod",
        "cpuquota",
        "cpusetcpus",
        "cpusetmems",
        "cpushares",
        "labels",
        "memory",
        "memswap",
        "shmsize",
        "target",
        "ulimits",
        "platform",
        "extrahosts",
        "isolation",
        "squash",
        "outputs",
        "remote",
        "session",
        "t",
    }
    for key, value in query.items():
        if key in ignored or key == "t":
            continue
        if key not in harmless_defaults:
            raise Refused(f"The build parameter {key!r} is not allowed")
        if value not in ("", "0", "false", "null", "[]", "{}"):
            raise Refused(f"The build parameter {key!r} is not allowed")
    if query.get("version", "1") not in ("", "1"):
        raise Refused("Only the classic builder is allowed (no BuildKit sessions)")
    if query.get("dockerfile", "Dockerfile") not in ("", "Dockerfile"):
        raise Refused("The Dockerfile must be called Dockerfile")
    if query.get("networkmode", "default") not in ("", "default", config.build_network):
        raise Refused(f"Builds use the {config.build_network!r} network")
    try:
        build_args = json.loads(query.get("buildargs") or "{}")
    except ValueError:
        raise Refused("buildargs is not JSON") from None
    if not isinstance(build_args, dict) or not set(build_args) <= _PROXY_ARGS:
        raise Refused("Only proxy settings may be passed as build arguments")
    if not all(isinstance(value, str) and "\n" not in value for value in build_args.values()):
        raise Refused("Build arguments must be single-line text")

    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(context), mode="r:*") as archive:
            for member in archive:
                name = str(PurePosixPath(member.name.lstrip("./") if member.name != "." else "."))
                if member.isdir():
                    continue
                if not member.isreg():
                    raise Refused(f"The build context may contain only files and folders, not {member.name!r}")
                if name.startswith("/") or ".." in PurePosixPath(name).parts:
                    raise Refused(f"The build context path {member.name!r} is not allowed")
                extracted = archive.extractfile(member)
                files[name] = extracted.read() if extracted is not None else b""
    except tarfile.TarError as exc:
        raise Refused(f"The build context is not a tar archive: {exc}") from None
    lock = files.pop("requirements.txt", None)
    dockerfile = files.pop("Dockerfile", None)
    ca = files.pop("ca.pem", None)
    if lock is None or dockerfile is None:
        raise Refused("The build context needs a Dockerfile and requirements.txt")
    lock_text = lock.decode("utf-8", "replace")
    check_lock(lock_text)
    if ca is not None and not ca.lstrip().startswith(b"-----BEGIN CERTIFICATE-----"):
        raise Refused("ca.pem must be a PEM certificate bundle")
    if files != _orkexa_files():
        raise Refused("The build context must contain exactly the Orkexa package the gateway runs")
    tag = query.get("t", "")
    for version in config.python_versions:
        base = f"python:{version}-slim"
        expected = _DOCKERFILE.format(
            base=base,
            label=LOCK_LABEL,
            lock_sha=hashlib.sha256(lock_text.encode("utf-8")).hexdigest(),
            ca="COPY ca.pem /build/ca.pem\nENV PIP_CERT=/build/ca.pem\n" if ca is not None else "",
        )
        if dockerfile.decode("utf-8", "replace") != expected:
            continue
        images = ContainerImages(base=lambda _version, base=base: base)  # type: ignore[misc]
        if tag != images.tag(lock_text, version):
            raise Refused(f"The image tag {tag!r} does not belong to this lock file")
        forwarded = {"t": tag, "rm": "1", "forcerm": "1", "version": "1", "dockerfile": "Dockerfile"}
        # The build's own containers get the same memory and CPU limits as sandbox containers.
        memory = str(config.max_memory_mb * 1024 * 1024)
        forwarded.update(memory=memory, memswap=memory, cpuperiod="100000", cpuquota=str(int(config.max_cpus * 1e5)))
        forwarded["networkmode"] = config.build_network
        if build_args:
            forwarded["buildargs"] = json.dumps(build_args)
        return forwarded
    raise Refused("Only Nodestro's own Dockerfile for sandbox images can be built")


def route(method: str, path: str) -> tuple[str, str]:
    """The kind of request (``ping``, ``create``, ``container:<action>``, ``image:<action>``, ``build``) and the
    container or image it names; raises :class:`Refused` for every other endpoint."""
    bare, _ = _split(path)
    if bare == "/_ping" and method in ("GET", "HEAD"):
        return "ping", ""
    if bare in ("/version", "/info") and method == "GET":
        return "ping", ""
    if bare == "/containers/create" and method == "POST":
        return "create", ""
    if bare == "/build" and method == "POST":
        return "build", ""
    match = re.fullmatch(r"/containers/([^/]+)(/[a-z]+)?", bare)
    if match is not None:
        ref, action = unquote(match.group(1)), (match.group(2) or "")[1:]
        actions = {
            ("POST", "attach"),
            ("POST", "start"),
            ("POST", "wait"),
            ("POST", "kill"),
            ("POST", "resize"),
            ("GET", "json"),
            ("DELETE", ""),
        }
        if (method, action) in actions:
            return f"container:{action or 'delete'}", ref
    match = re.fullmatch(r"/images/(.+?)(/json)?", bare)
    if match is not None:
        ref = unquote(match.group(1))
        if (method == "GET" and match.group(2)) or (method == "DELETE" and not match.group(2)):
            if not IMAGE.fullmatch(ref):
                raise Refused(f"Only Nodestro's sandbox images can be inspected or removed, not {ref!r}", 404)
            return f"image:{'inspect' if method == 'GET' else 'delete'}", ref
    raise Refused(f"{method} {bare} is not available through the Nodestro sandbox gateway")


# ---- HTTP plumbing ----------------------------------------------------------------------------------------------


@dataclass
class Request:
    method: str
    target: str
    headers: list[tuple[str, str]]
    body: bytes

    @property
    def path(self) -> str:
        return urlsplit(self.target).path

    @property
    def query(self) -> dict[str, str]:
        return dict(parse_qsl(urlsplit(self.target).query, keep_blank_values=True))

    def header(self, name: str) -> str:
        for key, value in self.headers:
            if key.lower() == name:
                return value
        return ""

    @property
    def upgrade(self) -> bool:
        return "upgrade" in self.header("connection").lower() and bool(self.header("upgrade"))


_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "DELETE"})
#: Visible ASCII without the fragment mark: what an origin-form request target may contain.
_TARGET = re.compile(r"^/[\x21-\x22\x24-\x7e]*$")
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
#: Control characters (tab is allowed in header values). A bare line feed would let a client end a header early
#: and smuggle a second request to the daemon, so the head is refused instead of passed on.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def parse_head(head: bytes) -> Request:
    """The request line and headers of one request head (ending in an empty line), checked strictly.

    Raises :class:`Refused` (400) for anything a lenient parser could read differently: bare CR or LF, control
    characters, folded headers, an unknown method, a target that is not a plain path and query."""
    text = head.decode("latin-1")
    if not text.endswith("\r\n\r\n"):
        raise Refused("Malformed request head", 400)
    lines = text[:-4].split("\r\n")
    if any(_CONTROL.search(line.replace("\t", " ")) for line in lines):
        raise Refused("Control characters are not allowed in a request head", 400)
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[0] not in _METHODS or parts[2] not in ("HTTP/1.1", "HTTP/1.0"):
        raise Refused("Malformed request line", 400)
    method, target, _version = parts
    if not _TARGET.fullmatch(target):
        raise Refused("The request target must be a plain path and query", 400)
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if not sep or not _TOKEN.fullmatch(key):
            raise Refused("Malformed header", 400)
        headers.append((key, value.strip(" \t")))
    return Request(method, target, headers, b"")


async def read_head(reader: asyncio.StreamReader) -> Request | None:
    """One request head (``None`` when the client closed without sending one)."""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise Refused("Incomplete request", 400) from None
    except asyncio.LimitOverrunError:
        raise Refused("Request head too large", 431) from None
    return parse_head(head)


async def read_request(reader: asyncio.StreamReader, *, limit: int) -> Request | None:
    """One HTTP/1.1 request head and its body (``Content-Length`` or chunked, at most ``limit`` bytes)."""
    request = await read_head(reader)
    if request is None:
        return None
    await read_body(reader, request, limit=limit)
    return request


async def read_body(reader: asyncio.StreamReader, request: Request, *, limit: int) -> None:
    """The body of ``request`` (``Content-Length`` or chunked, at most ``limit`` bytes)."""
    if request.header("transfer-encoding") and request.header("content-length"):
        raise Refused("Content-Length and Transfer-Encoding together are not allowed", 400)
    encoding = request.header("transfer-encoding").lower()
    if encoding not in ("", "chunked"):
        raise Refused("Only chunked transfer encoding is supported", 400)
    if encoding == "chunked":
        body = bytearray()
        while True:
            size_line = await reader.readuntil(b"\r\n")
            try:
                size = int(size_line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise Refused("Malformed chunk", 400) from None
            if size == 0:
                while (await reader.readuntil(b"\r\n")) != b"\r\n":
                    pass
                break
            if len(body) + size > limit:
                raise Refused(f"The request body exceeds {limit // (1024 * 1024)} MB", 413)
            body += await reader.readexactly(size)
            await reader.readexactly(2)
        request.body = bytes(body)
    elif request.header("content-length"):
        if not request.header("content-length").isdigit():
            raise Refused("Malformed Content-Length", 400)
        length = int(request.header("content-length"))
        if length < 0 or length > limit:
            raise Refused(f"The request body exceeds {max(limit // (1024 * 1024), 1)} MB", 413)
        request.body = await reader.readexactly(length)


def encode_request(request: Request, target: str, body: bytes, *, upgrade: bool) -> bytes:
    keep = {"user-agent", "content-type", "accept"}
    lines = [f"{request.method} {target} HTTP/1.1", "Host: docker"]
    lines += [f"{key}: {value}" for key, value in request.headers if key.lower() in keep]
    if body or request.method in ("POST", "PUT"):
        lines.append(f"Content-Length: {len(body)}")
    lines += ["Connection: Upgrade", "Upgrade: tcp"] if upgrade else ["Connection: close"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def refusal(status: int, message: str) -> bytes:
    body = json.dumps({"message": f"Nodestro sandbox gateway: {message}"}).encode("utf-8")
    reason = {400: "Bad Request", 403: "Forbidden", 404: "Not Found", 413: "Payload Too Large", 429: "Too Many"}
    return (
        f"HTTP/1.1 {status} {reason.get(status, 'Refused')}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode("latin-1") + body


def parse_response(raw: bytes) -> tuple[int, bytes]:
    """Status and (de-chunked) body of a complete HTTP response."""
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    if any(line.lower().startswith("transfer-encoding:") and "chunked" in line.lower() for line in lines[1:]):
        out = bytearray()
        rest = body
        while rest:
            size_line, _, rest = rest.partition(b"\r\n")
            size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                break
            out += rest[:size]
            rest = rest[size + 2 :]
        body = bytes(out)
    return status, body


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


# ---- the gateway ------------------------------------------------------------------------------------------------


class Gateway:
    """Serves the policy above on a UNIX socket and forwards what it allows to the Docker daemon."""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        #: Containers this gateway created: ID -> name (and name -> ID), until they are gone.
        self.containers: dict[str, str] = {}
        self._builds = asyncio.Semaphore(config.max_builds)
        self._creating = asyncio.Lock()

    # -- daemon access --

    async def _daemon(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_unix_connection(self.config.docker, limit=_HEAD_LIMIT * 4)

    async def call(self, method: str, target: str, body: Any = None) -> tuple[int, Any]:
        """One request of the gateway itself to the daemon; returns the status and the decoded JSON body."""
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        request = Request(method, target, [("Content-Type", "application/json")] if body is not None else [], data)
        reader, writer = await self._daemon()
        try:
            writer.write(encode_request(request, target, data, upgrade=False))
            await writer.drain()
            raw = await reader.read()
        finally:
            writer.close()
        status, payload = parse_response(raw)
        try:
            return status, json.loads(payload) if payload.strip() else None
        except ValueError:
            return status, None

    # -- bookkeeping --

    def _known(self, ref: str) -> str | None:
        """The ID of a container this gateway created, by ID or name."""
        if ref in self.containers:
            return ref
        for container_id, name in self.containers.items():
            if ref in (name, f"/{name}"):
                return container_id
        return None

    async def reconcile(self, *, remove_all: bool = False) -> None:
        """Forget containers that are gone; kill and remove those older than the limit (all of them at start)."""
        filters = quote(json.dumps({"label": [f"{LABEL}={self.config.instance}"]}))
        status, listed = await self.call("GET", f"/containers/json?all=1&filters={filters}")
        if status != 200 or not isinstance(listed, list):
            logger.warning("Listing the sandbox containers failed (HTTP %s)", status)
            return
        now = time.time()
        present: dict[str, str] = {}
        for item in listed:
            container_id = str(item.get("Id", ""))
            names = item.get("Names") or [""]
            name = str(names[0]).lstrip("/")
            expired = now - float(item.get("Created") or now) > self.config.max_lifetime
            if remove_all or expired:
                logger.info("Removing sandbox container %s (%s)", name, "start" if remove_all else "expired")
                await self.call("DELETE", f"/containers/{container_id}?force=1")
                continue
            present[container_id] = name
        known = dict(self.containers)
        self.containers = {key: known.get(key, present[key]) for key in present}

    async def reap(self, stop: asyncio.Event, interval: float = 10.0) -> None:
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
            if stop.is_set():
                break
            try:
                await self.reconcile()
            except (OSError, ValueError) as exc:
                logger.warning("Sandbox cleanup failed: %s", exc)

    # -- requests --

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._handle(reader, writer)
        except Refused as refused:
            logger.info("Refused: %s", refused)
            with contextlib.suppress(ConnectionError, OSError):
                writer.write(refusal(refused.status, str(refused)))
                await writer.drain()
        except (ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
            logger.debug("Connection ended: %s", exc)
        except Exception:
            logger.exception("Unexpected gateway error")
            with contextlib.suppress(ConnectionError, OSError):
                writer.write(refusal(400, "unexpected error"))
        finally:
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # The route is decided on the head; the body limit depends on it (an image build sends its context).
        request = await read_head(reader)
        if request is None:
            return
        kind, ref = route(request.method, request.path)
        bare, prefix = _split(request.path)
        await read_body(reader, request, limit=_BUILD_LIMIT if kind == "build" else _JSON_LIMIT)
        # Every target sent to the daemon is rebuilt from what was checked, never passed through as written.
        target, body = f"{prefix}{bare}", request.body
        if request.header("x-registry-auth") or (request.header("x-registry-config") not in ("", "e30=", "null")):
            logger.debug("Registry credentials of the client are dropped")
        if kind != "build" and len(body) > _JSON_LIMIT:
            raise Refused("The request body is too large", 413)
        if kind == "create":
            try:
                parsed = json.loads(body or b"{}")
            except ValueError:
                raise Refused("The container configuration is not JSON", 400) from None
            logger.debug("create %s", json.dumps(parsed)[:4000])
            query, body = plan_create(request.query, parsed, self.config)
            target = f"{prefix}/containers/create?{urlencode(query)}"
            async with self._creating:
                if len(self.containers) >= self.config.max_containers:
                    await self.reconcile()
                if len(self.containers) >= self.config.max_containers:
                    raise Refused(f"At most {self.config.max_containers} sandbox containers run at once", 429)
                await self._create(request, target, body, query["name"], writer)
            return
        if kind == "build":
            query = plan_build(request.query, body, self.config)
            target = f"{prefix}/build?{urlencode(query)}"
            request.headers = [(key, value) for key, value in request.headers if key.lower() != "content-type"]
            request.headers.append(("Content-Type", "application/x-tar"))
            async with self._builds:
                await self._forward(request, target, body, reader, writer)
            return
        if kind.startswith("container:"):
            container_id = self._known(ref)
            if container_id is None:
                raise Refused(f"The container {ref!r} was not started by the Nodestro sandbox gateway", 404)
            action = kind.split(":", 1)[1]
            query = request.query
            allowed = {
                "attach": {"stream", "stdin", "stdout", "stderr", "logs", "detachKeys"},
                "start": {"detachKeys"},
                "wait": {"condition"},
                "kill": {"signal"},
                "resize": {"h", "w"},
                "json": {"size"},
                "delete": {"force", "v", "link"},
            }[action]
            if not set(query) <= allowed or (action == "attach" and query.get("detachKeys")):
                raise Refused(f"Parameters {sorted(set(query) - allowed)} are not allowed")
            if action == "delete" and query.get("link") not in (None, "", "0", "false"):
                raise Refused("Removing links is not allowed")
            path = f"{prefix}/containers/{container_id}" + ("" if action == "delete" else f"/{action}")
            target = path + (f"?{urlencode(query)}" if query else "")
            await self._forward(request, target, b"", reader, writer)
            if action == "delete":
                self.containers.pop(container_id, None)
            return
        if kind.startswith("image:"):
            query = request.query
            if query and not set(query) <= {"force", "noprune"}:
                raise Refused("These parameters are not allowed")
            path = f"{prefix}/images/{quote(ref, safe=':')}" + ("/json" if kind == "image:inspect" else "")
            target = path + (f"?{urlencode(query)}" if query else "")
            await self._forward(request, target, b"", reader, writer)
            return
        await self._forward(request, target, b"", reader, writer)  # ping, version, info: the bare path, no query

    async def _create(
        self, request: Request, target: str, body: bytes, name: str, writer: asyncio.StreamWriter
    ) -> None:
        daemon_reader, daemon_writer = await self._daemon()
        try:
            daemon_writer.write(encode_request(request, target, body, upgrade=False))
            await daemon_writer.drain()
            raw = await daemon_reader.read()
        finally:
            daemon_writer.close()
        status, payload = parse_response(raw)
        if status == 201:
            try:
                container_id = str(json.loads(payload)["Id"])
            except (ValueError, KeyError, TypeError):
                container_id = ""
            if _CONTAINER_ID.fullmatch(container_id):
                self.containers[container_id] = name
        writer.write(raw)
        await writer.drain()

    async def _forward(
        self,
        request: Request,
        target: str,
        body: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        daemon_reader, daemon_writer = await self._daemon()
        upgrade = request.upgrade and target.split("?", 1)[0].endswith("/attach")
        try:
            daemon_writer.write(encode_request(request, target, body, upgrade=upgrade))
            await daemon_writer.drain()
            if upgrade:
                # Only a switched connection carries the client's raw bytes on. On any other answer the daemon may
                # keep the connection open for further requests, which must never reach it unchecked.
                try:
                    head = await daemon_reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    return
                writer.write(head)
                await writer.drain()
                if head.split(b" ", 2)[1:2] != [b"101"]:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(_pipe(daemon_reader, writer), timeout=10)
                    return
                await asyncio.gather(_pipe(daemon_reader, writer), _pipe(reader, daemon_writer))
            else:
                await _pipe(daemon_reader, writer)
        finally:
            daemon_writer.close()


async def serve(listen: str, config: GatewayConfig, *, ready: asyncio.Event | None = None) -> None:
    """Serve until SIGTERM/SIGINT (or until the task is cancelled)."""
    gateway = Gateway(config)
    socket_path = Path(listen)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        if stat.S_ISSOCK(socket_path.lstat().st_mode):
            socket_path.unlink()
    try:
        await gateway.reconcile(remove_all=True)
    except OSError as exc:
        raise SystemExit(f"The Docker daemon is not reachable at {config.docker}: {exc}") from None
    old = os.umask(0o117)  # the socket: rw for its owner and group only
    try:
        server = await asyncio.start_unix_server(gateway.handle, path=str(socket_path), limit=_HEAD_LIMIT)
    finally:
        os.umask(old)
    group = os.environ.get("NODESTRO_SANDBOX_SOCKET_GID", "").strip()
    if group:
        os.chown(socket_path, -1, int(group))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, stop.set)
    reaper = asyncio.create_task(gateway.reap(stop))
    logger.info(
        "Nodestro sandbox gateway listening on %s (daemon %s, instance %s)", listen, config.docker, config.instance
    )
    if ready is not None:
        ready.set()
    try:
        async with server:
            await stop.wait()
    finally:
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper
        with contextlib.suppress(OSError):
            await gateway.reconcile(remove_all=True)
        with contextlib.suppress(FileNotFoundError):
            socket_path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m nodestro_runtime.sandbox_gateway", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--listen", required=True, help="Path of the UNIX socket to offer to Nodestro")
    parser.add_argument("--docker", default=os.environ.get("NODESTRO_SANDBOX_DOCKER", "/var/run/docker.sock"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s")
    config = GatewayConfig.from_env(docker=args.docker)
    asyncio.run(serve(args.listen, config))
    return 0


__all__ = [
    "IMAGE",
    "LABEL",
    "TMPFS",
    "Gateway",
    "GatewayConfig",
    "Refused",
    "check_lock",
    "main",
    "plan_build",
    "plan_create",
    "route",
    "serve",
]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
