"""Continuing a run asks before tools are called again (audit B-02).

The confirmation of :func:`nodestro_runtime.resume.plan` must see every tool call a re-run node made: the calls of
its child runs (sub-workflows, orchestrator teams) and calls that started before the process died, when the node's
own records had not been stored yet.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
import shutil
import time
from typing import Any

import pytest

from nodestro_runtime import resume
from nodestro_runtime.schemas import Run, RunIn, Workflow, WorkflowIn
from nodestro_runtime.storage import SQLiteStore
from nodestro_runtime.testing import ScriptedClient, parsed, text, tool_use
from nodestro_runtime.workspace import DATABASE, LOCK_FILE, Workspace

TOOL = "record_note"
SOURCE = (
    "def record_note(note: str) -> str:\n"
    '    """Store a note.\n\n    Args:\n        note: The note.\n\n    Returns:\n        A confirmation.\n    """\n'
    "    total = 0\n"
    "    for i in range(int(note) if note.isdigit() else 0):\n"
    "        total += i\n"
    "    return 'stored'\n"
)
TESTS = "from tool import record_note\n\n\ndef test_note():\n    assert record_note('a') == 'stored'\n"
BOSS = "You coordinate teams"
Make = Callable[[dict[str, list[Any]]], Workspace]


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]], directory: Path | None = None) -> Workspace:
        fake = ScriptedClient(script)
        workspace = Workspace(
            directory or tmp_path / f"data{len(opened)}", client_factory=lambda _provider: fake, integrations=[]
        )
        opened.append(workspace)
        if directory is None:
            result, _ = workspace.tools.save_custom(
                SOURCE, TESTS, TOOL, trust=workspace.tools.editor_trust(TOOL, SOURCE)
            )
            assert result.ok, result
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def edge(source: str, target: str) -> dict[str, Any]:
    return {"id": f"{source}-{target}", "source": source, "target": target}


def acting(workspace: Workspace, name: str = "Child") -> Workflow:
    """A workflow whose agent calls the tool."""
    agent = {"name": "Act", "system_prompt": "ACT", "tools": [TOOL]}
    return workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": name,
                "description": f"{name} team",
                "nodes": [{"id": "start", "type": "start"}, {"id": "act", "type": "agent", "agent": agent}],
                "edges": [edge("start", "act")],
            }
        )
    )


async def finished(workspace: Workspace, workflow: Workflow | WorkflowIn) -> Run:
    saved = workspace.save_workflow(workflow) if isinstance(workflow, WorkflowIn) else workflow
    started = await workspace.run(saved, RunIn(input="go"))
    return await workspace.wait(started.id, timeout=30)


def node_of(found: resume.ResumePlan, node_id: str) -> resume.ResumeNode:
    return next(item for item in found.nodes if item.node_id == node_id)


async def test_a_rerun_sub_workflow_reports_the_tool_calls_of_its_child_run(make: Make) -> None:
    # The child's agent calls the tool, then fails: the sub-workflow node and the parent run fail.
    workspace = make({"ACT": [tool_use(TOOL, {"note": "x"}), text("x", stop_reason="max_tokens")]})
    child = acting(workspace)
    parent = WorkflowIn.model_validate(
        {
            "name": "Parent",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "sub", "type": "subworkflow", "label": "Sub", "subworkflow": {"workflow_id": child.id}},
            ],
            "edges": [edge("start", "sub")],
        }
    )
    failed = await finished(workspace, parent)
    assert failed.status == "failed"
    child_run = workspace.runs.get(failed.nodes["sub"].child_run_id or "")
    assert child_run is not None and [call.name for call in child_run.nodes["act"].tool_calls] == [TOOL]
    found = resume.plan(workspace, failed.id)
    sub = node_of(found, "sub")
    assert sub.action != "reuse"
    assert sub.side_effects == [TOOL] and found.needs_confirmation
    assert any(issue.code == "resume.side_effects" for issue in found.issues)


async def test_a_rerun_orchestrator_reports_the_tool_calls_of_its_teams(make: Make) -> None:
    ids: dict[str, str] = {}
    # The orchestrator delegates once, the team calls the tool; the orchestrator's next decision fails.
    workspace = make(
        {
            BOSS: [parsed(lambda model: model(action="delegate", member=ids["team"], task="Note it", reason="go"))],
            "ACT": [tool_use(TOOL, {"note": "x"}), text("noted")],
        }
    )
    ids["team"] = acting(workspace, "Team").id
    boss = WorkflowIn.model_validate(
        {
            "name": "Boss",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "lead",
                    "type": "orchestrator",
                    "label": "Lead",
                    "orchestrator": {"members": [{"workflow_id": ids["team"]}]},
                },
            ],
            "edges": [edge("start", "lead")],
        }
    )
    failed = await finished(workspace, boss)
    assert failed.status == "failed"
    lead = failed.nodes["lead"]
    assert lead.tool_calls == [] and len(lead.child_run_ids) == 1
    found = resume.plan(workspace, failed.id)
    assert node_of(found, "lead").side_effects == [TOOL] and found.needs_confirmation


async def test_a_call_that_started_before_a_crash_needs_confirmation(make: Make, tmp_path: Path) -> None:
    # The tool counts for a few seconds; no approvals, so the gate never stores the call on its own.
    workspace = make({"ACT": [tool_use(TOOL, {"note": "100000000"}), text("done")]})
    flow = acting(workspace, "Flow")
    started = await workspace.run(flow, RunIn(input="go"))
    deadline = time.monotonic() + 20
    while not workspace.runs.get(started.id).nodes["act"].tool_calls:  # type: ignore[union-attr]
        assert time.monotonic() < deadline, "the tool was never called"
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.5)  # the call is executing now (it counts for a few seconds)
    # What a process that dies right now leaves behind: the data directory with the database as it is.
    crashed = tmp_path / "crashed"
    shutil.copytree(workspace.data_dir, crashed, ignore=shutil.ignore_patterns(f"{DATABASE}*", LOCK_FILE))
    assert isinstance(workspace.store, SQLiteStore)
    workspace.store.snapshot(crashed / DATABASE)
    assert workspace.runs.get(started.id).nodes["act"].tool_calls[0].finished_at is None  # type: ignore[union-attr]
    done = await workspace.wait(started.id, timeout=30)
    assert done.status == "succeeded", done.error
    # After the restart the run is interrupted; its stored node shows no call of its own.
    restarted = make({}, crashed)
    interrupted = restarted.runs.get(started.id)
    assert interrupted is not None and interrupted.status == "failed"
    found = resume.plan(restarted, started.id)
    assert node_of(found, "act").side_effects == [TOOL] and found.needs_confirmation


async def test_the_notes_of_started_calls_go_once_the_node_is_stored(make: Make) -> None:
    from nodestro_runtime import tool_journal

    workspace = make({"ACT": [tool_use(TOOL, {"note": "x"}), text("done")]})
    done = await finished(workspace, acting(workspace, "Flow"))
    assert done.status == "succeeded", done.error
    assert tool_journal.started(workspace.store, done.id) == []
    # Nothing to confirm for a finished run, and a deleted run leaves no notes.
    tool_journal.record(workspace.store, done.id, "act", [], "toolu_x", TOOL, {"note": "x"})
    workspace.runs.discard_tool_waits(done.id, because="cancelled")
    assert len(tool_journal.started(workspace.store, done.id)) == 1
    workspace.store.delete("runs", done.id)
    workspace.runs.discard_tool_waits(done.id, because="run.deleted")
    assert tool_journal.started(workspace.store, done.id) == []
