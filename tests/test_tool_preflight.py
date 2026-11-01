"""The warning before an unattended start (S-2, risk R1): it names exactly the calls the engine would refuse.

Every positive case is checked against a real run with the same trigger: the warning and the engine's decision
must agree, and the engine keeps refusing (the warning never lets a call through).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime import tool_preflight
from nodestro_runtime.launcher import launch
from nodestro_runtime.schemas import RunIn, Workflow, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import ToolApprovals, ToolGate, ToolPreGrants
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.tool_policy import ToolCapability
from nodestro_runtime.tool_preflight import unattended_tool_issues
from nodestro_runtime.workspace import Workspace

TOOL = "celsius_to_fahrenheit"


def _save_tool(workspace: Workspace, code: str = CUSTOM_TOOL_TEMPLATE) -> None:
    result, _ = workspace.tools.save_custom(
        code, CUSTOM_TESTS_TEMPLATE, TOOL, trust=workspace.tools.editor_trust(TOOL, code)
    )
    assert result.ok, result


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    fake = ScriptedClient({"AGENT": [tool_use(TOOL, {"celsius": 100}), text("done")] * 6})
    opened = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
    _save_tool(opened)
    opened.runs.tool_gate = ToolGate(ToolApprovals(opened.store), pre_grants=ToolPreGrants(opened.store).of_scope)
    yield opened
    opened.close()


def _flow(workspace: Workspace, name: str = "Scheduled", **agent: Any) -> Workflow:
    config = {"name": "Writer", "system_prompt": "AGENT", "tools": [TOOL], **agent}
    return workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": name,
                "nodes": [{"id": "start", "type": "start"}, {"id": "a", "type": "agent", "agent": config}],
                "edges": [{"id": "e", "source": "start", "target": "a"}],
            }
        )
    )


def _grant(workspace: Workspace, scope_id: str = "sch_1") -> None:
    info = workspace.tools.get(TOOL)
    ToolPreGrants(workspace.store).grant(
        tool=TOOL,
        kind=info.kind,
        capability=info.capability,
        version=info.version,
        scope="schedule",
        scope_id=scope_id,
        by="usr_1",
    )


async def _engine_says(workspace: Workspace, workflow: Workflow, trigger: str, ref: str | None) -> str:
    started = await launch(workspace, workflow, RunIn(input="x"), trigger=trigger, trigger_ref=ref)  # type: ignore[arg-type]
    done = await workspace.wait(started.id, timeout=20)
    approval = done.nodes["a"].tool_calls[0].approval
    assert approval is not None
    return approval.status


def _codes(issues: list[Any]) -> list[str]:
    return [issue.code for issue in issues]


def test_nothing_is_reported_while_approvals_are_off(workspace: Workspace) -> None:
    workspace.runs.tool_gate = None
    flow = _flow(workspace)
    assert unattended_tool_issues(workspace, flow, trigger="schedule", trigger_ref="sch_1") == []
    assert unattended_tool_issues(workspace, flow, trigger="api") == []


async def test_a_schedule_warning_matches_the_engine_until_a_pre_grant_and_again_after_a_new_version(
    workspace: Workspace,
) -> None:
    flow = _flow(workspace)
    issues = unattended_tool_issues(workspace, flow, trigger="schedule", trigger_ref="sch_1")
    assert _codes(issues) == ["tool.unattended_pre_grant_missing"]
    found = issues[0]
    assert found.level == "warning" and found.node_id == "a"
    assert found.params["tool"] == TOOL and found.params["node"] == "Writer" and found.params["access"] == "write"
    assert await _engine_says(workspace, flow, "schedule", "sch_1") == "denied"

    _grant(workspace)
    assert unattended_tool_issues(workspace, flow, trigger="schedule", trigger_ref="sch_1") == []
    assert await _engine_says(workspace, flow, "schedule", "sch_1") == "allowed"
    # The grant names one schedule: another one is still warned about.
    assert _codes(unattended_tool_issues(workspace, flow, trigger="schedule", trigger_ref="sch_2")) == [
        "tool.unattended_pre_grant_missing"
    ]

    _save_tool(workspace, CUSTOM_TOOL_TEMPLATE.replace("9 / 5", "1.8"))  # a new version ends the grant
    assert _codes(unattended_tool_issues(workspace, flow, trigger="schedule", trigger_ref="sch_1")) == [
        "tool.unattended_pre_grant_outdated"
    ]
    assert await _engine_says(workspace, flow, "schedule", "sch_1") == "denied"


async def test_batches_before_their_start_and_published_apis_are_named_as_such(workspace: Workspace) -> None:
    flow = _flow(workspace)
    assert _codes(unattended_tool_issues(workspace, flow, trigger="batch")) == ["tool.unattended_not_started"]
    assert _codes(unattended_tool_issues(workspace, flow, trigger="benchmark")) == ["tool.unattended_not_started"]
    assert _codes(unattended_tool_issues(workspace, flow, trigger="api")) == ["tool.unattended_api"]
    assert await _engine_says(workspace, flow, "batch", "bat_1") == "denied"
    assert await _engine_says(workspace, flow, "api", "pub_1") == "denied"


async def test_reading_tools_and_a_justified_never_binding_are_not_reported(workspace: Workspace) -> None:
    reading = _flow(workspace, "Reading", tools=["calculator"])
    assert unattended_tool_issues(workspace, reading, trigger="api") == []
    justified = _flow(
        workspace,
        "Justified",
        tool_policies={TOOL: {"ask": "never", "reason": "Converts numbers only; nothing leaves the run."}},
    )
    assert unattended_tool_issues(workspace, justified, trigger="schedule", trigger_ref="sch_1") == []
    assert await _engine_says(workspace, justified, "schedule", "sch_1") == "allowed"


async def test_a_legacy_exemption_counts_only_for_its_tool_version(workspace: Workspace) -> None:
    saved = _flow(workspace)
    workspace.stamp_legacy_tool_bindings(by="usr_1")  # the one-time activation step marks the existing assignment
    flow = workspace.store.get("workflows", saved.id, Workflow)
    assert flow is not None and flow.nodes[1].agent is not None and flow.nodes[1].agent.tool_policies[TOOL].legacy
    assert unattended_tool_issues(workspace, flow, trigger="api") == []
    assert await _engine_says(workspace, flow, "api", "pub_1") == "allowed"
    _save_tool(workspace, CUSTOM_TOOL_TEMPLATE.replace("9 / 5", "1.8"))
    assert _codes(unattended_tool_issues(workspace, flow, trigger="api")) == ["tool.unattended_api"]
    assert await _engine_says(workspace, flow, "api", "pub_1") == "denied"


def test_sub_workflows_are_checked_and_named(workspace: Workspace) -> None:
    child = _flow(workspace, "Child")
    parent = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Parent",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "sub", "type": "subworkflow", "subworkflow": {"workflow_id": child.id}},
                ],
                "edges": [{"id": "e", "source": "start", "target": "sub"}],
            }
        )
    )
    issues = unattended_tool_issues(workspace, parent, trigger="schedule", trigger_ref="sch_1")
    assert _codes(issues) == ["tool.unattended_pre_grant_missing"]
    assert issues[0].params["workflow"] == "Child" and issues[0].node_id is None


@pytest.mark.parametrize(
    ("kind", "access", "code"),
    [("custom", "destructive", "tool.unattended_destructive"), ("mcp", "read", "tool.unattended_mcp")],
)
def test_destructive_and_mcp_tools_are_never_covered_by_a_pre_grant(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, kind: str, access: str, code: str
) -> None:
    flow = _flow(workspace)
    _grant(workspace)
    capability = ToolCapability(access=access, reach="external", source="admin")  # type: ignore[arg-type]
    monkeypatch.setattr(tool_preflight, "tool_facts", lambda _tools, _name: (capability, 1, kind, None))
    assert _codes(unattended_tool_issues(workspace, flow, trigger="schedule", trigger_ref="sch_1")) == [code]
