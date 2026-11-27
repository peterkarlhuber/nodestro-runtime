"""Tools of installed integrations run only in the Docker sandbox (N-22, ``docs/extension-isolation.md``).

The tests without Docker use a real installed-looking distribution in a temporary directory (a ``.dist-info`` with
``RECORD`` and ``entry_points.txt``) and, where a container would run, :class:`FakeContainer`: it runs the real
bootstrap and extension runner in a child process (no isolation; tests only), so the protocol, the contract module and
the host-side checks are the product's. What the container itself enforces (no network, no host files, no secrets,
limits, the time limit removing it) is tested on a real engine at the end (``NODESTRO_DOCKER_TESTS=1``, CI job
``docker``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
from typing import Any
import uuid

from orkexa import Tool, ToolResultError
from orkexa.dynamic import BoundedOutput, SandboxError
from orkexa.errors import ToolExecutionError
import pytest

from nodestro_runtime.extension_sandbox import (
    ExtensionPackage,
    ExtensionSandbox,
    ExtensionTool,
    PackageError,
    distribution_files,
    extension_spec,
    floor_capability,
    package_of,
)
from nodestro_runtime.extensions import (
    EXTENSION_TOOLS_ENV,
    HOST_INSTALLED,
    LOCKED_MESSAGE,
    Extensions,
    discover,
    find_packages,
)
from nodestro_runtime.profile import RuntimeProfile
from nodestro_runtime.tool_policy import UNKNOWN
from nodestro_runtime.tool_spec import HOST_TOOL_TIMEOUT_SECONDS, CallCounter, bind_tool, offered
from nodestro_runtime.workspace import Workspace

MODULE = "acme_ext_tools"

#: A foreign integration: import-time code that leaves a marker, a read-only claim, a destructive claim, a long time
#: limit and probes for what the container may reach.
PACKAGE_SOURCE = '''
import json
import os
import socket

from orkexa import Tool
from nodestro_runtime.extensions import Integration
from nodestro_runtime.tool_policy import ToolCapability
from nodestro_runtime.tool_spec import ToolSpec

MARKER = {marker!r}
try:  # import-time code: must never run in the host (in a container the host's directory does not exist)
    with open(MARKER, "w") as handle:
        handle.write("imported")
except OSError:
    pass


def echo(text: str) -> str:
    """Echo text.

    Args:
        text: Any text.

    Returns:
        The same text.
    """
    return text


def wipe(target: str) -> str:
    """Claims to delete something.

    Args:
        target: What.

    Returns:
        Done.
    """
    return "done " + target


def spin(seconds: float) -> str:
    """Busy loop (seconds < 0: forever).

    Args:
        seconds: How long.

    Returns:
        Never, when forever.
    """
    import time

    end = time.monotonic() + seconds
    while seconds < 0 or time.monotonic() < end:
        pass
    return "spun"


def probe(check: str) -> str:
    """Report what the container can reach.

    Args:
        check: The check to run.

    Returns:
        A JSON report.
    """
    return json.dumps(_probe(check))


def _try(action):
    try:
        return {{"ok": True, "value": action()}}
    except BaseException as exc:
        return {{"ok": False, "error": type(exc).__name__}}


def _read(path):
    with open(path, "rb") as handle:
        return handle.read(200).decode("utf-8", "replace")


def _probe(check):
    if check == "environment":
        return {{"environ": dict(os.environ), "proc1": _try(lambda: _read("/proc/1/environ"))}}
    if check == "files":
        paths = json.loads(os.environ.get("PROBE_PATHS", "[]")) or {paths!r}
        return {{path: os.path.exists(path) for path in paths}}
    if check == "network":
        def tcp(host, port, family=socket.AF_INET):
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(3)
                sock.connect((host, port))
            return "connected"

        def udp():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(3)
                sock.sendto(b"x", ("1.1.1.1", 53))
                return sock.recv(10)

        return {{
            "tcp": _try(lambda: tcp("1.1.1.1", 443)),
            "dns": _try(lambda: socket.getaddrinfo("pypi.org", 443)),
            "udp": _try(udp),
            "metadata": _try(lambda: tcp("169.254.169.254", 80)),
            "ipv6": _try(lambda: tcp("2606:4700:4700::1111", 443, socket.AF_INET6)),
            "interfaces": sorted(name for _index, name in socket.if_nameindex()),
        }}
    if check == "resources":
        def fork():
            pid = os.fork()
            if pid == 0:
                os._exit(0)
            return pid

        def status():
            lines = _read_all("/proc/self/status").splitlines()
            return {{line.split(":")[0]: line.split(":", 1)[1].strip() for line in lines if ":" in line}}

        found = status()
        return {{
            "docker_socket": os.path.exists("/var/run/docker.sock") or os.path.exists("/run/docker.sock"),
            "uid": os.getuid(),
            "setuid": _try(lambda: os.setuid(0)),
            "cap_eff": found.get("CapEff"),
            "no_new_privs": found.get("NoNewPrivs"),
            "pids_visible": len([name for name in os.listdir("/proc") if name.isdigit()]),
            "proc1": _try(lambda: _read("/proc/1/cmdline")),
            "devices": sorted(os.listdir("/dev")),
            "disk": _try(lambda: _read("/dev/sda")),
            "mknod": _try(lambda: os.mknod("/tmp/node", 0o600 | 0o060000, os.makedev(8, 0))),
            "fork": _try(fork),
        }}
    if check == "memory":
        return {{"memory": _try(lambda: len(bytearray(4 * 1024 * 1024 * 1024)))}}
    if check == "huge":
        os.write(1, b"x" * (64 * 1024 * 1024))
        return {{}}
    raise ValueError(check)


def _read_all(path):
    with open(path) as handle:
        return handle.read()


integration = Integration(
    name="acme",
    tools=[
        (ToolSpec(id="echo_text", capability=ToolCapability(access="read", reach="internal", source="author"),
                  timeout_seconds=5000.0), Tool(echo, name="echo_text")),
        (ToolSpec(id="wipe_all", capability=ToolCapability(access="destructive", reach="internal", source="author"),
                  timeout_seconds=7.0), Tool(wipe, name="wipe_all")),
        Tool(spin, name="spin_loop"),
        Tool(probe, name="probe_sandbox"),
    ],
)
'''


def install_package(root: Path, *, marker: Path, paths: list[str] | None = None, name: str = "acme-ext") -> Path:
    """A distribution as pip installs it into ``root`` (site-packages): the module, its ``.dist-info`` with
    ``RECORD`` and an entry point in ``nodestro.integrations``. Returns the ``.dist-info`` directory."""
    package = root / MODULE
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        textwrap.dedent(PACKAGE_SOURCE).format(marker=str(marker), paths=paths or []), encoding="utf-8"
    )
    info = root / f"{name.replace('-', '_')}-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n", encoding="utf-8")
    (info / "entry_points.txt").write_text(f"[nodestro.integrations]\nacme = {MODULE}:integration\n", encoding="utf-8")
    (info / "RECORD").write_text(
        f"{MODULE}/__init__.py,,\n{info.name}/METADATA,,\n{info.name}/RECORD,,\n{info.name}/entry_points.txt,,\n",
        encoding="utf-8",
    )
    return info


def entry_points_of(info: Path) -> Callable[[], list[metadata.EntryPoint]]:
    dist = metadata.PathDistribution(info)
    return lambda: list(dist.entry_points)


def store_packages(info: Path) -> list[ExtensionPackage]:
    """The package at ``info`` as the extension store will hand it to the sandbox (owner decision 2026-11-27: a
    package found in the host environment is refused, so the sandbox tests below get theirs from this stand-in)."""
    return [package_of(entry) for entry in entry_points_of(info)()]


class FakeContainer:
    """Stands in for :class:`~nodestro_runtime.container_images.RuntimeImageSandbox`: ``run_container`` runs the
    command after the image (``python -I -B -c BOOTSTRAP``) as a child process with an empty environment, in a fresh
    work directory and temporary directory. No isolation: tests only."""

    docker = "docker"
    environ: dict[str, str] | None = None

    def __init__(self, timeout: float, *, ready: bool = True) -> None:
        self.timeout = timeout
        self.is_ready = ready
        self.runs: list[dict[str, Any]] = []

    def ready_image(self) -> str:
        if not self.is_ready:
            raise SandboxError("The Docker sandbox is not available (self-test): no seccomp")
        return "sha256:" + "0" * 64

    def shaped_run(self, name: str, image_id: str) -> list[str]:
        return ["docker", "run", "--name", name, image_id]

    def run_container(self, args: list[str], name: str, data: bytes = b"", *, max_stdout: int) -> tuple[int, Any]:
        command = args[args.index("python") :]
        self.runs.append({"name": name, "request": json.loads(data)["request"]})
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as tmp:
            try:
                done = subprocess.run(
                    [sys.executable, *command[1:]],
                    input=data,
                    capture_output=True,
                    cwd=work,
                    env={"TMPDIR": tmp},
                    timeout=self.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise SandboxError(f"Timed out after {self.timeout:g} seconds") from None
        if len(done.stdout) > max_stdout:
            raise SandboxError(f"The sandbox wrote more than {max_stdout} bytes of output; the run was stopped")
        return done.returncode, BoundedOutput(stdout=done.stdout, stderr=done.stderr[-4096:], overflow=False)


@pytest.fixture(autouse=True)
def docker_cli(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A docker CLI for :class:`FakeContainer`: the sandbox confirms a stopped container's removal with ``docker ps``
    (and ``docker rm``), which must not reach a real CLI or daemon (review L3). It lists no container and records each
    call in the returned log, one line per call."""
    folder = tmp_path_factory.mktemp("docker-cli")
    log = folder / "calls.log"
    script = folder / "docker.py"
    script.write_text(
        "import sys\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as out:\n"
        "    out.write(' '.join(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        cli = folder / "docker.cmd"
        cli.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        cli = folder / "docker"
        cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        cli.chmod(0o755)
    monkeypatch.setattr(FakeContainer, "docker", str(cli))
    return log


class Containers:
    """The factory a workspace hands to :class:`ExtensionSandbox`, recording each sandbox it made."""

    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.made: list[FakeContainer] = []

    def __call__(self, timeout: float) -> FakeContainer:
        made = FakeContainer(timeout, ready=self.ready)
        self.made.append(made)
        return made

    @property
    def runs(self) -> list[dict[str, Any]]:
        return [run for made in self.made for run in made.runs]


@pytest.fixture
def package(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """``(site, marker)``: an installed foreign distribution and the file its import-time code would write."""
    site = tmp_path / "site"
    marker = tmp_path / "imported-in-host.txt"
    install_package(site, marker=marker)
    yield site, marker
    sys.modules.pop(MODULE, None)


def _workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: Path,
    containers: Containers,
    *,
    switch: str | None,
    kind: str = "desktop",
) -> Workspace:
    if switch is None:
        monkeypatch.delenv(EXTENSION_TOOLS_ENV, raising=False)
    else:
        monkeypatch.setenv(EXTENSION_TOOLS_ENV, switch)
    info = next(site.glob("*.dist-info"))
    monkeypatch.setattr("nodestro_runtime.workspace.find_packages", lambda **_options: store_packages(info))
    monkeypatch.setattr("nodestro_runtime.workspace.discover", lambda **_options: discover(entry_points_of(info)))
    monkeypatch.setattr(Workspace, "_extension_sandbox", lambda _self, timeout: containers(timeout))
    return Workspace(profile=RuntimeProfile(kind=kind, data_dir=tmp_path / "data"))  # type: ignore[arg-type]


# ---- the host never imports a foreign package -----------------------------------------------------------------------


def test_import_time_code_never_runs_in_the_host(package: tuple[Path, Path]) -> None:
    site, marker = package
    entries = entry_points_of(next(site.glob("*.dist-info")))
    assert discover(entries) == []  # only the trusted distribution is imported
    found = find_packages(entries)
    assert [(item.name, item.entry, item.distribution, item.version) for item in found] == [
        ("acme", f"{MODULE}:integration", "acme-ext", "1.0")
    ]
    assert found[0].rejected == HOST_INSTALLED  # owner decision 2026-11-27: reported from its metadata only
    with pytest.raises(PackageError, match="installed in the host environment"):
        found[0].read_files()
    assert list(store_packages(next(site.glob("*.dist-info")))[0].read_files()) == [f"{MODULE}/__init__.py"]
    assert not marker.exists() and MODULE not in sys.modules


@pytest.mark.parametrize("switch", [None, "1"])
def test_a_foreign_package_in_the_host_environment_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: tuple[Path, Path], switch: str | None
) -> None:
    """Owner decision 2026-11-27: a foreign extension package found in the host environment is refused as "installed
    in the host environment", also with the switch on and a proven sandbox: never offered, imported, read or sent to
    the sandbox (no container starts)."""
    site, marker = package
    if switch is None:
        monkeypatch.delenv(EXTENSION_TOOLS_ENV, raising=False)
    else:
        monkeypatch.setenv(EXTENSION_TOOLS_ENV, switch)
    info = next(site.glob("*.dist-info"))
    monkeypatch.setattr(
        "nodestro_runtime.workspace.find_packages", lambda **options: find_packages(entry_points_of(info), **options)
    )
    monkeypatch.setattr("nodestro_runtime.workspace.discover", lambda **_options: discover(entry_points_of(info)))
    containers = Containers()
    monkeypatch.setattr(Workspace, "_extension_sandbox", lambda _self, timeout: containers(timeout))
    read: list[str] = []
    monkeypatch.setattr("nodestro_runtime.extension_sandbox.distribution_files", lambda *args: read.append("x") or {})
    profile = RuntimeProfile(kind="desktop", data_dir=tmp_path / "data")  # type: ignore[arg-type]
    with Workspace(profile=profile) as workspace:
        assert containers.made == [] and containers.runs == []
        assert not ({"echo_text", "probe_sandbox"} & set(workspace.tools.builtin))
        assert workspace.tools.extension_tools == frozenset()
        [status] = workspace.extensions.packages_status()
        assert (status["name"], status["distribution"], status["version"]) == ("acme", "acme-ext", "1.0")
        assert (status["state"], status["reason"], status["detail"]) == ("locked", "invalid", HOST_INSTALLED)
        assert status["tools"] == []
    assert read == [] and not marker.exists() and MODULE not in sys.modules


@pytest.mark.parametrize("switch", [None, "0", "true", "yes"])
def test_switch_off_offers_no_tool_and_starts_no_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: tuple[Path, Path], switch: str | None
) -> None:
    site, marker = package
    containers = Containers()
    with _workspace(tmp_path, monkeypatch, site, containers, switch=switch) as workspace:
        assert containers.made == [] and containers.runs == []
        assert not ({"echo_text", "probe_sandbox"} & set(workspace.tools.builtin))
        assert workspace.tools.extension_tools == frozenset()
        [status] = workspace.extensions.packages_status()
        assert status["state"] == "locked" and status["reason"] == "off" and status["message"] == LOCKED_MESSAGE
        assert workspace.extensions.summary()[-1]["sandbox"] is True
    assert not marker.exists() and MODULE not in sys.modules


@pytest.mark.parametrize("kind", ["desktop", "local-container"])
def test_no_extension_tool_in_a_product_profile_without_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: tuple[Path, Path], kind: str
) -> None:
    """The switch is off unless set: no extension tool can be bound or called, no container starts. The server
    profile never offers extension tools (their modes), switch or not."""
    site, marker = package
    containers = Containers()
    with _workspace(tmp_path, monkeypatch, site, containers, switch=None, kind=kind) as workspace:
        assert workspace.profile.kind == kind
        assert "echo_text" not in workspace.tools.builtin and workspace.tools.extension_tools == frozenset()
        with pytest.raises(KeyError):
            workspace.tools.get("echo_text")
    assert containers.runs == [] and not marker.exists() and MODULE not in sys.modules
    assert not offered(extension_spec("echo_text", {}), "server")


def test_without_a_proven_sandbox_the_tools_stay_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: tuple[Path, Path]
) -> None:
    site, marker = package
    containers = Containers(ready=False)
    with _workspace(tmp_path, monkeypatch, site, containers, switch="1") as workspace:
        assert containers.runs == []  # nothing ran, not even the listing
        assert "echo_text" not in workspace.tools.builtin
        [status] = workspace.extensions.packages_status()
        assert (status["state"], status["reason"], status["message"]) == ("locked", "not_secured", LOCKED_MESSAGE)
        assert "self-test" in str(status["detail"])
    assert not marker.exists() and MODULE not in sys.modules  # no host fallback


def test_the_switch_and_a_proven_sandbox_offer_the_tools_from_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: tuple[Path, Path]
) -> None:
    site, _marker = package
    containers = Containers()
    with _workspace(tmp_path, monkeypatch, site, containers, switch="1") as workspace:
        assert [run["request"]["mode"] for run in containers.runs] == ["describe"]
        names = {"echo_text", "wipe_all", "spin_loop", "probe_sandbox"}
        assert workspace.tools.extension_tools == names and names <= set(workspace.tools.builtin)
        info = workspace.tools.get("echo_text")
        assert (info.kind, info.trust, info.capability) == ("builtin", "untrusted", UNKNOWN)
        assert workspace.extensions.packages_status()[0]["state"] == "sandboxed"
        tool = workspace.tools.builtin["echo_text"]
        assert isinstance(tool, ExtensionTool)
        assert tool.execute({"text": "hello"}) == "hello"
        assert containers.runs[-1]["request"] == {
            "mode": "call",
            "entry": f"{MODULE}:integration",
            "tool": "echo_text",
            "payload": {"text": "hello"},
        }
        # The tool library survives a reload with the extension tools.
        workspace.tools.reload()
        assert workspace.tools.get("wipe_all").capability.access == "destructive"
    # The package was imported in FakeContainer's child process only (which has no isolation, so it could write the
    # marker), never in this process.
    assert MODULE not in sys.modules


def test_a_package_cannot_shadow_a_built_in_or_stored_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: tuple[Path, Path]
) -> None:
    site, _marker = package
    source = (site / MODULE / "__init__.py").read_text(encoding="utf-8")
    (site / MODULE / "__init__.py").write_text(source.replace('name="echo_text"', 'name="calculator"'), "utf-8")
    containers = Containers()
    with _workspace(tmp_path, monkeypatch, site, containers, switch="1") as workspace:
        [status] = workspace.extensions.packages_status()
        assert status["reason"] == "invalid" and "calculator" in str(status["detail"])
        assert workspace.tools.extension_tools == frozenset()
        assert not isinstance(workspace.tools.builtin["calculator"], ExtensionTool)


# ---- package files, keys and modes (review F2 to F5) ------------------------------------------------------------


def test_a_linked_package_folder_is_refused(tmp_path: Path) -> None:
    site = tmp_path / "site"
    info = install_package(site, marker=tmp_path / "m")
    outside = tmp_path / "host-files"
    (site / MODULE).rename(outside)
    (site / MODULE).symlink_to(outside, target_is_directory=True)
    with pytest.raises(PackageError, match="symbolic link"):
        distribution_files(metadata.PathDistribution(info), f"{MODULE}:integration")


def test_packages_are_told_apart_by_distribution_name_and_entry() -> None:
    def made(distribution: str, name: str = "tools") -> ExtensionPackage:
        return ExtensionPackage(name, "mod:integration", distribution, "1", read_files=lambda: {"mod.py": ""})

    one, other, again = made("one-dist"), made("other-dist"), made("one-dist")
    assert one.key != other.key
    states = {(row["distribution"], row["reason"]) for row in Extensions(packages=[one, other]).packages_status()}
    assert states == {("one-dist", "off"), ("other-dist", "off")}
    twice = Extensions(packages=[one, again]).packages_status()
    assert [row["reason"] for row in twice] == ["invalid", "invalid"]


def test_a_server_never_starts_extension_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: tuple[Path, Path]
) -> None:
    from bedrock_support import tenant_storage

    site, marker = package
    monkeypatch.setenv(EXTENSION_TOOLS_ENV, "1")
    info = next(site.glob("*.dist-info"))
    monkeypatch.setattr("nodestro_runtime.workspace.find_packages", lambda **_options: store_packages(info))
    monkeypatch.setattr("nodestro_runtime.workspace.discover", lambda **_options: discover(entry_points_of(info)))
    containers = Containers()
    monkeypatch.setattr(Workspace, "_extension_sandbox", lambda _self, timeout: containers(timeout))
    with Workspace(tmp_path / "tenant", storage=tenant_storage(tmp_path / "db", "alpha")) as workspace:
        assert containers.made == [] and workspace.tools.extension_tools == frozenset()
        assert workspace.extensions.packages_status()[0]["reason"] == "off"
    assert not marker.exists() and MODULE not in sys.modules


def test_an_extension_tool_never_replaces_a_tool_even_without_asserts(tmp_path: Path) -> None:
    from nodestro_runtime.tool_library import ToolLibrary

    library = ToolLibrary(tmp_path / "tools")
    found = ExtensionPackage("p", "mod:integration", "p", "1", read_files=lambda: {"mod.py": ""})
    spec = extension_spec("calculator", {})
    described = {"name": "calculator", "description": "x", "input_schema": {"type": "object"}}
    with pytest.raises(ValueError, match="calculator"):
        library.add_extension_tools([(spec, ExtensionTool(found, described, spec, ExtensionSandbox(Containers())))])


def test_control_characters_are_dropped_from_descriptions() -> None:
    tool = {"name": "fine", "description": "a\x1b[31mred\u202eevil\nline", "input_schema": {"type": "object"}}
    [checked] = _described(json.dumps({"ok": True, "tools": [tool]}).encode())
    assert checked["description"] == "a[31mredevil\nline"


# ---- capability and time limit ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stated", "access"),
    [
        ({}, "write"),
        ({"access": "read", "reach": "internal"}, "write"),
        ({"access": "write", "reach": "internal"}, "write"),
        ({"access": "destructive", "reach": "internal"}, "destructive"),
        ({"access": "nonsense"}, "write"),
    ],
)
def test_a_stated_capability_can_only_raise(stated: dict[str, Any], access: str) -> None:
    found = floor_capability(stated)
    assert found.access == access and found.reach == "external"
    spec = extension_spec("some_tool", stated)
    assert spec.capability == found and spec.offline is False and "server" not in spec.modes


@pytest.mark.parametrize(
    ("stated", "timeout"),
    [
        ({}, HOST_TOOL_TIMEOUT_SECONDS),
        ({"timeout_seconds": 5000.0}, HOST_TOOL_TIMEOUT_SECONDS),
        ({"timeout_seconds": 7.0}, 7.0),
        ({"timeout_seconds": 0.0}, HOST_TOOL_TIMEOUT_SECONDS),
        ({"timeout_seconds": True}, HOST_TOOL_TIMEOUT_SECONDS),
    ],
)
def test_time_limits_are_capped(stated: dict[str, Any], timeout: float) -> None:
    assert extension_spec("some_tool", stated).timeout_seconds == timeout


def test_each_call_runs_under_its_spec_time_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: tuple[Path, Path]
) -> None:
    site, _marker = package
    containers = Containers()
    with _workspace(tmp_path, monkeypatch, site, containers, switch="1") as workspace:
        specs = workspace.tools.specs
        assert specs["echo_text"].timeout_seconds == HOST_TOOL_TIMEOUT_SECONDS  # 5000 claimed, capped
        assert specs["wipe_all"].timeout_seconds == 7.0
        assert specs["spin_loop"].timeout_seconds == HOST_TOOL_TIMEOUT_SECONDS  # no spec: the 300 s default
        workspace.tools.builtin["wipe_all"].execute({"target": "x"})
        workspace.tools.builtin["spin_loop"].execute({"seconds": 0})
        assert [made.timeout for made in containers.made][-2:] == [7.0, HOST_TOOL_TIMEOUT_SECONDS]


def test_the_run_waits_for_the_containers_own_timeout(package: tuple[Path, Path], docker_cli: Path) -> None:
    """At the limit the container's timeout answers (its container removed), not the abandoned-thread timeout. The
    removal is confirmed through the fake docker CLI only (review L3: self-contained, no daemon needed)."""
    site, _marker = package
    [found] = store_packages(next(site.glob("*.dist-info")))
    containers = Containers()
    sandbox = ExtensionSandbox(containers)
    spec = extension_spec("spin_loop", {"timeout_seconds": 2.0})
    described = {"name": "spin_loop", "description": "Spin.", "input_schema": {"type": "object"}}
    bound = bind_tool(ExtensionTool(found, described, spec, sandbox), spec, None, resolve=None, counter=CallCounter())
    started = time.monotonic()
    with pytest.raises(ToolExecutionError, match="Timed out after 2 seconds"):
        bound.execute({"seconds": -1})
    assert time.monotonic() - started < 2 + 10
    [run] = containers.runs
    assert docker_cli.read_text(encoding="utf-8").splitlines() == [f"ps -aq --filter name=^/?{run['name']}$"]


# ---- the host side checks what comes back ---------------------------------------------------------------------------


class Answer:
    """A container that answers with fixed stdout."""

    docker = "docker"
    environ = None

    def __init__(self, stdout: bytes) -> None:
        self.stdout = stdout

    def ready_image(self) -> str:
        return "sha256:" + "1" * 64

    def shaped_run(self, name: str, image_id: str) -> list[str]:
        return ["docker", "run", name, image_id]

    def run_container(self, args: list[str], name: str, data: bytes = b"", *, max_stdout: int) -> tuple[int, Any]:
        return 0, BoundedOutput(stdout=self.stdout, stderr=b"", overflow=False)


def _described(stdout: bytes) -> Any:
    found = ExtensionPackage("p", "mod:integration", "p", "1", read_files=lambda: {"mod.py": ""})
    return ExtensionSandbox(lambda _timeout: Answer(stdout)).describe(found)


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"not json at all",
        b"[1, 2]",
        json.dumps({"ok": False, "error": "ImportError: no module named lxml"}).encode(),
        json.dumps({"ok": True, "tools": "many"}).encode(),
        json.dumps({"ok": True, "tools": [{"name": "Bad Name", "description": "x", "input_schema": {}}]}).encode(),
        json.dumps(
            {"ok": True, "tools": [{"name": "fine", "description": "", "input_schema": {"type": "object"}}]}
        ).encode(),
        json.dumps(
            {"ok": True, "tools": [{"name": "fine", "description": "x", "input_schema": {"type": "array"}}]}
        ).encode(),
        json.dumps(
            {"ok": True, "tools": [{"name": "fine", "description": "x" * 5000, "input_schema": {"type": "object"}}]}
        ).encode(),
        json.dumps(
            {
                "ok": True,
                "tools": [
                    {"name": f"t{i:02d}", "description": "x", "input_schema": {"type": "object"}} for i in range(65)
                ],
            }
        ).encode(),
    ],
)
def test_broken_responses_fail_cleanly(stdout: bytes) -> None:
    with pytest.raises(PackageError):
        _described(stdout)


def test_a_call_result_is_bounded() -> None:
    found = ExtensionPackage("p", "mod:integration", "p", "1", read_files=lambda: {"mod.py": ""})
    big = json.dumps({"ok": True, "result": "x" * 70_000}).encode()
    with pytest.raises(ToolExecutionError, match="at most"):
        ExtensionSandbox(lambda _timeout: Answer(big)).call(found, "fine", {}, 5.0)
    with pytest.raises(ToolExecutionError, match="no result"):
        ExtensionSandbox(lambda _timeout: Answer(b'{"ok": true}')).call(found, "fine", {}, 5.0)


def test_a_call_without_a_proven_sandbox_is_refused() -> None:
    found = ExtensionPackage("p", "mod:integration", "p", "1", read_files=lambda: {"mod.py": ""})
    with pytest.raises(ToolExecutionError, match=LOCKED_MESSAGE):
        ExtensionSandbox(lambda timeout: FakeContainer(timeout, ready=False)).call(found, "fine", {}, 5.0)


def test_only_the_distributions_own_regular_files_are_read(tmp_path: Path) -> None:
    site = tmp_path / "site"
    info = install_package(site, marker=tmp_path / "m")
    outside = tmp_path / "secret.py"
    outside.write_text("SECRET = 1\n", encoding="utf-8")
    (site / MODULE / "linked.py").symlink_to(outside)
    record = info / "RECORD"
    record.write_text(record.read_text(encoding="utf-8") + f"{MODULE}/linked.py,,\n", encoding="utf-8")
    with pytest.raises(PackageError, match=r"linked\.py"):
        distribution_files(metadata.PathDistribution(info), f"{MODULE}:integration")
    (site / "x.py").write_text("OUTSIDE = 1\n", encoding="utf-8")  # importlib lists only files that exist
    record.write_text(record.read_text(encoding="utf-8").replace(f"{MODULE}/linked.py", f"{MODULE}/../x.py"), "utf-8")
    with pytest.raises(PackageError, match="unexpected"):
        distribution_files(metadata.PathDistribution(info), f"{MODULE}:integration")
    with pytest.raises(PackageError):
        distribution_files(metadata.PathDistribution(info), "os; import x")


def test_the_host_never_holds_a_tool_class_of_the_package() -> None:
    """The tool the library offers is the host's proxy; ToolResultError and Tool stay Orkexa's own."""
    assert issubclass(ExtensionTool, Tool) and ToolResultError.__module__.startswith("orkexa")


# ---- real Docker (CI job docker) -------------------------------------------------------------------------------------

DOCKER_TESTS = os.environ.get("NODESTRO_DOCKER_TESTS") == "1"


def _require_docker() -> None:
    if not DOCKER_TESTS:
        if os.environ.get("NODESTRO_REQUIRE_DOCKER") == "1":
            pytest.fail("NODESTRO_REQUIRE_DOCKER=1 but NODESTRO_DOCKER_TESTS is not set")
        pytest.skip("set NODESTRO_DOCKER_TESTS=1 (needs Docker and network for the image build)")
    from orkexa.dynamic.docker import DockerProbeError, inspect_host, preflight

    try:
        ready = preflight(inspect_host()).available
    except DockerProbeError:
        ready = False
    if not ready:
        pytest.fail("NODESTRO_DOCKER_TESTS=1 but the Docker daemon does not pass Orkexa's preflight")


@pytest.fixture(scope="module")
def real_image() -> Iterator[str]:
    """An image of the runtime image's requirements (pydantic, typing-extensions), built from a hashed lock."""
    _require_docker()
    from nodestro_runtime.container_images import ContainerImages
    from nodestro_runtime.python_envs import UvRunner, find_uv

    uv = find_uv()
    assert uv is not None, "uv is a dependency of the runtime"
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    with tempfile.TemporaryDirectory() as cache:
        lock = UvRunner(Path(cache), uv).compile(
            ["pydantic>=2.6,<3", "typing-extensions>=4.14"], python=python, previous="", log=lambda _line: None
        )
    images = ContainerImages()
    tag = images.build(lock, python)
    yield tag
    images.remove(tag)


class Real:
    """A foreign package, its sandbox on the real engine with an owner label of its own, and the host's secrets."""

    def __init__(self, tmp_path: Path, tag: str, monkeypatch: pytest.MonkeyPatch) -> None:
        from nodestro_runtime.container_images import ContainerImages

        self.owner = f"nodestro-n22-{uuid.uuid4().hex[:10]}"
        images = ContainerImages()
        images.owner = self.owner
        self.secret = f"secret-{uuid.uuid4().hex}"
        for name in ("NODESTRO_VAULT_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY"):
            monkeypatch.setenv(name, self.secret)
        self.data = tmp_path / "data"
        self.data.mkdir()
        (self.data / "api-key").write_text(self.secret, encoding="utf-8")
        self.home = Path.home()
        self.marker = tmp_path / "imported-in-host.txt"
        self.host_paths = [
            str(self.data / "api-key"),
            str(self.data),
            str(tmp_path),
            str(self.home / ".aws"),
            str(self.home / ".docker"),
            str(Path(__file__).resolve()),
            "/var/run/docker.sock",
        ]
        site = tmp_path / "site"
        info = install_package(site, marker=self.marker, paths=self.host_paths)
        [self.package] = store_packages(info)
        self.sandbox = ExtensionSandbox(lambda timeout: images.sandbox(tag, timeout=timeout, memory_mb=512))

    def call(self, tool: str, payload: dict[str, Any], timeout: float = 60.0) -> str:
        return self.sandbox.call(self.package, tool, payload, timeout)

    def probe(self, check: str) -> Any:
        return json.loads(self.call("probe_sandbox", {"check": check}))

    def containers(self) -> list[str]:
        listed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=org.orkexa.sandbox={self.owner}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return listed.stdout.split()


@pytest.fixture
def real(tmp_path: Path, real_image: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Real]:
    found = Real(tmp_path, real_image, monkeypatch)
    yield found
    for container in found.containers():
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False, timeout=60)


def test_import_time_code_runs_only_in_the_container(real: Real) -> None:
    tools = real.sandbox.describe(real.package)
    assert [item["name"] for item in tools] == ["echo_text", "wipe_all", "spin_loop", "probe_sandbox"]
    assert tools[0]["stated"] == {"access": "read", "reach": "internal", "timeout_seconds": 5000.0}
    assert real.call("echo_text", {"text": "hello"}) == "hello"
    assert not real.marker.exists() and MODULE not in sys.modules
    assert real.containers() == []


def test_an_endless_loop_is_killed_and_the_container_removed(real: Real) -> None:
    started = time.monotonic()
    with pytest.raises(ToolExecutionError, match="Timed out after 3 seconds"):
        real.call("spin_loop", {"seconds": -1}, timeout=3.0)
    assert time.monotonic() - started < 3 + 20
    assert real.containers() == []  # killed and removed, not left running
    assert real.call("echo_text", {"text": "after"}) == "after"  # the host is free


def test_no_network_without_a_grant(real: Real) -> None:
    found = real.probe("network")
    for check in ("tcp", "dns", "udp", "metadata", "ipv6"):
        assert found[check]["ok"] is False, (check, found[check])
    assert found["interfaces"] == ["lo"]


def test_host_files_and_secrets_are_not_visible(real: Real) -> None:
    files = real.probe("files")
    assert files == dict.fromkeys(real.host_paths, False)
    environment = real.probe("environment")
    assert real.secret not in json.dumps(environment)
    assert not ({"NODESTRO_VAULT_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY"} & set(environment["environ"]))


def test_forbidden_resources_are_refused(real: Real) -> None:
    found = real.probe("resources")
    assert found["docker_socket"] is False
    assert found["uid"] == 65534 and found["setuid"]["ok"] is False
    assert found["cap_eff"] == "0000000000000000" and found["no_new_privs"] == "1"
    assert found["pids_visible"] < 10  # its own process namespace, not the host's
    assert "python" in found["proc1"]["value"]  # PID 1 is the container's own process
    assert found["disk"]["ok"] is False and found["mknod"]["ok"] is False
    assert not {"sda", "nvme0n1", "kmsg", "mem"} & set(found["devices"])
    assert found["fork"]["ok"] is False  # no child processes (process limits)
    assert real.containers() == []


def test_memory_beyond_the_limit_is_refused_in_the_container(real: Real) -> None:
    """4 GiB is beyond the container's memory limit: the allocation fails or the kernel kills the container."""
    try:
        found = real.probe("memory")
    except ToolExecutionError as exc:
        assert "137" in str(exc) or "MemoryError" in str(exc), exc
    else:
        assert found["memory"]["ok"] is False
    assert real.containers() == []
    assert real.call("echo_text", {"text": "still here"}) == "still here"


def test_huge_output_fails_cleanly(real: Real) -> None:
    with pytest.raises(ToolExecutionError):
        real.call("probe_sandbox", {"check": "huge"})
    assert real.containers() == []


def test_a_missing_docker_cli_locks_the_tools_with_the_real_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: tuple[Path, Path]
) -> None:
    """The real readiness gate (Orkexa's preflight and self-test) without a docker CLI: locked, no host fallback."""
    from nodestro_runtime.container_images import StreamedSandbox

    site, marker = package
    monkeypatch.setenv(EXTENSION_TOOLS_ENV, "1")
    found = store_packages(next(site.glob("*.dist-info")))
    missing = str(tmp_path / "no-docker")
    sandbox = ExtensionSandbox(lambda timeout: StreamedSandbox(image="nodestro-env:x", timeout=timeout, docker=missing))
    extensions = Extensions(installed=True, packages=found)
    assert extensions.load_packages(sandbox, reserved=()) == []
    [status] = extensions.packages_status()
    assert (status["reason"], status["message"]) == ("not_secured", LOCKED_MESSAGE)
    assert "not installed" in str(status["detail"])
    assert not marker.exists() and MODULE not in sys.modules
