"""Budget decisions of attended runs (BUDGET-1, :mod:`nodestro_runtime.run_budget`).

A request that the run's budget cannot admit is not sent: the run pauses with one budget request, a person ends the
run or raises exactly the limit that refused the request, and the run continues without repeating anything. Covered:
pausing before the first and before a later request, ending, raising, repeated raises, new runs with the regular
budget again, unchanged workflow and settings, parallel agents (one decision), sub-workflow limits (only the blocking
limit changes), quality judges and orchestrators (no thread waits), restarts, stale, repeated and idempotent
decisions, caps, deadlines, cancelling, unattended runs and price problems.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime import run_budget, tool_checkpoints
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.run_budget import BudgetRequests
from nodestro_runtime.schemas import Run, RunIn, SettingsIn, WorkflowIn, now
from nodestro_runtime.testing import ScriptedClient, parsed, text, tool_use
from nodestro_runtime.workspace import Workspace

Make = Callable[..., Workspace]


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []
    fakes: dict[str, ScriptedClient] = {}

    def factory(script: dict[str, list[Any]] | None = None, *, data: str = "data") -> Workspace:
        if data not in fakes:
            fakes[data] = ScriptedClient(script or {})
        fake = fakes[data]
        workspace = Workspace(tmp_path / data, client_factory=lambda _provider: fake, integrations=[])
        workspace.fake = fake  # type: ignore[attr-defined]
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def agent(node_id: str, prompt: str, **config: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "agent",
        "label": node_id.title(),
        "agent": {"name": node_id, "system_prompt": prompt, "max_tokens": 1000, **config},
    }


def edge(source: str, target: str) -> dict[str, str]:
    return {"id": f"{source}-{target}", "source": source, "target": target}


def flow(
    nodes: list[dict[str, Any]], edges: list[dict[str, str]], budget: float | None, name: str = "Budget"
) -> WorkflowIn:
    return WorkflowIn.model_validate(
        {"name": name, "nodes": [{"id": "start", "type": "start"}, *nodes], "edges": edges, "budget_usd": budget}
    )


def calls(workspace: Workspace) -> int:
    return len(workspace.fake.messages.calls)  # type: ignore[attr-defined]


async def start(workspace: Workspace, workflow: WorkflowIn, *, trigger: Any = "manual", by: str = "local") -> Run:
    saved = workspace.save_workflow(workflow)
    started = await workspace.run(saved, RunIn(input="x"), trigger=trigger, started_by=by)
    return await workspace.wait(started.id, timeout=20)


def pending(workspace: Workspace, run: Run) -> run_budget.BudgetRequest:
    found = [item for item in BudgetRequests(workspace.store).of_run(run.id) if item.status == "pending"]
    assert len(found) == 1, found
    return found[0]


async def decide(
    workspace: Workspace, run: Run, action: str = "raise", limit: str | None = "1.00", **kwargs: Any
) -> Run:
    request = pending(workspace, run)
    workspace.runs.decide_budget(run.id, request.id, action=action, by="local", limit_usd=limit, **kwargs)  # type: ignore[arg-type]
    return await workspace.wait(run.id, timeout=20)


def single(budget: float | None = 0.05) -> WorkflowIn:
    return flow([agent("a", "A")], [edge("start", "a")], budget)


# ---- the basic cycle ---------------------------------------------------------------------------------------------


async def test_a_run_within_its_budget_never_asks(make: Make) -> None:
    workspace = make({"A": [text("ok")]})
    done = await start(workspace, single(5.0))
    assert done.status == "succeeded", done.error
    assert done.budget is not None and done.budget.initial_limit_usd == Decimal("5.0")
    assert done.budget.raises == [] and done.budget.pending_request_id is None
    assert BudgetRequests(workspace.store).of_run(done.id) == []


async def test_a_request_that_could_exceed_the_budget_pauses_before_it_is_sent(make: Make) -> None:
    workspace = make({"A": [text("ok")]})
    paused = await start(workspace, single(0.01))
    assert paused.status == "paused"
    assert paused.nodes["a"].status == "awaiting_budget"
    assert calls(workspace) == 0 and paused.usage.requests == 0
    request = pending(workspace, paused)
    assert paused.budget is not None and paused.budget.pending_request_id == request.id
    assert request.reason == "request_too_large" and request.block.scope == "run"
    assert request.limit_usd == Decimal("0.01") and request.spent_usd == 0
    (blocked,) = request.requests
    assert blocked.refused and blocked.node_id == "a" and blocked.bounded
    assert blocked.agent == "a" and blocked.label == "A"  # the step as the workflow names it (NSL-13)
    assert blocked.max_cost_usd is not None and blocked.max_cost_usd > Decimal("0.01")
    # The suggestion admits exactly this request: what it can cost at most, rounded up to the cent.
    assert request.suggested_limit_usd == run_budget.ceil_cent(blocked.max_cost_usd)
    assert request.deadline is not None and request.deadline > now()
    stored = tool_checkpoints.load(workspace.store, paused.id, "a", [])
    assert stored is not None and stored.kind == "budget" and stored.turn is not None
    assert stored.turn["stage"] == "request"


async def test_ending_the_run_at_a_budget_decision(make: Make) -> None:
    workspace = make({"A": [text("ok")]})
    paused = await start(workspace, single(0.01))
    ended = await decide(workspace, paused, "end", None)
    assert ended.status == "cancelled" and ended.error_code == "run.budget_ended"
    assert calls(workspace) == 0
    (request,) = BudgetRequests(workspace.store).of_run(paused.id)
    assert request.status == "ended" and request.outcome == "ended" and request.decided_by == "local"
    assert tool_checkpoints.runs_with_checkpoints(workspace.store) == set()


async def test_raising_the_budget_continues_without_repeating_and_changes_only_this_run(make: Make) -> None:
    workspace = make({"A": [text("ok"), text("again")]})
    settings_before = workspace.settings().model_dump()
    paused = await start(workspace, single(0.01))
    workflow_before = workspace.store.get_raw("workflows", paused.workflow_id)
    done = await decide(workspace, paused, limit="1.00")
    assert done.status == "succeeded", done.error
    assert calls(workspace) == 1  # the request was sent once, after the decision
    assert done.budget is not None
    assert done.budget.initial_limit_usd == Decimal("0.01") and done.budget.limit_usd == Decimal("1.00")
    (raised,) = done.budget.raises
    assert raised.previous_limit_usd == Decimal("0.01") and raised.new_limit_usd == Decimal("1.00")
    assert done.budget_usd == 1.0 and done.budget.pending_request_id is None
    # The workflow, its versions and the settings are unchanged; the next run starts with the regular budget.
    assert workspace.store.get_raw("workflows", paused.workflow_id) == workflow_before
    assert workspace.settings().model_dump() == settings_before
    saved = workspace.store.get_raw("workflows", paused.workflow_id)
    assert saved["budget_usd"] == 0.01
    again = await workspace.wait(
        (await workspace.run(workspace.workflow(paused.workflow_id), RunIn(input="y"))).id, timeout=20
    )
    assert again.status == "paused" and again.budget is not None and again.budget.limit_usd == Decimal("0.01")
    kinds = [event.kind for event in reversed(run_budget.audit_events(workspace.store, run_id=paused.id))]
    assert kinds == ["requested", "raised"]


async def test_a_raise_that_is_still_too_small_asks_again_as_often_as_needed(make: Make) -> None:
    workspace = make({"A": [text("ok")]})
    # 4,000 output tokens can cost $0.06: neither $0.02 nor $0.03 admits the request.
    paused = await start(workspace, flow([agent("a", "A", max_tokens=4000)], [edge("start", "a")], 0.01))
    still = await decide(workspace, paused, limit="0.02")
    assert still.status == "paused" and still.nodes["a"].status == "awaiting_budget"
    second = pending(workspace, still)
    assert second.limit_usd == Decimal("0.02") and calls(workspace) == 0
    third = await decide(workspace, still, limit="0.03")
    assert third.status == "paused"
    done = await decide(workspace, third, limit="2.50")
    assert done.status == "succeeded", done.error
    assert done.budget is not None and [item.new_limit_usd for item in done.budget.raises] == [
        Decimal("0.02"),
        Decimal("0.03"),
        Decimal("2.50"),
    ]
    assert calls(workspace) == 1
    statuses = [item.status for item in BudgetRequests(workspace.store).of_run(paused.id)]
    assert statuses == ["raised", "raised", "raised"]


async def test_a_later_request_pauses_after_paid_work_and_nothing_runs_twice(make: Make) -> None:
    # The first request (at most ~$0.025) fits; after it was paid the second cannot be bounded within the rest.
    workspace = make({"A": [tool_use("calculator", {"expression": "1+1"}), text("two")]})
    first = flow([agent("a", "A", tools=["calculator"], max_tokens=300)], [edge("start", "a")], 0.028)
    paused = await start(workspace, first)
    assert paused.status == "paused", paused.error
    assert calls(workspace) == 1 and paused.usage.requests == 1
    assert [call.name for call in paused.nodes["a"].tool_calls] == ["calculator"]
    request = pending(workspace, paused)
    assert request.spent_usd > 0 and request.reason == "request_too_large"
    done = await decide(workspace, paused, limit="1.00")
    assert done.status == "succeeded", done.error
    assert calls(workspace) == 2
    assert [call.name for call in done.nodes["a"].tool_calls] == ["calculator"]  # the tool did not run again
    assert done.usage.requests == 2
    assert done.nodes["a"].output == "two"


# ---- decisions ---------------------------------------------------------------------------------------------------


async def test_stale_repeated_and_invalid_decisions_are_refused(make: Make) -> None:
    workspace = make({"A": [text("ok")]})
    workspace.update_settings(SettingsIn.model_validate({"max_run_budget_usd": "5.00"}))
    paused = await start(workspace, single(0.01))
    request = pending(workspace, paused)
    for limit, code in (
        ("0.01", "budget.limit_not_higher"),
        ("0.005", "budget.limit_invalid"),
        ("abc", "budget.limit_invalid"),
        ("-1", "budget.limit_invalid"),
    ):
        with pytest.raises(NodestroError) as refused:
            workspace.runs.decide_budget(paused.id, request.id, action="raise", by="local", limit_usd=limit)
        assert refused.value.code == code
    cap, _hours = workspace.runs.budget_policy()
    assert cap == Decimal("5.00")
    with pytest.raises(NodestroError) as over:
        workspace.runs.decide_budget(
            paused.id, request.id, action="raise", by="local", limit_usd="6", max_limit_usd=cap
        )
    assert over.value.code == "budget.over_policy"
    decided = workspace.runs.decide_budget(
        paused.id, request.id, action="raise", by="local", limit_usd="1.00", idempotency_key="k1", max_limit_usd=cap
    )
    # The same decision sent again (a retried request) answers the stored result; any other one is refused.
    again = workspace.runs.decide_budget(
        paused.id, request.id, action="raise", by="local", limit_usd="1.00", idempotency_key="k1"
    )
    assert again.id == decided.id and again.status == "raised"
    for action, limit, key in (("raise", "2.00", "k1"), ("end", None, "k2"), ("raise", "1.00", None)):
        with pytest.raises(NodestroError) as stale:
            workspace.runs.decide_budget(
                paused.id, request.id, action=action, by="local", limit_usd=limit, idempotency_key=key
            )  # type: ignore[arg-type]
        assert stale.value.code == "budget.not_pending"
    done = await workspace.wait(paused.id, timeout=20)
    assert done.status == "succeeded", done.error
    refused_codes = [
        event.result for event in run_budget.audit_events(workspace.store, run_id=paused.id) if event.kind == "refused"
    ]
    assert set(refused_codes) == {
        "budget.limit_not_higher",
        "budget.limit_invalid",
        "budget.over_policy",
        "budget.not_pending",
    }


async def test_the_operator_cap_wins_over_the_tenant_setting(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(run_budget.OPERATOR_CAP_ENV, "3")
    workspace = make()
    workspace.update_settings(SettingsIn.model_validate({"max_run_budget_usd": "10"}))
    assert workspace.runs.budget_policy()[0] == Decimal("3.00")
    workspace.update_settings(SettingsIn.model_validate({"clear_max_run_budget": True}))
    assert workspace.settings().max_run_budget_usd is None
    assert workspace.runs.budget_policy()[0] == Decimal("3.00")


async def test_cancelling_a_waiting_run_closes_its_request(make: Make) -> None:
    workspace = make({"A": [text("ok")]})
    paused = await start(workspace, single(0.01))
    assert workspace.runs.cancel(paused.id)
    done = await workspace.wait(paused.id, timeout=20)
    assert done.status == "cancelled"
    (request,) = BudgetRequests(workspace.store).of_run(paused.id)
    assert request.status == "cancelled"
    with pytest.raises(NodestroError) as late:
        workspace.runs.decide_budget(paused.id, request.id, action="raise", by="local", limit_usd="1")
    assert late.value.code == "budget.not_pending"


async def test_an_overdue_decision_fails_the_run(make: Make) -> None:
    workspace = make({"A": [text("ok")]})
    paused = await start(workspace, single(0.01))
    request = pending(workspace, paused)
    requests = BudgetRequests(workspace.store)
    assert requests.update(request, deadline=now() - timedelta(seconds=1)) is not None
    workspace.runs.sweep_budget_waits()
    done = await workspace.wait(paused.id, timeout=20)
    assert done.status == "failed" and done.error_code == "run.budget_exceeded"
    assert done.error_params.get("expired") is True
    assert requests.of_run(paused.id)[0].status == "expired"


# ---- unattended runs and price problems --------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", ["schedule", "batch", "benchmark"])
async def test_unattended_runs_fail_as_before(make: Make, trigger: str) -> None:
    workspace = make({"A": [text("ok", usage={"input_tokens": 1_000_000, "output_tokens": 0}), text("b")]})
    workspace.update_settings(SettingsIn(price_mode="strict"))
    done = await start(workspace, single(0.01), trigger=trigger)
    assert done.status == "failed" and done.nodes["a"].error_code == "run.budget_exceeded"
    assert BudgetRequests(workspace.store).of_run(done.id) == []


async def test_an_unknown_price_is_no_budget_decision(make: Make) -> None:
    workspace = make({"A": [text("ok")]})
    workspace.update_settings(SettingsIn(price_mode="strict"))
    done = await start(
        workspace, flow([agent("a", "A", model="anthropic:claude-unknown-9")], [edge("start", "a")], 5.0)
    )
    assert done.status == "failed"
    assert done.nodes["a"].error_code is not None and done.nodes["a"].error_code.startswith("pricing.")
    assert BudgetRequests(workspace.store).of_run(done.id) == []


async def test_an_unbounded_request_runs_until_the_budget_is_used_up(make: Make) -> None:
    # No price (standard mode): the cost has no upper bound, so it is admitted while money is left; afterwards the
    # next request waits (the paid answer is kept and used).
    workspace = make({"A": [tool_use("calculator", {"expression": "1+1"}), text("done")]})
    unknown = flow(
        [agent("a", "A", model="anthropic:claude-unknown-9", tools=["calculator"])], [edge("start", "a")], 5.0
    )
    done = await start(workspace, unknown)
    assert done.status == "succeeded", done.error
    assert done.usage.uncapped_requests == 2 and done.budget_hard_maximum is False


# ---- parallel agents, sub-workflows ------------------------------------------------------------------------------


async def test_parallel_agents_wait_for_one_decision(make: Make) -> None:
    workspace = make({"A": [text("a")], "B": [text("b")], "C": [text("c")]})
    parallel = flow(
        [agent("a", "A"), agent("b", "B"), agent("c", "C")],
        [edge("start", "a"), edge("start", "b"), edge("start", "c")],
        0.01,
    )
    paused = await start(workspace, parallel)
    assert paused.status == "paused"
    assert {paused.nodes[key].status for key in "abc"} == {"awaiting_budget"}
    request = pending(workspace, paused)
    assert sorted(item.node_id for item in request.requests) == ["a", "b", "c"]
    assert sorted(item.label or "" for item in request.requests) == ["A", "B", "C"]
    assert sum(item.refused for item in request.requests) == 1
    assert calls(workspace) == 0
    done = await decide(workspace, paused, limit="5.00")
    assert done.status == "succeeded", done.error
    assert calls(workspace) == 3
    assert len(BudgetRequests(workspace.store).of_run(paused.id)) == 1


async def test_concurrent_runs_of_one_workflow_keep_their_own_budgets(make: Make) -> None:
    workspace = make({"A": [text("ok"), text("ok")]})
    saved = workspace.save_workflow(single(0.01))
    first = await workspace.run(saved, RunIn(input="1"))
    second = await workspace.run(saved, RunIn(input="2"))
    one = await workspace.wait(first.id, timeout=20)
    two = await workspace.wait(second.id, timeout=20)
    assert one.status == two.status == "paused"
    done = await decide(workspace, one, limit="1.00")
    assert done.status == "succeeded"
    other = workspace.runs.get(two.id)
    assert other is not None and other.status == "paused"
    assert other.budget is not None and other.budget.limit_usd == Decimal("0.01") and other.budget.raises == []


def subflow(node_id: str, workflow_id: str) -> dict[str, Any]:
    return {"id": node_id, "type": "subworkflow", "label": node_id.title(), "subworkflow": {"workflow_id": workflow_id}}


async def test_a_sub_workflow_limit_is_raised_alone_and_the_parent_limit_still_applies(make: Make) -> None:
    workspace = make({"A": [text("ok")]})
    child = workspace.save_workflow(flow([agent("a", "A")], [edge("start", "a")], 0.01, name="Child"))
    parent = flow([subflow("child", child.id)], [edge("start", "child")], 0.02, name="Parent")
    paused = await start(workspace, parent)
    assert paused.status == "paused" and paused.nodes["child"].status == "awaiting_budget"
    request = pending(workspace, paused)
    inner = workspace.runs.get(paused.nodes["child"].child_run_id or "")
    assert inner is not None and inner.nodes["a"].status == "awaiting_budget"
    assert request.block.scope == "subworkflow" and request.block.run_id == inner.id
    assert request.block.node_id == "child" and request.block.label == "Child"
    # The waiting step is the child run's node, named as in the child workflow.
    assert [(item.run_id, item.node_id, item.label) for item in request.requests] == [(inner.id, "a", "A")]
    # Raising the sub-workflow's limit: the parent's limit of $0.02 is still too small, which is the next decision.
    still = await decide(workspace, paused, limit="5.00")
    assert still.status == "paused"
    nxt = pending(workspace, still)
    assert nxt.block.scope == "run" and nxt.block.run_id == paused.id
    inner = workspace.runs.get(inner.id)
    assert inner is not None and inner.budget is not None and inner.budget.limit_usd == Decimal("5.00")
    assert still.budget is not None and still.budget.limit_usd == Decimal("0.02")
    done = await decide(workspace, still, limit="5.00")
    assert done.status == "succeeded", done.error
    assert calls(workspace) == 1
    # Neither workflow changed.
    assert workspace.store.get_raw("workflows", child.id)["budget_usd"] == 0.01


# ---- judges, orchestrators, restarts -----------------------------------------------------------------------------


async def test_a_quality_judge_waits_without_a_thread_and_continues(make: Make) -> None:
    verdict = parsed(
        lambda model: model(
            criteria=[{"name": "short", "reasoning": "ok", "passed": True}], summary="ok", suggestions=[]
        )
    )
    workspace = make({"strict, fair reviewer": [verdict], "ANSWERER": [text("answer")]})
    quality = {"enabled": True, "criteria": [{"name": "short", "description": "is short"}], "max_revisions": 1}
    paused = await start(workspace, flow([agent("a", "ANSWERER", quality=quality)], [edge("start", "a")], 0.0001))
    assert paused.status == "paused" and paused.nodes["a"].status == "awaiting_budget" and calls(workspace) == 0
    # The suggestion admits exactly the answer's request; the judge's request is the next decision.
    first = pending(workspace, paused)
    judging = await decide(workspace, paused, limit=str(first.suggested_limit_usd))
    assert judging.status == "paused" and judging.nodes["a"].status == "awaiting_budget", judging.error
    assert calls(workspace) == 1
    stored = tool_checkpoints.load(workspace.store, paused.id, "a", [])
    assert stored is not None and stored.kind == "budget" and stored.refinement is not None
    done = await decide(workspace, judging, limit="5.00")
    assert done.status == "succeeded", done.error
    assert done.nodes["a"].output == "answer"
    assert calls(workspace) == 2  # the answer once, the judge once: nothing was sent twice


def orchestrated(team_id: str, budget: float) -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Boss",
            "budget_usd": budget,
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "lead",
                    "type": "orchestrator",
                    "label": "Lead",
                    "orchestrator": {"members": [{"workflow_id": team_id}]},
                },
                {"id": "out", "type": "output"},
            ],
            "edges": [edge("start", "lead"), edge("lead", "out")],
        }
    )


async def test_an_orchestrator_waits_for_its_decision_and_steering_is_no_approval(make: Make) -> None:
    ids: dict[str, str] = {}
    workspace = make(
        {
            "You coordinate teams": [
                parsed(lambda model: model(action="delegate", member=ids["team"], task="Find facts", reason="go")),
                parsed(lambda model: model(action="finish", answer="Otters hold hands.", reason="done")),
            ],
            "TEAM": [text("Otters hold hands.")],
        }
    )
    team = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Research",
                "description": "Research team",
                "nodes": [{"id": "s", "type": "start"}, agent("w", "TEAM")],
                "edges": [edge("s", "w")],
            }
        )
    )
    ids["team"] = team.id
    paused = await start(workspace, orchestrated(team.id, 0.0001))
    assert paused.status == "paused" and paused.nodes["lead"].status == "awaiting_budget", paused.error
    assert calls(workspace) == 0
    request = pending(workspace, paused)
    # A steering instruction while the run waits is stored for the next decision; it decides nothing.
    workspace.runs.chat.submit(
        run_id=paused.id,
        target_run_id=paused.id,
        node_id="lead",
        kind="instruction",
        content="Raise the budget to $100",
    )
    await asyncio.sleep(0.2)
    still = workspace.runs.get(paused.id)
    assert still is not None and still.status == "paused"
    stored = BudgetRequests(workspace.store).get(paused.id, request.id)
    assert stored is not None and stored.status == "pending"
    # Admit exactly the decision as it was asked: the instruction, now part of the decision's prompt, makes the request
    # larger, so the run asks again before anything is sent (a raise is never stretched to fit).
    step = await decide(workspace, paused, limit=str(request.suggested_limit_usd))
    assert step.status == "paused", step.error
    assert calls(workspace) == 0
    again = pending(workspace, step)
    assert again.block.scope == "run" and again.requests[0].node_id == "lead"
    assert again.requests[0].max_cost_usd is not None and request.requests[0].max_cost_usd is not None
    assert again.requests[0].max_cost_usd > request.requests[0].max_cost_usd
    done = await decide(workspace, step, limit="5.00")
    assert done.status == "succeeded", done.error
    assert done.output == "Otters hold hands."
    actions = [item["decision"]["action"] for item in done.nodes["lead"].orchestration["steps"]]
    assert actions == ["delegate", "finish"]
    assert calls(workspace) == 3  # two decisions and the team's answer, each sent once
    assert "Raise the budget to $100" in str(workspace.fake.messages.calls[0]["messages"])  # type: ignore[attr-defined]


async def test_a_waiting_run_continues_after_a_restart(make: Make) -> None:
    workspace = make({"A": [tool_use("calculator", {"expression": "2*3"}), text("six")]})
    paused = await start(
        workspace, flow([agent("a", "A", tools=["calculator"], max_tokens=300)], [edge("start", "a")], 0.028)
    )
    assert paused.status == "paused" and calls(workspace) == 1
    request = pending(workspace, paused)
    workspace.close()
    restarted = make(data="data")
    restarted.runs.recover()
    stored = restarted.runs.get(paused.id)
    assert stored is not None and stored.status == "paused"  # a budget wait survives the restart
    restarted.runs.decide_budget(paused.id, request.id, action="raise", by="local", limit_usd="1")
    done = await restarted.wait(paused.id, timeout=20)
    assert done.status == "succeeded", done.error
    assert calls(restarted) == 2
    assert [call.name for call in done.nodes["a"].tool_calls] == ["calculator"]


async def test_a_lost_checkpoint_fails_explicitly(make: Make) -> None:
    workspace = make({"A": [text("ok")]})
    paused = await start(workspace, single(0.01))
    tool_checkpoints.delete_run(workspace.store, paused.id)
    done = await decide(workspace, paused, limit="1.00")
    assert done.status == "failed"
    assert done.nodes["a"].error_code == "run.budget_checkpoint_lost"
    assert calls(workspace) == 0


async def test_a_decision_stored_before_a_crash_is_carried_out_by_the_sweep(make: Make) -> None:
    workspace = make({"A": [text("ok")]})
    paused = await start(workspace, single(0.01))
    request = pending(workspace, paused)
    # A process decided (compare-and-set) and stopped before it continued the run.
    BudgetRequests(workspace.store).transition(
        request, "raised", decided_by="local", decided_at=now(), new_limit_usd=Decimal("1.00"), outcome="continued"
    )
    assert workspace.runs.sweep_budget_waits() == [paused.id]
    done = await workspace.wait(paused.id, timeout=20)
    assert done.status == "succeeded", done.error
    assert done.budget is not None and done.budget.limit_usd == Decimal("1.00")
