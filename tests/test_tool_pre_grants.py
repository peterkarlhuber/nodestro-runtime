"""Stored pre-grants for unattended runs (S-2, decision D10c): narrow, traceable, revocable, enforced per call."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import RunIn, WorkflowIn, now
from nodestro_runtime.storage import SQLiteStore
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import ToolApprovals, ToolGate, ToolPreGrants
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.tool_policy import ToolCapability
from nodestro_runtime.workspace import Workspace

TOOL = "celsius_to_fahrenheit"
WRITE = ToolCapability(access="write", reach="external", source="default")


def test_pre_grants_are_refused_where_d10_forbids_them() -> None:
    grants = ToolPreGrants(SQLiteStore(":memory:"))
    common = {"tool": TOOL, "scope": "schedule", "scope_id": "sch_1", "by": "usr_1"}
    cases = [
        ({"kind": "mcp", "capability": WRITE, "version": 1}, "tool.pre_grant_mcp"),
        (
            {"kind": "custom", "capability": WRITE.model_copy(update={"access": "destructive"}), "version": 1},
            "tool.pre_grant_destructive",
        ),
        ({"kind": "custom", "capability": WRITE, "version": None}, "tool.pre_grant_version"),
        (
            {"kind": "custom", "capability": WRITE, "version": 1, "expires_at": now() - timedelta(seconds=1)},
            "tool.pre_grant_expiry",
        ),
    ]
    for extra, code in cases:
        with pytest.raises(NodestroError) as error:
            grants.grant(**common, **extra)  # type: ignore[arg-type]
        assert error.value.code == code
    assert grants.of_scope("schedule", "sch_1") == []


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    fake = ScriptedClient({"AGENT": [tool_use(TOOL, {"celsius": 100}), text("done")] * 4})
    opened = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
    result, _ = opened.tools.save_custom(
        CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, TOOL, trust=opened.tools.editor_trust(TOOL, CUSTOM_TOOL_TEMPLATE)
    )
    assert result.ok
    grants = ToolPreGrants(opened.store)
    opened.runs.tool_gate = ToolGate(ToolApprovals(opened.store), pre_grants=grants.of_scope)
    yield opened
    opened.close()


def test_a_pre_grant_is_stored_per_scope_and_revocable(workspace: Workspace) -> None:
    grants = ToolPreGrants(workspace.store)
    info = workspace.tools.get(TOOL)
    grant = grants.grant(
        tool=TOOL,
        kind=info.kind,
        capability=info.capability,
        version=info.version,
        scope="benchmark",
        scope_id="suite_1",
        by="usr_1",
    )
    assert grant.granted_by == "usr_1" and grant.version == info.version
    assert grants.of_scope("benchmark", "suite_1") == [grant]
    assert grants.of_scope("benchmark", "suite_2") == []
    revoked = grants.revoke("benchmark", "suite_1", grant.id, by="usr_1")
    assert revoked.revoked_at is not None
    with pytest.raises(NodestroError):
        grants.revoke("benchmark", "suite_1", "tpg_missing", by="usr_1")


async def test_an_unattended_run_uses_a_matching_pre_grant_until_it_is_revoked(workspace: Workspace) -> None:
    from nodestro_runtime.launcher import launch

    flow = WorkflowIn.model_validate(
        {
            "name": "Scheduled",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "a", "type": "agent", "agent": {"system_prompt": "AGENT", "tools": [TOOL]}},
            ],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )
    saved = workspace.save_workflow(flow)
    info = workspace.tools.get(TOOL)
    grants = ToolPreGrants(workspace.store)
    grant = grants.grant(
        tool=TOOL,
        kind=info.kind,
        capability=info.capability,
        version=info.version,
        scope="schedule",
        scope_id="sch_1",
        by="usr_1",
    )

    async def approval(ref: str) -> object:
        started = await launch(workspace, saved, RunIn(input="x"), trigger="schedule", trigger_ref=ref)
        done = await workspace.wait(started.id, timeout=20)
        return done.nodes["a"].tool_calls[0].approval

    allowed = await approval("sch_1")
    assert allowed.status == "allowed" and allowed.pre_grant_id == grant.id  # type: ignore[attr-defined]
    other = await approval("sch_2")  # another schedule: no grant
    assert other.status == "denied" and other.code == "tool.approval_unattended"  # type: ignore[attr-defined]
    grants.revoke("schedule", "sch_1", grant.id, by="usr_1")
    revoked = await approval("sch_1")
    assert revoked.status == "denied" and revoked.code == "tool.approval_unattended"  # type: ignore[attr-defined]


async def test_a_new_tool_version_and_the_expiry_end_a_pre_grant(workspace: Workspace) -> None:
    """Security acceptance (D10c): a pre-grant covers exactly the tool version it names, and only until it expires."""
    import asyncio

    from nodestro_runtime.launcher import launch

    flow = WorkflowIn.model_validate(
        {
            "name": "Scheduled",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "a", "type": "agent", "agent": {"system_prompt": "AGENT", "tools": [TOOL]}},
            ],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )
    saved = workspace.save_workflow(flow)
    grants = ToolPreGrants(workspace.store)

    def grant(**extra: object) -> None:
        info = workspace.tools.get(TOOL)
        grants.grant(
            tool=TOOL,
            kind=info.kind,
            capability=info.capability,
            version=info.version,
            scope="schedule",
            scope_id="sch_1",
            by="usr_1",
            **extra,  # type: ignore[arg-type]
        )

    async def approval() -> object:
        started = await launch(workspace, saved, RunIn(input="x"), trigger="schedule", trigger_ref="sch_1")
        done = await workspace.wait(started.id, timeout=20)
        return done.nodes["a"].tool_calls[0].approval

    grant()
    code = CUSTOM_TOOL_TEMPLATE.replace("9 / 5", "1.8")
    result, _ = workspace.tools.save_custom(
        code, CUSTOM_TESTS_TEMPLATE, TOOL, trust=workspace.tools.editor_trust(TOOL, code)
    )
    assert result.ok
    outdated = await approval()
    assert outdated.status == "denied" and outdated.code == "tool.approval_unattended"  # type: ignore[attr-defined]
    grant(expires_at=now() + timedelta(seconds=1))
    await asyncio.sleep(1.2)
    expired = await approval()
    assert expired.status == "denied" and expired.code == "tool.approval_unattended"  # type: ignore[attr-defined]
