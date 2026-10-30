"""Managed Python environments for tools that need third-party libraries.

Principle: the standard library is always there; a third-party package is resolved,
installed and importable only after the user approved it for one *scope*:

* ``workflow:<id>``: the environment the workflow's tools run in (runs, agent tests);
* ``tool:<name>``: the test environment of one tool in the tool editor.

Per scope Nodestro stores a definition in the ``python_envs`` collection: the direct
requirements, the lock (exact versions of all direct and indirect packages, resolved by
``uv pip compile --universal``), the approvals and a change history. The environment
itself is a virtual environment under ``<data>/python/envs``, built from the lock with
``uv``; downloads are shared through the cache in ``<data>/python/cache``. Nodestro's own
Python installation is never changed.

* Planning (:meth:`PythonEnvironments.plan`) only resolves versions; nothing is installed.
  Previously approved versions are kept as preferences, so they never change silently.
* Approving a plan stores the lock and installs it into a fresh directory that replaces the
  old environment only when everything succeeded and every needed module can be found.
* Approvals carry the id of this Nodestro installation: a definition that comes from an
  export, or a backup restored on another computer, must be approved again here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import contextlib
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import TYPE_CHECKING, Any, Literal
import uuid

from orkexa.dynamic import Sandbox, SubprocessSandbox
from orkexa.providers.base import redact
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, Field

from .problems import NodestroError, Problem, problem
from .python_deps import RUNTIME_PACKAGES, ToolDependencies, analyze, blocking, normalize
from .schemas import Workflow, WorkflowIn, new_id, now
from .storage import Store

if TYPE_CHECKING:
    from .tool_library import ToolLibrary

logger = logging.getLogger(__name__)

_COLLECTION = "python_envs"
_PLAN_TTL = timedelta(hours=1)
_MAX_LOG_LINES = 300
_MARKER = "nodestro-env.json"

#: Variables uv receives from Nodestro's environment (everything else, such as provider keys and
#: tokens, stays out: building a package from source runs that package's own code). Kept are what
#: a package manager legitimately needs: system paths, locale, temporary directories, proxies,
#: certificates, uv's own configuration (``UV_*``: index URLs and their credentials) and the usual
#: compiler settings for source builds.
UV_ENV_ALLOWED = frozenset(
    {
        # system
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LANGUAGE", "TZ",
        "TMPDIR", "TEMP", "TMP", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
        # Windows
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
        "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
        # network
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NETRC",
        # source builds
        "CC", "CXX", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "PKG_CONFIG_PATH", "MACOSX_DEPLOYMENT_TARGET",
        "SDKROOT", "DEVELOPER_DIR",
    }
)  # fmt: skip
UV_ENV_PREFIXES = ("UV_", "LC_")


def uv_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of a uv process: the allowed variables of ``source`` (``os.environ``) only."""
    environ = os.environ if source is None else source
    allowed = {name.upper() for name in UV_ENV_ALLOWED}
    return {
        key: value
        for key, value in environ.items()
        if key.upper() in allowed or key.upper().startswith(UV_ENV_PREFIXES)
    }


#: Numerical libraries start one thread per core; the sandbox allows no extra processes.
SANDBOX_ENV = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}

ScopeKind = Literal["workflow", "tool"]
EnvState = Literal[
    "none", "ready", "approval_needed", "install_needed", "installing", "failed", "unresolved", "unsupported"
]
PackageStatus = Literal["approved", "new", "changed", "runtime", "removed"]
ChangeKind = Literal["approved", "installed", "install_failed", "removed", "imported", "copied", "cleaned"]


def current_python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def scope_of(kind: ScopeKind, ref: str) -> str:
    return f"{kind}:{ref}"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- models ---------------------------------------------------------------------------


class NeededRequirement(BaseModel):
    """A direct requirement of a scope: the union of what its tools need."""

    name: str
    key: str
    spec: str = ""
    extras: list[str] = Field(default_factory=list)
    marker: str = ""
    #: The tools that need it.
    required_by: list[str] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)

    def line(self) -> str:
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        marker = f"; {self.marker}" if self.marker else ""
        return f"{self.name}{extras}{self.spec}{marker}"


class ToolIssue(BaseModel):
    """A dependency problem of one tool (an unknown module, a dynamic import, ...)."""

    tool: str
    code: str
    message: str
    params: dict[str, Any] = Field(default_factory=dict)
    blocking: bool = True


class Needed(BaseModel):
    requirements: list[NeededRequirement] = Field(default_factory=list)
    issues: list[ToolIssue] = Field(default_factory=list)
    #: Tool name -> top-level modules of its third-party imports (for the static check).
    modules: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def blocking(self) -> list[ToolIssue]:
        return [issue for issue in self.issues if issue.blocking]

    def all_modules(self) -> set[str]:
        return {module for modules in self.modules.values() for module in modules}


class LockedPackage(BaseModel):
    name: str
    key: str
    version: str
    marker: str = ""
    direct: bool = False
    runtime: bool = False
    #: Tools (direct packages) or packages (indirect ones) that need it.
    required_by: list[str] = Field(default_factory=list)


class PackageApproval(BaseModel):
    key: str
    name: str
    version: str
    approved_at: datetime = Field(default_factory=now)
    #: The Nodestro installation the approval was given on.
    installation: str


class EnvChange(BaseModel):
    at: datetime = Field(default_factory=now)
    kind: ChangeKind
    #: E.g. ``pandas 3.1.0 (new)``, ``numpy 2.5.3 → 2.5.4``.
    details: list[str] = Field(default_factory=list)


class InstallState(BaseModel):
    status: Literal["none", "installing", "installed", "failed"] = "none"
    #: The current step for the UI: ``deps.step.*`` codes.
    step: str | None = None
    error: Problem | None = None
    log: list[str] = Field(default_factory=list)
    lock_sha: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class PythonEnvironment(BaseModel):
    """The stored definition of one scope's environment."""

    scope: str
    kind: ScopeKind
    ref: str
    label: str = ""
    #: The direct requirements as approved (with the tools that needed them).
    requirements: list[NeededRequirement] = Field(default_factory=list)
    lock: str = ""
    packages: list[LockedPackage] = Field(default_factory=list)
    python_version: str = ""
    approvals: list[PackageApproval] = Field(default_factory=list)
    history: list[EnvChange] = Field(default_factory=list)
    install: InstallState = Field(default_factory=InstallState)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class DependencyStatus(BaseModel):
    """What the UI shows for a scope: what is needed, what is approved and installed."""

    scope: str
    kind: ScopeKind
    ref: str
    label: str = ""
    state: EnvState
    needed: list[NeededRequirement] = Field(default_factory=list)
    issues: list[ToolIssue] = Field(default_factory=list)
    #: Needed packages that are not approved (new, or a changed requirement).
    pending: list[str] = Field(default_factory=list)
    #: Why approval is needed: ``new``, ``changed``, ``python``, ``other_installation``.
    reasons: list[str] = Field(default_factory=list)
    packages: list[LockedPackage] = Field(default_factory=list)
    approved_at: datetime | None = None
    python_version: str = ""
    current_python: str = Field(default_factory=current_python_version)
    install: InstallState = Field(default_factory=InstallState)
    history: list[EnvChange] = Field(default_factory=list)
    problem: Problem | None = None


class PlanPackage(BaseModel):
    name: str
    key: str
    version: str
    previous_version: str | None = None
    marker: str = ""
    direct: bool = False
    runtime: bool = False
    required_by: list[str] = Field(default_factory=list)
    status: PackageStatus
    #: Other scopes on this computer where this exact version is approved (information only).
    approved_elsewhere: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    """The approval overview: what would be installed, nothing has happened yet."""

    id: str = Field(default_factory=lambda: new_id("plan"))
    scope: str
    kind: ScopeKind
    ref: str
    label: str = ""
    python_version: str = Field(default_factory=current_python_version)
    requirements: list[NeededRequirement] = Field(default_factory=list)
    packages: list[PlanPackage] = Field(default_factory=list)
    issues: list[ToolIssue] = Field(default_factory=list)
    #: The resolution failed (conflict, unknown package, no network): nothing can be approved.
    error: Problem | None = None
    needs_approval: bool = False
    created_at: datetime = Field(default_factory=now)
    lock: str = ""


class EnvironmentInfo(BaseModel):
    """An environment directory on disk, for the maintenance page."""

    scope: str
    kind: ScopeKind | None = None
    ref: str = ""
    label: str = ""
    path: str
    size_bytes: int = 0
    #: No workflow or tool owns it any more.
    orphaned: bool = False
    has_definition: bool = False
    packages: int = 0
    installed_at: datetime | None = None


class PythonStatus(BaseModel):
    uv_available: bool
    uv_version: str | None = None
    python_version: str = Field(default_factory=current_python_version)
    environments: list[EnvironmentInfo] = Field(default_factory=list)
    cache_bytes: int = 0
    root: str


class ExportedDefinition(BaseModel):
    """The dependency part of an exported workflow: no approvals, they never travel."""

    requirements: list[NeededRequirement] = Field(default_factory=list)
    lock: str = ""
    packages: list[LockedPackage] = Field(default_factory=list)
    python_version: str = ""


# ---- uv -------------------------------------------------------------------------------


class UvError(Exception):
    def __init__(self, output: str) -> None:
        super().__init__(output)
        self.output = output


def find_uv() -> str | None:
    """The uv binary: the ``uv`` package Nodestro depends on, else one on the PATH."""
    try:
        from uv import find_uv_bin

        return str(find_uv_bin())
    except (ImportError, FileNotFoundError):
        return shutil.which("uv")


class UvRunner:
    """Runs uv with Nodestro's cache; one method per operation, output goes to ``log``."""

    def __init__(self, cache_dir: Path, binary: str | None = None) -> None:
        self.cache_dir = cache_dir
        self._binary = binary

    @property
    def binary(self) -> str | None:
        if self._binary is None:
            self._binary = find_uv()
        return self._binary

    def version(self) -> str | None:
        if self.binary is None:
            return None
        try:
            output = subprocess.run([self.binary, "--version"], capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return output.stdout.strip().removeprefix("uv ").split(" ")[0] or None

    def _environment(self) -> dict[str, str]:
        """An allowlist of Nodestro's environment (see :data:`UV_ENV_ALLOWED`) plus uv's settings.

        Secrets such as provider keys or tokens never reach uv, its build backends or the code of
        the packages they build. ``VIRTUAL_ENV``, ``PYTHONPATH`` and the like are not on the list,
        so Nodestro's own environment never leaks into the tool environments.
        """
        environment = uv_environment()
        environment.update({"UV_CACHE_DIR": str(self.cache_dir), "NO_COLOR": "1", "UV_NO_PROGRESS": "1"})
        return environment

    def _run(self, args: list[str], log: Callable[[str], None], *, timeout: float = 900) -> str:
        if self.binary is None:
            raise UvError("uv is not available")
        log("$ uv " + " ".join(args))
        try:
            completed = subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                env=self._environment(),
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise UvError(f"uv did not finish within {timeout:g} seconds") from exc
        except OSError as exc:
            raise UvError(f"uv could not be started: {exc}") from exc
        # uv's output is shown in the installation log and stored with the environment: credentials
        # (e.g. of a private index URL) are removed from it.
        for line in completed.stderr.splitlines():
            if line.strip():
                log(redact(line))
        if completed.returncode != 0:
            output = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            raise UvError(redact(output))
        return completed.stdout

    def compile(self, requirements: list[str], *, python: str, previous: str, log: Callable[[str], None]) -> str:
        """Resolve exact versions of all packages; ``previous`` pins are kept where possible."""
        with tempfile.TemporaryDirectory(prefix="nodestro-resolve-") as directory:
            source = Path(directory) / "requirements.in"
            output = Path(directory) / "requirements.txt"
            source.write_text("\n".join(requirements) + "\n", encoding="utf-8")
            if previous:
                output.write_text(previous, encoding="utf-8")
            self._run(
                [
                    "pip",
                    "compile",
                    str(source),
                    "-o",
                    str(output),
                    "--universal",
                    "--python-version",
                    python,
                    "--no-header",
                    "--quiet",
                ],
                log,
            )
            return output.read_text(encoding="utf-8")

    def create(self, target: Path, *, log: Callable[[str], None]) -> None:
        self._run(["venv", "--quiet", "--relocatable", "--python", sys.executable, str(target)], log)

    def sync(self, interpreter: Path, lock: str, *, log: Callable[[str], None]) -> None:
        with tempfile.TemporaryDirectory(prefix="nodestro-sync-") as directory:
            requirements = Path(directory) / "requirements.txt"
            requirements.write_text(lock, encoding="utf-8")
            self._run(["pip", "sync", "--python", str(interpreter), str(requirements)], log)

    def prune_cache(self, *, log: Callable[[str], None]) -> None:
        self._run(["cache", "prune"], log, timeout=300)


# ---- lock files -----------------------------------------------------------------------

_LOCK_LINE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;\\]+)\s*(?:;\s*(?P<marker>.+?))?\s*$")


def parse_lock(lock: str) -> list[LockedPackage]:
    """Packages of a ``uv pip compile`` output with the ``# via`` annotations."""
    packages: list[LockedPackage] = []
    current: LockedPackage | None = None
    in_via = False
    for raw in lock.splitlines():
        line = raw.strip()
        match = _LOCK_LINE.match(line)
        if match:
            current = LockedPackage(
                name=match.group("name"),
                key=normalize(match.group("name")),
                version=match.group("version"),
                marker=(match.group("marker") or "").strip(),
            )
            packages.append(current)
            in_via = False
            continue
        if current is None or not line.startswith("#"):
            continue
        text = line.lstrip("#").strip()
        if text.startswith("via"):
            rest = text[3:].strip()
            in_via = not rest
            if rest:
                _add_via(current, rest)
        elif in_via and text:
            _add_via(current, text)
    return packages


def _add_via(package: LockedPackage, source: str) -> None:
    if source.startswith("-r ") or source.startswith("-c "):
        package.direct = True
    else:
        package.required_by.append(normalize(source))


# ---- the manager ----------------------------------------------------------------------


class PythonEnvironments:
    """Requirements, plans, approvals and the environments of all scopes."""

    def __init__(
        self,
        store: Store,
        root: Path,
        tools: ToolLibrary,
        *,
        runner: Any | None = None,
        sandbox_kind: Callable[[], str] = lambda: "subprocess",
        workflow_exists: Callable[[str], bool] = lambda _id: True,
    ) -> None:
        self.store = store
        self.root = root
        self.tools = tools
        self.runner = runner if runner is not None else UvRunner(root / "cache")
        self.sandbox_kind = sandbox_kind
        self.workflow_exists = workflow_exists
        self._plans: dict[str, tuple[Plan, Needed]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.RLock()
        self._analysis: dict[str, ToolDependencies] = {}

    # ---- identity and paths ----------------------------------------------------------

    @property
    def installation(self) -> str:
        """A random id of this data directory, kept outside the database (backups do not carry it)."""
        path = self.root / "installation-id"
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            pass
        value = uuid.uuid4().hex
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return value

    def env_dir(self, scope: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", scope)[:80]
        return self.root / "envs" / f"{slug}-{_sha(scope)[:8]}"

    @staticmethod
    def interpreter(env_dir: Path) -> Path:
        if sys.platform == "win32":  # pragma: no cover - Windows layout
            return env_dir / "Scripts" / "python.exe"
        return env_dir / "bin" / "python"

    def _scope_lock(self, scope: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(scope, threading.Lock())

    # ---- definitions ----------------------------------------------------------------

    def get(self, scope: str) -> PythonEnvironment | None:
        return self.store.get(_COLLECTION, scope, PythonEnvironment)

    def _put(self, env: PythonEnvironment) -> None:
        env.updated_at = now()
        self.store.put(_COLLECTION, env.scope, env)

    def all(self) -> list[PythonEnvironment]:
        return self.store.list(_COLLECTION, PythonEnvironment)

    # ---- what is needed ----------------------------------------------------------------

    def analyze(self, code: str, tests: str = "") -> ToolDependencies:
        key = _sha(code + "\0" + tests)
        cached = self._analysis.get(key)
        if cached is None:
            cached = analyze(code, tests)
            with self._lock:
                if len(self._analysis) > 500:
                    self._analysis.clear()
                self._analysis[key] = cached
        return cached.model_copy(deep=True)

    def needed_for_sources(self, sources: Iterable[tuple[str, str, str]]) -> Needed:
        """``sources``: (tool name, code, tests) of every tool in the scope."""
        requirements: dict[str, NeededRequirement] = {}
        needed = Needed()
        for tool, code, tests in sources:
            dependencies = self.analyze(code, tests)
            if dependencies.import_names():
                needed.modules[tool] = sorted(dependencies.import_names())
            for issue in dependencies.issues:
                needed.issues.append(
                    ToolIssue(
                        tool=tool,
                        code=issue.code,
                        message=issue.message,
                        params=dict(issue.params),
                        blocking=issue in blocking(dependencies),
                    )
                )
            for item in dependencies.requirements:
                entry = requirements.get(item.key)
                if entry is None:
                    entry = requirements[item.key] = NeededRequirement(
                        name=item.name, key=item.key, spec=item.spec, extras=item.extras, marker=item.marker
                    )
                else:
                    if item.spec and item.spec not in entry.spec.split(","):
                        entry.spec = ",".join(part for part in (entry.spec, item.spec) if part)
                    entry.extras = sorted({*entry.extras, *item.extras})
                    if entry.marker != item.marker:
                        entry.marker = ""  # needed under different conditions: always
                if tool not in entry.required_by:
                    entry.required_by.append(tool)
                entry.modules = sorted({*entry.modules, *item.modules})
        needed.requirements = sorted(requirements.values(), key=lambda item: item.key)
        return needed

    def tool_sources(self, names: Iterable[str]) -> list[tuple[str, str, str]]:
        """Code and tests of registered (custom or generated) tools; built-in tools need nothing."""
        sources = []
        for name in dict.fromkeys(names):
            if name not in self.tools or name in self.tools.builtin:
                continue
            info = self.tools.get(name)
            if info.code is not None:
                sources.append((name, info.code, info.tests or ""))
        return sources

    def needed_for_workflow(self, workflow: WorkflowIn) -> Needed:
        names = [name for node in workflow.nodes if node.agent is not None for name in node.agent.tools]
        return self.needed_for_sources(self.tool_sources(names))

    # ---- status ------------------------------------------------------------------------

    def _approved(self, env: PythonEnvironment) -> dict[str, PackageApproval]:
        installation = self.installation
        return {item.key: item for item in env.approvals if item.installation == installation}

    def _installed_sha(self, scope: str) -> str | None:
        marker = self.env_dir(scope) / _MARKER
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not self.interpreter(self.env_dir(scope)).exists():
            return None
        sha = data.get("lock_sha")
        return sha if isinstance(sha, str) else None

    def status(self, kind: ScopeKind, ref: str, needed: Needed, *, label: str = "") -> DependencyStatus:
        scope = scope_of(kind, ref)
        env = self.get(scope)
        result = DependencyStatus(
            scope=scope, kind=kind, ref=ref, label=label or (env.label if env else ""), state="none"
        )
        result.needed, result.issues = needed.requirements, needed.issues
        if env is not None:
            result.packages, result.python_version = env.packages, env.python_version
            result.install, result.history = env.install, env.history[-30:]
            approved = self._approved(env)
            result.approved_at = max((item.approved_at for item in approved.values()), default=None)
        if not needed.requirements and not needed.blocking:
            return result
        if needed.blocking:
            result.state = "unresolved"
            return result
        if self.sandbox_kind() == "docker":
            result.state = "unsupported"
            result.problem = problem(
                "deps.docker_unsupported",
                "Tools with third-party libraries run in the subprocess sandbox only; "
                "switch the sandbox in the settings",
            )
            return result
        if self.runner.binary is None:
            result.state = "unsupported"
            result.problem = problem("deps.uv_missing", "uv was not found; reinstall Nodestro or install uv")
            return result
        pending, reasons = self._pending(env, needed)
        result.pending, result.reasons = pending, reasons
        if pending or reasons:
            result.state = "approval_needed"
            return result
        assert env is not None
        if env.install.status == "installing":
            result.state = "installing"
        elif self._installed_sha(scope) == _sha(env.lock):
            result.state = "ready"
        elif env.install.status == "failed" and env.install.lock_sha == _sha(env.lock):
            result.state = "failed"
        else:
            result.state = "install_needed"
        return result

    def _pending(self, env: PythonEnvironment | None, needed: Needed) -> tuple[list[str], list[str]]:
        """Needed packages without a valid approval, and why."""
        if env is None or not env.lock:
            return [item.name for item in needed.requirements], ["new"]
        reasons: set[str] = set()
        if env.python_version != current_python_version():
            reasons.add("python")
        locked = {item.key: item for item in env.packages}
        approved_requirements = {item.key: item for item in env.requirements}
        approvals = self._approved(env)
        pending: list[str] = []
        for requirement in needed.requirements:
            package = locked.get(requirement.key)
            if package is None:
                pending.append(requirement.name)
                reasons.add("new")
                continue
            approved = approved_requirements.get(requirement.key)
            if not _satisfies(package.version, requirement.spec) or (
                approved is not None and not set(requirement.extras) <= set(approved.extras)
            ):
                pending.append(requirement.name)
                reasons.add("changed")
        missing = [
            package
            for package in env.packages
            if not package.runtime
            and (package.key not in approvals or approvals[package.key].version != package.version)
        ]
        if missing:
            foreign = any(item.installation != self.installation for item in env.approvals)
            reasons.add("other_installation" if foreign else "new")
            pending += [item.name for item in missing if item.name not in pending and item.direct]
        return pending, sorted(reasons)

    # ---- plans -------------------------------------------------------------------------

    def plan(self, kind: ScopeKind, ref: str, needed: Needed, *, label: str = "") -> Plan:
        """Resolve all versions for the approval overview; nothing is installed."""
        scope = scope_of(kind, ref)
        plan = Plan(
            scope=scope, kind=kind, ref=ref, label=label, requirements=needed.requirements, issues=needed.issues
        )
        if needed.blocking:
            plan.error = problem("deps.unresolved", "Some imports cannot be resolved; see the issues")
            return plan
        status = self.status(kind, ref, needed, label=label)
        if status.problem is not None:
            plan.error = status.problem
            return plan
        if not needed.requirements:
            return plan
        env = self.get(scope)
        previous = env.lock if env is not None and env.python_version == current_python_version() else ""
        lines = [item.line() for item in needed.requirements] + _runtime_requirements()
        log: list[str] = []
        try:
            plan.lock = self.runner.compile(lines, python=current_python_version(), previous=previous, log=log.append)
        except UvError as exc:
            plan.error = explain_uv_error(exc.output)
            return plan
        plan.packages = self._plan_packages(env, parse_lock(plan.lock), needed)
        plan.needs_approval = any(item.status in ("new", "changed") for item in plan.packages) or bool(status.reasons)
        with self._lock:
            cutoff = now() - _PLAN_TTL
            for key in [key for key, (old, _) in self._plans.items() if old.created_at < cutoff]:
                del self._plans[key]
            self._plans[plan.id] = (plan, needed)
        return plan

    def _plan_packages(
        self, env: PythonEnvironment | None, locked: list[LockedPackage], needed: Needed
    ) -> list[PlanPackage]:
        runtime = {normalize(name) for name in RUNTIME_PACKAGES}
        direct = {item.key: item for item in needed.requirements}
        _mark_runtime(locked, runtime)
        approvals = self._approved(env) if env is not None else {}
        old = {item.key: item for item in env.packages} if env is not None else {}
        elsewhere = self._approved_elsewhere(env.scope if env else None)
        packages: list[PlanPackage] = []
        for item in locked:
            requirement = direct.get(item.key)
            required_by = requirement.required_by if requirement else item.required_by
            approval = approvals.get(item.key)
            previous = old.get(item.key)
            if item.runtime and requirement is None:
                status: PackageStatus = "runtime"
            elif approval is not None and approval.version == item.version:
                status = "approved"
            elif previous is not None and previous.version != item.version:
                status = "changed"
            else:
                status = "new"
            packages.append(
                PlanPackage(
                    name=item.name,
                    key=item.key,
                    version=item.version,
                    previous_version=previous.version if previous and previous.version != item.version else None,
                    marker=item.marker,
                    direct=requirement is not None,
                    runtime=item.runtime and requirement is None,
                    required_by=required_by,
                    status=status,
                    approved_elsewhere=elsewhere.get((item.key, item.version), []),
                )
            )
        kept = {item.key for item in locked}
        for key, previous in old.items():
            if key not in kept:
                packages.append(
                    PlanPackage(
                        name=previous.name,
                        key=key,
                        version=previous.version,
                        direct=previous.direct,
                        runtime=previous.runtime,
                        required_by=previous.required_by,
                        status="removed",
                    )
                )
        order = {"new": 0, "changed": 1, "removed": 2, "approved": 3, "runtime": 4}
        packages.sort(key=lambda item: (not item.direct, order[item.status], item.key))
        return packages

    def _approved_elsewhere(self, scope: str | None) -> dict[tuple[str, str], list[str]]:
        installation = self.installation
        found: dict[tuple[str, str], list[str]] = {}
        for env in self.all():
            if env.scope == scope:
                continue
            for approval in env.approvals:
                if approval.installation == installation:
                    found.setdefault((approval.key, approval.version), []).append(env.label or env.scope)
        return found

    def get_plan(self, plan_id: str) -> tuple[Plan, Needed]:
        with self._lock:
            found = self._plans.get(plan_id)
        if found is None:
            raise NodestroError("deps.plan_expired", "This approval overview has expired; please open it again")
        return found

    def approve(
        self, plan_id: str, *, wait: bool = False, log: Callable[[str], None] | None = None
    ) -> PythonEnvironment:
        """The user approved the plan: store lock and approvals, then install.

        The installation runs in a background thread, or in this one with ``wait=True``.
        """
        plan, needed = self.get_plan(plan_id)
        if plan.error is not None or not plan.lock:
            raise NodestroError("deps.plan_invalid", "This overview has nothing to approve")
        with self._lock:
            env = self._begin_install(plan.scope, approve=(plan, needed))
        if wait:
            self._install(env.scope, needed.all_modules(), log)
            return self.get(env.scope) or env
        threading.Thread(
            target=self._install,
            args=(env.scope, needed.all_modules(), log),
            name=f"python-env-{env.scope}",
            daemon=True,
        ).start()
        return env

    def _begin_install(self, scope: str, *, approve: tuple[Plan, Needed] | None = None) -> PythonEnvironment:
        """Mark the scope as installing (after storing an approved plan); refuses a second installation.

        Called with ``_lock`` held, so two requests at the same time never both start one.
        """
        if approve is None:
            env = self.get(scope)
            assert env is not None
            if env.install.status == "installing":
                raise NodestroError("deps.installing", "An installation is already running for this environment")
            env.install = InstallState(status="installing", step="deps.step.queued", started_at=now())
            self._put(env)
            return env
        plan, needed = approve
        env = self.get(plan.scope) or PythonEnvironment(scope=plan.scope, kind=plan.kind, ref=plan.ref)
        if env.install.status == "installing":
            raise NodestroError("deps.installing", "An installation is already running for this environment")
        installation = self.installation
        approvals = {item.key: item for item in env.approvals if item.installation == installation}
        details = []
        locked = parse_lock(plan.lock)
        _mark_runtime(locked, {normalize(name) for name in RUNTIME_PACKAGES})
        by_key = {item.key: item for item in plan.packages}
        for package in locked:
            info = by_key.get(package.key)
            package.direct = bool(info and info.direct)
            if info is not None:
                package.required_by = info.required_by
            if package.runtime and not package.direct:
                continue
            current = approvals.get(package.key)
            if current is None or current.version != package.version:
                approvals[package.key] = PackageApproval(
                    key=package.key, name=package.name, version=package.version, installation=installation
                )
            if info is not None and info.status in ("new", "changed"):
                change = f"{info.previous_version} → {info.version}" if info.previous_version else info.version
                details.append(f"{package.name} {change} ({info.status})")
        details += [f"{item.name} {item.version} (removed)" for item in plan.packages if item.status == "removed"]
        keys = {item.key for item in locked}
        env.label = plan.label or env.label
        env.requirements = needed.requirements
        env.lock = plan.lock
        env.packages = locked
        env.python_version = plan.python_version
        env.approvals = [item for key, item in approvals.items() if key in keys]
        env.history.append(EnvChange(kind="approved", details=details or ["confirmed"]))
        env.install = InstallState(status="installing", step="deps.step.queued", started_at=now())
        self._put(env)
        return env

    # ---- installation ----------------------------------------------------------------

    def install(self, scope: str, modules: Iterable[str] = (), *, wait: bool = True) -> PythonEnvironment:
        """(Re)build the environment from the approved lock; no new versions, no new approvals."""
        env = self.get(scope)
        if env is None or not env.lock:
            raise NodestroError("deps.no_definition", "There is nothing approved to install for this environment")
        approvals = self._approved(env)
        missing = [p.name for p in env.packages if not p.runtime and approvals.get(p.key, None) is None]
        if missing or env.python_version != current_python_version():
            raise NodestroError(
                "deps.approval_needed",
                f"Approval needed first: {', '.join(missing) or 'Python version changed'}",
                packages=", ".join(missing),
            )
        with self._lock:
            env = self._begin_install(scope)
        if wait:
            self._install(scope, modules, None)
            return self.get(scope) or env
        threading.Thread(target=self._install, args=(scope, list(modules), None), daemon=True).start()
        return env

    def _install(self, scope: str, modules: Iterable[str], log: Callable[[str], None] | None) -> None:
        with self._scope_lock(scope):
            env = self.get(scope)
            if env is None:
                return
            state = env.install
            state.log = []

            def write(line: str) -> None:
                state.log.append(line[:500])
                del state.log[:-_MAX_LOG_LINES]
                if log is not None:
                    log(line)

            def step(code: str) -> None:
                state.step = code
                self._update_install(scope, state)

            target = self.env_dir(scope)
            staging = target.with_name(target.name + ".staging")
            _restore_swap(target)
            try:
                shutil.rmtree(staging, ignore_errors=True)
                staging.parent.mkdir(parents=True, exist_ok=True)
                step("deps.step.create")
                self.runner.create(staging, log=write)
                step("deps.step.install")
                self.runner.sync(self.interpreter(staging), env.lock, log=write)
                step("deps.step.verify")
                missing = verify_imports(self.interpreter(staging), [*modules, "pydantic"])
                if missing:
                    raise _InstallProblem(
                        problem(
                            "deps.module_missing",
                            f"After the installation these modules cannot be found: {', '.join(missing)}",
                            modules=", ".join(missing),
                        )
                    )
                (staging / _MARKER).write_text(
                    json.dumps({"scope": scope, "lock_sha": _sha(env.lock), "installed_at": now().isoformat()}),
                    encoding="utf-8",
                )
                old = target.with_name(target.name + ".old")
                shutil.rmtree(old, ignore_errors=True)
                if target.exists():
                    target.replace(old)
                staging.replace(target)
                shutil.rmtree(old, ignore_errors=True)
                state.status, state.step, state.error = "installed", None, None
                kind: ChangeKind = "installed"
                details = [f"{len(env.packages)} packages"]
            except _InstallProblem as exc:
                state.status, state.error = "failed", exc.problem
                kind, details = "install_failed", [exc.problem.message]
            except UvError as exc:
                found = explain_uv_error(exc.output)
                state.status, state.error = "failed", found
                kind, details = "install_failed", [found.message[:300]]
            except Exception as exc:  # any other failure must leave a usable state
                logger.exception("Installing the environment %s failed", scope)
                state.status, state.error = "failed", problem("deps.install_failed", f"{type(exc).__name__}: {exc}")
                kind, details = "install_failed", [str(exc)[:300]]
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            state.lock_sha = _sha(env.lock)
            state.finished_at = now()
            latest = self.get(scope) or env
            latest.install = state
            latest.history.append(EnvChange(kind=kind, details=details))
            self._put(latest)

    def recover(self) -> int:
        """After a restart: finish what an interrupted installation left behind.

        An environment that was being replaced when Nodestro stopped is put back (the last
        working one), leftover staging directories are removed, and an installation that was
        still marked as running is marked as failed, so it can be started again. Returns the
        number of installations marked as interrupted.
        """
        envs_root = self.root / "envs"
        if envs_root.is_dir():
            for directory in sorted(envs_root.iterdir()):
                if directory.name.endswith(".old"):
                    _restore_swap(directory.with_name(directory.name.removesuffix(".old")))
                elif directory.name.endswith(".staging"):
                    shutil.rmtree(directory, ignore_errors=True)
        count = 0
        for env in self.all():
            if env.install.status != "installing":
                continue
            with self._lock:
                lock = self._scope_lock(env.scope)
            if lock.locked():
                continue  # an installation of this process is running
            env.install.status = "failed"
            env.install.finished_at = now()
            env.install.error = problem("deps.install_interrupted", "Nodestro stopped during the installation")
            env.install.lock_sha = _sha(env.lock)
            env.history.append(EnvChange(kind="install_failed", details=["interrupted"]))
            self._put(env)
            count += 1
        return count

    def _update_install(self, scope: str, state: InstallState) -> None:
        env = self.get(scope)
        if env is not None:
            env.install = state
            self._put(env)

    def ensure(self, kind: ScopeKind, ref: str, needed: Needed) -> DependencyStatus:
        """Make an approved environment usable, installing it from the lock if needed."""
        status = self.status(kind, ref, needed)
        if status.state == "install_needed":
            self.install(status.scope, needed.all_modules())
            status = self.status(kind, ref, needed)
        return status

    # ---- sandbox ----------------------------------------------------------------------

    def sandbox(self, scope: str) -> Sandbox | None:
        """A sandbox whose interpreter is the scope's environment, if it is installed."""
        env = self.get(scope)
        if env is None or not env.lock or self._installed_sha(scope) != _sha(env.lock):
            return None
        return SubprocessSandbox(
            timeout=60.0,
            memory_mb=2048,
            python=str(self.interpreter(self.env_dir(scope))),
            env=SANDBOX_ENV,
            acknowledge_unsafe=True,
        )

    def workflow_sandbox(self, workflow_id: str) -> Sandbox | None:
        return self.sandbox(scope_of("workflow", workflow_id))

    # ---- removal, copies, export -------------------------------------------------------

    def remove_environment(self, scope: str) -> None:
        """Delete the installed environment; the approved definition stays and can be reinstalled."""
        with self._scope_lock(scope):
            shutil.rmtree(self.env_dir(scope), ignore_errors=True)
        env = self.get(scope)
        if env is not None:
            env.install = InstallState()
            env.history.append(EnvChange(kind="removed", details=["environment deleted"]))
            self._put(env)

    def forget(self, scope: str) -> None:
        """The owner is gone: delete the definition and the environment."""
        with self._scope_lock(scope):
            shutil.rmtree(self.env_dir(scope), ignore_errors=True)
        self.store.delete(_COLLECTION, scope)

    def export(self, scope: str) -> ExportedDefinition | None:
        env = self.get(scope)
        if env is None or not env.lock:
            return None
        return ExportedDefinition(
            requirements=env.requirements, lock=env.lock, packages=env.packages, python_version=env.python_version
        )

    def adopt(
        self, kind: ScopeKind, ref: str, definition: ExportedDefinition, *, label: str, change: ChangeKind
    ) -> None:
        """Store an imported or copied definition; its packages must be approved again here."""
        if not definition.lock:
            return
        env = PythonEnvironment(
            scope=scope_of(kind, ref),
            kind=kind,
            ref=ref,
            label=label,
            requirements=definition.requirements,
            lock=definition.lock,
            packages=parse_lock(definition.lock) if not definition.packages else definition.packages,
            python_version=definition.python_version,
            history=[EnvChange(kind=change, details=[f"{len(definition.packages)} packages, approval required"])],
        )
        self._put(env)

    # ---- maintenance -----------------------------------------------------------------

    def python_status(self) -> PythonStatus:
        definitions = {env.scope: env for env in self.all()}
        environments: list[EnvironmentInfo] = []
        seen: set[str] = set()
        envs_root = self.root / "envs"
        if envs_root.is_dir():
            for directory in sorted(envs_root.iterdir()):
                if not directory.is_dir() or directory.name.endswith((".staging", ".old")):
                    continue
                marker: dict[str, Any] = {}
                with contextlib.suppress(OSError, ValueError):
                    marker = json.loads((directory / _MARKER).read_text(encoding="utf-8"))
                scope = str(marker.get("scope") or directory.name)
                seen.add(scope)
                env = definitions.get(scope)
                info = EnvironmentInfo(
                    scope=scope,
                    path=str(directory),
                    size_bytes=_size(directory),
                    has_definition=env is not None,
                    packages=len(env.packages) if env else 0,
                    installed_at=_parse_time(marker.get("installed_at")),
                )
                self._describe(info, env)
                environments.append(info)
        for scope, env in definitions.items():
            if scope not in seen:
                info = EnvironmentInfo(scope=scope, path="", has_definition=True, packages=len(env.packages))
                self._describe(info, env)
                environments.append(info)
        return PythonStatus(
            uv_available=self.runner.binary is not None,
            uv_version=self.runner.version(),
            environments=environments,
            cache_bytes=_size(self.root / "cache"),
            root=str(self.root),
        )

    def _describe(self, info: EnvironmentInfo, env: PythonEnvironment | None) -> None:
        kind, _, ref = info.scope.partition(":")
        if kind in ("workflow", "tool"):
            info.kind, info.ref = kind, ref  # type: ignore[assignment]
        info.label = env.label if env else ""
        if kind == "workflow":
            info.orphaned = not self.workflow_exists(ref)
        elif kind == "tool":
            info.orphaned = ref not in self.tools
        else:
            info.orphaned = True

    def cleanup(self, *, prune_cache: bool = True) -> list[str]:
        """Remove environments no workflow or tool owns any more, and unused cache entries."""
        removed = []
        for info in self.python_status().environments:
            if info.orphaned:
                self.forget(info.scope)
                if info.path:
                    shutil.rmtree(info.path, ignore_errors=True)
                removed.append(info.scope)
        if prune_cache and self.runner.binary is not None:
            try:
                self.runner.prune_cache(log=lambda _line: None)
            except UvError:
                logger.warning("Pruning the uv cache failed", exc_info=True)
        return removed


def _restore_swap(target: Path) -> None:
    """Put back the previous environment when a replacement was interrupted between its two renames."""
    old = target.with_name(target.name + ".old")
    if old.is_dir() and not target.exists():
        try:
            old.replace(target)
        except OSError:
            logger.warning("Restoring the environment %s failed", target, exc_info=True)
            return
        logger.info("Restored the environment %s after an interrupted installation", target)
    elif old.is_dir():
        shutil.rmtree(old, ignore_errors=True)


class _InstallProblem(Exception):
    def __init__(self, found: Problem) -> None:
        super().__init__(found.message)
        self.problem = found


def _runtime_requirements() -> list[str]:
    """The sandbox runner needs pydantic; pin the version Nodestro itself uses."""
    from importlib.metadata import PackageNotFoundError, version

    lines = []
    for name in RUNTIME_PACKAGES:
        try:
            lines.append(f"{name}=={version(name)}" if name == "pydantic" else name)
        except PackageNotFoundError:  # pragma: no cover - pydantic is a dependency of Nodestro
            lines.append(name)
    return lines


def _mark_runtime(packages: list[LockedPackage], runtime: set[str]) -> None:
    """Runtime packages and everything only they need."""
    by_key = {item.key: item for item in packages}
    marked = set(runtime) & set(by_key)
    changed = True
    while changed:
        changed = False
        for item in packages:
            if item.key in marked or item.direct:
                continue
            if item.required_by and all(source in marked for source in item.required_by):
                marked.add(item.key)
                changed = True
    for key in marked:
        by_key[key].runtime = True


def _satisfies(version: str, spec: str) -> bool:
    if not spec:
        return True
    try:
        return SpecifierSet(spec).contains(Version(version), prereleases=True)
    except (InvalidSpecifier, InvalidVersion):
        return False


def verify_imports(interpreter: Path, modules: Iterable[str]) -> list[str]:
    """Modules the environment's interpreter cannot find (without importing them)."""
    names = sorted({name for name in modules if name})
    script = (
        "import importlib.util, json, sys\n"
        "print(json.dumps([m for m in json.loads(sys.argv[1]) if importlib.util.find_spec(m) is None]))\n"
    )
    try:
        completed = subprocess.run(
            [str(interpreter), "-I", "-c", script, json.dumps(names)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")} if sys.platform == "win32" else {},
        )
        missing = json.loads(completed.stdout or "null")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return names
    return [str(name) for name in missing] if isinstance(missing, list) else names


_NOT_FOUND = re.compile(r"Because (?P<name>[A-Za-z0-9._-]+) was not found in the package registry")
_NO_VERSION = re.compile(r"there is no version of (?P<name>[A-Za-z0-9._-]+)(?P<spec>[^\s,]*)")


def explain_uv_error(output: str) -> Problem:
    """A code for the UI and uv's own explanation as the technical text."""
    text = output.strip()
    if text == "uv is not available":
        return problem("deps.uv_missing", "uv was not found; reinstall Nodestro or install uv")
    match = _NOT_FOUND.search(text)
    if match:
        return problem("deps.not_found", text, name=match.group("name"))
    if "No solution found" in text or "unsatisfiable" in text:
        found = _NO_VERSION.search(text)
        if found:
            return problem("deps.no_version", text, name=found.group("name"), spec=found.group("spec"))
        return problem("deps.conflict", text)
    lowered = text.lower()
    if any(word in lowered for word in ("failed to fetch", "error sending request", "dns", "connect", "offline")):
        return problem("deps.network", text)
    if "did not finish within" in text:
        return problem("deps.timeout", text)
    return problem("deps.uv_failed", text)


def _size(path: Path) -> int:
    total = 0
    seen: set[tuple[int, int]] = set()
    if not path.exists():
        return 0
    for directory, _dirs, files in os.walk(path):
        for name in files:
            try:
                stat = (Path(directory) / name).lstat()
            except OSError:
                continue
            key = (stat.st_dev, stat.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += stat.st_size
    return total


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def workflow_label(workflow: Workflow | WorkflowIn) -> str:
    return workflow.name
