"""The server's configuration (refused when incomplete or unsafe) and tenant secrets encrypted at rest."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.server_settings import ENCRYPTED_PREFIX, SecretBox, ServerSettings, is_encrypted

pytest.importorskip("cryptography")

KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
COMPLETE = {
    "NODESTRO_DATABASE_URL": "host=db user=nodestro dbname=nodestro",
    "NODESTRO_PUBLIC_ORIGIN": "https://nodestro.example.com/",
    "NODESTRO_OIDC_ISSUER": "https://login.example.com/realms/acme",
    "NODESTRO_OIDC_CLIENT_ID": "nodestro",
    "NODESTRO_OIDC_CLIENT_SECRET": "client-secret",
    "NODESTRO_SECRET_KEY": KEY,
}


def _refused(environ: dict[str, str]) -> list[str]:
    with pytest.raises(NodestroError) as error:
        ServerSettings.from_env(environ)
    assert error.value.code == "profile.invalid"
    return [item["field"] for item in error.value.params["problems"]]


def test_complete_settings_are_read_and_secrets_stay_out_of_repr() -> None:
    settings = ServerSettings.from_env(COMPLETE)
    assert settings.public_origin == "https://nodestro.example.com"
    assert settings.redirect_uri == "https://nodestro.example.com/api/auth/callback"
    assert settings.oidc_issuer == "https://login.example.com/realms/acme"
    assert settings.secure_cookies
    assert len(settings.secret_key) == 32
    shown = repr(settings)
    assert "client-secret" not in shown and "dbname=nodestro" not in shown and KEY not in shown


def test_every_missing_setting_is_listed_at_once() -> None:
    fields = _refused({})
    assert fields == [
        "NODESTRO_DATABASE_URL",
        "NODESTRO_PUBLIC_ORIGIN",
        "NODESTRO_OIDC_ISSUER",
        "NODESTRO_OIDC_CLIENT_ID",
        "NODESTRO_OIDC_CLIENT_SECRET",
        "NODESTRO_SECRET_KEY",
    ]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NODESTRO_PUBLIC_ORIGIN", "http://nodestro.example.com"),  # plain http outside loopback
        ("NODESTRO_PUBLIC_ORIGIN", "https://nodestro.example.com/studio"),  # not an origin
        ("NODESTRO_PUBLIC_ORIGIN", "https://user:pw@nodestro.example.com"),
        ("NODESTRO_PUBLIC_ORIGIN", "ftp://nodestro.example.com"),
        ("NODESTRO_OIDC_ISSUER", "http://login.example.com"),
        ("NODESTRO_SECRET_KEY", base64.b64encode(b"short").decode()),
        ("NODESTRO_SECRET_KEY", "not base64 at all!"),
    ],
)
def test_unsafe_values_are_refused(name: str, value: str) -> None:
    assert _refused({**COMPLETE, name: value}) == [name]


def test_loopback_http_is_allowed_for_development() -> None:
    settings = ServerSettings.from_env(
        {**COMPLETE, "NODESTRO_PUBLIC_ORIGIN": "http://127.0.0.1:8080", "NODESTRO_OIDC_ISSUER": "http://localhost:5556"}
    )
    assert settings.public_origin == "http://127.0.0.1:8080" and not settings.secure_cookies


def test_secrets_come_from_files_but_not_from_both_sources(tmp_path: Path) -> None:
    secret = tmp_path / "client-secret"
    secret.write_text("from-a-file\n")
    environ = {**COMPLETE, "NODESTRO_OIDC_CLIENT_SECRET": "", "NODESTRO_OIDC_CLIENT_SECRET_FILE": str(secret)}
    assert ServerSettings.from_env(environ).oidc_client_secret == "from-a-file"
    assert _refused({**COMPLETE, "NODESTRO_OIDC_CLIENT_SECRET_FILE": str(secret)}) == ["NODESTRO_OIDC_CLIENT_SECRET"]
    missing = {**environ, "NODESTRO_OIDC_CLIENT_SECRET_FILE": str(tmp_path / "nope")}
    assert _refused(missing) == ["NODESTRO_OIDC_CLIENT_SECRET_FILE"]


def test_secrets_are_bound_to_tenant_and_field() -> None:
    box = SecretBox(b"k" * 32)
    sealed = box.encrypt("acme", "providers.anthropic.api_key", "sk-ant-secret")
    assert is_encrypted(sealed) and "sk-ant-secret" not in sealed
    assert sealed != box.encrypt("acme", "providers.anthropic.api_key", "sk-ant-secret")  # random nonce
    assert box.decrypt("acme", "providers.anthropic.api_key", sealed) == "sk-ant-secret"
    for tenant, name in [("globex", "providers.anthropic.api_key"), ("acme", "providers.openai.api_key")]:
        with pytest.raises(NodestroError) as error:
            box.decrypt(tenant, name, sealed)
        assert error.value.code == "secrets.unreadable"
    with pytest.raises(NodestroError):
        SecretBox(b"x" * 32).decrypt("acme", "providers.anthropic.api_key", sealed)  # another server key
    with pytest.raises(NodestroError):
        box.decrypt("acme", "providers.anthropic.api_key", "sk-plain")
    with pytest.raises(NodestroError):
        box.decrypt("acme", "providers.anthropic.api_key", ENCRYPTED_PREFIX + "AAAA")
    assert "k" * 32 not in repr(box)
