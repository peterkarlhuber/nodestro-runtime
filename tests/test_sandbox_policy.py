"""The sandbox policy (ADR-0006): trust classes, provenance and where tool code may run."""

from __future__ import annotations

import ast
from collections.abc import Iterator
import itertools
from pathlib import Path
from typing import Any

from orkexa.dynamic import DockerSandbox, Sandbox, SubprocessSandbox, ValidationResult
import pytest

from nodestro_runtime.builtin_sandbox import LinuxSandbox, SandboxStatus
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.profile import PROFILE_KINDS
from nodestro_runtime.release import ReleaseBundle
from nodestro_runtime.sandbox_policy import TRUST_CLASSES, SandboxPolicy, isolation_of
from nodestro_runtime.schemas import AgentConfig, Workflow
from nodestro_runtime.testing import ContainerDouble
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE, ToolLibrary, make_sandbox
from nodestro_runtime.tool_provenance import MemoryProvenance, code_digest
from nodestro_runtime.workspace import Workspace

SRC = Path(__file__).resolve().parents[1] / "src" / "nodestro_runtime"
NAME = "celsius_to_fahrenheit"


class SpySandbox(SubprocessSandbox):
    """A host subprocess sandbox that records every attempt to run code in it."""

    def __init__(self) -> None:
        super().__init__(timeout=15.0, acknowledge_unsafe=True)
        self.calls: list[tuple[str, str]] = []

    def validate(self, code: str, tests: str, function_name: str) -> ValidationResult:
        self.calls.append(("validate", function_name))
        return super().validate(code, tests, function_name)

    def call(self, code: str, function_name: str, payload: dict[str, Any]) -> str:
        self.calls.append(("call", function_name))
        return super().call(code, function_name, payload)


@pytest.fixture
def library(tmp_path: Path) -> Iterator[ToolLibrary]:
    yield ToolLibrary(tmp_path / "tools", sandbox=SpySandbox())


def spy(tools: ToolLibrary) -> SpySandbox:
    sandbox = tools.registry.sandbox
    assert isinstance(sandbox, SpySandbox)
    return sandbox


def save_in_editor(tools: ToolLibrary, code: str = CUSTOM_TOOL_TEMPLATE, name: str = NAME) -> None:
    result, info = tools.save_custom(code, CUSTOM_TESTS_TEMPLATE, name, trust=tools.editor_trust(name, code))
    assert result.ok and info is not None, result


def save_generated(tools: ToolLibrary, sandbox: Sandbox) -> None:
    """A tool as an approved AI job or tool draft stores it (tested in an isolating sandbox)."""
    result, info = tools.save_custom(
        CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, NAME, requirement="Convert temperatures", sandbox=sandbox
    )
    assert result.ok and info is not None, result


# ---- the policy matrix ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "trust", "isolation"),
    list(itertools.product(PROFILE_KINDS, TRUST_CLASSES, ["none", "container", "kernel"])),
)
def test_policy_matrix(profile: Any, trust: Any, isolation: str, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = SandboxPolicy(profile)
    sandbox: Sandbox
    if isolation == "kernel":  # the built-in Linux sandbox on a computer that passed its checks
        up = SandboxStatus(True, "", "", "ubuntu 24.04", 4)
        monkeypatch.setattr(LinuxSandbox, "status", staticmethod(lambda refresh=False: up))
        sandbox = LinuxSandbox()
    else:
        sandbox = ContainerDouble() if isolation == "container" else SubprocessSandbox(acknowledge_unsafe=True)
    expected = (
        trust == "trusted"
        or isolation == "container"
        or (profile == "desktop" and (trust == "user-authored" or isolation == "kernel"))
    )
    assert policy.allows(trust, sandbox) is expected
    if expected:
        policy.require(trust, sandbox)
    else:
        with pytest.raises(NodestroError) as error:
            policy.require(trust, sandbox, name="tool_x")
        assert error.value.code == "sandbox.isolation_required"
        reason = "untested-system" if profile == "desktop" else ""
        assert error.value.params == {"name": "tool_x", "trust": trust, "reason": reason}


def test_an_unknown_trust_class_is_treated_as_untrusted() -> None:
    assert SandboxPolicy().needs_isolation("unknown")  # type: ignore[arg-type]


def test_only_a_docker_or_a_checked_builtin_sandbox_counts_as_isolating() -> None:
    class Claims(SubprocessSandbox):
        isolated = True  # a claim, not a container

    assert isolation_of(DockerSandbox()) == "container"
    assert isolation_of(Claims(acknowledge_unsafe=True)) == "none"
    assert isolation_of(SubprocessSandbox(acknowledge_unsafe=True)) == "none"
    assert isolation_of(LinuxSandbox()) == "none"  # its checks did not pass on this (test) computer


def test_the_server_profile_cannot_create_a_host_subprocess_sandbox() -> None:
    with pytest.raises(NodestroError) as error:
        make_sandbox("subprocess", SandboxPolicy("server"))
    assert error.value.code == "sandbox.unavailable"
    assert isinstance(make_sandbox("docker", SandboxPolicy("server")), DockerSandbox)
    assert isinstance(make_sandbox("subprocess", SandboxPolicy("desktop")), SubprocessSandbox)


def test_no_setting_or_environment_variable_selects_a_host_sandbox_for_untrusted_code(
    library: ToolLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    for variable in ("NODESTRO_SANDBOX", "NODESTRO_ALLOW_UNSAFE", "NODESTRO_UNSAFE_TOOLS"):
        monkeypatch.setenv(variable, "1")
    save_generated(library, ContainerDouble())
    for kind in ("subprocess", "unknown", ""):
        library.set_sandbox(make_sandbox(kind))
        with pytest.raises(NodestroError, match="isolating sandbox"):
            library.tool(NAME)


# ---- provenance -------------------------------------------------------------------------


def test_a_tool_written_in_the_editor_is_user_authored_and_runs_on_the_desktop(library: ToolLibrary) -> None:
    save_in_editor(library)
    assert library.get(NAME).trust == "user-authored"
    assert library.run(NAME, {"celsius": 100}) == ("212.0", False)
    assert ("call", NAME) in spy(library).calls


def test_generated_code_is_untrusted_also_after_approval_and_never_runs_on_the_host(library: ToolLibrary) -> None:
    container = ContainerDouble()
    save_generated(library, container)
    info = library.get(NAME)
    assert info.kind == "generated" and info.trust == "untrusted"
    with pytest.raises(NodestroError) as error:
        library.run(NAME, {"celsius": 100})
    assert error.value.code == "sandbox.isolation_required"
    assert spy(library).calls == []
    # In an isolating sandbox it runs.
    assert library.run(NAME, {"celsius": 100}, sandbox=container) == ("212.0", False)


def test_checking_untrusted_code_without_isolation_runs_nothing(library: ToolLibrary) -> None:
    result = library.check(CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, NAME)  # default: untrusted
    assert not result.ok and [item.code for item in result.errors] == ["sandbox.isolation_required"]
    assert spy(library).calls == []
    saved, info = library.save_custom(CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, NAME, requirement="x")
    assert info is None and saved.errors[0].code == "sandbox.isolation_required"
    assert spy(library).calls == []


def test_a_requirement_always_makes_a_tool_untrusted(library: ToolLibrary) -> None:
    _result, info = library.save_custom(
        CUSTOM_TOOL_TEMPLATE,
        CUSTOM_TESTS_TEMPLATE,
        NAME,
        requirement="x",
        trust="user-authored",
        sandbox=ContainerDouble(),
    )
    assert info is not None and info.trust == "untrusted"


def test_editing_an_untrusted_tool_keeps_it_untrusted(library: ToolLibrary) -> None:
    save_generated(library, ContainerDouble())
    edited = CUSTOM_TOOL_TEMPLATE.replace("+ 32", "+ 32.0")
    assert library.editor_trust(NAME, edited) == "untrusted"
    result, info = library.save_custom(edited, CUSTOM_TESTS_TEMPLATE, NAME, trust=library.editor_trust(NAME, edited))
    assert info is None and result.errors[0].code == "sandbox.isolation_required"
    # Deleting it and writing a new tool under the same name does not change its history either.
    library.delete(NAME)
    assert library.editor_trust(NAME, edited) == "untrusted"


def test_copying_untrusted_code_into_a_new_tool_keeps_it_untrusted(library: ToolLibrary) -> None:
    save_generated(library, ContainerDouble())
    copied = CUSTOM_TOOL_TEMPLATE.replace("\n", "\r\n") + "\n\n"
    assert library.editor_trust("other_name", copied) == "untrusted"
    library.untrusted_sources = lambda: ["def draft_tool():\n    return 1\n"]
    assert library.editor_trust("draft_tool", "def draft_tool():\n    return 1") == "untrusted"
    assert library.editor_trust("mine", "def mine() -> int:\n    return 2\n") == "user-authored"


def test_job_attempts_count_as_untrusted_code(library: ToolLibrary) -> None:
    from nodestro_runtime.tool_library import ToolAttempt, ToolJob

    job = ToolJob(requirement="x")
    job.attempts.append(ToolAttempt(number=1, function_name=NAME, description="", code=CUSTOM_TOOL_TEMPLATE, tests=""))
    library.jobs[job.id] = job
    assert library.editor_trust(NAME, CUSTOM_TOOL_TEMPLATE) == "untrusted"


def test_tools_without_recorded_provenance_are_untrusted(tmp_path: Path) -> None:
    first = ToolLibrary(tmp_path / "tools", sandbox=SpySandbox())
    save_in_editor(first)
    assert first.get(NAME).trust == "user-authored"
    # The same registry without the records (e.g. tools stored before provenance existed).
    second = ToolLibrary(tmp_path / "tools", sandbox=SpySandbox(), provenance=MemoryProvenance())
    assert second.get(NAME).trust == "untrusted"
    with pytest.raises(NodestroError):
        second.tool(NAME)


def test_built_in_tools_are_trusted(library: ToolLibrary) -> None:
    assert library.get("calculator").trust == "trusted"
    library.tool("calculator")


def test_code_digest_ignores_line_endings_and_trailing_space() -> None:
    assert code_digest("a = 1  \r\nb = 2\n\n") == code_digest("a = 1\nb = 2")
    assert code_digest("a = 1") != code_digest("a = 2")


# ---- tool generation ----------------------------------------------------------------------


def test_tool_generation_without_isolation_is_refused_before_anything_starts(library: ToolLibrary) -> None:
    with pytest.raises(NodestroError) as error:
        library.start_job("Convert temperatures", connection=None, model="claude-haiku-5-5")  # type: ignore[arg-type]
    assert error.value.code == "sandbox.isolation_required"
    assert library.jobs == {}


# ---- workspace: stored provenance, restore, preflight ---------------------------------------


def test_the_workspace_keeps_provenance_and_forgets_it_after_a_restore(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        save_in_editor(workspace.tools)
        assert workspace.tools.get(NAME).trust == "user-authored"
    finally:
        workspace.close()
    reopened = Workspace(tmp_path / "data", integrations=[])
    try:
        assert reopened.tools.get(NAME).trust == "user-authored"
        reopened.reload_after_restore()
        assert reopened.tools.get(NAME).trust == "untrusted"
    finally:
        reopened.close()


def test_the_agent_check_reports_an_untrusted_tool_without_isolation(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        save_generated(workspace.tools, ContainerDouble())
        issues = workspace.builder.validate(AgentConfig(name="a", tools=[NAME]))
        found = [issue for issue in issues if issue.code == "sandbox.isolation_required"]
        assert found and found[0].level == "error" and found[0].field == "tools"
        workspace.tools.set_sandbox(ContainerDouble())
        assert not [
            issue
            for issue in workspace.builder.validate(AgentConfig(name="a", tools=[NAME]))
            if issue.code.startswith("sandbox.")
        ]
    finally:
        workspace.close()


# ---- releases ----------------------------------------------------------------------------


def _bundle(**fields: Any) -> ReleaseBundle:
    return ReleaseBundle(
        workflow_id="wf",
        workflow=Workflow(id="wf", name="W", nodes=[], edges=[]),
        tools={NAME: {"name": NAME, "version": 2}},
        **fields,
    )


def test_a_bundle_without_trust_claims_keeps_its_digest() -> None:
    bundle = _bundle()
    data = bundle.model_dump(mode="json", exclude={"created_at", "tool_trust"})
    import hashlib
    import json

    expected = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    assert bundle.digest() == expected
    assert _bundle(tool_trust={NAME: "user-authored"}).digest() != expected


def test_bundle_provenance_records_only_user_authored_tools() -> None:
    assert _bundle(tool_trust={NAME: "user-authored"}).provenance().authored(NAME) == {2}
    assert _bundle(tool_trust={NAME: "untrusted"}).provenance().authored(NAME) == set()
    assert _bundle().provenance().authored(NAME) == set()


def test_a_release_library_follows_the_studio_provenance(tmp_path: Path) -> None:
    studio = ToolLibrary(tmp_path / "tools", sandbox=SpySandbox())
    save_in_editor(studio)
    release = ToolLibrary(tmp_path / "tools", sandbox=SpySandbox(), provenance=studio.linked_provenance())
    assert release.get(NAME).trust == "user-authored"
    studio.provenance.clear()  # e.g. after a restore
    assert release.get(NAME).trust == "untrusted"
    with pytest.raises(NodestroError):
        release.provenance.record_authored(NAME, 1)


# ---- no other path runs code on the host ------------------------------------------------


def test_host_subprocess_sandboxes_are_created_only_in_known_places() -> None:
    """Every ``SubprocessSandbox(...)`` in the runtime is listed here; each one is guarded by the policy
    (``make_sandbox`` checks the profile; the library checks every run; environments are used only for code
    the policy allows without isolation)."""
    allowed = {("tool_library.py", "make_sandbox"), ("python_envs.py", "sandbox"), ("testing.py", "__init__")}
    found = set()
    for path in SRC.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "SubprocessSandbox":
                    found.add((path.name, function.name))
    assert found == allowed
