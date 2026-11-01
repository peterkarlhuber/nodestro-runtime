"""Regression tests of the Bedrock review (PR #66): tenant session isolation, redaction, fail-closed clients,
imported settings, verification under residency, pinned price regions, concurrent checks. All offline."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import threading
import time
from typing import Any

from bedrock_support import (
    ACCOUNT,
    ROLE_A,
    STS_KEY,
    FakeSession,
    ScriptedRuntime,
    StsFactory,
    no_network,  # noqa: F401  (autouse)
    tenant_storage,
)
import pytest

from nodestro_runtime import bedrock as bedrock_module
from nodestro_runtime.bedrock import BedrockServerPolicy, BedrockSettings, TenantCredentials, redact_identity
from nodestro_runtime.engine import node_problem
from nodestro_runtime.postgres_import import ImportReport, _without_provider_keys
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import AgentConfig, SettingsIn
from nodestro_runtime.workspace import Workspace

CLAUDE = "bedrock:eu.anthropic.claude-sonnet-5-5"


def bedrock_change(**values: Any) -> SettingsIn:
    return SettingsIn.model_validate({"providers": {"bedrock": values}})


@pytest.fixture
def opened() -> Iterator[list[Workspace]]:
    found: list[Workspace] = []
    yield found
    for workspace in found:
        workspace.close()


def _tenant(tmp_path: Path, opened: list[Workspace], factory: StsFactory, **policy: Any) -> Workspace:
    storage = tenant_storage(
        tmp_path / "db",
        "alpha",
        BedrockServerPolicy(tenant_id="alpha", role=ROLE_A, sts_client_factory=factory, **policy),
    )
    workspace = Workspace(tmp_path / "alpha", integrations=[], storage=storage)
    opened.append(workspace)
    workspace.update_settings(bedrock_change(region="eu-central-1"))
    return workspace


# ---- the tenant session reads nothing of the operator's AWS configuration ----------------


@pytest.fixture
def operator_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The operator's AWS environment: a profile that does not exist, a config file with region, CA bundle,
    retries and a credential process (which leaves a marker if it ever runs), ambient keys and region."""
    marker = tmp_path / "credential-process-ran"
    config = tmp_path / "operator-config"
    config.write_text(
        "[default]\nregion = us-west-2\nca_bundle = /operator/ca.pem\nretry_mode = adaptive\n"
        f"credential_process = /bin/sh -c 'touch {marker}'\n"
        "[profile operator]\nregion = us-east-2\nca_bundle = /operator/other.pem\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(config))
    monkeypatch.setenv("AWS_PROFILE", "nonexistent-operator-profile")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_RETRY_MODE", "adaptive")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAOPERATOR00000000")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "operator-secret")
    monkeypatch.delenv("AWS_CA_BUNDLE", raising=False)
    return marker


@pytest.mark.parametrize("profile", ["nonexistent-operator-profile", "operator"])
def test_tenant_sessions_ignore_the_operator_profile_and_config(
    operator_config: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    monkeypatch.setenv("AWS_PROFILE", profile)
    factory = StsFactory()
    credentials = TenantCredentials(BedrockServerPolicy("alpha", ROLE_A, sts_client_factory=factory), "eu-central-1")
    session = credentials.session()  # no ProfileNotFound
    core = session._session
    assert session.region_name == "eu-central-1"
    assert core.get_scoped_config() == {} and core.full_config == {"profiles": {}}
    assert core.get_config_variable("profile") is None
    assert core.get_config_variable("ca_bundle") is None
    assert core.get_config_variable("retry_mode") == "legacy"  # botocore's default, not the operator's
    client = session.client("bedrock-runtime")
    assert client.meta.region_name == "eu-central-1"
    assert client.meta.endpoint_url == "https://bedrock-runtime.eu-central-1.amazonaws.com"
    # The operator's own STS client (base credentials) may use the operator's configuration; the stub's does not
    # need the missing profile.
    monkeypatch.delenv("AWS_PROFILE")
    monkeypatch.setattr("boto3.DEFAULT_SESSION", None)  # a default session cached by an earlier test
    factory.expect(ROLE_A, "alpha")
    assert session.get_credentials().get_frozen_credentials().access_key == STS_KEY
    assert not operator_config.exists()  # the operator's credential_process never ran


def test_the_operators_ca_bundle_environment_variable_is_honoured(
    operator_config: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "corporate-ca.pem"
    bundle.write_text("", encoding="utf-8")
    monkeypatch.setenv("AWS_CA_BUNDLE", str(bundle))
    session = TenantCredentials(
        BedrockServerPolicy("alpha", ROLE_A, sts_client_factory=StsFactory()), "eu-west-1"
    ).session()
    assert session._session.get_config_variable("ca_bundle") == str(bundle)


# ---- redaction (M1, M2) ----------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        f"User: arn:aws:sts::{ACCOUNT}:assumed-role/tenant-role/nodestro-alpha is not authorized on resource: "
        f"arn:aws:bedrock:eu-central-1:{ACCOUNT}:inference-profile/eu.anthropic.claude-sonnet-5-5",
        f"The provided model identifier is invalid. arn:aws:bedrock:eu-central-1:{ACCOUNT}:provisioned-model/xyz",
        f"arn:aws:bedrock:eu-central-1:{ACCOUNT}:application-inference-profile/abc is not available",
        f"bedrock: AccessDeniedException: Account {ACCOUNT} is not authorized",
        f"bedrock: ValidationException: owner {ACCOUNT}.",
    ],
)
def test_aws_accounts_are_removed_from_error_texts(message: str) -> None:
    from orkexa import AuthenticationError

    cleaned = redact_identity(message)
    assert ACCOUNT not in cleaned and "tenant-role" not in cleaned
    assert ACCOUNT not in node_problem(AuthenticationError(f"bedrock: {message}", provider="bedrock")).message
    if ":inference-profile/" in message:
        assert "arn:aws:bedrock:eu-central-1:[ACCOUNT]:inference-profile/eu.anthropic.claude-sonnet-5-5" in cleaned


def test_other_twelve_digit_numbers_stay_in_non_aws_texts() -> None:
    assert redact_identity("processed 123456789012 tokens") == "processed 123456789012 tokens"


class _FailingConnection:
    """A Coding Agent connection whose provider fails with the operator's identity in its message."""

    provider = "bedrock"

    def effort(self, model: str, wanted: Any) -> Any:
        from orkexa import AuthenticationError

        raise AuthenticationError(
            f"bedrock: AccessDeniedException: User: arn:aws:sts::{ACCOUNT}:assumed-role/nodestro-tenant-alpha/"
            f"nodestro-alpha is not authorized on arn:aws:bedrock:eu-central-1:{ACCOUNT}:inference-profile/x",
            provider="bedrock",
        )


def test_tool_job_errors_name_no_aws_identity(tmp_path: Path) -> None:
    from orkexa import UsageTracker

    from nodestro_runtime.tool_library import ToolJob, _Decision

    workspace = Workspace(tmp_path / "ws", integrations=[])
    try:
        job = ToolJob(requirement="add two numbers")
        workspace.tools._run_job(job, _Decision(), _FailingConnection(), CLAUDE, UsageTracker())  # type: ignore[arg-type]
        assert job.status == "failed" and job.error
        assert ACCOUNT not in job.error and "nodestro-tenant-alpha" not in job.error
    finally:
        workspace.close()


# ---- fail closed (L1) --------------------------------------------------------------------


def test_a_tenant_agent_without_options_still_uses_only_the_tenant_role(
    tmp_path: Path, opened: list[Workspace], monkeypatch: pytest.MonkeyPatch
) -> None:
    import botocore.credentials

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAOPERATOR00000000")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "operator-secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")

    def ambient(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the server's ambient AWS credential chain was used for a tenant")

    monkeypatch.setattr(botocore.credentials, "create_credential_resolver", ambient)
    factory = StsFactory()
    workspace = _tenant(tmp_path, opened, factory)
    connection = workspace.connection("bedrock")
    from orkexa import Agent

    # Built the wrong way (no options=): the runtime client is the tenant's own anyway.
    agent: Agent[Any] = Agent(model="eu.anthropic.claude-sonnet-5-5", **connection.agent_kwargs())
    client = connection.agent_kwargs()["client"]
    factory.expect(ROLE_A, "alpha")
    assert client._request_signer._credentials.get_frozen_credentials().access_key == STS_KEY
    assert client.meta.region_name == "eu-central-1"
    assert agent.provider == "bedrock"
    # Built the right way: options carry a control client of the same tenant session for residency checks.
    options = connection.options()
    assert options.control_client is not None and options.control_client.meta.region_name == "eu-central-1"
    built = workspace.builder.build(AgentConfig(name="T", model=CLAUDE), connection=connection)
    assert built.agent.provider == "bedrock"
    # A server connection without a tenant session refuses both ways.
    from nodestro_runtime.providers import Connection

    bare = Connection(provider="bedrock", bedrock=BedrockSettings(region="eu-central-1"), require_session=True)
    for build in (bare.agent_kwargs, bare.options, bare.settings):
        with pytest.raises(NodestroError) as caught:
            build()
        assert caught.value.code == "provider.bedrock_not_available"


# ---- imported and stored settings (L2) ---------------------------------------------------


def test_imports_drop_bedrock_sign_in_and_verification() -> None:
    report = ImportReport(tenant_id="alpha")
    data = json.dumps(
        {
            "providers": {
                "bedrock": {
                    "region": "eu-central-1",
                    "auth": "profile",
                    "profile_name": "work",
                    "data_residency": "eu_only",
                    "verification": {"state": "verified", "checked_at": "2020-01-01T00:00:00+00:00", "code": None},
                }
            }
        }
    )
    cleaned = json.loads(_without_provider_keys(data, report))
    assert cleaned["providers"]["bedrock"] == {"region": "eu-central-1", "data_residency": "eu_only"}
    assert any("Bedrock" in item for item in report.not_imported)


def test_invalid_or_forged_stored_values_are_not_trusted(tmp_path: Path, opened: list[Workspace]) -> None:
    workspace = _tenant(tmp_path, opened, StsFactory())
    forged = {"state": "verified", "checked_at": "2020-01-01T00:00:00+00:00", "code": None, "for": "x"}
    workspace.store.put_raw(
        "settings", "app", {"providers": {"bedrock": {"region": "eu-central-1", "verification": forged}}}
    )
    shown = next(item for item in workspace.settings().providers if item.id == "bedrock")
    assert shown.configured and shown.verification is not None and shown.verification.state == "configured"
    workspace.store.put_raw("settings", "app", {"providers": {"bedrock": {"region": "attacker.example/x?"}}})
    shown = next(item for item in workspace.settings().providers if item.id == "bedrock")
    assert shown.configured is False and shown.region is None


# ---- verification under residency (L3) ---------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-5-5",
        "global.anthropic.claude-sonnet-5-5",
    ],
)
def test_verification_refuses_what_a_run_would_refuse_before_any_call(model: str) -> None:
    session = FakeSession({})
    result = bedrock_module.verify(
        session, BedrockSettings(region="eu-central-1", data_residency="eu_only"), model=model
    )
    assert result["state"] == "failed" and result["code"] == "provider.data_residency_violation"
    assert session.calls == []


# ---- strict prices use the region the agent was built for (L4) ----------------------------


def _regional_price() -> Any:
    from orkexa.pricing import ModelPrice, Tariff, TariffOption, TokenRates

    return ModelPrice(
        provider="bedrock",
        model="eu.anthropic.claude-sonnet-5-5",
        rates=TokenRates.of("3", "15"),
        tariffs=(Tariff("aws_region", "eu-central-1", (TariffOption("eu-central-1"),)),),
        source="acme-aws-contract",
        quality="contract",
    )


def test_strict_price_check_uses_the_agents_own_region(tmp_path: Path) -> None:
    runtime = ScriptedRuntime([[{"text": "first"}]])
    workspace = Workspace(
        tmp_path / "ws",
        integrations=[],
        client_factory=lambda provider: runtime.client if provider == "bedrock" else None,
    )
    try:
        workspace.update_settings(bedrock_change(region="eu-central-1"))
        workspace.update_settings(SettingsIn.model_validate({"price_mode": "strict"}))
        workspace.pricing.set_override(_regional_price())
        tracker = workspace.tracker()
        built = workspace.builder.build(
            AgentConfig(name="Pinned", model=CLAUDE), connection=workspace.connection("bedrock"), usage_tracker=tracker
        )
        # The settings change while the agent exists: its requests still go out from (and are priced for) its region.
        workspace.update_settings(bedrock_change(region="eu-north-1"))
        assert workspace.pricing.request_conditions("bedrock") == {"aws_region": "eu-north-1"}
        assert built.agent.ask("x") == "first"
    finally:
        workspace.close()


# ---- concurrent verification (info) ------------------------------------------------------


def test_concurrent_checks_run_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = Workspace(tmp_path / "ws", integrations=[])
    try:
        workspace.update_settings(bedrock_change(region="eu-central-1"))
        calls: list[int] = []

        def verify(session: Any, settings: Any, model: Any = None) -> dict[str, Any]:
            calls.append(1)
            time.sleep(0.05)
            return {"state": "verified", "checked_at": "2026-11-01T00:00:00+00:00", "code": None}

        monkeypatch.setattr(bedrock_module, "verify", verify)
        monkeypatch.setattr(bedrock_module, "desktop_session", lambda settings: object())
        outcomes: list[str] = []

        def check() -> None:
            try:
                workspace.verify_provider("bedrock")
                outcomes.append("checked")
            except NodestroError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=check) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert calls == [1] and sorted(outcomes) == ["checked", *["provider.verify_rate_limited"] * 3]
    finally:
        workspace.close()


# ---- studio data: capabilities in the catalog, regions of a price -----------------------


def test_catalog_capabilities_for_bedrock_only() -> None:
    from nodestro_runtime.catalog import model_capabilities, models
    from nodestro_runtime.providers import CAPABILITY_NAMES

    found = {info.id: info for info in models()}
    claude = found[CLAUDE].capabilities
    assert claude is not None and set(claude) == set(CAPABILITY_NAMES)
    assert claude["structured_output"] == "supported" and claude["forced_tool_choice"] == "unsupported"
    assert found["bedrock:eu.amazon.nova-pro-v1:0"].capabilities["structured_output"] == "unsupported"  # type: ignore[index]
    assert all(info.capabilities is None for info in found.values() if info.provider != "bedrock")
    typed = model_capabilities("bedrock:anthropic.claude-sonnet-4-5-20250929-v1:0")
    assert typed is not None and typed["tools"] == "supported" and typed["reasoning"] == "unknown"
    assert model_capabilities("openai:gpt-5.5") is None and model_capabilities("not a: model") is None


def test_price_entries_list_their_regions(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "ws", integrations=[])
    try:
        workspace.pricing.set_override(_regional_price())
        entry = workspace.pricing.entry("bedrock", "eu.anthropic.claude-sonnet-5-5")
        assert entry is not None and entry.regions == ["eu-central-1"]
        anthropic = workspace.pricing.entry("anthropic", "claude-sonnet-5-5")
        assert anthropic is not None and anthropic.regions is None
    finally:
        workspace.close()
