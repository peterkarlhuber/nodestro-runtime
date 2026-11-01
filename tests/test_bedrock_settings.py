"""Amazon Bedrock settings, status, verification and tenant credentials (all offline; see ``bedrock_support``)."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
from typing import Any

from bedrock_support import (
    ACCOUNT,
    FAKE_AWS,
    ROLE_A,
    ROLE_B,
    STS_KEY,
    FakeSession,
    StsFactory,
    iter_strings,
    no_network,  # noqa: F401  (autouse)
    stubbed,
    tenant_storage,
)
from botocore.exceptions import EndpointConnectionError, NoCredentialsError
import pytest

from nodestro_runtime import bedrock as bedrock_module
from nodestro_runtime.bedrock import (
    BedrockOperatorConfig,
    BedrockServerPolicy,
    BedrockSettings,
    redact_identity,
    role_session_name,
)
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import (
    PROVIDERS,
    Connection,
    MissingApiKeyError,
    bedrock_settings,
    provider_status,
)
from nodestro_runtime.schemas import SettingsIn
from nodestro_runtime.workspace import Workspace


def bedrock_change(**values: Any) -> SettingsIn:
    return SettingsIn.model_validate({"providers": {"bedrock": values}})


def status(workspace: Workspace) -> Any:
    return next(item for item in workspace.settings().providers if item.id == "bedrock")


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[Workspace]:
    workspace = Workspace(tmp_path / "desk", integrations=[])
    yield workspace
    workspace.close()


@pytest.fixture
def opened() -> Iterator[list[Workspace]]:
    found: list[Workspace] = []
    yield found
    for workspace in found:
        workspace.close()


def tenant(
    tmp_path: Path, opened: list[Workspace], name: str, policy: BedrockServerPolicy | None, **kwargs: Any
) -> Workspace:
    workspace = Workspace(
        tmp_path / name, integrations=[], storage=tenant_storage(tmp_path / "db", name, policy), **kwargs
    )
    opened.append(workspace)
    return workspace


# ---- provider info and status ------------------------------------------------------------


def test_bedrock_is_a_provider_without_key_or_endpoint() -> None:
    info = PROVIDERS["bedrock"]
    assert (info.label, info.auth, info.needs_key, info.base_url, info.env_vars) == (
        "Amazon Bedrock (AWS)",
        "aws",
        False,
        False,
        [],
    )
    assert not info.declare_capabilities
    # The other providers keep their behaviour; their kind is derived.
    assert {name: PROVIDERS[name].auth for name in ("anthropic", "openai", "gemini")} == dict.fromkeys(
        ("anthropic", "openai", "gemini"), "api_key"
    )
    assert {PROVIDERS[name].auth for name in ("compatible", "ollama", "vllm")} == {"endpoint"}


def test_unconfigured_status_and_connection(desktop: Workspace) -> None:
    found = status(desktop)
    assert found.configured is False and found.key_source == "not_needed"
    assert found.auth == "default_chain" and found.auth_methods == ["default_chain", "profile"]
    assert found.verification.state == "not_configured" and found.operator_role is None
    with pytest.raises(MissingApiKeyError) as caught:
        desktop.connection("bedrock")
    assert caught.value.code == "provider.not_configured"
    assert "API key" not in str(caught.value) and "AWS region" in str(caught.value)
    # Other providers' status has no Bedrock fields.
    other = next(item for item in desktop.settings().providers if item.id == "openai")
    assert other.auth is None and other.region is None and other.verification is None


def test_desktop_default_chain_and_profile(desktop: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    desktop.update_settings(bedrock_change(region="eu-central-1"))
    found = status(desktop)
    assert found.configured and found.region == "eu-central-1" and found.auth == "default_chain"
    assert found.verification.state == "configured"
    built: list[BedrockSettings] = []
    monkeypatch.setattr(bedrock_module, "desktop_session", lambda settings: built.append(settings) or "session")
    connection = desktop.connection("bedrock")
    assert built == []  # the session is made per set of options, not when the connection is looked up
    options = connection.options()
    assert options.session == "session" and options.region_name == "eu-central-1" and options.profile_name is None
    desktop.update_settings(bedrock_change(auth="profile"))
    assert status(desktop).configured is False  # a profile needs its name
    desktop.update_settings(bedrock_change(profile_name="work-sso"))
    found = status(desktop)
    assert found.configured and (found.auth, found.profile_name) == ("profile", "work-sso")
    desktop.connection("bedrock").options()
    assert built[-1].profile_name == "work-sso" and built[-1].auth == "profile"


def test_desktop_session_uses_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    made: list[dict[str, Any]] = []
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: made.append(kwargs) or "s")
    bedrock_module.desktop_session(BedrockSettings(region="eu-west-1", auth="profile", profile_name="p1"))
    bedrock_module.desktop_session(BedrockSettings(region="eu-west-1", profile_name="ignored"))
    assert made == [
        {"profile_name": "p1", "region_name": "eu-west-1"},
        {"profile_name": None, "region_name": "eu-west-1"},
    ]


def test_a_missing_profile_has_a_stable_code(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(NodestroError) as caught:
        bedrock_module.desktop_session(BedrockSettings(region="eu-west-1", auth="profile", profile_name="nope"))
    assert caught.value.code == "provider.aws_profile_not_found"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"region": "Frankfurt"}, "provider.region_invalid"),
        ({"region": "eu-central-1; rm"}, "provider.region_invalid"),
        ({"auth": "operator_role"}, "provider.auth_not_allowed"),
        ({"auth": "keys"}, "provider.auth_invalid"),
        ({"profile_name": "../etc/passwd"}, "provider.profile_name_invalid"),
        ({"api_key": "AKIA..."}, "provider.field_not_supported"),
        ({"base_url": "https://bedrock-runtime.eu-central-1.amazonaws.com"}, "provider.field_not_supported"),
        ({"capabilities": {"tools": "supported"}}, "provider.field_not_supported"),
        ({"data_residency": "eu"}, "provider.data_residency_invalid"),
        ({"region": "us-east-1", "data_residency": "eu_only"}, "provider.data_residency_region"),
    ],
)
def test_invalid_settings_are_refused(desktop: Workspace, change: dict[str, Any], code: str) -> None:
    with pytest.raises(NodestroError) as caught:
        desktop.update_settings(bedrock_change(**change))
    assert caught.value.code == code
    assert "providers" not in (desktop.store.get_raw("settings", "app") or {})


@pytest.mark.parametrize("field", ["region", "auth", "profile_name", "data_residency"])
def test_bedrock_fields_are_refused_for_other_providers(desktop: Workspace, field: str) -> None:
    with pytest.raises(NodestroError) as caught:
        desktop.update_settings(SettingsIn.model_validate({"providers": {"openai": {field: "eu_only"}}}))
    assert caught.value.code == "provider.field_not_supported" and caught.value.params["field"] == field


def test_other_providers_settings_are_unchanged(desktop: Workspace) -> None:
    desktop.update_settings(
        SettingsIn.model_validate(
            {"providers": {"openai": {"api_key": "sk-test"}, "ollama": {"base_url": "http://localhost:11434/v1"}}}
        )
    )
    shown = {item.id: item for item in desktop.settings().providers}
    assert shown["openai"].configured and shown["openai"].key_source == "settings"
    assert shown["ollama"].configured and shown["ollama"].base_url == "http://localhost:11434/v1"
    assert desktop.connection("openai").api_key == "sk-test"


def test_eu_only_and_clearing(desktop: Workspace) -> None:
    desktop.update_settings(bedrock_change(region="eu-west-3", data_residency="eu_only"))
    found = status(desktop)
    assert found.data_residency == "eu_only" and found.data_residency_forced is False
    options = Connection(provider="bedrock", bedrock=desktop.connection("bedrock").bedrock, client=object()).options()
    assert options.data_residency == "eu_only" and options.region_name == "eu-west-3"
    desktop.update_settings(bedrock_change(data_residency=""))
    assert status(desktop).data_residency is None
    desktop.update_settings(bedrock_change(region=""))
    assert status(desktop).configured is False


def test_local_container_offers_the_default_chain_only() -> None:
    settings, problem = bedrock_settings(
        {"region": "eu-central-1", "auth": "profile", "profile_name": "p"},
        server=False,
        policy=None,
        local_container=True,
    )
    assert problem == "provider.auth_not_allowed" and settings.profile_name is None
    shown = provider_status("bedrock", {"region": "eu-central-1"}, None, server=False, local_container=True)
    assert shown.configured and shown.auth_methods == ["default_chain"]


# ---- verification ------------------------------------------------------------------------


def _verification_session(sts_error: Any = None, control_error: Any = None) -> tuple[FakeSession, Any, Any]:
    sts, sts_stub = stubbed("sts")
    control, control_stub = stubbed("bedrock")
    if sts_error is None:
        sts_stub.add_response(
            "get_caller_identity",
            {"UserId": "AIDAEXAMPLE", "Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/alice"},
            {},
        )
    else:
        sts_stub.add_client_error("get_caller_identity", **sts_error)
    if control_error is None:
        control_stub.add_response("list_foundation_models", {"modelSummaries": []}, {"byProvider": "anthropic"})
    else:
        control_stub.add_client_error("list_foundation_models", **control_error)
    return FakeSession({"sts": sts, "bedrock": control}), sts_stub, control_stub


def test_verification_with_free_calls_only(desktop: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    desktop.update_settings(bedrock_change(region="eu-central-1"))
    session, sts_stub, control_stub = _verification_session()
    monkeypatch.setattr(bedrock_module, "desktop_session", lambda settings: session)
    shown = desktop.verify_provider("bedrock")
    assert shown.verification.state == "verified" and shown.verification.code is None
    assert shown.verification.checked_at
    sts_stub.assert_no_pending_responses()
    control_stub.assert_no_pending_responses()
    assert [service for service, _ in session.calls] == ["sts", "bedrock"]  # never bedrock-runtime
    sts_kwargs = session.calls[0][1]
    assert sts_kwargs["endpoint_url"] == "https://sts.eu-central-1.amazonaws.com"
    for _service, kwargs in session.calls:
        config = kwargs["config"]
        assert config.connect_timeout <= 5 and config.read_timeout <= 10
        assert config.retries["total_max_attempts"] == 2
    # Stored, without the identity; shown on the next status too.
    stored = json.dumps(desktop.store.get_raw("settings", "app"))
    assert ACCOUNT not in stored and "alice" not in stored
    assert status(desktop).verification.state == "verified"
    # Checked again only after a pause.
    with pytest.raises(NodestroError) as caught:
        desktop.verify_provider("bedrock")
    assert caught.value.code == "provider.verify_rate_limited"
    # A changed configuration has to be checked again.
    desktop.update_settings(bedrock_change(region="eu-west-1"))
    assert status(desktop).verification.state == "configured"


@pytest.mark.parametrize(
    ("sts_error", "control_error", "code"),
    [
        (
            {"service_error_code": "InvalidClientTokenId", "service_message": "bad token"},
            None,
            "provider.aws_credentials_invalid",
        ),
        (
            {"service_error_code": "ExpiredToken", "service_message": "expired"},
            None,
            "provider.aws_credentials_invalid",
        ),
        (
            None,
            {
                "service_error_code": "AccessDeniedException",
                "service_message": f"User: arn:aws:sts::{ACCOUNT}:assumed-role/x/y is not authorized to perform "
                "bedrock:ListFoundationModels",
                "http_status_code": 403,
            },
            "provider.aws_access_denied",
        ),
    ],
)
def test_verification_failures_have_stable_codes(
    desktop: Workspace, monkeypatch: pytest.MonkeyPatch, sts_error: Any, control_error: Any, code: str
) -> None:
    desktop.update_settings(bedrock_change(region="eu-central-1"))
    session, _sts, _control = _verification_session(sts_error, control_error)
    monkeypatch.setattr(bedrock_module, "desktop_session", lambda settings: session)
    shown = desktop.verify_provider("bedrock")
    assert shown.verification.state == "failed" and shown.verification.code == code
    text = json.dumps([shown.model_dump(), desktop.store.get_raw("settings", "app")])
    assert ACCOUNT not in text and "assumed-role" not in text


class _Raising:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def get_caller_identity(self) -> Any:
        raise self.error


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (NoCredentialsError(), "provider.aws_credentials_missing"),
        (EndpointConnectionError(endpoint_url="https://sts.eu-central-1.amazonaws.com"), "provider.aws_unreachable"),
    ],
)
def test_missing_credentials_and_unreachable(
    desktop: Workspace, monkeypatch: pytest.MonkeyPatch, error: BaseException, code: str
) -> None:
    desktop.update_settings(bedrock_change(region="eu-central-1"))
    session = FakeSession({"sts": _Raising(error)})
    monkeypatch.setattr(bedrock_module, "desktop_session", lambda settings: session)
    assert desktop.verify_provider("bedrock").verification.code == code


def test_verification_checks_an_inference_profile_for_eu_only(
    desktop: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop.update_settings(bedrock_change(region="eu-central-1", data_residency="eu_only"))
    sts, sts_stub = stubbed("sts")
    sts_stub.add_response(
        "get_caller_identity", {"UserId": "u", "Account": ACCOUNT, "Arn": "arn:aws:iam::1:user/u"}, {}
    )
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
            "models": [
                {"modelArn": "arn:aws:bedrock:eu-central-1::foundation-model/anthropic.claude-sonnet-5-5"},
                {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-5-5"},
            ],
        },
        {"inferenceProfileIdentifier": model},
    )
    monkeypatch.setattr(
        bedrock_module, "desktop_session", lambda settings: FakeSession({"sts": sts, "bedrock": control})
    )
    shown = desktop.verify_provider("bedrock", model=model)
    assert shown.verification.state == "failed" and shown.verification.code == "provider.data_residency_violation"


def test_eu_only_refuses_a_non_eu_region_before_any_call() -> None:
    session = FakeSession({})
    result = bedrock_module.verify(session, BedrockSettings(region="us-east-1", data_residency="eu_only"))
    assert result["code"] == "provider.data_residency_region" and session.calls == []


# ---- server tenants ----------------------------------------------------------------------


def test_a_tenant_without_an_operator_role_cannot_use_bedrock(
    tmp_path: Path, opened: list[Workspace], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAAMBIENTSERVERKEY1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    for policy in (None, BedrockServerPolicy(tenant_id="alpha")):
        workspace = tenant(tmp_path, opened, f"alpha{len(opened)}" if policy is None else "alpha", policy)
        found = status(workspace)
        assert found.configured is False and found.operator_role is False
        assert found.auth == "operator_role" and found.auth_methods == ["operator_role"]
        assert found.verification.code == "provider.bedrock_not_available"
        with pytest.raises(NodestroError) as caught:
            workspace.update_settings(bedrock_change(region="eu-central-1"))
        assert caught.value.code == "provider.bedrock_not_available"
        with pytest.raises(MissingApiKeyError) as missing:
            workspace.connection("bedrock")
        assert missing.value.code == "provider.bedrock_not_available"


def test_a_policy_of_another_tenant_is_never_used(tmp_path: Path, opened: list[Workspace]) -> None:
    workspace = tenant(tmp_path, opened, "alpha", BedrockServerPolicy(tenant_id="beta", role=ROLE_B))
    workspace.store.put_raw("settings", "app", {"providers": {"bedrock": {"region": "eu-central-1"}}})
    assert status(workspace).operator_role is False
    with pytest.raises(MissingApiKeyError) as caught:
        workspace.connection("bedrock")
    assert caught.value.code == "provider.bedrock_not_available"


def test_tenant_settings_and_status_never_show_the_role(tmp_path: Path, opened: list[Workspace]) -> None:
    factory = StsFactory()
    workspace = tenant(
        tmp_path, opened, "alpha", BedrockServerPolicy(tenant_id="alpha", role=ROLE_A, sts_client_factory=factory)
    )
    for change, code in (
        ({"auth": "default_chain"}, "provider.auth_not_allowed"),
        ({"auth": "profile"}, "provider.auth_not_allowed"),
        ({"profile_name": "operator"}, "provider.field_not_supported"),
    ):
        with pytest.raises(NodestroError) as caught:
            workspace.update_settings(bedrock_change(**change))
        assert caught.value.code == code
    workspace.update_settings(bedrock_change(region="eu-central-1", auth="operator_role"))
    found = status(workspace)
    assert found.configured and found.operator_role is True and found.auth == "operator_role"
    text = json.dumps([workspace.settings().model_dump(), workspace.store.get_raw("settings", "app")])
    for secret in (ROLE_A.role_arn, ROLE_A.external_id, ACCOUNT):
        assert secret not in text
    connection = workspace.connection("bedrock")
    for shown in (repr(connection), repr(connection.options()), repr(BedrockServerPolicy("alpha", ROLE_A))):
        assert ROLE_A.role_arn not in shown and ROLE_A.external_id not in shown and ACCOUNT not in shown
    assert factory.regions == []  # nothing was assumed yet


def test_forced_eu_only_cannot_be_relaxed(tmp_path: Path, opened: list[Workspace]) -> None:
    policy = BedrockServerPolicy(
        tenant_id="alpha", role=ROLE_A, data_residency="eu_only", sts_client_factory=StsFactory()
    )
    workspace = tenant(tmp_path, opened, "alpha", policy)
    workspace.update_settings(bedrock_change(region="eu-north-1"))
    found = status(workspace)
    assert found.data_residency == "eu_only" and found.data_residency_forced is True
    assert "eu-central-1" in (found.allowed_regions or []) and "us-east-1" not in (found.allowed_regions or [])
    with pytest.raises(NodestroError) as caught:
        workspace.update_settings(bedrock_change(data_residency=""))
    assert caught.value.code == "provider.data_residency_forced"
    with pytest.raises(NodestroError) as caught:
        workspace.update_settings(bedrock_change(region="us-east-1"))
    assert caught.value.code == "provider.data_residency_region"
    # Even a stored (e.g. imported) non-EU region is refused before any call.
    workspace.store.put_raw("settings", "app", {"providers": {"bedrock": {"region": "us-west-2"}}})
    with pytest.raises(NodestroError) as caught:
        workspace.connection("bedrock")
    assert caught.value.code == "provider.data_residency_region"
    workspace.store.put_raw("settings", "app", {"providers": {"bedrock": {"region": "eu-north-1"}}})
    assert workspace.connection("bedrock").options().data_residency == "eu_only"


def test_allowed_regions_of_the_operator(tmp_path: Path, opened: list[Workspace]) -> None:
    policy = BedrockServerPolicy(tenant_id="alpha", role=ROLE_A, allowed_regions=("eu-central-1",))
    workspace = tenant(tmp_path, opened, "alpha", policy)
    with pytest.raises(NodestroError) as caught:
        workspace.update_settings(bedrock_change(region="eu-west-1"))
    assert caught.value.code == "provider.region_not_allowed"
    workspace.update_settings(bedrock_change(region="eu-central-1"))
    assert workspace.connection("bedrock").options().allowed_regions == ("eu-central-1",)


def test_tenant_credentials_come_only_from_the_assumed_role(
    tmp_path: Path, opened: list[Workspace], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAAMBIENTSERVERKEY1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    factory = StsFactory()
    workspace = tenant(
        tmp_path, opened, "alpha", BedrockServerPolicy(tenant_id="alpha", role=ROLE_A, sts_client_factory=factory)
    )
    workspace.update_settings(bedrock_change(region="eu-central-1"))
    options = workspace.connection("bedrock").options()
    assert options.session is not None and options.region_name == "eu-central-1" and options.profile_name is None
    assert factory.regions == []  # deferred: nothing is assumed until a request needs credentials
    factory.expect(ROLE_A, "alpha")
    credentials = options.session.get_credentials().get_frozen_credentials()
    assert credentials.access_key == STS_KEY  # never AWS_ACCESS_KEY_ID of the server
    assert factory.regions == ["eu-central-1"]
    factory.stubbers[0].assert_no_pending_responses()
    # A second agent of the same tenant gets its own session with the same (still valid) credentials.
    other = workspace.connection("bedrock").options().session
    assert other is not options.session
    assert other.get_credentials().get_frozen_credentials().access_key == STS_KEY
    assert factory.regions == ["eu-central-1"]


def test_two_tenants_never_share_roles_or_sessions(tmp_path: Path, opened: list[Workspace]) -> None:
    config = BedrockOperatorConfig(roles={"alpha": ROLE_A, "beta": ROLE_B})
    alpha_sts, beta_sts = StsFactory(), StsFactory()
    alpha_policy = config.for_tenant("alpha")
    beta_policy = config.for_tenant("beta")
    assert alpha_policy.role is ROLE_A and beta_policy.role is ROLE_B
    assert config.for_tenant("gamma").role is None
    from dataclasses import replace

    alpha = tenant(tmp_path, opened, "alpha", replace(alpha_policy, sts_client_factory=alpha_sts))
    beta = tenant(tmp_path, opened, "beta", replace(beta_policy, sts_client_factory=beta_sts))
    for workspace in (alpha, beta):
        workspace.update_settings(bedrock_change(region="eu-central-1"))
    alpha_sts.expect(ROLE_A, "alpha")
    beta_sts.expect(ROLE_B, "beta", response=None)
    a = alpha.connection("bedrock").options().session.get_credentials().get_frozen_credentials()
    b = beta.connection("bedrock").options().session.get_credentials().get_frozen_credentials()
    assert a.access_key == STS_KEY and b.access_key == STS_KEY  # same fake answer, but each assumed its own role
    for factory in (alpha_sts, beta_sts):
        factory.stubbers[0].assert_no_pending_responses()  # expected_params checked RoleArn/ExternalId/session name


def test_a_failed_role_assumption_names_no_role(tmp_path: Path, opened: list[Workspace]) -> None:
    factory = StsFactory()
    factory.fail(
        "AccessDenied",
        f"User: arn:aws:iam::{ACCOUNT}:user/operator is not authorized to perform: sts:AssumeRole on resource: "
        f"{ROLE_A.role_arn}",
    )
    workspace = tenant(
        tmp_path, opened, "alpha", BedrockServerPolicy(tenant_id="alpha", role=ROLE_A, sts_client_factory=factory)
    )
    workspace.update_settings(bedrock_change(region="eu-central-1"))
    session = workspace.connection("bedrock").options().session
    with pytest.raises(NodestroError) as caught:
        session.get_credentials().get_frozen_credentials()
    assert caught.value.code == "provider.aws_access_denied"
    assert ACCOUNT not in str(caught.value) and "arn:" not in str(caught.value)


def test_server_options_without_a_session_are_refused() -> None:
    connection = Connection(provider="bedrock", bedrock=BedrockSettings(region="eu-central-1"), require_session=True)
    with pytest.raises(NodestroError) as caught:
        connection.options()
    assert caught.value.code == "provider.bedrock_not_available"
    # An injected client (tests, scripted e2e) needs no session.
    assert Connection(provider="bedrock", client=object(), require_session=True).options().session is None


def test_verification_of_a_tenant_assumes_its_role(
    tmp_path: Path, opened: list[Workspace], monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = StsFactory()
    workspace = tenant(
        tmp_path, opened, "alpha", BedrockServerPolicy(tenant_id="alpha", role=ROLE_A, sts_client_factory=factory)
    )
    workspace.update_settings(bedrock_change(region="eu-central-1"))
    seen: list[Any] = []

    def verify(session: Any, settings: BedrockSettings, model: str | None = None) -> dict[str, Any]:
        factory.expect(ROLE_A, "alpha")
        seen.append(session.get_credentials().get_frozen_credentials().access_key)
        return {"state": "verified", "checked_at": "2026-11-01T00:00:00+00:00", "code": None}

    monkeypatch.setattr(bedrock_module, "verify", verify)
    assert workspace.verify_provider("bedrock").verification.state == "verified"
    assert seen == [STS_KEY]


# ---- operator configuration --------------------------------------------------------------


def test_operator_config_from_env(tmp_path: Path) -> None:
    roles = {"alpha": {"role_arn": ROLE_A.role_arn, "external_id": ROLE_A.external_id}}
    config = BedrockOperatorConfig.from_env(
        {
            "NODESTRO_BEDROCK_TENANT_ROLES": json.dumps(roles),
            "NODESTRO_BEDROCK_DATA_RESIDENCY": "eu_only",
            "NODESTRO_BEDROCK_ALLOWED_REGIONS": "eu-central-1, eu-west-1",
        }
    )
    assert config.roles["alpha"].role_arn == ROLE_A.role_arn
    assert config.data_residency == "eu_only" and config.allowed_regions == ("eu-central-1", "eu-west-1")
    assert ROLE_A.role_arn not in repr(config) and ROLE_A.external_id not in repr(config.for_tenant("alpha"))
    path = tmp_path / "roles.json"
    path.write_text(json.dumps(roles), encoding="utf-8")
    assert BedrockOperatorConfig.from_env({"NODESTRO_BEDROCK_TENANT_ROLES_FILE": str(path)}).roles.keys() == {"alpha"}
    empty = BedrockOperatorConfig.from_env({})
    assert empty.roles == {} and empty.for_tenant("alpha").role is None


@pytest.mark.parametrize(
    ("environ", "field"),
    [
        (
            {"NODESTRO_BEDROCK_TENANT_ROLES": "{", "NODESTRO_BEDROCK_TENANT_ROLES_FILE": "/x"},
            "NODESTRO_BEDROCK_TENANT_ROLES",
        ),
        ({"NODESTRO_BEDROCK_TENANT_ROLES": "not json"}, "NODESTRO_BEDROCK_TENANT_ROLES"),
        (
            {"NODESTRO_BEDROCK_TENANT_ROLES": '{"a": {"role_arn": "arn:aws:s3:::bucket", "external_id": "xx"}}'},
            "NODESTRO_BEDROCK_TENANT_ROLES",
        ),
        (
            {"NODESTRO_BEDROCK_TENANT_ROLES": '{"a": {"role_arn": "arn:aws:iam::123456789012:role/r"}}'},
            "NODESTRO_BEDROCK_TENANT_ROLES",
        ),
        ({"NODESTRO_BEDROCK_TENANT_ROLES_FILE": "/nonexistent/roles.json"}, "NODESTRO_BEDROCK_TENANT_ROLES_FILE"),
        ({"NODESTRO_BEDROCK_DATA_RESIDENCY": "eu"}, "NODESTRO_BEDROCK_DATA_RESIDENCY"),
        ({"NODESTRO_BEDROCK_ALLOWED_REGIONS": "eu-central-1,mars"}, "NODESTRO_BEDROCK_ALLOWED_REGIONS"),
        (
            {
                "NODESTRO_BEDROCK_DATA_RESIDENCY": "eu_only",
                "NODESTRO_BEDROCK_ALLOWED_REGIONS": "eu-central-1,us-east-1",
            },
            "NODESTRO_BEDROCK_ALLOWED_REGIONS",
        ),
    ],
)
def test_invalid_operator_config(environ: dict[str, str], field: str) -> None:
    with pytest.raises(NodestroError) as caught:
        BedrockOperatorConfig.from_env(environ)
    assert caught.value.code == "profile.invalid" and caught.value.params["field"] == field
    assert "123456789012" not in str(caught.value.params)


def test_role_session_names_and_identity_redaction() -> None:
    assert role_session_name("alpha") == "nodestro-alpha"
    long = role_session_name("t" * 200)
    assert len(long) <= 64 and long.startswith("nodestro-")
    assert role_session_name("a b/c") == "nodestro-a-b-c"
    text = f"User: arn:aws:sts::{ACCOUNT}:assumed-role/role-x/nodestro-alpha is not authorized (account {ACCOUNT})"
    cleaned = redact_identity(text)
    assert ACCOUNT not in cleaned and "role-x" not in cleaned
    assert all(value not in repr(ROLE_A) for value in (ROLE_A.role_arn, ROLE_A.external_id))
    assert FAKE_AWS["aws_access_key_id"] not in "".join(iter_strings({"a": [repr(BedrockServerPolicy("t", ROLE_A))]}))
