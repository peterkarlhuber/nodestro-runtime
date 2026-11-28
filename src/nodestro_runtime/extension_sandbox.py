"""Tools of installed integrations in the Docker sandbox (N-22, see ``docs/extension-isolation.md``).

An installed integration that is not Nodestro's own is never imported into the host process
(:func:`nodestro_runtime.extensions.find_packages`): it is an :class:`ExtensionPackage`, the entry point's
``module:attribute`` and the Python files its distribution lists. Since the owner decision of 2026-11-27 a package
found in the host environment is refused before it gets here (``HOST_INSTALLED``); this module stays for the
separate extension store, which will feed it. Listing its tools (:meth:`ExtensionSandbox.describe`)
and every call (:meth:`ExtensionSandbox.call`) run in a new container of Orkexa's one sandbox shape, started through a
:class:`~nodestro_runtime.container_images.StreamedSandbox` only after the daemon passed the preflight and the
in-container self-test. The files, Orkexa's runner modules, the contract module (:mod:`._extension_contract`) and one
JSON request travel on stdin (:data:`BOOTSTRAP`); one JSON line comes back on stdout. There is no fallback: without a
proven sandbox nothing runs (:class:`SandboxUnavailable`).

Every extension container also runs under Nodestro's own seccomp profile (:mod:`.extension_seccomp`), stricter than
the engine's default; a probe container of the same shape, without package code, proves once per daemon and image
that the profile is applied, or nothing runs (``not_secured``).

A call's time limit ends the container: the client is killed, the container killed and removed, and its absence
checked (:meth:`ExtensionSandbox._ensure_gone`). What a package states about its tools can only raise their capability
(:func:`floor_capability`) and only lower their time limit (:func:`extension_spec`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
import json
import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
from typing import Any
import uuid

from orkexa import Tool
from orkexa.dynamic import SandboxError, response_limit
from orkexa.dynamic.docker import runner_files
from orkexa.errors import ToolExecutionError

from . import extension_seccomp
from .extension_classes import NO_GRANTS, class_capability
from .tool_policy import ToolCapability
from .tool_spec import HOST_TOOL_TIMEOUT_SECONDS, SpecMode, ToolSpec

logger = logging.getLogger(__name__)

#: Where extension tools are offered: on this computer only, never to a server's tenants.
EXTENSION_MODES: frozenset[SpecMode] = frozenset({"desktop", "local-container"})
#: Bounds of what a package may send into the container.
MAX_PACKAGE_FILES = 500
MAX_PACKAGE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
#: Bounds of what a package may report about its tools.
MAX_TOOLS = 64
MAX_DESCRIPTION_CHARS = 4096
MAX_SCHEMA_BYTES = 64 * 1024
MAX_PAYLOAD_BYTES = 100_000
#: The longest tool result (characters).
MAX_RESULT_CHARS = 65_536
#: Time limit of listing a package's tools.
DESCRIBE_TIMEOUT_SECONDS = 60.0
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_DOTTED = r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*"
_ENTRY = re.compile(rf"^{_DOTTED}(:{_DOTTED})?$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_SOURCE = Path(__file__).resolve().parent

#: ``python -I -B -c BOOTSTRAP`` in the container: reads one JSON envelope from stdin, writes Orkexa's runner modules
#: and the contract module below the temporary directory and the package's files below ``<work>/pkg`` (``noexec`` file
#: systems; Python only reads them), then runs the extension runner with the request as its stdin.
BOOTSTRAP = """\
import io, json, os, runpy, sys, tempfile
envelope = json.loads(sys.stdin.read())
root = os.path.join(tempfile.gettempdir(), 'nodestro')
def place(base, path, content, first):
    parts = path.split('/')
    if (first and parts[0] not in first) or not path.endswith('.py') or any(
        not p or p.startswith('.') or '\\\\' in p for p in parts
    ):
        raise SystemExit('invalid path')
    target = os.path.join(base, *parts)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, 'w', encoding='utf-8') as handle:
        handle.write(content)
for path, content in envelope['runner'].items():
    place(root, path, content, ('orkexa', 'nodestro_runtime'))
for path, content in envelope['files'].items():
    place(os.path.join(os.getcwd(), 'pkg'), path, content, None)
request = dict(envelope['request'], root=root)
runner = os.path.join(root, 'nodestro_runtime', 'runner.py')
sys.stdin = io.StringIO(json.dumps(request))
sys.argv = [runner]
runpy.run_path(runner, run_name='__main__')
"""


class SandboxUnavailable(Exception):
    """No proven Docker sandbox: the package's tools stay locked (``not_secured``)."""


class PackageError(Exception):
    """A package that cannot be used (its files, its listing or its answers)."""


@dataclass(frozen=True)
class ExtensionPackage:
    """An installed integration that is never imported into the host process."""

    #: The entry point's name.
    name: str
    #: ``module:attribute`` of the entry point.
    entry: str
    distribution: str
    version: str
    #: Reads the package's files (``{"pkg/mod.py": source}``); called once, before the first container.
    read_files: Callable[[], dict[str, str]] = field(repr=False, compare=False)
    #: Why the package is never used, not even in the sandbox (it claims to be Nodestro's own and failed the checks of
    #: :mod:`nodestro_runtime.trusted_integrations`); empty otherwise.
    rejected: str = ""
    #: The SHA-256 of the store package this entry point belongs to (``<data>/extensions/<sha256>``); empty for a
    #: package that did not come from the store.
    sha256: str = ""
    #: Raises :class:`PackageError` when the package may no longer run (removed, revoked, replaced); called before
    #: every container of a call, so a call never starts after its package became invalid.
    check: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    @property
    def key(self) -> tuple[str, str, str, str]:
        """What tells two packages apart: distribution, entry point name, ``module:attribute`` and the store hash (a
        refused copy in the host environment never shares the state of the store's)."""
        return (self.distribution, self.name, self.entry, self.sha256)


def package_of(entry: metadata.EntryPoint, *, rejected: str = "") -> ExtensionPackage:
    """``entry`` as an :class:`ExtensionPackage` (nothing of it is imported). A ``rejected`` package's files are never
    read: only its metadata (name, entry point, distribution, version) is kept, to report it."""
    dist = entry.dist

    def read_files() -> dict[str, str]:
        if rejected:
            raise PackageError(rejected)
        return distribution_files(dist, entry.value)

    return ExtensionPackage(
        rejected=rejected,
        name=entry.name,
        entry=entry.value,
        distribution=dist.metadata["Name"] if dist is not None else "",
        version=dist.version if dist is not None else "",
        read_files=read_files,
    )


def distribution_files(dist: metadata.Distribution | None, entry: str) -> dict[str, str]:
    """The Python files of ``dist`` that belong to the top-level module of ``entry``, read as text (never imported).

    Only files the distribution's ``RECORD`` lists, inside its installation directory, regular files (no symbolic
    link is followed), UTF-8, within :data:`MAX_PACKAGE_FILES`, :data:`MAX_FILE_BYTES` and :data:`MAX_PACKAGE_BYTES`.
    Raises :class:`PackageError`."""
    if dist is None or not _ENTRY.match(entry):
        raise PackageError("the entry point names no module of an installed distribution")
    top = entry.split(":")[0].split(".")[0]
    base = Path(str(dist.locate_file(""))).resolve()
    found: dict[str, str] = {}
    total = 0
    for item in dist.files or ():
        parts = item.parts
        if not parts or parts[-1][-3:] != ".py" or not (parts[0] == top or parts == (f"{top}.py",)):
            continue
        if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
            raise PackageError(f"unexpected file name {item}")
        path = base.joinpath(*parts)
        if os.path.realpath(path) != str(path):  # every component, not only the last (no linked package folder)
            raise PackageError(f"{item} is reached through a symbolic link")
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        except OSError as exc:
            raise PackageError(f"cannot read {item}: {exc.strerror}") from None
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise PackageError(f"{item} is not a regular file within {MAX_FILE_BYTES} bytes")
            data = handle.read(MAX_FILE_BYTES + 1)
        total += len(data)
        if len(found) >= MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
            raise PackageError("the package is larger than the sandbox accepts")
        try:
            found["/".join(parts)] = data.decode("utf-8")
        except UnicodeDecodeError:
            raise PackageError(f"{item} is not UTF-8") from None
    if not found:
        raise PackageError(f"the distribution lists no Python file of {top!r}")
    return found


def _contract_files() -> dict[str, str]:
    """Orkexa's runner modules (for ``orkexa.Tool``), the contract module and the extension runner."""
    return {
        **runner_files(),
        "nodestro_runtime/__init__.py": "",
        "nodestro_runtime/_contract.py": (_SOURCE / "_extension_contract.py").read_text(encoding="utf-8"),
        "nodestro_runtime/runner.py": (_SOURCE / "_extension_runner.py").read_text(encoding="utf-8"),
    }


def floor_capability(stated: Mapping[str, Any] | None) -> ToolCapability:
    """At least :data:`~nodestro_runtime.tool_policy.UNKNOWN` (write, external): a package's statement can raise the
    access (``destructive``), never lower it; ``reach`` is always ``external``. A tool class released after gate G-6
    would count with its class instead (:func:`~.extension_classes.class_capability`); none is released, and the
    containers get no grant."""
    return class_capability(stated, NO_GRANTS)


def extension_spec(name: str, stated: Mapping[str, Any] | None) -> ToolSpec:
    """The spec of an extension tool: capability :func:`floor_capability`, time limit
    :data:`~nodestro_runtime.tool_spec.HOST_TOOL_TIMEOUT_SECONDS` unless the package states a lower one, never offline,
    only in :data:`EXTENSION_MODES`."""
    timeout = HOST_TOOL_TIMEOUT_SECONDS
    wanted = (stated or {}).get("timeout_seconds")
    if isinstance(wanted, (int, float)) and not isinstance(wanted, bool) and 1.0 <= wanted < timeout:
        timeout = float(wanted)
    return ToolSpec(
        id=name,
        category="data",
        capability=floor_capability(stated),
        network="external",
        offline=False,
        modes=EXTENSION_MODES,
        timeout_seconds=timeout,
    )


#: Makes the container sandbox for one time limit (the workspace's: Nodestro's runtime image, its owner label).
SandboxFactory = Callable[[float], Any]


class ExtensionSandbox:
    """Runs the extension runner in a new container per request (see the module documentation)."""

    def __init__(self, sandbox: SandboxFactory) -> None:
        self._make = sandbox
        self._sandboxes: dict[float, Any] = {}
        self._files: dict[tuple[str, str, str, str], dict[str, str]] = {}
        #: (docker, its environment, image id) for which the seccomp probe passed.
        self._proven: set[tuple[str, str, str]] = set()
        self._lock = threading.Lock()

    def _sandbox(self, timeout: float) -> Any:
        with self._lock:
            if timeout not in self._sandboxes:
                self._sandboxes[timeout] = self._make(timeout)
            return self._sandboxes[timeout]

    def ready(self) -> None:
        """Raise :class:`SandboxUnavailable` unless the daemon passed the preflight, the self-test and the seccomp
        probe."""
        sandbox = self._sandbox(DESCRIBE_TIMEOUT_SECONDS)
        try:
            image = sandbox.ready_image()
        except SandboxError as exc:
            raise SandboxUnavailable(str(exc)) from None
        self._prove(sandbox, image)

    @staticmethod
    def _shaped(sandbox: Any, name: str, image: str) -> list[str]:
        """Orkexa's sandbox shape for ``name`` plus the extension seccomp profile, up to the image."""
        args = list(sandbox.shaped_run(name, image))
        return [*args[:-1], "--security-opt", extension_seccomp.security_option(), args[-1]]

    def _prove(self, sandbox: Any, image: str) -> None:
        """Run :data:`~.extension_seccomp.PROBE_SCRIPT` in a container of the extension shape (no package code) once
        per daemon and image; raise :class:`SandboxUnavailable` unless every probed call was refused."""
        key = (str(sandbox.docker), json.dumps(sandbox.environ, sort_keys=True), image)
        with self._lock:
            if key in self._proven:
                return
        name = f"nodestro-sandbox-ext-probe-{uuid.uuid4().hex[:12]}"
        try:
            code, output = sandbox.run_container(
                [*self._shaped(sandbox, name, image), *extension_seccomp.probe_command()], name, b"", max_stdout=4096
            )
        except SandboxError as exc:
            self._ensure_gone(sandbox, name)
            raise SandboxUnavailable(f"the seccomp probe failed: {exc}") from None
        problems = extension_seccomp.check_probes(output.stdout.decode("utf-8", "replace"))
        if code != 0 or problems:
            stderr = output.stderr.decode("utf-8", "replace").strip()[-300:]
            detail = "; ".join(item for item in (problems[0] if problems else "", stderr) if item)
            raise SandboxUnavailable(f"the seccomp probe failed (exit code {code}): {detail}")
        with self._lock:
            self._proven.add(key)

    def _package_files(self, package: ExtensionPackage) -> dict[str, str]:
        """The package's files as read (and, for a store package, verified) once; later calls send these bytes."""
        key = package.key
        with self._lock:
            if key not in self._files:
                self._files[key] = package.read_files()
            return self._files[key]

    def _run(self, package: ExtensionPackage, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        sandbox = self._sandbox(timeout)
        try:
            image = sandbox.ready_image()
        except SandboxError as exc:
            raise SandboxUnavailable(str(exc)) from None
        self._prove(sandbox, image)
        files = self._package_files(package)
        if package.check is not None:  # removed, revoked or replaced since it was listed: never started again
            package.check()
        data = json.dumps({"runner": _contract_files(), "files": files, "request": request}).encode("utf-8")
        name = f"nodestro-sandbox-ext-{uuid.uuid4().hex[:12]}"
        args = [*self._shaped(sandbox, name, image), "python", "-I", "-B", "-c", BOOTSTRAP]
        try:
            code, output = sandbox.run_container(args, name, data, max_stdout=response_limit(MAX_RESULT_CHARS))
        except SandboxError:
            self._ensure_gone(sandbox, name)
            raise
        lines = output.stdout.decode("utf-8", "replace").strip().splitlines()
        try:
            response = json.loads(lines[-1]) if lines else None
        except ValueError:
            response = None
        if not isinstance(response, dict):
            stderr = output.stderr.decode("utf-8", "replace")[-500:]
            raise PackageError(f"the container exited with code {code} without an answer: {stderr}")
        return response

    @staticmethod
    def _ensure_gone(sandbox: Any, name: str) -> None:
        """Confirm that the container ``name`` no longer exists (Orkexa's ``stop_container`` removed it); remove it
        again otherwise, and raise :class:`SandboxError` when it still exists."""

        def listed() -> bool:
            found = subprocess.run(
                [sandbox.docker, "ps", "-aq", "--filter", f"name=^/?{name}$"],
                capture_output=True,
                timeout=30,
                check=False,
                env=sandbox.environ,
            )
            return found.returncode != 0 or bool(found.stdout.strip())

        try:
            if not listed():
                return
            subprocess.run(
                [sandbox.docker, "rm", "-f", name], capture_output=True, timeout=30, check=False, env=sandbox.environ
            )
            if not listed():
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        logger.error("The extension container %s could not be confirmed removed", name)
        raise SandboxError(f"The container {name} could not be confirmed removed")

    def describe(self, package: ExtensionPackage) -> list[dict[str, Any]]:
        """The tools ``package`` reports, checked (names, descriptions, schemas); raises :class:`PackageError` or
        :class:`SandboxUnavailable`."""
        try:
            response = self._run(package, {"mode": "describe", "entry": package.entry}, DESCRIBE_TIMEOUT_SECONDS)
        except SandboxError as exc:
            raise PackageError(f"listing the tools failed: {exc}") from None
        if not response.get("ok"):
            raise PackageError(f"listing the tools failed: {str(response.get('error'))[:500]}")
        tools = response.get("tools")
        if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
            raise PackageError(f"the package must report a list of at most {MAX_TOOLS} tools")
        return [_checked(item) for item in tools]

    def call(self, package: ExtensionPackage, tool: str, payload: dict[str, Any], timeout: float) -> str:
        """Run ``tool`` of ``package`` with ``payload`` under ``timeout``; raises :class:`ToolExecutionError`."""
        try:
            request = {"mode": "call", "entry": package.entry, "tool": tool, "payload": payload}
            response = self._run(package, request, timeout)
        except SandboxUnavailable as exc:
            raise ToolExecutionError(f"{tool}: locked, not sufficiently secured ({exc})") from None
        except (SandboxError, PackageError) as exc:
            raise ToolExecutionError(f"{tool}: {exc}") from None
        if not response.get("ok"):
            raise ToolExecutionError(f"{tool}: {str(response.get('error') or 'failed')[:2000]}")
        result = response.get("result")
        if not isinstance(result, str):
            raise ToolExecutionError(f"{tool}: the sandbox returned no result")
        if len(result) > MAX_RESULT_CHARS:
            raise ToolExecutionError(f"{tool}: the result has {len(result)} characters, at most {MAX_RESULT_CHARS}")
        return result


def _checked(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise PackageError("a tool entry is not an object")
    name, description, schema = item.get("name"), item.get("description"), item.get("input_schema")
    if not isinstance(name, str) or not _NAME.match(name):
        raise PackageError(f"invalid tool name {str(name)[:80]!r}")
    if not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION_CHARS:
        raise PackageError(f"{name}: the description must be text of at most {MAX_DESCRIPTION_CHARS} characters")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise PackageError(f"{name}: the input schema must be a JSON object schema")
    if len(json.dumps(schema)) > MAX_SCHEMA_BYTES:
        raise PackageError(f"{name}: the input schema is larger than {MAX_SCHEMA_BYTES} bytes")
    stated = item.get("stated")
    return {
        "name": name,
        # Control and bidi characters are dropped (not line breaks, tabs); the text stays untrusted, a UI escapes it.
        "description": _CONTROL.sub("", description),
        "input_schema": schema,
        "stated": stated if isinstance(stated, dict) else {},
    }


class ExtensionTool(Tool):
    """A tool of an installed integration: name, description and schema as the sandbox reported them; every call runs
    in a new container (:meth:`ExtensionSandbox.call`) under the spec's time limit. The code is never imported here."""

    #: The call ends its own execution at the time limit (the container is removed); the run waits a little longer
    #: for that answer (:data:`~nodestro_runtime.tool_spec.STOP_GRACE_SECONDS`).
    stops_itself = True

    def __init__(
        self, package: ExtensionPackage, described: Mapping[str, Any], spec: ToolSpec, sandbox: ExtensionSandbox
    ) -> None:
        super().__init__(name=str(described["name"]), strict=False)
        self.package = package
        self.spec = spec
        self.sandbox = sandbox
        self.description = str(described["description"])
        self.input_schema = json.loads(json.dumps(described["input_schema"]))
        self.function = self._call

    def __repr__(self) -> str:
        return f"<ExtensionTool {self.name!r} of {self.package.name!r}>"

    def _call(self, **kwargs: Any) -> str:
        return self.execute(kwargs)

    def execute(self, payload: dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            raise ToolExecutionError(f"{self.name}: tool input must be a JSON object")
        try:
            size = len(json.dumps(payload))
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError(f"{self.name}: tool input is not JSON: {exc}") from None
        if size > MAX_PAYLOAD_BYTES:
            raise ToolExecutionError(f"{self.name}: tool input too large ({size} characters)")
        assert self.name is not None
        return self.sandbox.call(self.package, self.name, payload, self.spec.timeout_seconds)

    async def aexecute(self, payload: dict[str, Any]) -> str:
        import asyncio

        return await asyncio.to_thread(self.execute, payload)


__all__ = [
    "BOOTSTRAP",
    "EXTENSION_MODES",
    "ExtensionPackage",
    "ExtensionSandbox",
    "ExtensionTool",
    "PackageError",
    "SandboxUnavailable",
    "distribution_files",
    "extension_spec",
    "floor_capability",
    "package_of",
]
