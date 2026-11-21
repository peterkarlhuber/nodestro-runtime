"""Offline per agent and per conversation (N-09): the strict offline mode's rules for one agent or one conversation,
enforced at every call.

Covered: the default stays off and byte-identical; an offline agent cannot use a cloud model (its own, its judge's)
or a tool with external reach (built-in internet tools, remote MCP servers, own tools that reach outside), while
another agent of the same workspace still can; the refusals happen at call time even when the pre-check is skipped
(``RunManager.start``), so a tool or model reached any other way is refused too; local MCP servers and internal tools
stay; an offline conversation makes its answers and every run below them offline and is read at every call; run chat
about an offline agent's run and the agent test keep to local models; the honest per-tool state in the agent's
validation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from nodestro_runtime import offline
from nodestro_runtime.agent_templates import AgentTemplates
from nodestro_runtime.agent_test import AgentTestIn
from nodestro_runtime.conversations import AgentChoice, ConversationChange, ConversationIn, MessageIn
from nodestro_runtime.local_models import LOCAL_DEFAULT_MODEL
from nodestro_runtime.mcp.models import SERVERS, TOOLS, McpServer, McpTool, StdioCommand, ToolDefinition
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import Connection
from nodestro_runtime.schemas import AgentConfig, QualityConfig, QualityCriterion, Run, RunIn, Workflow, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.workspace import Workspace

CLOUD = "anthropic:claude-sonnet-5-5"
LOCAL = LOCAL_DEFAULT_MODEL
REMOTE_MCP = "mcp__remote__lookup"
LOCAL_MCP = "mcp__local__lookup"
STDIO_MCP = "mcp__stdio__lookup"
Make = Callable[..., Workspace]


class LocalChat:
    """The local model server's Chat Completions API, scripted by system prompt: each entry is a text answer or a
    ``(tool name, arguments)`` call. Requests are recorded in ``calls``."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {key: list(value) for key, value in script.items()}
        self.calls: list[dict[str, Any]] = []
        self.chat = self
        self.completions = self

    def create(self, **request: Any) -> list[dict[str, Any]]:
        self.calls.append(request)
        system = str(request["messages"][0].get("content"))
        key = next(key for key in self.script if key in system)
        entry = self.script[key].pop(0)
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        if isinstance(entry, tuple):
            name, arguments = entry
            call = {
                "index": 0,
                "id": f"call_{len(self.calls)}",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            delta: dict[str, Any] = {"tool_calls": [call]}
            finish = "tool_calls"
        else:
            delta, finish = {"content": entry}, "stop"
        return [{"choices": [{"delta": delta, "finish_reason": finish}]}, {"choices": [], "usage": usage}]


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]] | None = None, local: dict[str, list[Any]] | None = None) -> Workspace:
        fake = ScriptedClient(script or {})
        chat = LocalChat(local or {})
        workspace = Workspace(
            tmp_path / "data",
            client_factory=lambda provider: chat if provider == "ollama" else fake,
            integrations=[],
        )
        workspace.fake = fake  # type: ignore[attr-defined]
        workspace.local = chat  # type: ignore[attr-defined]
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def flow(*agents: tuple[str, AgentConfig]) -> Workflow:
    nodes: list[dict[str, Any]] = [{"id": "start", "type": "start"}]
    edges: list[dict[str, Any]] = []
    for node_id, config in agents:
        nodes.append({"id": node_id, "type": "agent", "agent": config.model_dump(mode="json")})
        edges.append({"id": f"e-{node_id}", "source": "start", "target": node_id})
    return Workflow.model_validate({"id": "wf_offline", "name": "Offline", "nodes": nodes, "edges": edges})


async def finished(workspace: Workspace, workflow: Workflow, **start: Any) -> Run:
    """Start without the pre-check (``RunManager.start``): what is refused here is refused at call time."""
    run = workspace.runs.start(workflow, RunIn(input="go"), **start)
    return await workspace.wait(run.id, timeout=20)


def systems(workspace: Workspace) -> list[str]:
    return [str(call.get("system")) for call in workspace.fake.messages.calls]  # type: ignore[attr-defined]


def add_mcp(workspace: Workspace, tool_name: str, *, url: str, transport: str = "http") -> None:
    slug = tool_name.split("__")[1]
    server = McpServer(
        slug=slug,
        name=slug.title(),
        url=url,
        transport=transport,  # type: ignore[arg-type]
        stdio=StdioCommand(requirements=["lookup-server==1.0"], module="lookup_server")
        if transport == "stdio"
        else None,
    )
    definition = ToolDefinition(name="lookup", description="Look up")
    digest = definition.digest()
    tool = McpTool(
        server_id=server.id,
        name="lookup",
        tool_name=tool_name,
        definition=definition,
        hash=digest,
        status="approved",
        access="read",
        approved_definition=definition,
        approved_hash=digest,
    )
    workspace.store.put(SERVERS, server.id, server)
    workspace.store.put(TOOLS, tool.key, tool)


# ---- the default stays off --------------------------------------------------------------------------------------


def test_offline_is_off_by_default_and_left_out_of_stored_agents() -> None:
    config = AgentConfig()
    assert config.offline is False
    assert "offline" not in config.model_dump()
    assert AgentConfig(offline=True).model_dump()["offline"] is True
    assert offline.config_issues(config, object()) == []


def test_offline_agent_validation_reports_each_tool_honestly(make: Make) -> None:
    workspace = make()
    add_mcp(workspace, REMOTE_MCP, url="https://mcp.example.com/mcp")
    add_mcp(workspace, LOCAL_MCP, url="http://127.0.0.1:8765/mcp")
    tools = ["calculator", "search_web", "fetch_web_page", REMOTE_MCP, LOCAL_MCP]
    config = AgentConfig(model=CLOUD, tools=tools, offline=True)
    issues = {(item.code, item.params.get("name")): item for item in workspace.builder.validate(config)}
    assert issues[("offline.cloud_model", None)].level == "error"
    assert issues[("offline.cloud_model", None)].field == "model"
    for name, reason in (("search_web", "network"), ("fetch_web_page", "network"), (REMOTE_MCP, "remote_mcp")):
        found = issues[("offline.tool_unavailable", name)]
        assert (found.level, found.field, found.params["reason"]) == ("warning", "tools", reason)
        assert "not available offline" in found.message
    assert ("offline.tool_unavailable", "calculator") not in issues
    assert ("offline.tool_unavailable", LOCAL_MCP) not in issues
    # Not offline: the same agent has none of these issues (unchanged behaviour).
    plain = workspace.builder.validate(config.model_copy(update={"offline": False}))
    assert not [item for item in plain if item.code.startswith("offline.")]


def test_tool_states(make: Make) -> None:
    workspace = make()
    add_mcp(workspace, REMOTE_MCP, url="https://mcp.example.com/mcp")
    add_mcp(workspace, LOCAL_MCP, url="http://localhost:8765/mcp")
    add_mcp(workspace, STDIO_MCP, url="stdio:lookup_server", transport="stdio")
    states = {
        item.name: item
        for item in offline.tool_states(
            workspace.tools,
            [
                "calculator",
                "draft_email",
                "search_web",
                REMOTE_MCP,
                LOCAL_MCP,
                STDIO_MCP,
                "mcp__gone__x",
                "no_such_tool",
            ],
        )
    }
    assert states["calculator"].available and states["draft_email"].available
    assert states[LOCAL_MCP].available and states[STDIO_MCP].available
    assert (states["search_web"].available, states["search_web"].reason) == (False, "network")
    assert (states[REMOTE_MCP].available, states[REMOTE_MCP].reason) == (False, "remote_mcp")
    # Fails closed: what cannot be found is not available.
    assert (states["mcp__gone__x"].available, states["mcp__gone__x"].reason) == (False, "unknown")
    assert (states["no_such_tool"].available, states["no_such_tool"].reason) == (False, "unknown")


def test_an_own_tool_that_reaches_outside_is_not_available_offline() -> None:
    from nodestro_runtime.tool_policy import ToolCapability

    class Info:
        def __init__(self, reach: str) -> None:
            self.capability = ToolCapability(access="read", reach=reach, source="author")  # type: ignore[arg-type]

    class Library:
        builtin: ClassVar[dict[str, Any]] = {}
        mcp = None

        def get(self, name: str) -> Info:
            return Info("internal" if name == "inside" else "external")

    assert offline.tool_state(Library(), "inside").available
    assert offline.tool_state(Library(), "outside").reason == "external_reach"


def test_connections_of_an_offline_call() -> None:
    local = Connection(provider="ollama", base_url="http://127.0.0.1:11434/v1")
    assert offline.check_connection(local, "agent") is local
    assert offline.check_connection(Connection(provider="ollama", base_url="http://ollama:11434"), "agent")
    for connection, code in (
        (Connection(provider="anthropic", api_key="k"), "offline.cloud_model"),
        (Connection(provider="openai_compatible", base_url="http://127.0.0.1:8000/v1"), "offline.cloud_model"),
        (Connection(provider="ollama", base_url="https://gpu.example.com/v1"), "offline.remote_endpoint"),
    ):
        with pytest.raises(NodestroError) as caught:
            offline.check_connection(connection, "conversation")
        assert caught.value.code == code and caught.value.params["scope"] == "conversation"


# ---- models: refused at call time, another agent unaffected -----------------------------------------------------


async def test_an_offline_agent_cannot_use_a_cloud_model_while_another_agent_can(make: Make) -> None:
    workspace = make({"OFFLINE": [text("never")], "ONLINE": [text("online answer")]})
    run = await finished(
        workspace,
        flow(
            ("off", AgentConfig(system_prompt="OFFLINE", model=CLOUD, offline=True)),
            ("on", AgentConfig(system_prompt="ONLINE", model=CLOUD)),
        ),
    )
    assert run.nodes["off"].status == "failed"
    assert run.nodes["off"].error_code == "offline.cloud_model"
    assert run.nodes["off"].error_params == {"model": "anthropic", "scope": "agent"}
    assert run.nodes["on"].status == "succeeded" and run.nodes["on"].output == "online answer"
    assert not any("OFFLINE" in item for item in systems(workspace))  # nothing was sent for the offline agent


async def test_the_pre_check_refuses_an_offline_agent_with_a_cloud_model(make: Make) -> None:
    workspace = make()
    with pytest.raises(NodestroError) as caught:
        await workspace.run(flow(("off", AgentConfig(model=CLOUD, offline=True))), RunIn(input="go"))
    assert caught.value.code == "run.agents_invalid"


async def test_an_offline_agent_with_a_local_model_answers(make: Make) -> None:
    workspace = make(local={"LOCAL": ["local answer"]})
    run = await finished(workspace, flow(("off", AgentConfig(system_prompt="LOCAL", model=LOCAL, offline=True))))
    assert run.status == "succeeded" and run.nodes["off"].output == "local answer"
    assert systems(workspace) == []


async def test_a_cloud_judge_of_an_offline_agent_is_refused_at_call_time(make: Make) -> None:
    workspace = make({"judge": [text("never")]}, local={"LOCAL": ["draft"]})
    quality = QualityConfig(
        enabled=True, criteria=[QualityCriterion(name="correct", description="It is correct")], judge_model=CLOUD
    )
    run = await finished(
        workspace, flow(("off", AgentConfig(system_prompt="LOCAL", model=LOCAL, quality=quality, offline=True)))
    )
    assert run.nodes["off"].error_code == "offline.cloud_model"
    assert systems(workspace) == []
    issues = workspace.builder.validate(AgentConfig(model=LOCAL, quality=quality, offline=True))
    assert [(item.code, item.field) for item in issues if item.code.startswith("offline.")] == [
        ("offline.cloud_model", "quality")
    ]


async def test_a_local_model_server_elsewhere_is_refused_at_call_time(make: Make) -> None:
    workspace = make(local={"LOCAL": ["never"]})
    real = workspace.runs.connect

    def remote(provider: str) -> Connection:
        found = real(provider)
        return Connection(provider=found.provider, base_url="https://gpu.example.com/v1", client=found.client)

    workspace.runs.connect = remote
    run = await finished(workspace, flow(("off", AgentConfig(system_prompt="LOCAL", model=LOCAL, offline=True))))
    assert run.nodes["off"].error_code == "offline.remote_endpoint"
    assert workspace.local.calls == []  # type: ignore[attr-defined]


async def test_server_tools_of_an_offline_agent_never_start(make: Make) -> None:
    workspace = make(local={"LOCAL": ["never"]})
    run = await finished(
        workspace,
        flow(("off", AgentConfig(system_prompt="LOCAL", model=LOCAL, server_tools=["web_search"], offline=True))),
    )
    assert run.nodes["off"].error_code == "offline.server_tools"
    assert workspace.local.calls == []  # type: ignore[attr-defined]


# ---- tools and MCP: refused at call time --------------------------------------------------------------------------


def _answering_mcp(workspace: Workspace) -> list[str]:
    """Replace the MCP hub's call with one that answers without a server; returns the names it was called with."""
    called: list[str] = []

    def call(tool_name: str, _payload: dict[str, Any], **_kwargs: Any) -> Any:
        from types import SimpleNamespace

        called.append(tool_name)
        server = SimpleNamespace(name="Remote", max_output_chars=20_000)
        return server, None, SimpleNamespace(text="looked up", truncated=False, is_error=False)

    workspace.mcp.call = call  # type: ignore[method-assign]
    return called


async def test_an_offline_agent_cannot_call_internet_tools_but_internal_ones(make: Make) -> None:
    local = {
        "LOCAL": [
            ("search_web", {"query": "rain"}),
            ("fetch_web_page", {"url": "https://example.com"}),
            (REMOTE_MCP, {}),
            ("calculator", {"expression": "6 * 7"}),
            "done",
        ]
    }
    workspace = make({"ONLINE": [tool_use(REMOTE_MCP, {}), text("found")]}, local)
    add_mcp(workspace, REMOTE_MCP, url="https://mcp.example.com/mcp")
    called = _answering_mcp(workspace)
    tools = ["search_web", "fetch_web_page", REMOTE_MCP, "calculator"]
    run = await finished(
        workspace,
        flow(
            ("off", AgentConfig(system_prompt="LOCAL", model=LOCAL, tools=tools, offline=True)),
            ("on", AgentConfig(system_prompt="ONLINE", model=CLOUD, tools=[REMOTE_MCP])),
        ),
    )
    calls = {item.name: item for item in run.nodes["off"].tool_calls}
    for name in ("search_web", "fetch_web_page", REMOTE_MCP):
        approval = calls[name].approval
        assert approval is not None and (approval.status, approval.code) == ("denied", "offline.tool_unavailable")
        assert "not available offline" in (calls[name].output or "")
    assert calls["calculator"].approval is None and "42" in (calls["calculator"].output or "")
    assert run.nodes["off"].status == "succeeded" and run.nodes["off"].output == "done"
    assert "its MCP server is not on this computer" in (calls[REMOTE_MCP].output or "")
    # The other agent of the same workspace still calls the remote MCP tool and uses its cloud model.
    [online] = run.nodes["on"].tool_calls
    assert online.approval is None and "looked up" in (online.output or "")
    assert run.nodes["on"].output == "found"
    assert called == [REMOTE_MCP]  # once, for the agent that is not offline
    assert {item.code for item in run.warnings if item.node_id == "off"} == {"offline.tool_unavailable"}


async def test_an_offline_agent_may_call_a_local_mcp_tool(make: Make) -> None:
    workspace = make(local={"LOCAL": [(LOCAL_MCP, {}), "used it"]})
    add_mcp(workspace, LOCAL_MCP, url="http://127.0.0.1:8765/mcp")
    called = _answering_mcp(workspace)
    run = await finished(
        workspace, flow(("off", AgentConfig(system_prompt="LOCAL", model=LOCAL, tools=[LOCAL_MCP], offline=True)))
    )
    [call] = run.nodes["off"].tool_calls
    assert call.approval is None and "looked up" in (call.output or "")
    assert called == [LOCAL_MCP]


async def test_the_tool_check_needs_no_approval_gate_and_comes_before_it(make: Make) -> None:
    from nodestro_runtime.tool_approvals import ToolApprovals, ToolGate

    workspace = make(local={"LOCAL": [("fetch_web_page", {"url": "https://example.com"}), "ok"]})
    workspace.runs.tool_gate = ToolGate(ToolApprovals(workspace.store, poll_seconds=0.02))
    run = await finished(
        workspace,
        flow(("off", AgentConfig(system_prompt="LOCAL", model=LOCAL, tools=["fetch_web_page"], offline=True))),
    )
    assert run.status == "succeeded"
    [call] = run.nodes["off"].tool_calls
    assert call.approval is not None and call.approval.code == "offline.tool_unavailable"
    assert call.approval.request_id is None  # nobody was asked


# ---- conversations: every call of the answer and below it ---------------------------------------------------------


def template(workspace: Workspace, **fields: Any) -> str:
    return AgentTemplates(workspace.store).create(AgentConfig(name="Agent", system_prompt="AGENT", **fields)).id


async def test_an_offline_conversation_denies_internet_tools_and_another_conversation_keeps_them(make: Make) -> None:
    local = {"AGENT": [("fetch_web_page", {"url": "https://example.com"}), ("calculator", {"expression": "2*3"}), "6"]}
    workspace = make(local=local)
    agent = template(workspace, model=LOCAL, tools=["calculator", "fetch_web_page"])
    created = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=agent), model=LOCAL, offline=True), owner=None
    )
    sent = await workspace.conversations.send(created.id, None, MessageIn(content="2 times 3?"))
    run = await workspace.wait(sent.assistant.run_id or "", timeout=20)
    assert run.status == "succeeded"
    [node] = [item for item in run.workflow.nodes if item.agent is not None]
    assert node.agent is not None and node.agent.offline is True
    fetch, calculate = run.nodes[node.id].tool_calls
    assert fetch.approval is not None and fetch.approval.code == "offline.tool_unavailable"
    assert calculate.approval is None and "6" in (calculate.output or "")
    # The same agent in a conversation that is not offline may use a cloud model.
    other = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=agent), model=CLOUD), owner=None
    )
    assert other.model == CLOUD


async def test_an_offline_agent_template_answers_with_local_models_only(make: Make) -> None:
    workspace = make()
    agent = template(workspace, offline=True)
    with pytest.raises(NodestroError) as caught:
        workspace.conversations.create(ConversationIn(agent=AgentChoice(template_id=agent), model=CLOUD), owner=None)
    assert caught.value.code == "conversation.offline_cloud_model"
    created = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=agent), model=LOCAL), owner=None
    )
    with pytest.raises(NodestroError) as caught:
        workspace.conversations.update(created.id, None, ConversationChange(model=CLOUD))
    assert caught.value.code == "conversation.offline_cloud_model"
    workspace.conversations.update(created.id, None, ConversationChange(title="Renamed"))  # other changes still work


def _child_flow(workspace: Workspace) -> Workflow:
    child = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Child",
                "nodes": [
                    {"id": "s", "type": "start"},
                    {"id": "a", "type": "agent", "agent": {"system_prompt": "CHILD", "model": CLOUD}},
                ],
                "edges": [{"id": "e", "source": "s", "target": "a"}],
            }
        )
    )
    return Workflow.model_validate(
        {
            "id": "wf_parent",
            "name": "Parent",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "sub", "type": "subworkflow", "subworkflow": {"workflow_id": child.id}},
            ],
            "edges": [{"id": "e", "source": "start", "target": "sub"}],
        }
    )


async def test_a_conversation_is_read_at_every_call_also_below_its_answer(make: Make) -> None:
    """The run of an answer, and a sub-workflow below it, read the conversation's flag at each call: a cloud model
    is refused in the child run although the child's agent is not offline. Switched off, it is used again."""
    workspace = make({"CHILD": [text("cloud")]})
    parent = _child_flow(workspace)
    created = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template(workspace)), model=LOCAL, offline=True), owner=None
    )
    run = await finished(workspace, parent, trigger="conversation", trigger_ref=created.id)
    child_run = workspace.runs.get(run.nodes["sub"].child_run_id or "")
    assert child_run is not None
    assert child_run.nodes["a"].error_code == "offline.cloud_model"
    assert child_run.nodes["a"].error_params["scope"] == "conversation"
    assert systems(workspace) == []
    workspace.conversations.update(created.id, None, ConversationChange(offline=False))
    again = await finished(workspace, parent, trigger="conversation", trigger_ref=created.id)
    assert again.status == "succeeded"
    assert any("CHILD" in item for item in systems(workspace))


async def test_an_orchestrator_below_an_offline_conversation_uses_no_cloud_model(make: Make) -> None:
    workspace = make()
    member = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Team",
                "description": "A team",
                "nodes": [{"id": "s", "type": "start"}, {"id": "w", "type": "agent", "agent": {"model": LOCAL}}],
                "edges": [{"id": "e", "source": "s", "target": "w"}],
            }
        )
    )
    lead = Workflow.model_validate(
        {
            "id": "wf_lead",
            "name": "Lead",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "lead",
                    "type": "orchestrator",
                    "orchestrator": {"model": CLOUD, "members": [{"workflow_id": member.id}]},
                },
            ],
            "edges": [{"id": "e", "source": "start", "target": "lead"}],
        }
    )
    created = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template(workspace)), model=LOCAL, offline=True), owner=None
    )
    run = await finished(workspace, lead, trigger="conversation", trigger_ref=created.id)
    assert run.nodes["lead"].error_code == "offline.cloud_model"
    assert systems(workspace) == []


def test_an_unknown_conversation_counts_as_offline(make: Make) -> None:
    workspace = make()
    assert workspace.conversations.is_offline("cnv_missing") is True
    created = workspace.conversations.create(
        ConversationIn(agent=AgentChoice(template_id=template(workspace)), model=LOCAL), owner=None
    )
    assert workspace.conversations.is_offline(created.id) is False


# ---- run chat and the agent test ----------------------------------------------------------------------------------


async def test_run_chat_about_a_run_with_an_offline_team_keeps_to_local_models(make: Make) -> None:
    from nodestro_runtime.run_chat import answer_question
    from nodestro_runtime.testing import parsed

    ids: dict[str, str] = {}
    boss = "You coordinate teams"
    workspace = make(
        {
            boss: [
                parsed(lambda model: model(action="delegate", member=ids["team"], task="Draft", reason="r")),
                parsed(lambda model: model(action="finish", answer="done", reason="r")),
            ],
            "You answer a person's question": [text("never")],
        },
        local={"WRITER": ["secret local draft"]},
    )
    team = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Writer",
                "description": "Writes",
                "nodes": [
                    {"id": "s", "type": "start"},
                    {
                        "id": "w",
                        "type": "agent",
                        "agent": {"system_prompt": "WRITER", "model": LOCAL, "offline": True},
                    },
                ],
                "edges": [{"id": "e", "source": "s", "target": "w"}],
            }
        )
    )
    ids["team"] = team.id
    lead = Workflow.model_validate(
        {
            "id": "wf_lead",
            "name": "Lead",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "lead",
                    "type": "orchestrator",
                    "orchestrator": {"model": CLOUD, "members": [{"workflow_id": team.id}]},
                },
            ],
            "edges": [{"id": "e", "source": "start", "target": "lead"}],
        }
    )
    run = await finished(workspace, lead)
    assert run.status == "succeeded", run.error
    question, _new = workspace.runs.chat.submit(
        run_id=run.id, target_run_id=run.id, node_id="lead", kind="question", content="What happened?"
    )
    result = await asyncio.to_thread(
        answer_question, workspace.runs, workspace.runs.chat, workspace.usage_log, question, model=CLOUD
    )
    assert result.status == "failed" and result.code == "offline.cloud_model"
    assert not any("You answer" in item for item in systems(workspace))


def test_run_offline_looks_at_the_run_and_its_parents(make: Make) -> None:
    workspace = make()
    plain = Run(workflow_id="w", workflow_name="W", workflow=flow(("a", AgentConfig())))
    marked = Run(workflow_id="w", workflow_name="W", workflow=flow(("a", AgentConfig(offline=True))))
    assert workspace.runs.run_offline(plain) is False
    assert workspace.runs.run_offline(marked) is True
    workspace.store.put("runs", marked.id, marked)
    below = plain.model_copy(update={"id": "run_below", "parent_run_id": marked.id})
    assert workspace.runs.run_offline(below) is True
    orphan = plain.model_copy(update={"id": "run_orphan", "parent_run_id": "run_gone"})
    assert workspace.runs.run_offline(orphan) is True  # fails closed


def test_the_agent_test_of_an_offline_agent(make: Make) -> None:
    workspace = make(local={"LOCAL": [("fetch_web_page", {"url": "https://example.com"}), "fine"]})
    refused, _ = workspace.tester.run(AgentTestIn(agent=AgentConfig(model=CLOUD, offline=True), input="hi"))
    assert (refused.status, refused.error_code) == ("failed", "offline.cloud_model")
    config = AgentConfig(system_prompt="LOCAL", model=LOCAL, tools=["fetch_web_page"], offline=True)
    result, _ = workspace.tester.run(AgentTestIn(agent=config, input="hi"))
    assert result.status == "succeeded", result.error
    [call] = result.tool_calls
    assert call.approval is not None and call.approval.code == "offline.tool_unavailable"
