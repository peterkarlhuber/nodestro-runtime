"""Orchestrators with dynamic teams and approvals inside teams (Adaptive Orchestration M6b, orkexa ADR 0011).

Scenarios: an approval step in a team (B), in a sub-team of a team (C), in a team of a nested orchestrator (D),
rejection (E), a restart while waiting (F), cancel while waiting; teams assembled at run time, parallel teams,
team parameters (AO-5 level 2).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime.run_views import approvals_of_tree
from nodestro_runtime.schemas import Run, RunIn, Workflow, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, parsed, text
from nodestro_runtime.workspace import Workspace

BOSS = "You coordinate teams"
Make = Callable[..., Workspace]


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]], *, data: str = "data") -> Workspace:
        fake = ScriptedClient(script)
        workspace = Workspace(tmp_path / data, client_factory=lambda _provider: fake, integrations=[])
        workspace.fake = fake  # type: ignore[attr-defined]
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def decide(**values: Any) -> Any:
    return parsed(lambda model: model(**values))


def edge(source: str, target: str, handle: str | None = None) -> dict[str, Any]:
    item = {"id": f"{source}-{target}", "source": source, "target": target}
    if handle:
        item["source_handle"] = handle
    return item


def team(
    workspace: Workspace,
    prompt: str,
    name: str,
    *,
    approval: str | None = None,
    fields: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> Workflow:
    """A team: start (with form ``fields``) -> agent [-> approval]."""
    nodes = [
        {"id": "s", "type": "start", **({"start": {"fields": fields}} if fields else {})},
        {"id": "w", "type": "agent", "label": name, "agent": {"name": name, "system_prompt": prompt}},
        *extra.pop("nodes", []),
    ]
    edges = [edge("s", "w"), *extra.pop("edges", [])]
    if approval is not None:
        nodes.append({"id": "ok", "type": "approval", "label": "Check", "approval": {"on_reject": approval}})
        edges.append(edge("w", "ok"))
    return workspace.save_workflow(
        WorkflowIn.model_validate(
            {"name": name, "description": f"{name} team", "nodes": nodes, "edges": edges, **extra}
        )
    )


def boss(*teams: Workflow, name: str = "Boss", **config: Any) -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": name,
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "lead",
                    "type": "orchestrator",
                    "label": "Lead",
                    "orchestrator": {"members": [{"workflow_id": item.id} for item in teams], **config},
                },
                {"id": "out", "type": "output"},
            ],
            "edges": [edge("start", "lead"), edge("lead", "out")],
        }
    )


async def start(workspace: Workspace, flow: WorkflowIn) -> Run:
    saved = workspace.save_workflow(flow)
    started = await workspace.run(saved, RunIn(input="Write about otters"))
    return await workspace.wait(started.id, timeout=20)


def boss_calls(workspace: Workspace) -> int:
    return sum(BOSS in str(call.get("system")) for call in workspace.fake.messages.calls)  # type: ignore[attr-defined]


def waiting_run(workspace: Workspace, root: Run) -> tuple[str, str]:
    """The run and node of the one approval waiting somewhere below ``root``."""
    found = approvals_of_tree(workspace.runs, root)
    assert len(found) == 1, found
    return found[0].run_id, found[0].node_id


# B: an approval step in a team ------------------------------------------------------------------------------


async def test_a_team_waiting_for_an_approval_pauses_the_orchestrator_and_continues_after_it(make: Make) -> None:
    ids: dict[str, str] = {}
    workspace = make(
        {
            BOSS: [
                parsed(lambda model: model(action="delegate", member=ids["t"], task="Draft", reason="drafts")),
                decide(action="finish", answer="Approved draft", reason="done"),
            ],
            "TEAM": [text("draft")],
        }
    )
    member = team(workspace, "TEAM", "Team", approval="stop")
    ids["t"] = member.id
    paused = await start(workspace, boss(member))
    assert paused.status == "paused", paused.error
    lead = paused.nodes["lead"]
    assert lead.status == "awaiting_approval" and lead.finished_at is None
    began = lead.started_at
    record = lead.orchestration
    assert record["pending"]["outcomes"][0]["status"] == "waiting" and not record["steps"]
    child_id = lead.child_run_ids[0]
    assert record["pending"]["outcomes"][0]["ref"] == child_id
    assert workspace.runs.get(child_id).status == "paused"  # type: ignore[union-attr]
    assert waiting_run(workspace, paused) == (child_id, "ok")
    assert boss_calls(workspace) == 1

    workspace.decide(child_id, "ok", "approved", "")
    done = await workspace.wait(paused.id, timeout=20)
    assert done.status == "succeeded", done.error
    lead = done.nodes["lead"]
    assert lead.output == "Approved draft" and lead.child_run_ids == [child_id]  # the same team run continued
    assert lead.started_at == began  # X-02: continuing after the decision keeps when the step began
    step = lead.orchestration["steps"][0]
    assert step["outcomes"][0]["status"] == "succeeded" and step["outcomes"][0]["ref"] == child_id
    assert lead.orchestration["pending"] is None
    assert boss_calls(workspace) == 2  # the delegation was not asked again
    assert workspace.runs.get(child_id).status == "succeeded"  # type: ignore[union-attr]
    assert len(workspace.fake.messages.calls) == 3  # type: ignore[attr-defined]  # the team's agent ran once


# E: rejection ----------------------------------------------------------------------------------------------


async def test_a_rejection_that_stops_the_team_stops_the_orchestrated_run(make: Make) -> None:
    ids: dict[str, str] = {}
    workspace = make(
        {
            BOSS: [parsed(lambda model: model(action="delegate", member=ids["t"], task="Pay"))],
            "TEAM": [text("payment prepared")],
        }
    )
    member = team(workspace, "TEAM", "Payer", approval="stop")
    ids["t"] = member.id
    paused = await start(workspace, boss(member))
    child_id = paused.nodes["lead"].child_run_ids[0]
    workspace.decide(child_id, "ok", "rejected", "no")
    done = await workspace.wait(paused.id, timeout=20)
    # The run ends like a sub-workflow's rejection: no other team can get around it, nothing is asked again.
    assert done.status == "cancelled" and done.error_code == "run.approval_rejected", (done.status, done.error)
    lead = done.nodes["lead"]
    assert lead.status == "cancelled"
    assert lead.orchestration["steps"][0]["outcomes"][0]["status"] == "rejected"
    assert boss_calls(workspace) == 1


async def test_a_rejection_the_team_handles_itself_is_a_normal_result(make: Make) -> None:
    ids: dict[str, str] = {}
    workspace = make(
        {
            BOSS: [
                parsed(lambda model: model(action="delegate", member=ids["t"], task="Draft")),
                decide(action="finish", answer="Rejected; nothing sent", reason="person said no"),
            ],
            "TEAM": [text("draft")],
        }
    )
    member = team(workspace, "TEAM", "Team", approval="branch")
    ids["t"] = member.id
    paused = await start(workspace, boss(member))
    workspace.decide(paused.nodes["lead"].child_run_ids[0], "ok", "rejected", "")
    done = await workspace.wait(paused.id, timeout=20)
    assert done.status == "succeeded", done.error
    assert done.nodes["lead"].orchestration["steps"][0]["outcomes"][0]["status"] == "succeeded"


# C: an approval in a sub-team of a team ---------------------------------------------------------------------


async def test_an_approval_in_a_sub_workflow_of_a_team(make: Make) -> None:
    ids: dict[str, str] = {}
    workspace = make(
        {
            BOSS: [
                parsed(lambda model: model(action="delegate", member=ids["t"], task="Go")),
                decide(action="finish", answer="done"),
            ],
            "INNER": [text("inner draft")],
        }
    )
    inner = team(workspace, "INNER", "Inner", approval="stop")
    outer = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Outer",
                "nodes": [
                    {"id": "s", "type": "start"},
                    {"id": "sub", "type": "subworkflow", "subworkflow": {"workflow_id": inner.id}},
                ],
                "edges": [edge("s", "sub")],
            }
        )
    )
    ids["t"] = outer.id
    paused = await start(workspace, boss(outer))
    assert paused.status == "paused", paused.error
    run_id, node_id = waiting_run(workspace, paused)
    assert node_id == "ok" and workspace.runs.get(run_id).workflow_id == inner.id  # type: ignore[union-attr]
    workspace.decide(run_id, node_id, "approved", "")
    done = await workspace.wait(paused.id, timeout=20)
    assert done.status == "succeeded", done.error
    assert boss_calls(workspace) == 2


# D: nested orchestrators ------------------------------------------------------------------------------------


async def test_an_approval_below_a_nested_orchestrator(make: Make) -> None:
    ids: dict[str, str] = {}
    workspace = make(
        {
            BOSS: [
                parsed(lambda model: model(action="delegate", member=ids["next"], task="Go")),
                parsed(lambda model: model(action="delegate", member=ids["next2"], task="Go deeper")),
                decide(action="finish", answer="inner answer"),
                decide(action="finish", answer="outer answer"),
            ],
            "LEAF": [text("leaf draft")],
        }
    )
    leaf = team(workspace, "LEAF", "Leaf", approval="stop")
    middle = workspace.save_workflow(boss(leaf, name="Middle"))
    ids["next"] = middle.id
    ids["next2"] = leaf.id
    paused = await start(workspace, boss(middle))
    assert paused.status == "paused", paused.error
    middle_run = workspace.runs.get(paused.nodes["lead"].child_run_ids[0])
    assert middle_run is not None and middle_run.status == "paused"
    assert middle_run.nodes["lead"].status == "awaiting_approval"
    run_id, node_id = waiting_run(workspace, paused)
    assert workspace.runs.get(run_id).workflow_id == leaf.id  # type: ignore[union-attr]
    workspace.decide(run_id, node_id, "approved", "")
    done = await workspace.wait(paused.id, timeout=20)
    assert done.status == "succeeded", done.error
    assert done.output == "outer answer"
    assert boss_calls(workspace) == 4  # two delegations, two finishes: nothing asked twice


# F: restart while waiting -----------------------------------------------------------------------------------


async def test_the_decision_after_a_restart_continues_the_waiting_team(make: Make) -> None:
    ids: dict[str, str] = {}
    script = {
        BOSS: [
            parsed(lambda model: model(action="delegate", member=ids["t"], task="Draft")),
            decide(action="finish", answer="after restart"),
        ],
        "TEAM": [text("draft")],
    }
    first = make(script)
    member = team(first, "TEAM", "Team", approval="stop")
    ids["t"] = member.id
    paused = await start(first, boss(member))
    child_id = paused.nodes["lead"].child_run_ids[0]
    first.close()

    second = make({BOSS: [decide(action="finish", answer="after restart")]})  # a new process, same data
    second.decide(child_id, "ok", "approved", "")
    done = await second.wait(paused.id, timeout=20)
    assert done.status == "succeeded", done.error
    assert done.output == "after restart"
    assert done.nodes["lead"].child_run_ids == [child_id]
    assert boss_calls(second) == 1  # only the finish; the delegation came from the stored record
    assert not second.fake.messages.calls[0].get("tools")  # type: ignore[attr-defined]


async def test_cancelling_a_waiting_orchestrator_cancels_its_waiting_team(make: Make) -> None:
    ids: dict[str, str] = {}
    workspace = make(
        {BOSS: [parsed(lambda model: model(action="delegate", member=ids["t"], task="Draft"))], "TEAM": [text("d")]}
    )
    member = team(workspace, "TEAM", "Team", approval="stop")
    ids["t"] = member.id
    paused = await start(workspace, boss(member))
    child_id = paused.nodes["lead"].child_run_ids[0]
    assert workspace.runs.cancel(paused.id)
    stored = workspace.runs.get(paused.id)
    assert stored is not None and stored.status == "cancelled"
    assert workspace.runs.get(child_id).status == "cancelled"  # type: ignore[union-attr]
    record = stored.nodes["lead"].orchestration
    assert record["pending"] is None and record["stop_code"] == "run.cancelled"
    assert record["steps"][0]["outcomes"][0]["status"] == "cancelled"
    assert approvals_of_tree(workspace.runs, stored) == []


# Dynamic teams ---------------------------------------------------------------------------------------------


async def test_the_orchestrator_assembles_and_changes_its_team(make: Make) -> None:
    ids: dict[str, str] = {}
    workspace = make(
        {
            BOSS: [
                parsed(lambda model: model(action="delegate", member=ids["a"], task="x")),  # not in the team yet
                parsed(lambda model: model(action="staff", add=[ids["a"]], reason="research first")),
                parsed(lambda model: model(action="delegate", member=ids["a"], task="Find facts")),
                parsed(lambda model: model(action="staff", add=[ids["b"]], remove=[ids["a"]], reason="now write")),
                parsed(lambda model: model(action="delegate", member=ids["b"], task="Write")),
                decide(action="finish", answer="text"),
            ],
            "A": [text("facts")],
            "B": [text("text")],
        }
    )
    a, b = team(workspace, "A", "Research"), team(workspace, "B", "Writer")
    ids |= {"a": a.id, "b": b.id}
    done = await start(workspace, boss(a, b, staffing=True, max_team=1, max_decisions=8))
    assert done.status == "succeeded", done.error
    steps = done.nodes["lead"].orchestration["steps"]
    assert [step["verdict"]["code"] for step in steps] == ["orchestration.not_in_team", None, None, None, None, None]
    assert [step["decision"]["action"] for step in steps] == [
        "delegate",
        "staff",
        "delegate",
        "staff",
        "delegate",
        "finish",
    ]
    system = str(workspace.fake.messages.calls[0]["system"])  # type: ignore[attr-defined]
    assert '"staff"' in system and "Never try to get around a rejection" in system
    first_prompt = str(workspace.fake.messages.calls[0]["messages"])  # type: ignore[attr-defined]
    assert "Team now: nobody yet" in first_prompt


async def test_parallel_teams_with_one_waiting(make: Make) -> None:
    ids: dict[str, str] = {}
    workspace = make(
        {
            BOSS: [
                parsed(
                    lambda model: model(
                        action="parallel", members=[ids["a"], ids["b"]], tasks=["Facts", "Draft"], reason="independent"
                    )
                ),
                decide(action="finish", answer="both"),
            ],
            "A": [text("facts")],
            "B": [text("draft")],
        }
    )
    a = team(workspace, "A", "Research")
    b = team(workspace, "B", "Writer", approval="stop")
    ids |= {"a": a.id, "b": b.id}
    paused = await start(workspace, boss(a, b, max_parallel=2))
    assert paused.status == "paused", paused.error
    pending = paused.nodes["lead"].orchestration["pending"]
    assert [item["status"] for item in pending["outcomes"]] == ["succeeded", "waiting"]
    run_id, node_id = waiting_run(workspace, paused)
    workspace.decide(run_id, node_id, "approved", "")
    done = await workspace.wait(paused.id, timeout=20)
    assert done.status == "succeeded", done.error
    step = done.nodes["lead"].orchestration["steps"][0]
    assert [item["status"] for item in step["outcomes"]] == ["succeeded", "succeeded"]
    assert len(done.nodes["lead"].child_run_ids) == 2  # the finished team did not run again


async def test_team_parameters_are_checked_like_a_manual_start(make: Make) -> None:
    ids: dict[str, str] = {}
    workspace = make(
        {
            BOSS: [
                parsed(
                    lambda model: model(action="delegate", member=ids["t"], task="Write", values='{"tone": "rude"}')
                ),
                parsed(
                    lambda model: model(action="delegate", member=ids["t"], task="Write", values='{"tone": "formal"}')
                ),
                decide(action="finish", answer="ok"),
            ],
            "TEAM": [text("formal text")],
        }
    )
    member = team(
        workspace,
        "TEAM",
        "Writer",
        fields=[{"name": "tone", "type": "select", "options": ["formal", "casual"], "required": True}],
    )
    ids["t"] = member.id
    done = await start(workspace, boss(member, allow_values=True))
    assert done.status == "succeeded", done.error
    steps = done.nodes["lead"].orchestration["steps"]
    assert steps[0]["verdict"]["code"] == "orchestration.invalid_value"
    assert steps[1]["decision"]["values"] == {"tone": "formal"}
    child = workspace.runs.get(done.nodes["lead"].child_run_ids[0])
    assert child is not None and child.values == {"tone": "formal"}
    assert "parameter tone (one of formal, casual, required)" in str(
        workspace.fake.messages.calls[0]["messages"]  # type: ignore[attr-defined]
    )


async def test_without_allow_values_parameters_are_neither_offered_nor_used(make: Make) -> None:
    ids: dict[str, str] = {}
    workspace = make(
        {
            BOSS: [
                parsed(lambda model: model(action="delegate", member=ids["t"], task="Write")),
                decide(action="finish", answer="ok"),
            ],
            "TEAM": [text("text")],
        }
    )
    member = team(workspace, "TEAM", "Writer", fields=[{"name": "tone", "type": "text"}])
    ids["t"] = member.id
    done = await start(workspace, boss(member))
    assert done.status == "succeeded", done.error
    system = str(workspace.fake.messages.calls[0]["system"])  # type: ignore[attr-defined]
    assert "values" not in system and "parameter tone" not in str(workspace.fake.messages.calls[0]["messages"])  # type: ignore[attr-defined]
