"""Python dependencies of single tools: the tool editor, tool jobs and tool drafts.

A tool's tests run in the tool's own test environment (scope ``tool:<name>``). A tool that
only uses the standard library runs in the normal sandbox, exactly as before.
"""

from __future__ import annotations

from collections.abc import Callable

from orkexa.dynamic import Sandbox, check_tool_source, check_tool_tests

from .problems import NodestroError, Problem, problem
from .python_envs import DependencyStatus, Needed, PythonEnvironments, scope_of
from .sandbox_policy import TrustClass
from .tool_library import CheckResult, DependencyGate, DependencyOutcome, ToolInfo, ToolLibrary


def state_problem(status: DependencyStatus) -> Problem:
    """Why a scope's libraries are not usable yet, as a code for the UI."""
    names = ", ".join(status.pending or [item.name for item in status.needed])
    if status.problem is not None:
        return status.problem
    if status.state == "unresolved":
        details = "; ".join(issue.message for issue in status.issues if issue.blocking)
        return problem("deps.unresolved", f"Imports cannot be resolved: {details}", details=details)
    if status.state == "installing":
        return problem("deps.installing", "The libraries are being installed", packages=names)
    if status.state == "failed":
        error = status.install.error.message if status.install.error else ""
        return problem("deps.install_failed", f"Installing the libraries failed: {error}", error=error[:300])
    if status.state == "install_needed":
        return problem("deps.install_pending", f"Approved libraries will be installed: {names}", packages=names)
    return problem("deps.approval_needed", f"Third-party libraries need your approval first: {names}", packages=names)


class ToolEnvironment:
    """The dependencies of one tool's code and the sandbox its tests need."""

    def __init__(self, python: PythonEnvironments, name: str, code: str, tests: str) -> None:
        self.python = python
        self.name = name
        self.needed: Needed = python.needed_for_sources([(name, code, tests)])
        self.status: DependencyStatus = python.status("tool", name, self.needed, label=name)

    @property
    def third_party(self) -> bool:
        return bool(self.needed.requirements or self.needed.blocking)

    def ensure(self) -> None:
        """Install an approved environment that is missing (no new versions, no new approvals)."""
        if self.status.state == "install_needed":
            self.python.install(self.status.scope, self.needed.all_modules())
            self.status = self.python.status("tool", self.name, self.needed, label=self.name)

    @property
    def ready(self) -> bool:
        return not self.third_party or self.status.state == "ready"

    @property
    def sandbox(self) -> Sandbox | None:
        return self.python.sandbox(scope_of("tool", self.name)) if self.needed.requirements else None

    @property
    def extra_imports(self) -> set[str]:
        return self.needed.all_modules()


def libraries_problem(name: str) -> Problem:
    """Why code that needs isolation cannot use third-party libraries yet (ADR-0006)."""
    return problem(
        "sandbox.libraries_need_isolation",
        f"The tool {name!r} is AI-generated or of unknown origin and needs third-party libraries; such tools "
        "cannot run yet, because libraries are only available without isolation",
        name=name,
    )


def needs_isolated_libraries(tools: ToolLibrary, python: PythonEnvironments, trust: TrustClass, needed: Needed) -> bool:
    """Code of class ``trust`` needs libraries that would run without the isolation the policy requires."""
    return bool(needed.requirements) and tools.policy.needs_isolation(trust) and python.isolation() == "none"


def check_with_dependencies(
    tools: ToolLibrary,
    python: PythonEnvironments,
    code: str,
    tests: str,
    function_name: str,
    *,
    trust: TrustClass | None = None,
) -> CheckResult:
    """The tool editor's check: approved libraries run in the tool's test environment.

    ``trust`` is the class of the code; ``None`` means a person submitted it in the tool editor
    (:meth:`ToolLibrary.editor_trust`)."""
    if trust is None:
        trust = tools.editor_trust(function_name, code)
    environment = ToolEnvironment(python, function_name, code, tests)
    if needs_isolated_libraries(tools, python, trust, environment.needed):
        # Nothing is approved, installed or run for code that could not use the libraries anyway.
        extra = environment.extra_imports
        errors = [
            problem("tool.code_check", text)
            for text in check_tool_source(code, function_name, extra_imports=extra).errors
        ]
        errors += [problem("tool.test_check", text) for text in check_tool_tests(tests, extra_imports=extra).errors]
        return CheckResult(ok=False, errors=[*errors, libraries_problem(function_name)])
    environment.ensure()
    if environment.ready:
        return tools.check(
            code,
            tests,
            function_name,
            sandbox=environment.sandbox,
            extra_imports=environment.extra_imports,
            trust=trust,
        )
    # Report problems in the code first; the tests can only run once the libraries are there.
    extra = environment.extra_imports
    errors = [
        problem("tool.code_check", text) for text in check_tool_source(code, function_name, extra_imports=extra).errors
    ]
    errors += [problem("tool.test_check", text) for text in check_tool_tests(tests, extra_imports=extra).errors]
    errors.append(state_problem(environment.status))
    return CheckResult(ok=False, errors=errors, dependencies_pending=True)


def save_with_dependencies(
    tools: ToolLibrary,
    python: PythonEnvironments,
    code: str,
    tests: str,
    function_name: str,
    *,
    requirement: str | None = None,
    trust: TrustClass | None = None,
) -> tuple[CheckResult, ToolInfo | None]:
    """Check and store a tool; ``trust`` as in :func:`check_with_dependencies` (a ``requirement`` makes it
    ``untrusted``)."""
    if trust is None:
        trust = "untrusted" if requirement else tools.editor_trust(function_name, code)
    environment = ToolEnvironment(python, function_name, code, tests)
    if needs_isolated_libraries(tools, python, trust, environment.needed):
        return check_with_dependencies(tools, python, code, tests, function_name, trust=trust), None
    environment.ensure()
    if not environment.ready:
        return check_with_dependencies(tools, python, code, tests, function_name, trust=trust), None
    return tools.save_custom(
        code,
        tests,
        function_name,
        requirement=requirement,
        sandbox=environment.sandbox,
        extra_imports=environment.extra_imports,
        trust=trust,
    )


def tool_run_sandbox(tools: ToolLibrary, python: PythonEnvironments, name: str) -> Sandbox | None:
    """The sandbox for a test call of a saved tool from the tool editor."""
    info = tools.get(name)
    if info.code is None:
        return None
    environment = ToolEnvironment(python, name, info.code, info.tests or "")
    if not environment.needed.requirements:
        return None
    if needs_isolated_libraries(tools, python, info.trust, environment.needed):
        found = libraries_problem(name)
        raise NodestroError(found.code, found.message, **found.params)
    environment.ensure()
    if not environment.ready:
        found = state_problem(environment.status)
        raise NodestroError(found.code, found.message, **found.params)
    return environment.sandbox


_FEEDBACK = (
    "Declare every third-party package in a '# /// script' block at the top of `code`, for example\n"
    '# /// script\n# dependencies = ["package-name"]\n# ///\n'
    "and never import modules by name at run time."
)


class EnvironmentGate(DependencyGate):
    """Tool jobs: resolve, show and install the libraries a proposal needs."""

    def __init__(self, python: PythonEnvironments) -> None:
        self.python = python

    def prepare(self, name: str, code: str, tests: str) -> DependencyOutcome:
        environment = ToolEnvironment(self.python, name, code, tests)
        if environment.needed.blocking:
            issues = "\n".join(issue.message for issue in environment.needed.blocking)
            return DependencyOutcome(feedback=f"{issues}\n{_FEEDBACK}")
        if not environment.needed.requirements:
            return DependencyOutcome(ready=True)
        if self.python.isolation() == "none":
            # Generated code is untrusted: its libraries would run without isolation (ADR-0006).
            return DependencyOutcome(
                feedback="Third-party libraries cannot be used for generated tools here (they would run without "
                "isolation). Use only the standard library."
            )
        if environment.status.state == "unsupported":
            reason = environment.status.problem.message if environment.status.problem else "not available"
            return DependencyOutcome(
                feedback=f"Third-party libraries cannot be used here ({reason}). Use only the standard library."
            )
        environment.ensure()
        if environment.ready:
            return DependencyOutcome(
                ready=True,
                sandbox=environment.sandbox,
                extra_imports=sorted(environment.extra_imports),
                packages=[item.name for item in environment.needed.requirements],
            )
        # Generated code chose these packages, no person did yet: never run a package's build script here.
        plan = self.python.plan("tool", name, environment.needed, label=name, builds=False)
        return DependencyOutcome(plan=plan.model_dump(mode="json", exclude={"lock"}))

    def install(self, plan_id: str, log: Callable[[str], None]) -> None:
        env = self.python.approve(plan_id, wait=True, log=log)
        if env.install.status != "installed":
            error = env.install.error
            raise NodestroError(
                error.code if error else "deps.install_failed",
                error.message if error else "Installing the libraries failed",
                **(error.params if error else {}),
            )
