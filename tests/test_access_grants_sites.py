"""The access model v2 (N-06) beyond tool calls: workflow starts, MCP servers, connections and cloud models, and the
"who may do what and why" explanation (BER-1). Off by default; while off, nothing changes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
import json
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime import tool_policy
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import AgentConfig, RunIn, WorkflowIn, now
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import (
    AccessRules,
    Person,
    ToolApprovals,
    ToolGate,
    ToolPreGrants,
    audit_events,
)
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.tool_policy import (
    SAFETY_LIMITS,
    AccessContext,
    AccessRequest,
    AccessResource,
    CallContext,
    Deny,
    Grant,
    ToolBinding,
    ToolCapability,
    ToolFacts,
    ToolNow,
    decide,
    effect_for,
    explain,
    site_access,
    who_may,
)
from nodestro_runtime.workspace import Workspace

AT = now()
ALICE = Person("usr_alice")
TOOL = "celsius_to_fahrenheit"
MCP = AccessResource(type="mcp_server", id="mcs_1")
CONNECTION = AccessResource(type="connection", id="conn_1")
CLOUD = AccessResource(type="cloud_provider", id="anthropic")


def cap(access: Any, reach: Any = "internal") -> ToolCapability:
    return ToolCapability(access=access, reach=reach, source="admin")


def grant(resource: AccessResource, **extra: Any) -> Grant:
    values: dict[str, Any] = {
        "id": "agr_1",
        "resource": resource,
        "actions": ("execute",),
        "duration": "until_revoked",
        "created_at": AT - timedelta(days=1),
        "granted_by": "usr_alice",
    }
    return Grant(**{**values, **extra})


def deny(resource: AccessResource, **extra: Any) -> Deny:
    values: dict[str, Any] = {
        "id": "adn_1",
        "resource": resource,
        "actions": ("execute",),
        "created_at": AT - timedelta(days=1),
        "denied_by": "usr_alice",
    }
    return Deny(**{**values, **extra})


def situation(access: AccessContext | None, *, attended: bool = True, **extra: Any) -> CallContext:
    return CallContext(
        run_id="run_1",
        node_id="a",
        trigger="manual" if attended else "schedule",
        attended=attended,
        access=access,
        **extra,
    )


# ---- MCP servers and connections at the tool gate --------------------------------------------------------------------


def test_off_the_related_resources_change_nothing() -> None:
    """Switch off (``access=None``): the MCP server and the connection a call uses are not looked at."""
    for kind, capability, attended in [
        ("mcp", cap("write", "external"), True),
        ("mcp", cap("read", "external"), False),
        ("builtin", cap("write", "external"), True),
        ("builtin", cap("read", "external"), False),
    ]:
        arguments: dict[str, Any] = {"tool": "t", "kind": kind, "capability": capability, "binding": None, "at": AT}
        plain = decide(context=situation(None, attended=attended), **arguments)
        related = decide(context=situation(None, attended=attended), related=(MCP, CONNECTION), **arguments)
        assert plain == related and related.access_source is None


def test_a_deny_on_the_mcp_server_refuses_its_tools() -> None:
    access = AccessContext(denies=(deny(MCP),), agent="wf/a")
    verdict = decide(
        tool="mcp__srv__send",
        kind="mcp",
        capability=cap("read", "external"),
        binding=None,
        context=situation(access),
        at=AT,
        related=(MCP,),
    )
    assert (verdict.action, verdict.code, verdict.access_source) == ("deny", "access.denied", "deny:adn_1")
    assert "adn_1" not in (verdict.reason or "")  # the agent gets no source (N-07)


def test_a_grant_on_the_mcp_server_never_loosens_the_mcp_rules() -> None:
    """MCP stays strict: a grant on the server (or on the tool) neither replaces the question before a changing MCP
    call nor lets an MCP tool run unattended."""
    server_grant = grant(MCP, actions=("execute",))
    tool_grant = grant(
        AccessResource(type="tool", id="mcp__srv__send"),
        id="agr_2",
        tool=ToolFacts(access="write", reach="external"),
    )
    access = AccessContext(grants=(server_grant, tool_grant), agent="wf/a")
    asked = decide(
        tool="mcp__srv__send",
        kind="mcp",
        capability=cap("write", "external"),
        binding=ToolBinding(ask="never", reason="An administrator says it is harmless"),
        context=situation(access),
        at=AT,
        related=(MCP,),
    )
    assert (asked.action, asked.access_source) == ("ask", None)
    unattended = AccessContext(
        grants=(grant(MCP, mode="unattended", duration="until", expires_at=AT + timedelta(days=1), scope="all"),),
        agent="wf/a",
    )
    refused = decide(
        tool="mcp__srv__read",
        kind="mcp",
        capability=cap("read", "external"),
        binding=None,
        context=situation(unattended, attended=False),
        at=AT,
        related=(MCP,),
    )
    assert (refused.action, refused.code) == ("deny", "tool.approval_unattended")


def test_a_safety_limit_wins_over_a_deny_on_a_related_resource() -> None:
    access = AccessContext(denies=(deny(MCP),), agent="wf/a", offline=True)
    verdict = decide(
        tool="mcp__srv__read",
        kind="mcp",
        capability=cap("read", "external"),
        binding=None,
        context=situation(access),
        at=AT,
        related=(MCP,),
    )
    assert (verdict.code, verdict.access_source) == ("access.safety_limit", "limit:offline")


def test_a_deny_on_a_connection_refuses_the_tool_that_uses_it_for_the_named_agent_only() -> None:
    arguments: dict[str, Any] = {
        "tool": "send_mail",
        "kind": "builtin",
        "capability": cap("read", "external"),
        "binding": None,
        "at": AT,
        "related": (CONNECTION,),
    }
    for agent, expected in [("wf/a", "deny"), ("wf/b", "allow")]:
        access = AccessContext(denies=(deny(CONNECTION, scope="agent", agents=("wf/a",)),), agent=agent)
        verdict = decide(context=situation(access), **arguments)
        assert verdict.action == expected
    # A deny for some agents also holds when the agent is unknown (fail closed).
    unknown = decide(
        context=situation(AccessContext(denies=(deny(CONNECTION, scope="agent", agents=("wf/a",)),))), **arguments
    )
    assert unknown.action == "deny"
    # A connection grant does not allow a call S-2 would ask about.
    granted = AccessContext(grants=(grant(CONNECTION),), agent="wf/a")
    asked = decide(context=situation(granted), **{**arguments, "capability": cap("write", "external")})
    assert (asked.action, asked.access_source) == ("ask", None)


def test_the_gate_names_the_related_resources_only_while_the_switch_is_on() -> None:
    class Info:
        kind = "mcp"
        server_id = "mcs_1"

    class Tools:
        def get(self, name: str) -> Info:
            if name == "missing":
                raise KeyError(name)
            return Info()

    class Settings:
        connection_id = "conn_1"

    off = ToolGate(ToolApprovals.__new__(ToolApprovals))
    assert off.related(Tools(), "mcp__x__y", {"mcp__x__y": Settings()}) == ()
    assert off.site_refusal(CLOUD, attended=True, agent=None) is None
    on = ToolGate(ToolApprovals.__new__(ToolApprovals), access=lambda: ((), ()))
    assert on.related(Tools(), "mcp__x__y", {"mcp__x__y": Settings()}) == (MCP, CONNECTION)
    assert on.related(Tools(), "missing", {}) == ()


# ---- workflow starts and cloud models: the pure rules ----------------------------------------------------------------


def test_cloud_model_with_shared_content_needs_a_grant_per_provider_and_agent() -> None:
    """Spec 7.2 row "Cloud-Modell mit freigegebenen Inhalten": the default and its loosening."""
    plain = AccessContext(agent="wf/a")
    found = site_access(CLOUD, plain, attended=True, at=AT, agent="wf/a")
    assert (found.allowed, found.source) == (True, "default:cloud_without_shared_content")
    shared = site_access(CLOUD, plain, attended=True, at=AT, agent="wf/a", shared_content=True)
    assert (shared.allowed, shared.kind) == (False, "missing_grant")
    granted = AccessContext(grants=(grant(CLOUD, actions=("submit",), scope="agent", agents=("wf/a",)),), agent="wf/a")
    assert site_access(CLOUD, granted, attended=True, at=AT, agent="wf/a", shared_content=True).allowed
    other = site_access(CLOUD, granted, attended=True, at=AT, agent="wf/b", shared_content=True)
    assert other.kind == "missing_grant"
    blocked = AccessContext(denies=(deny(CLOUD, actions=("submit",)),), grants=granted.grants)
    assert site_access(CLOUD, blocked, attended=True, at=AT, agent="wf/a").source == "deny:adn_1"
    offline = AccessContext(grants=granted.grants, offline=True)
    assert site_access(CLOUD, offline, attended=True, at=AT, agent="wf/a").source == "limit:offline"


def test_a_top_level_start_counts_only_records_for_all_agents() -> None:
    workflow = AccessResource(type="workflow", id="wf_1")
    scoped = AccessContext(denies=(deny(workflow, scope="agent", agents=("wf_0/a",)),))
    # Without a record that counts, a start is allowed (a missing grant never refuses it, M-2 a).
    plain = site_access(workflow, scoped, attended=True, at=AT, agent=None)
    assert (plain.allowed, plain.outcome, plain.kind, plain.source) == (True, "allowed", None, "none")
    assert site_access(workflow, scoped, attended=True, at=AT, agent="wf_0/a").kind == "denied"
    everyone = AccessContext(denies=(deny(workflow, mode="unattended"),))
    assert site_access(workflow, everyone, attended=False, at=AT, agent=None).kind == "denied"
    assert site_access(workflow, everyone, attended=True, at=AT, agent=None).allowed


def test_effect_classes_of_the_call_sites() -> None:
    assert effect_for("workflow", "execute") == "read_internal"
    assert effect_for("cloud_provider", "submit") == "read_external"
    assert effect_for("folder", "read") == "read_internal"
    assert effect_for("folder", "move") == "write_internal"
    assert effect_for("folder", "delete") == "destructive"
    assert effect_for("tool", "execute") == "write_external"  # unknown tool
    assert effect_for("tool", "execute", tool=ToolFacts(access="read", reach="internal")) == "read_internal"
    assert effect_for("mcp_server", "execute", tool=ToolFacts(access="read", reach="internal")) == "read_external"
    assert effect_for("connection", "execute", tool=ToolFacts(access="destructive", reach="external")) == "destructive"


# ---- "who may do what and why" (BER-1) -------------------------------------------------------------------------------


def test_explain_names_the_deciding_record_and_the_ones_behind_it() -> None:
    folder = AccessResource(type="folder", id="/home/a/docs")
    parent = deny(AccessResource(type="folder", id="/home/a"), actions=("read",))
    sub = grant(folder, actions=("read",))
    request = AccessRequest(
        resource=folder, action="read", effect="read_internal", agent="wf/a", protected_folders=("/srv/nodestro",)
    )
    found = explain(request, grants=[sub], denies=[parent], at=AT)
    assert (found.allowed, found.kind, found.code, found.source) == (False, "denied", "access.denied", "deny:adn_1")
    assert found.deny == parent and found.grant is None and found.also == ("grant:agr_1",)
    allowed = explain(request, grants=[sub], denies=[], at=AT)
    assert (allowed.allowed, allowed.grant, allowed.also) == (True, sub, ())
    limited = explain(
        request.model_copy(update={"resource": AccessResource(type="folder", id="/etc/ssh")}),
        grants=[grant(AccessResource(type="folder", id="/etc"), actions=("read",))],
        denies=[],
        at=AT,
    )
    assert (limited.kind, limited.limit, limited.limit_text) == (
        "safety_limit",
        "protected_folder",
        SAFETY_LIMITS["protected_folder"],
    )
    assert limited.also == ("grant:agr_1",)  # the grant is there, the limit wins
    web = AccessRequest(resource=AccessResource(type="tool", id="search_web"), action="execute", effect="read_external")
    assert explain(web, grants=[], denies=[], at=AT).default_rule == "read_external_without_shared_content"


def test_explain_counts_the_records_as_the_call_site_does() -> None:
    """A tool call ignores ``once`` grants; a top-level workflow start ignores records for some agents."""
    facts = ToolFacts(access="write", reach="internal")
    tool = AccessResource(type="tool", id="tool_x")
    once = grant(tool, duration="once", tool=facts)
    request = AccessRequest(resource=tool, action="execute", effect="write_internal", tool=facts)
    ignored = explain(request, grants=[once], denies=[], at=AT)
    assert (ignored.allowed, ignored.outcome, ignored.grant) == (True, "per_call_rules", None)
    workflow = AccessResource(type="workflow", id="wf_1")
    start = AccessRequest(resource=workflow, action="execute", effect="read_internal")
    scoped = deny(workflow, scope="agent", agents=("wf_0/a",))
    assert explain(start, grants=[], denies=[scoped], at=AT).outcome == "allowed"
    assert explain(start.model_copy(update={"agent": "wf_0/a"}), grants=[], denies=[scoped], at=AT).kind == "denied"


def test_who_may_lists_every_active_record_with_its_reason() -> None:
    tool = AccessResource(type="tool", id="tool_x")
    facts = ToolFacts(access="write", reach="external", version=2)
    records_grants = [
        grant(tool, tool=facts, scope="agents", agents=("wf/a", "wf/b")),
        grant(CLOUD, id="agr_2", actions=("submit",), revoked_at=AT - timedelta(hours=1), status="revoked"),
        grant(AccessResource(type="folder", id="/home/a"), id="agr_3", actions=("read", "write")),
    ]
    records_denies = [deny(tool, scope="agent", agents=("wf/b",), mode="unattended")]
    found = who_may(records_grants, records_denies, at=AT)
    shown = {(item.resource.id, item.action, item.agent, item.mode): (item.allowed, item.source) for item in found}
    assert shown == {
        ("tool_x", "execute", "wf/a", "supervised"): (True, "grant:agr_1"),
        ("tool_x", "execute", "wf/b", "supervised"): (True, "grant:agr_1"),
        ("tool_x", "execute", "wf/b", "unattended"): (False, "deny:adn_1"),
    }  # the revoked grant is gone, and folders need the protected data folder
    with_folders = who_may(records_grants, records_denies, at=AT, protected_folders=("/srv/nodestro",))
    assert {(item.resource.type, item.action) for item in with_folders} >= {("folder", "read"), ("folder", "write")}
    narrowed = who_may(records_grants, records_denies, at=AT, agent="wf/b", mode="unattended")
    assert [(item.agent, item.source) for item in narrowed] == [("wf/b", "deny:adn_1")]
    only = who_may(records_grants, records_denies, at=AT, resource=CLOUD)
    assert only == []
    # The explanation carries no secrets: records name people by id, never a model.
    assert json.loads(found[0].model_dump_json())["grant"]["granted_by"] == "usr_alice"


# ---- in a workspace --------------------------------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    fake = ScriptedClient({"AGENT": [tool_use(TOOL, {"celsius": 100}), text("done")] * 6, "CHILD": [text("ok")] * 6})
    opened = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
    result, _ = opened.tools.save_custom(
        CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, TOOL, trust=opened.tools.editor_trust(TOOL, CUSTOM_TOOL_TEMPLATE)
    )
    assert result.ok
    opened.runs.tool_gate = ToolGate(
        ToolApprovals(opened.store, poll_seconds=0.02),
        pre_grants=ToolPreGrants(opened.store).of_scope,
        access=AccessRules(opened.store).current,
        protected_folders=(str(opened.data_dir),),
    )
    opened.fake = fake  # type: ignore[attr-defined]
    yield opened
    opened.close()


def _plain(workspace: Workspace, name: str = "Plain", prompt: str = "CHILD") -> Any:
    return workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": name,
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "a", "type": "agent", "agent": {"system_prompt": prompt}},
                ],
                "edges": [{"id": "e", "source": "start", "target": "a"}],
            }
        )
    )


def _sent(workspace: Workspace) -> str:
    return json.dumps(workspace.fake.messages.calls, default=str)  # type: ignore[attr-defined]


async def test_a_deny_on_a_workflow_refuses_its_start_and_records_the_source(workspace: Workspace) -> None:
    flow = _plain(workspace)
    rules = AccessRules(workspace.store)
    resource = AccessResource(type="workflow", id=flow.id)
    rules.deny(resource=resource, actions=["execute"], scope="agent", agents=["other/a"], by=ALICE)
    started = await workspace.run(flow, RunIn(input="x"), started_by="usr_bob")  # a deny for some agents: no effect
    assert (await workspace.wait(started.id, timeout=20)).status == "succeeded"
    blocked = rules.deny(resource=resource, actions=["execute"], by=ALICE)
    with pytest.raises(NodestroError) as refused:
        await workspace.run(flow, RunIn(input="x"), started_by="usr_bob")
    assert refused.value.code == "access.denied"
    assert refused.value.params == {"resource_type": "workflow", "resource_id": flow.id, "source": f"deny:{blocked.id}"}
    # A caller that is not a person (an API key, a trigger) gets no source (L-3); the audit keeps it.
    with pytest.raises(NodestroError) as keyed:
        workspace.runs.start(flow, RunIn(input="x"), trigger="api", api_key_id="key_1")
    assert keyed.value.code == "access.denied" and "source" not in keyed.value.params
    events = [item for item in audit_events(workspace.store) if item.kind == "access_refused"]
    assert sorted(item.actor for item in events) == ["api:key_1", "usr_bob"]
    assert {(item.outcome, item.details["source"], item.details["resource_id"]) for item in events} == {
        ("access.denied", f"deny:{blocked.id}", flow.id)
    }
    rules.revoke_deny(blocked.id, by=ALICE)
    again = await workspace.run(flow, RunIn(input="x"))  # the revocation applies at the next start
    assert (await workspace.wait(again.id, timeout=20)).status == "succeeded"


async def test_a_deny_for_unattended_runs_refuses_a_schedule_but_not_a_person(workspace: Workspace) -> None:
    from nodestro_runtime.launcher import launch

    flow = _plain(workspace)
    AccessRules(workspace.store).deny(
        resource=AccessResource(type="workflow", id=flow.id), actions=["execute"], mode="unattended", by=ALICE
    )
    with pytest.raises(NodestroError) as refused:
        await launch(workspace, flow, RunIn(input="x"), trigger="schedule", trigger_ref="sch_1")
    assert refused.value.code == "access.denied"
    started = await workspace.run(flow, RunIn(input="x"))
    assert (await workspace.wait(started.id, timeout=20)).status == "succeeded"


async def test_a_sub_workflow_start_is_refused_for_the_calling_agent(workspace: Workspace) -> None:
    child = _plain(workspace, "Child")
    parent = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Parent",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "sub", "type": "subworkflow", "subworkflow": {"workflow_id": child.id}},
                ],
                "edges": [{"id": "e", "source": "start", "target": "sub"}],
            }
        )
    )
    blocked = AccessRules(workspace.store).deny(
        resource=AccessResource(type="workflow", id=child.id),
        actions=["execute"],
        scope="agent",
        agents=[tool_policy.agent_ref(parent.id, "sub")],
        by=ALICE,
    )
    seen: list[Any] = []
    workspace.runs.event_sink = seen.append
    started = await workspace.run(parent, RunIn(input="x"), started_by="usr_bob")
    done = await workspace.wait(started.id, timeout=20)
    node = done.nodes["sub"]
    assert (node.status, node.error_code, node.child_run_id) == ("failed", "access.denied", None)
    assert node.error_params["source"] == f"deny:{blocked.id}"
    events = [event for event in seen if event.type == "access_refused"]
    assert [event.data["source"] for event in events] == [f"deny:{blocked.id}"]
    (audited,) = [item for item in audit_events(workspace.store) if item.kind == "access_refused"]
    assert (audited.run_id, audited.details["agent"]) == (started.id, f"{parent.id}/sub")
    assert "CHILD" not in _sent(workspace)  # the child never ran


async def test_a_blocked_cloud_model_never_gets_the_agents_content(workspace: Workspace) -> None:
    flow = _plain(workspace, prompt="CHILD")
    rules = AccessRules(workspace.store)
    blocked = rules.deny(resource=CLOUD, actions=["submit"], scope="agent", agents=[f"{flow.id}/a"], by=ALICE)
    started = await workspace.run(flow, RunIn(input="SECRET-CONTENT"), started_by="usr_bob")
    done = await workspace.wait(started.id, timeout=20)
    node = done.nodes["a"]
    assert (node.status, node.error_code) == ("failed", "access.denied")
    assert node.error_params == {
        "resource_type": "cloud_provider",
        "resource_id": "anthropic",
        "source": f"deny:{blocked.id}",
    }
    assert "SECRET-CONTENT" not in _sent(workspace)
    rules.revoke_deny(blocked.id, by=ALICE)
    again = await workspace.run(flow, RunIn(input="SECRET-CONTENT"))
    assert (await workspace.wait(again.id, timeout=20)).status == "succeeded"
    assert "SECRET-CONTENT" in _sent(workspace)


async def test_strict_offline_is_a_safety_limit_for_cloud_models(workspace: Workspace) -> None:
    flow = _plain(workspace)
    workspace.runs.tool_gate.offline = lambda: True  # type: ignore[union-attr]
    AccessRules(workspace.store).grant(resource=CLOUD, actions=["submit"], duration="until_revoked", by=ALICE)
    started = await workspace.run(flow, RunIn(input="x"), started_by="usr_bob")
    node = (await workspace.wait(started.id, timeout=20)).nodes["a"]
    assert (node.error_code, node.error_params["source"]) == ("access.safety_limit", "limit:offline")


def test_an_agent_test_and_the_run_chat_respect_a_cloud_deny(workspace: Workspace) -> None:
    from nodestro_runtime.agent_test import AgentTestIn

    blocked = AccessRules(workspace.store).deny(resource=CLOUD, actions=["submit"], by=ALICE)
    result, _tracker = workspace.tester.run(
        AgentTestIn(agent=AgentConfig(name="A", system_prompt="CHILD"), input="x"), actor="usr_bob"
    )
    assert (result.status, result.error_code, result.error_params["source"]) == (
        "failed",
        "access.denied",
        f"deny:{blocked.id}",
    )
    with pytest.raises(NodestroError) as refused:
        workspace.runs.check_cloud_access("anthropic", run_id="run_1", actor="usr_bob")
    assert refused.value.code == "access.denied"
    workspace.runs.check_cloud_access("ollama", run_id="run_1", actor="usr_bob")  # the local model server: no check
    # M-1: every other model use outside a run (assistants, guide, judges, model choice) connects through
    # connect_model, which checks the same way; a caller that is not a person gets no source.
    with pytest.raises(NodestroError) as assistant:
        workspace.connect_model("anthropic:claude-sonnet-5-5", actor="usr_bob")
    assert (assistant.value.code, assistant.value.params["source"]) == ("access.denied", f"deny:{blocked.id}")
    with pytest.raises(NodestroError) as keyed:
        workspace.connect_model("anthropic:claude-sonnet-5-5")
    assert "source" not in keyed.value.params
    assert _sent(workspace) == "[]"
    # L-4: the agent test's refusal is audited like the others.
    audited = [item for item in audit_events(workspace.store) if item.kind == "access_refused"]
    assert sorted(item.actor for item in audited) == ["system", "usr_bob", "usr_bob", "usr_bob"]
    assert {item.details["source"] for item in audited} == {f"deny:{blocked.id}"}


async def test_a_deny_on_a_connection_refuses_the_tool_that_uses_it_in_a_run(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connection the agent's tool settings name is evaluated with the call; the tool never runs."""
    from nodestro_runtime.connections import ConnectionIn

    monkeypatch.setenv("NODESTRO_VAULT_KEY", "a" * 44)
    made = workspace.connections.create(ConnectionIn(type="search", name="Search", provider="ddgs"))
    workspace.fake.messages.script["SEARCHER"] = [tool_use("search_web", {"query": "q"}), text("done")]  # type: ignore[attr-defined]
    flow = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Connected",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {
                        "id": "a",
                        "type": "agent",
                        "agent": {
                            "system_prompt": "SEARCHER",
                            "tools": ["search_web"],
                            "tool_policies": {"search_web": {"ask": "never"}},
                            "tool_settings": {"search_web": {"connection_id": made.id}},
                        },
                    },
                ],
                "edges": [{"id": "e", "source": "start", "target": "a"}],
            }
        )
    )
    blocked = AccessRules(workspace.store).deny(
        resource=AccessResource(type="connection", id=made.id), actions=["execute"], by=ALICE
    )
    started = await workspace.run(flow, RunIn(input="x"))
    call = (await workspace.wait(started.id, timeout=20)).nodes["a"].tool_calls[0]
    assert call.approval is not None
    assert (call.approval.status, call.approval.code, call.approval.access_source) == (
        "denied",
        "access.denied",
        f"deny:{blocked.id}",
    )
    assert blocked.id not in _sent(workspace)


@pytest.mark.parametrize("approvals", [False, True])
async def test_switch_off_nothing_is_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approvals: bool) -> None:
    """Records stored while the switch is off change nothing: no start, connection or model request is refused,
    with or without the per-call approvals."""
    from nodestro_runtime.tool_approvals import ACCESS_ENV, ENABLE_ENV

    monkeypatch.delenv(ACCESS_ENV, raising=False)
    if approvals:
        monkeypatch.setenv(ENABLE_ENV, "1")
    else:
        monkeypatch.delenv(ENABLE_ENV, raising=False)
    fake = ScriptedClient({"CHILD": [text("ok")] * 4})
    opened = Workspace(tmp_path / "off", client_factory=lambda _provider: fake, integrations=[])
    try:
        gate = opened.runs.tool_gate
        assert (gate is not None) == approvals and (gate is None or gate.access is None)
        flow = _plain(opened)
        rules = AccessRules(opened.store)
        rules.deny(resource=AccessResource(type="workflow", id=flow.id), actions=["execute"], by=ALICE)
        rules.deny(resource=CLOUD, actions=["submit"], by=ALICE)
        started = await opened.run(flow, RunIn(input="x"))
        assert (await opened.wait(started.id, timeout=20)).status == "succeeded"
        opened.runs.check_cloud_access("anthropic", run_id=started.id, actor="usr_bob")
        assert [item for item in audit_events(opened.store) if item.kind == "access_refused"] == []
    finally:
        opened.close()


def test_an_approvals_only_gate_keeps_the_plain_connection(workspace: Workspace) -> None:
    workspace.runs.tool_gate = ToolGate(ToolApprovals(workspace.store))
    assert workspace.runs._checked_connect(None, "a") == workspace.runs.connect  # type: ignore[arg-type]


async def test_a_team_start_is_refused_and_the_orchestrator_learns_no_source(tmp_path: Path) -> None:
    from nodestro_runtime.testing import parsed

    ids: dict[str, str] = {}
    boss = "You coordinate teams"
    fake = ScriptedClient(
        {
            boss: [
                parsed(lambda model: model(action="delegate", member=ids["t"], task="Draft", reason="drafts")),
                parsed(lambda model: model(action="finish", answer="No team", reason="blocked")),
            ],
            "TEAM": [text("draft")],
        }
    )
    opened = Workspace(tmp_path / "teams", client_factory=lambda _provider: fake, integrations=[])
    try:
        opened.runs.tool_gate = ToolGate(ToolApprovals(opened.store), access=AccessRules(opened.store).current)
        member = opened.save_workflow(
            WorkflowIn.model_validate(
                {
                    "name": "Team",
                    "description": "Team team",
                    "nodes": [
                        {"id": "s", "type": "start"},
                        {"id": "w", "type": "agent", "agent": {"name": "Team", "system_prompt": "TEAM"}},
                    ],
                    "edges": [{"id": "e", "source": "s", "target": "w"}],
                }
            )
        )
        ids["t"] = member.id
        lead = opened.save_workflow(
            WorkflowIn.model_validate(
                {
                    "name": "Boss",
                    "nodes": [
                        {"id": "start", "type": "start"},
                        {
                            "id": "lead",
                            "type": "orchestrator",
                            "orchestrator": {"members": [{"workflow_id": member.id}]},
                        },
                    ],
                    "edges": [{"id": "e", "source": "start", "target": "lead"}],
                }
            )
        )
        blocked = AccessRules(opened.store).deny(
            resource=AccessResource(type="workflow", id=member.id),
            actions=["execute"],
            scope="agent",
            agents=[tool_policy.agent_ref(lead.id, "lead")],
            by=ALICE,
        )
        started = await opened.run(lead, RunIn(input="x"))
        done = await opened.wait(started.id, timeout=20)
        assert done.status == "succeeded", done.error
        assert done.nodes["lead"].child_run_ids == []  # the team never started
        assert "TEAM" not in json.dumps([call.get("system") for call in fake.messages.calls], default=str)
        assert blocked.id not in json.dumps(fake.messages.calls, default=str)  # the orchestrator gets no source
        (audited,) = [item for item in audit_events(opened.store) if item.kind == "access_refused"]
        assert audited.details["source"] == f"deny:{blocked.id}"
    finally:
        opened.close()


def test_strict_offline_does_not_refuse_a_local_stdio_mcp_server() -> None:
    """L-1: a local (stdio) MCP server runs on this computer; a remote one is refused by strict offline."""
    access = AccessContext(agent="wf/a", offline=True, local_servers=frozenset({"mcs_1"}))
    arguments: dict[str, Any] = {
        "tool": "mcp__srv__read",
        "kind": "mcp",
        "capability": cap("read", "external"),
        "binding": None,
        "context": situation(access),
        "at": AT,
    }
    assert decide(related=(MCP,), **arguments).action == "allow"
    remote = decide(related=(AccessResource(type="mcp_server", id="mcs_2"),), **arguments)
    assert (remote.code, remote.access_source) == ("access.safety_limit", "limit:offline")
    blocked = AccessContext(denies=(deny(MCP),), agent="wf/a", offline=True, local_servers=frozenset({"mcs_1"}))
    assert decide(related=(MCP,), **{**arguments, "context": situation(blocked)}).code == "access.denied"


def test_the_gate_names_local_mcp_servers() -> None:
    gate = ToolGate(ToolApprovals.__new__(ToolApprovals), access=lambda: ((), ()), local_mcp=lambda: ["mcs_1"])
    found = gate.call_context(run_id="r", node_id="n", trigger="manual", trigger_ref=None)
    assert found.access is not None and found.access.local_servers == frozenset({"mcs_1"})


def test_who_may_takes_the_tool_as_it_is_now() -> None:
    """M-2 c/d: a grant for an older version shows as stale, and an MCP tool's grant is noted but never counts."""
    tool = AccessResource(type="tool", id="t")
    old = grant(tool, tool=ToolFacts(access="write", reach="internal", version=1))
    (found,) = who_may(
        [old], [], at=AT, tools={"t": ToolNow(facts=ToolFacts(access="write", reach="internal", version=2))}
    )
    assert (found.allowed, found.outcome, found.grant, found.stale) == (True, "per_call_rules", None, ("agr_1",))
    mcp = AccessResource(type="tool", id="mcp__s__t")
    facts = ToolFacts(access="write", reach="external")
    (noted,) = who_may([grant(mcp, tool=facts)], [], at=AT, tools={"mcp__s__t": ToolNow(facts=facts, kind="mcp")})
    assert (noted.outcome, noted.grant is not None, noted.grant_enforced) == ("per_call_rules", True, False)
    (server,) = who_may([grant(MCP)], [], at=AT)
    assert (server.allowed, server.outcome, server.grant_enforced) == (True, "per_call_rules", False)
    # L-B: a tool row counts the deny on its MCP server, as the gate does.
    blocked = deny(MCP, id="adn_m")
    rows = who_may(
        [grant(mcp, tool=facts)],
        [blocked],
        at=AT,
        tools={"mcp__s__t": ToolNow(facts=facts, kind="mcp", related=(MCP,))},
        resource=mcp,
    )
    assert [(row.allowed, row.code, row.source, row.deny) for row in rows] == [
        (False, "access.denied", "deny:adn_m", blocked)
    ]
    # L-C: strict offline refuses a remote MCP server, not a local one, nor the tools of a local one.
    (remote,) = who_may([grant(MCP)], [], at=AT, offline=True)
    assert (remote.allowed, remote.source) == (False, "limit:offline")
    (local,) = who_may([grant(MCP)], [], at=AT, offline=True, local_servers={"mcs_1"})
    assert local.allowed
    tools = {"mcp__s__t": ToolNow(facts=facts, kind="mcp", related=(MCP,))}
    (tool_row,) = who_may([grant(mcp, tool=facts)], [], at=AT, offline=True, tools=tools, local_servers={"mcs_1"})
    assert (tool_row.allowed, tool_row.outcome) == (True, "per_call_rules")
    (web,) = [
        explain(
            AccessRequest(
                resource=AccessResource(type="tool", id="search_web"), action="execute", effect="read_external"
            ),
            grants=[],
            denies=[],
            at=AT,
        )
    ]
    assert (web.outcome, web.default_rule) == ("per_call_rules", "read_external_without_shared_content")


# ---- explain agrees with every call site (M-2) -----------------------------------------------------------------------


def _tool_grant(facts: ToolFacts, **extra: Any) -> Grant:
    return grant(AccessResource(type="tool", id="t"), id="agr_t", tool=facts, **extra)


def test_explain_matches_the_outcome_at_every_call_site() -> None:
    """Property: for every combination of records, mode, offline, agent, tool, related MCP server and connection and
    local servers, ``explain(...).allowed`` is ``False`` exactly when the call site refuses (with the same source),
    and ``outcome`` ``granted`` exactly when a grant replaces the question (tool calls) or allows the access (cloud
    models)."""
    import itertools as it

    agents = (None, "wf/a", "wf/b")
    now_facts = ToolFacts(access="write", reach="internal", version=2)
    tool_records: list[tuple[tuple[Grant, ...], tuple[Deny, ...]]] = [
        ((), ()),
        ((_tool_grant(now_facts),), ()),
        ((_tool_grant(ToolFacts(access="write", reach="internal", version=1)),), ()),  # stale
        ((_tool_grant(now_facts, scope="agent", agents=("wf/a",)),), ()),
        ((_tool_grant(now_facts, duration="once"),), ()),
        (
            (
                _tool_grant(
                    now_facts,
                    mode="unattended",
                    duration="until",
                    expires_at=AT + timedelta(days=1),
                    scope="agent",
                    agents=("wf/a",),
                ),
            ),
            (),
        ),
        ((_tool_grant(now_facts),), (deny(AccessResource(type="tool", id="t")),)),
        ((), (deny(AccessResource(type="tool", id="t"), scope="agent", agents=("wf/b",)),)),
        ((), (deny(AccessResource(type="tool", id="t"), mode="unattended"),)),
    ]
    related_records: list[tuple[tuple[Grant, ...], tuple[Deny, ...]]] = [
        ((), ()),
        ((), (deny(MCP, id="adn_m"),)),
        ((), (deny(CONNECTION, id="adn_c", scope="agent", agents=("wf/a",)),)),
        ((grant(MCP, id="agr_m"), grant(CONNECTION, id="agr_c")), ()),
    ]
    checked = 0
    seen: set[tuple[str, str]] = set()
    for (tool_grants, tool_denies), (
        more_grants,
        more_denies,
    ), related, local, agent, attended, offline, kind in it.product(
        tool_records,
        related_records,
        [(), (MCP,), (MCP, CONNECTION)],
        [frozenset(), frozenset({"mcs_1"})],
        agents,
        [True, False],
        [False, True],
        ["custom", "mcp"],
    ):
        grants, denies = tool_grants + more_grants, tool_denies + more_denies
        capability = ToolCapability(access="write", reach="external" if kind == "mcp" else "internal", source="admin")
        facts = ToolFacts.of(capability, version=2 if kind == "custom" else None)
        if kind == "mcp":
            grants = tuple(
                item.model_copy(update={"tool": facts if item.tool == now_facts else item.tool}) for item in grants
            )
        access = AccessContext(grants=grants, denies=denies, agent=agent, offline=offline, local_servers=local)
        verdict = decide(
            tool="t",
            kind=kind,
            capability=capability,
            binding=None,
            context=situation(access, attended=attended),
            at=AT,
            version=facts.version,
            related=related,
        )
        request = AccessRequest(
            resource=AccessResource(type="tool", id="t"),
            action="execute",
            effect=effect_for("tool", "execute", tool=facts),
            agent=agent,
            mode="supervised" if attended else "unattended",
            offline=offline,
            tool=facts,
        )
        found = explain(
            request, grants=grants, denies=denies, at=AT, tool_kind=kind, related=related, local_servers=local
        )
        refused = verdict.action == "deny" and (verdict.code or "").startswith("access.")
        assert found.allowed is not refused, (found, verdict)
        if refused:
            assert (found.code, found.source) == (verdict.code, verdict.access_source), (found, verdict)
        assert (found.outcome == "granted") is (verdict.access_source or "").startswith("grant:"), (found, verdict)
        seen.add((kind, found.outcome))
        checked += 1
    # The grid reaches every outcome of a tool call; an MCP tool is never granted.
    assert seen == {
        ("custom", "refused"),
        ("custom", "granted"),
        ("custom", "per_call_rules"),
        ("mcp", "refused"),
        ("mcp", "per_call_rules"),
    }
    # MCP servers and connections: only their refusals count.
    for resource, (grants, denies), agent, attended, offline, local in it.product(
        [MCP, CONNECTION],
        [((), ()), ((grant(MCP), grant(CONNECTION, id="agr_2")), ()), ((), (deny(MCP), deny(CONNECTION, id="adn_2")))],
        agents,
        [True, False],
        [False, True],
        [frozenset(), frozenset({"mcs_1"})],
    ):
        capability = cap("read", "internal")
        access = AccessContext(grants=grants, denies=denies, agent=agent, offline=offline, local_servers=local)
        verdict = decide(
            tool="t",
            kind="builtin",
            capability=capability,
            binding=None,
            context=situation(access, attended=attended),
            at=AT,
            related=(resource,),
        )
        request = AccessRequest(
            resource=resource,
            action="execute",
            effect=effect_for(resource.type, "execute", tool=ToolFacts.of(capability, version=None)),
            agent=agent,
            mode="supervised" if attended else "unattended",
            offline=offline,
        )
        found = explain(request, grants=grants, denies=denies, at=AT, local_servers=local)
        refused = verdict.action == "deny" and (verdict.code or "").startswith("access.")
        assert found.allowed is not refused and found.outcome != "granted", (found, verdict)
        seen.add((resource.type, found.outcome))
        checked += 1
    # Workflow starts and cloud models: the gate's own check.
    workflow = AccessResource(type="workflow", id="wf_1")
    for resource, (grants, denies), agent, attended, offline in it.product(
        [workflow, CLOUD],
        [
            ((), ()),
            ((grant(workflow), grant(CLOUD, id="agr_2", actions=("submit",))), ()),
            ((), (deny(workflow), deny(CLOUD, id="adn_2", actions=("submit",)))),
            (
                (),
                (
                    deny(workflow, scope="agent", agents=("wf/a",)),
                    deny(CLOUD, id="adn_2", actions=("submit",), scope="agent", agents=("wf/a",)),
                ),
            ),
        ],
        agents,
        [True, False],
        [False, True],
    ):
        gate = ToolGate(
            ToolApprovals.__new__(ToolApprovals), access=lambda g=grants, d=denies: (g, d), offline=lambda o=offline: o
        )
        refusal = gate.site_refusal(resource, attended=attended, agent=agent)
        found = site_access(
            resource,
            AccessContext(grants=grants, denies=denies, offline=offline),
            attended=attended,
            at=AT,
            agent=agent,
        )
        assert found.allowed is (refusal is None), (resource, found, refusal)
        seen.add((resource.type, found.outcome))
        checked += 1
    assert {("workflow", "allowed"), ("workflow", "refused"), ("cloud_provider", "allowed")} <= seen
    assert {("cloud_provider", "granted"), ("cloud_provider", "refused")} <= seen
    assert {("mcp_server", "refused"), ("mcp_server", "per_call_rules"), ("connection", "refused")} <= seen
    assert checked == 9 * 4 * 3 * 2 * 3 * 2 * 2 * 2 + 2 * 3 * 3 * 2 * 2 * 2 + 2 * 4 * 3 * 2 * 2


def test_connect_model_probes_without_audit_and_knows_unattended_callers(workspace: Workspace) -> None:
    """L-A: a probe (model choice) is refused but not audited; a judge without a person meets unattended denies."""
    AccessRules(workspace.store).deny(resource=CLOUD, actions=["submit"], mode="unattended", by=ALICE)
    model = "anthropic:claude-sonnet-5-5"
    workspace.connect_model(model, actor="usr_bob")  # attended: the deny for unattended use does not apply
    with pytest.raises(NodestroError) as judged:
        workspace.connect_model(model, actor="usr_bob", attended=False)
    assert judged.value.code == "access.denied"
    with pytest.raises(NodestroError):
        workspace.connect_model(model, actor="usr_bob", attended=False, probe=True)
    audited = [item for item in audit_events(workspace.store) if item.kind == "access_refused"]
    assert len(audited) == 1  # the probe left no audit event
