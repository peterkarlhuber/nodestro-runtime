"""Adopting AI-generated tool code as one's own (ADR-0006, decision R-4): confirmed, audited, desktop only."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from orkexa.dynamic import SubprocessSandbox, ValidationResult
import pytest

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.profile import PROFILE_KINDS
from nodestro_runtime.sandbox_policy import SandboxPolicy
from nodestro_runtime.testing import ContainerDouble
from nodestro_runtime.tool_adoption import adopt_tool
from nodestro_runtime.tool_approvals import audit_events, audit_target
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE, ToolLibrary
from nodestro_runtime.tool_provenance import MemoryProvenance, ToolAdoption, content_hash
from nodestro_runtime.workspace import Workspace

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


def save_generated(tools: ToolLibrary, code: str = CUSTOM_TOOL_TEMPLATE) -> None:
    """A version an AI wrote (an approved job or draft), tested in an isolating sandbox."""
    result, info = tools.save_custom(
        code, CUSTOM_TESTS_TEMPLATE, NAME, requirement="Convert temperatures", sandbox=ContainerDouble()
    )
    assert result.ok and info is not None, result


def shown_hash(tools: ToolLibrary) -> str:
    """The hash the studio shows with the code and tests and sends back with the confirmation."""
    found = tools.get(NAME).content_hash
    assert found is not None
    return found


def adopt(tools: ToolLibrary, *, role: str = "owner", expected: str | None = None) -> ToolAdoption:
    return tools.adopt(NAME, actor="local", role=role, expected_hash=expected or shown_hash(tools))


# ---- the adoption -----------------------------------------------------------------------------------------------


def test_an_adopted_tool_is_user_authored_and_runs_on_the_desktop(library: ToolLibrary) -> None:
    save_generated(library)
    assert library.get(NAME).trust == "untrusted"
    adoption = adopt(library)
    assert adoption.version == 1 and adoption.actor == "local" and adoption.role == "owner"
    assert adoption.content_hash == content_hash(CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE)
    info = library.get(NAME)
    assert info.trust == "user-authored" and info.kind == "generated"
    assert info.adoption is not None and info.adoption.version == 1
    assert library.run(NAME, {"celsius": 100}) == ("212.0", False)
    assert ("call", NAME) in spy(library).calls


def test_a_tool_that_is_not_adopted_still_never_runs_on_the_host(library: ToolLibrary) -> None:
    save_generated(library)
    assert library.get(NAME).adoption is None
    with pytest.raises(NodestroError) as error:
        library.run(NAME, {"celsius": 100})
    assert error.value.code == "sandbox.isolation_required"
    assert spy(library).calls == []


def test_an_administrator_may_adopt_and_other_roles_may_not(library: ToolLibrary) -> None:
    save_generated(library)
    for role in ("editor", "runner", "viewer", "member", ""):
        with pytest.raises(NodestroError) as error:
            adopt(library, role=role)
        assert error.value.code == "tool.adoption_forbidden"
    assert library.get(NAME).trust == "untrusted"
    assert adopt(library, role="admin").role == "admin"


@pytest.mark.parametrize("profile", [kind for kind in PROFILE_KINDS if kind != "desktop"])
def test_adoption_is_refused_off_the_desktop(tmp_path: Path, profile: Any) -> None:
    tools = ToolLibrary(tmp_path / "tools", sandbox=ContainerDouble(), policy=SandboxPolicy(profile))
    save_generated(tools)
    with pytest.raises(NodestroError) as error:
        adopt(tools)
    assert error.value.code == "tool.adoption_desktop_only"
    assert error.value.params["profile"] == profile
    assert tools.get(NAME).trust == "untrusted" and tools.provenance.adoption(NAME) is None


def test_an_adoption_names_exactly_the_content_that_was_shown(library: ToolLibrary) -> None:
    save_generated(library)
    stale = shown_hash(library)
    save_generated(library, CUSTOM_TOOL_TEMPLATE.replace("+ 32", "+ 32.0"))  # an AI changed it meanwhile
    with pytest.raises(NodestroError) as error:
        adopt(library, expected=stale)
    assert error.value.code == "tool.adoption_stale" and error.value.params["version"] == 2
    assert library.get(NAME).trust == "untrusted"
    assert adopt(library).version == 2


def test_tools_that_need_no_adoption_are_refused(library: ToolLibrary) -> None:
    result, info = library.save_custom(CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, NAME, trust="user-authored")
    assert info is not None and info.trust == "user-authored", result
    with pytest.raises(NodestroError) as error:
        adopt(library)
    assert error.value.code == "tool.adoption_not_needed"
    with pytest.raises(NodestroError) as error:
        library.adopt("calculator", actor="local", role="owner", expected_hash="x")
    assert error.value.code == "tool.adoption_not_applicable"
    with pytest.raises(KeyError):
        library.adopt("missing", actor="local", role="owner", expected_hash="x")


# ---- what ends an adoption ---------------------------------------------------------------------------------------


def test_an_ai_change_ends_the_adoption(library: ToolLibrary) -> None:
    revoked: list[tuple[ToolAdoption, int]] = []
    library.on_adoption_revoked = lambda adoption, version: revoked.append((adoption, version))
    save_generated(library)
    adopt(library)
    save_generated(library, CUSTOM_TOOL_TEMPLATE.replace("+ 32", "+ 32.0"))
    assert library.get(NAME).trust == "untrusted" and library.get(NAME).adoption is None
    assert library.provenance.adoption(NAME) is None
    assert [(adoption.version, version) for adoption, version in revoked] == [(1, 2)]
    with pytest.raises(NodestroError):
        library.run(NAME, {"celsius": 100})


def test_an_edit_in_the_editor_keeps_the_adoption_until_an_ai_change(library: ToolLibrary) -> None:
    save_generated(library)
    adopt(library)
    edited = CUSTOM_TOOL_TEMPLATE.replace("+ 32", "+ 32.0")
    assert library.editor_trust(NAME, edited) == "user-authored"
    # The adopted code itself also stays the person's own, although a job attempt may have produced it.
    assert library.editor_trust(NAME, CUSTOM_TOOL_TEMPLATE) == "user-authored"
    result, info = library.save_custom(edited, CUSTOM_TESTS_TEMPLATE, NAME, trust=library.editor_trust(NAME, edited))
    assert info is not None and info.trust == "user-authored", result
    save_generated(library, CUSTOM_TOOL_TEMPLATE.replace("+ 32", "+ 32.00"))
    assert library.get(NAME).trust == "untrusted"
    assert library.editor_trust(NAME, edited) == "untrusted"


def test_a_copy_of_adopted_code_under_another_name_is_not_adopted(library: ToolLibrary) -> None:
    save_generated(library)
    library.untrusted_sources = lambda: [CUSTOM_TOOL_TEMPLATE]  # e.g. the draft it came from
    adopt(library)
    assert library.editor_trust("other_name", CUSTOM_TOOL_TEMPLATE) == "untrusted"


def test_an_adoption_whose_content_no_longer_matches_does_not_count(tmp_path: Path) -> None:
    provenance = MemoryProvenance()
    tools = ToolLibrary(tmp_path / "tools", sandbox=SpySandbox(), provenance=provenance)
    save_generated(tools)
    provenance.record_adoption(ToolAdoption(name=NAME, version=1, content_hash="0" * 64, actor="x", role="owner"))
    assert tools.get(NAME).trust == "untrusted"
    provenance.record_adoption(ToolAdoption(name=NAME, version=7, content_hash=shown_hash(tools), actor="x", role="o"))
    assert tools.get(NAME).trust == "untrusted"


def test_a_release_library_follows_the_studio_adoption(tmp_path: Path) -> None:
    studio = ToolLibrary(tmp_path / "tools", sandbox=SpySandbox())
    save_generated(studio)
    adopt(studio)
    release = ToolLibrary(tmp_path / "tools", sandbox=SpySandbox(), provenance=studio.linked_provenance())
    assert release.get(NAME).trust == "user-authored"
    with pytest.raises(NodestroError):
        release.provenance.record_adoption(studio.provenance.adoption(NAME))  # type: ignore[arg-type]
    studio.provenance.clear()  # e.g. after a restore
    assert release.get(NAME).trust == "untrusted"


# ---- the workspace: stored records, audit trail, restore -----------------------------------------------------


def test_the_workspace_audits_adoptions_refusals_and_revocations(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        save_generated(workspace.tools)
        expected = shown_hash(workspace.tools)
        with pytest.raises(NodestroError):
            adopt_tool(workspace.tools, workspace.store, NAME, actor="u1", role="editor", expected_hash=expected)
        adopt_tool(workspace.tools, workspace.store, NAME, actor="local", role="owner", expected_hash=expected)
        save_generated(workspace.tools, CUSTOM_TOOL_TEMPLATE.replace("+ 32", "+ 32.0"))
        events = list(reversed(audit_events(workspace.store)))
        assert [event.kind for event in events] == ["tool_adoption_refused", "tool_adopted", "tool_adoption_revoked"]
        refused, adopted, revoked = events
        assert refused.actor == "u1" and refused.outcome == "tool.adoption_forbidden" and refused.tool == NAME
        assert adopted.actor == "local" and adopted.details == {"role": "owner", "version": 1, "content_hash": expected}
        assert revoked.details["new_version"] == 2 and revoked.details["adopted_by"] == "local"
        assert audit_target(adopted) == ("tool", NAME)
    finally:
        workspace.close()


def test_the_workspace_keeps_the_adoption_and_forgets_it_after_a_restore(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        save_generated(workspace.tools)
        adopt(workspace.tools)
    finally:
        workspace.close()
    reopened = Workspace(tmp_path / "data", integrations=[])
    try:
        assert reopened.tools.get(NAME).trust == "user-authored"
        reopened.reload_after_restore()
        assert reopened.tools.get(NAME).trust == "untrusted"
    finally:
        reopened.close()


def test_a_server_tenant_workspace_refuses_adoption(tmp_path: Path) -> None:
    """A workspace on handed-in storage serves a server tenant: its policy is ``server`` whatever the profile."""
    tools = ToolLibrary(tmp_path / "tools", sandbox=ContainerDouble(), policy=SandboxPolicy("server"))
    save_generated(tools)
    with pytest.raises(NodestroError) as error:
        tools.adopt(NAME, actor="admin-1", role="admin", expected_hash=shown_hash(tools))
    assert error.value.code == "tool.adoption_desktop_only"
    # The server policy itself is unchanged: user-authored code still needs a container there.
    assert SandboxPolicy("server").needs_isolation("user-authored")
