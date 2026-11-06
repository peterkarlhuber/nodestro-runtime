"""Model parameters by capability: output limits of internal operations, model profiles, agent options per model,
declared endpoint limits, thinking budgets, and the orchestrator's structured-output check (RT-061).

No network: connections use Orkexa's documented values, declared settings and never-called clients."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from orkexa import GeminiOptions, ModelLimits
import pytest

from nodestro_runtime import output_limits
from nodestro_runtime.output_limits import OPERATIONS, effective, retry_limit
from nodestro_runtime.preflight import preflight
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import Connection, ProviderSettingsIn
from nodestro_runtime.schemas import AgentConfig, SettingsIn, Workflow, WorkflowIn
from nodestro_runtime.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    found = Workspace(tmp_path / "ws", integrations=[])
    yield found
    found.close()


def codes(workspace: Workspace, **config: Any) -> set[str]:
    return {issue.code for issue in workspace.builder.validate(AgentConfig.model_validate(config))}


# ---- output limits of internal operations ----------------------------------------------------------------------


def test_levels_setting_default_and_model_maximum() -> None:
    claude = ModelLimits(max_output_tokens=128_000, source="documented")
    default = effective("architect_plan", {}, claude)
    assert (default.value, default.source, default.reduced) == (OPERATIONS["architect_plan"], "default", None)
    assert OPERATIONS["architect_plan"] >= 32_000  # no longer the fixed 16,000 that cut off large workflows
    setting = effective("architect_plan", {"architect_plan": 60_000}, claude)
    assert (setting.value, setting.source) == (60_000, "setting")
    reduced = effective("architect_plan", {"architect_plan": 500_000}, claude)
    assert (reduced.value, reduced.configured, reduced.reduced) == (128_000, 500_000, "model_maximum")
    local = effective("architect_plan", {}, ModelLimits(context_window=32_768))
    assert (local.value, local.reduced) == (16_384, "context_window")  # half the window keeps room for the prompt
    unknown = effective("architect_plan", {"architect_plan": 500_000}, ModelLimits())
    assert unknown.value == 500_000  # nothing known, nothing lowered: the provider decides


def test_retry_limit_doubles_within_cap_and_model() -> None:
    claude = ModelLimits(max_output_tokens=128_000)
    assert retry_limit(32_000, 64_000, claude) == 64_000
    assert retry_limit(64_000, 64_000, claude) is None
    assert retry_limit(16_000, 100_000, ModelLimits(max_output_tokens=20_000)) == 20_000
    assert retry_limit(16_000, 100_000, ModelLimits()) == 32_000


def test_settings_overrides_are_validated_and_removable(workspace: Workspace) -> None:
    settings = workspace.settings()
    assert settings.output_limits == {} and settings.output_limit_defaults == OPERATIONS
    assert settings.architect_retry_truncated is True and settings.architect_retry_max_tokens == 64_000
    workspace.update_settings(SettingsIn(output_limits={"architect_plan": 50_000, "run_chat": 3000}))
    assert workspace.settings().output_limits == {"architect_plan": 50_000, "run_chat": 3000}
    workspace.update_settings(SettingsIn(output_limits={"run_chat": None}))  # removes only that one
    assert workspace.settings().output_limits == {"architect_plan": 50_000}
    with pytest.raises(NodestroError) as unknown:
        workspace.update_settings(SettingsIn(output_limits={"nonsense": 5000}))
    assert unknown.value.code == "settings.output_limit_unknown"
    with pytest.raises(NodestroError) as low:
        workspace.update_settings(SettingsIn(output_limits={"architect_plan": 10}))
    assert low.value.code == "settings.output_limit_invalid"
    assert workspace.settings().output_limits == {"architect_plan": 50_000}  # nothing stored on an error
    workspace.update_settings(SettingsIn(architect_retry_truncated=False, architect_retry_max_tokens=100_000))
    after = workspace.settings()
    assert after.architect_retry_truncated is False and after.architect_retry_max_tokens == 100_000


def test_stored_invalid_values_fall_back_to_defaults() -> None:
    assert output_limits.stored_overrides({"architect_plan": "x", "run_chat": 5, "unknown": 9000}) == {}


def test_workspace_output_limit_uses_the_model(workspace: Workspace) -> None:
    workspace.update_settings(SettingsIn(output_limits={"assistant_chat": 300_000}))
    found = workspace.output_limit("assistant_chat", "anthropic:claude-haiku-4-5")
    assert (found.value, found.reduced, found.model_maximum) == (64_000, "model_maximum", 64_000)


# ---- model profiles: capabilities, not provider names ----------------------------------------------------------


def test_profile_of_claude(workspace: Workspace) -> None:
    profile = workspace.builder.profile("anthropic:claude-sonnet-5-5")
    assert profile.limits.max_output_tokens == 128_000 and profile.max_tokens_limit == 128_000
    assert profile.options["thinking"].available and profile.options["thinking"].values == ["adaptive", "between_tools"]
    assert profile.options["cache_control"].available and profile.options["fallbacks"].available
    assert {tool.key: tool.available for tool in profile.native_tools} == {
        "web_search": True,
        "web_fetch": True,
        "code_execution": True,
    }


@pytest.mark.parametrize(
    ("model", "thinking", "budget", "cache"),
    [
        ("openai:gpt-6.1-sol", "provider", "provider", "automatic"),
        ("gemini:gemini-3-pro", "provider", "model", "automatic"),
        ("bedrock:eu.anthropic.claude-sonnet-5-5", "provider", "provider", "not_mapped"),
        ("ollama:qwen3.5:4b", "provider", "provider", "provider"),
    ],
)
def test_other_providers_say_why_an_option_is_unavailable(
    workspace: Workspace, model: str, thinking: str, budget: str, cache: str
) -> None:
    profile = workspace.builder.profile(model)
    assert profile.error is None
    assert (profile.options["thinking"].available, profile.options["thinking"].reason) == (False, thinking)
    assert profile.options["thinking_budget"].reason == budget
    assert profile.options["cache_control"].reason == cache


def test_gemini_2_5_offers_a_thinking_budget_and_no_effort(workspace: Workspace) -> None:
    profile = workspace.builder.profile("gemini:gemini-2.5-flash")
    assert profile.options["thinking_budget"].available
    assert profile.limits.efforts == [] and profile.capabilities["reasoning"] == "unsupported"


def test_native_tools_of_openai_and_gemini_are_shown_as_not_integrated(workspace: Workspace) -> None:
    for model in ("openai:gpt-6.1-sol", "gemini:gemini-3-pro"):
        tools = {tool.key: tool for tool in workspace.builder.profile(model).native_tools}
        assert tools["web_search"].reason == "not_integrated" and not tools["web_search"].available
        assert tools["web_fetch"].reason == "provider"


def test_invalid_model_has_an_error(workspace: Workspace) -> None:
    profile = workspace.builder.profile("llama3.1:8b")  # no provider prefix
    assert profile.error == "config.model_provider_missing" and not profile.options


# ---- agent options and limits by model --------------------------------------------------------------------------


def test_max_tokens_above_the_model_maximum_is_an_issue(workspace: Workspace) -> None:
    found = workspace.builder.validate(AgentConfig(model="anthropic:claude-haiku-4-5", effort=None, max_tokens=100_000))
    issue = next(item for item in found if item.code == "config.max_tokens_above_model")
    assert issue.field == "max_tokens" and issue.params["maximum"] == 64_000
    assert codes(workspace, model="anthropic:claude-opus-5-5", max_tokens=128_000) == set()


def test_max_tokens_bound_is_no_longer_128k() -> None:
    assert AgentConfig(max_tokens=500_000).max_tokens == 500_000  # checked against the model, not a fixed cap
    with pytest.raises(ValueError):
        AgentConfig(max_tokens=2_000_000)


def test_switching_the_model_flags_options_it_cannot_use(workspace: Workspace) -> None:
    """A model switch never leaves an invalid parameter unnoticed: the inspector and the workflow check name it."""
    config = {"thinking": "between_tools", "effort": "high"}
    assert codes(workspace, model="anthropic:claude-sonnet-5-5", **config) == set()
    assert "config.option_unsupported" in codes(workspace, model="anthropic:claude-opus-5-5", **config)
    assert "config.option_unsupported" in codes(workspace, model="openai:gpt-6.1-sol", **config)
    effort = codes(workspace, model="anthropic:claude-sonnet-4-6", effort="xhigh")
    assert effort & {"config.capability_unsupported", "library"}


def test_server_tools_follow_the_provider_native_tools(workspace: Workspace) -> None:
    assert "config.native_tool_unsupported" in codes(workspace, model="openai:gpt-6.1-sol", server_tools=["web_search"])
    assert "config.native_tool_unsupported" not in codes(
        workspace, model="anthropic:claude-opus-5-5", server_tools=["web_search"]
    )


def test_thinking_budget_goes_to_gemini_2_5_only(workspace: Workspace) -> None:
    connection = Connection(provider="gemini", client=object())
    config = AgentConfig(model="gemini:gemini-2.5-flash", effort=None, thinking_budget=2048)
    built = workspace.builder.build(config, connection=connection)
    assert isinstance(built.agent.options, GeminiOptions) and built.agent.options.thinking_budget == 2048
    assert "config.option_unsupported" in codes(workspace, model="gemini:gemini-3-pro", effort=None, thinking_budget=1)
    assert "config.thinking_budget_with_effort" in codes(
        workspace, model="gemini:gemini-2.5-flash", effort="low", thinking_budget=1024
    )


def test_internal_effort_falls_back_to_an_accepted_level() -> None:
    connection = Connection(provider="anthropic", client=object())
    assert connection.effort("anthropic:claude-sonnet-4-6", "xhigh") == "high"
    assert connection.effort("anthropic:claude-opus-5-5", "xhigh") == "xhigh"
    assert connection.effort("anthropic:claude-haiku-4-5", "high") is None
    assert Connection(provider="gemini", client=object()).effort("gemini:gemini-3-pro", "max") == "high"


# ---- declared endpoint limits ------------------------------------------------------------------------------------


def test_declared_endpoint_limits_count_for_validation(workspace: Workspace) -> None:
    workspace.update_settings(
        SettingsIn(
            providers={
                "compatible": ProviderSettingsIn(
                    base_url="http://localhost:8000/v1", limits={"max_output_tokens": 4096, "context_window": 32_768}
                )
            }
        )
    )
    status = next(item for item in workspace.settings().providers if item.id == "compatible")
    assert status.limits == {"max_output_tokens": 4096, "context_window": 32_768}
    profile = workspace.builder.profile("compatible:my-model")
    assert profile.limits.max_output_tokens == 4096 and profile.limits.source == "declared"
    assert "config.max_tokens_above_model" in codes(
        workspace, model="compatible:my-model", effort=None, max_tokens=8000
    )
    assert workspace.output_limit("architect_plan", "compatible:my-model").value == 4096


@pytest.mark.parametrize(
    ("provider", "limits", "code"),
    [
        ("anthropic", {"max_output_tokens": 4096}, "provider.field_not_supported"),
        ("compatible", {"max_output_tokens": 10}, "provider.limit_invalid"),
        ("compatible", {"max_output_tokens": 9000, "context_window": 4096}, "provider.limit_invalid"),
        ("compatible", {"tokens": 4096}, "provider.limit_unknown"),
    ],
)
def test_declared_limits_are_validated(workspace: Workspace, provider: str, limits: dict[str, int], code: str) -> None:
    with pytest.raises(NodestroError) as caught:
        workspace.update_settings(
            SettingsIn(providers={provider: ProviderSettingsIn(base_url="http://localhost:8000/v1", limits=limits)})
            if provider != "anthropic"
            else SettingsIn(providers={provider: ProviderSettingsIn(limits=limits)})
        )
    assert caught.value.code == code


def test_local_window_limits_internal_operations(workspace: Workspace) -> None:
    workspace.builder.local_window = lambda: 32_768
    found = workspace.output_limit("architect_plan", "ollama:qwen3.5:4b")
    assert (found.value, found.reduced) == (16_384, "context_window")
    assert workspace.builder.profile("ollama:qwen3.5:4b").limits.context_window == 32_768


# ---- the orchestrator model needs structured output (RT-061) -----------------------------------------------------


def _orchestrator(model: str) -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Lead",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "lead", "type": "orchestrator", "label": "Lead", "orchestrator": {"model": model}},
                {"id": "out", "type": "output"},
            ],
            "edges": [
                {"id": "a", "source": "start", "target": "lead"},
                {"id": "b", "source": "lead", "target": "out"},
            ],
        }
    )


def test_orchestrator_model_without_structured_output_is_an_error(workspace: Workspace) -> None:
    workspace.update_settings(
        SettingsIn(providers={"compatible": ProviderSettingsIn(base_url="http://localhost:8000/v1")})
    )
    check = preflight(workspace, Workflow.model_validate(_orchestrator("compatible:my-model").model_dump()))
    issue = next(item for item in check.problems if item.code == "check.orchestrator_model_unsupported")
    assert issue.level == "error" and issue.node_id == "lead" and issue.params["support"] == "unknown"
    ok = preflight(workspace, Workflow.model_validate(_orchestrator("anthropic:claude-opus-5-5").model_dump()))
    assert "check.orchestrator_model_unsupported" not in {item.code for item in ok.problems}
