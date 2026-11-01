"""Bedrock agents: model ids, options from the connection only, runs on stubbed Converse, residency, export."""

from __future__ import annotations

import base64
from collections.abc import Iterator
import json
from pathlib import Path
from typing import Any

from bedrock_support import (
    ACCOUNT,
    FakeSession,
    ScriptedRuntime,
    no_network,  # noqa: F401  (autouse)
    stubbed,
)
from orkexa import BedrockOptions
from orkexa.dynamic import ProviderSettings
import pytest

from nodestro_runtime.agent_test import AgentTestIn
from nodestro_runtime.bedrock import BedrockSettings
from nodestro_runtime.catalog import models
from nodestro_runtime.engine import node_problem
from nodestro_runtime.preflight import PROVIDER_CHECKS, preflight
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import (
    Connection,
    MissingApiKeyError,
    check_model,
    environment_connection,
    exported_settings,
    model_id,
    provider_of,
    qualify,
)
from nodestro_runtime.schemas import AgentConfig, RunIn, SettingsIn, WorkflowIn
from nodestro_runtime.workspace import Workspace

CLAUDE = "bedrock:eu.anthropic.claude-sonnet-5-5"
ARN = f"arn:aws:bedrock:eu-central-1:{ACCOUNT}:application-inference-profile/abc123"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


# ---- model ids ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "bedrock:anthropic.claude-3-5-sonnet-20240620-v1:0",
        "bedrock:eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        f"bedrock:{ARN}",
        "bedrock:arn:aws:bedrock:eu-central-1::foundation-model/anthropic.claude-sonnet-5-5",
    ],
)
def test_full_model_ids_are_kept(model: str) -> None:
    assert check_model(model) == model and qualify(model) == model
    assert provider_of(model) == "bedrock" and model_id(model) == model.split(":", 1)[1]


def test_an_unprefixed_bedrock_id_needs_its_provider() -> None:
    with pytest.raises(NodestroError) as caught:
        check_model("anthropic.claude-3-5-sonnet-20240620-v1:0")
    assert caught.value.code == "config.model_provider_missing"


def test_capabilities_come_from_orkexa_only() -> None:
    connection = Connection(provider="bedrock", capabilities={"structured_output": "unsupported"})
    caps = connection.model_capabilities(CLAUDE)
    assert caps.supports("structured_output") and caps.supports("tools") and caps.supports("image_input")
    nova = connection.model_capabilities("bedrock:eu.amazon.nova-pro-v1:0")
    assert not nova.supports("structured_output")
    assert connection.model_capabilities(f"bedrock:{ARN}").tools == "unknown"  # an application profile hides its model


def test_catalog_suggests_eu_inference_profiles() -> None:
    found = {info.id: info for info in models() if info.provider == "bedrock"}
    assert CLAUDE in found and "bedrock:eu.amazon.nova-pro-v1:0" in found
    assert all(info.model.startswith("eu.") for info in found.values())
    assert all("suggestion" in info.description for info in found.values())
    assert all(info.price_status == "unknown" for info in found.values())  # never Anthropic's prices


# ---- options -------------------------------------------------------------------------------


def _connection(**settings: Any) -> Connection:
    return Connection(
        provider="bedrock",
        bedrock=BedrockSettings(region="eu-central-1", **settings),
        session_factory=lambda: "session",
    )


def test_options_come_from_the_connection_only() -> None:
    options = _connection(data_residency="eu_only").options({"top_k": 20})
    assert isinstance(options, BedrockOptions)
    assert (options.region_name, options.session, options.data_residency) == ("eu-central-1", "session", "eu_only")
    assert options.additional_model_request_fields == {"top_k": 20}
    settings = _connection().settings()
    assert isinstance(settings, ProviderSettings) and settings.api_key is None and settings.base_url is None
    assert isinstance(settings.options, BedrockOptions) and settings.options.session == "session"
    assert _connection().agent_kwargs() == {"provider": "bedrock"}


@pytest.mark.parametrize(
    "options",
    [
        {"region_name": "us-east-1"},
        {"region": "us-east-1"},
        {"profile_name": "admin"},
        {"data_residency": None},
        {"allowed_regions": ["us-east-1"]},
        {"endpoint_url": "https://evil.example.com"},
        {"base_url": "https://evil.example.com"},
        {"aws_access_key_id": "AKIA"},
        {"session": "x"},
        {"guardrail": {"guardrailIdentifier": "g"}},
        {"extra_body": {"top_k": 1}},
        {"extra_headers": {"x": "y"}},
        {"inferenceConfig": {"maxTokens": 1}},
        {"tools": []},
        {"anthropic_beta": ["x"]},
        {"mcp_servers": [{"url": "https://mcp.example.com"}]},
    ],
)
def test_request_options_cannot_change_the_connection(options: dict[str, Any], tmp_path: Path) -> None:
    with pytest.raises(NodestroError) as caught:
        _connection(data_residency="eu_only").options(options)
    assert caught.value.code == "config.reserved_request_options"
    # The same through an agent configuration (the inspector shows it).
    workspace = Workspace(tmp_path / "ws", integrations=[])
    try:
        issues = workspace.builder.validate(AgentConfig(model=CLAUDE, request_options=options))
        assert "config.reserved_request_options" in {issue.code for issue in issues}
    finally:
        workspace.close()


@pytest.mark.parametrize(
    "change",
    [
        {"thinking": "adaptive"},
        {"cache_control": False},
        {"cache_ttl": "1h"},
        {"fallbacks": ["bedrock:eu.anthropic.claude-opus-5-5"]},
        {"server_tools": ["web_search"]},
    ],
)
def test_anthropic_only_options_stay_refused_for_claude_on_bedrock(change: dict[str, Any], tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "ws", integrations=[])
    try:
        config = AgentConfig.model_validate({"model": CLAUDE, **change})
        issues = workspace.builder.validate(config)
        assert "config.anthropic_only" in {issue.code for issue in issues}
    finally:
        workspace.close()


def test_a_valid_bedrock_agent_has_no_issues(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "ws", integrations=[])
    try:
        assert workspace.builder.validate(AgentConfig(model=CLAUDE, tools=["calculator"])) == []
        nova = AgentConfig.model_validate(
            {
                "model": "bedrock:eu.amazon.nova-pro-v1:0",
                "effort": None,
                "output": {"mode": "structured", "fields": [{"name": "a"}]},
            }
        )
        codes = {issue.code for issue in workspace.builder.validate(nova)}
        assert "config.capability_unsupported" in codes  # Orkexa's value: Nova has no structured output
    finally:
        workspace.close()


# ---- runs on a stubbed Converse ----------------------------------------------------------


@pytest.fixture
def runtime() -> ScriptedRuntime:
    return ScriptedRuntime([])


@pytest.fixture
def workspace(tmp_path: Path, runtime: ScriptedRuntime) -> Iterator[Workspace]:
    opened = Workspace(
        tmp_path / "ws",
        integrations=[],
        client_factory=lambda provider: runtime.client if provider == "bedrock" else None,
    )
    opened.update_settings(SettingsIn.model_validate({"providers": {"bedrock": {"region": "eu-central-1"}}}))
    yield opened
    opened.close()


def test_agent_test_with_tool_calls_and_streaming(workspace: Workspace, runtime: ScriptedRuntime) -> None:
    runtime.answers = [
        [{"toolUse": {"toolUseId": "tool_1", "name": "calculator", "input": {"expression": "2+3"}}}],
        [{"text": "The answer is 5."}],
    ]
    result, _tracker = workspace.tester.run(
        AgentTestIn(agent=AgentConfig(name="Calc", model=CLAUDE, tools=["calculator"]), input="2+3?")
    )
    assert result.status == "succeeded", result.error
    assert result.text == "The answer is 5." and result.model == CLAUDE
    assert [call.name for call in result.tool_calls] == ["calculator"]
    first, second = runtime.calls
    assert first["modelId"] == "eu.anthropic.claude-sonnet-5-5"
    assert [tool["toolSpec"]["name"] for tool in first["toolConfig"]["tools"]] == ["calculator"]
    results = [block["toolResult"] for block in second["messages"][-1]["content"] if "toolResult" in block]
    assert results and results[0]["toolUseId"] == "tool_1" and "5" in json.dumps(results[0]["content"])
    runtime.stubber.assert_no_pending_responses()  # no other operation was called


def test_full_model_ids_reach_converse(workspace: Workspace, runtime: ScriptedRuntime) -> None:
    runtime.answers = [[{"text": "ok"}]]
    model = "bedrock:anthropic.claude-sonnet-5-5-20260101-v1:0"
    result, _ = workspace.tester.run(AgentTestIn(agent=AgentConfig(name="Ids", model=model), input="x"))
    assert result.status == "succeeded", result.error
    assert runtime.calls[0]["modelId"] == "anthropic.claude-sonnet-5-5-20260101-v1:0"


def test_structured_output(workspace: Workspace, runtime: ScriptedRuntime) -> None:
    runtime.answers = [[{"text": '{"city": "Vienna", "population": 2000000}'}]]
    config = AgentConfig.model_validate(
        {
            "name": "S",
            "model": CLAUDE,
            "output": {
                "mode": "structured",
                "fields": [{"name": "city", "type": "string"}, {"name": "population", "type": "integer"}],
            },
        }
    )
    result, _ = workspace.tester.run(AgentTestIn(agent=config, input="Capital of Austria"))
    assert result.status == "succeeded", result.error
    assert result.output == {"city": "Vienna", "population": 2000000}
    assert runtime.calls[0]["outputConfig"]["textFormat"]["type"] == "json_schema"


def test_image_input(workspace: Workspace, runtime: ScriptedRuntime) -> None:
    runtime.answers = [[{"text": "A black square."}]]
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode()},
    }
    built = workspace.builder.build(
        AgentConfig(name="Eyes", model=CLAUDE), connection=workspace.connection("bedrock"), extra_context=[image]
    )
    assert built.agent.ask("Describe the image") == "A black square."
    blocks = runtime.calls[0]["messages"][0]["content"]
    assert any(block.get("image", {}).get("format") == "png" for block in blocks)


async def test_workflow_run(workspace: Workspace, runtime: ScriptedRuntime) -> None:
    runtime.answers = [[{"text": "done"}]]
    flow = WorkflowIn.model_validate(
        {
            "name": "Bedrock flow",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "a",
                    "type": "agent",
                    "label": "A",
                    "agent": {"name": "A", "system_prompt": "A", "model": CLAUDE},
                },
                {"id": "out", "type": "output"},
            ],
            "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
        }
    )
    saved = workspace.save_workflow(flow)
    started = await workspace.run(saved, RunIn(input="go"))
    finished = await workspace.wait(started.id, timeout=20)
    assert finished.status == "succeeded", finished.error
    assert runtime.calls[0]["system"][0]["text"] == "A"


def test_eu_only_refuses_a_profile_routed_outside_the_eu_before_the_call() -> None:
    runtime, runtime_stub = stubbed("bedrock-runtime")
    calls: list[Any] = []
    runtime.converse_stream = lambda **params: calls.append(params)  # never reached
    control, control_stub = stubbed("bedrock")
    model = "eu.anthropic.claude-sonnet-5-5"
    control_stub.add_response(
        "get_inference_profile",
        {
            "inferenceProfileName": model,
            "inferenceProfileArn": f"arn:aws:bedrock:eu-central-1:{ACCOUNT}:inference-profile/{model}",
            "inferenceProfileId": model,
            "status": "ACTIVE",
            "type": "SYSTEM_DEFINED",
            "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-5-5"}],
        },
        {"inferenceProfileIdentifier": model},
    )
    session = FakeSession({"bedrock-runtime": runtime, "bedrock": control})
    connection = Connection(
        provider="bedrock",
        bedrock=BedrockSettings(region="eu-central-1", data_residency="eu_only"),
        session_factory=lambda: session,
    )
    from orkexa import Agent, DataResidencyError

    agent: Agent[Any] = Agent(model=model, options=connection.options(), stream=True, **connection.agent_kwargs())
    with pytest.raises(DataResidencyError) as caught:
        agent.ask("hello")
    assert calls == []
    control_stub.assert_no_pending_responses()
    runtime_stub.assert_no_pending_responses()
    found = node_problem(caught.value)
    assert found.code == "provider.data_residency_violation"


def test_run_errors_name_no_aws_identity() -> None:
    from orkexa import AuthenticationError

    error = AuthenticationError(
        f"bedrock: AccessDeniedException: User: arn:aws:sts::{ACCOUNT}:assumed-role/tenant-role/nodestro-alpha "
        "is not authorized to perform: bedrock:InvokeModel",
        provider="bedrock",
    )
    found = node_problem(error)
    assert ACCOUNT not in found.message and "tenant-role" not in found.message


# ---- preflight and exports ---------------------------------------------------------------


def test_preflight_has_bedrock_codes(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "ws", integrations=[])
    try:
        flow = WorkflowIn.model_validate(
            {
                "name": "Needs Bedrock",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "model": CLAUDE}},
                    {"id": "out", "type": "output"},
                ],
                "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
            }
        )
        saved = workspace.save_workflow(flow)
        codes = {item.code for item in preflight(workspace, saved).problems}
        assert "check.bedrock_not_configured" in codes and "check.bedrock_not_configured" in PROVIDER_CHECKS
        assert "check.api_key_missing" not in codes
    finally:
        workspace.close()


def test_exported_projects_use_the_default_chain_and_keep_eu_only() -> None:
    stored = {"region": "eu-central-1", "auth": "profile", "profile_name": "work", "data_residency": "eu_only"}
    exported = exported_settings("bedrock", stored)
    assert exported == {"region": "eu-central-1", "data_residency": "eu_only"}
    assert exported_settings("openai", {"api_key": "sk"}) == {}
    connection = environment_connection("bedrock", environ={}, settings={"bedrock": exported})
    assert connection.bedrock == BedrockSettings(region="eu-central-1", data_residency="eu_only")
    options = connection.options()
    assert options.session is None and options.profile_name is None and options.data_residency == "eu_only"
    # The target machine's region wins, but not over the residency.
    moved = environment_connection("bedrock", environ={"AWS_REGION": "eu-west-1"}, settings={"bedrock": exported})
    assert moved.bedrock is not None and moved.bedrock.region == "eu-west-1"
    with pytest.raises(NodestroError) as caught:
        environment_connection("bedrock", environ={"AWS_DEFAULT_REGION": "us-east-1"}, settings={"bedrock": exported})
    assert caught.value.code == "provider.data_residency_region"
    with pytest.raises(MissingApiKeyError) as missing:
        environment_connection("bedrock", environ={})
    assert missing.value.code == "provider.region_missing"
    # Keys of other providers are never read for Bedrock.
    plain = environment_connection("bedrock", environ={"AWS_REGION": "eu-central-1", "BEDROCK_API_KEY": "x"})
    assert plain.api_key is None and plain.agent_kwargs() == {"provider": "bedrock"}


def test_secret_keys_and_repr_hold_no_aws_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "never-shown-secret")
    workspace = Workspace(tmp_path / "ws", integrations=[])
    try:
        workspace.update_settings(SettingsIn.model_validate({"providers": {"bedrock": {"region": "eu-central-1"}}}))
        assert "never-shown-secret" not in workspace.secret_keys()
        assert "never-shown-secret" not in repr(workspace.connection("bedrock"))
        assert "never-shown-secret" not in json.dumps(workspace.settings().model_dump())
    finally:
        workspace.close()


# ---- prices per region -------------------------------------------------------------------


def _regional_price() -> Any:
    from orkexa.pricing import ModelPrice, Tariff, TariffOption, TokenRates

    return ModelPrice(
        provider="bedrock",
        model="eu.anthropic.claude-sonnet-5-5",
        rates=TokenRates.of("3", "15"),
        tariffs=(Tariff("aws_region", "eu-central-1", (TariffOption("eu-central-1"), TariffOption("eu-west-1"))),),
        source="acme-aws-contract",
        quality="contract",
    )


def test_price_conditions_follow_the_current_region(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "ws", integrations=[])
    try:
        assert workspace.pricing.request_conditions("bedrock") == {}  # not configured
        assert workspace.pricing.request_conditions("anthropic") == {}
        workspace.update_settings(SettingsIn.model_validate({"providers": {"bedrock": {"region": "eu-west-1"}}}))
        assert workspace.pricing.request_conditions("bedrock") == {"aws_region": "eu-west-1"}
        workspace.update_settings(SettingsIn.model_validate({"providers": {"bedrock": {"region": "eu-north-1"}}}))
        assert workspace.pricing.request_conditions("bedrock") == {"aws_region": "eu-north-1"}
    finally:
        workspace.close()


def test_preflight_and_catalog_check_the_price_region(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "ws", integrations=[])
    try:
        workspace.pricing.set_override(_regional_price())
        flow = WorkflowIn.model_validate(
            {
                "name": "Priced",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "model": CLAUDE}},
                    {"id": "out", "type": "output"},
                ],
                "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
            }
        )
        saved = workspace.save_workflow(flow)

        def unpriced(region: str) -> bool:
            workspace.update_settings(SettingsIn.model_validate({"providers": {"bedrock": {"region": region}}}))
            return "check.model_unpriced" in {item.code for item in preflight(workspace, saved).problems}

        assert unpriced("eu-central-1") is False
        assert unpriced("eu-west-1") is False
        assert unpriced("eu-north-1") is True  # the price does not cover this region
        shown = {info.id: info for info in models(workspace.pricing)}[CLAUDE]
        assert shown.price_region == "eu-central-1" and shown.price_regions == ["eu-central-1", "eu-west-1"]
    finally:
        workspace.close()


def test_strict_price_mode_prices_bedrock_by_the_connection_region(
    workspace: Workspace, runtime: ScriptedRuntime
) -> None:
    workspace.pricing.set_override(_regional_price())
    workspace.update_settings(SettingsIn.model_validate({"price_mode": "strict"}))
    runtime.answers = [[{"text": "priced"}]]
    result, _ = workspace.tester.run(AgentTestIn(agent=AgentConfig(name="P", model=CLAUDE), input="x"))
    assert result.status == "succeeded", result.error
    assert result.usage.known_cost_usd is not None and result.usage.known_cost_usd > 0
    workspace.update_settings(SettingsIn.model_validate({"providers": {"bedrock": {"region": "eu-north-1"}}}))
    refused, _ = workspace.tester.run(AgentTestIn(agent=AgentConfig(name="P", model=CLAUDE), input="x"))
    assert refused.status == "failed" and refused.error_code == "pricing.price_unknown"
    assert len(runtime.calls) == 1  # refused before the request
