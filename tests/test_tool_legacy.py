"""Legacy exemptions (S-2, decision D10b): existing bindings keep working, visibly; nothing else creates one."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime.schemas import RunIn, Workflow, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import ToolApprovals, ToolGate
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.tool_policy import ToolBinding
from nodestro_runtime.workspace import Workspace

TOOL = "celsius_to_fahrenheit"


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    script = {"AGENT": [tool_use(TOOL, {"celsius": 100}), text("done")] * 5}
    fake = ScriptedClient(script)
    opened = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
    save_tool(opened)
    yield opened
    opened.close()


def save_tool(workspace: Workspace, code: str = CUSTOM_TOOL_TEMPLATE) -> None:
    result, _ = workspace.tools.save_custom(
        code, CUSTOM_TESTS_TEMPLATE, TOOL, trust=workspace.tools.editor_trust(TOOL, code)
    )
    assert result.ok, result


def flow(**agent: Any) -> WorkflowIn:
    config = {"name": "Agent", "system_prompt": "AGENT", "tools": [TOOL], **agent}
    return WorkflowIn.model_validate(
        {
            "name": "Legacy",
            "nodes": [{"id": "start", "type": "start"}, {"id": "a", "type": "agent", "agent": config}],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )


def policies(workflow: Workflow | WorkflowIn) -> dict[str, ToolBinding]:
    node = next(node for node in workflow.nodes if node.id == "a")
    assert node.agent is not None
    return node.agent.tool_policies


def gate(workspace: Workspace, wait: timedelta = timedelta(milliseconds=300)) -> None:
    workspace.runs.tool_gate = ToolGate(ToolApprovals(workspace.store, wait=wait, poll_seconds=0.02))


def test_activation_stamps_every_existing_binding_visibly_and_once(workspace: Workspace) -> None:
    saved = workspace.save_workflow(flow())
    workspace.save_workflow(saved.model_copy(update={"name": "Legacy v2"}))
    version = workspace.tools.get(TOOL).version
    report = workspace.stamp_legacy_tool_bindings()
    assert report.workflows == 1 and report.versions == 2
    assert {(item.workflow_id, item.tool, item.bound_version) for item in report.bindings} == {
        (saved.id, TOOL, version)
    }
    expected = ToolBinding(ask="never", legacy=True, bound_version=version)
    assert policies(workspace.workflow(saved.id)) == {TOOL: expected}
    latest, number = workspace.resolve_workflow(saved.id, None)
    assert number == 2 and policies(latest) == {TOOL: expected}  # schedules and sub-workflows run versions
    assert policies(workspace.resolve_workflow(saved.id, 1)[0]) == {TOOL: expected}
    assert workspace.store.get_raw("tool_approvals_activation", "legacy") is not None
    again = workspace.stamp_legacy_tool_bindings()
    assert again.bindings == [] and again.workflows == 0


async def test_a_legacy_binding_runs_unasked_until_the_tool_changes(workspace: Workspace) -> None:
    saved = workspace.save_workflow(flow())
    workspace.stamp_legacy_tool_bindings()
    gate(workspace)
    done = await workspace.wait((await workspace.run(workspace.workflow(saved.id), RunIn(input="x"))).id, timeout=20)
    record = done.nodes["a"].tool_calls[0]
    assert record.approval is not None and record.approval.status == "allowed" and record.output == "212.0"
    # A new version of the tool ends the exemption: the next run asks (and times out here).
    save_tool(workspace, CUSTOM_TOOL_TEMPLATE.replace("9 / 5", "1.8"))
    done = await workspace.wait((await workspace.run(workspace.workflow(saved.id), RunIn(input="x"))).id, timeout=20)
    record = done.nodes["a"].tool_calls[0]
    assert record.approval is not None and record.approval.status == "expired"


def test_saving_never_creates_a_legacy_exemption(workspace: Workspace) -> None:
    version = workspace.tools.get(TOOL).version
    forged = {TOOL: ToolBinding(ask="never", legacy=True, bound_version=version).model_dump()}
    # A new or imported workflow cannot bring one along.
    assert policies(workspace.save_workflow(flow(tool_policies=forged))) == {}
    saved = workspace.save_workflow(flow())
    workspace.stamp_legacy_tool_bindings()
    stamped = workspace.workflow(saved.id)
    # Saving the workflow again keeps its own exemption.
    assert policies(workspace.save_workflow(stamped.model_copy(update={"name": "Renamed"}))) == policies(stamped)
    # A copy (a new workflow) gets a new binding.
    copy = WorkflowIn.model_validate(stamped.model_dump(exclude={"id", "created_at", "updated_at"}))
    assert policies(workspace.save_workflow(copy)) == {}
    # Moving the exemption to another tool version or another node is refused, too.
    node = stamped.nodes[1]
    assert node.agent is not None
    bumped = {TOOL: ToolBinding(ask="never", legacy=True, bound_version=version + 1)}
    moved = stamped.model_copy(
        update={
            "nodes": [
                stamped.nodes[0],
                node.model_copy(update={"agent": node.agent.model_copy(update={"tool_policies": bumped})}),
            ]
        }
    )
    assert policies(workspace.save_workflow(moved)) == {}
    # Bindings the person chose (with a reason) are kept as they are.
    reasoned = {TOOL: ToolBinding(ask="never", reason="Converts numbers only, nothing is stored").model_dump()}
    assert policies(workspace.save_workflow(flow(tool_policies=reasoned)))[TOOL].reason is not None


def test_bindings_of_missing_tools_are_not_stamped(workspace: Workspace) -> None:
    saved = workspace.save_workflow(flow(tools=[TOOL, "gone_tool"]))
    report = workspace.stamp_legacy_tool_bindings()
    assert [item.tool for item in report.bindings if item.workflow_version is None] == [TOOL]
    assert set(policies(workspace.workflow(saved.id))) == {TOOL}
