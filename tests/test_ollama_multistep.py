"""Multi-step agent runs on Ollama: the production-downtime flow through the whole runtime.

Regression for the run that failed with ``InvalidRequestError: ollama: BadRequestError: Error code: 400 - invalid
message content type: <nil>``: a local Qwen agent with tools and an output format called its tools in several
rounds (thinking, no text), ended the tool phase with thinking only, and the request for the structured answer
(Orkexa's ``FORMAT_PROMPT``, Ollama takes tools and the format in separate requests) repeated that turn as
``{"role": "assistant", "content": null}``. Ollama's OpenAI-compatible API accepts a ``null`` content only next to
tool calls.

The double below answers like Ollama 0.40.14 does: its request validation (``openai/openai.go``,
``FromChatRequest``) first, then scripted streamed answers in Ollama's chunk shapes (``reasoning`` deltas, each tool
call complete in one chunk).
"""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import re
from typing import Any

import httpx2
import openai
from orkexa.core.runtime import FORMAT_PROMPT
import pytest

from nodestro_runtime import resume
from nodestro_runtime.schemas import RunIn, SettingsIn, WorkflowIn
from nodestro_runtime.workspace import Workspace

MODEL = "ollama:qwen3.5:4b"
TABLE = "\n".join(
    [
        "| Nr. | Anlage | Dauer (min) | Störcode |",
        "|---|---|---|---|",
        "| 1 | Etikettierer | 45 | E-17 |",
        "| 2 | Füller | 120 | F-03 |",
        "| 3 | Etikettierer | 30 | E-17 |",
        "| 4 | Packer | 25 | P-08 |",
        "| 5 | Füller | 90 | F-11 |",
    ]
)
INPUT = f"Geplante Produktionszeit 14.400 Minuten, Ziel unter 3 %.\n\n{TABLE}"


class FakeOllama:
    """Ollama's OpenAI-compatible endpoint: its message validation, then the agents' scripted answers."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.refused: list[dict[str, Any]] = []
        #: Answer this many requests of the downtime analyst with HTTP 503 first (a transient failure).
        self.unavailable_for_format = 0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        for message in body["messages"]:
            if message.get("content") is None and message.get("tool_calls") is None:
                self.refused.append(body)
                error = {"message": "invalid message content type: <nil>", "type": "invalid_request_error"}
                return httpx2.Response(400, json={"error": {**error, "param": None, "code": None}})
        self.requests.append(body)
        system = body["messages"][0]["content"]
        if "downtime analyst" in system:
            if self.unavailable_for_format and body["messages"][-1]["content"] == FORMAT_PROMPT:
                self.unavailable_for_format -= 1
                return httpx2.Response(503, json={"error": {"message": "server busy"}})
            chunks = self.downtime(body)
        elif "cause analyst" in system:
            chunks = [{"reasoning": "6M: material, machine."}, {"content": "Hypotheses: label rolls of supplier B."}]
        else:
            chunks = [{"reasoning": "Combine both."}, {"content": "Measures: change the label supplier."}]
        return _sse(chunks, "tool_calls" if any("tool_calls" in c for c in chunks) else "stop")

    @staticmethod
    def downtime(body: dict[str, Any]) -> list[dict[str, Any]]:
        results = [m for m in body["messages"] if m["role"] == "tool"]
        if body["messages"][-1]["content"] == FORMAT_PROMPT:
            assert "tools" not in body
            share = json.loads(results[0]["content"])["share_of_reference_percent"]
            answer = {"analysis": "Labeler and filler cause most downtime.", "downtime_share_percent": round(share, 2)}
            return [{"reasoning": "Now the JSON."}, {"content": json.dumps(answer)}]
        rounds: list[tuple[str, dict[str, Any]]] = [
            (
                "table_totals",
                {"table": TABLE, "value_column": "Dauer (min)", "group_by": "Anlage", "reference_total": 14400},
            ),
            ("table_totals", {"table": TABLE, "value_column": "Dauer (min)", "group_by": "Störcode"}),
            ("calculator", {"expression": "310 / 14400 * 100"}),
        ]
        if len(results) < len(rounds):
            name, arguments = rounds[len(results)]
            call = {"index": 0, "id": f"call_{len(results)}", "type": "function"}
            call["function"] = {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}
            return [{"reasoning": f"Round {len(results) + 1}: {name}."}, {"tool_calls": [call]}]
        # The tool phase ends with thinking only: no text, no tool call.
        return [{"reasoning": "The share is about 2.15 %, below the target."}, {"content": ""}]


def _sse(deltas: list[dict[str, Any]], finish: str) -> httpx2.Response:
    def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> str:
        data = {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "qwen3.5:4b",
            "choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(data)}\n\n"

    usage = {"prompt_tokens": 900, "completion_tokens": 60, "total_tokens": 960}
    tail = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "qwen3.5:4b",
        "choices": [],
        "usage": usage,
    }
    body = "".join(chunk(d) for d in deltas) + chunk({}, finish) + f"data: {json.dumps(tail)}\n\ndata: [DONE]\n\n"
    return httpx2.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def _workflow() -> WorkflowIn:
    def agent(prompt: str, **extra: Any) -> dict[str, Any]:
        return {"system_prompt": prompt, "model": MODEL, "effort": "high", **extra}

    downtime = agent(
        "You are the downtime analyst. Compute every total and share with table_totals.",
        tools=["table_totals", "calculator"],
        output={
            "mode": "structured",
            "name": "Downtime",
            "fields": [
                {"name": "analysis", "type": "string", "required": True},
                {"name": "downtime_share_percent", "type": "number", "required": True, "source": "tool"},
            ],
        },
    )
    return WorkflowIn.model_validate(
        {
            "name": "Downtime",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "downtime", "type": "agent", "label": "Downtime analyst", "agent": downtime},
                {
                    "id": "causes",
                    "type": "agent",
                    "label": "Cause analyst",
                    "agent": agent("You are the cause analyst."),
                },
                {"id": "measures", "type": "agent", "label": "Measures", "agent": agent("You plan measures.")},
                {"id": "output", "type": "output"},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "downtime"},
                {"id": "e2", "source": "start", "target": "causes"},
                {"id": "e3", "source": "downtime", "target": "measures"},
                {"id": "e4", "source": "causes", "target": "measures"},
                {"id": "e5", "source": "measures", "target": "output"},
            ],
        }
    )


@pytest.fixture
def ollama() -> FakeOllama:
    return FakeOllama()


@pytest.fixture
def workspace(tmp_path: Path, ollama: FakeOllama) -> Iterator[Workspace]:
    def client(_provider: str) -> Any:
        return openai.OpenAI(
            api_key="ollama",
            base_url="http://127.0.0.1:9/v1",
            max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(ollama.handler)),
        )

    opened = Workspace(tmp_path / "data", client_factory=client, integrations=[])
    capabilities = {"tools": "supported", "reasoning": "supported", "structured_output": "supported"}
    opened.update_settings(
        SettingsIn.model_validate(
            {"providers": {"ollama": {"base_url": "http://127.0.0.1:9/v1", "capabilities": capabilities}}}
        )
    )
    yield opened
    opened.close()


def _of(ollama: FakeOllama, role: str) -> list[dict[str, Any]]:
    return [body for body in ollama.requests if role in body["messages"][0]["content"]]


async def test_the_downtime_flow_runs_through_after_several_tool_rounds_with_thinking(
    workspace: Workspace, ollama: FakeOllama
) -> None:
    saved = workspace.save_workflow(_workflow())
    run = await workspace.wait((await workspace.run(saved, RunIn(input=INPUT))).id, timeout=30)

    assert ollama.refused == []
    assert run.status == "succeeded", (run.error, {k: v.error for k, v in run.nodes.items()})
    downtime = run.nodes["downtime"]
    assert downtime.output["downtime_share_percent"] == pytest.approx(2.15)
    assert [call.name for call in downtime.tool_calls] == ["table_totals", "table_totals", "calculator"]
    sent = _of(ollama, "downtime analyst")
    assert len(sent) == 5  # three tool rounds, the thinking-only end of the tool phase, the structured answer
    last = sent[-1]["messages"]
    assert last[-2] == {"role": "assistant", "content": "", "reasoning": "The share is about 2.15 %, below the target."}
    # Only the latest assistant turn's reasoning goes back to Ollama (RT-067: replaying every round's thinking
    # filled the context window); here that is the thinking-only end of the tool phase.
    assert [m.get("reasoning") for m in last if m.get("tool_calls")] == [None, None, None]
    assert run.nodes["measures"].status == "succeeded"


async def test_a_refused_request_is_explained_and_never_retried(workspace: Workspace, ollama: FakeOllama) -> None:
    """A provider 400 about the conversation: one request, a code of its own, no message content in the text."""

    def refuse(request: httpx2.Request) -> httpx2.Response:
        ollama.refused.append(json.loads(request.content))
        error = {"message": "invalid message content type: <nil>", "type": "invalid_request_error"}
        return httpx2.Response(400, json={"error": error})

    ollama.handler = refuse  # type: ignore[method-assign]
    workflow = WorkflowIn.model_validate(
        {
            "name": "Causes",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "causes",
                    "type": "agent",
                    "agent": {"system_prompt": "You are the cause analyst.", "model": MODEL},
                },
                {"id": "output", "type": "output"},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "causes"},
                {"id": "e2", "source": "causes", "target": "output"},
            ],
        }
    )
    saved = workspace.save_workflow(workflow)
    run = await workspace.wait((await workspace.run(saved, RunIn(input="secret customer 4711"))).id, timeout=30)

    state = run.nodes["causes"]
    assert state.status == "failed"
    assert state.error_code == "run.provider_rejected_messages"
    assert state.error_params == {"provider": "ollama", "status": 400}
    assert "invalid message content type: <nil>" in (state.error or "")
    assert re.search(r"\[request messages: #0 user\[text\]\]$", state.error or "")
    assert "4711" not in (state.error or "")
    assert len(ollama.refused) == 1


async def test_continuing_after_a_failed_model_request_reuses_finished_steps(
    workspace: Workspace, ollama: FakeOllama
) -> None:
    """A transient failure of the last request of the downtime analyst: the cause analyst finished, the measures
    step stayed blocked. Continuing the run reuses the cause analyst, runs the analyst again (its read-only built-in
    tools need no confirmation) and then the blocked step."""
    ollama.unavailable_for_format = 1
    saved = workspace.save_workflow(_workflow())
    failed = await workspace.wait((await workspace.run(saved, RunIn(input=INPUT))).id, timeout=30)
    assert failed.status == "failed"
    assert failed.nodes["downtime"].error_code == "run.provider_overloaded"
    assert failed.nodes["causes"].status == "succeeded"
    assert failed.nodes["measures"].status != "succeeded"
    # Four answered requests and the refused fifth: the runtime does not repeat it on its own (the SDK client of
    # this double has no retries; a real connection retries a 503 up to MAX_RETRIES times, which is safe: nothing ran).
    assert len(_of(ollama, "downtime analyst")) == 5

    plan = resume.plan(workspace, failed.id)
    actions = {node.node_id: node.action for node in plan.nodes}
    assert actions["causes"] == "reuse" and actions["downtime"] == "rerun" and actions["measures"] == "run"
    assert not plan.needs_confirmation
    causes_before = len(_of(ollama, "cause analyst"))

    continued = await resume.resume(workspace, failed.id, mode="continue", confirm=False)
    run = await workspace.wait(continued.id, timeout=30)
    assert run.status == "succeeded", (run.error, {k: v.error for k, v in run.nodes.items()})
    assert len(_of(ollama, "cause analyst")) == causes_before  # not asked again
    assert run.nodes["downtime"].output["downtime_share_percent"] == pytest.approx(2.15)
    assert run.nodes["measures"].status == "succeeded"
    assert ollama.refused == []
