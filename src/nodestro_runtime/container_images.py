"""Container images for tools with third-party libraries (ADR-0006 point 6, plan step PR-11).

An approved environment (:mod:`nodestro_runtime.python_envs`) is a lock file: exact versions with the hashes of every
distribution. For the Docker sandbox it becomes an image ``nodestro-env:<key>``, where the key is the SHA-256 of the
lock, the Python version and the base image, so the same lock always means the same image and a changed lock never
reuses an old one. Images are a cache: deleting one only costs a rebuild.

The image is built in two stages:

1. **download** (the only step with network): ``pip download --require-hashes --only-binary=:all: --no-deps`` fetches
   exactly the locked wheels and checks every hash. No package code runs (no source builds); a package without a
   wheel for the container's platform fails the build (``deps.wheels_required``).
2. **install** (``--network none`` is not needed: nothing is fetched): ``pip install --no-index --require-hashes
   --no-deps`` installs the downloaded wheels into the final image, which keeps no download cache, no certificate and
   no proxy setting, and runs as ``nobody``.

Tool calls then run in that image with the flags of Orkexa's :class:`~orkexa.dynamic.DockerSandbox` (no network,
read-only file system, no capabilities, ``nobody``, memory/CPU/process limits). If Docker or the image is missing, the
call fails (``sandbox.unavailable``); there is never a fallback to a host subprocess.

Proxies and an extra certificate authority for the download stage come from the host: ``HTTP(S)_PROXY`` /
``NO_PROXY`` are passed as build arguments, and the file in ``NODESTRO_BUILD_CA_FILE`` (or ``SSL_CERT_FILE``) is
copied into the download stage only. ``NODESTRO_DOCKER_BUILD_NETWORK`` chooses the build network (default: Docker's).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

from orkexa.dynamic import DockerSandbox

from .problems import NodestroError

#: The repository of environment images (the tag is the image key).
IMAGE_REPOSITORY = "nodestro-env"
#: The label that names the lock an image was built from.
LOCK_LABEL = "org.nodestro.lock-sha256"
_PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
_HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")
_BUILD_TIMEOUT = 1800.0

_DOCKERFILE = """\
# Built by Nodestro from an approved lock file (nodestro_runtime.container_images).
FROM {base} AS download
COPY requirements.txt /build/requirements.txt
{ca}RUN python -m pip download --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: \\
        --no-deps -r /build/requirements.txt -d /build/wheels

FROM {base}
COPY --from=download /build /build
RUN python -m pip install --disable-pip-version-check --no-cache-dir --no-index --require-hashes --no-deps \\
        --find-links /build/wheels -r /build/requirements.txt \\
    && rm -rf /build /root/.cache
LABEL {label}="{lock_sha}"
USER 65534:65534
WORKDIR /work
"""


def lock_has_hashes(lock: str) -> bool:
    """Whether every package line of a lock carries at least one SHA-256 hash (needed for ``--require-hashes``)."""
    entries = _entries(lock)
    return bool(entries) and all(_HASH.search(entry) for entry in entries)


def _entries(lock: str) -> list[str]:
    """The logical package lines of a lock (continuation lines joined, comments dropped)."""
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
    return [entry for entry in entries if not entry.startswith("-")]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ContainerImages:
    """Builds and finds the image of an environment; ``docker`` is the CLI (tests pass a stand-in)."""

    def __init__(
        self,
        *,
        docker: str = "docker",
        base: Callable[[str], str] = lambda python_version: f"python:{python_version}-slim",
        environ: Mapping[str, str] | None = None,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.docker = docker
        self.base = base
        self.environ = os.environ if environ is None else environ
        self._run = run

    # ---- names --------------------------------------------------------------------------------

    def tag(self, lock: str, python_version: str) -> str:
        key = _sha(json.dumps([_sha(lock), python_version, self.base(python_version)]))
        return f"{IMAGE_REPOSITORY}:{key[:32]}"

    # ---- docker -------------------------------------------------------------------------------

    def available(self) -> bool:
        return DockerSandbox.available(self.docker)

    def _docker(self, args: list[str], *, timeout: float = 60.0, log: Callable[[str], None] | None = None) -> str:
        if shutil.which(self.docker) is None and not os.path.isabs(self.docker):
            raise NodestroError("sandbox.unavailable", "Docker is not installed; tools with libraries need it")
        try:
            completed = self._run(
                [self.docker, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=self._cli_environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise NodestroError("deps.image_failed", f"docker {args[0]} timed out") from exc
        except OSError as exc:
            raise NodestroError("sandbox.unavailable", f"Cannot start docker: {exc}") from exc
        output = (completed.stdout or "") + (completed.stderr or "")
        if log is not None:
            for line in output.splitlines()[-200:]:
                log(line)
        if completed.returncode != 0:
            raise _DockerFailed(output.strip()[-2000:] or f"exit code {completed.returncode}")
        return completed.stdout or ""

    def _cli_environment(self) -> dict[str, str]:
        """The docker CLI gets the variables it needs to reach its daemon, nothing else (no provider keys)."""
        keep = (
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONFIG",
            "DOCKER_CONTEXT",
            "DOCKER_CERT_PATH",
            "DOCKER_TLS_VERIFY",
        )
        return {key: value for key, value in self.environ.items() if key in keep or key in _PROXY_VARIABLES}

    def exists(self, tag: str) -> bool:
        try:
            self._docker(["image", "inspect", "--format", "{{.Id}}", tag])
        except _DockerFailed:
            return False
        return True

    def lock_of(self, tag: str) -> str | None:
        """The lock hash an existing image was built from (``None``: no such image)."""
        try:
            output = self._docker(["image", "inspect", "--format", f'{{{{index .Config.Labels "{LOCK_LABEL}"}}}}', tag])
        except _DockerFailed:
            return None
        return output.strip() or None

    def build(self, lock: str, python_version: str, *, log: Callable[[str], None] | None = None) -> str:
        """Build (or find) the image of a lock; returns its tag. Refuses a lock without hashes."""
        if not lock_has_hashes(lock):
            raise NodestroError(
                "deps.lock_without_hashes",
                "This environment was approved before container images existed; approve it again to use it in the "
                "Docker sandbox",
            )
        tag = self.tag(lock, python_version)
        if self.lock_of(tag) == _sha(lock):
            return tag
        with tempfile.TemporaryDirectory(prefix="nodestro-image-") as directory:
            context = Path(directory)
            (context / "requirements.txt").write_text(lock, encoding="utf-8")
            ca = self._certificate_authority()
            if ca is not None:
                shutil.copyfile(ca, context / "ca.pem")
            (context / "Dockerfile").write_text(
                _DOCKERFILE.format(
                    base=self.base(python_version),
                    label=LOCK_LABEL,
                    lock_sha=_sha(lock),
                    ca="COPY ca.pem /build/ca.pem\nENV PIP_CERT=/build/ca.pem\n" if ca is not None else "",
                ),
                encoding="utf-8",
            )
            args = ["build", "-t", tag]
            network = self.environ.get("NODESTRO_DOCKER_BUILD_NETWORK")
            if network:
                args += ["--network", network]
            for name in _PROXY_VARIABLES:
                if self.environ.get(name):
                    args += ["--build-arg", f"{name}={self.environ[name]}"]
            try:
                self._docker([*args, str(context)], timeout=_BUILD_TIMEOUT, log=log)
            except _DockerFailed as exc:
                raise NodestroError(*_explain(exc.output)) from None
        return tag

    def _certificate_authority(self) -> Path | None:
        for name in ("NODESTRO_BUILD_CA_FILE", "SSL_CERT_FILE"):
            value = self.environ.get(name)
            if value and Path(value).is_file():
                return Path(value)
        return None

    def missing_modules(self, tag: str, modules: Iterable[str]) -> list[str]:
        """The modules that cannot be found in the image (checked without network, as ``nobody``)."""
        names = sorted({name for name in modules if name})
        script = (
            "import importlib.util, json, sys; "
            "print(json.dumps([n for n in json.loads(sys.argv[1]) if importlib.util.find_spec(n) is None]))"
        )
        output = self._docker(
            [
                *("run", "--rm", "--pull", "never", "--network", "none", "--read-only", tag),
                *("python", "-I", "-c", script, json.dumps(names)),
            ],
            timeout=120,
        )
        try:
            found: Any = json.loads(output.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return names
        return [str(item) for item in found]

    def remove(self, tag: str) -> None:
        with contextlib.suppress(_DockerFailed, NodestroError):
            self._docker(["image", "rm", tag])

    def sandbox(self, tag: str, *, timeout: float = 60.0, memory_mb: int = 2048) -> DockerSandbox:
        """A sandbox whose calls run in the image ``tag`` (Orkexa's container flags, no network, never pulled)."""
        return LocalImageSandbox(image=tag, timeout=timeout, memory_mb=memory_mb, docker=self.docker)


class LocalImageSandbox(DockerSandbox):
    """Orkexa's Docker sandbox for a locally built image: ``docker run --pull never``.

    Without it, a missing ``nodestro-env:<key>`` image would make Docker look for one with that name in a registry,
    and run whatever it found there."""

    def command(self, workdir: Path, container_name: str) -> list[str]:
        command = super().command(workdir, container_name)
        position = command.index("run") + 1
        return [*command[:position], "--pull", "never", *command[position:]]


class _DockerFailed(Exception):
    def __init__(self, output: str) -> None:
        super().__init__(output)
        self.output = output


def _explain(output: str) -> tuple[str, str]:
    lowered = output.lower()
    if "no matching distribution" in lowered or "could not find a version" in lowered:
        return (
            "deps.wheels_required",
            "A package has no ready-made wheel for the container (Linux); packages are not built from source there",
        )
    if "hashes are required" in lowered or "do not match the hashes" in lowered:
        return (
            "deps.hash_mismatch",
            "A downloaded package did not match the approved lock file; nothing was installed",
        )
    if "cannot connect to the docker daemon" in lowered or "is the docker daemon running" in lowered:
        return ("sandbox.unavailable", "Docker is not running")
    return ("deps.image_failed", f"Building the container image failed: {output[-500:]}")


__all__ = ["IMAGE_REPOSITORY", "LOCK_LABEL", "ContainerImages", "LocalImageSandbox", "lock_has_hashes"]
