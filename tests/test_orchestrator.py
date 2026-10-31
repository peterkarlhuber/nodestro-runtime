"""Orchestrator nodes (Adaptive Orchestration M4): an AI model delegates to teams of its pool."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime.engine import check_workflow
from nodestro_runtime.schemas import RunIn, Workflow, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, parsed, text
from nodestro_runtime.workspace import Workspace

Make = Callable[[dict[str, list[Any]]], Workspace]
BOSS = "You coordinate teams"


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]]) -> Workspace:
        fake = ScriptedClient(script)
        workspace = Workspace(tmp_path / f"data{len(opened)}", client_factory=lambda _provider: fake, integrations=[])
        workspace.fake = fake  # type: ignore[attr-defined]
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def decision(**values: Any) -> Any:
    return parsed(lambda model: model(**values))


def edge(source: str, target: str) -> dict[str, Any]:
    return {"id": f"{source}-{target}", "source": source, "target": target}


def team(workspace: Workspace, prompt: str, name: str, **extra: Any) -> Workflow:
    nodes = [
        {"id": "s", "type": "start"},
        {"id": "w", "type": "agent", "label": name, "agent": {"name": name, "system_prompt": prompt}},
        *extra.pop("nodes", []),
    ]
    edges = [edge("s", "w"), *extra.pop("edges", [])]
    return workspace.save_workflow(
        WorkflowIn.model_validate(
            {"name": name, "description": f"{name} team", "nodes": nodes, "edges": edges, **extra}
        )
    )


def boss(*teams: Workflow, **config: Any) -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Boss",
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


async def run(workspace: Workspace, flow: WorkflowIn) -> Any:
    saved = workspace.save_workflow(flow)
    started = await workspace.run(saved, RunIn(input="Write about otters"))
    return await workspace.wait(started.id, timeout=20)


async def test_the_orchestrator_delegates_to_a_team_and_finishes(make: Make) -> None:
    ids: dict[str, str] = {}
    workspace = make(
        {
            BOSS: [
                parsed(
                    lambda model: model(
                        action="delegate", member=ids["research"], task="Find otter facts", reason="facts first"
                    )
                ),
                decision(action="finish", answer="Otters hold hands while sleeping.", reason="enough"),
            ],
            "RESEARCH": [text("Otters hold hands.")],
        }
    )
    research = team(workspace, "RESEARCH", "Research", capabilities=["research"])
    ids["research"] = research.id
    done = await run(workspace, boss(research))
    assert done.status == "succeeded", done.error
    lead = done.nodes["lead"]
    assert lead.output == "Otters hold hands while sleeping." and done.output == lead.output
    steps = lead.orchestration["steps"]
    assert [step["decision"]["action"] for step in steps] == ["delegate", "finish"]
    assert steps[0]["result"] == "Otters hold hands." and steps[0]["decision"]["reason"] == "facts first"
    assert len(lead.child_run_ids) == 1
    child = workspace.runs.get(lead.child_run_ids[0])
    assert (
        child.parent_run_id == done.id and child.parent_node_id == "lead" and child.input.startswith("Find otter facts")
    )
    # The pool was shown to the model with the team's capabilities; the orchestrator has no tools.
    first = next(call for call in workspace.fake.messages.calls if BOSS in str(call.get("system")))
    assert "capabilities: research" in str(first["messages"]) and not first.get("tools")
    assert lead.usage.input_tokens > 0


async def test_refused_decisions_and_the_decision_limit(make: Make) -> None:
    ids: dict[str, str] = {}
    workspace = make(
        {
            BOSS: [
                decision(action="delegate", member="wf_unknown", task="x"),
                decision(action="create", answer="a new agent"),
                parsed(lambda model: model(action="delegate", member=ids["t"], task="go")),
            ],
            "TEAM": [text("done")],
        }
    )
    member = team(workspace, "TEAM", "Team")
    ids["t"] = member.id
    done = await run(workspace, boss(member, max_decisions=3))
    assert done.status == "failed"
    lead = done.nodes["lead"]
    assert lead.error_code == "run.orchestration_limit" and lead.error_params["max_decisions"] == 3
    codes = [step["verdict"]["code"] for step in lead.orchestration["steps"]]
    assert codes == ["orchestration.unknown_member", "orchestration.invalid_decision", None]


async def test_a_failing_team_is_shown_to_the_orchestrator(make: Make) -> None:
    ids: dict[str, str] = {}

    def broken(request: dict[str, Any]) -> Any:
        raise RuntimeError("provider down")

    workspace = make(
        {
            BOSS: [
                parsed(lambda model: model(action="delegate", member=ids["t"], task="go")),
                decision(action="finish", answer="The team failed; no answer.", reason="team down"),
            ],
            "TEAM": [broken],
        }
    )
    member = team(workspace, "TEAM", "Team")
    ids["t"] = member.id
    done = await run(workspace, boss(member))
    assert done.status == "succeeded", done.error
    step = done.nodes["lead"].orchestration["steps"][0]
    assert step["error"].startswith("RuntimeError: Team failed")


async def test_a_team_that_waits_for_an_approval_is_cancelled(make: Make) -> None:
    ids: dict[str, str] = {}
    workspace = make(
        {
            BOSS: [
                parsed(lambda model: model(action="delegate", member=ids["t"], task="go")),
                decision(action="finish", answer="stopped", reason="needs a person"),
            ],
            "TEAM": [text("draft")],
        }
    )
    member = team(
        workspace,
        "TEAM",
        "Team",
        nodes=[{"id": "ok", "type": "approval", "label": "Check", "approval": {"instructions": "ok?"}}],
        edges=[edge("w", "ok")],
    )
    ids["t"] = member.id
    done = await run(workspace, boss(member))
    lead = done.nodes["lead"]
    assert "approval step" in lead.orchestration["steps"][0]["error"]
    assert workspace.runs.get(lead.child_run_ids[0]).status == "cancelled"


def test_checks_of_an_orchestrator() -> None:
    def codes(**config: Any) -> list[str]:
        flow = Workflow.model_validate(
            {
                "id": "wf_self",
                "nodes": [{"id": "lead", "type": "orchestrator", "orchestrator": config}],
            }
        )
        return sorted(issue.code for issue in check_workflow(flow) if issue.code.startswith("workflow.orchestrator"))

    assert codes() == ["workflow.orchestrator_no_members"]
    assert codes(members=[{"workflow_id": "wf_a"}, {"workflow_id": "wf_a"}]) == [
        "workflow.orchestrator_member_duplicate"
    ]
    assert codes(members=[{"workflow_id": "wf_self"}]) == ["workflow.orchestrator_self"]
    assert codes(members=[{"workflow_id": "wf_a"}], max_decisions=2, max_per_member=3) == [
        "workflow.orchestrator_limits"
    ]
    assert codes(members=[{"workflow_id": "wf_a"}]) == []


def test_preflight_checks_the_teams_and_runs_pin_their_versions(make: Make) -> None:
    from nodestro_runtime.preflight import preflight

    workspace = make({})
    good = team(workspace, "TEAM", "Good")
    pausing = team(
        workspace,
        "TEAM",
        "Pausing",
        nodes=[{"id": "ok", "type": "approval", "label": "Check", "approval": {"instructions": "ok?"}}],
        edges=[edge("w", "ok")],
    )
    flow = boss(good, pausing)
    flow.nodes[1].orchestrator.members.append(  # type: ignore[union-attr]
        type(flow.nodes[1].orchestrator.members[0])(workflow_id="wf_gone")  # type: ignore[union-attr]
    )
    saved = workspace.save_workflow(flow)
    codes = sorted(issue.code for issue in preflight(workspace, saved).problems if "orchestrator" in issue.code)
    assert codes == ["check.orchestrator_member_approval", "check.orchestrator_member_missing"]

    # A team that calls the boss again is a cycle.
    caller = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Caller",
                "nodes": [
                    {"id": "s", "type": "start"},
                    {"id": "sub", "type": "subworkflow", "subworkflow": {"workflow_id": saved.id}},
                ],
                "edges": [edge("s", "sub")],
            }
        )
    )
    looping = workspace.save_workflow(Workflow(**{**boss(caller).model_dump(), "id": saved.id}))
    codes = [issue.code for issue in preflight(workspace, looping).problems]
    assert "check.orchestrator_cycle" in codes

    pinned = workspace.runs.pin_subworkflows(saved)
    assert [member.version for member in pinned.nodes[1].orchestrator.members] == [1, 1, None]  # type: ignore[union-attr]


async def test_costs_count_for_the_orchestrator_model_and_the_teams(make: Make) -> None:
    from nodestro_runtime.run_views import models_of_run

    ids: dict[str, str] = {}
    workspace = make(
        {
            BOSS: [
                parsed(lambda model: model(action="delegate", member=ids["t"], task="go")),
                decision(action="finish", answer="done", reason="ok"),
            ],
            "TEAM": [text("result")],
        }
    )
    member = team(workspace, "TEAM", "Team")
    ids["t"] = member.id
    flow = boss(member, model="anthropic:boss-model")
    done = await run(workspace, flow)
    assert done.status == "succeeded", done.error
    lead = done.nodes["lead"]
    assert lead.decision_usage is not None and lead.decision_usage.requests == 2
    assert lead.usage.requests == 3
    models = models_of_run(workspace, done)
    assert models["anthropic:boss-model"].requests == 2
    assert sum(usage.requests for usage in models.values()) == 3


async def test_capability_selection_adds_matching_teams_to_the_pool(make: Make) -> None:
    from nodestro_runtime.preflight import preflight

    ids: dict[str, str] = {}
    workspace = make(
        {
            BOSS: [
                parsed(lambda model: model(action="delegate", member=ids["best"], task="go")),
                decision(action="finish", answer="done", reason="ok"),
            ],
            "BEST": [text("best result")],
        }
    )
    best = team(workspace, "BEST", "Best", capabilities=["Research", "summaries"])
    ids["best"] = best.id
    other = team(workspace, "OTHER", "Other", capabilities=["research"])
    team(workspace, "NONE", "Unrelated", capabilities=["billing"])
    team(
        workspace,
        "PAUSE",
        "Pausing",
        capabilities=["research"],
        nodes=[{"id": "ok", "type": "approval", "label": "Check", "approval": {"instructions": "ok?"}}],
        edges=[edge("w", "ok")],
    )
    flow = boss(select={"capabilities": ["research", "Summaries"], "max_teams": 5})
    saved = workspace.save_workflow(flow)
    assert not [issue for issue in check_workflow(saved) if issue.level == "error"]

    pinned = workspace.runs.pin_subworkflows(saved)
    members = pinned.nodes[1].orchestrator.members  # type: ignore[union-attr]
    # Two matching tags first, then one; no approval team, no unrelated team; versions pinned.
    assert [(member.workflow_id, member.matched) for member in members] == [
        (best.id, ["research", "Summaries"]),
        (other.id, ["research"]),
    ]
    assert all(member.version == 1 for member in members)
    problems = preflight(workspace, saved).problems
    selected = next(issue for issue in problems if issue.code == "check.orchestrator_selected")
    assert selected.params["teams"] == "Best, Other"

    done = await run(workspace, flow)
    assert done.status == "succeeded", done.error
    assert done.workflow.nodes[1].orchestrator.members[0].matched == ["research", "Summaries"]  # type: ignore[union-attr]

    # No match: the check stops the start with its own code.
    lonely = workspace.save_workflow(boss(select={"capabilities": ["astronomy"]}))
    codes = [issue.code for issue in preflight(workspace, lonely).problems if issue.level == "error"]
    assert "check.orchestrator_no_match" in codes


def test_select_teams_respects_the_pool_size() -> None:
    from nodestro_runtime.orchestrator import select_teams
    from nodestro_runtime.schemas import OrchestratorConfig

    config = OrchestratorConfig.model_validate(
        {"members": [{"workflow_id": f"wf_{n}"} for n in range(18)], "select": {"capabilities": ["x"], "max_teams": 5}}
    )
    candidates = [
        (Workflow.model_validate({"id": f"wf_c{n}", "name": f"C{n}", "capabilities": ["x"]}), 1) for n in range(5)
    ]
    chosen = select_teams(config, None, candidates, lambda _workflow: True)
    assert [member.workflow_id for member in chosen] == ["wf_c0", "wf_c1"]
