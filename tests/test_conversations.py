"""Conversations with own agents (N-04): every answer is a run of the one engine.

Covered: create, resume after a restart, rename, delete and the Markdown export; switching agent and model per
conversation, and that a cloud model is never chosen automatically; cancelling an answer ends the model connection;
tool calls with arguments, result and duration; the cost of a conversation is the sum of its runs; an image goes only
to a model with image input; per-call approvals and conversation grants (N-06) inside a conversation. Tenants on
PostgreSQL: ``test_conversations_postgres``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
import dataclasses
from pathlib import Path
import socket
import struct
import threading
import time
from typing import Any
import zlib

import pytest

from nodestro_runtime import engine
from nodestro_runtime.agent_templates import AgentTemplates
from nodestro_runtime.conversations import (
    AGENT_NODE,
    COLLECTION,
    MESSAGES,
    AgentChoice,
    ConversationChange,
    ConversationIn,
    ConversationMessage,
    ConversationNotFound,
    MessageIn,
    answer_input,
    conversations_enabled,
)
from nodestro_runtime.local_models import LOCAL_DEFAULT_MODEL
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import AgentConfig, Run, SettingsIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.workspace import Workspace

CLOUD = "anthropic:claude-sonnet-5-5"
OTHER_CLOUD = "anthropic:claude-opus-5-5"
Make = Callable[..., Workspace]


def png() -> bytes:
    """A valid 1x1 PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]] | None = None, *, data: str = "data") -> Workspace:
        fake = ScriptedClient(script or {})
        workspace = Workspace(tmp_path / data, client_factory=lambda _provider: fake, integrations=[])
        workspace.fake = fake  # type: ignore[attr-defined]
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def template(workspace: Workspace, prompt: str = "AGENT", **fields: Any) -> str:
    agent = AgentConfig(name=prompt.title(), system_prompt=prompt, **fields)
    return AgentTemplates(workspace.store).create(agent).id


def start(workspace: Workspace, template_id: str, model: str | None = CLOUD, **fields: Any) -> str:
    created = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template_id), model=model, **fields), owner=None
    )
    return created.id


async def answer(workspace: Workspace, conversation_id: str, content: str, **fields: Any) -> ConversationMessage:
    sent = await workspace.conversations.send(conversation_id, None, MessageIn(content=content, **fields))
    assert sent.assistant.run_id is not None
    await workspace.wait(sent.assistant.run_id, timeout=20)
    return workspace.conversations.message(conversation_id, None, sent.assistant.id)


def requests(workspace: Workspace) -> list[dict[str, Any]]:
    return workspace.fake.messages.calls  # type: ignore[attr-defined,no-any-return]


def user_text(request: dict[str, Any]) -> str:
    parts = []
    for message in request["messages"]:
        content = message["content"]
        if isinstance(content, str):
            parts.append(content)
        else:
            parts += [block.get("text", "") for block in content if isinstance(block, dict)]
    return "\n".join(parts)


def test_the_switch_is_off_by_default() -> None:
    assert conversations_enabled({}) is False
    assert conversations_enabled({"NODESTRO_CONVERSATIONS": "0"}) is False
    assert conversations_enabled({"NODESTRO_CONVERSATIONS": "1"}) is True


# ---- create, resume after a restart, rename, delete, export ------------------------------------------------------


async def test_create_resume_after_a_restart_rename_delete_and_export(make: Make) -> None:
    first = make({"AGENT": [text("Vienna."), text("About 2 million.")]})
    template_id = template(first)
    conversation_id = start(first, template_id)
    created = first.conversations.get(conversation_id, None)
    assert created.tenant_id == first.tenant_id and created.owner is None and created.provenance == "unchecked"
    assert (created.agent.template_id, created.agent.version, created.model) == (template_id, 1, CLOUD)
    replied = await answer(first, conversation_id, "Capital of Austria?")
    assert (replied.status, replied.content) == ("succeeded", "Vienna.")
    assert first.conversations.get(conversation_id, None).title == "Capital of Austria?"
    first.close()

    second = make({"AGENT": [text("About 2 million.")]})  # the same data directory: Nodestro restarted
    resumed = second.conversations.detail(conversation_id, None)
    assert [(item.role, item.content) for item in resumed.messages] == [
        ("user", "Capital of Austria?"),
        ("assistant", "Vienna."),
    ]
    follow_up = await answer(second, conversation_id, "And its population?")
    assert follow_up.content == "About 2 million."
    assert "Capital of Austria?" in user_text(requests(second)[0]) and "Vienna." in user_text(requests(second)[0])

    renamed = second.conversations.update(conversation_id, None, ConversationChange(title="Austria"))
    assert renamed.title == "Austria" and renamed.auto_title is False
    assert renamed.model == CLOUD  # a rename changes nothing else

    exported = second.conversations.export_markdown(conversation_id, None)
    assert exported.startswith("# Austria\n")
    assert "## User" in exported and "Capital of Austria?" in exported and "About 2 million." in exported
    assert f"Run {follow_up.run_id}" in exported

    second.conversations.delete(conversation_id, None)
    with pytest.raises(ConversationNotFound):
        second.conversations.get(conversation_id, None)
    assert second.store.list_prefix(MESSAGES, f"{conversation_id}:", ConversationMessage) == []
    assert second.runs.get(follow_up.run_id or "") is not None  # the run stays, with its cost


async def test_a_conversation_belongs_to_its_owner(make: Make) -> None:
    workspace = make()
    template_id = template(workspace)
    mine = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template_id), model=CLOUD), owner="usr_alice"
    )
    assert [item.id for item in workspace.conversations.summaries("usr_alice")] == [mine.id]
    assert workspace.conversations.summaries("usr_bob") == []
    for call in (
        lambda: workspace.conversations.get(mine.id, "usr_bob"),
        lambda: workspace.conversations.update(mine.id, "usr_bob", ConversationChange(title="x")),
        lambda: workspace.conversations.delete(mine.id, "usr_bob"),
        lambda: workspace.conversations.export_markdown(mine.id, "usr_bob"),
    ):
        with pytest.raises(ConversationNotFound):
            call()
    with pytest.raises(ConversationNotFound):
        await workspace.conversations.send(mine.id, "usr_bob", MessageIn(content="hi"))
    # A document of another tenant in this store (a copied database) is never read either.
    raw = workspace.store.get_raw(COLLECTION, mine.id)
    workspace.store.put_raw(COLLECTION, mine.id, {**raw, "tenant_id": "other"})
    with pytest.raises(ConversationNotFound):
        workspace.conversations.get(mine.id, "usr_alice")


# ---- every answer is a run; cost; tool calls ---------------------------------------------------------------------


async def test_every_answer_is_a_run_and_the_cost_is_the_sum_of_the_runs(make: Make) -> None:
    workspace = make({"AGENT": [text("One."), text("Two.")]})
    conversation_id = start(workspace, template(workspace))
    first = await answer(workspace, conversation_id, "First")
    second = await answer(workspace, conversation_id, "Second")
    runs = [workspace.runs.get(item.run_id or "") for item in (first, second)]
    assert all(isinstance(run, Run) for run in runs)
    for run, message in zip(runs, (first, second), strict=True):
        assert run is not None
        assert (run.trigger, run.trigger_ref, run.workflow_id, run.status) == (
            "conversation",
            conversation_id,
            conversation_id,
            "succeeded",
        )
        assert [node.id for node in run.workflow.nodes] == ["start", AGENT_NODE]
        assert run.workflow.notifications.events == []
        assert message.usage == run.usage and message.cost_usd == run.usage.estimated_cost_usd
    costs = [run.usage.estimated_cost_usd for run in runs if run is not None]
    assert all(cost is not None and cost > 0 for cost in costs)
    total = workspace.conversations.usage(conversation_id, None)
    assert total.estimated_cost_usd == pytest.approx(sum(cost or 0.0 for cost in costs))
    assert total.requests == sum(run.usage.requests for run in runs if run is not None)
    assert workspace.conversations.summaries(None)[0].cost_usd == pytest.approx(total.estimated_cost_usd)
    # The runs are in the run list like every other run (their usage is the ledger of runs).
    listed = {item.id for item in workspace.store.find("runs", "trigger_ref", [conversation_id], Run)}
    assert listed == {first.run_id, second.run_id}


async def test_tool_calls_show_their_arguments_result_and_duration(make: Make) -> None:
    script = {"AGENT": [tool_use("calculator", {"expression": "6 * 7"}), text("It is 42.")]}
    workspace = make(script)
    conversation_id = start(workspace, template(workspace, tools=["calculator"]))
    replied = await answer(workspace, conversation_id, "What is 6 times 7?")
    assert replied.content == "It is 42."
    [call] = replied.tool_calls
    assert (call.name, call.arguments, call.is_error) == ("calculator", {"expression": "6 * 7"}, False)
    assert call.result is not None and "42" in call.result
    assert call.finished_at is not None and call.duration_ms is not None and call.duration_ms >= 0
    exported = workspace.conversations.export_markdown(conversation_id, None)
    assert "`calculator`" in exported and '{"expression": "6 * 7"}' in exported


async def test_one_answer_at_a_time_and_the_budget_of_the_conversation(make: Make) -> None:
    workspace = make({"AGENT": [text("One.")]})
    conversation_id = start(workspace, template(workspace), budget_usd=0.0)
    with pytest.raises(NodestroError) as caught:
        await workspace.conversations.send(conversation_id, None, MessageIn(content="hi"))
    assert caught.value.code == "conversation.budget_exhausted"
    with pytest.raises(NodestroError) as caught:
        await workspace.conversations.send(conversation_id, None, MessageIn(content="  "))
    assert caught.value.code == "conversation.empty_message"
    assert workspace.conversations.messages(conversation_id, None) == []  # nothing stored
    workspace.conversations.update(conversation_id, None, ConversationChange(budget_usd=1.0))
    replied = await answer(workspace, conversation_id, "hi")
    run = workspace.runs.get(replied.run_id or "")
    assert run is not None and run.budget_usd == pytest.approx(1.0)


# ---- agent and model per conversation; never a cloud model automatically ---------------------------------------


async def test_agent_and_model_switch_per_conversation(make: Make) -> None:
    workspace = make({"FIRST": [text("first agent")], "SECOND": [text("second agent")]})
    first_id, second_id = template(workspace, "FIRST"), template(workspace, "SECOND")
    conversation_id = start(workspace, first_id)
    one = await answer(workspace, conversation_id, "Hello")
    switched = workspace.conversations.update(
        conversation_id, None, ConversationChange(agent=AgentChoice(template_id=second_id), model=OTHER_CLOUD)
    )
    assert (switched.agent.template_id, switched.model) == (second_id, OTHER_CLOUD)
    two = await answer(workspace, conversation_id, "Hello again")
    assert (one.content, one.agent and one.agent.template_id, one.model) == ("first agent", first_id, CLOUD)
    assert (two.content, two.agent and two.agent.template_id, two.model) == ("second agent", second_id, OTHER_CLOUD)
    calls = requests(workspace)
    assert [call["model"] for call in calls] == ["claude-sonnet-5-5", "claude-opus-5-5"]
    # Another conversation keeps its own agent and model.
    other = workspace.conversations.get(start(workspace, first_id), None)
    assert (other.agent.template_id, other.model) == (first_id, CLOUD)
    # A pinned template version stays until it is switched, also after the template changed.
    AgentTemplates(workspace.store).save(first_id, AgentConfig(name="Changed", system_prompt="FIRST v2"))
    assert workspace.conversations.get(other.id, None).agent.version == 1
    latest = workspace.conversations.update(other.id, None, ConversationChange(agent=AgentChoice(template_id=first_id)))
    assert latest.agent.version == 2
    with pytest.raises(NodestroError) as caught:
        workspace.conversations.update(other.id, None, ConversationChange(agent=AgentChoice(template_id="tpl_missing")))
    assert caught.value.code == "conversation.agent_not_found"


def test_a_cloud_model_is_never_chosen_automatically(make: Make) -> None:
    workspace = make()
    assert workspace.settings().default_model == LOCAL_DEFAULT_MODEL  # a new installation: local by default
    # The agent's own model is a cloud model with a cloud fallback: the conversation still takes the local default.
    cloudy = template(workspace, model=CLOUD, fallbacks=[OTHER_CLOUD])
    conversation = workspace.conversations.get(start(workspace, cloudy, model=None), None)
    assert conversation.model == LOCAL_DEFAULT_MODEL
    # A local agent model is taken as it is.
    local = template(workspace, model="ollama:llama3.2:3b", effort=None)
    assert workspace.conversations.get(start(workspace, local, model=None), None).model == "ollama:llama3.2:3b"
    # Without a local default, a person has to choose: no cloud model is picked.
    workspace.update_settings(SettingsIn.model_validate({"default_model": CLOUD}))
    with pytest.raises(NodestroError) as caught:
        start(workspace, cloudy, model=None)
    assert caught.value.code == "conversation.model_required"
    # Chosen explicitly, a cloud model answers; an offline conversation refuses it.
    assert workspace.conversations.get(start(workspace, cloudy, model=CLOUD), None).model == CLOUD
    with pytest.raises(NodestroError) as caught:
        start(workspace, cloudy, model=CLOUD, offline=True)
    assert caught.value.code == "conversation.offline_cloud_model"
    offline = start(workspace, cloudy, model=LOCAL_DEFAULT_MODEL, offline=True)
    with pytest.raises(NodestroError) as caught:
        workspace.conversations.update(offline, None, ConversationChange(model=CLOUD))
    assert caught.value.code == "conversation.offline_cloud_model"


# ---- cancel ends the model connection ------------------------------------------------------------------------------


@pytest.fixture
def silent_server() -> Iterator[int]:
    """A local model server that accepts the connection and never answers (as in RT3-05)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except OSError:
                return
            held.append(connection)

    threading.Thread(target=accept, daemon=True).start()
    yield server.getsockname()[1]
    stop.set()
    server.close()
    for connection in held:
        connection.close()


async def test_cancel_ends_the_model_connection_of_a_local_default_model(
    tmp_path: Path, silent_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local default model with a cloud fallback configured on the agent: the answer uses the local model only,
    and cancelling it closes the model connection through the run's CancelFlag."""
    from orkexa.local import _http

    def no_answer(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise _http.OllamaError("unreachable", "no answer")

    monkeypatch.setattr(_http.OllamaHTTP, "request", no_answer)
    interrupted: list[Any] = []
    original = engine.interrupt_client

    def recording(client: Any) -> bool:
        done = original(client)
        interrupted.append(done)
        return done

    monkeypatch.setattr(engine, "interrupt_client", recording)
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        base_url = f"http://127.0.0.1:{silent_server}/v1"
        workspace.store.put_raw("settings", "app", {"providers": {"ollama": {"base_url": base_url}}})
        template_id = template(workspace, model=LOCAL_DEFAULT_MODEL, fallbacks=[CLOUD])
        conversation_id = start(workspace, template_id, model=None)
        sent = await workspace.conversations.send(conversation_id, None, MessageIn(content="hi"))
        run = workspace.runs.get(sent.assistant.run_id or "")
        assert run is not None
        agent = run.workflow.nodes[1].agent
        assert agent is not None and (agent.model, agent.fallbacks) == (LOCAL_DEFAULT_MODEL, [])
        await asyncio.sleep(1.0)  # the request is sent and waits
        assert workspace.conversations.message(conversation_id, None, sent.assistant.id).status == "running"
        workspace.conversations.cancel(conversation_id, None, sent.assistant.id)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = workspace.conversations.message(conversation_id, None, sent.assistant.id)
            if current.status == "cancelled":
                break
            await asyncio.sleep(0.1)
        assert current.status == "cancelled"
        assert True in interrupted  # the model connection was closed, not left to its timeout
        # The cancelled answer is final: the next message may be sent.
        assert workspace.conversations.detail(conversation_id, None).answering is None
    finally:
        workspace.close()


# ---- files: an image only to a model with image input -------------------------------------------------------------


async def test_an_image_goes_only_to_a_model_with_image_input(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = make({"AGENT": [text("A black dot."), text("I cannot see images.")]})
    seeing = workspace.builder.capabilities

    def capabilities(model: str) -> Any:
        found = seeing(model)
        return dataclasses.replace(found, image_input="unsupported") if model == OTHER_CLOUD else found

    monkeypatch.setattr(workspace.builder, "capabilities", capabilities)
    conversation_id = start(workspace, template(workspace))
    added = workspace.conversations.add_file(
        conversation_id, None, png(), name="dot.png", media_type="image/png", uploaded_by="local"
    )
    assert workspace.inputs.get(added.id).origin == "conversation"
    first = await answer(workspace, conversation_id, "What is this?", file_ids=[added.id])
    workspace.conversations.update(conversation_id, None, ConversationChange(model=OTHER_CLOUD))
    second = await answer(workspace, conversation_id, "And now?", file_ids=[added.id])
    assert (first.status, second.status) == ("succeeded", "succeeded")

    def images(request: dict[str, Any]) -> int:
        return sum(
            1
            for message in request["messages"]
            if isinstance(message["content"], list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "image"
        )

    calls = requests(workspace)
    assert [images(call) for call in calls] == [1, 0]
    messages = workspace.conversations.messages(conversation_id, None)
    modes = [item.attachments[0].mode for item in messages if item.role == "user"]
    assert modes == ["image", "unavailable"]
    # The file belongs to the conversation: it is kept by the clean-up and removed with the conversation.
    assert workspace.inputs.references()[added.id] >= {"conversation"}
    with pytest.raises(ConversationNotFound):
        await workspace.conversations.send(conversation_id, None, MessageIn(content="x", file_ids=["in_000000000000"]))
    workspace.conversations.delete(conversation_id, None)
    assert added.id not in workspace.inputs


def test_the_history_holds_the_finished_messages_only() -> None:
    def message(seq: int, role: str, content: str, status: str = "complete") -> ConversationMessage:
        return ConversationMessage(conversation_id="conv_x", seq=seq, role=role, content=content, status=status)  # type: ignore[arg-type]

    history = [
        message(1, "user", "Hi"),
        message(2, "assistant", "Hello", "succeeded"),
        message(3, "user", "Fail"),
        message(4, "assistant", "partial", "failed"),
    ]
    built = answer_input(history, "Again")
    assert "User: Hi" in built and "Assistant: Hello" in built and "User: Fail" in built
    assert "partial" not in built and built.endswith("The user's new message:\nAgain")
    assert answer_input([], "Only") == "Only"


# ---- approvals and grants inside a conversation (the existing S-2 and N-06 mechanism) ------------------------------


async def test_a_tool_approval_in_a_conversation_and_a_grant_for_this_conversation(make: Make) -> None:
    from nodestro_runtime.tool_approvals import AccessRules, Person, ToolApprovals, ToolGate, ToolPreGrants
    from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
    from nodestro_runtime.tool_policy import AccessResource, ToolFacts

    tool = "celsius_to_fahrenheit"
    workspace = make({"AGENT": [tool_use(tool, {"celsius": 100}), text("212 F.")] * 2})
    saved, _ = workspace.tools.save_custom(
        CUSTOM_TOOL_TEMPLATE,
        CUSTOM_TESTS_TEMPLATE,
        tool,
        trust=workspace.tools.editor_trust(tool, CUSTOM_TOOL_TEMPLATE),
    )
    assert saved.ok
    rules = AccessRules(workspace.store)
    workspace.runs.tool_gate = ToolGate(
        ToolApprovals(workspace.store, poll_seconds=0.02),
        pre_grants=ToolPreGrants(workspace.store).of_scope,
        access=rules.current,
        protected_folders=(str(workspace.data_dir),),
    )
    conversation_id = start(workspace, template(workspace, tools=[tool]))

    # Without a grant the call waits for a person; the answer references the approval request.
    sent = await workspace.conversations.send(conversation_id, None, MessageIn(content="100 C in F?"))
    run_id = sent.assistant.run_id or ""
    deadline = time.monotonic() + 10
    waiting = sent.assistant
    while not waiting.approval_ids and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        waiting = workspace.conversations.message(conversation_id, None, sent.assistant.id)
    [request_id] = waiting.approval_ids
    request = ToolApprovals(workspace.store).get(run_id, request_id)
    assert request is not None and request.tool == tool
    workspace.runs.decide_tool_call(run_id, request_id, approved=True, by="local", arguments=request.arguments_hash)
    await workspace.wait(run_id, timeout=20)
    approved = workspace.conversations.message(conversation_id, None, sent.assistant.id)
    assert approved.status == "succeeded" and approved.approval_ids == []
    assert approved.tool_calls[0].approval is not None and approved.tool_calls[0].approval.status == "approved"

    # A grant "for this conversation" names the conversation and holds for its later answers (each its own run).
    info = workspace.tools.get(tool)
    granted = rules.grant(
        resource=AccessResource(type="tool", id=tool),
        tool=ToolFacts.of(info.capability, version=info.version),
        actions=["execute"],
        duration="conversation",
        conversation_id=conversation_id,
        by=Person("usr_alice"),
    )
    again = await answer(workspace, conversation_id, "And again?")
    approval = again.tool_calls[0].approval
    assert again.status == "succeeded" and approval is not None
    assert (approval.status, approval.access_source) == ("allowed", f"grant:{granted.id}")
