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
    NO_OWNER,
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
from nodestro_runtime.schemas import AgentConfig, Edge, Node, Run, RunIn, RunSummary, SettingsIn, Workflow
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

    await second.conversations.delete(conversation_id, None)
    with pytest.raises(ConversationNotFound):
        second.conversations.get(conversation_id, None)
    assert second.store.list_prefix(MESSAGES, f"{conversation_id}:", ConversationMessage) == []


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
        lambda: workspace.conversations.export_markdown(mine.id, "usr_bob"),
    ):
        with pytest.raises(ConversationNotFound):
            call()
    with pytest.raises(ConversationNotFound):
        await workspace.conversations.delete(mine.id, "usr_bob")
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
    await workspace.conversations.delete(conversation_id, None)
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


# ---- review fixes (independent review of N-04) ---------------------------------------------------------------------


async def test_h1_the_runs_of_an_answer_belong_to_the_conversation_and_go_with_it(make: Make) -> None:
    """H-1: an answer's run holds the conversation (input), the answer (output) and the files. Only who sees the
    conversation sees the run; deleting the conversation deletes its runs with their files and checkpoints."""
    workspace = make({"AGENT": [text("Noted."), text("Plain.")]})
    template_id = template(workspace)
    mine = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template_id), model=CLOUD), owner="usr_alice"
    )
    added = workspace.conversations.add_file(
        mine.id, "usr_alice", b"secret notes", name="notes.txt", media_type="text/plain", uploaded_by="usr_alice"
    )
    sent = await workspace.conversations.send(mine.id, "usr_alice", MessageIn(content="Keep this", file_ids=[added.id]))
    run_id = sent.assistant.run_id or ""
    await workspace.wait(run_id, timeout=20)
    assert workspace.conversations.run_visible(run_id, "usr_alice") is True
    assert workspace.conversations.run_visible(run_id, None) is True  # the desktop's only person
    assert workspace.conversations.run_visible(run_id, "usr_bob") is False
    agent = AgentConfig(name="Plain", system_prompt="AGENT", model=CLOUD)
    plain = Workflow(
        name="Plain",
        nodes=[Node(id="start", type="start"), Node(id="agent", type="agent", agent=agent)],
        edges=[Edge(id="e1", source="start", target="agent")],
    )
    other = await workspace.run(workspace.save_workflow(plain), RunIn(input="x"))
    await workspace.wait(other.id, timeout=20)
    assert workspace.conversations.run_visible(other.id, "usr_bob") is True  # other runs are not affected
    assert workspace.conversations.run_visible("run_unknown", "usr_bob") is True

    await workspace.conversations.delete(mine.id, "usr_alice")
    assert workspace.runs.get(run_id) is None
    assert workspace.store.find("runs", "trigger_ref", [mine.id], Run) == []
    assert added.id not in workspace.inputs
    assert workspace.conversations.run_visible(run_id, None) is True  # unknown now: the run routes answer 404


async def test_h1_a_run_left_by_a_deleted_conversation_is_hidden_from_everyone(make: Make) -> None:
    workspace = make({"AGENT": [text("Hi.")]})
    conversation_id = start(workspace, template(workspace))
    replied = await answer(workspace, conversation_id, "Hello")
    workspace.store.delete(COLLECTION, conversation_id)  # e.g. a run still going when it was deleted
    assert workspace.conversations.run_visible(replied.run_id or "", None) is False


async def test_l2_deleting_a_conversation_revokes_its_conversation_grants(make: Make) -> None:
    from nodestro_runtime.tool_approvals import AccessRules, Person
    from nodestro_runtime.tool_policy import AccessResource, ToolFacts

    workspace = make()
    conversation_id = start(workspace, template(workspace))
    info = workspace.tools.get("calculator")
    rules = AccessRules(workspace.store)

    def grant(conversation: str) -> str:
        return rules.grant(
            resource=AccessResource(type="tool", id="calculator"),
            tool=ToolFacts.of(info.capability, version=info.version),
            actions=["execute"],
            duration="conversation",
            conversation_id=conversation,
            by=Person("usr_alice"),
        ).id

    mine, elsewhere = grant(conversation_id), grant("conv_other")
    await workspace.conversations.delete(conversation_id, None)
    revoked = rules.revoke_conversation(conversation_id, by=Person("usr_alice"))  # what the delete route does
    assert [item.id for item in revoked] == [mine]
    status = {item.id: item.status for item in rules.grants()}
    assert (status[mine], status[elsewhere]) == ("revoked", "active")


def test_h2_no_quality_check_with_a_cloud_judge_in_an_answer() -> None:
    """H-2: the judge model defaults to a cloud model; a conversation never uses it without being told."""
    from nodestro_runtime.conversations import answer_agent
    from nodestro_runtime.schemas import QualityConfig, TemplateRef

    ref = TemplateRef(template_id="tpl_x", version=1)
    judged = AgentConfig(name="A", quality=QualityConfig(enabled=True))
    assert not judged.quality.judge_model.startswith("ollama:")
    assert answer_agent(judged, model=LOCAL_DEFAULT_MODEL, ref=ref).quality.enabled is False
    local = AgentConfig(name="A", quality=QualityConfig(enabled=True, judge_model=LOCAL_DEFAULT_MODEL))
    assert answer_agent(local, model=LOCAL_DEFAULT_MODEL, ref=ref).quality.enabled is True


async def test_h2_an_answer_runs_without_the_cloud_judge(make: Make) -> None:
    from nodestro_runtime.schemas import QualityConfig

    workspace = make({"AGENT": [text("Fine.")]})
    template_id = template(workspace, quality=QualityConfig(enabled=True, judge_model=OTHER_CLOUD))
    replied = await answer(workspace, start(workspace, template_id), "Check this")
    run = workspace.runs.get(replied.run_id or "")
    assert run is not None
    agent = run.workflow.nodes[1].agent
    assert agent is not None and agent.quality.enabled is False
    assert [call["model"] for call in requests(workspace)] == ["claude-sonnet-5-5"]  # no judge request


def test_h2_every_model_of_the_answer_is_checked_before_it_starts() -> None:
    from nodestro_runtime.conversations import check_answer_models
    from nodestro_runtime.schemas import QualityConfig

    def workflow(model: str, judge: str) -> Workflow:
        agent = AgentConfig(name="A", model=model, quality=QualityConfig(enabled=True, judge_model=judge))
        return Workflow(name="W", nodes=[Node(id="start", type="start"), Node(id="agent", type="agent", agent=agent)])

    check_answer_models(workflow(CLOUD, LOCAL_DEFAULT_MODEL), model=CLOUD, offline=False)
    check_answer_models(workflow(LOCAL_DEFAULT_MODEL, LOCAL_DEFAULT_MODEL), model=LOCAL_DEFAULT_MODEL, offline=True)
    for given, offline, code in (
        (workflow(CLOUD, OTHER_CLOUD), False, "conversation.cloud_model_not_chosen"),
        (workflow(LOCAL_DEFAULT_MODEL, OTHER_CLOUD), True, "conversation.offline_cloud_model"),
        (workflow(LOCAL_DEFAULT_MODEL, OTHER_CLOUD), False, "conversation.cloud_model_not_chosen"),
    ):
        with pytest.raises(NodestroError) as caught:
            check_answer_models(given, model=LOCAL_DEFAULT_MODEL if offline else CLOUD, offline=offline)
        assert caught.value.code == code


async def test_h2_a_refused_model_check_stores_nothing(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    from nodestro_runtime import conversations as module

    workspace = make()
    conversation_id = start(workspace, template(workspace))

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise NodestroError("conversation.cloud_model_not_chosen", "refused")

    monkeypatch.setattr(module, "check_answer_models", refuse)
    with pytest.raises(NodestroError):
        await workspace.conversations.send(conversation_id, None, MessageIn(content="hi"))
    assert workspace.conversations.messages(conversation_id, None) == []
    assert workspace.conversations.detail(conversation_id, None).answering is None


async def test_m1_a_change_never_writes_back_an_older_conversation(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    """M-1: while one process switches the agent, another one attaches a file; the switch must not drop it."""
    from nodestro_runtime.conversations import Conversations

    workspace = make()
    first_id, second_id = template(workspace, "FIRST"), template(workspace, "SECOND")
    conversation_id = start(workspace, first_id)
    other_process = Conversations(workspace)
    resolve = workspace.conversations._agent

    def resolve_while_another_process_writes(choice: AgentChoice) -> Any:
        found = resolve(choice)
        other_process.add_file(
            conversation_id, None, b"late", name="late.txt", media_type="text/plain", uploaded_by="local"
        )
        return found

    monkeypatch.setattr(workspace.conversations, "_agent", resolve_while_another_process_writes)
    switched = workspace.conversations.update(
        conversation_id, None, ConversationChange(agent=AgentChoice(template_id=second_id))
    )
    assert switched.agent.template_id == second_id
    assert [item.name for item in switched.files] == ["late.txt"]


async def test_m2_two_processes_never_answer_at_once(make: Make) -> None:
    """M-2: two sends of different processes (here two objects with their own locks) at the same moment: one answer
    starts, the other is refused, and the budget is checked under the store lock."""
    from nodestro_runtime.conversations import Conversations

    workspace = make({"AGENT": [text("One."), text("Two.")]})
    conversation_id = start(workspace, template(workspace))
    other_process = Conversations(workspace)
    results = await asyncio.gather(
        workspace.conversations.send(conversation_id, None, MessageIn(content="first")),
        other_process.send(conversation_id, None, MessageIn(content="second")),
        return_exceptions=True,
    )
    refused = [item for item in results if isinstance(item, NodestroError)]
    started = [item for item in results if not isinstance(item, BaseException)]
    assert len(started) == 1 and [item.code for item in refused] == ["conversation.answer_running"]
    runs = workspace.store.find("runs", "trigger_ref", [conversation_id], Run)
    assert len(runs) == 1


async def test_m2_a_taken_message_key_is_never_overwritten(make: Make) -> None:
    workspace = make()
    conversation_id = start(workspace, template(workspace))
    taken = ConversationMessage(conversation_id=conversation_id, seq=1, role="user", content="other process")
    workspace.store.put(MESSAGES, f"{conversation_id}:000001", taken)  # written with a newer last_seq elsewhere
    with pytest.raises(NodestroError) as caught:
        await workspace.conversations.send(conversation_id, None, MessageIn(content="mine"))
    assert caught.value.code == "conversation.answer_running"
    kept = workspace.store.get(MESSAGES, f"{conversation_id}:000001", ConversationMessage)
    assert kept is not None and kept.content == "other process"


async def test_m2_a_reservation_whose_run_never_started_expires(make: Make) -> None:
    from datetime import timedelta

    from nodestro_runtime.conversations import RESERVATION_TIMEOUT
    from nodestro_runtime.schemas import now

    workspace = make({"AGENT": [text("Back.")]})
    conversation_id = start(workspace, template(workspace))
    stale = ConversationMessage(
        conversation_id=conversation_id,
        seq=2,
        role="assistant",
        status="queued",
        created_at=now() - RESERVATION_TIMEOUT - timedelta(seconds=1),
    )
    workspace.store.put(MESSAGES, f"{conversation_id}:000002", stale)
    raw = workspace.store.get_raw(COLLECTION, conversation_id)
    workspace.store.put_raw(COLLECTION, conversation_id, {**raw, "last_seq": 2})
    [expired] = workspace.conversations.messages(conversation_id, None)
    assert expired.status == "failed" and expired.error is not None
    assert expired.error.code == "conversation.run_missing"
    assert (await answer(workspace, conversation_id, "Again")).content == "Back."


def test_l4_no_state_per_conversation_is_kept_in_memory(make: Make) -> None:
    workspace = make()
    assert set(vars(workspace.conversations)) == {"workspace", "store", "templates"}


async def test_l7_a_conversation_without_owner_is_seen_by_nobody_on_a_server(make: Make) -> None:
    workspace = make()
    conversation_id = start(workspace, template(workspace))  # made on a desktop (no owner), restored on a server
    assert [item.id for item in workspace.conversations.summaries(None)] == [conversation_id]
    assert workspace.conversations.summaries("usr_alice") == []
    with pytest.raises(ConversationNotFound):
        workspace.conversations.get(conversation_id, "usr_alice")


async def test_w3s1_who_has_no_owner_identity_sees_and_starts_no_conversation(make: Make) -> None:
    """W3-S1: a principal without an owner identity on a server (``NO_OWNER``) sees no conversation, no answer run,
    no run below one and no file, and starts none. Before, it was passed as ``None`` and saw everything."""
    workspace = make({"AGENT": [text("Noted.")]})
    template_id = template(workspace)
    mine = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template_id), model=CLOUD), owner="usr_alice"
    )
    added = workspace.conversations.add_file(
        mine.id, "usr_alice", b"secret notes", name="notes.txt", media_type="text/plain", uploaded_by="usr_alice"
    )
    sent = await workspace.conversations.send(mine.id, "usr_alice", MessageIn(content="Keep this", file_ids=[added.id]))
    run_id = sent.assistant.run_id or ""
    await workspace.wait(run_id, timeout=20)
    run = workspace.runs.get(run_id)
    assert run is not None
    child = RunSummary(**run.model_dump(include=set(RunSummary.model_fields))).model_copy(
        update={"id": "run_child", "trigger": "subworkflow", "trigger_ref": run_id, "parent_run_id": run_id}
    )
    workspace.store.put("runs", child.id, child)

    assert workspace.conversations.summaries(NO_OWNER) == []
    for call in (
        lambda: workspace.conversations.get(mine.id, NO_OWNER),
        lambda: workspace.conversations.detail(mine.id, NO_OWNER),
        lambda: workspace.conversations.messages(mine.id, NO_OWNER),
        lambda: workspace.conversations.file(mine.id, NO_OWNER, added.id),
        lambda: workspace.conversations.usage(mine.id, NO_OWNER),
        lambda: workspace.conversations.export_markdown(mine.id, NO_OWNER),
        lambda: workspace.conversations.run_of(mine.id, NO_OWNER, sent.assistant.id),
        lambda: workspace.conversations.update(mine.id, NO_OWNER, ConversationChange(title="x")),
        lambda: workspace.conversations.remove_file(mine.id, NO_OWNER, added.id),
        lambda: workspace.conversations.cancel(mine.id, NO_OWNER, sent.assistant.id),
        lambda: workspace.conversations.add_file(
            mine.id, NO_OWNER, b"x", name="x.txt", media_type="text/plain", uploaded_by="svc"
        ),
    ):
        with pytest.raises(ConversationNotFound):
            call()
    with pytest.raises(ConversationNotFound):
        await workspace.conversations.send(mine.id, NO_OWNER, MessageIn(content="hi"))
    with pytest.raises(ConversationNotFound):
        await workspace.conversations.delete(mine.id, NO_OWNER)
    for run_ref in (run_id, child.id):
        assert workspace.conversations.run_visible(run_ref, NO_OWNER) is False
    assert workspace.conversations.summary_visible(run, NO_OWNER) is False
    assert workspace.conversations.summary_visible(child, NO_OWNER) is False
    # It starts none either, so no conversation is ever shared by everyone without an owner identity.
    with pytest.raises(NodestroError) as refused:
        workspace.conversations.create(
            ConversationIn(agent=AgentChoice(template_id=template_id), model=CLOUD), owner=NO_OWNER
        )
    assert refused.value.code == "access.person_required"
    # A stored conversation that names it as its owner (a crafted or restored document) is seen by no such request.
    raw = workspace.store.get_raw(COLLECTION, mine.id)
    workspace.store.put_raw(COLLECTION, mine.id, {**raw, "owner": NO_OWNER})
    assert workspace.conversations.summaries(NO_OWNER) == []
    assert workspace.conversations.run_visible(run_id, NO_OWNER) is False
    # The desktop's only person (``None``) and the owner are not affected.
    assert [item.id for item in workspace.conversations.summaries(None)] == [mine.id]
    workspace.store.put_raw(COLLECTION, mine.id, raw)
    assert workspace.conversations.run_visible(run_id, "usr_alice") is True
    assert workspace.conversations.run_visible(child.id, "usr_alice") is True


async def test_w3s1_an_answer_never_writes_into_a_shared_memory_space(make: Make) -> None:
    """W3-S1 review (medium): a template with persistent memory answered with the shared space, which every member
    reads (``/api/memory``). An answer keeps run memory instead: the space stays untouched."""
    note = tool_use("memory", {"command": "create", "path": "/memories/n.md", "file_text": "salary zanzibar"})
    workspace = make({"AGENT": [note, text("Noted.")]})
    template_id = template(workspace, memory={"mode": "persistent", "space": "Team"})
    conversation = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template_id), model=CLOUD), owner="usr_alice"
    )
    sent = await workspace.conversations.send(conversation.id, "usr_alice", MessageIn(content="Remember my salary"))
    run_id = sent.assistant.run_id or ""
    await workspace.wait(run_id, timeout=20)
    run = workspace.runs.get(run_id)
    assert run is not None and run.status == "succeeded", run
    [agent] = [node for node in run.workflow.nodes if node.agent is not None]
    assert agent.agent is not None and (agent.agent.memory.mode, agent.agent.memory.space) == ("run", None)
    assert workspace.memory.open("Team").entries() == []
    # The template itself is unchanged (a workflow run still uses the space).
    stored = AgentTemplates(workspace.store).get(template_id)
    assert stored.agent.memory.mode == "persistent"


# ---- re-review fixes ---------------------------------------------------------------------------------------------


async def test_n4_a_send_whose_reservation_expired_while_its_run_started_writes_no_second_answer(
    make: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-4: the launch took longer than RESERVATION_TIMEOUT, a reader failed the reservation (and another answer may
    have started). The late send must not write its run into the answer: its run is cancelled instead."""
    from datetime import timedelta

    from nodestro_runtime import conversations as module
    from nodestro_runtime.conversations import RESERVATION_TIMEOUT
    from nodestro_runtime.schemas import now

    workspace = make({"AGENT": [text("Late.")]})
    conversation_id = start(workspace, template(workspace))
    real_launch = module.launch
    started: list[str] = []

    async def slow_launch(*args: Any, **kwargs: Any) -> Run:
        run = await real_launch(*args, **kwargs)
        started.append(run.id)
        key = f"{conversation_id}:000002"
        raw = workspace.store.get_raw(MESSAGES, key)
        old = (now() - RESERVATION_TIMEOUT - timedelta(seconds=1)).isoformat()
        workspace.store.put_raw(MESSAGES, key, {**raw, "created_at": old})
        workspace.conversations.messages(conversation_id, None)  # a reader in the meantime
        return run

    monkeypatch.setattr(module, "launch", slow_launch)
    with pytest.raises(NodestroError) as caught:
        await workspace.conversations.send(conversation_id, None, MessageIn(content="hi"))
    assert caught.value.code == "conversation.answer_running"
    answer = workspace.conversations.messages(conversation_id, None)[-1]
    assert (answer.status, answer.run_id) == ("failed", None)
    assert answer.error is not None and answer.error.code == "conversation.run_missing"
    [run_id] = started
    finished = await workspace.wait(run_id, timeout=20)
    assert finished.status == "cancelled"


def test_n2_a_run_at_hand_that_is_no_answer_needs_no_read(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    """N-2: run lists filter their summaries without a store read per run; only answers' runs read their
    conversation."""
    from nodestro_runtime.schemas import Usage, now

    workspace = make()
    conversation_id = start(workspace, template(workspace))

    def summary(trigger: str, ref: str | None = None) -> RunSummary:
        return RunSummary(
            id="run_x",
            workflow_id="wf",
            workflow_name="W",
            status="succeeded",
            input="",
            usage=Usage(),
            created_at=now(),
            trigger=trigger,
            trigger_ref=ref,  # type: ignore[arg-type]
        )

    reads: list[tuple[str, str]] = []
    real_get = workspace.store.get

    def counting(collection: str, key: str, model: Any) -> Any:
        reads.append((collection, key))
        return real_get(collection, key, model)

    monkeypatch.setattr(workspace.store, "get", counting)
    assert workspace.conversations.summary_visible(summary("manual"), "usr_bob") is True
    assert reads == []
    assert workspace.conversations.summary_visible(summary("conversation", conversation_id), None) is True
    assert workspace.conversations.summary_visible(summary("conversation", conversation_id), "usr_bob") is False
    assert reads == [(COLLECTION, conversation_id)] * 2


async def test_low1_a_child_run_whose_parent_is_gone_is_hidden_from_everyone(make: Make) -> None:
    """Review Low 1: a run below an answer belongs to the answer's conversation. When its parent is missing (deleted
    while the child stayed), nobody can tell whose it is, so nobody sees it; a loop of parents fails closed too."""
    workspace = make({"AGENT": [text("Hi.")]})
    mine = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template(workspace)), model=CLOUD), owner="usr_alice"
    )
    sent = await workspace.conversations.send(mine.id, "usr_alice", MessageIn(content="Hello"))
    run_id = sent.assistant.run_id or ""
    await workspace.wait(run_id, timeout=20)
    parent = workspace.runs.get(run_id)
    assert parent is not None
    child = parent.model_copy(
        update={"id": "run_child", "trigger": "subworkflow", "trigger_ref": run_id, "parent_run_id": run_id}
    )
    workspace.store.put("runs", child.id, child)
    assert workspace.conversations.run_visible(child.id, "usr_alice") is True
    assert workspace.conversations.run_visible(child.id, "usr_bob") is False

    workspace.store.delete("runs", run_id)
    for owner in ("usr_alice", "usr_bob", None):
        assert workspace.conversations.run_visible(child.id, owner) is False, owner
        stored = workspace.store.get("runs", child.id, RunSummary)
        assert stored is not None and workspace.conversations.summary_visible(stored, owner) is False, owner

    looped = child.model_copy(update={"id": "run_loop", "parent_run_id": "run_loop"})
    workspace.store.put("runs", looped.id, looped)
    assert workspace.conversations.run_visible(looped.id, "usr_bob") is False
