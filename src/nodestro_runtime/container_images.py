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

Tool calls then run in that image as Orkexa's :class:`~orkexa.dynamic.DockerSandbox` runs them
(:class:`StreamedSandbox`): one container shape (:func:`orkexa.dynamic.docker.run_args`: no network, read-only file
system, no capabilities, ``nobody``, private IPC, memory/CPU/process/file limits, no logging), the image run by its
id, code and Orkexa's runner on stdin, and only after the daemon passed Orkexa's preflight and in-container self-test.
If Docker or the image is missing, or the daemon does not prove the sandbox's limits, the call fails; there is never a
fallback to a host subprocess.

Proxies and an extra certificate authority for the download stage come from the host: ``HTTP(S)_PROXY`` /
``NO_PROXY`` are passed as build arguments, and the file in ``NODESTRO_BUILD_CA_FILE`` (or ``SSL_CERT_FILE``) is
copied into the download stage only. ``NODESTRO_DOCKER_BUILD_NETWORK`` chooses the build network (default: Docker's).

Images are capped: a built image larger than ``NODESTRO_MAX_IMAGE_GB`` (default 8 GiB) is removed again and the build
fails (``deps.image_too_large``), and after every build :meth:`ContainerImages.gc` removes the least recently used
environment images beyond 20 images or 20 GiB (:mod:`nodestro_runtime.image_limits`). Only images that carry
:data:`LOCK_LABEL` are ever removed, never an image Nodestro did not build, nor one a caller keeps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any
import uuid

from orkexa.dynamic import DockerSandbox, SandboxError, SandboxProbe
from orkexa.dynamic.docker import (
    BOOTSTRAP,
    ContainerLimits,
    copy_runner,
    run_args,
    runner_files,
    runner_fingerprint,
)
from orkexa.providers.base import redact

from . import docker_status
from .docker_status import OWNER
from .image_limits import (
    GC_MAX_GB,
    GC_MAX_IMAGES,
    GIB,
    IMAGE_REPOSITORY,
    LOCK_LABEL,
    MAX_IMAGE_GB,
    MAX_IMAGE_GB_VARIABLE,
)
from .problems import NodestroError

logger = logging.getLogger(__name__)

#: The tags of environment images (``IMAGE_REPOSITORY`` and a 32-digit key); the only tags :meth:`ContainerImages.gc`
#: looks at.
_TAG = re.compile(rf"^{re.escape(IMAGE_REPOSITORY)}:[0-9a-f]{{32}}$")
#: A use is written to the usage record at most this often (seconds) per image.
_USE_INTERVAL = 60.0
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
#: The image that resolves requirements in a container when a package has to be built from source to read its
#: dependencies (:meth:`ContainerImages.compile_in_container`): uv's official image with Python, pinned by digest.
#: The digest is the multi-arch index of ``0.9.5-python3.13-bookworm-slim`` (linux/amd64 and linux/arm64), checked
#: against ghcr.io on 2026-11-02. A placeholder digest is refused. Update with
#: ``docker buildx imagetools inspect ghcr.io/astral-sh/uv:<version>-python3.13-bookworm-slim``.
UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.9.5-python3.13-bookworm-slim"
    "@sha256:60d8ee2c1f7ffce050822adcb44907514e39f29d2d7b37732e8fca9f4f6113af"
)
_UV_IMAGE_PLACEHOLDER = "@sha256:" + "0" * 64
_PINNED = re.compile(r"^[a-z0-9./:_-]+@sha256:[0-9a-f]{64}$")
_RESOLVE_TIMEOUT = 900.0
#: Runs in the resolve container: writes the requirements (and the previous lock, and a certificate authority) from
#: stdin into the tmpfs ``/tmp``, runs ``uv pip compile`` there and prints only the lock on stdout.
_RESOLVE = (
    "import json, os, subprocess, sys\n"
    "envelope = json.loads(sys.stdin.read())\n"
    "work = '/tmp/resolve'\n"
    "os.makedirs(work, mode=0o700)\n"
    "env = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'), 'HOME': '/tmp',\n"
    "       'UV_CACHE_DIR': '/tmp/uv-cache', 'UV_NO_PROGRESS': '1', 'NO_COLOR': '1', 'UV_PYTHON_DOWNLOADS': 'never'}\n"
    "for name in json.loads(sys.argv[1]):\n"
    "    if os.environ.get(name):\n"
    "        env[name] = os.environ[name]\n"
    "if envelope.get('ca'):\n"
    "    with open('/tmp/ca.pem', 'w', encoding='utf-8') as handle:\n"
    "        handle.write(envelope['ca'])\n"
    "    env['SSL_CERT_FILE'] = '/tmp/ca.pem'\n"
    "with open(os.path.join(work, 'requirements.in'), 'w', encoding='utf-8') as handle:\n"
    "    handle.write(envelope['requirements'])\n"
    "if envelope.get('previous'):\n"
    "    with open(os.path.join(work, 'requirements.txt'), 'w', encoding='utf-8') as handle:\n"
    "        handle.write(envelope['previous'])\n"
    "command = ['uv', 'pip', 'compile', 'requirements.in', '-o', 'requirements.txt', '--universal',\n"
    "           '--python-version', envelope['python'], '--no-header', '--quiet', '--generate-hashes']\n"
    "code = subprocess.run(command, cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=sys.stderr).returncode\n"
    "if code:\n"
    "    sys.exit(code)\n"
    "with open(os.path.join(work, 'requirements.txt'), encoding='utf-8') as handle:\n"
    "    sys.stdout.write(handle.read())\n"
)


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
        usage_file: Path | None = None,
        keep: Callable[[], Iterable[str]] | None = None,
        collect: bool = True,
    ) -> None:
        """``usage_file`` keeps when each image was last used (for :meth:`gc`); ``keep`` names images that are
        never collected (those of installed environments); ``collect=False`` turns the collection after a build
        off (a server, whose daemon serves every tenant)."""
        self.docker = docker
        self.base = base
        self.environ = os.environ if environ is None else environ
        self._run = run
        self.usage_file = usage_file
        self.keep = keep
        self.collect = collect
        self._usage_lock = threading.Lock()
        self._noted: dict[str, float] = {}

    # ---- names --------------------------------------------------------------------------------

    def tag(self, lock: str, python_version: str) -> str:
        key = _sha(json.dumps([_sha(lock), python_version, self.base(python_version), orkexa_fingerprint()]))
        return f"{IMAGE_REPOSITORY}:{key[:32]}"

    # ---- docker -------------------------------------------------------------------------------

    def available(self) -> bool:
        return DockerSandbox.available(self.docker)

    def _docker(
        self,
        args: list[str],
        *,
        timeout: float = 60.0,
        log: Callable[[str], None] | None = None,
        stdin: str | None = None,
    ) -> str:
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
                **({} if stdin is None else {"input": stdin}),
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
            self.note_use(tag)
            return tag
        limit = self._max_image_bytes()
        with tempfile.TemporaryDirectory(prefix="nodestro-image-") as directory:
            context = Path(directory)
            (context / "requirements.txt").write_text(lock, encoding="utf-8")
            ca = self._certificate_authority()
            if ca is not None:
                shutil.copyfile(ca, context / "ca.pem")
            copy_runner(context)  # context/orkexa/...: the runner, copied to /opt/orkexa/orkexa
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
        found = self._inspect(tag)
        size = found[0] if found is not None else 0
        if size > limit:
            self.remove(tag)
            raise NodestroError(
                "deps.image_too_large",
                f"The container image of these libraries is {size / GIB:.1f} GB, more than the limit of "
                f"{limit / GIB:g} GB ({MAX_IMAGE_GB_VARIABLE}); it was removed again",
                size_gb=f"{size / GIB:.1f}",
                limit_gb=f"{limit / GIB:g}",
            )
        self.note_use(tag, force=True)
        if self.collect:
            try:
                self.gc(keep={tag})
            except (NodestroError, _DockerFailed, OSError) as exc:  # a failed collection never fails the build
                logger.warning("Removing unused environment images failed: %s", exc)
        return tag

    def _max_image_bytes(self) -> int:
        value = self.environ.get(MAX_IMAGE_GB_VARIABLE, "").strip()
        if not value:
            return int(MAX_IMAGE_GB * GIB)
        try:
            limit = float(value)
        except ValueError:
            limit = 0.0
        if not limit > 0 or limit == float("inf"):
            raise NodestroError("deps.image_failed", f"{MAX_IMAGE_GB_VARIABLE} must be a positive number of GB")
        return int(limit * GIB)

    # ---- size and collection ------------------------------------------------------------------

    def _inspect(self, tag: str) -> tuple[int, str] | None:
        """Size (bytes) and lock label of an image (``None``: no such image); an empty label: not Nodestro's."""
        try:
            output = self._docker(
                ["image", "inspect", "--format", f'{{{{.Size}}}}|{{{{index .Config.Labels "{LOCK_LABEL}"}}}}', tag]
            )
        except _DockerFailed:
            return None
        size, _, label = output.strip().partition("|")
        label = label.strip()
        try:
            return int(size), "" if label == "<no value>" else label
        except ValueError:
            return 0, "" if label == "<no value>" else label

    def note_use(self, tag: str, *, force: bool = False) -> None:
        """Record that ``tag`` was used now (at most once a minute per image unless ``force``)."""
        if self.usage_file is None:
            return
        moment = time.time()
        if not force and moment - self._noted.get(tag, 0.0) < _USE_INTERVAL:
            return
        self._noted[tag] = moment
        with self._usage_lock:
            usage = self._read_usage()
            usage[tag] = moment
            self._write_usage(usage)

    def _read_usage(self) -> dict[str, float]:
        if self.usage_file is None:
            return {}
        try:
            raw: Any = json.loads(self.usage_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(tag): float(when)
            for tag, when in raw.items()
            if _TAG.fullmatch(str(tag)) and isinstance(when, (int, float))
        }

    def _write_usage(self, usage: Mapping[str, float]) -> None:
        if self.usage_file is None:
            return
        try:
            self.usage_file.parent.mkdir(parents=True, exist_ok=True)
            partial = self.usage_file.with_name(self.usage_file.name + f".{uuid.uuid4().hex[:8]}.tmp")
            partial.write_text(json.dumps(dict(sorted(usage.items()))), encoding="utf-8")
            partial.replace(self.usage_file)
        except OSError as exc:
            logger.warning("Cannot write the image usage record %s: %s", self.usage_file, exc)

    def _listed(self) -> set[str]:
        """The environment images the daemon lists (empty where listing is refused, e.g. by the sandbox gateway)."""
        try:
            output = self._docker(
                [
                    *("image", "ls", "--filter", f"label={LOCK_LABEL}"),
                    *("--filter", f"reference={IMAGE_REPOSITORY}:*", "--format", "{{.Repository}}:{{.Tag}}"),
                ]
            )
        except _DockerFailed:
            return set()
        return {line.strip() for line in output.splitlines() if _TAG.fullmatch(line.strip())}

    def gc(
        self,
        max_images: int = GC_MAX_IMAGES,
        max_bytes: int = int(GC_MAX_GB * GIB),
        keep: Iterable[str] = (),
    ) -> list[str]:
        """Remove the least recently used environment images until at most ``max_images`` images of at most
        ``max_bytes`` remain; returns the removed tags.

        Only ``nodestro-env:<key>`` images that carry :data:`LOCK_LABEL` are counted and removed; ``keep`` (and what
        the ``keep`` callback of the constructor names) is never removed, nor an image a container still uses (the
        daemon refuses that). Images missing from the usage record count as the least recently used."""
        kept = set(keep) | (set(self.keep()) if self.keep is not None else set())
        with self._usage_lock:
            usage = self._read_usage()
        images: list[tuple[float, str, int]] = []
        gone: set[str] = set()
        for tag in sorted(set(usage) | self._listed()):
            found = self._inspect(tag)
            if found is None:
                gone.add(tag)
                continue
            size, label = found
            if not label:  # not built by Nodestro: never counted, never removed
                continue
            images.append((usage.get(tag, 0.0), tag, size))
        images.sort()
        count, total = len(images), sum(size for _, _, size in images)
        removed: list[str] = []
        for _, tag, size in images:
            if count <= max_images and total <= max_bytes:
                break
            if tag in kept:
                continue
            try:
                self._docker(["image", "rm", tag])
            except _DockerFailed as exc:
                logger.info("Keeping the environment image %s: %s", tag, exc.output[-200:])
                continue
            removed.append(tag)
            count, total = count - 1, total - size
        if removed or gone:
            with self._usage_lock:
                usage = self._read_usage()
                for tag in [*removed, *gone]:
                    usage.pop(tag, None)
                    self._noted.pop(tag, None)
                self._write_usage(usage)
        return removed

    def _certificate_authority(self) -> Path | None:
        for name in ("NODESTRO_BUILD_CA_FILE", "SSL_CERT_FILE"):
            value = self.environ.get(name)
            if value and Path(value).is_file():
                return Path(value)
        return None

    # ---- resolving with source builds, in a container -----------------------------------------

    def compile_in_container(
        self,
        requirements: list[str],
        *,
        python: str,
        previous: str,
        log: Callable[[str], None],
        image: str | None = None,
    ) -> str:
        """Resolve ``requirements`` like :meth:`~nodestro_runtime.python_envs.UvRunner.compile`, but in a container,
        so the build scripts of packages without a wheel never run on the host. Returns the lock (with hashes).

        The container has network (the only thing it is for: the package index), no bind mount, no host variable
        but the proxy settings (passed by name, their values never on a command line), no capabilities, no new
        privileges, user ``nobody``, a read-only root file system with a ``tmpfs`` ``/tmp``, memory/CPU/process
        limits. The requirements, the previous lock and a certificate authority (``NODESTRO_BUILD_CA_FILE`` or
        ``SSL_CERT_FILE``) travel on stdin; the lock comes back on stdout. The image is uv's, pinned by digest
        (:data:`UV_IMAGE`); an image that is not pinned by a real digest is refused (``deps.build_unavailable``).

        Raises :class:`ResolveError` with uv's output when resolving fails, :class:`NodestroError` when Docker is
        missing (``sandbox.unavailable``) or the image is not pinned."""
        image = image or UV_IMAGE
        if image.endswith(_UV_IMAGE_PLACEHOLDER) or not _PINNED.fullmatch(image):
            raise NodestroError(
                "deps.build_unavailable",
                "Building packages from source in a container is not set up in this version (the resolver image is "
                "not pinned), and Nodestro never runs build scripts on this computer",
            )
        ca = self._certificate_authority()
        envelope = json.dumps(
            {
                "requirements": "\n".join(requirements) + "\n",
                "previous": previous,
                "python": python,
                "ca": ca.read_text(encoding="utf-8", errors="replace") if ca is not None else "",
            }
        )
        name = f"nodestro-resolve-{uuid.uuid4().hex[:12]}"
        environ = self._cli_environment()
        proxies = [variable for variable in _PROXY_VARIABLES if environ.get(variable)]
        # Network only for this step: Docker's default network, or the one image builds use.
        network = self.environ.get("NODESTRO_DOCKER_BUILD_NETWORK")
        args = [
            *("run", "--rm", "-i", "--pull", "missing", "--name", name),
            *(("--network", network) if network else ()),
            *("--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534"),
            *("--pids-limit", "256", "--memory", "2048m", "--memory-swap", "2048m", "--cpus", "2"),
            *("--tmpfs", "/tmp:rw,nosuid,size=1024m,uid=65534,gid=65534,mode=0700"),
            *(item for variable in proxies for item in ("--env", variable)),
            image,
            *("python", "-I", "-B", "-c", _RESOLVE, json.dumps(proxies)),
        ]
        log(f"$ uv pip compile (in a container of {image.split('@', 1)[0]})")
        try:
            return self._docker(args, timeout=_RESOLVE_TIMEOUT, log=lambda line: log(redact(line)), stdin=envelope)
        except _DockerFailed as exc:
            if _daemon_unreachable(exc.output):
                raise NodestroError("sandbox.unavailable", "Docker is not running") from None
            raise ResolveError(redact(exc.output)) from None
        except NodestroError:
            # A timed-out docker client leaves its container running.
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                self._run(
                    [self.docker, "rm", "-f", name],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                    env=environ,
                )
            raise

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
        self.note_use(tag)
        return StreamedSandbox(
            image=tag, timeout=timeout, memory_mb=memory_mb, docker=self.docker, environ=self._cli_environment()
        )


#: The owner label of local MCP servers' containers: they live as long as their connection, so the tool containers'
#: reaper never touches them.
MCP_OWNER = "nodestro-mcp"
#: Seconds a container made by :func:`sandbox_run_args` may exist by default (its deadline label).
DEFAULT_LIFETIME = 600.0


def sandbox_run_args(
    docker: str,
    container_name: str,
    image: str,
    *,
    memory_mb: int,
    cpus: float,
    pids_limit: int,
    owner: str = OWNER,
    lifetime_s: float = DEFAULT_LIFETIME,
) -> list[str]:
    """``docker run`` up to the image for a container of the sandbox shape (the command follows).

    Orkexa's one shape (:func:`orkexa.dynamic.docker.run_args`): no network, read-only root file system, no
    capabilities, no new privileges, user ``nobody``, private IPC, a small ``/dev/shm``, memory (without swap), CPU,
    process, open-file, file-size and core limits, no logging, small ``noexec`` temporary file systems, stdin attached,
    removed on exit, never pulled, and the labels ``org.orkexa.sandbox=<owner>`` and ``org.orkexa.deadline`` (now plus
    ``lifetime_s``). The sandbox gateway accepts exactly this shape
    (:func:`~nodestro_runtime.sandbox_gateway.plan_create`)."""
    limits = ContainerLimits(memory_mb=memory_mb, cpus=cpus, pids=pids_limit, lifetime_s=lifetime_s)
    return run_args(docker, container_name, image, limits, owner=owner)


#: Runs first in the container (Orkexa's): writes the runner modules and the tool files from stdin into the tmpfs file
#: systems and starts the runner there.
_BOOTSTRAP = BOOTSTRAP


class StreamedSandbox(DockerSandbox):
    """Orkexa's Docker sandbox for an image Nodestro built, as Nodestro runs it.

    Exactly :class:`~orkexa.dynamic.DockerSandbox` with ``transport="stdin"``: nothing of the host's file system is
    mounted (the tool's files and Orkexa's runner travel on stdin into a ``tmpfs`` work directory), so the daemon may
    run on another host or in another container (a server's workers, the sandbox gateway); each call runs the image
    by its id in Orkexa's one container shape, and only after the daemon passed the preflight and the in-container
    self-test. Its containers carry the owner label :data:`OWNER`. The docker CLI gets only ``environ`` (the variables
    it needs to reach its daemon, :meth:`ContainerImages._cli_environment`), never the process's. Cancelling a run
    (:mod:`nodestro_runtime.sandbox_cancel`) or :meth:`cancel` removes the call's container.
    """

    def __init__(self, *, environ: Mapping[str, str] | None = None, owner: str = OWNER, **options: Any) -> None:
        options.pop("transport", None)  # always stdin: a mount needs the daemon to see this computer's files
        super().__init__(transport="stdin", owner=owner, environ=environ, **options)

    def self_test(self, *, image: str | None = None) -> SandboxProbe:
        """Orkexa's self-test; a pass also proves the daemon for the sandbox policy
        (:func:`nodestro_runtime.docker_status.daemon_proven`)."""
        probe = super().self_test(image=image)
        docker_status.record_proof(self.docker, self.environ, probe)
        return probe


class RuntimeImageSandbox(StreamedSandbox):
    """A :class:`StreamedSandbox` whose image is found (or built) on first use by ``resolve``.

    Tool code without third-party libraries runs in the image of Nodestro's own runtime requirements (see
    :meth:`nodestro_runtime.python_envs.PythonEnvironments.runtime_image`), on the desktop as on a server. ``resolve``
    names the image (a tag); every call and the self-test run it by its id, so a tag moved later is never run
    unchecked. If it cannot be built, the call fails; nothing runs anywhere else."""

    def __init__(self, resolve: Callable[[], str], **options: Any) -> None:
        super().__init__(image="", **options)
        self._resolve = resolve

    def _resolved(self) -> str:
        if not self.image:
            try:
                self.image = self._resolve()
            except NodestroError as exc:
                raise SandboxError(f"The sandbox image is not available: {exc}") from exc
        return self.image

    def self_test(self, *, image: str | None = None) -> SandboxProbe:
        if image is None:
            try:
                self._resolved()
            except SandboxError as exc:
                return SandboxProbe("docker", False, "image", str(exc))
        return super().self_test(image=image)

    def command(self, workdir: Path, container_name: str, *, image: str | None = None) -> list[str]:
        return super().command(workdir, container_name, image=image or self._resolved())

    def _execute(self, workdir: Path, request: str) -> str:
        self._resolved()
        return super()._execute(workdir, request)


#: Kept for code that imported the earlier name (PR-11).
LocalImageSandbox = StreamedSandbox


def _orkexa_dir() -> Path:
    import orkexa

    return Path(orkexa.__file__).resolve().parent


def _orkexa_files() -> dict[str, str]:
    """What an image carries of Orkexa: the runner and the modules it imports (``{"orkexa/<path>": source}``)."""
    return runner_files()


def orkexa_fingerprint() -> str:
    """The SHA-256 of Orkexa's sandbox runner (:func:`orkexa.dynamic.docker.runner_fingerprint`); part of every image
    key (the image carries a copy)."""
    return runner_fingerprint()


def _copy_orkexa(target: Path) -> None:
    """Write Orkexa's runner modules into the folder ``target`` (``target/<path>``, as the ``orkexa`` package)."""
    copy_runner(target.parent)
    if target.name != "orkexa":  # copy_runner writes <parent>/orkexa/...
        raise ValueError("The target of the runner copy must be a folder named orkexa")


class ResolveError(Exception):
    """Resolving in a container failed; ``output`` is uv's explanation (see ``python_envs.explain_uv_error``)."""

    def __init__(self, output: str) -> None:
        super().__init__(output)
        self.output = output


class _DockerFailed(Exception):
    def __init__(self, output: str) -> None:
        super().__init__(output)
        self.output = output


_DAEMON_UNREACHABLE = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "failed to connect to the docker api",  # Docker CLI 28 and later
)


def _daemon_unreachable(output: str) -> bool:
    """Whether the docker client's ``output`` says it could not reach a daemon (old and new CLI wording)."""
    lowered = output.lower()
    return any(marker in lowered for marker in _DAEMON_UNREACHABLE)


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
    if _daemon_unreachable(output):
        return ("sandbox.unavailable", "Docker is not running")
    return ("deps.image_failed", f"Building the container image failed: {output[-500:]}")


__all__ = [
    "GC_MAX_GB",
    "GC_MAX_IMAGES",
    "IMAGE_REPOSITORY",
    "LOCK_LABEL",
    "MAX_IMAGE_GB",
    "MCP_OWNER",
    "OWNER",
    "UV_IMAGE",
    "ContainerImages",
    "LocalImageSandbox",
    "ResolveError",
    "RuntimeImageSandbox",
    "StreamedSandbox",
    "lock_has_hashes",
    "orkexa_fingerprint",
    "sandbox_run_args",
]
