"""The owner's consent before a workflow uses a private memory space (RT-089d), and private runs in the inbox.

On a server a memory space is its owner's alone (RT-089c). Before RT-089d any editor could build a shared workflow
that names Alice's space: when Alice ran it, its steps read her memory and could hand it to any tool of the workflow.
Now a step opens a space only with Alice's consent for exactly this workflow content (its digest); a change, a
workflow around it or after it, a continued run and a revoked consent all fail closed with
``memory.consent_required``. Every denial is checked to reach no model.

The workspaces are desktop workspaces given the owner store a server tenant gets, as in ``test_memory_space_owners``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import contextvars
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime.agent_test import AgentTestIn
from nodestro_runtime.connections import ConnectionIn, ConnectionPatch
from nodestro_runtime.mcp.models import SERVERS, TOOLS, McpServer, McpTool, ToolDefinition
from nodestro_runtime.memory_consents import MAX_TARGETS, combined_digest, consent_targets, workflow_digest
from nodestro_runtime.memory_spaces import CONSENTS, NOBODY
from nodestro_runtime.notifications import LocalInbox, RunNotifier
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import AgentConfig, MemoryConfig, NotificationRules, Run, RunIn, ToolSettings, Workflow
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.workspace import Workspace

ALICE, BOB = "usr_alice", "usr_bob"
SECRET = "salary zanzibar"
VIEW = tool_use("memory", {"command": "view", "path": "/memories/note.md"})
Make = Callable[..., Workspace]


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]], *, owners: bool = True) -> Workspace:
        client = ScriptedClient(script)
        workspace = Workspace(tmp_path / f"data{len(opened)}", client_factory=lambda _p: client, integrations=[])
        if owners:
            workspace.memory.owners = workspace.store  # what a server tenant's workspace gets
        workspace.client = client  # type: ignore[attr-defined]
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def _agent(prompt: str = "MEM", space: str | None = "team", name: str = "Mem") -> AgentConfig:
    memory = MemoryConfig(mode="persistent", space=space) if space else MemoryConfig()
    return AgentConfig(name=name, system_prompt=prompt, model="anthropic:claude-sonnet-5-5", memory=memory)


def _flow(workflow_id: str, *nodes: dict[str, Any], name: str = "Shared") -> Workflow:
    """A chain: start -> nodes in order."""
    items = [{"id": "start", "type": "start"}, *nodes]
    edges = [
        {"id": f"e{i}", "source": items[i]["id"], "target": items[i + 1]["id"]}
        | ({"source_handle": "approved"} if items[i]["type"] == "approval" else {})
        for i in range(len(items) - 1)
    ]
    return Workflow.model_validate({"id": workflow_id, "name": name, "nodes": items, "edges": edges})


def _agent_node(node_id: str, agent: AgentConfig) -> dict[str, Any]:
    return {"id": node_id, "type": "agent", "agent": agent.model_dump()}


def _sub(node_id: str, workflow_id: str) -> dict[str, Any]:
    return {"id": node_id, "type": "subworkflow", "subworkflow": {"workflow_id": workflow_id}}


def _calls(workspace: Workspace) -> int:
    return len(workspace.client.messages.calls)  # type: ignore[attr-defined]


def _sent(workspace: Workspace) -> str:
    return str(workspace.client.messages.calls)  # type: ignore[attr-defined]


def _seed(workspace: Workspace) -> None:
    """Alice's space ``team`` with her note (she wrote it herself)."""
    workspace.memory.open_for("team", ALICE).write("/memories/note.md", SECRET)


def _allow(workspace: Workspace, workflow_id: str, owner: str = ALICE, space: str = "team") -> list[str]:
    """What the HTTP API does when the owner allows a workflow: every workflow it runs, in its current content."""
    targets = _targets(workspace, workflow_id)
    for target in targets:
        workspace.memory.grant(
            space, owner, "workflow", target.digest, workflow_id=target.workflow_id, workflow_version=target.version
        )
    return [target.workflow_id for target in targets]


def _targets(workspace: Workspace, workflow_id: str) -> Any:
    runs = workspace.runs
    return consent_targets(workspace.resolve_workflow, runs.pin_subworkflows, runs.consent_digest, workflow_id)


def _digest(workspace: Workspace, flow: Workflow) -> str:
    """The digest of ``flow`` as a run executes it (pinned; with its agents' means)."""
    return workspace.runs.consent_digest(workspace.runs.pin_subworkflows(flow))


def _consented(workspace: Workspace, flow: Workflow) -> bool:
    return workspace.memory.consented("team", ALICE, "workflow", flow.id, _digest(workspace, flow))


async def _start(workspace: Workspace, flow: Workflow, by: str | None = ALICE, **start: Any) -> Run:
    run = workspace.runs.start(flow, RunIn(input="go"), started_by=by, **start)
    return await workspace.wait(run.id, timeout=30)


# ---- the PoC: a shared workflow that names Alice's space ------------------------------------------------------------


async def test_poc_a_shared_workflow_uses_alices_memory_only_after_her_consent(make: Make) -> None:
    """PoC (RT-089d): Bob builds a shared workflow whose agent reads the space ``team``; Alice owns it. Before, Alice's
    run of it read her note into Bob's agent. Now it fails before the agent starts; after Alice allowed exactly this
    workflow it runs; Bob's next change (a new prompt) is not covered, and Alice's run of it fails closed again."""
    workspace = make({"MEM": [VIEW, text("read")], "CHANGED": [VIEW, text("read")]})
    _seed(workspace)
    flow = workspace.save_workflow(_flow("wf_shared", _agent_node("a", _agent())))
    refused = await _start(workspace, flow)
    assert (refused.nodes["a"].status, refused.nodes["a"].error_code) == ("failed", "memory.consent_required")
    assert refused.nodes["a"].error_params == {"space": "team", "workflow_id": "wf_shared", "workflow": "Shared"}
    assert _calls(workspace) == 0  # nothing reached a model
    # Review F3: the refused run is Alice's alone, so its error (her space, the workflow) reaches no other member.
    assert (refused.memory_owner, refused.memory_spaces) == (ALICE, ["team"])
    assert workspace.conversations.run_visible(refused.id, BOB) is False
    assert workspace.conversations.run_visible(refused.id, ALICE) is True
    assert _allow(workspace, flow.id) == ["wf_shared"]
    allowed = await _start(workspace, flow)
    assert allowed.status == "succeeded", allowed.nodes["a"].error
    assert SECRET in _sent(workspace)
    assert (allowed.memory_owner, allowed.memory_spaces) == (ALICE, ["team"])
    # The stored run has the same digest as the consent: a paused run continued elsewhere is checked the same way.
    stored = workspace.store.get("runs", allowed.id, Run)
    assert stored is not None
    assert workspace.runs.consent_digest(stored.workflow) == workspace.runs.consent_digest(allowed.workflow)
    calls = _calls(workspace)
    changed = workspace.save_workflow(_flow("wf_shared", _agent_node("a", _agent("CHANGED"))))
    again = await _start(workspace, changed)
    assert again.nodes["a"].error_code == "memory.consent_required"
    assert _calls(workspace) == calls


async def test_bob_cannot_consent_for_alices_space_and_his_run_stays_not_found(make: Make) -> None:
    workspace = make({"MEM": [VIEW, text("read")]})
    _seed(workspace)
    flow = workspace.save_workflow(_flow("wf_shared", _agent_node("a", _agent())))
    digest = _digest(workspace, flow)
    with pytest.raises(NodestroError) as caught:
        workspace.memory.grant("team", BOB, "workflow", digest, workflow_id=flow.id)
    assert caught.value.code == "memory.space_not_found"
    assert workspace.store.list(CONSENTS, dict) == []  # type: ignore[type-var]
    _allow(workspace, flow.id)  # Alice's consent covers her runs only
    bobs = await _start(workspace, flow, by=BOB)
    assert bobs.nodes["a"].error_code == "memory.space_not_found"  # as before RT-089d: he learns nothing
    assert bobs.memory_spaces == []  # not his: nothing recorded, his later runs need no consent for it
    nobodys = await _start(workspace, flow, by=None, trigger="schedule", trigger_ref="sch_1")
    assert nobodys.nodes["a"].error_code == "memory.space_not_found"
    assert _calls(workspace) == 0
    with pytest.raises(ValueError):
        workspace.memory.grant("team", NOBODY, "workflow", digest, workflow_id=flow.id)
    with pytest.raises(ValueError):
        workspace.memory.grant("team", ALICE, "workflow", "not-a-digest", workflow_id=flow.id)


async def test_a_revoked_consent_denies_again(make: Make) -> None:
    workspace = make({"MEM": [VIEW, text("read")]})
    _seed(workspace)
    flow = workspace.save_workflow(_flow("wf_shared", _agent_node("a", _agent())))
    _allow(workspace, flow.id)
    (consent,) = workspace.memory.consents(ALICE)
    assert workspace.memory.consents(BOB) == []
    assert workspace.memory.revoke(consent.id, BOB) is None  # another person's consent stays
    assert workspace.memory.revoke(consent.id, ALICE) == consent
    run = await _start(workspace, flow)
    assert run.nodes["a"].error_code == "memory.consent_required" and _calls(workspace) == 0


async def test_a_new_space_also_needs_a_consent_which_claims_it(make: Make) -> None:
    """A run never creates a person's space without consent; the consent itself claims a new space (first writer)."""
    workspace = make({"MEM": [text("nothing yet")]})
    flow = workspace.save_workflow(_flow("wf_new", _agent_node("a", _agent(space="fresh"))))
    refused = await _start(workspace, flow)
    assert refused.nodes["a"].error_code == "memory.consent_required"
    assert workspace.memory.owner("fresh") is None
    _allow(workspace, flow.id, space="fresh")
    assert workspace.memory.owner("fresh") == ALICE
    assert (await _start(workspace, flow)).status == "succeeded"


# ---- workflows around and after the step ----------------------------------------------------------------------------


async def test_a_workflow_that_runs_the_memory_step_as_a_sub_workflow_needs_the_consent_too(make: Make) -> None:
    """The parent gets the step's output: Bob's parent around Alice's allowed child is refused, naming the parent."""
    workspace = make({"MEM": [VIEW, text("read")]})
    _seed(workspace)
    child = workspace.save_workflow(_flow("wf_child", _agent_node("a", _agent()), name="Child"))
    parent = workspace.save_workflow(_flow("wf_parent", _sub("sub", child.id), name="Parent"))
    _allow(workspace, child.id)
    refused = await _start(workspace, parent)
    assert refused.nodes["sub"].status == "failed"
    stored = workspace.store.get("runs", refused.nodes["sub"].child_run_id or "", Run)
    assert stored is not None and stored.nodes["a"].error_code == "memory.consent_required"
    assert stored.nodes["a"].error_params["workflow_id"] == "wf_parent"
    assert _calls(workspace) == 0
    assert _allow(workspace, parent.id) == ["wf_parent", "wf_child"]
    allowed = await _start(workspace, parent)
    assert allowed.status == "succeeded", allowed.nodes["sub"].error
    assert (allowed.memory_owner, allowed.memory_spaces) == (ALICE, ["team"])


async def test_a_workflow_started_after_the_memory_step_needs_the_consent(make: Make) -> None:
    """A sub-workflow after the step gets its output: Bob changes that sub-workflow after Alice's consent, and its run
    is refused before it starts, so the memory never reaches his new content."""
    workspace = make({"MEM": [VIEW, text("read"), VIEW, text("again")], "SEND": [text("sent")]})
    _seed(workspace)
    courier = workspace.save_workflow(_flow("wf_courier", _agent_node("c", _agent("SEND", space=None)), name="Courier"))
    flow = workspace.save_workflow(_flow("wf_main", _agent_node("a", _agent()), _sub("after", courier.id)))
    assert _allow(workspace, flow.id) == ["wf_main", "wf_courier"]
    first = await _start(workspace, flow)
    assert first.status == "succeeded", first.nodes["after"].error
    calls = _calls(workspace)
    # Bob changes the courier; the main workflow (latest sub-workflow version) now pins the changed one.
    workspace.save_workflow(_flow("wf_courier", _agent_node("c", _agent("SEND", space=None, name="Leak")), name="C"))
    second = await _start(workspace, flow)
    assert second.nodes["a"].error_code == "memory.consent_required"  # the main workflow's pins changed too
    assert _calls(workspace) == calls
    # Even when only the main workflow's own consent is renewed, the courier's new content is refused.
    pinned = workspace.runs.pin_subworkflows(workspace.resolve_workflow(flow.id, None)[0])
    workspace.memory.grant("team", ALICE, "workflow", workspace.runs.consent_digest(pinned), workflow_id=flow.id)
    third = await _start(workspace, flow)
    assert third.nodes["a"].status == "succeeded"
    courier_run = workspace.store.get("runs", third.nodes["after"].child_run_id or "", Run)
    assert courier_run is not None and courier_run.error_code == "memory.consent_required"
    assert courier_run.error_params == {"space": "team", "workflow_id": "wf_courier", "workflow": "C"}
    assert courier_run.nodes["c"].status != "succeeded"
    assert _calls(workspace) == calls + 2  # the memory step's two requests; the courier's agent never asked


async def test_a_paused_run_continues_only_while_the_consent_holds(make: Make) -> None:
    workspace = make({"MEM": [VIEW, text("read")], "NEXT": [text("next")]})
    _seed(workspace)
    flow = workspace.save_workflow(
        _flow(
            "wf_pause",
            _agent_node("a", _agent()),
            {"id": "check", "type": "approval", "approval": {"instructions": "ok?"}},
            _agent_node("b", _agent("NEXT", space=None, name="Next")),
        )
    )
    _allow(workspace, flow.id)
    paused = await _start(workspace, flow)
    assert paused.status == "paused" and paused.memory_spaces == ["team"]
    workspace.memory.revoke(workspace.memory.consents(ALICE)[0].id, ALICE)
    calls = _calls(workspace)
    workspace.decide(paused.id, "check", "approved")
    ended = await workspace.wait(paused.id, timeout=30)
    assert (ended.status, ended.error_code) == ("failed", "memory.consent_required")
    assert _calls(workspace) == calls


async def test_a_continued_run_stays_private_and_needs_the_consent(make: Make) -> None:
    """A continued run takes over the earlier run's results (the memory's output): it is the owner's alone and runs
    only with the consent for its (possibly changed) workflow. Before, it was visible to every member."""
    workspace = make({"MEM": [VIEW, text("read")]})
    _seed(workspace)
    flow = workspace.save_workflow(_flow("wf_main", _agent_node("a", _agent())))
    _allow(workspace, flow.id)
    first = await _start(workspace, flow)
    assert first.memory_owner == ALICE
    changed = workspace.save_workflow(_flow("wf_main", _agent_node("a", _agent()), _agent_node("z", _agent("X", None))))
    calls = _calls(workspace)
    continued = await _start(workspace, changed, resumed_from=first.id, preset={"a": first.nodes["a"]})
    assert (continued.status, continued.error_code) == ("failed", "memory.consent_required")
    assert (continued.memory_owner, continued.memory_spaces) == (ALICE, ["team"])
    assert workspace.conversations.run_visible(continued.id, BOB) is False
    assert _calls(workspace) == calls


async def test_a_private_run_without_recorded_spaces_fails_closed(make: Make) -> None:
    """A run marked for a person before RT-089d recorded no spaces: continuing it is refused (fail closed)."""
    workspace = make({"N": [text("n")]})
    flow = workspace.save_workflow(_flow("wf_old", _agent_node("a", _agent("N", None))))
    legacy = await _start(workspace, flow)
    legacy.memory_owner = ALICE
    workspace.store.put("runs", legacy.id, legacy)
    continued = await _start(workspace, flow, resumed_from=legacy.id)
    assert continued.error_code == "memory.consent_required" and continued.memory_owner == ALICE


async def test_the_parent_stream_is_told_when_a_sub_workflow_reads_memory(make: Make) -> None:
    """Review gap: ``memory_private`` went only to the step's own run; a reader of the parent's stream kept getting its
    events. Now every run above the step is told."""
    workspace = make({"MEM": [VIEW, text("read")]})
    _seed(workspace)
    events: list[Any] = []
    workspace.runs.event_sink = events.append
    child = workspace.save_workflow(_flow("wf_child", _agent_node("a", _agent()), name="Child"))
    middle = workspace.save_workflow(_flow("wf_middle", _sub("sub", child.id), name="Middle"))
    top = workspace.save_workflow(_flow("wf_top", _sub("sub", middle.id), name="Top"))
    _allow(workspace, top.id)
    run = await _start(workspace, top)
    assert run.status == "succeeded", run.nodes["sub"].error
    middle_id = run.nodes["sub"].child_run_id
    told = {event.run_id for event in events if event.type == "memory_private"}
    assert {run.id, middle_id} <= told and len(told) == 3
    # Review F4: the run in between is marked too (its steps got the memory step's output), not only told.
    middle_run = workspace.store.get("runs", middle_id or "", Run)
    assert middle_run is not None and middle_run.memory_owner == ALICE
    assert workspace.conversations.run_visible(middle_id or "", BOB) is False


# ---- what the agents use outside the workflow (review F1) -----------------------------------------------------------

CUSTOM = "celsius_to_fahrenheit"


def _save_tool(workspace: Workspace, code: str = CUSTOM_TOOL_TEMPLATE) -> None:
    result, info = workspace.tools.save_custom(
        code, CUSTOM_TESTS_TEMPLATE, CUSTOM, trust=workspace.tools.editor_trust(CUSTOM, code)
    )
    assert result.ok and info is not None, result


async def test_a_new_version_of_a_custom_tool_makes_the_consent_stale(make: Make) -> None:
    """Review F1: an agent names its custom tools, a run takes their latest version, and every member who may run
    workflows may save a new one. Before, Alice's consent still held after Bob's new version (one that posts what it
    gets elsewhere). Now the digest covers each tool's version and code: her run fails before the agent starts."""
    workspace = make({"MEM": [VIEW, text("read")]})
    _seed(workspace)
    _save_tool(workspace)
    agent = _agent().model_copy(update={"tools": [CUSTOM]})
    flow = workspace.save_workflow(_flow("wf_tool", _agent_node("a", agent)))
    _allow(workspace, flow.id)
    tested = workspace.runs.agent_consent_digest(agent)
    assert _consented(workspace, flow)
    _save_tool(workspace, CUSTOM_TOOL_TEMPLATE + "\n# version 2: posts its input elsewhere\n")
    assert not _consented(workspace, flow)
    assert workspace.runs.agent_consent_digest(agent) != tested  # an agent test's consent too
    refused = await _start(workspace, flow)
    assert refused.nodes["a"].error_code == "memory.consent_required" and _calls(workspace) == 0


async def test_a_changed_connection_makes_the_consent_stale(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review F1: a tool sends to the connection its settings name (``tool_settings.connection_id``); a member who
    may edit connections could point it elsewhere. The digest covers the connection's settings, reachable addresses
    and (sealed) secret, so a changed address or a new secret makes the consent stale."""
    monkeypatch.setenv("NODESTRO_VAULT_KEY", "a" * 44)
    workspace = make({"MEM": [VIEW, text("read")]})
    _seed(workspace)
    hook = workspace.connections.create(
        ConnectionIn(type="webhook", name="Hook", settings={"url": "https://hooks.example.com/in"})
    )
    bound = {"search_web": ToolSettings(connection_id=hook.id)}
    agent = _agent().model_copy(update={"tools": ["search_web"], "tool_settings": bound})
    flow = workspace.save_workflow(_flow("wf_hook", _agent_node("a", agent)))
    _allow(workspace, flow.id)
    assert _consented(workspace, flow)
    workspace.connections.update(hook.id, ConnectionPatch(settings={"url": "https://elsewhere.example.com/in"}))
    assert not _consented(workspace, flow)
    refused = await _start(workspace, flow)
    assert refused.nodes["a"].error_code == "memory.consent_required" and _calls(workspace) == 0
    _allow(workspace, flow.id)
    assert _consented(workspace, flow)
    workspace.connections.set_secret(hook.id, "another token")
    assert not _consented(workspace, flow)
    workspace.connections.delete(hook.id, force=True)
    assert not _consented(workspace, flow)


def test_a_changed_mcp_server_or_tool_makes_the_consent_stale(make: Make) -> None:
    """Review F1: an MCP tool calls the server its configuration names; the digest covers the server's address,
    transport and sign-in kind and the tool's definition, not its check times."""
    workspace = make({})
    server = McpServer(slug="docs", name="Docs", url="https://mcp.example/mcp")
    definition = ToolDefinition(name="lookup", description="Look up")
    tool = McpTool(
        server_id=server.id,
        name="lookup",
        tool_name="mcp__docs__lookup",
        definition=definition,
        hash=definition.digest(),
        status="approved",
        access="read",
    )
    workspace.store.put(SERVERS, server.id, server)
    workspace.store.put(TOOLS, tool.key, tool)
    flow = _flow("wf_mcp", _agent_node("a", _agent().model_copy(update={"tools": [tool.tool_name]})))
    before = workspace.runs.consent_digest(flow)
    workspace.store.put(SERVERS, server.id, server.model_copy(update={"last_error_code": "mcp.unreachable"}))
    assert workspace.runs.consent_digest(flow) == before  # its status is not what a call does
    workspace.store.put(SERVERS, server.id, server.model_copy(update={"url": "https://elsewhere.example/mcp"}))
    assert workspace.runs.consent_digest(flow) != before
    workspace.store.put(SERVERS, server.id, server)
    assert workspace.runs.consent_digest(flow) == before
    workspace.store.put(TOOLS, tool.key, tool.model_copy(update={"hash": "0" * 64}))
    assert workspace.runs.consent_digest(flow) != before


async def test_a_sub_workflow_without_saved_versions_is_covered_by_its_content(make: Make) -> None:
    """Review F2: a sub-workflow that was never saved as a version runs its current content, which the parent's own
    content (``version`` unset) does not pin. The parent's digest covers that content too: a change of it makes the
    consent stale before the memory step runs."""
    workspace = make({"MEM": [VIEW, text("read")]})
    _seed(workspace)
    draft = _flow("wf_draft", _agent_node("c", _agent("SEND", space=None)), name="Draft")
    workspace.store.put("workflows", draft.id, draft)  # no saved version
    parent = workspace.save_workflow(_flow("wf_parent", _agent_node("a", _agent()), _sub("after", draft.id)))
    pinned = workspace.runs.pin_subworkflows(parent)
    assert [node.subworkflow.version for node in pinned.nodes if node.subworkflow is not None] == [None]
    assert _allow(workspace, parent.id) == ["wf_parent", "wf_draft"]
    assert _consented(workspace, parent)
    leak = _flow("wf_draft", _agent_node("c", _agent("LEAK", space=None)), name="Draft")
    workspace.store.put("workflows", draft.id, leak)
    assert not _consented(workspace, parent)
    refused = await _start(workspace, parent)
    assert refused.nodes["a"].error_code == "memory.consent_required" and _calls(workspace) == 0


def test_the_combined_digest_covers_every_target(make: Make) -> None:
    """Review F2: what the owner confirms when granting is one digest over every workflow of the consent."""
    workspace = make({})
    child = workspace.save_workflow(_flow("wf_child", _agent_node("a", _agent("X", None)), name="Child"))
    parent = workspace.save_workflow(_flow("wf_parent", _sub("sub", child.id), name="Parent"))
    before = combined_digest(_targets(workspace, parent.id))
    assert combined_digest(_targets(workspace, parent.id)) == before
    workspace.save_workflow(_flow("wf_child", _agent_node("a", _agent("Y", None)), name="Child"))
    assert combined_digest(_targets(workspace, parent.id)) != before


# ---- agent tests ----------------------------------------------------------------------------------------------------


def test_an_agent_test_needs_the_consent_for_exactly_its_configuration(make: Make) -> None:
    workspace = make({"MEM": [VIEW, text("read")], "OTHER": [VIEW, text("read")]})
    _seed(workspace)

    def test_as(actor: str, agent: AgentConfig) -> Any:
        result, _ = contextvars.copy_context().run(
            workspace.tester.run, AgentTestIn(agent=agent, input="go"), actor=actor
        )
        return result

    refused = test_as(ALICE, _agent())
    assert (refused.status, refused.error_code) == ("failed", "memory.consent_required")
    assert _calls(workspace) == 0
    workspace.memory.grant("team", ALICE, "agent", workspace.runs.agent_consent_digest(_agent()))
    assert test_as(BOB, _agent()).error_code == "memory.space_not_found"  # still nothing for Bob
    changed = test_as(ALICE, _agent("OTHER"))
    assert changed.error_code == "memory.consent_required" and _calls(workspace) == 0
    allowed = test_as(ALICE, _agent())
    assert allowed.status == "succeeded", allowed.error
    assert SECRET in _sent(workspace)


async def test_the_desktop_needs_no_consent(make: Make) -> None:
    workspace = make({"MEM": [VIEW, text("read")]}, owners=False)
    flow = workspace.save_workflow(_flow("wf_any", _agent_node("a", _agent())))
    assert (await _start(workspace, flow, by="local")).status == "succeeded"
    assert workspace.memory.consents(None) == [] and workspace.memory.consented("team", None, "workflow", "x", "0")


def test_the_digest_covers_content_not_time_stamps(make: Make) -> None:
    digest = make({}).runs.consent_digest
    flow = _flow("wf_d", _agent_node("a", _agent()))
    same = flow.model_copy(update={"updated_at": flow.updated_at.replace(year=2000)})
    assert digest(flow) == digest(same)
    assert digest(flow) != digest(_flow("wf_d", _agent_node("a", _agent("X"))))
    assert digest(flow) != digest(_flow("wf_other", _agent_node("a", _agent())))
    assert make({}).runs.agent_consent_digest(_agent()) != make({}).runs.agent_consent_digest(_agent(space="other"))
    # The means are part of it: the same workflow with other means is other content.
    assert workflow_digest(flow, lambda _config: []) != workflow_digest(flow, lambda _config: ["other"])


def test_a_consent_covers_at_most_max_targets_workflows(make: Make) -> None:
    workspace = make({})
    previous = workspace.save_workflow(_flow("wf_0", _agent_node("a", _agent("X", None))))
    for number in range(1, MAX_TARGETS + 1):
        previous = workspace.save_workflow(_flow(f"wf_{number}", _sub("sub", previous.id)))
    with pytest.raises(NodestroError) as caught:
        _targets(workspace, previous.id)
    assert caught.value.code == "memory.consent_too_large"


# ---- notifications of private runs (RT-089d, requirement 2) ---------------------------------------------------------


def _private_run(**update: Any) -> Run:
    flow = _flow("wf_n", _agent_node("a", _agent()))
    flow = flow.model_copy(
        update={"notifications": NotificationRules(events=["run_failed", "run_finished"], recipients="members")}
    )
    run = Run(workflow_id=flow.id, workflow_name="Salary review", workflow=flow, status="failed", started_by=ALICE)
    return run.model_copy(update=update)


def test_a_private_runs_events_notify_only_its_owner(make: Make) -> None:
    workspace = make({})
    inbox = LocalInbox(workspace.store)
    notifier = RunNotifier(inbox)
    notifier(_private_run(memory_owner=ALICE))
    assert {item.user_id for item in inbox.recent()} == {ALICE}
    assert inbox.for_user(BOB) == [] and inbox.unread(BOB) == 0 and len(inbox.for_user(ALICE)) == 1
    notifier(_private_run(id="run_nobody", memory_owner=NOBODY))
    assert [item.run_id for item in inbox.recent() if item.run_id == "run_nobody"] == []
    notifier(_private_run(id="run_open"))  # not private: the rules decide (members)
    assert [item.user_id for item in inbox.recent() if item.run_id == "run_open"] == [None]


def test_a_private_runs_notify_user_messages_go_to_its_owner_only(make: Make) -> None:
    workspace = make({})
    inbox = LocalInbox(workspace.store)
    notifier = RunNotifier(inbox)
    run = _private_run(memory_owner=ALICE, started_by=BOB)  # a continued run another person started
    notifier.message(run, "a", "Found it", SECRET, "info")
    assert [item.user_id for item in inbox.recent()] == [ALICE] and inbox.for_user(BOB) == []
    with pytest.raises(ValueError, match=r"notify\.private"):
        notifier.message(_private_run(id="run_n", memory_owner=NOBODY), "a", "t", "x", "info")
