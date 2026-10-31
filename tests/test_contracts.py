"""Output contracts and capability tags of workflows (Adaptive Orchestration M3, teams)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from pydantic import ValidationError
import pytest

from nodestro_runtime.contracts import ContractError, apply_contract, contract_of
from nodestro_runtime.engine import check_workflow
from nodestro_runtime.preflight import preflight
from nodestro_runtime.schemas import OutputField, RunIn, Workflow, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, parsed, text
from nodestro_runtime.workspace import Workspace

Make = Callable[..., Workspace]
CONTRACT: list[dict[str, Any]] = [
    {"name": "summary", "type": "string"},
    {"name": "score", "type": "integer"},
    {"name": "notes", "type": "string", "required": False},
]


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]]) -> Workspace:
        fake = ScriptedClient(script)
        workspace = Workspace(tmp_path / f"data{len(opened)}", client_factory=lambda _provider: fake, integrations=[])
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
        "agent": {"name": node_id, "system_prompt": prompt, **config},
    }


def structured(*names: str) -> dict[str, Any]:
    types = {"summary": "string", "score": "integer", "notes": "string", "extra": "string"}
    return {"mode": "structured", "fields": [{"name": name, "type": types[name]} for name in names]}


def edge(source: str, target: str, handle: str | None = None) -> dict[str, Any]:
    return {"id": f"{source}-{target}", "source": source, "target": target, "source_handle": handle}


def team(worker: dict[str, Any], contract: list[dict[str, Any]] = CONTRACT, **extra: Any) -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Team",
            "nodes": [
                {"id": "start", "type": "start"},
                worker,
                {"id": "out", "type": "output", "label": "Result", "output": {"contract": contract}},
            ],
            "edges": [edge("start", worker["id"]), edge(worker["id"], "out")],
            **extra,
        }
    )


async def run(workspace: Workspace, flow: WorkflowIn) -> Any:
    saved = workspace.save_workflow(flow)
    started = await workspace.run(saved, RunIn(input="x"))
    return await workspace.wait(started.id, timeout=20)


def answer(**values: Any) -> Any:
    return parsed(lambda model: model(**values))


# ---- at run time ------------------------------------------------------------------------------


async def test_a_fitting_result_delivers_exactly_the_contract(make: Make) -> None:
    workspace = make({"WORK": [answer(summary="ok", score=4, extra="internal")]})
    worker = agent("work", "WORK", output=structured("summary", "score", "extra"))
    done = await run(workspace, team(worker))
    assert done.status == "succeeded", done.error
    assert done.output == {"summary": "ok", "score": 4, "notes": None}  # "extra" is not promised


async def test_json_text_can_fulfil_a_contract(make: Make) -> None:
    workspace = make({"WORK": [text('{"summary": "fine", "score": "7"}')]})
    done = await run(workspace, team(agent("work", "WORK")))
    assert done.status == "succeeded", done.error
    assert done.output == {"summary": "fine", "score": 7, "notes": None}


async def test_a_result_that_misses_the_contract_fails_the_run(make: Make) -> None:
    workspace = make({"WORK": [text("just prose")], "OTHER": [text('{"summary": "x", "score": "many"}')]})
    done = await run(workspace, team(agent("work", "WORK")))
    assert done.status == "failed"
    assert done.nodes["out"].error_code == "run.output_contract"
    assert done.nodes["out"].error_params == {"fields": "summary, score"}
    wrong = await run(workspace, team(agent("work", "OTHER")))
    assert wrong.nodes["out"].error_params == {"fields": "score"}


async def test_a_parent_decides_on_the_fields_of_a_team(make: Make) -> None:
    workspace = make({"WORK": [answer(summary="ok", score=9)], "HIGH": [text("high")]})
    child = workspace.save_workflow(team(agent("work", "WORK", output=structured("summary", "score"))))
    rule = {"id": "high", "label": "High", "field": "score", "operator": "gte", "value": 5}
    parent = WorkflowIn.model_validate(
        {
            "name": "Parent",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "team", "type": "subworkflow", "label": "Team", "subworkflow": {"workflow_id": child.id}},
                {"id": "gate", "type": "condition", "label": "Gate", "condition": {"rules": [rule]}},
                agent("high", "HIGH"),
            ],
            "edges": [edge("start", "team"), edge("team", "gate"), edge("gate", "high", "high")],
        }
    )
    done = await run(workspace, parent)
    assert done.status == "succeeded", done.error
    assert done.nodes["team"].output == {"summary": "ok", "score": 9, "notes": None}
    assert done.nodes["high"].status == "succeeded"


# ---- checks -----------------------------------------------------------------------------------


def test_invalid_and_repeated_contract_fields_block_a_run() -> None:
    bad = [{"name": "summary"}, {"name": "summary"}, {"name": "not valid"}]
    flow = Workflow.model_validate(team(agent("work", "WORK"), contract=bad).model_dump())
    codes = sorted(issue.code for issue in check_workflow(flow))
    assert codes == ["workflow.contract_field_duplicate", "workflow.contract_field_name"]


def test_the_pre_check_warns_when_no_agent_delivers_the_contract(make: Make) -> None:
    workspace = make({})
    text_worker = Workflow.model_validate(team(agent("work", "WORK")).model_dump())
    partial = Workflow.model_validate(team(agent("work", "WORK", output=structured("summary"))).model_dump())
    complete = Workflow.model_validate(team(agent("work", "WORK", output=structured("summary", "score"))).model_dump())

    def contract_codes(flow: Workflow) -> list[str]:
        return [item.code for item in preflight(workspace, flow).problems if item.code.startswith("check.contract")]

    assert contract_codes(text_worker) == ["check.contract_text_source"]
    assert contract_codes(partial) == ["check.contract_field_missing"]
    assert contract_codes(complete) == []


def test_the_contract_of_a_workflow() -> None:
    assert [item.name for item in contract_of(team(agent("work", "WORK")))] == ["summary", "score", "notes"]
    plain = WorkflowIn.model_validate({"nodes": [{"id": "out", "type": "output"}]})
    assert contract_of(plain) == []
    two = WorkflowIn.model_validate(
        {"nodes": [{"id": "a", "type": "output", "output": {"contract": CONTRACT}}, {"id": "b", "type": "output"}]}
    )
    assert contract_of(two) == []  # several results: an object per result node, no single contract


def test_apply_contract_reports_what_is_missing() -> None:
    fields = [OutputField(name="a"), OutputField(name="b", type="number", required=False)]
    assert apply_contract(fields, {"a": "x", "b": 2}) == {"a": "x", "b": 2.0}
    with pytest.raises(ContractError) as missing:
        apply_contract(fields, {"b": 1})
    assert missing.value.fields == ["a"]
    with pytest.raises(ContractError):
        apply_contract(fields, ["not", "an", "object"])


# ---- capability tags --------------------------------------------------------------------------


def test_capabilities_are_trimmed_and_unique() -> None:
    flow = WorkflowIn.model_validate({"capabilities": [" Research ", "research", "", "translate  texts"]})
    assert flow.capabilities == ["Research", "translate texts"]
    with pytest.raises(ValidationError):
        WorkflowIn.model_validate({"capabilities": ["x" * 41]})
    with pytest.raises(ValidationError):
        WorkflowIn.model_validate({"capabilities": [f"tag{i}" for i in range(21)]})
