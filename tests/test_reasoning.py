"""Live reasoning through the runtime: provider stream -> Orkexa -> run events, test results and chat answers.

The reasoning a provider transmits reaches the run's events in the order it arrived (status, text, status, answer
text), for every provider and without any per-agent display switch; a provider that transmits none leaves no text.
Asking OpenAI and Gemini for summaries is the workspace setting ``reasoning_summaries``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import json
from pathlib import Path
from typing import Any

import httpx2
import openai
from orkexa import GeminiOptions, OpenAIOptions
import pytest

from nodestro_runtime.agent_test import AgentTestIn
from nodestro_runtime.providers import Connection
from nodestro_runtime.reasoning import ReasoningTrace, reasoning_hook, trace_hook
from nodestro_runtime.run_chat import RunChat, RunMessage
from nodestro_runtime.schemas import AgentConfig, RunEvent, RunIn, SettingsIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, thinking
from nodestro_runtime.workspace import Workspace

PROMPT = "You explain tides"


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Callable[..., Workspace]]:
    opened: list[Workspace] = []

    def factory(client: Any) -> Workspace:
        workspace = Workspace(tmp_path / f"data{len(opened)}", client_factory=lambda _provider: client, integrations=[])
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def _workflow(model: str = "anthropic:claude-sonnet-5-5") -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Tides",
            "nodes": [
                {"id": "s", "type": "start"},
                {"id": "a", "type": "agent", "label": "A", "agent": {"system_prompt": PROMPT, "model": model}},
                {"id": "o", "type": "output"},
            ],
            "edges": [{"id": "e1", "source": "s", "target": "a"}, {"id": "e2", "source": "a", "target": "o"}],
        }
    )


def _shape(events: list[RunEvent]) -> list[tuple[Any, ...]]:
    shape: list[tuple[Any, ...]] = []
    for event in events:
        if event.type == "thinking_status":
            shape.append(("status", event.data["state"], event.data["reasoning_kind"], event.data["redacted"]))
        elif event.type == "thinking":
            item = ("thinking", event.data["reasoning_kind"], event.data["delta"])
            if shape and shape[-1][:2] == item[:2]:
                shape[-1] = (*item[:2], shape[-1][2] + item[2])
            else:
                shape.append(item)
        elif event.type == "text":
            if shape and shape[-1][0] == "text":
                shape[-1] = ("text", shape[-1][1] + event.data["delta"])
            else:
                shape.append(("text", event.data["delta"]))
    return shape


async def test_claude_thinking_reaches_the_run_events_in_order_without_a_display_option(
    make: Callable[..., Workspace],
) -> None:
    workspace = make(ScriptedClient({PROMPT: [thinking("Tides follow the moon. The sun adds a little.", "Moon.")]}))
    events: list[RunEvent] = []
    workspace.runs.event_sink = events.append
    saved = workspace.save_workflow(_workflow())
    assert saved.nodes[1].agent is not None and saved.nodes[1].agent.thinking_display is None
    run = await workspace.wait((await workspace.run(saved, RunIn(input="tides"))).id, timeout=10)
    assert run.status == "succeeded", run.error
    assert _shape([e for e in events if e.node_id == "a"]) == [
        ("status", "started", "summary", False),
        ("thinking", "summary", "Tides follow the moon. The sun adds a little."),
        ("status", "finished", "summary", False),
        ("text", "Moon."),
    ]
    assert len([e for e in events if e.type == "thinking"]) > 1  # streamed in pieces, not at the end
    state = run.nodes["a"]
    assert state.thinking == "Tides follow the moon. The sun adds a little."
    assert (state.thinking_kind, state.thinking_phases, state.thinking_redacted) == ("summary", 1, False)


async def test_redacted_thinking_is_a_phase_without_text(make: Callable[..., Workspace]) -> None:
    workspace = make(ScriptedClient({PROMPT: [thinking(None, "Moon.")]}))
    events: list[RunEvent] = []
    workspace.runs.event_sink = events.append
    saved = workspace.save_workflow(_workflow())
    run = await workspace.wait((await workspace.run(saved, RunIn(input="tides"))).id, timeout=10)
    assert _shape([e for e in events if e.node_id == "a"]) == [
        ("status", "started", "summary", True),
        ("status", "finished", "summary", False),
        ("text", "Moon."),
    ]
    state = run.nodes["a"]
    assert (state.thinking, state.thinking_kind, state.thinking_phases, state.thinking_redacted) == ("", None, 1, True)


# ---- OpenAI over the real SDK: the workspace setting asks for summaries -------------------------


def _openai_stream(summary: str, answer: str) -> bytes:
    usage = {
        "input_tokens": 10,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 30,
        "output_tokens_details": {"reasoning_tokens": 20},
        "total_tokens": 40,
    }
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": summary}]}
    message = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": answer, "annotations": []}],
    }
    base = {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": "gpt-5.5",
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": usage,
    }
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": {**base, "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "output_index": 0, "item": {**reasoning, "summary": []}},
        {
            "type": "response.reasoning_summary_part.added",
            "output_index": 0,
            "item_id": "rs_1",
            "summary_index": 0,
            "part": {"type": "summary_text", "text": ""},
        },
    ]
    for piece in (summary[: len(summary) // 2], summary[len(summary) // 2 :]):
        events.append(
            {
                "type": "response.reasoning_summary_text.delta",
                "output_index": 0,
                "item_id": "rs_1",
                "summary_index": 0,
                "delta": piece,
            }
        )
    events += [
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
        {"type": "response.output_item.added", "output_index": 1, "item": {**message, "content": []}},
        {
            "type": "response.content_part.added",
            "output_index": 1,
            "item_id": "msg_1",
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "output_index": 1,
            "item_id": "msg_1",
            "content_index": 0,
            "delta": answer,
            "logprobs": [],
        },
        {"type": "response.output_item.done", "output_index": 1, "item": message},
        {"type": "response.completed", "response": {**base, "status": "completed", "output": [reasoning, message]}},
    ]
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps({**e, 'sequence_number': n})}\n\n" for n, e in enumerate(events)
    ).encode()


async def test_openai_summaries_follow_the_workspace_setting_through_the_whole_chain(
    make: Callable[..., Workspace],
) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        body = _openai_stream("Moon pulls water.", "Moon.")
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = openai.OpenAI(api_key="sk-test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    workspace = make(client)
    workspace.update_settings(SettingsIn.model_validate({"providers": {"openai": {"api_key": "sk-test"}}}))
    saved = workspace.save_workflow(_workflow("openai:gpt-5.5"))
    events: list[RunEvent] = []
    workspace.runs.event_sink = events.append

    run = await workspace.wait((await workspace.run(saved, RunIn(input="tides"))).id, timeout=10)
    assert run.status == "succeeded", run.error
    assert "summary" not in sent[0]["reasoning"]  # off by default
    # Without summaries the phase is still marked: the interface shows "thinking", never invented text.
    assert _shape([e for e in events if e.node_id == "a"])[0] == ("status", "started", "summary", False)

    assert workspace.update_settings(SettingsIn(reasoning_summaries=True)).reasoning_summaries is True
    events.clear()
    run = await workspace.wait((await workspace.run(saved, RunIn(input="tides"))).id, timeout=10)
    assert sent[1]["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert _shape([e for e in events if e.node_id == "a"]) == [
        ("status", "started", "summary", False),
        ("thinking", "summary", "Moon pulls water."),
        ("status", "finished", "summary", False),
        ("text", "Moon."),
    ]
    assert run.nodes["a"].thinking == "Moon pulls water."


def test_connection_asks_for_summaries_only_where_the_provider_needs_a_request() -> None:
    on = Connection(provider="openai", api_key="k", reasoning_summaries=True)
    assert on.options().reasoning_summary == "auto"
    assert on.with_reasoning_summaries(OpenAIOptions(reasoning_summary="detailed")).reasoning_summary == "detailed"
    assert Connection(provider="openai", api_key="k").options().reasoning_summary is None
    gemini = Connection(provider="gemini", api_key="k", reasoning_summaries=True)
    assert gemini.options().include_thoughts is True
    # An agent's own Gemini options (a thinking budget) get the request too.
    budget = gemini.with_reasoning_summaries(GeminiOptions(thinking_budget=512))
    assert (budget.include_thoughts, budget.thinking_budget) == (True, 512)
    anthropic = Connection(provider="anthropic", api_key="k", reasoning_summaries=True).options()
    assert anthropic.thinking_display is None  # Claude agents keep their own thinking_display


def test_reasoning_stream_info_per_provider() -> None:
    info = Connection(provider="openai", api_key="k", reasoning_summaries=True).reasoning_stream("openai:gpt-5.5")
    assert (info.text, info.status, info.request_option, info.requested) == ("summary", True, "reasoning_summary", True)
    off = Connection(provider="gemini", api_key="k").reasoning_stream("gemini:gemini-3-pro")
    assert (off.text, off.request_option, off.requested) == ("summary", "include_thoughts", False)
    none = Connection(provider="openai", api_key="k").reasoning_stream("openai:gpt-4o")
    assert none.text == "none" and none.requested is None and "not a reasoning model" in none.reason
    claude = Connection(provider="anthropic", api_key="k").reasoning_stream("anthropic:claude-sonnet-5-5")
    assert (claude.text, claude.request_option, claude.requested) == ("summary", "thinking_display", None)
    local = Connection(
        provider="ollama", base_url="http://127.0.0.1:11434/v1", reported=lambda _m: {"reasoning": "supported"}
    )
    assert local.reasoning_stream("ollama:qwen3:4b").text == "thinking"


# ---- agent tests and chat answers ---------------------------------------------------------------


def test_an_agent_test_returns_the_transmitted_reasoning(make: Callable[..., Workspace]) -> None:
    workspace = make(ScriptedClient({PROMPT: [thinking("Short plan.", "Done.")]}))
    result, _tracker = workspace.tester.run(AgentTestIn(agent=AgentConfig(system_prompt=PROMPT), input="x"))
    assert result.status == "succeeded", result.error
    assert result.reasoning.model_dump() == {
        "text": "Short plan.",
        "kind": "summary",
        "phases": 1,
        "redacted": False,
        "active": False,
    }


def test_trace_separates_later_phases_and_counts_text_without_status() -> None:
    trace = ReasoningTrace()
    hook = trace_hook(trace)
    from orkexa import ReasoningDelta, ReasoningStatus, TextDelta

    for event in (
        ReasoningStatus("started", kind="summary"),
        ReasoningDelta("one", kind="summary"),
        ReasoningStatus("finished", kind="summary"),
        TextDelta("answer"),
        ReasoningStatus("started", kind="summary"),
        ReasoningDelta("two", kind="summary"),
    ):
        hook(event)
    assert (trace.text, trace.phases, trace.active) == ("one\n\ntwo", 2, True)
    plain = ReasoningTrace()
    assert plain.delta("raw", "thinking") == "raw" and plain.phases == 1
    seen: list[Any] = []
    reasoning_hook(lambda text, kind: seen.append((text, kind)))(ReasoningDelta("", kind="thinking"))
    assert seen == []  # empty deltas are not passed on


def test_live_answers_are_copies_and_end_with_the_answer() -> None:
    from nodestro_runtime.storage import DocumentStore  # noqa: F401  # the chat needs no store for live state

    chat = RunChat.__new__(RunChat)
    RunChat.__init__(chat, store=None)  # type: ignore[arg-type]
    question = RunMessage(run_id="r", target_run_id="r", node_id="n", kind="question", content="?", status="pending")
    chat.begin_answer(question)
    chat.update_live(question.id, lambda live: setattr(live, "text", "Hal"))
    chat.update_live(question.id, lambda live: live.reasoning.delta("plan", "summary"))
    (live,) = chat.live_answers("r")
    assert (live.text, live.reasoning.text) == ("Hal", "plan")
    live.text = "changed"
    assert chat.live_answers("r")[0].text == "Hal"
    assert chat.live_answers("other") == []
    chat.end_answer(question.id)
    assert chat.live_answers("r") == []
