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
from nodestro_runtime.local_models import LOCAL_DEFAULT_MODEL
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
        [
            Node(id="start", type="start"),
            agent_node("a", "A", model=LOCAL_DEFAULT_MODEL, memory=MemoryConfig(mode="persistent", space="crm")),
        ],
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


# ---- review fixes (independent review of N-20) ------------------------------------------------------------------


def lookup_conversation(workspace: Workspace, **agent: Any) -> tuple[str, Workflow]:
    lookup = workspace.save_workflow(
        WorkflowIn(
            name="Lookup",
            nodes=[Node(id="start", type="start"), agent_node("find", "LOOKUP", tools=["calculator"])],
            edges=[Edge(id="e1", source="start", target="find")],
        )
    )
    binding = binding_for(workspace, lookup, 1, name="workflow_lookup")
    config = AgentConfig(name="Agent", system_prompt="AGENT", workflow_tools=[binding], **agent)
    template = AgentTemplates(workspace.store).create(config).id
    conversation_id = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template), model=CLOUD), owner=None
    ).id
    return conversation_id, lookup


async def test_h1_a_call_never_holds_the_thread_its_workflows_agents_need(make: Make) -> None:
    workspace = make(
        {"AGENT": [tool_use("workflow_lookup", {"input": "ACME"}), text("Found.")], "LOOKUP": [text("Yes")]}
    )
    workspace.runs.run_threads = 1  # one agent thread: the workflow's agent needs it while the call waits
    conversation_id, _lookup = lookup_conversation(workspace)
    sent = await workspace.conversations.send(conversation_id, None, MessageIn(content="Who is ACME?"))
    await workspace.wait(sent.assistant.run_id or "", timeout=15)
    assert workspace.conversations.message(conversation_id, None, sent.assistant.id).status == "succeeded"


def test_m1_no_pre_grant_lets_an_unattended_run_start_a_changing_workflow() -> None:
    from nodestro_runtime.tool_policy import ToolPreGrant

    at = datetime.now(UTC)
    grant = ToolPreGrant(
        id="g1", tool="crm_note", version=1, scope="schedule", scope_id="s1", granted_by="admin", granted_at=at
    )
    context = CallContext(
        run_id="r", node_id="a", trigger="schedule", attended=False, pre_grants=(grant,), scope=("schedule", "s1")
    )
    capability = ToolCapability(access="write", reach="external", source="derived")
    verdict = decide(
        tool="crm_note", kind="workflow", capability=capability, binding=None, context=context, at=at, version=1
    )
    assert verdict.action == "deny" and verdict.code == "tool.approval_unattended"


@pytest.mark.parametrize("name", ["calculator", "memory", "web_search", "mcp__crm__x"])
async def test_m1_a_workflow_tool_never_takes_the_name_of_another_tool(make: Make, name: str) -> None:
    workspace = make({"AGENT": [text("never")]})
    quote = workspace.save_workflow(quote_workflow())
    conversation_id = conversation(workspace, binding_for(workspace, quote, 1, name=name))
    sent = await workspace.conversations.send(conversation_id, None, MessageIn(content="Quote please"))
    await workspace.wait(sent.assistant.run_id or "", timeout=20)
    answer = workspace.conversations.message(conversation_id, None, sent.assistant.id)
    assert answer.status == "failed" and answer.error is not None and answer.error.code == "workflow_tool.name_taken"


def test_m2_a_model_that_is_not_local_reaches_outside() -> None:
    cloud = chain("w", "W", [Node(id="start", type="start"), agent_node("a", "A", tools=["calculator"])])
    local = chain(
        "w",
        "W",
        [Node(id="start", type="start"), agent_node("a", "A", tools=["calculator"], model=LOCAL_DEFAULT_MODEL)],
    )
    facts = {"calculator": READ}.get
    assert aggregate(cloud, 1, resolve=lambda *_: (cloud, 1), facts=facts).reach == "external"
    assert aggregate(local, 1, resolve=lambda *_: (local, 1), facts=facts).reach == "internal"
    fallback = chain(
        "w",
        "W",
        [Node(id="start", type="start"), agent_node("a", "A", model=LOCAL_DEFAULT_MODEL, fallbacks=[CLOUD])],
    )
    assert aggregate(fallback, 1, resolve=lambda *_: (fallback, 1), facts=facts).reach == "external"


def test_m2_an_orchestrator_model_that_is_not_local_reaches_outside() -> None:
    from nodestro_runtime.schemas import OrchestratorConfig, OrchestratorMember

    team = chain("wf_team", "Team", [Node(id="start", type="start"), agent_node("a", "A", model=LOCAL_DEFAULT_MODEL)])

    def lead(model: str | None) -> Workflow:
        config = OrchestratorConfig(members=[OrchestratorMember(workflow_id="wf_team", version=1)])
        if model is not None:
            config = config.model_copy(update={"model": model})
        return chain(
            "w", "W", [Node(id="start", type="start"), Node(id="lead", type="orchestrator", orchestrator=config)]
        )

    facts = {"calculator": READ}.get
    # The default orchestrator model is a cloud model: a local-only team does not keep the workflow internal.
    assert aggregate(lead(None), 1, resolve=lambda *_: (team, 1), facts=facts).reach == "external"
    assert aggregate(lead(LOCAL_DEFAULT_MODEL), 1, resolve=lambda *_: (team, 1), facts=facts).reach == "internal"


def test_m2_an_ollama_server_elsewhere_reaches_outside(make: Make) -> None:
    from nodestro_runtime.schemas import SettingsIn

    local = chain(
        "w",
        "W",
        [Node(id="start", type="start"), agent_node("a", "A", tools=["calculator"], model=LOCAL_DEFAULT_MODEL)],
    )
    facts = {"calculator": READ}.get
    resolve = lambda *_: (local, 1)  # noqa: E731
    assert aggregate(local, 1, resolve=resolve, facts=facts, local_server=lambda: True).reach == "internal"
    assert aggregate(local, 1, resolve=resolve, facts=facts, local_server=lambda: False).reach == "external"
    workspace = make({})
    assert workspace.runs.workflow_effect(local, 1).reach == "internal"
    workspace.update_settings(SettingsIn(providers={"ollama": {"base_url": "http://127.0.0.1:11434/v1"}}))
    assert workspace.runs.workflow_effect(local, 1).reach == "internal"
    workspace.update_settings(SettingsIn(providers={"ollama": {"base_url": "http://gpu-box.example:11434/v1"}}))
    assert workspace.runs.workflow_effect(local, 1).reach == "external"


def paused_tree(workspace: Workspace, *, top_status: str = "running", waiting: str = "awaiting_approval") -> Any:
    from nodestro_runtime.schemas import NodeRun, Run

    child_workflow = chain("wf_child", "Child", [Node(id="start", type="start"), Node(id="check", type="approval")])
    child = Run(
        workflow_id="wf_child",
        workflow_name="Child",
        workflow=child_workflow,
        status="paused",
        nodes={"start": NodeRun(status="succeeded"), "check": NodeRun(status=waiting)},  # type: ignore[arg-type]
        trigger="subworkflow",
    )
    top_workflow = chain("wf_top", "Top", [Node(id="start", type="start"), agent_node("agent", "AGENT")])
    top = Run(
        workflow_id="wf_top",
        workflow_name="Top",
        workflow=top_workflow,
        status=top_status,  # type: ignore[arg-type]
        nodes={"start": NodeRun(status="succeeded"), "agent": NodeRun(status="running", child_run_ids=[child.id])},
    )
    child.parent_run_id, child.parent_node_id, child.trigger_ref = top.id, "agent", top.id
    workspace.store.put("runs", top.id, top)
    workspace.store.put("runs", child.id, child)
    return top, child


async def test_m3_a_budget_decision_of_another_process_continues_the_call(make: Make) -> None:
    from decimal import Decimal

    from nodestro_runtime.run_budget import BudgetBlock, BudgetRequest, BudgetRequests

    workspace = make({})
    top, child = paused_tree(workspace, waiting="awaiting_budget")
    requests = BudgetRequests(workspace.store)
    asked = requests.put(
        BudgetRequest(
            run_id=top.id,
            workflow_id=top.workflow_id,
            workflow_name=top.workflow_name,
            block=BudgetBlock(run_id=top.id),
            limit_usd=Decimal(1),
            spent_usd=Decimal(1),
        )
    )
    approvals = ToolApprovals(workspace.store)
    assert not workspace.runs._workflow_tool_decided(child.id, approvals, top.id)
    requests.put(asked.model_copy(update={"status": "raised"}))
    assert workspace.runs._workflow_tool_decided(child.id, approvals, top.id)


async def test_l1_a_waiting_child_of_an_ended_call_takes_no_decision_and_is_cancelled_after_a_restart(
    make: Make,
) -> None:
    from nodestro_runtime.problems import NodestroError

    workspace = make({})
    _top, child = paused_tree(workspace, top_status="failed")
    with pytest.raises(NodestroError) as refused:
        workspace.runs.decide(child.id, "check", "approved")
    assert refused.value.code == "workflow_tool.not_waiting"
    assert workspace.runs.get(child.id).nodes["check"].approval is None  # type: ignore[union-attr]

    _top, child = paused_tree(workspace)  # the process ended while the call waited
    assert workspace.runs.recover() >= 1
    stopped = workspace.runs.get(child.id)
    assert stopped is not None and stopped.status == "cancelled"


async def test_l4_l5_parallel_calls_keep_their_own_runs_and_nested_result_files_are_attachments(make: Make) -> None:
    from nodestro_runtime.testing import message

    both = message(
        [
            {"type": "tool_use", "id": "call_a", "name": "workflow_wrap", "input": {"input": "ACME"}},
            {"type": "tool_use", "id": "call_b", "name": "workflow_wrap", "input": {"input": "BETA"}},
        ],
        stop_reason="tool_use",
    )
    workspace = make({"AGENT": [both, text("Done.")], "QUOTE_V1": [text("Q")] * 2})
    gate(workspace)
    quote = workspace.save_workflow(quote_workflow(approval=False))
    quote_start = quote.nodes[0].model_copy(update={"start": None})
    plain = workspace.save_workflow(
        WorkflowIn(name="Plain quote", nodes=[quote_start, *quote.nodes[1:]], edges=quote.edges)
    )
    wrap = workspace.save_workflow(
        WorkflowIn(
            name="Wrap",
            nodes=[
                Node(id="start", type="start"),
                Node(id="sub", type="subworkflow", subworkflow=SubworkflowConfig(workflow_id=plain.id, version=1)),
                Node(id="out", type="output"),
            ],
            edges=[Edge(id="e1", source="start", target="sub"), Edge(id="e2", source="sub", target="out")],
        )
    )
    agent = AgentConfig(
        name="Agent",
        system_prompt="AGENT",
        max_parallel_tools=2,
        workflow_tools=[binding_for(workspace, wrap, 1, name="workflow_wrap")],
    )
    template = AgentTemplates(workspace.store).create(agent).id
    conversation_id = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template), model=CLOUD), owner=None
    ).id
    sent = await workspace.conversations.send(conversation_id, None, MessageIn(content="Two quotes"))
    run_id = sent.assistant.run_id or ""

    def read() -> ConversationMessage:
        return workspace.conversations.message(conversation_id, None, sent.assistant.id)

    approvals = ToolApprovals(workspace.store)
    for _ in range(2):
        found = await until(read, lambda item: bool(item.approval_ids))
        request = approvals.get(run_id, found.approval_ids[0])
        assert request is not None
        workspace.runs.decide_tool_call(run_id, request.id, approved=True, by="local", arguments=request.arguments_hash)
        await until(read, lambda item, asked=request.id: asked not in item.approval_ids)
    await workspace.wait(run_id, timeout=20)
    answer = read()
    assert answer.status == "succeeded"
    inputs = {}
    for call in answer.tool_calls:
        assert call.workflow is not None
        child = workspace.runs.get(call.workflow.run_id)
        assert child is not None
        inputs[call.id] = child.input
    assert inputs == {"call_a": "ACME", "call_b": "BETA"}
    assert len(answer.result_files) == 2 and {item.name for item in answer.result_files} == {"quote.md"}
    assert all(
        item.run_id not in {call.workflow.run_id for call in answer.tool_calls if call.workflow}
        for item in answer.result_files
    )


# ---- RT-096: a waiting call frees its working slot ------------------------------------------------------------------


def quote_and_lookup(workspace: Workspace) -> tuple[str, str]:
    """Conversation A calls the quote workflow (a confirmation and an approval step: it waits for a person twice),
    conversation B a reading lookup workflow; both agents' steps have workflow tools at depth 0."""
    gate(workspace)
    quote = workspace.save_workflow(quote_workflow())
    first = conversation(workspace, binding_for(workspace, quote, 1))
    lookup = workspace.save_workflow(
        WorkflowIn(
            name="Lookup",
            nodes=[Node(id="start", type="start"), agent_node("find", "LOOKUP", tools=["calculator"])],
            edges=[Edge(id="e1", source="start", target="find")],
        )
    )
    agent = AgentConfig(
        name="Agent B",
        system_prompt="SEARCHER",
        workflow_tools=[binding_for(workspace, lookup, 1, name="workflow_lookup")],
    )
    template = AgentTemplates(workspace.store).create(agent).id
    second = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template), model=CLOUD), owner=None
    ).id
    return first, second


def quote_script() -> dict[str, list[Any]]:
    return {
        "AGENT": [tool_use("workflow_quote", {"customer": "ACME", "amount": 5}), text("Quote ready.")],
        "QUOTE_V1": [text("Quote for ACME")],
        "SEARCHER": [tool_use("workflow_lookup", {"input": "ACME"}), text("Found.")] * 2,
        "LOOKUP": [text("ACME is a customer")] * 2,
    }


async def lookup_finishes(workspace: Workspace, conversation_id: str) -> None:
    """A step with workflow tools of the same depth runs to its end while the other call waits."""
    sent = await workspace.conversations.send(conversation_id, None, MessageIn(content="Who is ACME?"))
    await workspace.wait(sent.assistant.run_id or "", timeout=15)
    found = workspace.conversations.message(conversation_id, None, sent.assistant.id)
    assert found.status == "succeeded", found.model_dump_json()[:3000]
    assert "ACME is a customer" in (found.tool_calls[0].result or "")


def slots_free(workspace: Workspace) -> bool:
    return all(item.working == 0 and item.waiting == 0 for item in workspace.runs._workflow_slots.values())


async def test_rt096_a_call_waiting_for_a_person_never_holds_back_a_step_of_the_same_depth(make: Make) -> None:
    workspace = make(quote_script())
    workspace.runs.run_threads = 1  # one working slot per depth: before RT-096 the waiting call held it
    first, second = quote_and_lookup(workspace)
    sent = await workspace.conversations.send(first, None, MessageIn(content="A quote for ACME"))
    run_id = sent.assistant.run_id or ""

    def read() -> ConversationMessage:
        return workspace.conversations.message(first, None, sent.assistant.id)

    # 1. The call waits for the confirmation of the workflow's start (a tool approval in the agent's thread).
    waiting = await until(read, lambda found: bool(found.approval_ids))
    await lookup_finishes(workspace, second)
    request = ToolApprovals(workspace.store).get(run_id, waiting.approval_ids[0])
    assert request is not None
    workspace.runs.decide_tool_call(run_id, request.id, approved=True, by="local", arguments=request.arguments_hash)

    # 2. The workflow waits at its approval step (the call waits in the agent's thread for up to a day).
    paused = await until(
        read,
        lambda found: bool(
            found.tool_calls and found.tool_calls[0].workflow and found.tool_calls[0].workflow.approvals
        ),
    )
    await lookup_finishes(workspace, second)
    [slots] = workspace.runs._workflow_slots.values()
    assert (slots.working, slots.waiting) == (0, 1)

    # 3. The decision lets the call go on: it takes a working slot again and the answer ends.
    card = paused.tool_calls[0].workflow.approvals[0]  # type: ignore[union-attr]
    workspace.runs.decide(card.run_id, card.node_id, "approved")
    await workspace.wait(run_id, timeout=20)
    assert read().status == "succeeded"
    assert slots_free(workspace)


@pytest.mark.parametrize("end", ["cancel", "time_limit"])
async def test_rt096_a_cancelled_or_timed_out_waiting_call_gives_its_slot_back(
    make: Make, monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    from nodestro_runtime import engine

    workspace = make(quote_script())
    workspace.runs.run_threads = 1
    first, second = quote_and_lookup(workspace)
    if end == "time_limit":
        monkeypatch.setattr(engine, "WORKFLOW_TOOL_WAIT_SECONDS", 1.0)
    sent = await workspace.conversations.send(first, None, MessageIn(content="A quote for ACME"))
    run_id = sent.assistant.run_id or ""

    def read() -> ConversationMessage:
        return workspace.conversations.message(first, None, sent.assistant.id)

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
    if end == "cancel":
        workspace.conversations.cancel(first, None, sent.assistant.id)
    await workspace.wait(run_id, timeout=20)
    answer = read()
    if end == "cancel":
        assert answer.status == "cancelled"
    else:
        assert answer.status == "succeeded" and "Nobody decided in time" in (answer.tool_calls[0].result or "")
    child = workspace.runs.get(child_id)
    assert child is not None and child.status == "cancelled"
    assert slots_free(workspace)
    await lookup_finishes(workspace, second)
    assert slots_free(workspace)


async def test_rt096_h1_a_wait_inside_a_child_run_never_moves_the_calling_steps_slot(make: Make) -> None:
    """Review H1: the child run's ordinary step waits in its own thread for a tool approval. Before the fix it
    inherited the calling step's slot and parked it; the other conversation's step took that slot, and once the
    approval was decided the child's thread waited for the parent's slot (never freed) while the other step's
    workflow waited for that very thread: a deadlock with one thread. Now the calling step keeps its slot (it is
    busy with its child run), the other step waits for it, and everything ends."""
    from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE

    tool = "celsius_to_fahrenheit"
    workspace = make(
        {
            "AGENT": [tool_use("workflow_convert", {"input": "100"}), text("Converted.")],
            "WORKER": [tool_use(tool, {"celsius": 100}), text("Child done.")],
            "SEARCHER": [tool_use("workflow_lookup", {"input": "ACME"}), text("Found.")],
            "LOOKUP": [text("ACME is a customer")],
        }
    )
    workspace.runs.run_threads = 1  # one working slot per depth and one thread for ordinary steps
    result, _ = workspace.tools.save_custom(
        CUSTOM_TOOL_TEMPLATE,
        CUSTOM_TESTS_TEMPLATE,
        tool,
        trust=workspace.tools.editor_trust(tool, CUSTOM_TOOL_TEMPLATE),
    )
    assert result.ok, result
    gate(workspace)  # approvals are waited for in the asking thread (no checkpoints)
    assert workspace.runs.tool_gate is not None and not workspace.runs.tool_gate.checkpoints
    convert = workspace.save_workflow(
        WorkflowIn(
            name="Convert",
            nodes=[Node(id="start", type="start"), agent_node("work", "WORKER", tools=[tool])],
            edges=[Edge(id="e1", source="start", target="work")],
        )
    )
    first = conversation(workspace, binding_for(workspace, convert, 1, name="workflow_convert"))
    _quote, second = quote_and_lookup(workspace)
    approvals = ToolApprovals(workspace.store)

    sent = await workspace.conversations.send(first, None, MessageIn(content="Convert 100"))
    run_id = sent.assistant.run_id or ""
    # The start of the changing workflow is confirmed (the calling step itself waits: it may park).
    found = await until(
        lambda: workspace.conversations.message(first, None, sent.assistant.id), lambda item: bool(item.approval_ids)
    )
    asked = approvals.get(run_id, found.approval_ids[0])
    assert asked is not None
    workspace.runs.decide_tool_call(run_id, asked.id, approved=True, by="local", arguments=asked.arguments_hash)

    # The child run's step waits in its thread for the approval of its own tool call.
    def child_asks(item: ConversationMessage) -> bool:
        view = item.tool_calls[0].workflow if item.tool_calls else None
        return bool(view and view.tool_approvals)

    asking = await until(lambda: workspace.conversations.message(first, None, sent.assistant.id), child_asks)
    pointer = asking.tool_calls[0].workflow.tool_approvals[0]  # type: ignore[union-attr]
    child_request = approvals.get(pointer.run_id, pointer.request_id)
    assert child_request is not None and child_request.tool == tool
    [slots] = workspace.runs._workflow_slots.values()
    assert (slots.working, slots.waiting) == (1, 0)  # the calling step keeps its slot: its child run works

    other = await workspace.conversations.send(second, None, MessageIn(content="Who is ACME?"))
    await asyncio.sleep(0.5)  # the other step waits for the depth's only working slot
    workspace.runs.decide_tool_call(
        child_request.run_id, child_request.id, approved=False, by="local", arguments=child_request.arguments_hash
    )
    await workspace.wait(run_id, timeout=20)
    await workspace.wait(other.assistant.run_id or "", timeout=20)
    assert workspace.conversations.message(first, None, sent.assistant.id).status == "succeeded"
    assert workspace.conversations.message(second, None, other.assistant.id).status == "succeeded"
    assert slots_free(workspace)


async def test_rt096_m1_a_call_that_works_keeps_the_step_on_its_working_slot(make: Make) -> None:
    """Review M1 in a run: of two parallel calls, one waits at the workflow's approval step and one works. The step
    parks only once the working call ended."""
    from nodestro_runtime.testing import message

    both = message(
        [
            {"type": "tool_use", "id": "call_q", "name": "workflow_quote", "input": {"customer": "ACME", "amount": 5}},
            {"type": "tool_use", "id": "call_l", "name": "workflow_lookup", "input": {"input": "ACME"}},
        ],
        stop_reason="tool_use",
    )
    workspace = make({"AGENT": [both, text("Done.")], "QUOTE_V1": [text("Q")], "LOOKUP": [text("Found it")]})
    workspace.runs.run_threads = 1
    gate(workspace)
    quote = workspace.save_workflow(quote_workflow())
    lookup = workspace.save_workflow(
        WorkflowIn(
            name="Lookup",
            nodes=[Node(id="start", type="start"), agent_node("find", "LOOKUP", tools=["calculator"])],
            edges=[Edge(id="e1", source="start", target="find")],
        )
    )
    agent = AgentConfig(
        name="Agent",
        system_prompt="AGENT",
        max_parallel_tools=2,
        workflow_tools=[binding_for(workspace, quote, 1), binding_for(workspace, lookup, 1, name="workflow_lookup")],
    )
    template = AgentTemplates(workspace.store).create(agent).id
    conversation_id = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template), model=CLOUD), owner=None
    ).id
    seen: list[tuple[int, int]] = []
    original = workspace.runs._call_workflow_tool

    def observed(context: Any, node: Node, binding: WorkflowToolConfig, *args: Any) -> str:
        if binding.name == "workflow_lookup":
            [slots] = workspace.runs._workflow_slots.values()
            deadline = time.monotonic() + 10  # the quote's start waits for its confirmation meanwhile
            while not ToolApprovals(workspace.store).pending(context.run.id):
                assert time.monotonic() < deadline
                time.sleep(0.02)
            time.sleep(0.2)
            seen.append((slots.working, slots.waiting))  # while this call works
        return original(context, node, binding, *args)

    workspace.runs._call_workflow_tool = observed  # type: ignore[method-assign]
    sent = await workspace.conversations.send(conversation_id, None, MessageIn(content="Quote and look up"))
    run_id = sent.assistant.run_id or ""

    def read() -> ConversationMessage:
        return workspace.conversations.message(conversation_id, None, sent.assistant.id)

    approvals = ToolApprovals(workspace.store)
    waiting = await until(read, lambda item: bool(item.approval_ids))
    request = approvals.get(run_id, waiting.approval_ids[0])
    assert request is not None
    paused = None

    def quote_paused(item: ConversationMessage) -> bool:
        return any(call.workflow and call.workflow.approvals for call in item.tool_calls) and all(
            call.finished_at for call in item.tool_calls if call.name == "workflow_lookup"
        )

    workspace.runs.decide_tool_call(run_id, request.id, approved=True, by="local", arguments=request.arguments_hash)
    paused = await until(read, quote_paused)
    assert seen == [(1, 0)]  # the lookup worked on the step's working slot although the quote waited
    [slots] = workspace.runs._workflow_slots.values()
    assert (slots.working, slots.waiting) == (0, 1)  # only the waiting call is left: the step parked
    card = next(call.workflow for call in paused.tool_calls if call.workflow and call.workflow.approvals)
    assert card is not None
    workspace.runs.decide(card.approvals[0].run_id, card.approvals[0].node_id, "approved")
    await workspace.wait(run_id, timeout=20)
    assert read().status == "succeeded"
    assert slots_free(workspace)
