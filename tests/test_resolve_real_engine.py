"""The dependency-resolve path on a real Docker engine (RT-038; R-1 and R-5 of the 2026-11-02 review).

:meth:`~nodestro_runtime.container_images.ContainerImages.compile_in_container` against a real daemon: the pinned
resolver image pulled by digest and run by its id, the self-test with the resolve container's own limits before any
resolve container starts, the effective limits of that container (``docker container inspect`` and the kernel's view
from inside), its network and environment, timeout, cancel and cleanup. The stand-in tests of the same code are in
``test_container_images.py``.

The tests need Docker and network (ghcr.io for the resolver image, PyPI for one tiny pure-Python wheel) and run with
``NODESTRO_DOCKER_TESTS=1`` (set in the CI ``docker`` job). Without it they are skipped, unless
``NODESTRO_REQUIRE_DOCKER=1`` turns that into a failure. Tests of the approved path also need a daemon on which the
resolve sandbox passes Orkexa's self-test (cgroup v2 among others); on another daemon they are skipped with the
self-test's refusal, and fail under ``NODESTRO_REQUIRE_DOCKER=1``. The tests of the refusal paths run on any daemon.
On a network that intercepts TLS, ``NODESTRO_BUILD_CA_FILE`` names the extra certificate authority (it reaches the
resolve container on stdin, as in production).

The containers carry an owner label of this module only, so the cleanup checks see nothing of other tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any
import uuid

from orkexa.dynamic import BoundedOutput, SandboxProbe
from orkexa.dynamic.docker import DEADLINE_LABEL, OWNER_LABEL, ContainerLimits, reap, run_args
import pytest

from nodestro_runtime import container_images, sandbox_cancel
from nodestro_runtime.container_images import (
    RESOLVE_ALLOWANCES,
    UV_IMAGE,
    ContainerImages,
    StreamedSandbox,
    check_shape,
    lock_has_hashes,
)
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.testing import FakeUv
from nodestro_runtime.workspace import Workspace

DOCKER_TESTS = os.environ.get("NODESTRO_DOCKER_TESTS") == "1"
REQUIRED = os.environ.get("NODESTRO_REQUIRE_DOCKER") == "1"
#: The owner label of every container this module starts (``org.orkexa.sandbox=<owner>``).
OWNER = f"nodestro-resolve-test-{uuid.uuid4().hex[:8]}"
#: The Python version the resolver image carries.
PYTHON = "3.13"
#: A tiny pure-Python wheel (no dependencies) and its lock.
WHEEL = "six==1.17.0"
SIX_HASHES = (
    "4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274",
    "ff70335d468e7eb6ec65b95b99d3a2836546063f63acc5171de367e834932a81",
)
SECRET = f"s3cr3t-{uuid.uuid4().hex}"
MIB = 1024 * 1024
#: The file the host creates (``docker exec``) once it inspected a probe container; the probe waits for it.
INSPECTED = "/tmp/.nodestro-inspected"

#: Runs in the resolve container instead of uv (the container itself is the real resolve container): waits until the
#: host inspected it, then prints what the kernel applied, as JSON. Standard library only.
PROBE = f"""\
import json, os, resource, subprocess, sys, time
sys.stdin.read()
deadline = time.monotonic() + 60
while not os.path.exists({INSPECTED!r}) and time.monotonic() < deadline:
    time.sleep(0.05)

def read(path):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None

def attempt(action):
    try:
        action()
    except OSError as exc:
        return '%s:%s' % (type(exc).__name__, exc.errno)
    return 'allowed'

def run_in(directory):
    path = os.path.join(directory, '.probe')
    def run():
        with open(path, 'w') as handle:
            handle.write('#!/bin/sh\\nexit 0\\n')
        os.chmod(path, 0o755)
        subprocess.run([path], check=True)
    return attempt(run)

def size(path):
    stat = os.statvfs(path)
    return stat.f_blocks * stat.f_frsize

lines = (read('/proc/self/status') or '').splitlines()
status = dict((key, value.strip()) for key, _, value in (line.partition(':') for line in lines))
mounts = {{}}
for line in read('/proc/self/mountinfo').splitlines():
    parts = line.split()
    separator = parts.index('-')
    mounts[parts[4]] = sorted(set(parts[5].split(',')) | set(parts[separator + 3].split(',')))
relative = (read('/proc/self/cgroup') or '').partition('0::')[2].strip()
cgroup = {{}}
for base in ('/sys/fs/cgroup', '/sys/fs/cgroup' + relative):
    if os.path.exists(os.path.join(base, 'memory.max')):
        names = ('memory.max', 'memory.swap.max', 'pids.max', 'cpu.max')
        cgroup = dict((name, read(os.path.join(base, name))) for name in names)
        break
devices = (read('/proc/net/dev') or '').splitlines()[2:]
fields = ('Uid', 'Gid', 'CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb', 'NoNewPrivs', 'Seccomp')
sealed = {{}}
for point in ('/run', '/var/tmp'):
    sealed[point] = attempt(lambda: open(os.path.join(point, '.probe'), 'w').close())
limits = ('RLIMIT_NOFILE', 'RLIMIT_FSIZE', 'RLIMIT_CORE')
print(json.dumps({{
    'uid': os.getuid(), 'gid': os.getgid(),
    'status': dict((key, status.get(key)) for key in fields),
    'mounts': dict((point, mounts.get(point)) for point in ('/', '/tmp', '/work', '/run', '/var/tmp', '/dev/shm')),
    'sizes': dict((point, size(point)) for point in ('/tmp', '/work', '/dev/shm')),
    'write_root': attempt(lambda: open('/.probe', 'w').close()),
    'write_sealed': sealed,
    'exec_tmp': run_in('/tmp'), 'exec_work': run_in('/work'),
    'cgroup': cgroup,
    'rlimits': dict((name, list(resource.getrlimit(getattr(resource, name)))) for name in limits),
    'interfaces': sorted(line.split(':')[0].strip() for line in devices if ':' in line),
    'environ': dict(os.environ),
}}))
"""
#: Runs in the resolve container instead of uv for the timeout and cancel tests: never ends by itself.
HANG = "import sys, time; sys.stdin.read(); time.sleep(600)"


#: A tool that needs six (the plans of the profile tests).
TOOL = '''# /// script
# dependencies = ["six"]
# ///
import six


def show(text: str) -> str:
    """Show text."""
    return six.text_type(text)
'''


# ---- the engine ------------------------------------------------------------------------------------


def _require_engine() -> None:
    if not DOCKER_TESTS:
        if REQUIRED:
            pytest.fail("NODESTRO_REQUIRE_DOCKER=1 but NODESTRO_DOCKER_TESTS is not set")
        pytest.skip("set NODESTRO_DOCKER_TESTS=1 (needs Docker and network for the resolver image and PyPI)")
    from orkexa.dynamic.docker import DockerProbeError, inspect_host, preflight

    try:
        ready = preflight(inspect_host()).available
    except DockerProbeError:
        ready = False
    if not ready:
        pytest.fail("NODESTRO_DOCKER_TESTS=1 but the Docker daemon does not pass Orkexa's preflight")


def _docker(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


def _environ(**extra: str) -> dict[str, str]:
    """What a ContainerImages of these tests sees of the host: what the docker CLI needs, the build certificate
    authority if one is set, and ``extra``; never the host's own proxy settings (the tests set them)."""
    names = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")
    environ = {name: os.environ[name] for name in names if name in os.environ}
    if os.environ.get("NODESTRO_BUILD_CA_FILE"):
        environ["NODESTRO_BUILD_CA_FILE"] = os.environ["NODESTRO_BUILD_CA_FILE"]
    return {**environ, **extra}


def _created_since(since: float, kind: str = "container", event: str = "create") -> list[str]:
    """The names of the containers created (or the images pulled) on the daemon since ``since``."""
    done = _docker(
        *("events", "--since", f"{since:.3f}", "--until", f"{time.time() + 0.5:.3f}"),
        *("--filter", f"type={kind}", "--filter", f"event={event}", "--format", "{{.Actor.Attributes.name}}"),
    )
    assert done.returncode == 0, done.stderr
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def _exists(name: str) -> bool:
    return bool(_docker("ps", "-aq", "--filter", f"name=^/?{name}$").stdout.strip())


def _leftovers() -> list[str]:
    """Containers of this module's owner, and resolve containers of any owner, that still exist."""
    found = _docker("ps", "-a", "--filter", f"label={OWNER_LABEL}={OWNER}", "--format", "{{.Names}}").stdout.split()
    found += _docker("ps", "-a", "--filter", "name=nodestro-resolve-", "--format", "{{.Names}}").stdout.split()
    return sorted(set(found))


def _volumes() -> set[str]:
    return set(_docker("volume", "ls", "-q").stdout.split())


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    """After every test: no container of this module's owner, no resolve container and no new volume (anonymous
    volumes of an image's ``VOLUME`` included). What is left is removed, then reported."""
    try:
        usable = DOCKER_TESTS and _docker("info").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        usable = False
    if not usable:
        yield
        return
    before = _volumes()
    yield
    left: list[str] = []
    for _ in range(50):  # --rm removes a container a moment after its client returned
        left = _leftovers()
        if not left:
            break
        time.sleep(0.1)
    volumes = _volumes() - before
    if left:
        reap("docker", OWNER, remove_all=True)
        for name in left:
            _docker("rm", "-f", "-v", name)
    for volume in volumes:
        _docker("volume", "rm", "-f", volume)
    assert left == [], f"containers left behind: {left}"
    assert volumes == set(), f"volumes left behind: {volumes}"


@dataclass(frozen=True)
class Resolver:
    """The pinned resolver image on this daemon and the resolve sandbox's self-test there."""

    image_id: str
    repo_digests: list[str]
    probe: SandboxProbe


@pytest.fixture(scope="module")
def resolver() -> Resolver:
    _require_engine()
    images = ContainerImages(source_builds=True, owner=OWNER, environ=_environ())
    image_id = images._pinned_image(UV_IMAGE, log=lambda _line: None)  # pulled by its digest if missing
    inspected = _docker("image", "inspect", "--format", "{{json .RepoDigests}}", image_id)
    assert inspected.returncode == 0, inspected.stderr
    sandbox = StreamedSandbox(
        image=image_id,
        timeout=container_images._RESOLVE_TIMEOUT,
        container_limits=container_images._RESOLVE_LIMITS,
        environ=images._cli_environment(),
        owner=OWNER,
    )
    return Resolver(image_id, json.loads(inspected.stdout), sandbox.self_test())


@pytest.fixture
def proven(resolver: Resolver) -> Resolver:
    """The resolver, on a daemon that proved the resolve sandbox; skipped (or failed) elsewhere."""
    if not resolver.probe.available:
        message = f"the resolve sandbox is not proven on this daemon ({resolver.probe.reason}): {resolver.probe.detail}"
        if REQUIRED:
            pytest.fail(message)
        pytest.skip(message)
    return resolver


# ---- a recording sandbox ---------------------------------------------------------------------------


@dataclass
class Trace:
    """What a :func:`recording` sandbox saw: self-tests and container starts in order, ``docker run`` commands,
    ``docker container inspect`` of each started container, and each container's output."""

    events: list[tuple[str, Any]] = field(default_factory=list)
    runs: list[list[str]] = field(default_factory=list)
    inspected: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[BoundedOutput] = field(default_factory=list)
    started: threading.Event = field(default_factory=threading.Event)


def recording(
    trace: Trace,
    *,
    command: list[str] | None = None,
    signal: bool = False,
    limits: ContainerLimits | None = None,
) -> type[StreamedSandbox]:
    """A :class:`StreamedSandbox` that runs the real self-test and the real container and records them.

    ``command`` replaces what runs in the resolve container (``python -I -B -c <script>``, after the image), not the
    container: its ``docker run`` options are the ones :meth:`ContainerImages.compile_in_container` built and
    checked. While it runs, the container is inspected; with ``signal``, :data:`INSPECTED` is created in it then.
    ``limits`` replaces the limits the sandbox proves (a refusal test)."""

    class Recording(StreamedSandbox):
        def limits(self) -> ContainerLimits:
            return limits if limits is not None else super().limits()

        def self_test(self, *, image: str | None = None) -> SandboxProbe:
            probe = super().self_test(image=image)
            trace.events.append(("self_test", (self.limits(), probe)))
            return probe

        def run_container(
            self, args: list[str], name: str, data: bytes = b"", *, max_stdout: int
        ) -> tuple[int, BoundedOutput]:
            trace.events.append(("run", name))
            trace.runs.append(list(args))
            if command is not None:
                start = args.index("python", args.index("--name"))
                args = [*args[:start], *command]
            stop = threading.Event()
            watcher = threading.Thread(target=self._watch, args=(name, stop), daemon=True)
            watcher.start()
            try:
                code, output = super().run_container(args, name, data, max_stdout=max_stdout)
            finally:
                stop.set()
                watcher.join(30)
            trace.outputs.append(output)
            return code, output

        def _watch(self, name: str, stop: threading.Event) -> None:
            while not stop.is_set():
                done = _docker("container", "inspect", name, timeout=30)
                if done.returncode == 0:
                    (found,) = json.loads(done.stdout)
                    if found.get("State", {}).get("Running"):
                        trace.inspected.append(found)
                        if signal:
                            _docker("exec", name, "python", "-c", f"open({INSPECTED!r}, 'w').close()", timeout=30)
                        trace.started.set()
                        return
                time.sleep(0.05)

    return Recording


def _images(trace: Trace, **options: Any) -> ContainerImages:
    environ = options.pop("environ", None)
    return ContainerImages(
        source_builds=True,
        owner=OWNER,
        environ=environ if environ is not None else _environ(),
        sandbox_class=recording(trace, **options),
    )


def _report(trace: Trace) -> dict[str, Any]:
    (output,) = trace.outputs
    lines = output.stdout.decode("utf-8", "replace").strip().splitlines()
    assert lines, output.stderr.decode("utf-8", "replace")[-2000:]
    report: dict[str, Any] = json.loads(lines[-1])
    return report


def _options(args: list[str], image_id: str) -> list[tuple[str, str | None]]:
    """The ``docker run`` options of ``args`` up to the image, as pairs."""
    options, image = container_images._run_options(args[: args.index(image_id) + 1], "docker")
    assert image == image_id
    return options


def _proven_shape(name: str, image_id: str, deadline: int) -> list[str]:
    return run_args("docker", name, image_id, container_images._RESOLVE_LIMITS, owner=OWNER, deadline=deadline)


# ---- 1. preflight and self-test before any resolve container ---------------------------------------


def test_the_resolve_sandbox_is_proven_with_the_resolve_limits_before_any_resolve_container(
    resolver: Resolver,
) -> None:
    """The first thing a resolve does on the daemon is Orkexa's preflight and self-test, in the pinned image and with
    exactly the resolve container's limits. On a daemon that passes, the resolve container starts after it; on one
    that does not (cgroup v1, for example), the resolve is refused with the self-test's reason and no resolve
    container is ever created."""
    trace = Trace()
    since = time.time()
    try:
        lock: str | None = _images(trace).compile_in_container([WHEEL], python=PYTHON, previous="", log=print)
    except NodestroError as exc:
        refused: NodestroError | None = exc
        lock = None
    else:
        refused = None
    kind, (limits, probe) = trace.events[0]
    assert kind == "self_test" and limits == container_images._RESOLVE_LIMITS
    assert dict(probe.facts).get("image_id") == resolver.image_id  # the self-test ran the pinned image by its id
    assert probe.available == resolver.probe.available
    created = _created_since(since)
    if probe.available:
        assert refused is None and lock is not None and lock_has_hashes(lock)
        assert [kind for kind, _ in trace.events] == ["self_test", "run"]
        assert len([name for name in created if name.startswith("nodestro-resolve-")]) == 1
    else:
        assert refused is not None and refused.code == "sandbox.unavailable"
        assert probe.reason in str(refused) and probe.detail in str(refused)
        assert trace.runs == [] and [kind for kind, _ in trace.events] == ["self_test"]
        assert not [name for name in created if name.startswith("nodestro-resolve-")]


def test_a_resolve_container_that_left_the_proven_shape_is_refused_before_it_is_created(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolve shape with one more flag than the allowances (``--privileged``) is refused by the shape check
    after the self-test (``sandbox.shape_mismatch``); on a daemon that is not proven, the self-test refuses first.
    Either way no resolve container is created on the daemon."""
    original = ContainerImages._resolve_args

    def privileged(self: ContainerImages, *args: Any, **options: Any) -> list[str]:
        shaped = original(self, *args, **options)
        return [*shaped[:-1], "--privileged", shaped[-1]]

    monkeypatch.setattr(ContainerImages, "_resolve_args", privileged)
    trace = Trace()
    since = time.time()
    with pytest.raises(NodestroError) as caught:
        _images(trace).compile_in_container([WHEEL], python=PYTHON, previous="", log=print)
    expected = "sandbox.shape_mismatch" if resolver.probe.available else "sandbox.unavailable"
    assert caught.value.code == expected
    assert trace.runs == [] and not [name for name in _created_since(since) if name.startswith("nodestro-resolve-")]


def test_a_sandbox_that_proved_other_limits_starts_no_resolve_container(resolver: Resolver) -> None:
    """The shape check compares the resolve container with the shape the self-test ran, the sandbox's own limits:
    a sandbox that proved other limits (here: twice the memory) than the resolve container would get is refused
    (``sandbox.shape_mismatch``; ``sandbox.unavailable`` where the self-test itself refuses) and nothing starts."""
    trace = Trace()
    other = replace(container_images._RESOLVE_LIMITS, memory_mb=4096)
    since = time.time()
    with pytest.raises(NodestroError) as caught:
        _images(trace, limits=other).compile_in_container([WHEEL], python=PYTHON, previous="", log=print)
    kind, (limits, _probe) = trace.events[0]
    assert kind == "self_test" and limits == other
    assert caught.value.code in ("sandbox.shape_mismatch", "sandbox.unavailable")
    if resolver.probe.available:
        assert caught.value.code == "sandbox.shape_mismatch"
    assert trace.runs == [] and not [name for name in _created_since(since) if name.startswith("nodestro-resolve-")]


# ---- 2. image id and digest pinning ----------------------------------------------------------------


def test_the_resolver_image_is_pinned_by_digest_and_run_by_its_id(resolver: Resolver) -> None:
    """The resolver image on the daemon carries the pinned digest; a local tag of the same name that points to
    another image (as anyone who can tag images could arrange) is never self-tested or run: the sandbox and the
    resolve container use the id of the image with the digest, never a tag."""
    repository, _, digest = UV_IMAGE.partition("@")
    name = repository.rsplit(":", 1)[0]
    assert any(entry == f"{name}@{digest}" for entry in resolver.repo_digests), resolver.repo_digests
    previous = _docker("image", "inspect", "--format", "{{.Id}}", repository).stdout.strip()
    dockerfile = f"FROM {name}@{digest}\nLABEL org.nodestro.test=moved-tag\n"
    built = subprocess.run(
        ["docker", "build", "-q", "-t", repository, "-"],
        input=dockerfile,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    moved = _docker("image", "inspect", "--format", "{{.Id}}", repository).stdout.strip()
    try:
        assert moved and moved != resolver.image_id
        trace = Trace()
        try:
            _images(trace).compile_in_container([WHEEL], python=PYTHON, previous="", log=print)
        except NodestroError as exc:
            assert not resolver.probe.available and exc.code == "sandbox.unavailable"
        _, (_, probe) = trace.events[0]
        assert dict(probe.facts).get("image_id") == resolver.image_id
        for args in trace.runs:
            image = args.index(resolver.image_id)
            assert moved not in args and not [item for item in args[:image] if item.startswith(name)]
            assert ("--pull", "never") in _options(args, resolver.image_id)
        assert len(trace.runs) == (1 if resolver.probe.available else 0)
    finally:
        _docker("image", "rm", repository)
        if previous:
            _docker("tag", previous, repository)


# ---- 3. the effective limits of the resolve container -----------------------------------------------


def test_the_resolve_container_runs_with_the_proven_limits(proven: Resolver) -> None:
    """The running resolve container, as the daemon reports it and as the kernel applies it from inside, is the
    proven shape with the resolve limits, differing only in :data:`RESOLVE_ALLOWANCES` (a network, ``noexec``
    dropped from ``/tmp``; no proxy variable is set here, so no ``--env``)."""
    limits = container_images._RESOLVE_LIMITS
    trace = Trace()
    images = _images(trace, command=["python", "-I", "-B", "-c", PROBE], signal=True)
    images.compile_in_container([WHEEL], python=PYTHON, previous="", log=print)
    (args,) = trace.runs
    (found,) = trace.inspected
    report = _report(trace)
    name = args[args.index("--name") + 1]
    labels = [value or "" for flag, value in _options(args, proven.image_id) if flag == "--label"]
    deadline = int(next(value for value in labels if value.startswith(DEADLINE_LABEL + "=")).partition("=")[2])
    expected = _proven_shape(name, proven.image_id, deadline)
    check_shape(args[: args.index(proven.image_id) + 1], expected, allow=RESOLVE_ALLOWANCES)
    tmpfs = {
        flag_value.partition(":")[0]: flag_value.partition(":")[2]
        for flag, flag_value in _options(expected, proven.image_id)
        if flag == "--tmpfs" and flag_value
    }
    tmpfs["/tmp"] = ",".join(item for item in tmpfs["/tmp"].split(",") if item != "noexec")  # exec_tmp

    # What the daemon applied (docker container inspect).
    host, config = found["HostConfig"], found["Config"]
    assert found["Image"] == proven.image_id and config["Image"] == proven.image_id
    assert host["Memory"] == limits.memory_mb * MIB and host["MemorySwap"] == limits.memory_mb * MIB
    assert host["NanoCpus"] == int(limits.cpus * 1e9) and host["PidsLimit"] == limits.pids
    assert host["ShmSize"] == limits.shm_mb * MIB
    assert host["ReadonlyRootfs"] is True and host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"] and not host.get("CapAdd")
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert config["User"] == "65534:65534" and config["WorkingDir"] == "/work"
    assert host["IpcMode"] == "private" and host["LogConfig"]["Type"] == "none" and host["AutoRemove"] is True
    assert host["Tmpfs"] == tmpfs
    assert sorted((item["Name"], item["Soft"], item["Hard"]) for item in host["Ulimits"]) == sorted(
        [
            ("nofile", limits.nofile, limits.nofile),
            ("fsize", limits.fsize_mb * MIB, limits.fsize_mb * MIB),
            ("core", 0, 0),
        ]
    )
    assert not host.get("Binds") and not host.get("Mounts") and found["Mounts"] == []  # no bind mount, no volume
    assert host["NetworkMode"] in ("default", "bridge")  # network: Docker's default instead of none
    assert config["Labels"][OWNER_LABEL] == OWNER and config["Labels"][DEADLINE_LABEL] == str(deadline)
    image_env = json.loads(_docker("image", "inspect", "--format", "{{json .Config.Env}}", proven.image_id).stdout)
    assert sorted(config["Env"]) == sorted(image_env)  # nothing of the host's environment

    # What the kernel applied, from inside.
    status = report["status"]
    assert report["uid"] == 65534 and report["gid"] == 65534
    assert set(status["Uid"].split()) == {"65534"} and set(status["Gid"].split()) == {"65534"}
    for capability in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert int(status[capability], 16) == 0, (capability, status[capability])
    assert status["NoNewPrivs"] == "1" and status["Seccomp"] == "2"
    assert report["cgroup"] == {
        "memory.max": str(limits.memory_mb * MIB),
        "memory.swap.max": "0",
        "pids.max": str(limits.pids),
        "cpu.max": f"{int(limits.cpus * 100000)} 100000",
    }
    fsize = limits.fsize_mb * MIB
    assert report["rlimits"] == {
        "RLIMIT_NOFILE": [limits.nofile, limits.nofile],
        "RLIMIT_FSIZE": [fsize, fsize],
        "RLIMIT_CORE": [0, 0],
    }
    mounts = report["mounts"]
    assert "ro" in mounts["/"] and report["write_root"] != "allowed"
    assert {"nosuid", "nodev"} <= set(mounts["/tmp"])  # whether /tmp is executable: the next test
    assert {"noexec", "nosuid", "nodev"} <= set(mounts["/work"]) and report["exec_work"] != "allowed"
    assert report["write_sealed"]["/run"] != "allowed" and report["write_sealed"]["/var/tmp"] != "allowed"
    for point, size in (("/tmp", limits.tmp_mb), ("/work", limits.tmp_mb), ("/dev/shm", limits.shm_mb)):
        assert 0 < report["sizes"][point] <= size * MIB, (point, report["sizes"][point])
    assert "lo" in report["interfaces"] and len(report["interfaces"]) > 1  # network: one interface besides lo
    # Inside: the image's variables and what the engine sets, nothing else (LC_CTYPE is Python's own C-locale
    # coercion, PEP 538: the resolver image sets no LANG).
    assert set(report["environ"]) - {item.partition("=")[0] for item in image_env} <= {"HOSTNAME", "HOME", "LC_CTYPE"}


def test_build_tools_can_run_programs_from_the_resolve_container_s_tmp(proven: Resolver) -> None:
    """The ``exec_tmp`` allowance exists so that build tools can run what they put into ``/tmp`` (RT-038, R-5).

    Dropping ``noexec`` from the ``--tmpfs`` options is not enough for that: Docker mounts a tmpfs ``noexec`` unless
    its options say ``exec``. This test shows what the resolve container really gets."""
    trace = Trace()
    _images(trace, command=["python", "-I", "-B", "-c", PROBE], signal=True).compile_in_container(
        [WHEEL], python=PYTHON, previous="", log=print
    )
    report = _report(trace)
    assert "noexec" not in report["mounts"]["/tmp"], (
        f"/tmp of the resolve container is mounted {report['mounts']['/tmp']}: Docker's tmpfs default is noexec"
    )
    assert report["exec_tmp"] == "allowed", report["exec_tmp"]
    assert report["exec_work"] != "allowed"  # /work stays noexec


# ---- 4. network rules ------------------------------------------------------------------------------


def test_a_wheel_only_requirement_resolves_in_the_resolve_container(proven: Resolver) -> None:
    """The approved path end to end: a tiny pure-Python wheel resolved by uv in the real resolve container with
    Docker's default network, to a lock with the wheel's hashes; the container ran the pinned image by id, in the
    proven shape up to the allowances, after the self-test."""
    trace = Trace()
    lines: list[str] = []
    lock = _images(trace).compile_in_container([WHEEL], python=PYTHON, previous="", log=lines.append)
    assert lock_has_hashes(lock) and lock.startswith("six==1.17.0 ")
    assert all(f"--hash=sha256:{digest}" in lock for digest in SIX_HASHES), lock
    assert [kind for kind, _ in trace.events] == ["self_test", "run"]
    (args,) = trace.runs
    image = args.index(proven.image_id)
    assert args[image + 1 : image + 5] == ["python", "-I", "-B", "-c"]
    assert args[image + 5] == container_images._RESOLVE and json.loads(args[image + 6]) == []
    options = _options(args, proven.image_id)
    assert [flag for flag, _ in options if flag in ("--network", "--env", "-e", "-v", "--volume", "--mount")] == []
    name = args[args.index("--name") + 1]
    assert not _exists(name)


def test_off_the_desktop_a_source_build_is_refused_before_any_docker_call(tmp_path: Path, resolver: Resolver) -> None:
    """R-1 on a real engine: on a server tenant's workspace and in Nodestro Local Docker, a plan a person accepted
    that needs a source build reports ``deps.source_build_disabled``; the docker CLI is never called and the daemon
    sees no container and no pull. A ContainerImages with source builds off refuses the same way."""
    from bedrock_support import tenant_storage

    from nodestro_runtime.profile import RuntimeProfile

    calls: list[list[str]] = []

    def recorded(args: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return container_images.bounded_run(args, **options)

    since = time.time()
    trace = Trace()
    opened = [
        Workspace(
            tmp_path / "tenant",
            python_runner=FakeUv({"six": [("1.17.0", [], "")]}),
            integrations=[],
            storage=tenant_storage(tmp_path / "db", "alpha"),
        ),
        Workspace(
            profile=RuntimeProfile(kind="local-container", data_dir=tmp_path / "local"),
            python_runner=FakeUv({"six": [("1.17.0", [], "")]}),
            integrations=[],
        ),
    ]
    try:
        for workspace in opened:
            python = workspace.python
            python.sandbox_kind = lambda: "docker"
            python.images = ContainerImages(
                owner=OWNER, environ=_environ(), run=recorded, sandbox_class=recording(trace), source_builds=True
            )
            assert python.source_builds is False and python.images.source_builds is False
            runner = python.runner
            assert isinstance(runner, FakeUv)
            runner.source_only = {"six"}
            plan = python.plan("tool", "show", python.needed_for_sources([("show", TOOL, "")]))
            assert plan.error is not None and plan.error.code == "deps.source_build_disabled", plan.error
            assert plan.error.params == {"name": "six"} and not plan.lock
    finally:
        for workspace in opened:
            workspace.close()
    with pytest.raises(NodestroError) as caught:
        ContainerImages(owner=OWNER, environ=_environ(), run=recorded).compile_in_container(
            [WHEEL], python=PYTHON, previous="", log=print
        )
    assert caught.value.code == "deps.source_build_disabled"
    assert calls == [] and trace.events == [] and trace.runs == []
    prefixes = ("nodestro-resolve-", "orkexa-selftest-")
    assert not [name for name in _created_since(since) if name.startswith(prefixes)]
    assert _created_since(since, "image", "pull") == []


def test_the_desktop_resolves_an_accepted_source_build_only_in_a_proven_container(
    tmp_path: Path, resolver: Resolver
) -> None:
    """The desktop profile as the code stands (R-1, nothing new turned on): a plan a person accepted whose package
    needs a source build (here: the host's uv stand-in has no wheel for six) is resolved again in the real resolve
    container. On a proven daemon that gives the lock; elsewhere the plan stays ``deps.build_required`` with the
    reason ``sandbox.unavailable`` and no resolve container is created."""
    workspace = Workspace(tmp_path / "data", python_runner=FakeUv({"six": [("1.17.0", [], "")]}), integrations=[])
    trace = Trace()
    since = time.time()
    try:
        assert workspace.sandbox_policy.profile == "desktop"
        python = workspace.python
        python.sandbox_kind = lambda: "docker"
        python.images = _images(trace)
        assert python.source_builds is True and python.images.source_builds is True
        runner = python.runner
        assert isinstance(runner, FakeUv)
        runner.source_only = {"six"}
        plan = python.plan("tool", "show", python.needed_for_sources([("show", TOOL, "")]))
    finally:
        workspace.close()
    assert runner.builds == [False]  # uv on the host never builds
    if resolver.probe.available:
        assert plan.error is None, plan.error
        assert lock_has_hashes(plan.lock) and "six==1.17.0" in plan.lock and "pydantic==" in plan.lock
        assert [kind for kind, _ in trace.events] == ["self_test", "run"]
    else:
        assert plan.error is not None and plan.error.code == "deps.build_required"
        assert plan.error.params == {"name": "six", "reason": "sandbox.unavailable"} and not plan.lock
        assert trace.runs == []
        assert not [name for name in _created_since(since) if name.startswith("nodestro-resolve-")]


# ---- 5. timeout and cancel -------------------------------------------------------------------------


def test_a_resolve_that_times_out_is_stopped_and_its_container_removed(
    proven: Resolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(container_images, "_RESOLVE_TIMEOUT", 8.0)
    trace = Trace()
    started = time.monotonic()
    with pytest.raises(NodestroError) as caught:
        _images(trace, command=["python", "-I", "-B", "-c", HANG]).compile_in_container(
            [WHEEL], python=PYTHON, previous="", log=print
        )
    assert caught.value.code == "deps.image_failed" and "Timed out" in str(caught.value)
    assert time.monotonic() - started < 120
    assert trace.started.is_set() and len(trace.inspected) == 1  # it was running
    (args,) = trace.runs
    assert not _exists(args[args.index("--name") + 1])


def test_a_cancelled_resolve_is_stopped_and_its_container_removed(proven: Resolver) -> None:
    trace = Trace()
    images = _images(trace, command=["python", "-I", "-B", "-c", HANG])
    cancelled = threading.Event()
    threading.Thread(target=lambda: trace.started.wait(120) and cancelled.set(), daemon=True).start()
    token = sandbox_cancel.bind(cancelled.is_set)
    started = time.monotonic()
    try:
        with pytest.raises(NodestroError) as caught:
            images.compile_in_container([WHEEL], python=PYTHON, previous="", log=print)
    finally:
        sandbox_cancel.reset(token)
    assert caught.value.code == "deps.image_failed" and "cancelled" in str(caught.value)
    assert cancelled.is_set() and time.monotonic() - started < 120
    (args,) = trace.runs
    assert not _exists(args[args.index("--name") + 1])


# ---- 7. proxy credentials ----------------------------------------------------------------------------


def test_proxy_credentials_never_reach_the_resolve_container(proven: Resolver) -> None:
    """Proxy settings with a user name and password reach the resolve container only without them: not in its
    ``docker run`` command, not in ``docker container inspect``, not in its environment seen from inside, not in its
    output and not in the installation log. A setting that cannot be cleaned is left out. Only proxy variables are
    passed; other host variables (here a provider key) are not."""
    environ = _environ(
        HTTPS_PROXY=f"http://user:{SECRET}@proxy.invalid:3128",
        http_proxy=f"http://{SECRET}@proxy.invalid:8080",
        HTTP_PROXY=f"http://[{SECRET}@broken",
        NO_PROXY="localhost,.internal",
        ANTHROPIC_API_KEY=SECRET,
    )
    trace = Trace()
    lines: list[str] = []
    images = _images(trace, command=["python", "-I", "-B", "-c", PROBE], signal=True, environ=environ)
    images.compile_in_container([WHEEL], python=PYTHON, previous="", log=lines.append)
    (args,) = trace.runs
    (found,) = trace.inspected
    report = _report(trace)
    (output,) = trace.outputs
    passed = {
        "HTTPS_PROXY": "http://proxy.invalid:3128",
        "http_proxy": "http://proxy.invalid:8080",
        "NO_PROXY": "localhost,.internal",
    }
    env = [value for flag, value in _options(args, proven.image_id) if flag == "--env"]
    assert sorted(env) == sorted(f"{name}={value}" for name, value in passed.items())
    image_env = json.loads(_docker("image", "inspect", "--format", "{{json .Config.Env}}", proven.image_id).stdout)
    assert sorted(found["Config"]["Env"]) == sorted([*image_env, *env])
    for name, value in passed.items():
        assert report["environ"][name] == value
    assert "HTTP_PROXY" not in report["environ"] and "ANTHROPIC_API_KEY" not in report["environ"]
    for text in (
        json.dumps(args),
        json.dumps(found),
        json.dumps(report),
        output.stdout.decode("utf-8", "replace"),
        output.stderr.decode("utf-8", "replace"),
        "\n".join(lines),
    ):
        assert SECRET not in text and "user:" not in text
    assert {line for line in lines if "credentials" in line} == {
        f"The credentials in {name} are not passed to the resolver container (build code reads it)"
        for name in ("HTTPS_PROXY", "http_proxy", "HTTP_PROXY")
    }


# ---- 6. cleanup ------------------------------------------------------------------------------------


def test_nothing_of_the_resolve_path_is_left_on_the_daemon(resolver: Resolver) -> None:
    """After every test the cleanup fixture checks it; this checks it once more for the whole module: no container
    of this module's owner (self-test, resolve), no resolve container, and the owner's reaper finds nothing."""
    assert _leftovers() == []
    assert reap("docker", OWNER, remove_all=True) == 0
