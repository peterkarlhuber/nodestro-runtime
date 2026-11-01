"""Live run chat with Amazon Bedrock models (stubbed Converse, never a network call): a team leader on Bedrock takes
an instruction at its next decision while its team works, and a question is answered by a Bedrock assistant model
with its cost in the usage log under ``run_chat``, not in the run."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
import json
from pathlib import Path
import threading
from typing import Any

from bedrock_support import ScriptedRuntime, no_network, stream_events
import pytest

from nodestro_runtime.run_chat import answer_question, targets_of
from nodestro_runtime.schemas import RunIn, SettingsIn, WorkflowIn
from nodestro_runtime.workspace import Workspace

__all__ = ["no_network"]  # the autouse fixture that refuses every AWS request

CLAUDE = "bedrock:eu.anthropic.claude-sonnet-5-5"
BOSS = "You coordinate teams"
CHAT = "You answer a person's question about a workflow run"


class RoutedRuntime(ScriptedRuntime):
    """Answers by the system prompt: the team leader's decisions, the chat answer, and a team that waits."""

    def __init__(self) -> None:
        super().__init__([])
        self.decisions: list[dict[str, Any]] = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.lock = threading.Lock()

    def _stream(self, **params: Any) -> Any:
        with self.lock:
            self.calls.append(params)
        system = " ".join(str(block.get("text", "")) for block in params.get("system") or [])
        if system.startswith(BOSS):
            with self.lock:
                content = [{"text": json.dumps(self.decisions.pop(0))}]
        elif system.startswith(CHAT):
            content = [{"text": "One team is drafting; no result yet."}]
        else:
            self.entered.set()
            assert self.release.wait(20), "the test never released the team"
            content = [{"text": "draft"}]
        return {"stream": iter(stream_events(content, "end_turn")), "ResponseMetadata": {"RequestId": "req"}}


def decision(**values: Any) -> dict[str, Any]:
    return {"action": "finish", "member": None, "task": None, "answer": None, "reason": "test", **values}


@pytest.fixture
def runtime() -> RoutedRuntime:
    return RoutedRuntime()


@pytest.fixture
def workspace(tmp_path: Path, runtime: RoutedRuntime) -> Iterator[Workspace]:
    opened = Workspace(
        tmp_path / "ws",
        integrations=[],
        client_factory=lambda provider: runtime.client if provider == "bedrock" else None,
    )
    opened.update_settings(
        SettingsIn.model_validate({"providers": {"bedrock": {"region": "eu-central-1"}}, "assistant_model": CLAUDE})
    )
    yield opened
    opened.close()


async def test_a_bedrock_team_leader_takes_guidance_and_a_bedrock_model_answers(
    workspace: Workspace, runtime: RoutedRuntime
) -> None:
    team = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Writer",
                "description": "writes",
                "nodes": [
                    {"id": "s", "type": "start"},
                    {"id": "w", "type": "agent", "label": "Writer", "agent": {"name": "W", "model": CLAUDE}},
                ],
                "edges": [{"id": "e", "source": "s", "target": "w"}],
            }
        )
    )
    lead = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Lead",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {
                        "id": "lead",
                        "type": "orchestrator",
                        "label": "Lead",
                        "orchestrator": {"members": [{"workflow_id": team.id}], "model": CLAUDE},
                    },
                ],
                "edges": [{"id": "e", "source": "start", "target": "lead"}],
            }
        )
    )
    runtime.decisions = [
        decision(action="delegate", member=team.id, task="Draft", reason="start"),
        decision(answer="final with summary", reason="as asked"),
    ]
    started = await workspace.run(lead, RunIn(input="x"))
    for _ in range(750):
        if runtime.entered.is_set():
            break
        await asyncio.sleep(0.02)
    assert runtime.entered.is_set(), "the team never started"

    live = workspace.runs.get(started.id)
    assert live is not None
    (target,) = targets_of(workspace.runs.get, live)
    assert target.steering and target.deciding  # a person started it in the studio (attended)
    instruction, _ = workspace.runs.chat.submit(
        run_id=started.id, target_run_id=started.id, node_id="lead", kind="instruction", content="Add a summary"
    )
    question, _ = workspace.runs.chat.submit(
        run_id=started.id, target_run_id=started.id, node_id="lead", kind="question", content="Progress?"
    )
    answer = await asyncio.to_thread(
        answer_question, workspace.runs, workspace.runs.chat, workspace.usage_log, question, model=CLAUDE
    )
    assert answer.kind == "answer", (answer.status, answer.code)
    assert answer.content == "One team is drafting; no result yet."
    runtime.release.set()
    done = await workspace.wait(started.id, timeout=20)
    assert done.status == "succeeded", done.error

    boss_calls = [call for call in runtime.calls if BOSS in json.dumps(call.get("system"))]
    assert all(call["modelId"] == "eu.anthropic.claude-sonnet-5-5" for call in boss_calls)
    assert "Add a summary" in json.dumps(boss_calls[1]["messages"]) and "<<<guidance" not in json.dumps(boss_calls[0])
    stored = workspace.runs.chat.get(started.id, instruction.id)
    assert stored is not None and stored.status == "considered" and stored.step == 1
    chat_entries = [entry for entry in workspace.usage_log.entries() if entry.source == "run_chat"]
    assert len(chat_entries) == 1 and chat_entries[0].model == CLAUDE
    assert done.usage.requests == 3  # two decisions and the team; the chat answer is not part of the run
    runtime.stubber.assert_no_pending_responses()
