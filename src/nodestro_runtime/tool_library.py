"""The tool library: built-in, hand-written and AI-generated tools.

Built-in tools are trusted ``@Tool`` functions. Tools a user writes in the
editor and tools the Coding Agent generates are stored in a
``orkexa.dynamic.ToolRegistry``: their code is checked statically, their
tests run in the sandbox, and every call runs in the sandbox as well.

Generating a tool takes a while and ends with a human decision, so it runs as a
:class:`ToolJob` in a background thread: ``ToolBuilder`` does the work, and its
``CallbackApprover`` waits until the user approves or rejects in the UI.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
import json
import logging
from pathlib import Path
import threading
from typing import Any, Literal

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
from pydantic import BaseModel, Field

from .builtin_tools import BUILTIN_TOOLS
from .problems import NodestroError, Problem, problem
from .providers import Connection, qualify
from .schemas import new_id, now

logger = logging.getLogger(__name__)

ToolKind = Literal["builtin", "custom", "generated"]
_APPROVAL_TIMEOUT_SECONDS = 3600

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
    description: str
    input_schema: dict[str, Any] | None = None
    version: int = 1
    created_at: str | None = None
    function_name: str | None = None
    code: str | None = None
    tests: str | None = None
    requirement: str | None = None


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


def make_sandbox(kind: str) -> Sandbox:
    if kind == "docker":
        return DockerSandbox()
    return SubprocessSandbox(timeout=15.0, acknowledge_unsafe=True)


class ToolLibrary:
    """All tools agents can use, plus the jobs that generate new ones."""

    def __init__(
        self, path: str | Path | None, *, sandbox: Sandbox | None = None, extra_tools: Sequence[Tool] = ()
    ) -> None:
        """``extra_tools``: trusted tools of installed integrations (:mod:`nodestro_runtime.extensions`)."""
        self.registry = ToolRegistry(path, sandbox=sandbox or SubprocessSandbox(timeout=15.0, acknowledge_unsafe=True))
        self.builtin: dict[str, Tool] = {}
        for tool in (*BUILTIN_TOOLS, *extra_tools):
            assert tool.name is not None
            self.registry.register_trusted(tool)
            self.builtin[tool.name] = tool
        self.jobs: dict[str, ToolJob] = {}
        #: Python dependencies of generated tools; ``None`` allows the standard library only.
        self.dependencies: DependencyGate | None = None
        self._decisions: dict[str, _Decision] = {}
        self._lock = threading.RLock()

    def set_sandbox(self, sandbox: Sandbox) -> None:
        self.registry.sandbox = sandbox

    def reload(self, sandbox: Sandbox | None = None) -> None:
        """Read the tools again from disk, e.g. after a backup was restored."""
        registry = ToolRegistry(self.registry.path, sandbox=sandbox or self.registry.sandbox)
        for tool in self.builtin.values():
            registry.register_trusted(tool)
        with self._lock:
            self.registry = registry

    # ---- catalog -----------------------------------------------------------------

    def _info(self, name: str) -> ToolInfo:
        record = self.registry.record(name)
        if record.kind == "trusted":
            return ToolInfo(
                name=record.name,
                kind="builtin",
                description=record.description,
                input_schema=record.input_schema,
                created_at=record.created_at,
            )
        return ToolInfo(
            name=record.name,
            kind="generated" if record.requirement else "custom",
            description=record.description,
            input_schema=record.input_schema,
            version=record.version,
            created_at=record.created_at,
            function_name=record.function_name,
            code=record.code,
            tests=record.tests,
            requirement=record.requirement,
        )

    def all(self) -> list[ToolInfo]:
        return [self._info(name) for name in self.registry.names()]

    def get(self, name: str) -> ToolInfo:
        try:
            return self._info(name)
        except RegistryError:
            raise KeyError(name) from None

    def __contains__(self, name: object) -> bool:
        return name in self.registry

    def tool(self, name: str, *, sandbox: Sandbox | None = None) -> Tool:
        """A ready-to-use tool (generated ones run in the sandbox).

        ``sandbox`` replaces the registry's sandbox for custom and generated tools, e.g. one
        whose interpreter is a workflow's Python environment with approved libraries.
        """
        try:
            tool = self.registry.get_tool(name)
        except RegistryError:
            raise KeyError(name) from None
        if sandbox is not None and isinstance(tool, SandboxedTool):
            assert tool.input_schema is not None and tool.name is not None and tool.description is not None
            return SandboxedTool(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
                code=tool.code,
                function_name=tool.function_name,
                sandbox=sandbox,
            )
        return tool

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
    ) -> CheckResult:
        """Static checks plus a test run in the sandbox, without saving anything.

        ``extra_imports`` are approved third-party modules, ``sandbox`` the environment they
        are installed in.
        """
        source = check_tool_source(code, function_name, extra_imports=extra_imports)
        test_report = check_tool_tests(tests, extra_imports=extra_imports)
        errors = [problem("tool.code_check", text) for text in source.errors] + [
            problem("tool.test_check", text) for text in test_report.errors
        ]
        if function_name in RESERVED_TOOL_NAMES or function_name in self.builtin:
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
            result = (sandbox or self.registry.sandbox).validate(code, tests, function_name)
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
    ) -> tuple[CheckResult, ToolInfo | None]:
        """Check a tool and store it as a new version if everything passes.

        ``requirement`` marks an AI-written tool (a draft of the Workflow Architect the user approved).
        """
        result = self.check(code, tests, function_name, sandbox=sandbox, extra_imports=extra_imports)
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
            record = self.registry.add_generated(
                name=result.name,
                function_name=function_name,
                description=result.description,
                code=code,
                tests=tests,
                input_schema=result.input_schema,
                output_schema=None,
                requirement=requirement,
            )
            for older in existing:
                if older.status == "approved":
                    self.registry.set_status(older.name, older.version, "revoked")
            self.registry.approve(record.name, record.version)
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
            builder = ToolBuilder(
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
            job.error = f"{type(exc).__name__}: {exc}"
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
