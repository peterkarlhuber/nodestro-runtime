"""Per-call tool approvals in real runs (S-2a step 2b, decision D10): the engine asks, waits, denies, expires.

Scripted model answers; the tool is an own tool written in the tool editor (no capability statement, so it counts
as ``write`` + ``external`` and ``auto`` asks before every call).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime.schemas import Run, RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import ENABLE_ENV, ToolApprovalRequest, ToolApprovals, ToolGate, WaitingSlots
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.tool_policy import ToolBinding
from nodestro_runtime.workspace import Workspace

TOOL = "celsius_to_fahrenheit"
Make = Callable[..., Workspace]


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(*, gate: bool = True, wait: timedelta = timedelta(minutes=15), slots: int = 3) -> Workspace:
        script = {"AGENT": [tool_use(TOOL, {"celsius": 100}), text("done")]}
        fake = ScriptedClient(script)
        workspace = Workspace(tmp_path / f"data{len(opened)}", client_factory=lambda _provider: fake, integrations=[])
        opened.append(workspace)
        result, _ = workspace.tools.save_custom(
            CUSTOM_TOOL_TEMPLATE,
            CUSTOM_TESTS_TEMPLATE,
            TOOL,
            trust=workspace.tools.editor_trust(TOOL, CUSTOM_TOOL_TEMPLATE),
        )
        assert result.ok, result
        if gate:
            approvals = ToolApprovals(workspace.store, wait=wait, poll_seconds=0.02)
            workspace.runs.tool_gate = ToolGate(approvals, slots=WaitingSlots(slots))
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def flow(**agent: Any) -> WorkflowIn:
    config = {"name": "Agent", "system_prompt": "AGENT", "tools": [TOOL], **agent}
    return WorkflowIn.model_validate(
        {
            "name": "Approvals",
            "nodes": [{"id": "start", "type": "start"}, {"id": "a", "type": "agent", "agent": config}],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )


async def start(workspace: Workspace, **agent: Any) -> Run:
    saved = workspace.save_workflow(flow(**agent))
    return await workspace.run(saved, RunIn(input="x"))


async def pending(workspace: Workspace, run_id: str) -> ToolApprovalRequest:
    approvals = ToolApprovals(workspace.store)
    for _ in range(500):
        found = approvals.pending(run_id)
        if found:
            return found[0]
        await asyncio.sleep(0.02)
    raise AssertionError("no approval request")


def call_of(run: Run) -> Any:
    calls = run.nodes["a"].tool_calls
    assert len(calls) == 1
    return calls[0]


async def test_without_the_switch_runs_call_tools_as_before(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    workspace = make(gate=False)
    assert workspace.runs.tool_gate is None
    done = await workspace.wait((await start(workspace)).id, timeout=20)
    assert done.status == "succeeded", done.error
    record = call_of(done)
    assert record.output == "212.0" and record.approval is None


def test_the_switch_turns_the_gate_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        assert workspace.runs.tool_gate is not None
    finally:
        workspace.close()


async def test_an_approved_call_runs_with_the_shown_arguments(make: Make) -> None:
    workspace = make()
    started = await start(workspace)
    request = await pending(workspace, started.id)
    assert request.tool == TOOL and request.input == {"celsius": 100} and request.node_id == "a"
    live = workspace.runs.get(started.id)
    assert live is not None and live.status == "running"
    assert call_of(live).approval.status == "pending"
    ToolApprovals(workspace.store).decide(
        started.id, request.id, approved=True, by="usr_1", arguments=request.arguments_hash
    )
    done = await workspace.wait(started.id, timeout=20)
    assert done.status == "succeeded", done.error
    record = call_of(done)
    assert record.output == "212.0" and not record.is_error
    assert record.approval.status == "approved" and record.approval.decided_by == "usr_1"
    assert record.approval.capability == {"access": "write", "reach": "external", "source": "default"}


async def test_a_rejected_call_is_denied_with_the_comment(make: Make) -> None:
    workspace = make()
    started = await start(workspace)
    request = await pending(workspace, started.id)
    ToolApprovals(workspace.store).decide(
        started.id, request.id, approved=False, by="usr_1", arguments=request.arguments_hash, comment="Wrong unit"
    )
    done = await workspace.wait(started.id, timeout=20)
    assert done.status == "succeeded", done.error
    record = call_of(done)
    assert record.is_error and "Wrong unit" in (record.output or "")
    assert record.approval.status == "rejected" and record.approval.code == "tool.approval_rejected"
    # AD-03: the run succeeded without the call, and says so.
    assert [(item.code, item.tool, item.count) for item in done.warnings] == [
        ("tool.approval_rejected", record.name, 1)
    ]
    assert done.warnings[0].node_id in done.nodes


async def test_an_unanswered_call_is_denied_at_the_deadline(make: Make) -> None:
    workspace = make(wait=timedelta(milliseconds=300))
    done = await workspace.wait((await start(workspace)).id, timeout=20)
    assert done.status == "succeeded", done.error
    record = call_of(done)
    assert record.is_error and record.approval.status == "expired"
    assert record.approval.code == "tool.approval_timeout"
    assert [(item.code, item.tool) for item in done.warnings] == [("tool.approval_timeout", record.name)]
    stored = ToolApprovals(workspace.store).get(done.id, record.approval.request_id)
    assert stored is not None and stored.status == "expired"


async def test_cancelling_a_waiting_run_cancels_its_question(make: Make) -> None:
    workspace = make()
    started = await start(workspace)
    request = await pending(workspace, started.id)
    assert workspace.cancel(started.id)
    done = await workspace.wait(started.id, timeout=20)
    assert done.status == "cancelled"
    stored = ToolApprovals(workspace.store).get(started.id, request.id)
    assert stored is not None and stored.status == "cancelled"
    assert workspace.runs.tool_gate is not None and workspace.runs.tool_gate.slots.waiting == 0


async def test_a_full_waiting_capacity_denies_at_once(make: Make) -> None:
    workspace = make(slots=1)
    assert workspace.runs.tool_gate is not None
    assert workspace.runs.tool_gate.slots.enter("run_other")
    done = await workspace.wait((await start(workspace)).id, timeout=20)
    assert done.status == "succeeded", done.error
    record = call_of(done)
    assert record.is_error and record.approval.status == "denied"
    assert record.approval.code == "tool.approval_capacity"
    assert ToolApprovals(workspace.store).of_run(done.id) == []


async def test_a_legacy_exemption_holds_for_its_tool_version_only(make: Make) -> None:
    workspace = make()
    saved = workspace.save_workflow(flow())
    workspace.stamp_legacy_tool_bindings()  # the activation step (the only source of legacy exemptions)
    started = await workspace.run(workspace.workflow(saved.id), RunIn(input="x"))
    done = await workspace.wait(started.id, timeout=20)
    assert done.status == "succeeded", done.error
    assert call_of(done).approval.status == "allowed" and call_of(done).output == "212.0"

    # An exemption for another tool version (here: forged in a save, so dropped) never applies.
    other = make(wait=timedelta(milliseconds=200))
    version = other.tools.get(TOOL).version
    stale = {TOOL: ToolBinding(ask="never", legacy=True, bound_version=version + 1).model_dump()}
    done = await other.wait((await start(other, tool_policies=stale)).id, timeout=20)
    assert call_of(done).approval.status == "expired"


async def test_a_restart_expires_the_questions_of_interrupted_runs(make: Make) -> None:
    workspace = make(gate=False)
    done = await workspace.wait((await start(workspace)).id, timeout=20)
    stuck = done.model_copy(update={"id": "run_stuck", "status": "running"})
    workspace.store.put("runs", stuck.id, stuck)
    approvals = ToolApprovals(workspace.store)
    request = approvals.request(
        run_id=stuck.id,
        node_id="a",
        call_id="toolu_1",
        tool=TOOL,
        arguments={"celsius": 1},
        capability=workspace.tools.get(TOOL).capability,
    )
    assert workspace.runs.recover() == 1
    stored = approvals.get(stuck.id, request.id)
    assert stored is not None and stored.status == "expired"
    with pytest.raises(Exception) as error:
        approvals.decide(stuck.id, request.id, approved=True, by="usr_1", arguments=request.arguments_hash)
    assert getattr(error.value, "code", None) == "tool.approval_expired"


async def test_an_unattended_run_denies_what_would_ask(make: Make) -> None:
    workspace = make()
    saved = workspace.save_workflow(flow())
    started = await workspace.run(saved, RunIn(input="x"), trigger="benchmark")
    done = await workspace.wait(started.id, timeout=20)
    assert done.status == "succeeded", done.error
    record = call_of(done)
    assert record.is_error and record.approval.status == "denied"
    assert record.approval.code == "tool.approval_unattended"
    assert ToolApprovals(workspace.store).of_run(done.id) == []


def test_the_switch_opens_changing_mcp_tools_only_behind_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    closed = Workspace(tmp_path / "closed", integrations=[])
    monkeypatch.setenv(ENABLE_ENV, "1")
    opened = Workspace(tmp_path / "opened", integrations=[])
    try:
        assert closed.mcp.write_gate is False and closed.runs.tool_gate is None
        assert opened.mcp.write_gate is True and opened.runs.tool_gate is not None
    finally:
        closed.close()
        opened.close()
