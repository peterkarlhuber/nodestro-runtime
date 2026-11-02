"""The tool library: built-in, hand-written and AI-generated tools.

Built-in tools are trusted ``@Tool`` functions. Tools a user writes in the
editor and tools the Coding Agent generates are stored in a
``orkexa.dynamic.ToolRegistry``: their code is checked statically, their
tests run in the sandbox, and every call runs in the sandbox as well.

Before any stored or proposed code runs, the :class:`~nodestro_runtime.sandbox_policy.SandboxPolicy`
checks its trust class against the sandbox (ADR-0006): AI-generated code and code of unknown
provenance runs only in an isolating sandbox, never in a host subprocess.

Generating a tool takes a while and ends with a human decision, so it runs as a
:class:`ToolJob` in a background thread: ``ToolBuilder`` does the work, and its
``CallbackApprover`` waits until the user approves or rejects in the UI.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import threading
from typing import TYPE_CHECKING, Any, Literal

from orkexa import Tool, ToolExecutionError, ToolInputError, ToolResultError, UsageTracker
from orkexa.dynamic import (
    ApprovalDeniedError,
    ApprovalRequest,
    CallbackApprover,
    CodingAgent,
    DockerSandbox,
    DynamicAgentError,
    DynamicPolicy,
    RegistryError,
    Sandbox,
    SandboxedTool,
    SubprocessSandbox,
    ToolBuilder,
    ToolProposal,
    ToolRegistry,
    check_tool_source,
    check_tool_tests,
)
from orkexa.dynamic.coding_agent import DEFAULT_CODING_PROMPT
from orkexa.dynamic.policy import RESERVED_TOOL_NAMES
from orkexa.memory import MEMORY_TOOL_NAME
from orkexa.providers.base import redact
from pydantic import BaseModel, Field, model_validator

from .bedrock import redact_identity
from .builtin_sandbox import LinuxSandbox
from .builtin_tools import BUILTIN_TOOLS
from .mcp_access import is_mcp_tool
from .problems import NodestroError, Problem, problem
from .providers import Connection, qualify
from .sandbox_policy import HOST_AWS_ENV, Isolation, SandboxPolicy, TrustClass, isolation_of
from .schemas import new_id, now
from .tool_policy import UNKNOWN, ToolCapability, capability_of
from .tool_provenance import MemoryProvenance, ToolProvenance, code_digest

if TYPE_CHECKING:
    from .mcp.models import McpTool
    from .mcp.service import McpService

logger = logging.getLogger(__name__)

ToolKind = Literal["builtin", "custom", "generated", "mcp"]
_APPROVAL_TIMEOUT_SECONDS = 3600
#: Names no own or generated tool may have: Orkexa's dynamic-agent tools and the agent's system memory tool
#: (re-review R-2: a tool named ``memory`` would otherwise pass for the memory tool).
RESERVED_NAMES: frozenset[str] = RESERVED_TOOL_NAMES | {MEMORY_TOOL_NAME}

# Tools are technical artefacts: whatever language the requirement is written in, the code, the
# names, the docstring (which becomes the tool description agents read) and the tests stay English.
CODING_PROMPT = (
    DEFAULT_CODING_PROMPT
    + """
Write everything in English, whatever language the requirement uses: identifiers, the
docstring, comments, error messages, test names and the `description`.

Exception to the import rule above: if a well-known third-party library (e.g. numpy, pandas,
scipy) clearly makes the tool better, you may import it. Prefer the standard library for simple
tasks. Declare every third-party package at the very top of `code` in an inline metadata block,
with a version range when it matters:

# /// script
# dependencies = ["pandas>=2.2", "numpy"]
# ///

The user approves each library before it is installed; if they decline, you will be asked to
use the standard library only.
"""
)

CUSTOM_TOOL_TEMPLATE = '''\
def celsius_to_fahrenheit(celsius: float) -> float:
    """Convert a temperature from degrees Celsius to degrees Fahrenheit.

    Args:
        celsius: Temperature in degrees Celsius.

    Returns:
        The temperature in degrees Fahrenheit.
    """
    return celsius * 9 / 5 + 32
'''

CUSTOM_TESTS_TEMPLATE = """\
from tool import celsius_to_fahrenheit


def test_freezing_point():
    assert celsius_to_fahrenheit(0) == 32


def test_boiling_point():
    assert celsius_to_fahrenheit(100) == 212
"""


class ToolInfo(BaseModel):
    name: str
    kind: ToolKind
    #: Who wrote the code (ADR-0006): ``trusted`` (built in), ``user-authored`` (the tool editor) or
    #: ``untrusted`` (AI-generated or unknown provenance; runs only in an isolating sandbox).
    trust: TrustClass = "untrusted"
    description: str
    input_schema: dict[str, Any] | None = None
    version: int = 1
    created_at: str | None = None
    function_name: str | None = None
    code: str | None = None
    tests: str | None = None
    requirement: str | None = None
    #: MCP tools (``kind == "mcp"``): the server, the server's own tool name and the approved definition's hash.
    server_id: str | None = None
    server_name: str | None = None
    mcp_name: str | None = None
    definition_hash: str | None = None
    #: What the tool may do (shared model S-1, :mod:`nodestro_runtime.tool_policy`). Set for every tool; without a
    #: statement ``write`` + ``external``. Information for people and for the approval rules, not proof of behavior.
    capability: ToolCapability = UNKNOWN

    @model_validator(mode="before")
    @classmethod
    def _default_capability(cls, data: Any) -> Any:
        # Created without a statement (older callers): what the kind and name alone allow, never more.
        if isinstance(data, dict) and data.get("capability") is None:
            data = {**data, "capability": capability_of(name=str(data.get("name", "")), kind=str(data.get("kind")))}
        return data


class TestResult(BaseModel):
    name: str
    ok: bool
    error: str | None = None


class CheckResult(BaseModel):
    ok: bool
    #: The tool needs third-party packages that are not approved and installed for its tests yet.
    dependencies_pending: bool = False
    #: Problems with a code for the UI; library check messages keep their technical text in ``message``.
    errors: list[Problem] = Field(default_factory=list)
    tests: list[TestResult] = Field(default_factory=list)
    output: str = ""
    name: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] | None = None


class ToolAttempt(BaseModel):
    number: int
    function_name: str
    description: str
    code: str
    tests: str


JobStatus = Literal["running", "awaiting_dependencies", "awaiting_approval", "approved", "rejected", "failed"]


class ToolJob(BaseModel):
    id: str = Field(default_factory=lambda: new_id("job"))
    requirement: str
    status: JobStatus = "running"
    #: The current step: ``message_code``/``message_params`` for the UI, ``message`` in English.
    message: str = "The Coding Agent is writing code and tests"
    message_code: str = "job.writing"
    message_params: dict[str, Any] = Field(default_factory=dict)
    attempts: list[ToolAttempt] = Field(default_factory=list)
    approval: dict[str, Any] | None = None
    #: The approval overview of third-party packages the latest proposal needs (status
    #: ``awaiting_dependencies``); ``dependency_issues`` explain imports that cannot be resolved.
    dependency_plan: dict[str, Any] | None = None
    #: Package names the generated tool uses (approved for its test environment).
    dependencies: list[str] = Field(default_factory=list)
    tool_name: str | None = None
    error: str | None = None
    cost_usd: float | None = None
    created_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None


class _ReservingToolBuilder(ToolBuilder):
    """Orkexa's tool builder, refusing Nodestro's reserved names too (:data:`RESERVED_NAMES`, re-review R-2): the
    Coding Agent gets the refusal as feedback and proposes another name."""

    def _validate(self, proposal: Any) -> tuple[list[str], Any]:
        if proposal.function_name in RESERVED_NAMES:
            return [f"{proposal.function_name!r} is a reserved tool name; choose another"], None
        return super()._validate(proposal)


class _RecordingCodingAgent(CodingAgent):
    """A Coding Agent that reports every proposal to its job."""

    def __init__(
        self, job: ToolJob, *, on_proposal: Callable[[ToolProposal], ToolProposal] | None = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.job = job
        self.on_proposal = on_proposal

    def ask_again(self, feedback: str) -> ToolProposal:
        """A corrected proposal without the attempt bookkeeping (dependency feedback)."""
        return self._record(super().propose("", feedback=feedback))

    def propose(self, requirement: str, *, feedback: str | None = None) -> ToolProposal:
        number = len(self.job.attempts) + 1
        if feedback is None:
            _step(self.job, problem("job.writing", "The Coding Agent is writing code and tests"))
        else:
            _step(
                self.job,
                problem("job.fixing", f"Attempt {number}: the Coding Agent is correcting its proposal", attempt=number),
            )
        proposal = self._record(super().propose(requirement, feedback=feedback))
        if self.on_proposal is not None:
            proposal = self.on_proposal(proposal)
        _step(
            self.job,
            problem("job.testing", f"Attempt {number}: static check and tests in the sandbox", attempt=number),
        )
        return proposal

    def _record(self, proposal: ToolProposal) -> ToolProposal:
        number = len(self.job.attempts) + 1
        self.job.attempts.append(
            ToolAttempt(
                number=number,
                function_name=proposal.function_name,
                description=proposal.description,
                code=proposal.code,
                tests=proposal.tests,
            )
        )
        return proposal


def _step(job: ToolJob, found: Problem) -> None:
    job.message, job.message_code, job.message_params = found.message, found.code, found.params


class _Decision:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.approved = False
        #: The answer to a dependency overview: "approve", "without" or "cancel".
        self.dependencies = threading.Event()
        self.dependency_choice = "cancel"


DependencyChoice = Literal["approve", "without", "cancel"]


class DependencyOutcome(BaseModel):
    """What a tool proposal needs before its tests can run (see :class:`DependencyGate`)."""

    model_config = {"arbitrary_types_allowed": True}

    #: Ready: run the tests with these approved modules in this sandbox (``None``: the default one).
    ready: bool = False
    sandbox: Any = None
    extra_imports: list[str] = Field(default_factory=list)
    packages: list[str] = Field(default_factory=list)
    #: Imports that cannot be resolved: sent back to the Coding Agent.
    feedback: str | None = None
    #: The approval overview to show the user.
    plan: dict[str, Any] | None = None


class DependencyGate:
    """Connects tool jobs to the Python environments (set by the application context)."""

    def prepare(self, name: str, code: str, tests: str) -> DependencyOutcome:  # pragma: no cover - interface
        return DependencyOutcome(ready=True)

    def install(self, plan_id: str, log: Callable[[str], None]) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def make_sandbox(
    kind: str, policy: SandboxPolicy | None = None, container: Callable[[], Sandbox] | None = None
) -> Sandbox:
    """The sandbox of the ``sandbox`` setting: ``docker`` (isolating) or a host subprocess (``subprocess`` and
    ``auto``; in ``auto`` the workspace also sets :attr:`ToolLibrary.container_fallback`).

    ``container`` builds the container sandbox where Orkexa's default (``orkexa-sandbox`` image, bind mounts) does
    not fit, as on a server. The subprocess sandbox runs only trusted and user-authored code; the policy refuses
    everything else in it (and, in the server profile, refuses to create it at all)."""
    if kind == "docker":
        return container() if container is not None else DockerSandbox()
    (policy or SandboxPolicy()).check_construction("none")
    return SubprocessSandbox(timeout=15.0, env=dict(HOST_AWS_ENV), acknowledge_unsafe=True)


class _LinkedProvenance:
    """Read-only provenance that follows another library (see :meth:`ToolLibrary.linked_provenance`)."""

    def __init__(self, source: ToolLibrary) -> None:
        self.source = source

    def authored(self, name: str) -> set[int]:
        return self.source.provenance.authored(name) if self.source.trust(name) == "user-authored" else set()

    def record_authored(self, name: str, version: int) -> None:
        raise NodestroError("tool.frozen", "Tools of a published release cannot be changed")

    def clear(self) -> None:
        """Nothing to forget: the records belong to the source library."""


_Fingerprint = tuple[tuple[str, str, int, int, int, int], ...]


def _fingerprint(path: Path | None) -> _Fingerprint:
    """A cheap identity of the registry files under ``path``: name, inode, change times and size of each.

    Records are replaced atomically (a new file, so a new inode), so any save, approval or revocation by another
    process changes it."""
    if path is None:
        return ()
    entries = []
    try:
        files = list(path.glob("*/v*.json"))
    except OSError:
        files = []
    for file in files:
        try:
            stat = file.stat()
        except OSError:
            continue
        entries.append((file.parent.name, file.name, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
    return tuple(sorted(entries))


def _stored_current(path: Path, name: str) -> tuple[int, str] | None:
    """Version and code of the latest approved, intact version of ``name`` as stored now, read past every cache;
    ``None`` when there is none (deleted or never stored)."""
    best: tuple[int, str] | None = None
    try:
        files = list((path / name).glob("v*.json"))
    except OSError:
        return None
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("name") != name or data.get("status") != "approved":
            continue
        code, version = data.get("code"), data.get("version")
        if not isinstance(code, str) or not isinstance(version, int) or data.get("kind") != "generated":
            continue
        if data.get("sha256") != hashlib.sha256(code.encode("utf-8")).hexdigest():
            continue
        if best is None or version > best[0]:
            best = (version, code)
    return best


class PinnedTool(SandboxedTool):
    """A stored tool bound to the version it was built from (F1).

    Agents get their tools when a step starts and keep them for the whole step. Before every call this checks, past
    every cache, that this version is still the current approved one, so a tool that another process changed or
    deleted in the meantime never runs in its old form: the call is refused and the step can be started again. The
    per-call rules (S-2) decide on the current version, so an approval or advance approval never makes a different
    version run."""

    def __init__(self, *, version: int, path: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.version = version
        self.path = path

    def current(self) -> bool:
        stored = _stored_current(self.path, str(self.name))
        return stored is not None and stored == (self.version, self.code)

    def execute(self, payload: dict[str, Any]) -> str:
        if not self.current():
            raise ToolExecutionError(
                f"{self.name}: the tool was changed or deleted after this step started (version {self.version}), "
                "so this call was not made; start the run again to use the current version"
            )
        return super().execute(payload)


class ToolLibrary:
    """All tools agents can use, plus the jobs that generate new ones."""

    def __init__(
        self,
        path: str | Path | None,
        *,
        sandbox: Sandbox | None = None,
        extra_tools: Sequence[Tool] = (),
        policy: SandboxPolicy | None = None,
        provenance: ToolProvenance | None = None,
        protected: tuple[str, ...] | None = None,
    ) -> None:
        """``extra_tools``: trusted tools of installed integrations (:mod:`nodestro_runtime.extensions`).

        ``protected`` are paths the built-in sandbox never makes readable; by default the directory that holds
        ``path`` (Nodestro's data directory).

        ``provenance`` records which tools a person wrote in the editor; without it, records live in memory only
        and every stored tool counts as ``untrusted``."""
        self.policy = policy or SandboxPolicy()
        if protected is None:
            protected = (str(Path(path).resolve().parent),) if path is not None else ()
        self.protected = protected
        self.provenance: ToolProvenance = provenance or MemoryProvenance()
        self._lock = threading.RLock()
        self.builtin: dict[str, Tool] = {}
        for tool in (*BUILTIN_TOOLS, *extra_tools):
            assert tool.name is not None
            self.builtin[tool.name] = tool
        self._seen: _Fingerprint = ()
        self._registry = self._load(
            Path(path) if path is not None else None, sandbox or make_sandbox("subprocess", self.policy)
        )
        self.jobs: dict[str, ToolJob] = {}
        #: Python dependencies of generated tools; ``None`` allows the standard library only.
        self.dependencies: DependencyGate | None = None
        self._decisions: dict[str, _Decision] = {}
        #: Code that is untrusted but not stored as a tool, e.g. pending tool drafts (set by the workspace).
        self.untrusted_sources: Callable[[], Iterable[str]] = lambda: ()
        #: Tools of connected MCP servers (:mod:`nodestro_runtime.mcp`); ``None`` when MCP is not set up.
        self.mcp: McpService | None = None
        #: The ``auto`` sandbox mode on the desktop: a container sandbox for code that needs isolation when the
        #: built-in Linux sandbox is not available (``None`` while Docker is not running or in other modes).
        self.container_fallback: Callable[[], Sandbox | None] | None = None

    def _load(self, path: Path | None, sandbox: Sandbox) -> ToolRegistry:
        seen = _fingerprint(path)
        registry = ToolRegistry(path, sandbox=sandbox)
        for tool in self.builtin.values():
            registry.register_trusted(tool)
        self._seen = seen
        return registry

    @property
    def registry(self) -> ToolRegistry:
        """The tool registry, read again from disk whenever another process changed its files (F1).

        Several processes (the API and every worker of a server) share one tool directory; without this a process
        would keep using the tools it read at its start until it restarts."""
        registry = self._registry
        if registry.path is not None and _fingerprint(registry.path) != self._seen:
            with self._lock:
                if self._registry is registry:
                    self._registry = self._load(registry.path, registry.sandbox)
                registry = self._registry
        return registry

    def set_sandbox(self, sandbox: Sandbox) -> None:
        self.registry.sandbox = sandbox

    def reload(self, sandbox: Sandbox | None = None) -> None:
        """Read the tools again from disk, e.g. after a backup was restored."""
        with self._lock:
            current = self._registry
            self._registry = self._load(current.path, sandbox or current.sandbox)

    # ---- catalog -----------------------------------------------------------------

    def _info(self, name: str) -> ToolInfo:
        record = self.registry.record(name)
        if record.kind == "trusted":
            return ToolInfo(
                name=record.name,
                kind="builtin",
                trust="trusted",
                description=record.description,
                input_schema=record.input_schema,
                created_at=record.created_at,
                capability=capability_of(name=record.name, kind="builtin"),
            )
        kind: ToolKind = "generated" if record.requirement else "custom"
        trust = self.trust(name)
        return ToolInfo(
            name=record.name,
            kind=kind,
            trust=trust,
            description=record.description,
            input_schema=record.input_schema,
            version=record.version,
            created_at=record.created_at,
            function_name=record.function_name,
            code=record.code,
            tests=record.tests,
            requirement=record.requirement,
            capability=capability_of(name=record.name, kind=kind, isolation=self.isolation(trust, name)),
        )

    def isolation(self, trust: TrustClass, name: str = "") -> Isolation | None:
        """How the library's sandbox would isolate code of class ``trust``; ``None`` when it may not run here.

        A workflow can choose another environment, so this is the library's default; the rules before a call
        (S-2) look at the sandbox of that call."""
        try:
            return isolation_of(self._require(trust, self.registry.sandbox, name))
        except NodestroError:
            return None

    # ---- provenance ---------------------------------------------------------------------

    def trust(self, name: str) -> TrustClass:
        """The trust class of a stored tool (ADR-0006); ``untrusted`` for unknown names.

        ``user-authored`` only while every version under the name was written in the tool editor."""
        try:
            versions = self.registry.versions(name)
        except RegistryError:
            return "untrusted"
        if not versions:
            return "untrusted"
        if any(record.kind == "trusted" for record in versions):
            return "trusted"
        authored = self.provenance.authored(name)
        if all(record.version in authored and not record.requirement for record in versions):
            return "user-authored"
        return "untrusted"

    def editor_trust(self, function_name: str, code: str) -> TrustClass:
        """The class of code a person submits in the tool editor.

        New code is ``user-authored``. A new version of a tool that is not ``user-authored`` stays ``untrusted``,
        and so does code identical to a stored untrusted tool or a pending tool draft (copying AI-generated code
        into a new tool does not change where it came from)."""
        try:
            existing = self.registry.versions(function_name)
        except RegistryError:
            existing = ()
        if existing and self.trust(function_name) != "user-authored":
            return "untrusted"
        digest = code_digest(code)
        if digest in self._untrusted_digests():
            return "untrusted"
        return "user-authored"

    def _untrusted_digests(self) -> set[str]:
        digests = {code_digest(source) for source in self.untrusted_sources()}
        for name in self.registry.names():
            if self.trust(name) == "untrusted":
                for record in self.registry.versions(name):
                    if record.code:
                        digests.add(code_digest(record.code))
        with self._lock:
            for job in self.jobs.values():
                digests.update(code_digest(attempt.code) for attempt in job.attempts)
        return digests

    def linked_provenance(self) -> ToolProvenance:
        """Provenance for a library of frozen copies of these tools (a published release).

        A copy is ``user-authored`` only while the tool here is: the studio's records decide, never a claim
        stored with the copy."""
        return _LinkedProvenance(self)

    def _require(self, trust: TrustClass, sandbox: Sandbox, name: str) -> Sandbox:
        """The sandbox code of class ``trust`` runs in, instead of ``sandbox`` (ADR-0006).

        ``sandbox`` itself if the policy allows it; on the desktop, code that needs isolation but was given a host
        subprocess (the default sandbox or a Python environment's interpreter) runs in the built-in Linux sandbox
        with the same interpreter, if this computer passed its checks. Otherwise ``sandbox.isolation_required`` is
        raised: there is no fallback to the host subprocess."""
        if self.policy.allows(trust, sandbox):
            return sandbox
        isolated = self._isolated(sandbox)
        if isolated is not None and self.policy.allows(trust, isolated):
            return isolated
        fallback = self._fallback(sandbox)
        if fallback is not None and self.policy.allows(trust, fallback):
            return fallback
        self.policy.require(trust, sandbox, name=name)
        return sandbox  # pragma: no cover - require() raised

    def _fallback(self, sandbox: Sandbox) -> Sandbox | None:
        """The ``auto`` mode's container for code without third-party libraries (a Python environment's
        interpreter has packages the container image lacks, so its code is not moved)."""
        if self.container_fallback is None or not isinstance(sandbox, SubprocessSandbox):
            return None
        if os.path.realpath(sandbox.python) != os.path.realpath(sys.executable):
            return None
        return self.container_fallback()

    def _isolated(self, sandbox: Sandbox) -> Sandbox | None:
        if not isinstance(sandbox, SubprocessSandbox) or not self.policy.builtin_available():
            return None
        return LinuxSandbox(
            timeout=max(sandbox.timeout, 15.0),
            memory_mb=sandbox.memory_mb,
            max_output_chars=sandbox.max_output_chars,
            python=sandbox.python,
            env=dict(sandbox.env),
            protected=self.protected,
        )

    def _mcp_info(self, tool: McpTool, server_name: str) -> ToolInfo:
        definition = tool.approved_definition or tool.definition
        return ToolInfo(
            name=tool.tool_name,
            kind="mcp",
            trust="untrusted",
            description=definition.description,
            input_schema=definition.input_schema,
            created_at=tool.approved_at.isoformat() if tool.approved_at else None,
            server_id=tool.server_id,
            server_name=server_name,
            mcp_name=tool.name,
            definition_hash=tool.approved_hash,
            capability=tool.capability(),
        )

    def _mcp_tools(self) -> list[ToolInfo]:
        if self.mcp is None:
            return []
        names = {server.id: server.name for server in self.mcp.servers()}
        return [self._mcp_info(tool, names.get(tool.server_id, "")) for tool in self.mcp.usable()]

    def all(self) -> list[ToolInfo]:
        """Every tool agents can be given: stored and built-in tools, and the approved, usable MCP tools."""
        return [*(self._info(name) for name in self.registry.names()), *self._mcp_tools()]

    def get(self, name: str) -> ToolInfo:
        if self.mcp is not None and is_mcp_tool(name):
            found = self.mcp.find(name)
            if found is None:
                raise KeyError(name)
            return self._mcp_info(found[1], found[0].name)
        try:
            return self._info(name)
        except RegistryError:
            raise KeyError(name) from None

    def __contains__(self, name: object) -> bool:
        if isinstance(name, str) and is_mcp_tool(name):
            return self.mcp is not None and self.mcp.find(name) is not None
        return name in self.registry

    def tool(
        self,
        name: str,
        *,
        sandbox: Sandbox | None = None,
        cancelled: Callable[[], bool] | None = None,
        gated: bool = False,
    ) -> Tool:
        """A ready-to-use tool (generated ones run in the sandbox).

        ``sandbox`` replaces the registry's sandbox for custom and generated tools, e.g. one
        whose interpreter is a workflow's Python environment with approved libraries.

        MCP tools are checked against the MCP policy (``mcp.*`` refusals); ``cancelled`` stops a running MCP call
        when the run is cancelled. ``gated`` is true only for callers that ask the per-call approval gate before every
        call (the run engine); MCP tools that change data are refused otherwise.
        """
        if is_mcp_tool(name):
            if self.mcp is None:
                raise KeyError(name)
            return self.mcp.agent_tool(name, cancelled=cancelled, gated=gated)
        if name == MEMORY_TOOL_NAME:
            # Reserved for the agent's system memory tool (re-review R-2): an own or generated tool stored under this
            # name before the reservation is never handed out (it stays listed, so it can be renamed or deleted).
            raise NodestroError(
                "tool.name_reserved", f"The name {name!r} is reserved for the memory tool; rename this tool", name=name
            )
        registry = self.registry
        try:
            tool = registry.get_tool(name)
            record = registry.record(name)
        except RegistryError:
            raise KeyError(name) from None
        if not isinstance(tool, SandboxedTool):
            return tool
        routed = self._require(self.trust(name), sandbox or registry.sandbox, name)
        sandbox = routed if routed is not registry.sandbox else sandbox
        assert tool.input_schema is not None and tool.name is not None and tool.description is not None
        made: dict[str, Any] = dict(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
            output_schema=tool.output_schema,
            code=tool.code,
            function_name=tool.function_name,
            sandbox=sandbox or tool.sandbox,
        )
        if registry.path is None:
            return SandboxedTool(**made) if sandbox is not None else tool
        return PinnedTool(version=record.version, path=registry.path, **made)

    def catalog(self) -> list[dict[str, str]]:
        return self.registry.catalog()

    # ---- hand-written tools ------------------------------------------------------------

    def check(
        self,
        code: str,
        tests: str,
        function_name: str,
        *,
        sandbox: Sandbox | None = None,
        extra_imports: Iterable[str] = (),
        trust: TrustClass = "untrusted",
    ) -> CheckResult:
        """Static checks plus a test run in the sandbox, without saving anything.

        ``extra_imports`` are approved third-party modules, ``sandbox`` the environment they
        are installed in. ``trust`` is the class of the code (ADR-0006); the tests run only in a
        sandbox the policy allows for it, otherwise the result reports ``sandbox.isolation_required``.
        """
        source = check_tool_source(code, function_name, extra_imports=extra_imports)
        test_report = check_tool_tests(tests, extra_imports=extra_imports)
        errors = [problem("tool.code_check", text) for text in source.errors] + [
            problem("tool.test_check", text) for text in test_report.errors
        ]
        if function_name in RESERVED_NAMES or function_name in self.builtin:
            errors.append(
                problem(
                    "tool.name_reserved",
                    f"The name {function_name!r} is reserved; please choose another one",
                    name=function_name,
                )
            )
        if errors:
            return CheckResult(ok=False, errors=errors)
        try:
            chosen = self._require(trust, sandbox or self.registry.sandbox, function_name)
        except NodestroError as exc:
            return CheckResult(ok=False, errors=[exc.problem])
        try:
            result = chosen.validate(code, tests, function_name)
        except DynamicAgentError as exc:
            return CheckResult(ok=False, errors=[problem("tool.sandbox", f"Sandbox: {exc}")])
        if result.error:
            failures = [problem("tool.validation", result.error)]
        elif not result.tests:
            failures = [problem("tool.no_tests", "No tests were run")]
        else:
            failures = []
        return CheckResult(
            ok=result.ok and result.input_schema is not None,
            errors=failures,
            tests=[TestResult(name=t.name, ok=t.ok, error=t.error) for t in result.tests],
            output=result.output,
            name=result.name,
            description=result.description,
            input_schema=result.input_schema,
        )

    def save_custom(
        self,
        code: str,
        tests: str,
        function_name: str,
        *,
        requirement: str | None = None,
        sandbox: Sandbox | None = None,
        extra_imports: Iterable[str] = (),
        trust: TrustClass = "untrusted",
    ) -> tuple[CheckResult, ToolInfo | None]:
        """Check a tool and store it as a new version if everything passes.

        ``requirement`` marks an AI-written tool (a draft of the Workflow Architect the user approved); such a
        tool is always ``untrusted``. Only ``trust="user-authored"`` records the version as written in the editor.
        """
        if requirement:
            trust = "untrusted"
        result = self.check(code, tests, function_name, sandbox=sandbox, extra_imports=extra_imports, trust=trust)
        if not result.ok:
            return result, None
        assert result.name is not None and result.description is not None and result.input_schema is not None
        existing = self.registry.versions(result.name)
        if any(record.kind == "trusted" for record in existing):
            result.ok = False
            result.errors.append(
                problem("tool.name_builtin", f"The name {result.name!r} belongs to a built-in tool", name=result.name)
            )
            return result, None
        with self._lock:
            registry = self.registry
            record = registry.add_generated(
                name=result.name,
                function_name=function_name,
                description=result.description,
                code=code,
                tests=tests,
                input_schema=result.input_schema,
                output_schema=None,
                requirement=requirement,
            )
            if trust == "user-authored":
                self.provenance.record_authored(record.name, record.version)
            for older in existing:
                if older.status == "approved":
                    registry.set_status(older.name, older.version, "revoked")
            registry.approve(record.name, record.version)
        return result, self._info(record.name)

    def delete(self, name: str) -> None:
        if name in self.builtin:
            raise NodestroError("tool.builtin_delete", "Built-in tools cannot be deleted")
        try:
            self.registry.revoke(name)
        except RegistryError:
            raise KeyError(name) from None

    def run(self, name: str, payload: dict[str, Any], *, sandbox: Sandbox | None = None) -> tuple[str, bool]:
        """Run a tool once with JSON input, e.g. from the tool editor's test panel."""
        tool = self.tool(name, sandbox=sandbox)
        try:
            output = tool.execute(payload)
        except (ToolResultError, ToolInputError, ToolExecutionError) as exc:
            return str(exc), True
        return output if isinstance(output, str) else json.dumps(output, ensure_ascii=False), False

    # ---- generated tools ---------------------------------------------------------------

    def start_job(
        self,
        requirement: str,
        *,
        connection: Connection,
        model: str,
        usage_tracker: UsageTracker | None = None,
        on_finished: Callable[[ToolJob, UsageTracker], None] | None = None,
    ) -> ToolJob:
        """Start generating a tool in a background thread and return its job.

        ``on_finished(job, tracker)`` runs when the job ends, e.g. to record its usage. ``connection``
        belongs to the provider of ``model``.
        """
        if not requirement.strip():
            raise NodestroError("tool.requirement_missing", "Please describe what the tool should do")
        # The Coding Agent's tests run automatically: without isolation nothing is generated at all.
        self._require("untrusted", self.registry.sandbox, "")
        job = ToolJob(requirement=requirement.strip())
        decision = _Decision()
        with self._lock:
            self.jobs[job.id] = job
            self._decisions[job.id] = decision
        thread = threading.Thread(
            target=self._run_job,
            args=(job, decision, connection, model, usage_tracker or UsageTracker(), on_finished),
            name=f"tool-job-{job.id}",
            daemon=True,
        )
        thread.start()
        return job

    def _run_job(
        self,
        job: ToolJob,
        decision: _Decision,
        connection: Connection,
        model: str,
        tracker: UsageTracker,
        on_finished: Callable[[ToolJob, UsageTracker], None] | None = None,
    ) -> None:
        def approve(request: ApprovalRequest) -> bool:
            job.approval = {
                "name": request.name,
                "summary": request.summary,
                **request.details,
                "dependencies": job.dependencies,
            }
            job.status = "awaiting_approval"
            _step(job, problem("job.awaiting_approval", "All tests passed. Please review code and tests and approve."))
            if not decision.event.wait(_APPROVAL_TIMEOUT_SECONDS):
                _step(job, problem("job.timeout", "No decision within an hour; rejected."))
                return False
            return decision.approved

        builder: ToolBuilder | None = None
        coding_agent: _RecordingCodingAgent | None = None

        def prepare(proposal: ToolProposal) -> ToolProposal:
            """Prepare the libraries, then make sure the tests may run where they will run."""
            assert builder is not None
            proposal = prepare_dependencies(proposal)
            # The tests of generated code run where the policy allows (the built-in sandbox on the desktop).
            builder.test_sandbox = self._require("untrusted", builder.sandbox, proposal.function_name)
            return proposal

        def prepare_dependencies(proposal: ToolProposal) -> ToolProposal:
            """Third-party packages: resolve, ask the user, install, before the tests run."""
            gate = self.dependencies
            assert builder is not None and coding_agent is not None
            builder.extra_imports, builder.test_sandbox, job.dependencies = frozenset(), None, []
            if gate is None:
                return proposal
            for _round in range(4):
                outcome = gate.prepare(proposal.function_name, proposal.code, proposal.tests)
                if outcome.feedback:
                    _step(job, problem("job.dependency_feedback", "Some imports cannot be resolved; correcting"))
                    proposal = coding_agent.ask_again(outcome.feedback)
                    continue
                if outcome.ready:
                    builder.extra_imports = frozenset(outcome.extra_imports)
                    builder.test_sandbox = outcome.sandbox
                    job.dependencies = outcome.packages
                    return proposal
                # Cleared before the job asks, so the answer to this round is never taken for an old one.
                decision.dependencies.clear()
                job.dependency_plan = outcome.plan
                job.status = "awaiting_dependencies"
                _step(
                    job,
                    problem("job.awaiting_dependencies", "The tool needs third-party libraries. Please review them."),
                )
                if not decision.dependencies.wait(_APPROVAL_TIMEOUT_SECONDS):
                    _step(job, problem("job.timeout", "No decision within an hour; rejected."))
                    raise ApprovalDeniedError("No decision about the libraries")
                choice = decision.dependency_choice
                plan = job.dependency_plan or {}
                job.status, job.dependency_plan = "running", None
                if choice == "cancel":
                    _step(job, problem("job.rejected", "The tool was rejected and not saved."))
                    raise ApprovalDeniedError("The libraries were not approved")
                if choice == "without":
                    names = ", ".join(item["name"] for item in plan.get("requirements", []))
                    _step(job, problem("job.without_libraries", "Rewriting with the standard library only"))
                    proposal = coding_agent.ask_again(
                        f"The user did not approve the third-party packages ({names}). Return a complete "
                        "proposal that uses only the standard library modules allowed above, without a "
                        "'# /// script' block."
                    )
                    continue
                _step(job, problem("job.installing", "Installing the approved libraries"))
                gate.install(str(plan.get("id")), lambda _line: None)
            raise ApprovalDeniedError("The libraries could not be prepared")

        try:
            coding_agent = _RecordingCodingAgent(
                job,
                on_proposal=prepare,
                model=qualify(model),
                system_prompt=CODING_PROMPT,
                effort=connection.effort(model, "high"),
                credentials={connection.provider: connection.settings()},
                usage_tracker=tracker,
            )
            builder = _ReservingToolBuilder(
                coding_agent,
                self.registry,
                approver=CallbackApprover(approve),
                policy=DynamicPolicy(max_generated_tools=1),
                max_attempts=3,
            )
            tool = builder.build(job.requirement)
            job.tool_name = tool.name
            job.status = "approved"
            _step(
                job,
                problem(
                    "job.approved",
                    f"Tool {tool.name!r} was approved and is available to all agents.",
                    name=tool.name,
                ),
            )
        except ApprovalDeniedError:
            job.status = "rejected"
            if job.message_code != "job.timeout":
                _step(job, problem("job.rejected", "The tool was rejected and not saved."))
        except Exception as exc:  # the job reports every failure to the UI
            logger.exception("Tool job %s failed", job.id)
            job.status = "failed"
            # Shown to the user (GET /api/jobs): no credentials and, on a server, no AWS identity of the operator.
            job.error = redact_identity(redact(f"{type(exc).__name__}: {exc}"))
            if isinstance(exc, NodestroError) and exc.code.startswith("sandbox."):
                # E.g. ``sandbox.isolation_required``: reported with its code; the code was not run.
                _step(job, exc.problem)
            else:
                _step(job, problem("job.failed", "The tool could not be created."))
        finally:
            cost = tracker.usage.estimated_cost_usd
            job.cost_usd = float(cost) if cost is not None else None
            job.finished_at = now()
            if on_finished is not None:
                try:
                    on_finished(job, tracker)
                except Exception:  # recording usage must never break the job
                    logger.exception("Recording the usage of tool job %s failed", job.id)

    def decide(self, job_id: str, approved: bool) -> ToolJob:
        with self._lock:
            job = self.jobs.get(job_id)
            decision = self._decisions.get(job_id)
        if job is None or decision is None:
            raise KeyError(job_id)
        with self._lock:
            # Decided once: a second decision (e.g. a double click) never overrides the first.
            if job.status != "awaiting_approval" or decision.event.is_set():
                raise NodestroError("job.not_awaiting", "This job is not waiting for a decision")
            decision.approved = approved
            decision.event.set()
        _step(job, problem("job.deciding", "Applying the decision"))
        return job

    def decide_dependencies(self, job_id: str, choice: DependencyChoice) -> ToolJob:
        """The user's answer to the libraries a generated tool needs."""
        with self._lock:
            job = self.jobs.get(job_id)
            decision = self._decisions.get(job_id)
        if job is None or decision is None:
            raise KeyError(job_id)
        with self._lock:
            if job.status != "awaiting_dependencies" or decision.dependencies.is_set():
                raise NodestroError("job.not_awaiting", "This job is not waiting for a decision")
            decision.dependency_choice = choice
            decision.dependencies.set()
        _step(job, problem("job.deciding", "Applying the decision"))
        return job

    def list_jobs(self) -> list[ToolJob]:
        with self._lock:
            return sorted(self.jobs.values(), key=lambda job: job.created_at, reverse=True)
