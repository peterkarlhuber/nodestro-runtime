"""Built-in tool specs, binding settings, time and call limits, checks, integration contract v2 (NAL-A1, ADR-0001)."""

from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Any

from orkexa import Tool, ToolResultError
import pytest

from nodestro_runtime.agent_test import AgentTestIn
from nodestro_runtime.builtin_tools import BUILTIN_TOOLS
from nodestro_runtime.connections import ConnectionIn, ResolvedConnection
from nodestro_runtime.extensions import CONTRACT, SUPPORTED_CONTRACTS, ExtensionError, Extensions, Integration
from nodestro_runtime.launcher import external_tools
from nodestro_runtime.preflight import preflight
from nodestro_runtime.release import ReleaseBundle
from nodestro_runtime.schemas import AgentConfig, SettingsIn, ToolSettings, Workflow
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_library import RESERVED_NAMES
from nodestro_runtime.tool_policy import BUILTIN_CAPABILITIES, UNKNOWN, ToolCapability
from nodestro_runtime.tool_spec import (
    BUILTIN_SPECS,
    PLANNED_BUILTIN_NAMES,
    CallCounter,
    SpecTool,
    ToolSpec,
    bind_tool,
    current_tool_context,
    settings_problems,
)
from nodestro_runtime.workspace import Workspace

EXTERNAL = ToolCapability(access="read", reach="external", source="nodestro")


@Tool
def slow_lookup(query: str) -> str:
    """Look something up slowly.

    Args:
        query: What to look up.

    Returns:
        The answer.
    """
    time.sleep(float(query))
    return "late"


@Tool
def whoami(query: str) -> str:
    """Report the binding the tool sees.

    Args:
        query: Anything.

    Returns:
        The binding.
    """
    context = current_tool_context()
    assert context is not None
    connection = context.connection
    return f"{context.spec.id}|{dict(context.params)}|{connection.provider if connection else None}"


def spec(tool_id: str, **changes: Any) -> ToolSpec:
    data: dict[str, Any] = {"id": tool_id, "category": "web", "capability": EXTERNAL, "network": "external"}
    data["offline"] = False
    data.update(changes)
    return ToolSpec(**data)


def integration(*pairs: tuple[ToolSpec, Tool] | Tool, contract: int = CONTRACT) -> Integration:
    return Integration(name="acme", version="1", tools=pairs, contract=contract)


def workflow(agent: AgentConfig) -> Workflow:
    return Workflow.model_validate(
        {
            "id": "w",
            "name": "Spec flow",
            "nodes": [
                {"id": "s", "type": "start"},
                {"id": "a", "type": "agent", "agent": agent.model_dump()},
                {"id": "o", "type": "output"},
            ],
            "edges": [{"id": "e1", "source": "s", "target": "a"}, {"id": "e2", "source": "a", "target": "o"}],
        }
    )


# ---- the specs of the existing tools (acceptance 1) ---------------------------------------------------------------


def test_the_five_tools_keep_capability_and_schema() -> None:
    """Snapshot: specs add metadata only; what the model sees and what the approval rules read is unchanged."""
    assert set(BUILTIN_SPECS) == {tool.name for tool in BUILTIN_TOOLS} == set(BUILTIN_CAPABILITIES)
    for tool in BUILTIN_TOOLS:
        assert tool.name is not None
        item = BUILTIN_SPECS[tool.name]
        assert item.capability == BUILTIN_CAPABILITIES[tool.name]
        assert (item.network, item.offline, item.connection_type, item.version) == ("none", True, None, 1)
        wrapped = bind_tool(tool, item, None, resolve=None, counter=CallCounter())
        assert wrapped.schema() == tool.schema() and wrapped.definition() == tool.definition()
        assert item.ref == f"{tool.name}@1" and item.i18n_key == f"tools.builtin.{tool.name}"


def test_a_spec_that_needs_the_network_is_never_offline_or_internal() -> None:
    with pytest.raises(ValueError):
        spec("search_x", offline=True)
    with pytest.raises(ValueError):
        spec("search_x", capability=ToolCapability(access="read", reach="internal", source="nodestro"))
    with pytest.raises(ValueError):
        spec("Bad-Name")


def test_planned_names_are_reserved_and_not_the_server_tool_keys() -> None:
    assert PLANNED_BUILTIN_NAMES <= RESERVED_NAMES
    assert not PLANNED_BUILTIN_NAMES & {"web_search", "web_fetch", "code_execution"}


# ---- binding: context, time limit, call limit ---------------------------------------------------------------------


def test_the_tool_sees_its_parameters_and_connection_but_the_model_does_not() -> None:
    item = spec(
        "whoami", connection_type="search", params_schema={"type": "object", "properties": {"n": {"type": "integer"}}}
    )
    resolved = ResolvedConnection(
        id="conn_1", type="search", provider="test", name="S", settings={}, egress=(), secret="k"
    )
    wrapped = bind_tool(
        whoami,
        item,
        ToolSettings(connection_id="conn_1", params={"n": 3}),
        resolve=lambda _id, _type: resolved,
        counter=CallCounter(),
    )
    assert wrapped.execute({"query": "x"}) == "whoami|{'n': 3}|test"
    assert "conn_1" not in str(wrapped.definition()) and "'n'" not in str(wrapped.definition())
    assert current_tool_context() is None


def test_a_tool_without_its_connection_gets_a_tool_error() -> None:
    wrapped = bind_tool(whoami, spec("whoami", connection_type="search"), None, resolve=None, counter=CallCounter())
    with pytest.raises(ToolResultError, match=r"check\.connection_missing"):
        wrapped.execute({"query": "x"})


def test_a_call_over_the_time_limit_ends_with_tool_timeout() -> None:
    """Acceptance 4 (unit): the call returns after its limit, not after the function."""
    wrapped = SpecTool(slow_lookup, spec("slow_lookup", timeout_seconds=0.2))
    started = time.monotonic()
    with pytest.raises(ToolResultError, match=r"tool\.timeout"):
        wrapped.execute({"query": "2"})
    assert time.monotonic() - started < 1.5


def test_the_call_limit_counts_per_agent_run() -> None:
    counter = CallCounter()
    first = SpecTool(whoami, spec("whoami", max_calls_per_run=2), counter=counter)
    assert first.execute({"query": "a"}) and first.execute({"query": "b"})
    with pytest.raises(ToolResultError, match=r"tool\.call_limit"):
        first.execute({"query": "c"})
    assert SpecTool(whoami, spec("whoami", max_calls_per_run=2), counter=CallCounter()).execute({"query": "d"})


def test_settings_are_validated_against_the_spec() -> None:
    item = spec(
        "whoami",
        params_schema={"type": "object", "properties": {"n": {"type": "integer"}}, "additionalProperties": False},
    )
    assert settings_problems("whoami", item, ToolSettings(params={"n": 1})) == []
    codes = [p.code for p in settings_problems("whoami", item, ToolSettings(params={"n": "x", "other": 1}))]
    assert codes and set(codes) == {"tool.settings_invalid"}
    assert [p.code for p in settings_problems("calc", None, ToolSettings())] == ["tool.settings_unsupported"]
    assert [p.code for p in settings_problems("whoami", spec("whoami"), ToolSettings(connection_id="c"))] == [
        "tool.settings_invalid"
    ]


# ---- integration contract v2 --------------------------------------------------------------------------------------


def test_contract_1_stays_accepted_and_contract_2_takes_specs() -> None:
    assert {1, 2} == SUPPORTED_CONTRACTS and CONTRACT == 2
    old = Extensions([integration(whoami, contract=1)])
    assert [tool.name for tool in old.tools()] == ["whoami"] and old.specs() == {}
    new = Extensions([integration((spec("whoami"), whoami))])
    assert new.specs()["whoami"].capability.source == "author"
    assert new.summary()[0]["contract"] == 2


def test_a_spec_needs_contract_2_and_its_own_tool() -> None:
    with pytest.raises(ExtensionError):
        Extensions([integration((spec("whoami"), whoami), contract=1)])
    with pytest.raises(ExtensionError) as caught:
        Extensions([integration((spec("other"), whoami))])
    assert caught.value.code == "extension.tool_spec"


def test_an_integration_spec_never_lowers_the_effective_capability(tmp_path: Path) -> None:
    harmless = ToolCapability(access="read", reach="internal", source="nodestro")
    item = ToolSpec(id="whoami", category="data", capability=harmless)
    workspace = Workspace(tmp_path / "ws", integrations=[integration((item, whoami))])
    try:
        info = next(info for info in workspace.tools.all() if info.name == "whoami")
        assert info.capability == UNKNOWN
        assert info.spec is not None and info.spec.capability.source == "author"
    finally:
        workspace.close()


# ---- checks (acceptance 2 and 3) and the workspace --------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("NODESTRO_VAULT_KEY", "a" * 44)
    online = spec("whoami", connection_type="search")
    opened = Workspace(
        tmp_path / "ws",
        integrations=[integration((online, whoami), slow_lookup)],
        client_factory=lambda _p: ScriptedClient(
            {"SLOW": [tool_use("slow_lookup", {"query": "1"}), text("answered without it")]}
        ),
    )
    yield opened
    opened.close()


def test_a_network_tool_is_refused_in_the_strict_offline_mode(workspace: Workspace) -> None:
    workspace.update_settings(SettingsIn(strict_offline=True))
    codes = [item.code for item in preflight(workspace, workflow(AgentConfig(name="A", tools=["whoami"]))).problems]
    assert "check.offline_tool" in codes
    workspace.update_settings(SettingsIn(strict_offline=False))
    codes = [item.code for item in preflight(workspace, workflow(AgentConfig(name="A", tools=["calculator"]))).problems]
    assert "check.offline_tool" not in codes


def test_integration_tools_are_named_for_automatic_starts(workspace: Workspace) -> None:
    """Acceptance 3: an integration tool (with or without spec) needs the user's agreement for schedules."""
    flow = workflow(AgentConfig(name="A", tools=["calculator", "slow_lookup", "whoami"]))
    assert external_tools(workspace, flow) == ["slow_lookup", "whoami"]


def test_the_agent_check_needs_a_connection_of_the_right_type(workspace: Workspace) -> None:
    agent = AgentConfig(name="A", tools=["whoami"])
    codes = [issue.code for issue in workspace.builder.validate(agent)]
    assert "check.connection_missing" in codes
    mail = workspace.connections.create(
        ConnectionIn(type="smtp", name="Mail", settings={"host": "smtp.example.com", "from_address": "a@example.com"})
    )
    agent.tool_settings = {"whoami": ToolSettings(connection_id=mail.id), "calculator": ToolSettings()}
    codes = [issue.code for issue in workspace.builder.validate(agent)]
    assert "connection.type_mismatch" in codes and "tool.settings_unused" in codes
    search = workspace.connections.create(ConnectionIn(type="search", name="Search", provider="test"))
    agent.tool_settings = {"whoami": ToolSettings(connection_id=search.id)}
    assert not [
        issue for issue in workspace.builder.validate(agent) if issue.level == "error" and "connection" in issue.code
    ]
    flow = workflow(agent)
    codes = [item.code for item in preflight(workspace, flow).problems]
    assert "check.connection_unchecked" in codes
    workspace.connections.set_secret(search.id, "key")
    workspace.connections.check(search.id)
    assert "check.connection_unchecked" not in [item.code for item in preflight(workspace, flow).problems]


def test_a_tool_not_offered_in_this_mode_is_reported(tmp_path: Path) -> None:
    item = spec("whoami", modes=frozenset({"server"}))
    opened = Workspace(tmp_path / "ws", integrations=[integration((item, whoami))])
    try:
        codes = [i.code for i in preflight(opened, workflow(AgentConfig(name="A", tools=["whoami"]))).problems]
        assert "check.tool_mode_unsupported" in codes
    finally:
        opened.close()


def test_a_slow_host_tool_times_out_and_the_agent_still_answers(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 4: the run stays in control: the tool error goes to the model, which answers."""
    monkeypatch.setattr("nodestro_runtime.tool_spec.HOST_TOOL_TIMEOUT_SECONDS", 0.2)
    workspace.builder.tools.specs.pop("slow_lookup", None)
    from nodestro_runtime import tool_spec

    monkeypatch.setattr(
        tool_spec,
        "host_tool_spec",
        lambda name: spec(name, timeout_seconds=0.2, capability=UNKNOWN) if name == "slow_lookup" else None,
    )
    monkeypatch.setattr("nodestro_runtime.agent_builder.host_tool_spec", tool_spec.host_tool_spec)
    before = threading.active_count()
    result, _tracker = workspace.tester.run(
        AgentTestIn(agent=AgentConfig(name="A", system_prompt="SLOW", tools=["slow_lookup"]), input="x")
    )
    call = next(item for item in result.tool_calls if item.name == "slow_lookup")
    assert call.is_error and "tool.timeout" in (call.output or "")
    assert result.output == "answered without it"
    assert threading.active_count() <= before + 2


# ---- releases ---------------------------------------------------------------------------------------------------


def test_releases_record_builtin_versions_without_changing_old_digests() -> None:
    flow = workflow(AgentConfig(name="A", tools=["calculator"]))
    old = ReleaseBundle(workflow_id="w", workflow=flow, builtin_tools=["calculator"])
    same = ReleaseBundle(workflow_id="w", workflow=flow, builtin_tools=["calculator"], created_at="later")
    assert old.digest() == same.digest() and old.tool_marker("calculator") == "builtin:calculator"
    new = old.model_copy(update={"builtin_versions": {"calculator": 1}})
    assert new.tool_marker("calculator") == "builtin:calculator@1" and new.digest() != old.digest()
