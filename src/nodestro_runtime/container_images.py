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
import uuid

from orkexa.dynamic import DockerSandbox, SandboxError, response_limit, run_bounded

from .problems import NodestroError

#: The repository of environment images (the tag is the image key).
IMAGE_REPOSITORY = "nodestro-env"
#: The label that names the lock an image was built from.
LOCK_LABEL = "org.nodestro.lock-sha256"
_PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
_HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")
#: Base images of environment images, pinned by the digest of their multi-platform index (amd64 and arm64), so a
#: build is reproducible and a changed upstream tag cannot slip into it. Update them together, on purpose, with
#: ``docker buildx imagetools inspect python:<v>-slim``; every update gives every environment a new image tag.
BASE_IMAGES: dict[str, str] = {
    "3.10": "python:3.10-slim@sha256:6ff506466f8b1e981719b468cc0023b8ef000b983cdec0eafe119d01f379e31d",
    "3.11": "python:3.11-slim@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60",
    "3.12": "python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159",
    "3.13": "python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb",
    "3.14": "python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8",
}
_BUILD_TIMEOUT = 1800.0


def default_base(python_version: str) -> str:
    """The pinned base image of a Python version (:data:`BASE_IMAGES`); other versions fall back to the plain tag."""
    return BASE_IMAGES.get(python_version, f"python:{python_version}-slim")


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
COPY orkexa /opt/orkexa/orkexa
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
        base: Callable[[str], str] = default_base,
        environ: Mapping[str, str] | None = None,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.docker = docker
        self.base = base
        self.environ = os.environ if environ is None else environ
        self._run = run

    # ---- names --------------------------------------------------------------------------------

    def tag(self, lock: str, python_version: str) -> str:
        key = _sha(json.dumps([_sha(lock), python_version, self.base(python_version), orkexa_fingerprint()]))
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
            # 0 selects the classic builder (a sandbox gateway accepts no BuildKit sessions).
            "DOCKER_BUILDKIT",
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
            _copy_orkexa(context / "orkexa")
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

    def sandbox(self, tag: str, *, timeout: float = 60.0, memory_mb: int = 2048) -> StreamedSandbox:
        """A sandbox whose calls run in the image ``tag`` (Orkexa's container flags, no network, never pulled)."""
        return StreamedSandbox(
            image=tag, timeout=timeout, memory_mb=memory_mb, docker=self.docker, environ=self._cli_environment()
        )


def sandbox_run_args(
    docker: str, container_name: str, image: str, *, memory_mb: int, cpus: float, pids_limit: int
) -> list[str]:
    """``docker run`` up to the image for a container of Nodestro's sandbox shape (the command follows).

    No network, read-only root file system, no capabilities, no new privileges, user ``nobody``, memory/CPU/process
    limits, small ``noexec`` temporary file systems, stdin attached, removed on exit and never pulled. The sandbox
    gateway accepts exactly this shape (:func:`~nodestro_runtime.sandbox_gateway.plan_create`)."""
    return [
        docker,
        "run",
        "--rm",
        "-i",
        "--pull",
        "never",
        "--name",
        container_name,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "65534:65534",
        "--pids-limit",
        str(pids_limit),
        "--memory",
        f"{memory_mb}m",
        "--memory-swap",
        f"{memory_mb}m",
        "--cpus",
        str(cpus),
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",
        "--tmpfs",
        "/work:rw,noexec,nosuid,size=16m,uid=65534,gid=65534,mode=0700",
        "-w",
        "/work",
        image,
    ]


#: Runs first in the container: writes the tool files into the tmpfs ``/work`` and starts Orkexa's runner there.
_BOOTSTRAP = (
    "import io, json, os, runpy, sys\n"
    "envelope = json.loads(sys.stdin.read())\n"
    "for name, content in envelope['files'].items():\n"
    "    if not name or '/' in name or name.startswith('.'):\n"
    "        raise SystemExit('invalid file name')\n"
    "    with open(os.path.join('/work', name), 'w', encoding='utf-8') as handle:\n"
    "        handle.write(content)\n"
    "os.chdir('/work')\n"
    "runner = '/opt/orkexa/orkexa/dynamic/_sandbox_runner.py'\n"
    "sys.stdin = io.StringIO(envelope['request'])\n"
    "sys.argv = [runner]\n"
    "sys.path.insert(0, os.path.dirname(runner))\n"
    "runpy.run_path(runner, run_name='__main__')\n"
)


class StreamedSandbox(DockerSandbox):
    """Orkexa's Docker sandbox for an image Nodestro built, without bind mounts.

    Orkexa's :class:`~orkexa.dynamic.DockerSandbox` mounts the call's work directory and its own package from the host
    into the container. That needs the Docker daemon to see the same file system as Nodestro, which is not the case
    for a daemon on another host or in another container (a server's workers). Here the image already contains
    Orkexa (``/opt/orkexa/orkexa``, copied in at build time), and the tool's files travel with the request on stdin
    into a ``tmpfs`` work directory. The container flags are otherwise Orkexa's: no network, read-only root file
    system, no capabilities, ``nobody``, memory/CPU/process limits; plus ``--pull never``, so a missing image is
    never looked for in a registry (and whatever was found there never runs). The docker CLI gets only the variables
    it needs to reach its daemon.
    """

    def __init__(self, *, environ: Mapping[str, str] | None = None, **options: Any) -> None:
        super().__init__(**options)
        self.environ = dict(environ) if environ is not None else None

    def command(self, workdir: Path, container_name: str) -> list[str]:
        return [
            *sandbox_run_args(
                self.docker,
                container_name,
                self.image,
                memory_mb=self.memory_mb,
                cpus=self.cpus,
                pids_limit=self.pids_limit,
            ),
            "python",
            "-I",
            "-B",
            "-c",
            _BOOTSTRAP,
        ]

    def _execute(self, workdir: Path, request: str) -> str:
        files = {path.name: path.read_text(encoding="utf-8") for path in sorted(workdir.iterdir()) if path.is_file()}
        envelope = json.dumps({"files": files, "request": request})
        name = f"nodestro-sandbox-{uuid.uuid4().hex[:12]}"

        def remove() -> None:
            # Killing the docker client does not stop the container.
            with contextlib.suppress(OSError):
                subprocess.run([self.docker, "rm", "-f", name], capture_output=True, check=False, env=self.environ)

        try:
            # Bounded: tool code can write to the container's stdout directly, past the runner's limit.
            code, output = run_bounded(
                self.command(workdir, name),
                envelope.encode("utf-8"),
                timeout=self.timeout,
                max_stdout=response_limit(self.max_output_chars),
                env=self.environ,
                on_abort=remove,
            )
        except OSError as exc:
            raise SandboxError(f"Cannot start docker: {exc}") from exc
        stdout = output.stdout.decode("utf-8", "replace")
        if not stdout.strip():
            stderr = output.stderr.decode("utf-8", "replace")
            raise SandboxError(f"Container exited with code {code}: {stderr[-1000:]}")
        return stdout


class RuntimeImageSandbox(StreamedSandbox):
    """A :class:`StreamedSandbox` whose image is found (or built) on first use by ``resolve``.

    A server's tool code without third-party libraries runs in the image of Nodestro's own runtime requirements
    (see :meth:`nodestro_runtime.python_envs.PythonEnvironments.runtime_image`). If it cannot be built, the call
    fails; nothing runs anywhere else."""

    def __init__(self, resolve: Callable[[], str], **options: Any) -> None:
        super().__init__(image="", **options)
        self._resolve = resolve

    def _execute(self, workdir: Path, request: str) -> str:
        if not self.image:
            try:
                self.image = self._resolve()
            except NodestroError as exc:
                raise SandboxError(f"The sandbox image is not available: {exc}") from exc
        return super()._execute(workdir, request)


#: Kept for code that imported the earlier name (PR-11).
LocalImageSandbox = StreamedSandbox


def _orkexa_dir() -> Path:
    import orkexa

    return Path(orkexa.__file__).resolve().parent


def _orkexa_files() -> list[Path]:
    root = _orkexa_dir()
    return sorted(
        path for path in root.rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )


_FINGERPRINT: str | None = None


def orkexa_fingerprint() -> str:
    """The SHA-256 of the Orkexa package this process runs; part of every image key (the image carries a copy)."""
    global _FINGERPRINT
    if _FINGERPRINT is None:
        root = _orkexa_dir()
        digest = hashlib.sha256()
        for path in _orkexa_files():
            digest.update(str(path.relative_to(root)).encode("utf-8") + b"\0")
            digest.update(path.read_bytes() + b"\0")
        _FINGERPRINT = digest.hexdigest()
    return _FINGERPRINT


def _copy_orkexa(target: Path) -> None:
    root = _orkexa_dir()
    for path in _orkexa_files():
        destination = target / path.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)


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


__all__ = [
    "IMAGE_REPOSITORY",
    "LOCK_LABEL",
    "ContainerImages",
    "LocalImageSandbox",
    "RuntimeImageSandbox",
    "StreamedSandbox",
    "lock_has_hashes",
    "orkexa_fingerprint",
]
