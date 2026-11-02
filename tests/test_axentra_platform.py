"""Axentra platform services in the server profile: settings, the keyring secret box, re-encryption and the schema.

The PostgreSQL cases need ``NODESTRO_TEST_POSTGRES``; everything needs the ``axentra`` package (Python 3.11+), which
the server profile installs (``nodestro-runtime[postgres]``).
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
import json
from pathlib import Path
import secrets
from typing import Any

import postgres_support
import pytest

from nodestro_runtime import axentra_platform
from nodestro_runtime.axentra_platform import (
    AxentraConfig,
    KeyringSecretBox,
    keyring_secret_box,
    reencrypt_tenant_secrets,
    secret_context,
)
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.server_settings import ENCRYPTED_PREFIX, SecretBox, ServerSettings, is_encrypted

pytest.importorskip("axentra", reason="Axentra needs Python 3.11+ and nodestro-runtime[postgres]")


def _key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _env(**extra: str) -> dict[str, str]:
    return {
        "NODESTRO_DATABASE_URL": "postgresql://nodestro@db/nodestro",
        "NODESTRO_PUBLIC_ORIGIN": "https://studio.example",
        "NODESTRO_OIDC_ISSUER": "https://id.example",
        "NODESTRO_OIDC_CLIENT_ID": "studio",
        "NODESTRO_OIDC_CLIENT_SECRET": "s3cret",
        "NODESTRO_SECRET_KEY": base64.b64encode(b"k" * 32).decode(),
        **extra,
    }


def _problems(environ: dict[str, str]) -> list[str]:
    with pytest.raises(NodestroError) as caught:
        ServerSettings.from_env(environ)
    return [item["field"] for item in caught.value.params["problems"]]


# ---- settings -----------------------------------------------------------------------------------------


def test_axentra_is_off_by_default_and_the_v1_box_is_unchanged() -> None:
    settings = ServerSettings.from_env(_env())
    assert settings.axentra == AxentraConfig()
    assert isinstance(settings.secret_box(), SecretBox)


def test_switching_axentra_on_needs_a_valid_keyring() -> None:
    assert _problems(_env(NODESTRO_AXENTRA="on")) == ["NODESTRO_AXENTRA_KEYRING"]
    assert _problems(_env(NODESTRO_AXENTRA="on", NODESTRO_AXENTRA_KEYRING="nokid")) == ["NODESTRO_AXENTRA_KEYRING"]
    assert _problems(_env(NODESTRO_AXENTRA="maybe")) == ["NODESTRO_AXENTRA"]
    # The axs1 format is Axentra's keyring: without Axentra it is refused instead of silently writing v1.
    assert _problems(_env(NODESTRO_SECRETS_FORMAT="axs1")) == ["NODESTRO_SECRETS_FORMAT"]
    assert _problems(_env(NODESTRO_SECRETS_FORMAT="v2")) == ["NODESTRO_SECRETS_FORMAT"]


def test_the_keyring_comes_from_the_environment_or_a_file_and_stays_out_of_repr(tmp_path: Path) -> None:
    ring = f"k1:{_key()}"
    settings = ServerSettings.from_env(_env(NODESTRO_AXENTRA="on", NODESTRO_AXENTRA_KEYRING=ring))
    assert settings.axentra.enabled and settings.axentra.keyring_ref == "env:NODESTRO_AXENTRA_KEYRING"
    assert ring not in repr(settings) and ring.split(":")[1] not in repr(settings.axentra)
    path = tmp_path / "keyring"
    path.write_text(ring + "\n")
    from_file = ServerSettings.from_env(_env(NODESTRO_AXENTRA="on", NODESTRO_AXENTRA_KEYRING_FILE=str(path)))
    assert from_file.axentra.keyring == ring and from_file.axentra.keyring_ref == f"file:{path}"
    both = _env(NODESTRO_AXENTRA="on", NODESTRO_AXENTRA_KEYRING=ring, NODESTRO_AXENTRA_KEYRING_FILE=str(path))
    assert _problems(both) == ["NODESTRO_AXENTRA_KEYRING"]


def test_axentra_settings_follow_nodestros_server(tmp_path: Path) -> None:
    config_file = tmp_path / "axentra.toml"
    config_file.write_text('[product]\nslug = "other"\n[security]\nsession_idle_minutes = 30\n')
    config = AxentraConfig(
        enabled=True, keyring=f"k1:{_key()}", keyring_ref="env:RING", config_file=str(config_file)
    )
    built = axentra_platform.build_settings(
        config, database_url="postgresql://u@db/n", public_origin="https://studio.example", schema="tenant_data"
    )
    assert built.mode == "single" and built.product.slug == "nodestro"  # Nodestro's values win over the file
    assert built.security.session_idle_minutes == 30  # Axentra's own knobs come from the file
    assert built.is_production and built.security.master_keys_ref is None and built.security.master_keys is None
    assert "search_path=tenant_data" in built.database.url
    assert set(built.modules) == set(axentra_platform.MODULES)
    local = axentra_platform.build_settings(config, database_url="dbname=n", public_origin="http://localhost:8000")
    assert not local.is_production


# ---- the secret box --------------------------------------------------------------------------------------


def _box(ring: str, *, write: str = "axs1", legacy: SecretBox | None = None) -> KeyringSecretBox:
    return keyring_secret_box(
        AxentraConfig(enabled=True, keyring=ring, secrets_format=write),  # type: ignore[arg-type]
        legacy if legacy is not None else SecretBox(b"L" * 32),
    )


def test_new_secrets_use_the_keyring_and_old_ones_stay_readable() -> None:
    legacy = SecretBox(b"L" * 32)
    old = legacy.encrypt("alpha", "providers.openai.api_key", "sk-old")
    box = _box(f"k1:{_key()}", legacy=legacy)
    new = box.encrypt("alpha", "providers.openai.api_key", "sk-new")
    assert new.startswith("axs1.k1.") and is_encrypted(new) and is_encrypted(old)
    assert box.decrypt("alpha", "providers.openai.api_key", new) == "sk-new"
    assert box.decrypt("alpha", "providers.openai.api_key", old) == "sk-old"
    assert box.needs_rewrite(old) and not box.needs_rewrite(new)


def test_a_secret_is_bound_to_its_tenant_and_field() -> None:
    box = _box(f"k1:{_key()}")
    token = box.encrypt("alpha", "providers.openai.api_key", "sk-1")
    for tenant, name in (("beta", "providers.openai.api_key"), ("alpha", "providers.anthropic.api_key")):
        with pytest.raises(NodestroError) as caught:
            box.decrypt(tenant, name, token)
        assert caught.value.code == "secrets.unreadable"
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(NodestroError):
        box.decrypt("alpha", "providers.openai.api_key", tampered)


def test_rotation_keeps_old_keys_readable_until_they_are_removed() -> None:
    first, second = f"k1:{_key()}", f"k2:{_key()}"
    token = _box(first).encrypt("alpha", "f", "value")
    rotated = _box(f"{second},{first}")
    assert rotated.decrypt("alpha", "f", token) == "value" and rotated.needs_rewrite(token)
    with pytest.raises(NodestroError) as caught:
        _box(second).decrypt("alpha", "f", token)
    assert caught.value.code == "secrets.unreadable"


def test_writing_v1_keeps_the_old_format_and_reads_both() -> None:
    box = _box(f"k1:{_key()}", write="v1")
    token = box.encrypt("alpha", "f", "value")
    assert token.startswith(ENCRYPTED_PREFIX) and not box.needs_rewrite(token)
    axs1 = box.box.encrypt("x", secret_context("alpha", "f"))  # written before a rollback of the format switch
    assert box.decrypt("alpha", "f", axs1) == "x" and box.decrypt("alpha", "f", token) == "value"


def test_a_retired_legacy_key_reads_v1_values_as_unreadable() -> None:
    old = SecretBox(b"L" * 32).encrypt("alpha", "f", "value")
    ring = f"k1:{_key()}"
    from axentra.config.secrets import Keyring
    from axentra.config.secrets import SecretBox as AxentraBox

    retired = KeyringSecretBox(None, AxentraBox(Keyring.parse(ring)))
    with pytest.raises(NodestroError) as caught:
        retired.decrypt("alpha", "f", old)
    assert caught.value.code == "secrets.unreadable"
    with pytest.raises(ValueError):
        KeyringSecretBox(None, AxentraBox(Keyring.parse(ring)), write="v1")


# ---- re-encryption on PostgreSQL --------------------------------------------------------------------------


@pytest.fixture
def database() -> Iterator[Any]:
    pytest.importorskip("psycopg")
    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    opened.migrate()
    opened.test_schema = schema  # type: ignore[attr-defined]
    yield opened
    opened.close()
    postgres_support.drop_schema(schema)


def _seed(database: Any, legacy: SecretBox) -> dict[str, Any]:
    stores = {tenant: database.store(tenant) for tenant in ("alpha", "beta")}
    settings = {
        "providers": {
            "openai": {"api_key": legacy.encrypt("alpha", "providers.openai.api_key", "sk-alpha")},
            "anthropic": {"api_key": "nodestro:enc:v1:broken", "base_url": "https://api.example"},
        },
        "default_model": "openai:gpt",
    }
    stores["alpha"].put_raw("settings", "app", settings)
    stores["alpha"].put_raw(
        "mcp_servers",
        "srv1",
        {
            "id": "srv1",
            "name": "Docs",
            "token_sealed": legacy.encrypt("alpha", "mcp:srv1", "mcp-token"),
            "oauth_pending_sealed": legacy.encrypt("alpha", "mcp:srv1:oauth-pending", '{"state": 1}'),
        },
    )
    stores["beta"].put_raw(
        "pricing_billing", "openai", {"admin_key": legacy.encrypt("beta", "pricing.billing.openai.admin_key", "adm")}
    )
    stores["beta"].put_raw("workflows", "w1", {"name": "not a secret", "api_key": "plain but not a secret field"})
    return stores


def test_reencryption_moves_every_secret_and_never_touches_an_unreadable_one(database: Any) -> None:
    legacy = SecretBox(b"L" * 32)
    stores = _seed(database, legacy)
    box = _box(f"k1:{_key()}", legacy=legacy)
    dry = reencrypt_tenant_secrets(database, box, dry_run=True)
    assert (dry.total, dry.pending, dry.reencrypted) == (5, 5, 0)
    assert dry.by_format == {"v1": 5}
    assert dry.failed == ["alpha/settings/app:providers.anthropic.api_key"]
    assert stores["alpha"].get_raw("settings", "app")["providers"]["openai"]["api_key"].startswith(ENCRYPTED_PREFIX)

    report = reencrypt_tenant_secrets(database, box)
    assert (report.pending, report.reencrypted) == (5, 4)
    assert report.failed == ["alpha/settings/app:providers.anthropic.api_key"]
    app = stores["alpha"].get_raw("settings", "app")
    assert app["providers"]["anthropic"]["api_key"] == "nodestro:enc:v1:broken"  # left exactly as it was
    assert app["providers"]["anthropic"]["base_url"] == "https://api.example" and app["default_model"] == "openai:gpt"
    assert box.decrypt("alpha", "providers.openai.api_key", app["providers"]["openai"]["api_key"]) == "sk-alpha"
    server = stores["alpha"].get_raw("mcp_servers", "srv1")
    assert box.decrypt("alpha", "mcp:srv1", server["token_sealed"]) == "mcp-token"
    assert box.decrypt("alpha", "mcp:srv1:oauth-pending", server["oauth_pending_sealed"]) == '{"state": 1}'
    billing = stores["beta"].get_raw("pricing_billing", "openai")
    assert billing["admin_key"].startswith("axs1.k1.")
    assert box.decrypt("beta", "pricing.billing.openai.admin_key", billing["admin_key"]) == "adm"
    assert stores["beta"].get_raw("workflows", "w1")["api_key"] == "plain but not a secret field"

    again = reencrypt_tenant_secrets(database, box)  # idempotent: only the unreadable value is still pending
    assert (again.pending, again.reencrypted, again.by_format) == (1, 0, {"axs1:k1": 4, "v1": 1})


def test_reencryption_rotates_to_a_new_key_and_can_be_limited_to_a_tenant(database: Any) -> None:
    legacy = SecretBox(b"L" * 32)
    stores = _seed(database, legacy)
    first = f"k1:{_key()}"
    reencrypt_tenant_secrets(database, _box(first, legacy=legacy))
    rotated = _box(f"k2:{_key()},{first}", legacy=legacy)
    only_beta = reencrypt_tenant_secrets(database, rotated, tenant="beta")
    assert (only_beta.total, only_beta.reencrypted) == (1, 1)
    assert stores["beta"].get_raw("pricing_billing", "openai")["admin_key"].startswith("axs1.k2.")
    assert stores["alpha"].get_raw("mcp_servers", "srv1")["token_sealed"].startswith("axs1.k1.")
    rest = reencrypt_tenant_secrets(database, rotated)
    assert rest.reencrypted == 3 and rest.by_format == {"axs1:k1": 3, "axs1:k2": 1, "v1": 1}
    final = reencrypt_tenant_secrets(database, rotated, dry_run=True)
    assert final.by_format == {"axs1:k2": 4, "v1": 1} and final.pending == 1


def test_a_workspace_on_the_keyring_box_reads_old_and_writes_new_provider_keys(database: Any, tmp_path: Path) -> None:
    from nodestro_runtime.postgres_tables import tenant_storage
    from nodestro_runtime.schemas import SettingsIn
    from nodestro_runtime.workspace import Workspace

    legacy = SecretBox(b"L" * 32)
    database.store("alpha").put_raw(
        "settings",
        "app",
        {"providers": {"openai": {"api_key": legacy.encrypt("alpha", "providers.openai.api_key", "sk-old-1234")}}},
    )
    box = _box(f"k1:{_key()}", legacy=legacy)
    workspace = Workspace(tmp_path / "alpha", integrations=[], storage=tenant_storage(database, "alpha", secrets=box))
    try:
        assert workspace._providers()["openai"]["api_key"] == "sk-old-1234"
        workspace.update_settings(SettingsIn.model_validate({"providers": {"anthropic": {"api_key": "sk-ant-5678"}}}))
        stored = database.store("alpha").get_raw("settings", "app")["providers"]
        assert stored["anthropic"]["api_key"].startswith("axs1.k1.")
        assert stored["openai"]["api_key"].startswith(ENCRYPTED_PREFIX)  # untouched until re-encryption
        providers = workspace._providers()
        assert providers["anthropic"]["api_key"] == "sk-ant-5678" and providers["openai"]["api_key"] == "sk-old-1234"
    finally:
        workspace.close()
    assert secret_context("alpha", "x") == "nodestro|tenant:alpha|x"


# ---- Axentra's schema next to Nodestro's ------------------------------------------------------------------


def test_axentra_migrates_into_nodestros_schema_and_creates_the_product(database: Any) -> None:
    from axentra.core.errors import ConfigurationError  # noqa: F401 - the package is importable

    schema = database.test_schema
    config = AxentraConfig(enabled=True, keyring=f"k1:{_key()}", allow_superuser=True)
    services = axentra_platform.AxentraServices(
        config, database_url=postgres_support.DSN, public_origin="http://localhost:8000", schema=schema
    )
    try:
        with pytest.raises(NodestroError) as caught:
            services.require_current_schema()
        assert caught.value.code == "storage.schema_mismatch"
        pending = services.pending_migrations()
        assert pending and all(item.startswith("axentra:") for item in pending)
        seen: list[list[str]] = []
        applied = services.migrate(seen.append)
        assert seen == [pending] and len(applied) == len(pending)
        assert services.pending_migrations() == [] and services.schema_problems() == []
        assert services.migrate(seen.append) == [] and len(seen) == 1  # nothing pending: no second backup
        services.require_current_schema()
        services.start()
        assert services.product_id.startswith("prd_")
        tables = {
            row[0]
            for row in database._query(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,)
            )
        }
        assert {"axentra_products", "axentra_tenants", "nodestro_documents"} <= tables
    finally:
        services.close()


def test_the_server_refuses_axentra_without_the_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(axentra_platform, "available", lambda: False)
    assert _problems(_env(NODESTRO_AXENTRA="on", NODESTRO_AXENTRA_KEYRING=f"k1:{_key()}")) == ["NODESTRO_AXENTRA"]


def test_secrets_never_appear_in_the_reencryption_report(database: Any) -> None:
    legacy = SecretBox(b"L" * 32)
    _seed(database, legacy)
    report = reencrypt_tenant_secrets(database, _box(f"k1:{_key()}", legacy=legacy))
    text = json.dumps(report.as_dict())
    for secret in ("sk-alpha", "mcp-token", '"adm"', "nodestro:enc:v1:", "axs1."):
        assert secret not in text


# ---- links between Nodestro tenants and Axentra tenants ------------------------------------------------------


@pytest.fixture
def linked(database: Any) -> Iterator[Any]:
    services = axentra_platform.AxentraServices(
        AxentraConfig(enabled=True, keyring=f"k1:{_key()}", allow_superuser=True),
        database_url=postgres_support.DSN,
        public_origin="http://localhost:8000",
        schema=database.test_schema,
    )
    services.migrate()
    services.start()
    try:
        yield axentra_platform.AxentraLink(services, database)
    finally:
        services.close()


def test_tenant_slugs_fit_axentra() -> None:
    assert axentra_platform.tenant_slug("acme_eu") == "acme-eu"
    assert axentra_platform.tenant_slug("a") == "a-0"
    assert axentra_platform.tenant_slug("x" * 63) == "x" * 63


def test_tenants_are_linked_once_with_unique_slugs(linked: Any) -> None:
    from identity_support import create_tenants

    create_tenants(linked.database, [("acme_eu", "Acme EU"), ("acme-eu", "Acme (dash)"), ("a", "A")])
    report = linked.backfill_tenants()
    assert report == {"tenants": 3, "already_linked": 0, "created": 3}
    assert linked.backfill_tenants() == {"tenants": 3, "already_linked": 3, "created": 0}
    ids = {tenant: linked.tenant(tenant) for tenant in ("acme_eu", "acme-eu", "a")}
    assert len(set(ids.values())) == 3 and all(value.startswith("ten_") for value in ids.values())
    rows = linked.database._query(
        "SELECT slug, settings->>'nodestro_tenant', name FROM axentra_tenants ORDER BY settings->>'nodestro_tenant'"
    )
    assert sorted(row[0] for row in rows) == ["a-0", "acme-eu", "acme-eu-2"]
    assert {row[1]: row[2] for row in rows} == {"a": "A", "acme-eu": "Acme (dash)", "acme_eu": "Acme EU"}
    assert linked.ensure_tenant("acme_eu", "ignored") == ids["acme_eu"]
    assert linked.reverse("tenant", ids["a"]) == "a"


def test_a_rolled_back_nodestro_transaction_leaves_no_axentra_tenant(linked: Any) -> None:
    class Stop(Exception):
        pass

    with pytest.raises(Stop), linked.joined():
        linked.ensure_tenant("ghost", "Ghost")
        raise Stop
    assert linked.lookup("tenant", "ghost") is None and linked.tenant("ghost") is None  # nothing cached either
    assert linked.database._query("SELECT count(*) FROM axentra_tenants WHERE slug = 'ghost'")[0][0] == 0
    assert linked.ensure_tenant("ghost", "Ghost").startswith("ten_")
