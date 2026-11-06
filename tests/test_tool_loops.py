"""RT-067: a step whose model keeps calling tools until its rounds run out says what the calls show."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime.engine import TOOL_LOOP_REPEATS, tool_rounds_problem
from nodestro_runtime.problems import problem
from nodestro_runtime.schemas import AgentConfig, RunIn, ToolCallRecord, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    fake = ScriptedClient({})
    found = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
    found.fake = fake  # type: ignore[attr-defined]
    yield found
    found.close()


def flow(**agent: Any) -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Loop",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "calc",
                    "type": "agent",
                    "label": "Calc",
                    "agent": {"name": "Calc", "system_prompt": "CALC", "tools": ["calculator"], **agent},
                },
            ],
            "edges": [{"id": "e1", "source": "start", "target": "calc"}],
        }
    )


async def failed_step(workspace: Workspace, answers: list[Any], **agent: Any) -> Any:
    workspace.fake.messages.script["CALC"] = answers  # type: ignore[attr-defined]
    saved = workspace.save_workflow(flow(**agent))
    started = await workspace.run(saved, RunIn(input="What is 2 + 2?"))
    run = await workspace.wait(started.id, timeout=30)
    assert run.status == "failed"
    return run.nodes["calc"]


async def test_a_model_that_repeats_the_same_call_is_reported_as_a_tool_loop(workspace: Workspace) -> None:
    same = [tool_use("calculator", {"expression": "2 + 2"}, call_id=f"toolu_{i}") for i in range(4)]
    state = await failed_step(workspace, same, max_tool_rounds=3)
    assert state.error_code == "run.agent.MaxToolRoundsError"  # the code the studio translates stays the same
    params = state.error_params
    assert params["type"] == "MaxToolRoundsError"
    assert params["rounds"] == 3 and params["calls"] == 3
    assert params["repeated"] == 2 and params["looping"] is True and params["tool"] == "calculator"
    assert params["local"] is False
    assert "tool loop" in state.error and "limit 3 rounds" in state.error
    assert "advanced settings" in state.error


async def test_different_calls_are_not_called_a_loop(workspace: Workspace) -> None:
    calls = [tool_use("calculator", {"expression": f"{i} + 1"}, call_id=f"toolu_{i}") for i in range(3)]
    state = await failed_step(workspace, calls, max_tool_rounds=2)
    assert state.error_params["repeated"] == 0 and state.error_params["looping"] is False
    assert state.error_params["calls"] == 2
    assert "tool loop" not in state.error and "2 tool calls without giving its answer" in state.error


async def test_a_step_that_answers_in_time_is_not_touched(workspace: Workspace) -> None:
    workspace.fake.messages.script["CALC"] = [  # type: ignore[attr-defined]
        tool_use("calculator", {"expression": "2 + 2"}),
        text("4"),
    ]
    saved = workspace.save_workflow(flow(max_tool_rounds=3))
    started = await workspace.run(saved, RunIn(input="What is 2 + 2?"))
    run = await workspace.wait(started.id, timeout=30)
    assert run.status == "succeeded" and run.output == "4"


def test_a_local_model_gets_advice_for_small_models() -> None:
    record = ToolCallRecord(id="1", name="calculator", input={"expression": "1"})
    again = ToolCallRecord(id="2", name="calculator", input={"expression": "1"})
    third = ToolCallRecord(id="3", name="calculator", input={"expression": "1"})
    found = tool_rounds_problem(
        problem("run.agent.MaxToolRoundsError", "MaxToolRoundsError: x", type="MaxToolRoundsError"),
        [record, again, third],
        AgentConfig(name="A", model="ollama:qwen3.5:2b", max_tool_rounds=3),
    )
    assert found.params["local"] is True and found.params["repeated"] == TOOL_LOOP_REPEATS
    assert "Small local models" in found.message
    # Without calls (the limit 0) nothing breaks.
    empty = tool_rounds_problem(problem("run.agent.MaxToolRoundsError", "x"), [], AgentConfig(name="A"))
    assert empty.params["calls"] == 0 and empty.params["tool"] == ""


def test_the_agent_test_reports_the_loop_too(workspace: Workspace) -> None:
    from nodestro_runtime.agent_test import AgentTestIn

    workspace.fake.messages.script["CALC"] = [  # type: ignore[attr-defined]
        tool_use("calculator", {"expression": "2 + 2"}, call_id=f"toolu_{i}") for i in range(3)
    ]
    agent = AgentConfig(name="Calc", system_prompt="CALC", tools=["calculator"], max_tool_rounds=2)
    result, _tracker = workspace.tester.run(AgentTestIn(agent=agent, input="x"))
    assert result.status == "failed" and result.error_code == "run.agent.MaxToolRoundsError"
    assert result.error_params["calls"] == 2 and result.error_params["repeated"] == 1
    assert result.error_params["looping"] is False
