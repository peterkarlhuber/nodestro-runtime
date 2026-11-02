"""Container images per lock file for tools with libraries (ADR-0006 point 6, plan step PR-11).

The unit tests drive a stand-in for the docker CLI. The tests marked ``docker`` build a real image and run tool code
in it; they need Docker and network for the build (``NODESTRO_DOCKER_TESTS=1``, set in CI). Without the variable
they are skipped, unless ``NODESTRO_REQUIRE_DOCKER=1`` turns that into a failure.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

from orkexa.dynamic import DockerSandbox, SubprocessSandbox
from orkexa.errors import ToolExecutionError
import pytest

from nodestro_runtime.container_images import LOCK_LABEL, ContainerImages, StreamedSandbox, lock_has_hashes
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.python_envs import parse_lock
from nodestro_runtime.testing import FakeUv
from nodestro_runtime.workspace import Workspace

HASHED = """\
six==1.17.0 \\
    --hash=sha256:4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274 \\
    --hash=sha256:ff70335d468e7eb6ec65b95b99d3a2836546063f63acc5171de367e834932a81
    # via -r requirements.in
"""
PLAIN = "six==1.17.0\n    # via -r requirements.in\n"


class FakeDocker:
    """Answers docker CLI calls; ``images`` maps tag -> lock label."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.images: dict[str, str] = {}
        self.build_output = ""
        self.build_fails = False
        self.contexts: list[dict[str, str]] = []
        self.missing: list[str] = []
        self.orkexa_files: list[str] = []
        #: Image sizes in bytes (default 100 MB); tags in ``in_use`` cannot be removed; ``list_fails`` refuses
        #: ``image ls`` like the sandbox gateway does.
        self.sizes: dict[str, int] = {}
        self.build_size = 100 * 1024**2
        self.in_use: set[str] = set()
        self.list_fails = False
        #: Runs of the resolve container: (arguments, stdin); what it answers.
        self.resolves: list[tuple[list[str], str]] = []
        self.resolve_lock = HASHED
        self.resolve_error = ""

    def __call__(self, args: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        self.environments.append(dict(options.get("env") or {}))
        command = args[1:]
        if command[:2] == ["image", "inspect"]:
            tag = command[-1]
            if tag not in self.images:
                return subprocess.CompletedProcess(args, 1, "", "No such image")
            if "{{.Size}}" in command[command.index("--format") + 1]:
                label = self.images[tag] or "<no value>"
                return subprocess.CompletedProcess(args, 0, f"{self.sizes.get(tag, 0)}|{label}\n", "")
            return subprocess.CompletedProcess(args, 0, self.images[tag] + "\n", "")
        if command[:2] == ["image", "ls"]:
            if self.list_fails:
                return subprocess.CompletedProcess(args, 1, "", "GET /images/json is not available")
            assert f"label={LOCK_LABEL}" in command
            listed = [tag for tag, label in self.images.items() if label]
            return subprocess.CompletedProcess(args, 0, "".join(f"{tag}\n" for tag in listed), "")
        if command[0] == "build":
            context = Path(command[-1])
            self.contexts.append(
                {item.name: item.read_text() if item.is_file() else "<dir>" for item in context.iterdir()}
            )
            self.orkexa_files = (
                sorted(str(path.relative_to(context)) for path in (context / "orkexa").rglob("*.py"))
                if (context / "orkexa").is_dir()
                else []
            )
            if self.build_fails:
                return subprocess.CompletedProcess(args, 1, "", self.build_output)
            lock = (context / "requirements.txt").read_text()
            import hashlib

            built = command[command.index("-t") + 1]
            self.images[built] = hashlib.sha256(lock.encode()).hexdigest()
            self.sizes[built] = self.build_size
            return subprocess.CompletedProcess(args, 0, "built\n", "")
        if command[0] == "run" and any(item.startswith("nodestro-resolve-") for item in command):
            self.resolves.append((args, str(options.get("input"))))
            if self.resolve_error:
                return subprocess.CompletedProcess(args, 2, "", self.resolve_error)
            return subprocess.CompletedProcess(args, 0, self.resolve_lock, "resolved\n")
        if command[0] == "run":
            return subprocess.CompletedProcess(args, 0, json.dumps(self.missing) + "\n", "")
        if command[:2] == ["image", "rm"]:
            if command[-1] in self.in_use:
                return subprocess.CompletedProcess(args, 1, "", "image is being used by running container")
            self.images.pop(command[-1], None)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker()


def images(docker: FakeDocker, **environ: str) -> ContainerImages:
    return ContainerImages(docker="/usr/bin/docker", environ={"PATH": "/usr/bin", **environ}, run=docker)


# ---- locks and tags ----------------------------------------------------------------------------


def test_locks_with_hashes_are_parsed_and_recognised() -> None:
    assert lock_has_hashes(HASHED) and not lock_has_hashes(PLAIN) and not lock_has_hashes("")
    (package,) = parse_lock(HASHED)
    assert (package.name, package.version, package.direct) == ("six", "1.17.0", True)
    mixed = HASHED + "idna==3.10\n"
    assert not lock_has_hashes(mixed)  # one package without a hash is enough to refuse


def test_the_tag_follows_the_lock_the_python_version_and_the_base(docker: FakeDocker) -> None:
    built = images(docker)
    tag = built.tag(HASHED, "3.13")
    assert tag.startswith("nodestro-env:") and tag == built.tag(HASHED, "3.13")
    assert tag != built.tag(HASHED + "\n", "3.13") and tag != built.tag(HASHED, "3.12")
    other_base = ContainerImages(base=lambda version: f"registry.example/python:{version}", run=docker)
    assert other_base.tag(HASHED, "3.13") != tag


# ---- building ----------------------------------------------------------------------------------


def test_a_lock_without_hashes_is_refused_before_docker_runs(docker: FakeDocker) -> None:
    with pytest.raises(NodestroError) as caught:
        images(docker).build(PLAIN, "3.13")
    assert caught.value.code == "deps.lock_without_hashes" and docker.calls == []


def test_the_image_downloads_with_hashes_and_installs_offline(docker: FakeDocker, tmp_path: Path) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("CERT")
    built = images(
        docker,
        HTTPS_PROXY="http://proxy:3128",
        NODESTRO_DOCKER_BUILD_NETWORK="host",
        NODESTRO_BUILD_CA_FILE=str(ca),
        ANTHROPIC_API_KEY="sk-secret",
    )
    tag = built.build(HASHED, "3.13")
    build = next(call for call in docker.calls if call[1] == "build")
    assert build[build.index("-t") + 1] == tag
    assert build[build.index("--network") + 1] == "host"
    assert "HTTPS_PROXY=http://proxy:3128" in build
    context = docker.contexts[0]
    assert context["requirements.txt"] == HASHED and context["ca.pem"] == "CERT"
    dockerfile = context["Dockerfile"]
    download, final = dockerfile.split("\nFROM ", 2)[1:]
    assert "--require-hashes" in download and "--only-binary=:all:" in download and "ca.pem" in download
    assert "--no-index" in final and "--require-hashes" in final and "ca.pem" not in final
    assert "USER 65534:65534" in final and LOCK_LABEL in final
    # The image carries the Orkexa package this process runs (no bind mount of it at call time).
    assert "COPY orkexa /opt/orkexa/orkexa" in final
    assert "orkexa/dynamic/_sandbox_runner.py" in docker.orkexa_files
    # The docker CLI never gets provider keys.
    assert all("ANTHROPIC_API_KEY" not in environment for environment in docker.environments)
    # A second build of the same lock finds the image.
    docker.calls.clear()
    assert built.build(HASHED, "3.13") == tag
    assert [call[1] for call in docker.calls] == ["image"]


@pytest.mark.parametrize(
    ("output", "code"),
    [
        ("ERROR: Could not find a version that satisfies the requirement six==1.17.0", "deps.wheels_required"),
        ("ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.", "deps.hash_mismatch"),
        ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock", "sandbox.unavailable"),
        (
            "failed to connect to the docker API at unix:///var/run/docker.sock; check if the path is correct",
            "sandbox.unavailable",
        ),
        ("something else", "deps.image_failed"),
    ],
)
def test_build_failures_are_explained(docker: FakeDocker, output: str, code: str) -> None:
    docker.build_fails, docker.build_output = True, output
    with pytest.raises(NodestroError) as caught:
        images(docker).build(HASHED, "3.13")
    assert caught.value.code == code


def test_the_sandbox_of_an_image_is_a_docker_sandbox(docker: FakeDocker) -> None:
    sandbox = images(docker).sandbox("nodestro-env:abc")
    assert isinstance(sandbox, DockerSandbox) and sandbox.image == "nodestro-env:abc"
    command = sandbox.command(Path("/tmp/work"), "name")
    assert command[command.index("--network") + 1] == "none" and "--read-only" in command
    assert command[command.index("--pull") + 1] == "never"
    assert command.index("--pull") < command.index("nodestro-env:abc")
    # Nothing of the host is mounted: the files travel on stdin into a tmpfs.
    assert "-v" not in command and "--volume" not in command
    assert any(item.startswith("/work:") for item in command)


def test_the_image_key_names_the_orkexa_package(docker: FakeDocker) -> None:
    from nodestro_runtime import container_images

    built = images(docker)
    tag = built.tag(HASHED, "3.13")
    original = container_images._FINGERPRINT
    container_images._FINGERPRINT = "0" * 64
    try:
        assert built.tag(HASHED, "3.13") != tag  # another Orkexa means another image
    finally:
        container_images._FINGERPRINT = original


# ---- size caps and collection of unused images ---------------------------------------------------

GIB = 1024**3


def _tag(number: int) -> str:
    return f"nodestro-env:{number:032x}"


def _collected(docker: FakeDocker, tmp_path: Path, **environ: str) -> ContainerImages:
    built = images(docker, **environ)
    built.usage_file = tmp_path / "image-usage.json"
    return built


def test_an_image_larger_than_the_limit_is_removed_and_refused(docker: FakeDocker, tmp_path: Path) -> None:
    docker.build_size = 9 * GIB
    built = _collected(docker, tmp_path)
    with pytest.raises(NodestroError) as caught:
        built.build(HASHED, "3.13")
    assert caught.value.code == "deps.image_too_large" and caught.value.params["limit_gb"] == "8"
    assert docker.images == {}  # never left behind for a sandbox
    # The limit is configurable; an image below it is kept.
    assert _collected(docker, tmp_path, NODESTRO_MAX_IMAGE_GB="10").build(HASHED, "3.13") in docker.images


@pytest.mark.parametrize("value", ["0", "-1", "lots", "inf", "nan"])
def test_an_invalid_image_limit_fails_before_building(docker: FakeDocker, tmp_path: Path, value: str) -> None:
    with pytest.raises(NodestroError) as caught:
        _collected(docker, tmp_path, NODESTRO_MAX_IMAGE_GB=value).build(HASHED, "3.13")
    assert caught.value.code == "deps.image_failed"
    assert not [call for call in docker.calls if call[1] == "build"]


def test_gc_removes_the_least_recently_used_images_and_never_foreign_ones(docker: FakeDocker, tmp_path: Path) -> None:
    built = _collected(docker, tmp_path)
    for number in range(5):
        docker.images[_tag(number)] = "a" * 64
        docker.sizes[_tag(number)] = GIB
    docker.images[_tag(99)] = ""  # a foreign image in Nodestro's repository, without the lock label
    docker.sizes[_tag(99)] = 50 * GIB
    built.usage_file.write_text(json.dumps({_tag(number): 1000.0 + number for number in range(5)}))
    built.note_use(_tag(0), force=True)  # used just now: the most recent one
    assert built.gc(max_images=3, keep={_tag(1)}) == [_tag(2), _tag(3)]
    assert set(docker.images) == {_tag(0), _tag(1), _tag(4), _tag(99)}
    usage = json.loads(built.usage_file.read_text())
    assert set(usage) == {_tag(0), _tag(1), _tag(4)}
    # The size cap alone also collects (oldest first), and never below what is kept.
    assert built.gc(max_bytes=GIB, keep={_tag(1)}) == [_tag(4), _tag(0)]
    assert set(docker.images) == {_tag(1), _tag(99)}
    assert not [call for call in docker.calls if call[1:3] == ["image", "rm"] and call[-1] == _tag(99)]


def test_gc_works_from_the_usage_record_where_images_cannot_be_listed(docker: FakeDocker, tmp_path: Path) -> None:
    # Through the sandbox gateway only nodestro-env images can be inspected and removed, not listed.
    docker.list_fails = True
    built = _collected(docker, tmp_path)
    for number in range(3):
        docker.images[_tag(number)] = "a" * 64
    docker.in_use.add(_tag(0))  # a running container still uses it: the daemon refuses, gc goes on
    built.usage_file.write_text(json.dumps({_tag(number): float(number) for number in range(3)} | {"evil:1": 0}))
    assert built.gc(max_images=1) == [_tag(1), _tag(2)]
    assert set(docker.images) == {_tag(0)}
    assert not [call for call in docker.calls if call[1:3] == ["image", "inspect"] and call[-1] == "evil:1"]


def test_a_build_collects_unused_images_but_keeps_what_environments_name(docker: FakeDocker, tmp_path: Path) -> None:
    built = _collected(docker, tmp_path)
    for number in range(25):
        docker.images[_tag(number)] = "a" * 64
    built.keep = lambda: {_tag(0)}
    tag = built.build(HASHED, "3.13")
    assert tag in docker.images and _tag(0) in docker.images
    assert len([item for item, label in docker.images.items() if label]) == 20
    # Collection off (a server): nothing is removed.
    docker.images[_tag(30)] = "a" * 64
    server = _collected(docker, tmp_path)
    server.collect = False
    server.build(HASHED + "\n", "3.13")
    assert _tag(30) in docker.images


def test_a_failing_collection_never_fails_the_build(docker: FakeDocker, tmp_path: Path) -> None:
    built = _collected(docker, tmp_path)

    def broken() -> set[str]:
        raise NodestroError("sandbox.unavailable", "gone")

    built.keep = broken
    assert built.build(HASHED, "3.13") in docker.images


def test_the_gateway_shares_the_image_names_and_limits() -> None:
    from nodestro_runtime import image_limits, sandbox_gateway

    config = sandbox_gateway.GatewayConfig()
    assert (config.max_images, config.max_images_gb) == (32, 20.0)  # unchanged
    assert sandbox_gateway._LOCK_LABEL == LOCK_LABEL == image_limits.LOCK_LABEL
    assert config.max_images > image_limits.GC_MAX_IMAGES and config.max_images_gb >= image_limits.GC_MAX_GB


def test_environments_keep_their_images_when_unused_ones_are_collected(
    workspace: Workspace, docker: FakeDocker
) -> None:
    scope = _approve(workspace)
    python = workspace.python
    env = python.get(scope)
    assert env is not None
    tag = python.images.tag(env.lock, env.python_version)
    assert python.images.usage_file == python.root / "image-usage.json"
    for number in range(30):
        docker.images[_tag(number)] = "a" * 64
    removed = python.images.gc(max_images=0)
    assert len(removed) == 30 and tag not in removed
    assert list(docker.images) == [tag]


# ---- resolving with source builds in a container (no build scripts on the host) -----------------

PINNED_UV = "ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:" + "ab" * 32


def test_the_resolver_image_is_pinned_by_digest() -> None:
    from nodestro_runtime.container_images import UV_IMAGE

    name, _, digest = UV_IMAGE.partition("@sha256:")
    assert name.startswith("ghcr.io/astral-sh/uv:") and len(digest) == 64 and set(digest) != {"0"}


def test_resolving_in_a_container_is_refused_for_an_unpinned_or_placeholder_image(docker: FakeDocker) -> None:
    placeholder = "ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:" + "0" * 64
    for image in (placeholder, "ghcr.io/astral-sh/uv:latest"):
        with pytest.raises(NodestroError) as caught:
            images(docker).compile_in_container(["six"], python="3.13", previous="", log=print, image=image)
        assert caught.value.code == "deps.build_unavailable"
    assert docker.calls == []


def test_resolving_in_a_container_is_locked_down_and_uses_only_stdin_and_stdout(
    docker: FakeDocker, tmp_path: Path
) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("CERT")
    built = images(
        docker,
        HTTPS_PROXY="http://user:secret@proxy:3128",
        NODESTRO_BUILD_CA_FILE=str(ca),
        ANTHROPIC_API_KEY="sk-secret",
        AWS_SECRET_ACCESS_KEY="aws-secret",
    )
    lines: list[str] = []
    lock = built.compile_in_container(
        ["six==1.17.0"], python="3.12", previous="six==1.16.0\n", log=lines.append, image=PINNED_UV
    )
    assert lock == HASHED
    ((args, stdin),) = docker.resolves
    run = args[1:]
    image = run.index(PINNED_UV)
    flags, command = run[:image], run[image + 1 :]
    for flag in ("--read-only", "--rm", "-i"):
        assert flag in flags
    pairs = [(flags[index], flags[index + 1]) for index in range(len(flags) - 1)]
    expected = [
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--user", "65534:65534"),
        ("--pull", "missing"),
        ("--env", "HTTPS_PROXY"),
    ]
    for pair in expected:
        assert pair in pairs, pair
    assert [flag for flag in flags if flag in ("-v", "--volume", "--mount", "--privileged", "--env-file")] == []
    # Network only for this step (Docker's default), but nothing of the host: the only variable is a proxy, by name.
    assert "--network" not in flags
    assert [flags[index + 1] for index, flag in enumerate(flags) if flag in ("--env", "-e")] == ["HTTPS_PROXY"]
    assert not any("secret" in item for item in args)
    tmpfs = [flags[index + 1] for index, flag in enumerate(flags) if flag == "--tmpfs"]
    assert len(tmpfs) == 1 and tmpfs[0].startswith("/tmp:")
    assert command[:4] == ["python", "-I", "-B", "-c"]
    envelope = json.loads(stdin)
    assert envelope == {"requirements": "six==1.17.0\n", "previous": "six==1.16.0\n", "python": "3.12", "ca": "CERT"}
    # The docker CLI gets the proxy (to pass it on) but no provider or cloud keys.
    environment = docker.environments[-1]
    assert environment["HTTPS_PROXY"] and "ANTHROPIC_API_KEY" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    compile(command[4], "<resolve>", "exec")  # the script that runs in the container is valid Python


def test_a_failed_resolve_in_a_container_carries_uv_s_explanation(docker: FakeDocker) -> None:
    from nodestro_runtime.container_images import ResolveError
    from nodestro_runtime.python_envs import explain_uv_error

    docker.resolve_error = "No solution found: Because nothere was not found in the package registry"
    with pytest.raises(ResolveError) as caught:
        images(docker).compile_in_container(["nothere"], python="3.13", previous="", log=print, image=PINNED_UV)
    assert explain_uv_error(caught.value.output).code == "deps.not_found"
    docker.resolve_error = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    with pytest.raises(NodestroError) as down:
        images(docker).compile_in_container(["six"], python="3.13", previous="", log=print, image=PINNED_UV)
    assert down.value.code == "sandbox.unavailable"


def test_an_accepted_source_build_is_resolved_in_a_container(
    workspace: Workspace, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nodestro_runtime import container_images

    monkeypatch.setattr(container_images, "UV_IMAGE", PINNED_UV)
    python = workspace.python
    runner = python.runner
    assert isinstance(runner, FakeUv)
    runner.source_only = {"tablib"}
    needed = python.needed_for_sources([("show", TOOL, "")])
    # A generated tool's plan (nobody accepted a build) stops at deps.build_required; nothing runs in a container.
    unbuilt = python.plan("tool", "show", needed, builds=False)
    assert unbuilt.error is not None and unbuilt.error.code == "deps.build_required" and docker.resolves == []
    docker.resolve_lock = (
        "tablib==1.0 \\\n    --hash=sha256:" + "1" * 64 + "\n    # via -r requirements.in\n"
        "pydantic==2.0.0 \\\n    --hash=sha256:" + "2" * 64 + "\n    # via -r requirements.in\n"
    )
    plan = python.plan("tool", "show", needed)
    assert runner.builds == [False, False]  # uv on the host never builds
    assert plan.error is None and plan.lock == docker.resolve_lock and len(docker.resolves) == 1
    assert {item.name for item in plan.packages} >= {"tablib"}
    envelope = json.loads(docker.resolves[0][1])
    assert "tablib" in envelope["requirements"]


def test_without_docker_an_accepted_source_build_stays_refused(
    workspace: Workspace, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nodestro_runtime import container_images

    monkeypatch.setattr(container_images, "UV_IMAGE", PINNED_UV)
    python = workspace.python
    assert isinstance(python.runner, FakeUv)
    python.runner.source_only = {"tablib"}
    docker.resolve_error = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    plan = python.plan("tool", "show", python.needed_for_sources([("show", TOOL, "")]))
    assert plan.error is not None and plan.error.code == "deps.build_required"
    assert plan.error.params == {"name": "tablib", "reason": "sandbox.unavailable"} and not plan.lock


# ---- environments in the Docker sandbox -------------------------------------------------------

TOOL = '''# /// script
# dependencies = ["tablib"]
# ///
import tablib


def show(text: str) -> str:
    """Show text."""
    return tablib.show(text)
'''
INDEX = {"tablib": [("1.0", [], "tablib\ndef show(text):\n    return 'T:' + text\n")]}


@pytest.fixture
def workspace(tmp_path: Path, docker: FakeDocker) -> Iterator[Workspace]:
    opened = Workspace(tmp_path / "data", python_runner=FakeUv(INDEX), integrations=[])
    opened.python.sandbox_kind = lambda: "docker"
    opened.python.images = images(docker)
    yield opened
    opened.close()


def _approve(workspace: Workspace) -> str:
    python = workspace.python
    needed = python.needed_for_sources([("show", TOOL, "")])
    plan = python.plan("tool", "show", needed)
    python.approve(plan.id, wait=True)
    return plan.scope


def test_with_docker_an_environment_is_a_container_image(workspace: Workspace, docker: FakeDocker) -> None:
    python = workspace.python
    assert python.isolation() == "container"
    needed = python.needed_for_sources([("show", TOOL, "")])
    assert python.status("tool", "show", needed).state == "approval_needed"  # no more deps.docker_unsupported
    scope = _approve(workspace)
    status = python.status("tool", "show", needed)
    assert status.state == "ready", status
    env = python.get(scope)
    assert env is not None
    tag = python.images.tag(env.lock, env.python_version)
    assert tag in docker.images
    assert not python.env_dir(scope).exists()  # no host virtual environment
    sandbox = python.sandbox(scope)
    assert isinstance(sandbox, DockerSandbox) and not isinstance(sandbox, SubprocessSandbox)
    assert sandbox.image == tag
    check = next(call for call in docker.calls if call[1] == "run")
    assert check[check.index("--network") + 1] == "none"
    assert env.history[-1].kind == "installed"


def test_a_missing_module_in_the_image_fails_the_installation(workspace: Workspace, docker: FakeDocker) -> None:
    docker.missing = ["tablib"]
    scope = _approve(workspace)
    env = workspace.python.get(scope)
    assert env is not None and env.install.status == "failed"
    assert env.install.error is not None and env.install.error.code == "deps.module_missing"
    assert workspace.python.sandbox(scope) is None


def test_an_environment_approved_without_hashes_must_be_approved_again(
    workspace: Workspace, docker: FakeDocker
) -> None:
    scope = _approve(workspace)
    python = workspace.python
    env = python.get(scope)
    assert env is not None
    # As stored by an earlier release: the same versions, no hashes.
    env.lock = "\n".join(line for line in env.lock.splitlines() if "--hash" not in line).replace(" \\", "") + "\n"
    python._put(env)
    needed = python.needed_for_sources([("show", TOOL, "")])
    status = python.status("tool", "show", needed)
    assert status.state == "approval_needed" and "hashes" in status.reasons
    assert python.sandbox(scope) is None


def test_without_docker_nothing_is_ready_and_nothing_falls_back(workspace: Workspace, docker: FakeDocker) -> None:
    scope = _approve(workspace)
    docker.images.clear()  # the image was deleted (or Docker is gone)
    python = workspace.python
    python._images_ready.clear()
    needed = python.needed_for_sources([("show", TOOL, "")])
    assert python.status("tool", "show", needed).state == "install_needed"
    assert python.sandbox(scope) is None  # never a subprocess sandbox


def test_removing_an_environment_removes_its_image(workspace: Workspace, docker: FakeDocker) -> None:
    scope = _approve(workspace)
    assert docker.images
    workspace.python.remove_environment(scope)
    assert docker.images == {}


# ---- real Docker ---------------------------------------------------------------------------------

DOCKER_TESTS = os.environ.get("NODESTRO_DOCKER_TESTS") == "1"


def _require_docker() -> None:
    if not DOCKER_TESTS:
        if os.environ.get("NODESTRO_REQUIRE_DOCKER") == "1":
            pytest.fail("NODESTRO_REQUIRE_DOCKER=1 but NODESTRO_DOCKER_TESTS is not set")
        pytest.skip("set NODESTRO_DOCKER_TESTS=1 (needs Docker and network for the image build)")
    if not DockerSandbox.available():
        pytest.fail("NODESTRO_DOCKER_TESTS=1 but Docker is not available")


REAL_TOOL = '''import six
import socket


def probe(host: str) -> str:
    """Report the library version, network and file system access.

    Args:
        host: A host to try to reach.

    Returns:
        The version, then "network" or "no-network", then "write" or "read-only".
    """
    result = [six.__version__]
    try:
        socket.create_connection((host, 443), timeout=3)
        result.append("network")
    except OSError:
        result.append("no-network")
    try:
        open("/opt/escape.txt", "w").write("x")
        result.append("write")
    except OSError:
        result.append("read-only")
    return " ".join(result)
'''


@pytest.fixture
def real_images() -> Iterator[Callable[[str], str]]:
    _require_docker()
    built = ContainerImages()
    tags: list[str] = []

    def build(lock: str) -> str:
        tag = built.build(lock, f"{sys.version_info.major}.{sys.version_info.minor}")
        tags.append(tag)
        return tag

    yield build
    for tag in tags:
        built.remove(tag)


def test_a_tool_with_a_library_runs_in_its_image_without_network(real_images: Callable[[str], str]) -> None:
    from nodestro_runtime.python_envs import UvRunner, find_uv

    uv = find_uv()
    assert uv is not None, "uv is a dependency of the runtime"

    with tempfile.TemporaryDirectory() as cache:
        lock = UvRunner(Path(cache), uv).compile(
            ["six==1.17.0", "pydantic>=2.6,<3", "typing-extensions>=4.14"],
            python=f"{sys.version_info.major}.{sys.version_info.minor}",
            previous="",
            log=lambda _line: None,
        )
    assert lock_has_hashes(lock)
    tag = real_images(lock)
    images = ContainerImages()
    assert images.missing_modules(tag, ["six", "pydantic"]) == []
    sandbox = images.sandbox(tag)
    result = sandbox.call(REAL_TOOL, "probe", {"host": "pypi.org"})
    assert result == "1.17.0 no-network read-only", result


def test_a_missing_image_fails_closed() -> None:
    _require_docker()
    sandbox = ContainerImages().sandbox("nodestro-env:does-not-exist")
    with pytest.raises(ToolExecutionError) as raised:
        sandbox.call(REAL_TOOL, "probe", {"host": "pypi.org"})
    assert "pull access denied" not in str(raised.value)  # never looked for in a registry


# ---- the runtime image of a server -------------------------------------------------------------


def test_the_runtime_image_is_built_once_from_a_hashed_lock(tmp_path: Path, docker: FakeDocker) -> None:
    from nodestro_runtime import python_envs

    opened = Workspace(
        tmp_path / "data", python_runner=FakeUv({"pydantic": [("2.0", [], "pydantic\n")]}), integrations=[]
    )
    try:
        opened.python.images = images(docker)
        python_envs._RUNTIME_IMAGES.clear()
        tag = opened.python.runtime_image()
        assert tag.startswith("nodestro-env:") and tag in docker.images
        assert lock_has_hashes(docker.contexts[0]["requirements.txt"])
        builds = [call for call in docker.calls if call[1] == "build"]
        assert opened.python.runtime_image() == tag  # found, not built again
        assert [call for call in docker.calls if call[1] == "build"] == builds
        sandbox = opened.python.runtime_sandbox()
        assert sandbox.isolated and sandbox.image == ""  # resolved on the first call
    finally:
        python_envs._RUNTIME_IMAGES.clear()
        opened.close()


def test_a_runtime_image_that_cannot_be_built_fails_the_call(docker: FakeDocker) -> None:
    from nodestro_runtime.container_images import RuntimeImageSandbox

    def refuse() -> str:
        raise NodestroError("sandbox.unavailable", "Docker is not running")

    sandbox = RuntimeImageSandbox(refuse)
    with pytest.raises(ToolExecutionError) as raised:
        sandbox.call("def f(x: int) -> int:\n    return x\n", "f", {"x": 1})
    assert "Docker is not running" in str(raised.value)


PLAIN_TOOL = '''def double(value: int) -> int:
    """Double a number.

    Args:
        value: The number.

    Returns:
        Twice the number.
    """
    return value * 2
'''

PLAIN_TESTS = """from tool import double


def test_double():
    assert double(2) == 4
"""


def test_a_server_runs_plain_tools_in_its_runtime_image(tmp_path: Path) -> None:
    _require_docker()
    from nodestro_runtime import python_envs

    opened = Workspace(tmp_path / "data", integrations=[])
    try:
        sandbox = opened.python.runtime_sandbox()
        validation = sandbox.validate(PLAIN_TOOL, PLAIN_TESTS, "double")
        assert validation.ok, validation.error
        assert [test.ok for test in validation.tests] == [True]
        assert sandbox.call(PLAIN_TOOL, "double", {"value": 21}) == "42"
        assert sandbox.image.startswith("nodestro-env:")
    finally:
        ContainerImages().remove(python_envs._RUNTIME_IMAGES.pop(next(iter(python_envs._RUNTIME_IMAGES)), ""))
        opened.close()


def test_a_local_mcp_server_gets_its_packages_through_the_same_approval(
    workspace: Workspace, docker: FakeDocker
) -> None:
    """WP-8: a stdio server's packages are planned, approved and built into a sandbox image like a tool's; the
    server starts only in that image, and removing the server removes its environment."""
    pytest.importorskip("mcp")
    from nodestro_runtime.mcp.models import McpServerIn, StdioCommand

    mcp = workspace.mcp
    assert mcp.python is workspace.python
    server = mcp.add(
        McpServerIn(name="Tables", transport="stdio", stdio=StdioCommand(requirements=["tablib"], module="tablib"))
    )
    assert mcp.environment(server.id).state == "approval_needed"
    with pytest.raises(NodestroError) as caught:
        mcp.stdio_launch(server)
    assert caught.value.code == "mcp.environment_not_ready"
    plan = mcp.plan_environment(server.id)
    assert plan.scope == f"mcp:{server.id}" and plan.needs_approval
    workspace.python.approve(plan.id, wait=True)
    assert mcp.environment(server.id).state == "ready"
    assert mcp.view(mcp.server(server.id)).environment_state == "ready"
    env = workspace.python.get(plan.scope)
    assert env is not None
    tag = workspace.python.images.tag(env.lock, env.python_version)
    started = mcp.stdio_launch(server)
    run = started.args[started.args.index("--") + 1 :]
    assert tag in run and run[run.index("--network") + 1] == "none"
    assert [item.scope for item in workspace.python.python_status().environments if item.orphaned] == []
    mcp.remove(server.id)
    assert workspace.python.get(plan.scope) is None


def test_without_docker_a_local_mcp_server_is_refused(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from nodestro_runtime.mcp.models import McpServerIn, StdioCommand

    opened = Workspace(tmp_path / "data", python_runner=FakeUv(INDEX), integrations=[])
    try:
        assert not opened.python.containers()
        with pytest.raises(NodestroError) as caught:
            opened.mcp.add(
                McpServerIn(name="T", transport="stdio", stdio=StdioCommand(requirements=["tablib"], module="tablib"))
            )
        assert caught.value.code == "sandbox.isolation_required"
    finally:
        opened.close()


def test_a_streamed_sandbox_bounds_what_the_host_reads(tmp_path: Path) -> None:
    # The container's stdout is read with a limit: code that floods it past the runner is stopped and its
    # container removed, so the API or worker process never buffers more than the response limit.
    class Flooding(StreamedSandbox):
        def command(self, workdir: Path, container_name: str) -> list[str]:
            flood = "import os, sys\nsys.stdin.read()\nblock = b'x' * 1048576\nfor _ in range(64): os.write(1, block)\n"
            return [sys.executable, "-c", flood]

    removed: list[list[str]] = []
    real_run = subprocess.run

    def record(args: list[str], *rest: object, **options: object) -> object:
        if args[1:3] == ["rm", "-f"]:
            removed.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(args, *rest, **options)  # type: ignore[call-overload]

    sandbox = Flooding(image="nodestro-test", max_output_chars=10, timeout=20)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subprocess, "run", record)
        with pytest.raises(ToolExecutionError, match="more than 1048696 bytes of output"):
            sandbox.call("def f() -> str:\n    return 'x'\n", "f", {})
    assert len(removed) == 1 and removed[0][3].startswith("nodestro-sandbox-")
