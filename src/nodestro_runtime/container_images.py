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
The module check after a build (:meth:`ContainerImages.missing_modules`) runs package code, so it runs the same way.
Both it and the resolve container are checked against Orkexa's proven shape right before they start
(:func:`check_shape`): the module check must be exactly that shape, the resolve container may differ only in
:data:`RESOLVE_ALLOWANCES`.
Source builds that a person accepted (:meth:`ContainerImages.compile_in_container`) run in a container with network;
they are off unless the desktop profile turns them on (``deps.source_build_disabled``, RT-038).
Every docker CLI call reads bounded output (:func:`bounded_run`), and an image is only built on a base image pinned by
digest (:data:`BASE_IMAGES`; other Python versions are refused).

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
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
import uuid

from orkexa.dynamic import BoundedOutput, DockerSandbox, SandboxError, SandboxProbe, communicate_bounded, run_bounded
from orkexa.dynamic.docker import (
    BOOTSTRAP,
    DEADLINE_LABEL,
    ContainerLimits,
    copy_runner,
    run_args,
    runner_files,
    runner_fingerprint,
    stop_container,
)
from orkexa.providers.base import redact

from . import docker_status
from .docker_status import IMAGE_BUSY, OWNER, mcp_owner
from .image_limits import (
    BUILD_BUSY,
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


#: Seconds the module check of a built image (:meth:`ContainerImages.missing_modules`) may take.
_VERIFY_TIMEOUT = 120.0
#: Most bytes of stdout the host reads from the module check and from the resolve container (the lock); more stops
#: the container.
_VERIFY_STDOUT = 1024 * 1024
_RESOLVE_STDOUT = 8 * 1024 * 1024
#: Seconds a resolve container may outlive its timeout before the reaper removes it (its deadline label).
_RESOLVE_GRACE = 60.0
#: Most bytes of stdout the host keeps from one docker CLI call (:func:`bounded_run`; more kills the CLI) and how
#: much of the end of its stderr it keeps.
MAX_CLI_STDOUT = 16 * 1024 * 1024
MAX_CLI_STDERR = 256 * 1024
#: The limits of the resolve container (:meth:`ContainerImages.compile_in_container`). Its sandbox runs Orkexa's
#: self-test with exactly these limits in the resolver image, so they are proven on the daemon before a build runs.
_RESOLVE_LIMITS = ContainerLimits(
    memory_mb=2048,
    cpus=2.0,
    pids=256,
    nofile=4096,
    fsize_mb=1024,
    tmp_mb=1024,
    shm_mb=64,
    lifetime_s=_RESOLVE_TIMEOUT + _RESOLVE_GRACE,
)

#: A difference from Orkexa's proven container shape (:func:`orkexa.dynamic.docker.run_args`) that
#: :func:`check_shape` may allow, and nothing else:
#:
#: - ``network``: ``--network none`` replaced by Docker's default network (no ``--network``) or one named network
#:   (never ``host``, ``container:<id>``, ``ns:<path>`` or another namespace mode);
#: - ``exec_tmp``: ``noexec`` of the ``/tmp`` tmpfs replaced by ``exec``, every other option of it kept (Docker mounts
#:   a tmpfs ``noexec`` unless its options say ``exec``, so only dropping ``noexec`` leaves it non-executable);
#: - ``proxy_env``: ``--env`` for the proxy variables only, without credentials.
ShapeAllowance = Literal["network", "exec_tmp", "proxy_env"]


def _exec_tmp(value: str) -> str:
    """The ``/tmp:<options>`` tmpfs value with ``noexec`` replaced by ``exec`` (the ``exec_tmp`` allowance)."""
    return "/tmp:" + ",".join("exec" if item == "noexec" else item for item in value[5:].split(","))


#: What the resolve container may differ in (it is started only on the desktop profile, RT-038).
RESOLVE_ALLOWANCES: frozenset[ShapeAllowance] = frozenset({"network", "exec_tmp", "proxy_env"})
#: ``docker run`` flags of the proven shape that take no value.
_VALUE_FREE_FLAGS = frozenset({"--rm", "-i", "--read-only"})
#: A network a resolve container may be attached to: a plain network name (no ``mode:argument`` forms).
_NETWORK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_NAMESPACE_NETWORKS = frozenset({"host"})


def _run_options(args: list[str], docker: str) -> tuple[list[tuple[str, str | None]], str]:
    """The options of ``docker run ... <image>`` (``args`` ends with the image) as ``(flag, value)`` pairs in order,
    and the image. Raises :class:`ValueError` for anything else: another command, a positional argument before the
    image, ``--flag=value`` forms or a flag without its value."""
    if len(args) < 3 or args[0] != docker or args[1] != "run":
        raise ValueError("not a docker run command")
    body, image = args[2:-1], args[-1]
    if not image or image.startswith("-"):
        raise ValueError(f"invalid image {image!r}")
    options: list[tuple[str, str | None]] = []
    index = 0
    while index < len(body):
        flag = body[index]
        if not flag.startswith("-") or "=" in flag:
            raise ValueError(f"unexpected argument {flag!r}")
        if flag in _VALUE_FREE_FLAGS:
            options.append((flag, None))
            index += 1
            continue
        if index + 1 >= len(body):
            raise ValueError(f"{flag} has no value")
        options.append((flag, body[index + 1]))
        index += 2
    return options, image


def _allowed_network(value: str) -> bool:
    return bool(_NETWORK_NAME.fullmatch(value)) and value.lower() not in _NAMESPACE_NETWORKS


def _allowed_proxy_env(value: str) -> bool:
    name, separator, setting = value.partition("=")
    return (
        bool(separator)
        and name in _PROXY_VARIABLES
        and "\n" not in setting
        and "\x00" not in setting
        and strip_credentials(setting) == setting
    )


def _normalized(
    options: list[tuple[str, str | None]], allow: frozenset[ShapeAllowance], *, candidate: bool
) -> list[tuple[str, str | None]]:
    """``options`` with the allowed differences taken out (raises :class:`ValueError` for a difference that is not
    allowed in the form it has)."""
    kept: list[tuple[str, str | None]] = []
    networks = 0
    for flag, value in options:
        if flag == "--network" and "network" in allow:
            networks += 1
            if candidate and not (value is not None and _allowed_network(value)):
                raise ValueError(f"the network {value!r} is not allowed")
            continue
        if flag == "--tmpfs" and value is not None and value.startswith("/tmp:") and "exec_tmp" in allow:
            kept.append((flag, _exec_tmp(value)))
            continue
        if flag == "--env" and candidate and "proxy_env" in allow:
            if value is None or not _allowed_proxy_env(value):
                raise ValueError("only proxy settings without credentials may be passed")
            continue
        kept.append((flag, value))
    if networks > 1:
        raise ValueError("more than one --network")
    return kept


def check_shape(candidate: list[str], proven: list[str], *, allow: frozenset[ShapeAllowance] = frozenset()) -> None:
    """Refuse ``candidate`` (``docker run`` up to the image) unless it is ``proven`` (the same call of Orkexa's
    :func:`~orkexa.dynamic.docker.run_args`, the shape the in-container self-test proves) with at most the
    differences in ``allow`` (:data:`ShapeAllowance`): the same options with the same values in the same order, the
    same docker CLI and the same image. Raises :class:`NodestroError` ``sandbox.shape_mismatch`` otherwise.

    Every container Nodestro starts outside a :class:`StreamedSandbox` tool call (the module check, the resolve
    container) is checked this way right before it starts, so a later change of either side cannot quietly move it
    away from the proven shape."""
    docker = proven[0] if proven else ""
    try:
        theirs, their_image = _run_options(candidate, docker)
        ours, our_image = _run_options(proven, docker)
        if their_image != our_image:
            raise ValueError("another image")
        if _normalized(theirs, allow, candidate=True) != _normalized(ours, allow, candidate=False):
            raise ValueError("options that differ from the proven shape")
    except ValueError as exc:
        raise NodestroError(
            "sandbox.shape_mismatch",
            f"A container was refused before it started: it differs from the sandbox's proven shape ({exc})",
        ) from None


def default_base(python_version: str) -> str:
    """The pinned base image of a Python version (:data:`BASE_IMAGES`).

    A version without a pinned base image is refused (``deps.python_unsupported``): an image is never built on an
    unpinned tag such as ``python:<version>-slim``, whose content the upstream registry can change at any time."""
    base = BASE_IMAGES.get(python_version)
    if base is None:
        raise NodestroError(
            "deps.python_unsupported",
            f"There is no pinned base image for Python {python_version} (supported: {', '.join(BASE_IMAGES)}); "
            "environment images are only built on base images pinned by digest",
            version=python_version,
        )
    return base


class OutputLimitExceeded(subprocess.SubprocessError):
    """A docker CLI call wrote more than :data:`MAX_CLI_STDOUT` bytes to stdout; it was killed."""


def bounded_run(
    args: list[str],
    *,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    input: str | None = None,
    max_stdout: int | None = None,
    max_stderr: int | None = None,
    **_ignored: Any,
) -> subprocess.CompletedProcess[str]:
    """:func:`subprocess.run` for the docker CLI (``capture_output=True, text=True``) that never holds more than
    ``max_stdout`` bytes of stdout and the last ``max_stderr`` bytes of stderr (defaults: :data:`MAX_CLI_STDOUT`,
    :data:`MAX_CLI_STDERR`).

    More stdout kills the process and raises :class:`OutputLimitExceeded`; a timeout kills it and raises
    :class:`subprocess.TimeoutExpired`. A daemon, a gateway or a build that floods its output therefore cannot exhaust
    the memory of the API or worker process."""
    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env) if env is not None else None,
    )
    data = (input or "").encode("utf-8")
    max_stdout = MAX_CLI_STDOUT if max_stdout is None else max_stdout
    max_stderr = MAX_CLI_STDERR if max_stderr is None else max_stderr
    result: dict[str, BoundedOutput] = {}

    def communicate() -> None:
        result["output"] = communicate_bounded(process, data, max_stdout=max_stdout, max_stderr=max_stderr)

    reader = threading.Thread(target=communicate, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        with contextlib.suppress(OSError):
            process.kill()
        reader.join(10)
        with contextlib.suppress(subprocess.SubprocessError):
            process.wait(10)
        raise subprocess.TimeoutExpired(args, timeout or 0.0)
    process.wait()
    output = result["output"]
    if output.overflow:
        raise OutputLimitExceeded(
            f"{Path(args[0]).name} {args[1] if len(args) > 1 else ''} wrote more than "
            f"{max_stdout} bytes of output and was stopped"
        )
    return subprocess.CompletedProcess(
        args,
        process.returncode if process.returncode is not None else -1,
        output.stdout.decode("utf-8", "replace"),
        output.stderr.decode("utf-8", "replace"),
    )


def strip_credentials(url: str) -> str:
    """``url`` without a user name and password (``http://user:secret@proxy:3128`` -> ``http://proxy:3128``).

    Values that are not URLs (``NO_PROXY`` lists) are returned unchanged."""
    if "@" not in url:
        return url
    if "://" not in url:  # proxy:3128 written without a scheme
        return url.rsplit("@", 1)[1]
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""  # not a URL Nodestro can clean: rather no proxy than a leaked password
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1]))


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
        run: Callable[..., subprocess.CompletedProcess[str]] = bounded_run,
        usage_file: Path | None = None,
        keep: Callable[[], Iterable[str]] | None = None,
        collect: bool = True,
        owner: str = OWNER,
        sandbox_class: type[StreamedSandbox] | None = None,
        source_builds: bool = False,
    ) -> None:
        """``usage_file`` keeps when each image was last used (for :meth:`gc`); ``keep`` names images that are
        never collected (those of installed environments); ``collect=False`` turns the collection after a build
        off (a server, whose daemon serves every tenant). ``owner`` labels the sandbox containers of
        :meth:`sandbox` (an installation's own, :func:`~nodestro_runtime.docker_status.installation_owner`).

        ``run`` runs one docker CLI call (default :func:`bounded_run`: bounded output); ``sandbox_class`` makes the
        sandboxes of :meth:`sandbox`, :meth:`missing_modules` and :meth:`compile_in_container` (tests pass a
        stand-in).

        ``source_builds`` allows :meth:`compile_in_container` (source builds a person accepted, in a container with
        network). It is off unless the caller turns it on: only the desktop profile does
        (:class:`~nodestro_runtime.python_envs.PythonEnvironments`), because no filtered build network exists yet
        (RT-038)."""
        self.docker = docker
        self.owner = owner
        self.base = base
        self.environ = os.environ if environ is None else environ
        self._run = run
        self.usage_file = usage_file
        self.keep = keep
        self.collect = collect
        self.sandbox_class = sandbox_class or StreamedSandbox
        self.source_builds = source_builds
        self._usage_lock = threading.Lock()
        self._noted: dict[str, float] = {}

    # ---- names --------------------------------------------------------------------------------

    def tag(self, lock: str, python_version: str) -> str:
        try:
            base = self.base(python_version)
        except NodestroError:
            # No pinned base: the tag only names an image that is never built (build() refuses), so lookups such as
            # "is it installed?" answer no instead of failing.
            base = f"unpinned:{python_version}"
        key = _sha(json.dumps([_sha(lock), python_version, base, orkexa_fingerprint()]))
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
        except OutputLimitExceeded as exc:
            raise NodestroError("deps.image_failed", f"docker {args[0]} stopped: {exc}") from exc
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
        base = self.base(python_version)  # deps.python_unsupported: never an unpinned base
        if not _PINNED.fullmatch(base):
            raise NodestroError(
                "deps.python_unsupported",
                f"The base image {base} is not pinned by digest; environment images are only built on pinned ones",
                version=python_version,
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
                    base=base,
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
                if build_busy(exc.output):
                    raise ImageBuildBusy() from None
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

        The build scripts are untrusted code, so the container is Orkexa's proven sandbox shape
        (:func:`orkexa.dynamic.docker.run_args` with :data:`_RESOLVE_LIMITS`, the shape the self-test checked on
        this daemon in this image) with exactly three differences (:data:`RESOLVE_ALLOWANCES`, enforced by
        :func:`check_shape` right before the container starts): network, an executable ``/tmp`` (compilers and build
        tools run there) and the proxy variables. So: no bind mount, no capabilities, no new privileges, user
        ``nobody``, a read-only root file system, ``noexec`` ``/work``, sealed ``/run`` and ``/var/tmp``, private IPC,
        memory (without swap), CPU, process, open-file, file-size and core limits, no logging, never pulled (the image
        is pulled by its digest first and run by its id), and the labels ``org.orkexa.sandbox=<owner>`` and
        ``org.orkexa.deadline``, so the reaper removes a container a crashed process left behind. The host reads at
        most :data:`_RESOLVE_STDOUT` bytes of the lock and the end of stderr; a timeout, too much output or a
        cancelled call kills and removes the container.

        It is the only container with network: the resolver needs the package index, and a package's metadata is
        only known after its build script ran. The network is Docker's default one (or
        ``NODESTRO_DOCKER_BUILD_NETWORK``, a network name; ``host`` and other namespace modes are refused), and it is
        not filtered (RT-038). No host variable reaches it but the proxy settings, **without** a user name or password
        (:func:`strip_credentials`): the build code could read them. The requirements, the previous lock and a
        certificate authority (``NODESTRO_BUILD_CA_FILE`` or ``SSL_CERT_FILE``) travel on stdin; the lock comes back
        on stdout.

        It runs only after the daemon passed Orkexa's preflight and the in-container self-test for the resolver image
        with the resolve container's limits (``sandbox.unavailable`` otherwise). The image is uv's, pinned by digest
        (:data:`UV_IMAGE`); an image that is not pinned by a real digest is refused (``deps.build_unavailable``).

        Source builds are off unless :attr:`source_builds` is set (only the desktop profile sets it): the network of
        this container is not filtered, so on a server or in Nodestro Local Docker build scripts could reach the LAN,
        ports the host publishes and a cloud's instance metadata service (RT-038). Then nothing is started, pulled or
        inspected (``deps.source_build_disabled``).

        Raises :class:`ResolveError` with uv's output when resolving fails, :class:`NodestroError` when source builds
        are off (``deps.source_build_disabled``), Docker is missing or not proven (``sandbox.unavailable``), the image
        is not pinned or cannot be pulled (``deps.build_unavailable``), the container left the proven shape
        (``sandbox.shape_mismatch``, nothing started) or the container was stopped (``deps.image_failed``)."""
        if not self.source_builds:
            # Before any docker call: no pull, no self-test, no container.
            raise NodestroError(
                "deps.source_build_disabled",
                "Building packages from source is turned off here: build scripts would run with unfiltered network "
                "access, and only the desktop profile allows that (after a person accepted the build)",
            )
        image = image or UV_IMAGE
        if image.endswith(_UV_IMAGE_PLACEHOLDER) or not _PINNED.fullmatch(image):
            raise NodestroError(
                "deps.build_unavailable",
                "Building packages from source in a container is not set up in this version (the resolver image is "
                "not pinned), and Nodestro never runs build scripts on this computer",
            )
        network = self._build_network()
        environ = self._cli_environment()
        image_id = self._pinned_image(image, log)
        sandbox = self.sandbox_class(
            image=image_id,
            timeout=_RESOLVE_TIMEOUT,
            container_limits=_RESOLVE_LIMITS,
            docker=self.docker,
            environ=environ,
            owner=self.owner,
        )
        # Preflight and self-test on this daemon, for this image and with exactly the resolve container's limits:
        # or nothing runs.
        probe = sandbox.self_test()
        if not probe.available:
            raise NodestroError(
                "sandbox.unavailable",
                f"The Docker sandbox is not proven on this daemon ({probe.reason}): {probe.detail}",
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
        proxies: dict[str, str] = {}
        for variable in _PROXY_VARIABLES:
            value = environ.get(variable, "")
            cleaned = strip_credentials(value)
            if cleaned != value:
                log(f"The credentials in {variable} are not passed to the resolver container (build code reads it)")
            if cleaned:
                proxies[variable] = cleaned
        name = f"nodestro-resolve-{uuid.uuid4().hex[:12]}"
        deadline = math.ceil(time.time() + _RESOLVE_LIMITS.lifetime_s)
        shaped = self._resolve_args(name, image_id, proxies, network=network, deadline=deadline)
        # The proven shape is the one the self-test above ran: this sandbox's limits, not the constant, so a sandbox
        # that proved other limits than the resolve container would get is refused here.
        proven = run_args(self.docker, name, image_id, sandbox.limits(), owner=self.owner, deadline=deadline)
        check_shape(shaped, proven, allow=RESOLVE_ALLOWANCES)  # sandbox.shape_mismatch: nothing starts
        args = [
            *shaped,
            "python",
            "-I",
            "-B",
            "-c",
            _RESOLVE,
            json.dumps(list(proxies)),
        ]
        log(f"$ uv pip compile (in a container of {image.split('@', 1)[0]})")
        try:
            code, output = sandbox.run_container(args, name, envelope.encode("utf-8"), max_stdout=_RESOLVE_STDOUT)
        except SandboxError as exc:  # timed out, too much output or cancelled: the container was removed
            raise NodestroError("deps.image_failed", f"Resolving in a container stopped: {exc}") from None
        stdout = output.stdout.decode("utf-8", "replace")
        stderr = output.stderr.decode("utf-8", "replace")
        for line in stderr.splitlines()[-200:]:
            log(redact(line))
        if code != 0:
            explanation = (stderr + stdout).strip()[-2000:] or f"exit code {code}"
            if _daemon_unreachable(explanation):
                raise NodestroError("sandbox.unavailable", "Docker is not running")
            raise ResolveError(redact(explanation))
        return stdout

    def _build_network(self) -> str | None:
        """The network of the resolve container: ``NODESTRO_DOCKER_BUILD_NETWORK`` or Docker's default (``None``).
        A namespace mode (``host``, ``container:<id>``, ...) is refused (``deps.build_unavailable``)."""
        network = self.environ.get("NODESTRO_DOCKER_BUILD_NETWORK", "").strip()
        if not network:
            return None
        if not _allowed_network(network):
            raise NodestroError(
                "deps.build_unavailable",
                f"NODESTRO_DOCKER_BUILD_NETWORK={network!r} is not allowed for building packages from source: it must "
                "name a Docker network (not host or another namespace mode)",
            )
        return network

    def _resolve_args(
        self, name: str, image_id: str, proxies: Mapping[str, str], *, network: str | None, deadline: int
    ) -> list[str]:
        """``docker run`` up to the image for the resolve container (see :meth:`compile_in_container`).

        Orkexa's proven shape (:func:`orkexa.dynamic.docker.run_args` with :data:`_RESOLVE_LIMITS`) with exactly
        :data:`RESOLVE_ALLOWANCES`: the network (``network``, or Docker's default), an executable ``/tmp`` (compilers
        and build tools run there) and the proxy variables in ``proxies``. :func:`check_shape` enforces that."""
        proven = run_args(self.docker, name, image_id, _RESOLVE_LIMITS, owner=self.owner, deadline=deadline)
        options, image = _run_options(proven, self.docker)
        args = [self.docker, "run"]
        for flag, value in options:
            if flag == "--network":
                if network:
                    args += [flag, network]
                continue
            if flag == "--tmpfs" and value is not None and value.startswith("/tmp:"):
                value = _exec_tmp(value)
            args += [flag] if value is None else [flag, value]
        for variable, setting in proxies.items():
            args += ["--env", f"{variable}={setting}"]
        return [*args, image]

    def _pinned_image(self, image: str, log: Callable[[str], None]) -> str:
        """The id of the local image ``image`` (``repository[:tag]@sha256:<digest>``), pulled by its digest when it is
        missing; refused unless the local image carries exactly that digest (``deps.build_unavailable``)."""
        named, _, digest = image.partition("@")
        repository = _repository(named)
        reference = f"{repository}@{digest}"

        def inspect() -> tuple[str, list[str]] | None:
            try:
                output = self._docker(["image", "inspect", "--format", "{{.Id}}|{{json .RepoDigests}}", reference])
            except _DockerFailed as exc:
                if _daemon_unreachable(exc.output):
                    raise NodestroError("sandbox.unavailable", "Docker is not running") from None
                return None
            found_id, _, digests = output.strip().partition("|")
            try:
                listed: Any = json.loads(digests or "[]")
            except ValueError:
                listed = []
            return found_id, [str(item) for item in listed] if isinstance(listed, list) else []

        found = inspect()
        if found is None:
            log(f"$ docker pull {reference}")
            try:
                self._docker(["pull", reference], timeout=_RESOLVE_TIMEOUT)
            except _DockerFailed as exc:
                if _daemon_unreachable(exc.output):
                    raise NodestroError("sandbox.unavailable", "Docker is not running") from None
                raise NodestroError(
                    "deps.build_unavailable", f"Cannot pull the resolver image {reference}: {exc.output[-500:]}"
                ) from None
            found = inspect()
        image_id, digests = found if found is not None else ("", [])
        pinned = any(
            entry.partition("@")[2] == digest and _repository(entry.partition("@")[0]) == repository
            for entry in digests
        )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) or not pinned:
            raise NodestroError(
                "deps.build_unavailable", f"The local resolver image does not carry the digest {digest} of {repository}"
            )
        return image_id

    def missing_modules(self, tag: str, modules: Iterable[str]) -> list[str]:
        """The modules that cannot be found in the image.

        The check runs package code (a package's ``.pth`` files run when the interpreter starts, even with ``-I``),
        so it runs exactly like a tool call: in the sandbox's container shape
        (:func:`orkexa.dynamic.docker.run_args`: no network, read-only, no capabilities, ``nobody``, limits, the
        owner and deadline labels), the image by its id, only after the daemon passed Orkexa's preflight and the
        self-test for this image (``sandbox.unavailable`` otherwise), with bounded output, and stopped and removed
        after :data:`_VERIFY_TIMEOUT` seconds (``deps.image_failed``). Its arguments must be exactly that shape
        (:func:`check_shape` without allowances; ``sandbox.shape_mismatch`` otherwise, nothing started)."""
        names = sorted({name for name in modules if name})
        script = (
            "import importlib.util, json, sys; "
            "print(json.dumps([n for n in json.loads(sys.argv[1]) if importlib.util.find_spec(n) is None]))"
        )
        sandbox = self.sandbox(tag, timeout=_VERIFY_TIMEOUT)
        try:
            image_id = sandbox.ready_image()
        except SandboxError as exc:
            raise NodestroError("sandbox.unavailable", f"The image {tag} cannot be checked: {exc}") from None
        name = f"nodestro-sandbox-verify-{uuid.uuid4().hex[:12]}"
        command = ["python", "-I", "-B", "-c", script, json.dumps(names)]
        shaped = sandbox.shaped_run(name, image_id)
        deadline = next(value for value in shaped if value.startswith(DEADLINE_LABEL + "="))
        proven = run_args(
            self.docker, name, image_id, sandbox.limits(), owner=self.owner, deadline=float(deadline.partition("=")[2])
        )
        check_shape(shaped, proven)  # exactly the self-tested shape, no allowance
        import time as _t  # DEBUG
        print("DEBUG MM start", _t.time(), name)
        try:
            code, output = sandbox.run_container([*shaped, *command], name, max_stdout=_VERIFY_STDOUT)
        except SandboxError as exc:
            raise NodestroError("deps.image_failed", f"Checking the modules of {tag} stopped: {exc}") from None
        stdout = output.stdout.decode("utf-8", "replace")
        import subprocess as _sp  # DEBUG
        print("DEBUG MM image_id", image_id, "tag id", _sp.run([self.docker, "image", "inspect", "-f", "{{.Id}}", tag], capture_output=True, text=True, env=sandbox.environ).stdout.strip())
        print("DEBUG MM argv", [*shaped, *command])
        print("DEBUG MM code", code, "stdout", repr(output.stdout[-3000:]), "stderr", repr(output.stderr[-3000:]))
        print("DEBUG MM environ", {k: v for k, v in sandbox.environ.items() if "DOCKER" in k})
        import os as _os  # DEBUG
        _log = _os.path.join(_os.path.dirname(sandbox.environ.get("DOCKER_HOST", "unix:///x/y")[7:]), "gateway.log")
        if _os.path.exists(_log):
            print("DEBUG MM gateway log\n" + open(_log, encoding="utf-8", errors="replace").read()[-40000:])
        for _i in range(3):
            _n = f"nodestro-sandbox-verify-{uuid.uuid4().hex[:12]}"
            _args = [_n if a == name else a for a in [*shaped, *command]]
            _r = _sp.run(_args, input=b"", capture_output=True, env=sandbox.environ, timeout=120)
            print("DEBUG MM gateway retry", _i, _r.returncode, _r.stdout[-300:], _r.stderr[-1500:])
        for _variant, _extra in (("direct-shape", []), ("direct-no-i", ["-i"])):
            _args = [a for a in [*shaped, *command] if a not in _extra]
            _n = f"nodestro-sandbox-verify-{uuid.uuid4().hex[:12]}"
            _args = [_n if a == name else a for a in _args]
            _env = {k: v for k, v in _os.environ.items() if k not in ("DOCKER_HOST",)}
            _r = _sp.run(_args, input=b"", capture_output=True, env=_env, timeout=120)
            print("DEBUG MM", _variant, _r.returncode, _r.stdout[-300:], _r.stderr[-1500:])
        if code != 0:
            stderr = output.stderr.decode("utf-8", "replace")
            raise NodestroError(
                "deps.image_failed", f"Checking the modules of {tag} failed (exit code {code}): {stderr[-500:]}"
            )
        try:
            found: Any = json.loads(stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return names
        if not isinstance(found, list):
            return names
        return [str(item) for item in found]

    def remove(self, tag: str) -> None:
        with contextlib.suppress(_DockerFailed, NodestroError):
            self._docker(["image", "rm", tag])

    def sandbox(self, tag: str, *, timeout: float = 60.0, memory_mb: int = 2048) -> StreamedSandbox:
        """A sandbox whose calls run in the image ``tag`` (Orkexa's container flags, no network, never pulled)."""
        self.note_use(tag)
        return self.sandbox_class(
            image=tag,
            timeout=timeout,
            memory_mb=memory_mb,
            docker=self.docker,
            environ=self._cli_environment(),
            owner=self.owner,
        )


#: The owner label of a server's local MCP servers' containers (an installation's own:
#: :func:`~nodestro_runtime.docker_status.mcp_owner`): they live as long as their connection, so the tool containers'
#: reaper never touches them.
MCP_OWNER = mcp_owner(OWNER)
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
    self-test. Its containers carry the owner label ``owner`` (the installation's,
    :func:`~nodestro_runtime.docker_status.installation_owner`; :data:`OWNER` on a server). The docker CLI gets only
    ``environ`` (the variables it needs to reach its daemon, :meth:`ContainerImages._cli_environment`), never the
    process's. Cancelling a run (:mod:`nodestro_runtime.sandbox_cancel`) or :meth:`cancel` removes the call's
    container.
    """

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        owner: str = OWNER,
        container_limits: ContainerLimits | None = None,
        **options: Any,
    ) -> None:
        """``container_limits`` replaces the limits Orkexa derives from ``memory_mb``, ``cpus``, ``pids_limit`` and
        ``timeout`` (the resolve container's larger file, tmpfs and open-file limits); the self-test then proves
        exactly these."""
        options.pop("transport", None)  # always stdin: a mount needs the daemon to see this computer's files
        if container_limits is not None:
            options.update(
                memory_mb=container_limits.memory_mb, cpus=container_limits.cpus, pids_limit=container_limits.pids
            )
        self._container_limits = container_limits  # before Orkexa's __init__, which validates limits()
        super().__init__(transport="stdin", owner=owner, environ=environ, **options)

    def limits(self) -> ContainerLimits:
        """The limits of this sandbox's containers (and of its self-test)."""
        return self._container_limits if self._container_limits is not None else super().limits()

    def self_test(self, *, image: str | None = None) -> SandboxProbe:
        """Orkexa's self-test; a pass also proves the daemon for the sandbox policy
        (:func:`nodestro_runtime.docker_status.daemon_proven`)."""
        probe = super().self_test(image=image)
        docker_status.record_proof(self.docker, self.environ, probe)
        return probe

    def ready_image(self) -> str:
        """The id of this sandbox's image once the daemon passed the preflight and the self-test for it (the gate of
        every tool call); raises :class:`SandboxError` otherwise."""
        return self._require_ready()

    def shaped_run(self, name: str, image_id: str) -> list[str]:
        """``docker run`` up to the image for one container of this sandbox's shape
        (:func:`orkexa.dynamic.docker.run_args` with this sandbox's limits and owner); the command follows."""
        return run_args(self.docker, name, image_id, self.limits(), owner=self.owner)

    def run_container(
        self, args: list[str], name: str, data: bytes = b"", *, max_stdout: int
    ) -> tuple[int, BoundedOutput]:
        """Run the docker command ``args`` (which starts the container ``name``) with ``data`` on stdin, as a tool call
        runs (:func:`orkexa.dynamic.run_bounded`): at most ``max_stdout`` bytes of stdout and the end of stderr are
        read, and a timeout (:attr:`timeout`), more output or a cancelled call (:meth:`cancel`,
        :mod:`nodestro_runtime.sandbox_cancel`) kills the client and removes the container
        (:func:`orkexa.dynamic.docker.stop_container`). Returns the exit code and the output; raises
        :class:`SandboxError` when it was stopped or docker cannot be started."""

        def remove() -> None:
            stop_container(self.docker, name, env=self.environ)  # killing the client does not stop the container

        try:
            with self._call_scope() as stop:
                return run_bounded(
                    args,
                    data,
                    timeout=self.timeout,
                    max_stdout=max_stdout,
                    env=self.environ,
                    on_abort=remove,
                    stop=stop,
                )
        except OSError as exc:
            raise SandboxError(f"Cannot start docker: {exc}") from exc


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
        """The self-test of the resolved image. An image that cannot be built fails with reason ``image``; one whose
        build the sandbox gateway refused because another build runs (:func:`build_busy`) with ``image-busy``, which
        the Docker monitor reports as ``checking`` and tries again soon (:mod:`nodestro_runtime.docker_status`)."""
        if image is None:
            try:
                self._resolved()
            except SandboxError as exc:
                busy = isinstance(exc.__cause__, ImageBuildBusy)
                return SandboxProbe("docker", False, IMAGE_BUSY if busy else "image", str(exc))
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


class ImageBuildBusy(NodestroError):
    """``deps.image_failed`` because the sandbox gateway refused the build while another one runs (:func:`build_busy`):
    the build was not started and nothing is wrong with it. Same code and parameters as any failed build, so a user
    interface needs no new text; the Docker monitor tells it apart by its class."""

    def __init__(self) -> None:
        super().__init__(
            "deps.image_failed",
            f"The sandbox gateway builds another image now; try again when it is done ({BUILD_BUSY})",
        )


class _DockerFailed(Exception):
    def __init__(self, output: str) -> None:
        super().__init__(output)
        self.output = output


_DAEMON_UNREACHABLE = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "failed to connect to the docker api",  # Docker CLI 28 and later
)


def _repository(name: str) -> str:
    """``docker.io/library/python:3.12-slim`` -> ``python``: an image name without its tag, as Docker lists it."""
    head, _, last = name.rpartition("/")
    name = (f"{head}/" if head else "") + last.split(":", 1)[0]
    for prefix in ("docker.io/library/", "index.docker.io/library/", "docker.io/", "library/"):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def build_busy(output: str) -> bool:
    """Whether a failed ``docker build``'s ``output`` is the sandbox gateway's refusal (429) because another image build
    runs (:data:`~nodestro_runtime.image_limits.BUILD_BUSY`): nothing is wrong with this build, it was not started."""
    return BUILD_BUSY.lower() in output.lower()


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
    "MAX_CLI_STDERR",
    "MAX_CLI_STDOUT",
    "MAX_IMAGE_GB",
    "MCP_OWNER",
    "OWNER",
    "UV_IMAGE",
    "ContainerImages",
    "ImageBuildBusy",
    "LocalImageSandbox",
    "OutputLimitExceeded",
    "ResolveError",
    "RuntimeImageSandbox",
    "StreamedSandbox",
    "bounded_run",
    "build_busy",
    "check_shape",
    "default_base",
    "lock_has_hashes",
    "orkexa_fingerprint",
    "sandbox_run_args",
    "strip_credentials",
]
