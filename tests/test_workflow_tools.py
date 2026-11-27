"""Workflows as tools of an agent (N-20): the agent calls a saved workflow in a fixed version.

Covered: the effect of a workflow with a nested sub-workflow and an MCP tool (the highest of everything it contains,
unknown counts as write + external); U-4 end to end with a scripted model in a conversation (the agent calls the
"quote" workflow with the right arguments, the confirmation card before the start and the approval card of the
workflow's approval step appear, the result file is an attachment of the answer, the cost is the answer's, and the
version stays fixed after a newer version was saved; the newer one is only reported); cancelling the conversation's
answer cancels the workflow's run; the switch is off by default and a changing workflow without approvals is refused.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
import itertools
import json
from pathlib import Path
import time
from typing import Any

import pytest

from nodestro_runtime.agent_templates import AgentTemplates
from nodestro_runtime.conversations import AgentChoice, ConversationIn, ConversationMessage, MessageIn
from nodestro_runtime.schemas import (
    AgentConfig,
    ApprovalConfig,
    Edge,
    InputField,
    MemoryConfig,
    Node,
    OutputNodeConfig,
    ResultFileSpec,
    StartConfig,
    SubworkflowConfig,
    Workflow,
    WorkflowEffect,
    WorkflowIn,
    WorkflowToolConfig,
)
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import AccessRules, ToolApprovals, ToolGate, ToolPreGrants, tool_facts
from nodestro_runtime.tool_policy import UNKNOWN, CallContext, ToolCapability, decide
from nodestro_runtime.workflow_tools import (
    WORKFLOW_TOOLS_ENV,
    aggregate,
    arguments_schema,
    call_values,
    effective,
    workflow_tools_enabled,
)
from nodestro_runtime.workspace import Workspace

CLOUD = "anthropic:claude-sonnet-5-5"
Make = Callable[..., Workspace]
READ = ToolCapability(access="read", reach="internal", source="nodestro")
CRM_WRITE = ToolCapability(access="write", reach="external", source="admin")


@pytest.fixture
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Make]:
    monkeypatch.setenv(WORKFLOW_TOOLS_ENV, "1")
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]]) -> Workspace:
        fake = ScriptedClient(script)
        workspace = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
        workspace.fake = fake  # type: ignore[attr-defined]
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def agent_node(node_id: str, prompt: str, **fields: Any) -> Node:
    return Node(id=node_id, type="agent", agent=AgentConfig(name=prompt.title(), system_prompt=prompt, **fields))


def chain(workflow_id: str, name: str, nodes: list[Node], **fields: Any) -> Workflow:
    edges = [
        Edge(id=f"e{index}", source=a.id, target=b.id, source_handle="approved" if a.type == "approval" else None)
        for index, (a, b) in enumerate(itertools.pairwise(nodes))
    ]
    return Workflow(id=workflow_id, name=name, nodes=nodes, edges=edges, **fields)


def quote_workflow(prompt: str = "QUOTE_V1", *, approval: bool = True) -> WorkflowIn:
    """A "quote" workflow: a start form (customer, amount), an agent, an approval step and a Markdown result file."""
    start = Node(
        id="start",
        type="start",
        start=StartConfig(
            fields=[
                InputField(name="customer", label="Customer", required=True),
                InputField(name="amount", label="Amount", type="number", required=True),
            ]
        ),
    )
    nodes = [start, agent_node("writer", prompt, task_template="Quote for {{form.customer}}: {{form.amount}}")]
    if approval:
        nodes.append(Node(id="check", type="approval", approval=ApprovalConfig(instructions="Check the quote")))
    nodes.append(
        Node(id="out", type="output", output=OutputNodeConfig(files=[ResultFileSpec(format="md", name="quote")]))
    )
    built = chain("", "Quote", nodes)
    return WorkflowIn(name="Quote", description="Writes a quote for a customer.", nodes=built.nodes, edges=built.edges)


def binding_for(
    workspace: Workspace, workflow: Workflow, version: int, name: str = "workflow_quote"
) -> WorkflowToolConfig:
    info = workspace.runs.describe_workflow_tool(workflow, version, name=name)
    assert info.latest_version is not None and info.latest_version >= version
    return info.binding


def conversation(workspace: Workspace, binding: WorkflowToolConfig) -> str:
    agent = AgentConfig(name="Agent", system_prompt="AGENT", workflow_tools=[binding])
    template = AgentTemplates(workspace.store).create(agent).id
    return workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template), model=CLOUD), owner=None
    ).id


def gate(workspace: Workspace) -> None:
    workspace.runs.tool_gate = ToolGate(
        ToolApprovals(workspace.store, poll_seconds=0.02),
        pre_grants=ToolPreGrants(workspace.store).of_scope,
        access=AccessRules(workspace.store).current,
        protected_folders=(str(workspace.data_dir),),
    )


async def until(read: Callable[[], ConversationMessage], done: Callable[[ConversationMessage], bool]) -> Any:
    deadline = time.monotonic() + 15
    found = read()
    while not done(found):
        assert time.monotonic() < deadline, found.model_dump_json(indent=1)[:4000]
        await asyncio.sleep(0.05)
        found = read()
    return found


# ---- the effect -------------------------------------------------------------------------------------------------


def test_the_effect_is_the_highest_of_everything_inside_with_a_nested_subworkflow_and_an_mcp_tool() -> None:
    inner = chain(
        "inner", "Inner", [Node(id="start", type="start"), agent_node("crm", "CRM", tools=["mcp__crm__create_lead"])]
    )
    middle = chain(
        "middle",
        "Middle",
        [
            Node(id="start", type="start"),
            Node(id="sub", type="subworkflow", subworkflow=SubworkflowConfig(workflow_id="inner", version=3)),
        ],
    )
    outer = chain(
        "outer",
        "Outer",
        [
            Node(id="start", type="start"),
            agent_node("calc", "CALC", tools=["calculator"]),
            Node(id="sub", type="subworkflow", subworkflow=SubworkflowConfig(workflow_id="middle", version=2)),
        ],
    )
    saved = {("inner", 3): inner, ("middle", 2): middle}

    def resolve(workflow_id: str, version: int | None) -> tuple[Workflow, int | None]:
        return saved[(workflow_id, version or 0)], version

    facts = {"calculator": READ, "mcp__crm__create_lead": CRM_WRITE}
    effect = aggregate(outer, 1, resolve=resolve, facts=facts.get)
    assert (effect.access, effect.reach) == ("write", "external")
    assert effect.workflows == ["outer@1", "middle@2", "inner@3"] and effect.unknown == []

    # Without the nested workflow it reads only; an MCP tool that is not there counts as unknown (write + external).
    alone = chain("outer", "Outer", [Node(id="start", type="start"), agent_node("calc", "CALC", tools=["calculator"])])
    assert (aggregate(alone, 1, resolve=resolve, facts=facts.get).access) == "read"
    gone = aggregate(outer, 1, resolve=resolve, facts={"calculator": READ}.get)
    assert (gone.access, gone.reach, gone.unknown) == ("write", "external", ["mcp__crm__create_lead"])
    # A nested workflow that cannot be read is unknown too, and so is a destructive tool deeper down the highest.
    missing = aggregate(outer, 1, resolve=lambda *_: (_ for _ in ()).throw(KeyError("x")), facts=facts.get)
    assert (missing.access, missing.reach, missing.unknown) == ("write", "external", ["middle@2"])
    facts["mcp__crm__create_lead"] = ToolCapability(access="destructive", reach="external", source="admin")
    assert aggregate(outer, 1, resolve=resolve, facts=facts.get).access == "destructive"


def test_a_persistent_memory_space_and_result_files_are_part_of_the_effect() -> None:
    remembering = chain(
        "w",
        "W",
        [Node(id="start", type="start"), agent_node("a", "A", memory=MemoryConfig(mode="persistent", space="crm"))],
    )
    effect = aggregate(remembering, 1, resolve=lambda *_: (remembering, 1), facts=lambda _name: None)
    assert effect.memory and (effect.access, effect.reach) == ("write", "internal")
    stored = WorkflowEffect(access="read", reach="internal", workflows=["w@1"])
    # A stored effect never lowers the current one; a higher stored one stays.
    assert effective(stored, effect).access == "write"
    assert effective(WorkflowEffect(access="destructive", reach="external"), effect).access == "destructive"


def test_the_arguments_are_the_start_form_and_unknown_ones_are_refused() -> None:
    built = Workflow(**quote_workflow().model_dump())
    schema = arguments_schema(built)
    assert schema["required"] == ["customer", "amount"]
    assert schema["properties"]["amount"]["type"] == "number" and schema["additionalProperties"] is False
    assert call_values(built, {"customer": "ACME", "amount": 3}) == ("", {"customer": "ACME", "amount": 3})
    with pytest.raises(Exception, match="Unknown arguments: price"):
        call_values(built, {"customer": "ACME", "price": 3})


def test_a_changing_workflow_tool_is_asked_before_every_start() -> None:
    attended = CallContext(run_id="run_1", node_id="agent", trigger="conversation", attended=True)
    unattended = CallContext(run_id="run_1", node_id="agent", trigger="schedule", attended=False)
    at = datetime.now(UTC)
    common: dict[str, Any] = {"tool": "workflow_quote", "kind": "workflow", "binding": None, "at": at, "version": 1}
    write = ToolCapability(access="write", reach="internal", source="derived")
    assert decide(capability=write, context=attended, **common).action == "ask"
    assert decide(capability=write, context=unattended, **common).action == "deny"
    assert decide(capability=READ, context=attended, **common).action == "allow"
    assert decide(capability=UNKNOWN, context=attended, **common).action == "ask"


def test_the_switch_is_off_by_default() -> None:
    assert not workflow_tools_enabled({})
    assert workflow_tools_enabled({WORKFLOW_TOOLS_ENV: "1"})


# ---- U-4 end to end -----------------------------------------------------------------------------------------------


async def test_u4_the_agent_calls_the_quote_workflow_in_a_conversation(make: Make) -> None:
    workspace = make(
        {
            "AGENT": [tool_use("workflow_quote", {"customer": "ACME", "amount": 1200}), text("Your quote is ready.")]
            * 2,
            "QUOTE_V1": [text("Quote for ACME: 1200 EUR")] * 2,
        }
    )
    gate(workspace)
    quote = workspace.save_workflow(quote_workflow())
    binding = binding_for(workspace, quote, 1)
    assert binding.effect is not None and (binding.effect.access, binding.effect.approvals) == ("write", 1)
    conversation_id = conversation(workspace, binding)

    sent = await workspace.conversations.send(conversation_id, None, MessageIn(content="A quote for ACME, 1200."))
    run_id = sent.assistant.run_id or ""

    def read() -> ConversationMessage:
        return workspace.conversations.message(conversation_id, None, sent.assistant.id)

    # The workflow changes something (it writes a file): its start is confirmed first, with the right arguments.
    waiting = await until(read, lambda found: bool(found.approval_ids))
    [request_id] = waiting.approval_ids
    request = ToolApprovals(workspace.store).get(run_id, request_id)
    assert request is not None and request.tool == "workflow_quote" and request.capability.access == "write"
    assert waiting.tool_calls[0].arguments == {"customer": "ACME", "amount": 1200}
    workspace.runs.decide_tool_call(run_id, request_id, approved=True, by="local", arguments=request.arguments_hash)

    # The workflow's approval step appears as a card of the answer; deciding it lets the workflow finish.
    card_message = await until(
        read,
        lambda found: bool(
            found.tool_calls and found.tool_calls[0].workflow and found.tool_calls[0].workflow.approvals
        ),
    )
    view = card_message.tool_calls[0].workflow
    assert view is not None and view.version == 1 and view.status == "paused"
    [card] = view.approvals
    assert (card.node_id, card.instructions) == ("check", "Check the quote")
    workspace.runs.decide(card.run_id, card.node_id, "approved")
    await workspace.wait(run_id, timeout=20)
    read_answer = read()
    assert read_answer.status == "succeeded" and read_answer.content == "Your quote is ready."
    [file] = read_answer.result_files
    assert (file.name, file.format, file.run_id) == ("quote.md", "md", view.run_id)
    assert b"ACME" in workspace.files.content(file.run_id, file.file_id)[1]
    child = workspace.runs.get(view.run_id)
    assert child is not None and child.parent_run_id == run_id and child.workflow_version == 1
    assert child.values == {"customer": "ACME", "amount": 1200}
    result = json.loads(read_answer.tool_calls[0].result or "{}")
    assert result["output"] and result["files"] == [{"name": "quote.md", "format": "md"}]
    # The workflow's model request is part of the answer's usage (and so of the conversation's budget).
    assert read_answer.usage is not None and read_answer.usage.requests == 3

    # A newer version is only reported; the agent keeps calling version 1.
    current = workspace.runs.resolve_workflow(quote.id, None)[0]
    changed = quote_workflow("QUOTE_V2")
    workspace.save_workflow(Workflow(**{**current.model_dump(), "nodes": changed.nodes, "edges": changed.edges}))
    assert workspace.runs.resolve_workflow(quote.id, None)[1] == 2
    detail = workspace.conversations.detail(conversation_id, None)
    [update] = detail.workflow_tool_updates
    assert (update.name, update.version, update.latest_version) == ("workflow_quote", 1, 2)
    again = await workspace.conversations.send(conversation_id, None, MessageIn(content="Once more."))

    def read_again() -> ConversationMessage:
        return workspace.conversations.message(conversation_id, None, again.assistant.id)

    second = await until(read_again, lambda found: bool(found.approval_ids))
    [second_request] = second.approval_ids
    asked = ToolApprovals(workspace.store).get(again.assistant.run_id or "", second_request)
    assert asked is not None
    workspace.runs.decide_tool_call(
        again.assistant.run_id or "", second_request, approved=True, by="local", arguments=asked.arguments_hash
    )
    card_again = await until(
        read_again,
        lambda found: bool(
            found.tool_calls and found.tool_calls[0].workflow and found.tool_calls[0].workflow.approvals
        ),
    )
    view_again = card_again.tool_calls[0].workflow
    assert view_again is not None and view_again.version == 1
    workspace.runs.decide(view_again.approvals[0].run_id, "check", "approved")
    await workspace.wait(again.assistant.run_id or "", timeout=20)
    assert read_again().status == "succeeded"
    prompts = [str(call.get("system", "")) for call in workspace.fake.messages.calls]  # type: ignore[attr-defined]
    assert not any("QUOTE_V2" in prompt for prompt in prompts)


async def test_cancelling_the_answer_cancels_the_workflows_run(make: Make) -> None:
    workspace = make(
        {"AGENT": [tool_use("workflow_quote", {"customer": "ACME", "amount": 5})], "QUOTE_V1": [text("Q")]}
    )
    gate(workspace)
    quote = workspace.save_workflow(quote_workflow())
    conversation_id = conversation(workspace, binding_for(workspace, quote, 1))
    sent = await workspace.conversations.send(conversation_id, None, MessageIn(content="Quote please"))
    run_id = sent.assistant.run_id or ""

    def read() -> ConversationMessage:
        return workspace.conversations.message(conversation_id, None, sent.assistant.id)

    waiting = await until(read, lambda found: bool(found.approval_ids))
    request = ToolApprovals(workspace.store).get(run_id, waiting.approval_ids[0])
    assert request is not None
    workspace.runs.decide_tool_call(run_id, request.id, approved=True, by="local", arguments=request.arguments_hash)
    paused = await until(
        read,
        lambda found: bool(
            found.tool_calls and found.tool_calls[0].workflow and found.tool_calls[0].workflow.approvals
        ),
    )
    child_id = paused.tool_calls[0].workflow.run_id  # type: ignore[union-attr]

    workspace.conversations.cancel(conversation_id, None, sent.assistant.id)
    await workspace.wait(run_id, timeout=20)
    assert read().status == "cancelled"
    child = workspace.runs.get(child_id)
    assert child is not None and child.status == "cancelled"
    assert child.nodes["check"].status == "cancelled"


async def test_without_approvals_a_changing_workflow_is_refused_and_a_reading_one_runs(make: Make) -> None:
    workspace = make(
        {
            "AGENT": [
                tool_use("workflow_quote", {"customer": "ACME", "amount": 5}),
                text("Refused."),
                tool_use("workflow_lookup", {"input": "ACME"}),
                text("Found."),
            ],
            "LOOKUP": [text("ACME is a customer")],
        }
    )
    quote = workspace.save_workflow(quote_workflow())
    lookup = workspace.save_workflow(
        WorkflowIn(
            name="Lookup",
            nodes=[Node(id="start", type="start"), agent_node("find", "LOOKUP", tools=["calculator"])],
            edges=[Edge(id="e1", source="start", target="find")],
        )
    )
    reading = binding_for(workspace, lookup, 1, name="workflow_lookup")
    assert reading.effect is not None and reading.effect.access == "read"
    agent = AgentConfig(name="Agent", system_prompt="AGENT", workflow_tools=[binding_for(workspace, quote, 1), reading])
    template = AgentTemplates(workspace.store).create(agent).id
    conversation_id = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template), model=CLOUD), owner=None
    ).id
    sent = await workspace.conversations.send(conversation_id, None, MessageIn(content="Quote please"))
    await workspace.wait(sent.assistant.run_id or "", timeout=20)
    refused = workspace.conversations.message(conversation_id, None, sent.assistant.id)
    assert refused.tool_calls[0].approval is not None
    assert refused.tool_calls[0].approval.code == "workflow_tool.approval_required"
    assert refused.tool_calls[0].workflow is None
    refused_run = workspace.runs.get(sent.assistant.run_id or "")
    assert refused_run is not None and refused_run.nodes["agent"].child_run_ids == []

    sent = await workspace.conversations.send(conversation_id, None, MessageIn(content="Who is ACME?"))
    await workspace.wait(sent.assistant.run_id or "", timeout=20)
    found = workspace.conversations.message(conversation_id, None, sent.assistant.id)
    assert found.status == "succeeded" and found.tool_calls[0].workflow is not None
    assert found.tool_calls[0].workflow.status == "succeeded"
    assert "ACME is a customer" in (found.tool_calls[0].result or "")


async def test_the_switch_off_stops_an_agent_with_workflow_tools(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = make({"AGENT": [text("never")]})
    quote = workspace.save_workflow(quote_workflow())
    conversation_id = conversation(workspace, binding_for(workspace, quote, 1))
    monkeypatch.delenv(WORKFLOW_TOOLS_ENV)
    sent = await workspace.conversations.send(conversation_id, None, MessageIn(content="Quote please"))
    await workspace.wait(sent.assistant.run_id or "", timeout=20)
    answer = workspace.conversations.message(conversation_id, None, sent.assistant.id)
    assert answer.status == "failed" and answer.error is not None and answer.error.code == "workflow_tool.disabled"
    assert workspace.fake.messages.calls == []  # type: ignore[attr-defined]


def test_the_facts_of_a_workflow_tool_are_only_bound_inside_its_step(make: Make) -> None:
    workspace = make({})
    assert tool_facts(workspace.tools, "workflow_quote")[0] == UNKNOWN


def test_a_memory_consent_covers_the_called_workflow_in_its_fixed_version(make: Make) -> None:
    from nodestro_runtime.memory_consents import consent_targets

    workspace = make({})
    quote = workspace.save_workflow(quote_workflow())
    caller = workspace.save_workflow(
        WorkflowIn(
            name="Caller",
            nodes=[
                Node(id="start", type="start"),
                agent_node("a", "A", workflow_tools=[binding_for(workspace, quote, 1)]),
            ],
            edges=[Edge(id="e1", source="start", target="a")],
        )
    )
    runs = workspace.runs
    targets = consent_targets(runs.resolve_workflow, runs.pin_subworkflows, runs.consent_digest, caller.id)
    assert [(item.workflow_id, item.version) for item in targets] == [(caller.id, 1), (quote.id, 1)]
