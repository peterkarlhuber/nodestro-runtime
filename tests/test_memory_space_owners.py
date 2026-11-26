"""Memory spaces with an owner (RT-089c): on a server a space is its owner's alone.

A space belongs to its first writer: the member who writes its first file, or the person who started the run whose
step first opens it. Only the owner's runs and agent tests use it; another member's run, a run that acts for no
person (a schedule, an API key) and a run that names nobody get ``memory.space_not_found``, as for a space that does
not exist. A space that already had files before owners were recorded has no owner and fails closed until the
operator assigns one (never by itself, nothing is deleted). The desktop's single person keeps every space.

The workspaces here are desktop workspaces whose memory spaces are given the owner store a server tenant gets
(``Workspace`` passes ``owners=storage.store`` for server storage), so the rules run without PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import contextvars
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime.agent_test import AgentTestIn
from nodestro_runtime.memory_spaces import NOBODY, OWNERS, MemorySpaces, SpaceOwner
from nodestro_runtime.preview import build_preview
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import AgentConfig, MemoryConfig, Run, RunIn, Workflow
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.workspace import Workspace

ALICE, BOB = "usr_alice", "usr_bob"
SECRET = "salary zanzibar"
NOTE = tool_use("memory", {"command": "create", "path": "/memories/note.md", "file_text": SECRET})
VIEW = tool_use("memory", {"command": "view", "path": "/memories/note.md"})
Make = Callable[..., Workspace]


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]], *, owners: bool = True) -> Workspace:
        client = ScriptedClient(script)
        workspace = Workspace(tmp_path / f"data{len(opened)}", client_factory=lambda _p: client, integrations=[])
        if owners:
            workspace.memory.owners = workspace.store  # what a server tenant's workspace gets
        workspace.client = client  # type: ignore[attr-defined]
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def _agent(prompt: str = "MEM", space: str = "team") -> AgentConfig:
    return AgentConfig(
        name="Mem",
        system_prompt=prompt,
        model="anthropic:claude-sonnet-5-5",
        memory=MemoryConfig(mode="persistent", space=space),
    )


def _flow(agent: AgentConfig) -> Workflow:
    return Workflow.model_validate(
        {
            "id": "wf_mem",
            "name": "Memory",
            "nodes": [{"id": "start", "type": "start"}, {"id": "a", "type": "agent", "agent": agent.model_dump()}],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )


async def _run(workspace: Workspace, started_by: str | None, **start: Any) -> Run:
    run = workspace.runs.start(_flow(_agent()), RunIn(input="go"), started_by=started_by, **start)
    return await workspace.wait(run.id, timeout=30)


def allow(workspace: Workspace, owner: str = ALICE, space: str = "team", flow: Workflow | None = None) -> None:
    """The owner's explicit consent (RT-089d) for the workflow (default :func:`_flow`) to use their space."""
    flow = flow or _flow(_agent(space=space))
    digest = workspace.runs.consent_digest(workspace.runs.pin_subworkflows(flow))
    workspace.memory.grant(space, owner, "workflow", digest, workflow_id=flow.id)


def _sent(workspace: Workspace) -> str:
    return str(workspace.client.messages.calls)  # type: ignore[attr-defined]


async def test_poc_another_member_cannot_read_a_space(make: Make) -> None:
    """PoC (RT-089c): Alice's run writes a note into the space ``team``; Bob's run of the same workflow asks its
    memory for the note. Before, Bob's agent read it (every member used every space); now his step fails before the
    agent starts, and the note never reaches his model."""
    workspace = make({"MEM": [NOTE, text("saved"), VIEW, text("read")]})
    allow(workspace)  # RT-089d: Alice allowed this workflow (it creates her space "team")
    first = await _run(workspace, ALICE)
    assert first.status == "succeeded", first.nodes["a"].error
    assert workspace.memory.owner("team") == ALICE
    calls = len(workspace.client.messages.calls)  # type: ignore[attr-defined]
    second = await _run(workspace, BOB)
    assert (second.nodes["a"].status, second.nodes["a"].error_code) == ("failed", "memory.space_not_found")
    assert len(workspace.client.messages.calls) == calls  # type: ignore[attr-defined]  # Bob's model got nothing
    # Bob sees no trace of the space: not listed, not usable, the same answer as for a space that does not exist.
    assert [item.name for item in workspace.memory.visible(BOB)] == []
    assert [item.name for item in workspace.memory.visible(ALICE)] == ["team"]
    with pytest.raises(NodestroError) as missing:
        workspace.memory.open_for("team", BOB)
    with pytest.raises(NodestroError) as unknown:
        workspace.memory.open_for("other", NOBODY)
    assert missing.value.code == unknown.value.code == "memory.space_not_found"


async def test_runs_of_nobody_use_no_space(make: Make) -> None:
    """A run that acts for no person (a schedule without a starter, an API key) uses no space and creates none."""
    workspace = make({"MEM": [NOTE, text("saved")]})
    run = await _run(workspace, None, trigger="schedule", trigger_ref="sch_1")
    assert run.nodes["a"].error_code == "memory.space_not_found"
    keyed = await _run(workspace, "api:key_1", trigger="api")
    assert keyed.nodes["a"].error_code == "memory.space_not_found"
    assert workspace.memory.owner("team") is None and workspace.client.messages.calls == []  # type: ignore[attr-defined]


def test_an_agent_test_reads_only_its_testers_space(make: Make) -> None:
    workspace = make({"MEM": [VIEW, text("read")]})
    space = workspace.memory.open_for("team", ALICE)
    space.write("/memories/note.md", SECRET)

    def test_as(actor: str) -> Any:
        result, _ = contextvars.copy_context().run(
            workspace.tester.run, AgentTestIn(agent=_agent(), input="go"), actor=actor
        )
        return result

    refused = test_as(BOB)
    assert (refused.status, refused.error_code) == ("failed", "memory.space_not_found")
    assert workspace.client.messages.calls == []  # type: ignore[attr-defined]
    # RT-089d: Alice allowed this agent
    workspace.memory.grant("team", ALICE, "agent", workspace.runs.agent_consent_digest(_agent()))
    allowed = test_as(ALICE)
    assert allowed.status == "succeeded", allowed.error
    assert SECRET in _sent(workspace)


async def test_a_space_without_an_owner_fails_closed_until_the_operator_assigns_one(make: Make) -> None:
    """A space with files from before owners were recorded is nobody's: no run uses it, nothing claims it by
    itself; the operator's assignment (audited by the caller) makes it its owner's, and an owner is never replaced."""
    workspace = make({"MEM": [VIEW, text("read")]})
    MemorySpaces.open(workspace.memory, "team").write("/memories/note.md", SECRET)  # written before owners
    for person in (ALICE, BOB):
        run = await _run(workspace, person)
        assert run.nodes["a"].error_code == "memory.space_not_found"
    assert workspace.memory.owner("team") is None and [item.name for item in workspace.memory.ownerless()] == ["team"]
    assert workspace.client.messages.calls == []  # type: ignore[attr-defined]
    assert workspace.memory.assign("team", ALICE)
    assert not workspace.memory.assign("team", BOB)  # an owner is never replaced
    stored = workspace.store.get(OWNERS, "team", SpaceOwner)
    assert stored is not None and (stored.owner, stored.how) == (ALICE, "assigned")
    allow(workspace)  # RT-089d: the new owner allows the workflow
    run = await _run(workspace, ALICE)
    assert run.status == "succeeded", run.nodes["a"].error
    assert SECRET in _sent(workspace)
    assert workspace.memory.ownerless() == []
    with pytest.raises(ValueError):
        workspace.memory.assign("other", NOBODY)


def test_a_member_writes_a_new_space_and_owns_it(make: Make) -> None:
    workspace = make({})
    workspace.memory.open_for("notes", BOB).write("/memories/a.md", "b")
    assert workspace.memory.owner("notes") == BOB
    with pytest.raises(NodestroError):
        workspace.memory.open_for("notes", ALICE)
    # A principal without an owner identity creates nothing.
    with pytest.raises(NodestroError):
        workspace.memory.open_for("fresh", NOBODY)
    assert workspace.memory.owner("fresh") is None


def test_the_context_preview_counts_only_the_owners_files(make: Make) -> None:
    workspace = make({})
    workspace.memory.open_for("team", ALICE).write("/memories/note.md", SECRET)
    flow = _flow(_agent())
    assert build_preview(workspace, flow, "a", given_input="x", run=None, owner=ALICE).memory_files == 1
    assert build_preview(workspace, flow, "a", given_input="x", run=None, owner=BOB).memory_files == 0


async def test_the_desktop_keeps_every_space(make: Make) -> None:
    """Without owners (the desktop's single person) nothing changes: every run uses every space, nobody owns it."""
    workspace = make({"MEM": [NOTE, text("saved"), VIEW, text("read")]}, owners=False)
    assert (await _run(workspace, "local")).status == "succeeded"
    assert (await _run(workspace, None)).status == "succeeded"
    assert SECRET in _sent(workspace)
    assert workspace.memory.owner("team") is None
    assert [item.name for item in workspace.memory.visible(BOB)] == ["team"]


# ---- review H-2: a run that used a person's memory space is that person's alone -----------------------------------


async def test_a_run_with_persistent_memory_is_seen_only_by_its_owner(make: Make) -> None:
    """Review H-2 (RT-089c): the memory tool's output is part of the run (steps, events, trace). Before, every member
    saw Alice's run and read her space through it; now the run is marked as hers before the step reads the space, and
    every run route (``run_visible``, ``summary_visible``) shows it to her alone: not to an administrator, not to a
    principal without an owner identity. The desktop's single person (``None``) still sees it."""
    from nodestro_runtime.conversations import NO_OWNER
    from nodestro_runtime.schemas import RunSummary

    workspace = make({"MEM": [NOTE, text("saved"), VIEW, text("read")]})
    events: list[Any] = []
    workspace.runs.event_sink = events.append
    allow(workspace)  # RT-089d
    first = await _run(workspace, ALICE)
    assert first.status == "succeeded"
    run = await _run(workspace, ALICE)
    assert run.memory_owner == ALICE and SECRET in run.model_dump_json()
    conversations = workspace.conversations
    for viewer, seen in ((ALICE, True), (None, True), (BOB, False), (NO_OWNER, False)):
        assert conversations.run_visible(run.id, viewer) is seen, viewer
        summary = workspace.store.get("runs", run.id, RunSummary)
        assert summary is not None and conversations.summary_visible(summary, viewer) is seen, viewer
    # The mark is stored before the space is read, and the stream is told (``memory_private``) before any event of
    # the step's memory tool, so an open stream of another member ends before it shows anything of the space.
    mine = [event for event in events if event.run_id == run.id]
    kinds = [event.type for event in mine]
    marked = kinds.index("memory_private")
    leaked = next(i for i, event in enumerate(mine) if SECRET in event.model_dump_json())
    assert marked < leaked, kinds
    # A run below it (a sub-workflow's) is seen by nobody else either: the walk checks every run up to the top.
    child = run.model_copy(update={"id": "run_child", "parent_run_id": run.id, "memory_owner": None})
    workspace.store.put("runs", child.id, child)
    assert conversations.run_visible(child.id, BOB) is False
    assert conversations.run_visible(child.id, ALICE) is True


async def test_a_run_without_persistent_memory_stays_visible_and_unmarked(make: Make) -> None:
    from nodestro_runtime.schemas import MemoryConfig as Memory

    workspace = make({"MEM": [text("plain")]})
    agent = _agent().model_copy(update={"memory": Memory()})
    run = await workspace.wait(workspace.runs.start(_flow(agent), RunIn(input="go"), started_by=ALICE).id, timeout=30)
    assert run.status == "succeeded" and run.memory_owner is None
    assert "memory_owner" not in workspace.store.get_raw("runs", run.id)  # stored records stay as they were
    assert workspace.conversations.run_visible(run.id, BOB) is True


async def test_the_desktop_marks_no_run(make: Make) -> None:
    workspace = make({"MEM": [NOTE, text("saved")]}, owners=False)
    run = await _run(workspace, "local")
    assert run.status == "succeeded" and run.memory_owner is None
