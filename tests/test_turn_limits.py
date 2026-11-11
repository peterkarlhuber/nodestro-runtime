"""Limits a step's agent reaches and continues at instead of failing (RT-067, the regression of 2026-11-08).

With Ollama's thinking models (Qwen 3.5) effort ``medium`` means unbounded thinking, and the thinking counts against
the answer limit: a 9b model wrote 5,791 tokens for one step. A small model that kept calling its calculator failed
the whole run with ``MaxToolRoundsError``. Now the agent answers once more without thinking, and at the round limit
the last request asks for the answer; the step records both (``NodeRun.limits_handled``). The double answers like
Ollama 0.40.14 (streamed chunks, ``finish_reason: "length"`` at the output limit, ``tool_choice`` ignored).
"""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
from typing import Any

import httpx2
import openai
from orkexa import IncompleteResponseError
from orkexa.core.runtime import FINAL_ROUND_PROMPT
import pytest
from test_ollama_multistep import _sse

from nodestro_runtime.engine import node_problem
from nodestro_runtime.schemas import RunIn, SettingsIn, WorkflowIn
from nodestro_runtime.workspace import Workspace

MODEL = "ollama:qwen3.5:4b"


class Scripted:
    """Ollama's endpoint with one scripted streamed answer per request (a function of the request body)."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        deltas, finish = self.answer(body, len(self.requests))
        return _sse(deltas, finish)


@pytest.fixture
def scripted() -> Scripted:
    return Scripted(lambda _body, _n: ([{"content": "ok"}], "stop"))


@pytest.fixture
def workspace(tmp_path: Path, scripted: Scripted) -> Iterator[Workspace]:
    def client(_provider: str) -> Any:
        return openai.OpenAI(
            api_key="ollama",
            base_url="http://127.0.0.1:9/v1",
            max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(scripted.handler)),
        )

    opened = Workspace(tmp_path / "data", client_factory=client, integrations=[])
    capabilities = {"tools": "supported", "reasoning": "supported", "structured_output": "supported"}
    opened.update_settings(
        SettingsIn.model_validate(
            {
                "providers": {"ollama": {"base_url": "http://127.0.0.1:9/v1", "capabilities": capabilities}},
                "local_reasoning": "full",
            }
        )
    )
    yield opened
    opened.close()


def _workflow(**agent: Any) -> WorkflowIn:
    config = {"system_prompt": "You are the analyst.", "model": MODEL, "effort": "medium", **agent}
    return WorkflowIn.model_validate(
        {
            "name": "One step",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "step", "type": "agent", "agent": config},
                {"id": "output", "type": "output"},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "step"},
                {"id": "e2", "source": "step", "target": "output"},
            ],
        }
    )


async def _run(workspace: Workspace, **agent: Any) -> Any:
    saved = workspace.save_workflow(_workflow(**agent))
    return await workspace.wait((await workspace.run(saved, RunIn(input="Compute 2+2."))).id, timeout=30)


async def test_a_step_that_reasons_until_the_limit_answers_once_more_without_reasoning(
    workspace: Workspace, scripted: Scripted
) -> None:
    def answer(body: dict[str, Any], n: int) -> tuple[list[dict[str, Any]], str]:
        if n == 1:
            return [{"reasoning": "Let me think. " * 40}], "length"
        return [{"content": "It is 4."}], "stop"

    scripted.answer = answer
    run = await _run(workspace, max_tokens=500)

    state = run.nodes["step"]
    assert run.status == "succeeded", state.error
    assert state.output == "It is 4."
    assert state.limits_handled == ["reasoning"]
    assert state.reasoning_reduced == "output_limit"
    assert [body.get("reasoning_effort") for body in scripted.requests] == ["medium", "none"]
    assert state.usage.requests == 2  # the overflowed request is billed and counted


async def test_a_step_that_keeps_calling_tools_answers_in_its_last_round(
    workspace: Workspace, scripted: Scripted
) -> None:
    def answer(body: dict[str, Any], n: int) -> tuple[list[dict[str, Any]], str]:
        if "tools" not in body:
            assert body["messages"][-1] == {"role": "user", "content": FINAL_ROUND_PROMPT}
            return [{"content": "It is 4."}], "stop"
        call = {"index": 0, "id": f"call_{n}", "type": "function"}
        call["function"] = {"name": "calculator", "arguments": json.dumps({"expression": "2+2"})}
        return [{"reasoning": "Once more."}, {"tool_calls": [call]}], "tool_calls"

    scripted.answer = answer
    run = await _run(workspace, tools=["calculator"], max_tool_rounds=3)

    state = run.nodes["step"]
    assert run.status == "succeeded", state.error
    assert state.output == "It is 4."
    assert state.limits_handled == ["tool_rounds"]
    assert len(state.tool_calls) == 3
    assert len(scripted.requests) == 4  # max_tool_rounds + 1, as before: no extra request


async def test_a_tool_call_in_the_last_round_still_fails_with_the_loop_explained(
    workspace: Workspace, scripted: Scripted
) -> None:
    def answer(_body: dict[str, Any], n: int) -> tuple[list[dict[str, Any]], str]:
        call = {"index": 0, "id": f"call_{n}", "type": "function"}
        call["function"] = {"name": "calculator", "arguments": json.dumps({"expression": "2+2"})}
        return [{"tool_calls": [call]}], "tool_calls"

    scripted.answer = answer
    run = await _run(workspace, tools=["calculator"], max_tool_rounds=3)

    state = run.nodes["step"]
    assert state.status == "failed"
    assert state.error_code == "run.agent.MaxToolRoundsError"
    assert state.error_params["looping"] is True and state.error_params["local"] is True
    assert state.limits_handled == ["tool_rounds"]


@pytest.mark.parametrize(
    ("error", "cause", "words"),
    [
        (
            IncompleteResponseError("x", stop_reason="max_tokens", max_tokens=8000, reasoning=True, answered=False),
            "reasoning",
            "spent the whole answer limit on reasoning",
        ),
        (
            IncompleteResponseError("x", stop_reason="max_tokens", max_tokens=8000, reasoning=False, answered=True),
            "answer",
            "longer than the agent's answer limit",
        ),
        (
            IncompleteResponseError("x", stop_reason="context_window_exceeded", max_tokens=8000),
            "context_window",
            "context window",
        ),
        (IncompleteResponseError("x"), "answer", "answer limit"),
    ],
)
def test_an_incomplete_answer_says_why_and_what_to_change(
    error: IncompleteResponseError, cause: str, words: str
) -> None:
    found = node_problem(error)
    assert found.code == "run.agent.IncompleteResponseError"
    assert found.params["cause"] == cause
    assert words in found.message
