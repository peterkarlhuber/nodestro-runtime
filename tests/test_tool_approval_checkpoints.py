"""Runs that pause at a tool call waiting for a person (M6b, ``NODESTRO_TOOL_APPROVAL_CHECKPOINTS``).

The agent's turn stops at the call (an orkexa turn checkpoint), the run pauses and frees its worker; the decision
continues it without asking the model again. Covers the Evolution review conditions: only the exact approved request
answers the call, the deadline and the execution window, the switch off at restart, checkpoints bound to the tenant
and deleted with the run, the deadline sweep before anything continues, the per-tenant cap, at most once
(``executed`` by compare-and-set, ``tool.result_lost``) and the audit trail without arguments.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime import tool_checkpoints
from nodestro_runtime.schemas import Run, RunIn, WorkflowIn, now
from nodestro_runtime.testing import ScriptedClient, message, text, tool_use
from nodestro_runtime.tool_approvals import (
    CHECKPOINTS_ENV,
    ENABLE_ENV,
    MAX_PAUSED_ENV,
    ToolApprovalRequest,
    ToolApprovals,
    audit_events,
)
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.workspace import Workspace

TOOL = "celsius_to_fahrenheit"
Make = Callable[..., Workspace]


def two_calls() -> Any:
    return message(
        [
            {"type": "tool_use", "id": "toolu_1", "name": TOOL, "input": {"celsius": 100}},
            {"type": "tool_use", "id": "toolu_2", "name": TOOL, "input": {"celsius": 0}},
        ],
        stop_reason="tool_use",
    )


@pytest.fixture(autouse=True)
def switches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(CHECKPOINTS_ENV, "1")


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []
    fakes: dict[str, ScriptedClient] = {}

    def factory(
        *, data: str = "data", script: list[Any] | None = None, wait: timedelta | None = None, tool: bool = True
    ) -> Workspace:
        if data not in fakes:
            fakes[data] = ScriptedClient({"AGENT": script or [tool_use(TOOL, {"celsius": 100}), text("done")]})
        fake = fakes[data]
        workspace = Workspace(tmp_path / data, client_factory=lambda _provider: fake, integrations=[])
        workspace.fake = fake  # type: ignore[attr-defined]
        opened.append(workspace)
        if tool and TOOL not in workspace.tools:
            result, _ = workspace.tools.save_custom(
                CUSTOM_TOOL_TEMPLATE,
                CUSTOM_TESTS_TEMPLATE,
                TOOL,
                trust=workspace.tools.editor_trust(TOOL, CUSTOM_TOOL_TEMPLATE),
            )
            assert result.ok, result
        gate = workspace.runs.tool_gate
        assert gate is not None and gate.checkpoints
        if wait is not None:
            gate.approvals = ToolApprovals(workspace.store, wait=wait)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def flow() -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Approvals",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "a", "type": "agent", "agent": {"name": "Agent", "system_prompt": "AGENT", "tools": [TOOL]}},
            ],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )


async def paused_run(workspace: Workspace) -> Run:
    saved = workspace.save_workflow(flow())
    started = await workspace.run(saved, RunIn(input="x"))
    return await workspace.wait(started.id, timeout=20)


def requests(workspace: Workspace, run_id: str) -> list[ToolApprovalRequest]:
    return ToolApprovals(workspace.store).of_run(run_id)


def model_calls(workspace: Workspace) -> int:
    return len(workspace.fake.messages.calls)  # type: ignore[attr-defined]


def kinds(workspace: Workspace) -> list[str]:
    return [event.kind for event in reversed(audit_events(workspace.store))]


async def decide(workspace: Workspace, run: Run, *, approved: bool = True, comment: str | None = None) -> Run:
    (request,) = [item for item in requests(workspace, run.id) if item.status == "pending"]
    workspace.runs.decide_tool_call(
        run.id, request.id, approved=approved, by="usr_1", arguments=request.arguments_hash, comment=comment
    )
    return await workspace.wait(run.id, timeout=20)


def call(run: Run, index: int = 0) -> Any:
    return run.nodes["a"].tool_calls[index]


# ---- the main path ------------------------------------------------------------------------------------------------


async def test_a_run_pauses_at_the_call_and_continues_after_the_approval(make: Make) -> None:
    workspace = make()
    paused = await paused_run(workspace)
    assert paused.status == "paused", paused.error
    assert paused.nodes["a"].status == "awaiting_approval" and paused.nodes["a"].finished_at is None
    (request,) = requests(workspace, paused.id)
    assert request.checkpointed and request.status == "pending" and request.use == "open"
    assert call(paused).approval.status == "pending" and call(paused).output is None
    assert model_calls(workspace) == 1
    # The worker is free: no waiting slot is held while the run is paused.
    gate = workspace.runs.tool_gate
    assert gate is not None and gate.slots.waiting == 0
    stored = tool_checkpoints.load(workspace.store, paused.id, "a", [])
    assert stored is not None and stored.request_ids == [request.id] and "tools" not in stored.turn

    done = await decide(workspace, paused)
    assert done.status == "succeeded", done.error
    assert call(done).output == "212.0" and call(done).approval.status == "approved"
    assert len(done.nodes["a"].tool_calls) == 1  # the resumed call kept its record
    assert model_calls(workspace) == 2  # the model was not asked again for the paused round
    assert done.nodes["a"].usage.requests == 2  # the usage before the pause is kept
    (used,) = requests(workspace, done.id)
    assert used.use == "executed" and used.executed_at is not None
    assert tool_checkpoints.load(workspace.store, done.id, "a", []) is None
    assert kinds(workspace) == ["suspended", "decided", "resumed", "executed"]
    assert all(event.details.get("input") is None for event in audit_events(workspace.store))


async def test_a_rejected_call_is_not_made(make: Make) -> None:
    workspace = make()
    done = await decide(workspace, await paused_run(workspace), approved=False, comment="No")
    assert done.status == "succeeded", done.error
    assert call(done).is_error and "No" in (call(done).output or "")
    assert call(done).approval.status == "rejected"
    # Closed when the run continued, so a later continue of the run never audits it again.
    assert requests(workspace, done.id)[0].use == "closed"
    assert kinds(workspace).count("resumed") == 1


async def test_two_calls_of_one_round_wait_together(make: Make) -> None:
    workspace = make(script=[two_calls(), text("done")])
    paused = await paused_run(workspace)
    first, second = requests(workspace, paused.id)
    workspace.runs.decide_tool_call(paused.id, first.id, approved=True, by="u", arguments=first.arguments_hash)
    still = workspace.runs.get(paused.id)
    assert still is not None and still.status == "paused"  # one call still waits: nothing continues yet
    workspace.runs.decide_tool_call(paused.id, second.id, approved=False, by="u", arguments=second.arguments_hash)
    done = await workspace.wait(paused.id, timeout=20)
    assert done.status == "succeeded", done.error
    assert call(done, 0).output == "212.0" and call(done, 1).approval.status == "rejected"
    assert model_calls(workspace) == 2


# ---- deadline, window, staleness, at most once --------------------------------------------------------------------


async def test_the_sweep_expires_an_unanswered_call(make: Make) -> None:
    workspace = make(wait=timedelta(milliseconds=100))
    paused = await paused_run(workspace)
    await asyncio.sleep(0.2)
    assert workspace.runs.sweep_tool_waits() == [paused.id]
    done = await workspace.wait(paused.id, timeout=20)
    assert done.status == "succeeded", done.error
    assert call(done).approval.status == "expired" and call(done).output != "212.0"
    with pytest.raises(Exception) as error:
        request = requests(workspace, done.id)[0]
        ToolApprovals(workspace.store).decide(
            done.id, request.id, approved=True, by="u", arguments=request.arguments_hash
        )
    assert getattr(error.value, "code", None) == "tool.approval_expired"


async def test_an_approval_used_too_late_is_stale(make: Make) -> None:
    workspace = make()
    paused = await paused_run(workspace)
    (request,) = requests(workspace, paused.id)
    approvals = ToolApprovals(workspace.store)
    approvals.decide(paused.id, request.id, approved=True, by="u", arguments=request.arguments_hash)
    decided = approvals.get(paused.id, request.id)
    assert decided is not None
    old = decided.model_copy(update={"decided_at": now() - timedelta(minutes=16)})
    workspace.store.put("tool_approvals", old.key, old)
    workspace.runs.sweep_tool_waits()
    done = await workspace.wait(paused.id, timeout=20)
    assert call(done).approval.code == "tool.approval_stale" and call(done).output != "212.0"
    assert requests(workspace, done.id)[0].use == "stale"
    assert "stale" in kinds(workspace)


async def save_tool(workspace: Workspace, code: str) -> None:
    result, _ = workspace.tools.save_custom(
        code, CUSTOM_TESTS_TEMPLATE, TOOL, trust=workspace.tools.editor_trust(TOOL, code)
    )
    assert result.ok, result


async def test_a_new_tool_version_makes_the_approval_stale(make: Make) -> None:
    workspace = make()
    paused = await paused_run(workspace)
    before = workspace.tools.get(TOOL).version
    # Same definition (name, description, schema), other code: a new version the person did not see.
    await save_tool(workspace, CUSTOM_TOOL_TEMPLATE.replace("return celsius * 9 / 5 + 32", "return 32 + celsius * 1.8"))
    assert workspace.tools.get(TOOL).version == (before or 0) + 1
    done = await decide(workspace, paused)
    assert done.status == "succeeded", done.error
    assert call(done).approval.code == "tool.approval_stale" and call(done).output != "212.0"
    assert requests(workspace, done.id)[0].use == "stale"


async def test_a_changed_tool_definition_ends_the_waiting_turn(make: Make) -> None:
    workspace = make()
    paused = await paused_run(workspace)
    await save_tool(workspace, CUSTOM_TOOL_TEMPLATE.replace("Convert", "Convert (v2)", 1))
    done = await decide(workspace, paused)
    assert done.nodes["a"].status == "failed" and done.nodes["a"].error_code == "tool.checkpoint_stale"
    assert call(done).output is None and call(done).approval.status == "cancelled"
    assert requests(workspace, done.id)[0].use == "closed"
    assert tool_checkpoints.runs_with_checkpoints(workspace.store) == set()


async def test_a_call_marked_executed_is_never_made_again(make: Make) -> None:
    workspace = make()
    paused = await paused_run(workspace)
    (request,) = requests(workspace, paused.id)
    approvals = ToolApprovals(workspace.store)
    approvals.decide(paused.id, request.id, approved=True, by="u", arguments=request.arguments_hash)
    decided = approvals.get(paused.id, request.id)
    assert decided is not None and approvals.use(decided, "executed") is not None  # a crash right after the mark
    assert approvals.use(decided, "executed") is None  # the mark is set once
    workspace.runs.sweep_tool_waits()
    done = await workspace.wait(paused.id, timeout=20)
    assert call(done).approval.code == "tool.result_lost" and call(done).output != "212.0"
    assert "result_lost" in kinds(workspace)


# ---- restarts, switch off, cancel, cap, tenant ---------------------------------------------------------------------


async def test_a_restart_before_the_decision_keeps_the_pause(make: Make) -> None:
    workspace = make()
    paused = await paused_run(workspace)
    workspace.close()
    again = make()
    assert again.runs.get(paused.id).status == "paused"  # type: ignore[union-attr]
    done = await decide(again, paused)
    assert done.status == "succeeded", done.error
    assert call(done).output == "212.0" and model_calls(again) == 2


async def test_a_restart_after_the_decision_continues_in_the_sweep(make: Make) -> None:
    workspace = make()
    paused = await paused_run(workspace)
    (request,) = requests(workspace, paused.id)
    ToolApprovals(workspace.store).decide(
        paused.id, request.id, approved=True, by="u", arguments=request.arguments_hash
    )
    workspace.close()  # stopped before the run continued
    again = make()
    assert again.runs.get(paused.id).status == "paused"  # type: ignore[union-attr]
    assert again.runs.sweep_tool_waits() == [paused.id]
    done = await again.wait(paused.id, timeout=20)
    assert done.status == "succeeded" and call(done).output == "212.0"


async def test_with_the_switch_off_at_restart_the_waiting_call_never_runs(
    make: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = make()
    paused = await paused_run(workspace)
    (request,) = requests(workspace, paused.id)
    ToolApprovals(workspace.store).decide(
        paused.id, request.id, approved=True, by="u", arguments=request.arguments_hash
    )
    workspace.close()
    monkeypatch.delenv(CHECKPOINTS_ENV)
    fake = ScriptedClient({"AGENT": [text("unused")]})
    again = Workspace(Path(workspace.data_dir), client_factory=lambda _provider: fake, integrations=[])
    try:
        assert again.runs.tool_gate is not None and not again.runs.tool_gate.checkpoints
        closed = ToolApprovals(again.store).get(paused.id, request.id)
        assert closed is not None and closed.use == "closed" and closed.closed_because == "switch_off"
        assert again.runs.sweep_tool_waits() == [paused.id]
        done = await again.wait(paused.id, timeout=20)
        assert tool_checkpoints.runs_with_checkpoints(again.store) == set()
        assert done.nodes["a"].status == "failed" and done.nodes["a"].error_code == "tool.checkpoint_expired"
        assert call(done).output is None
        assert "switch_off" in kinds(again)
    finally:
        again.close()


async def test_cancelling_a_paused_run_closes_its_question_and_checkpoint(make: Make) -> None:
    workspace = make()
    paused = await paused_run(workspace)
    assert workspace.cancel(paused.id)
    done = await workspace.wait(paused.id, timeout=20)
    assert done.status == "cancelled"
    (request,) = requests(workspace, paused.id)
    assert request.status == "cancelled" and request.closed_because == "cancelled"
    assert tool_checkpoints.runs_with_checkpoints(workspace.store) == set()


async def test_the_sweep_drops_the_checkpoint_of_a_deleted_run(make: Make) -> None:
    workspace = make()
    paused = await paused_run(workspace)
    workspace.store.delete("runs", paused.id)
    assert workspace.runs.sweep_tool_waits() == []
    assert tool_checkpoints.runs_with_checkpoints(workspace.store) == set()
    assert requests(workspace, paused.id)[0].status == "cancelled"


async def test_the_tenant_cap_denies_at_once(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MAX_PAUSED_ENV, "0")
    workspace = make()
    done = await paused_run(workspace)
    assert done.status == "succeeded", done.error
    assert call(done).approval.code == "tool.approval_capacity"
    assert requests(workspace, done.id) == []


def test_a_checkpoint_of_another_tenant_or_format_is_never_used(make: Make) -> None:
    workspace = make()
    checkpoint = tool_checkpoints.AgentCheckpoint(
        run_id="run_1", node_id="a", turn={}, expires_at=now() + timedelta(minutes=5)
    )
    tool_checkpoints.save(workspace.store, checkpoint)
    assert tool_checkpoints.load(workspace.store, "run_1", "a", []) is not None
    forged = checkpoint.model_copy(update={"tenant_id": "ten_other"})
    workspace.store.put(tool_checkpoints.COLLECTION, tool_checkpoints.key("run_1", "a", []), forged)
    assert tool_checkpoints.load(workspace.store, "run_1", "a", []) is None
    newer = checkpoint.model_copy(update={"version": 99})
    workspace.store.put(tool_checkpoints.COLLECTION, tool_checkpoints.key("run_1", "a", []), newer)
    assert tool_checkpoints.load(workspace.store, "run_1", "a", []) is None
    expired = checkpoint.model_copy(update={"expires_at": now() - timedelta(seconds=1)})
    workspace.store.put(tool_checkpoints.COLLECTION, tool_checkpoints.key("run_1", "a", []), expired)
    assert tool_checkpoints.load(workspace.store, "run_1", "a", []) is None


def test_without_the_second_switch_runs_wait_in_the_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CHECKPOINTS_ENV)
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        assert workspace.runs.tool_gate is not None and not workspace.runs.tool_gate.checkpoints
    finally:
        workspace.close()


# ---- inside orchestrated teams (scenario A) ---------------------------------------------------------------------


async def test_a_team_paused_at_a_tool_call_pauses_its_orchestrator(make: Make, tmp_path: Path) -> None:
    from nodestro_runtime.run_views import approvals_of_tree
    from nodestro_runtime.testing import parsed

    ids: dict[str, str] = {}
    boss = [
        parsed(lambda model: model(action="delegate", member=ids["t"], task="Convert", reason="needs the tool")),
        parsed(lambda model: model(action="finish", answer="212", reason="done")),
    ]
    workspace = make(data="teams", script=[tool_use(TOOL, {"celsius": 100}), text("212")])
    workspace.fake.messages.script["You coordinate teams"] = boss  # type: ignore[attr-defined]
    team = workspace.save_workflow(flow())
    ids["t"] = team.id
    lead = WorkflowIn.model_validate(
        {
            "name": "Lead",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "lead", "type": "orchestrator", "orchestrator": {"members": [{"workflow_id": team.id}]}},
            ],
            "edges": [{"id": "e", "source": "start", "target": "lead"}],
        }
    )
    started = await workspace.run(workspace.save_workflow(lead), RunIn(input="x"))
    parent = await workspace.wait(started.id, timeout=20)
    assert parent.status == "paused", parent.error
    assert parent.nodes["lead"].status == "awaiting_approval"
    child_id = parent.nodes["lead"].child_run_ids[0]
    assert approvals_of_tree(workspace.runs, parent) == []  # no approval node: the wait is a tool call
    (request,) = requests(workspace, child_id)
    workspace.runs.decide_tool_call(child_id, request.id, approved=True, by="u", arguments=request.arguments_hash)
    done = await workspace.wait(parent.id, timeout=20)
    assert done.status == "succeeded", done.error
    child = workspace.runs.get(child_id)
    assert child is not None and call(child).output == "212.0"
    assert done.nodes["lead"].orchestration["steps"][0]["outcomes"][0]["status"] == "succeeded"
