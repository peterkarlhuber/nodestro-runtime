"""Workspaces of different tenants on one PostgreSQL database (needs ``NODESTRO_TEST_POSTGRES``)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import postgres_support
import pytest

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import WorkflowIn
from nodestro_runtime.workspace import LOCAL_TENANT, Workspace

pytest.importorskip("psycopg")

from nodestro_runtime.postgres_tables import tenant_storage
from nodestro_runtime.storage_postgres import PostgresDatabase


@pytest.fixture
def database() -> Iterator[PostgresDatabase]:
    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    opened.migrate()
    yield opened
    opened.close()
    postgres_support.drop_schema(schema)


def _workspace(database: PostgresDatabase, root: Path, tenant: str) -> Workspace:
    return Workspace(root / tenant, integrations=[], storage=tenant_storage(database, tenant))


def test_each_tenant_sees_only_its_own_data(database: PostgresDatabase, tmp_path: Path) -> None:
    alpha, beta = _workspace(database, tmp_path, "alpha"), _workspace(database, tmp_path, "beta")
    try:
        assert (alpha.tenant_id, beta.tenant_id) == ("alpha", "beta")
        saved = alpha.save_workflow(WorkflowIn.model_validate({"name": "Alpha only", "nodes": [], "edges": []}))
        assert [item.id for item in alpha.store.list("workflows", type(saved))] == [saved.id]
        assert beta.store.list("workflows", type(saved)) == []
        with pytest.raises(KeyError):
            beta.workflow(saved.id)
        alpha.knowledge.add(b"Alpha secret contract", name="alpha.txt")
        assert beta.knowledge.files() == [] and beta.knowledge.search("secret") == []
        alpha.memory.open("Team").write("/memories/a.md", "alpha")
        assert beta.memory.all() == []
        assert alpha.data_dir != beta.data_dir  # files of each tenant in their own area
    finally:
        alpha.close()
        beta.close()
    assert database.closed is False  # the workspaces do not own the server database


def test_a_tenant_workspace_has_no_sqlite_file(database: PostgresDatabase, tmp_path: Path) -> None:
    workspace = _workspace(database, tmp_path, "alpha")
    try:
        with pytest.raises(NodestroError) as caught:
            workspace.sqlite_store  # noqa: B018
        assert caught.value.code == "storage.not_sqlite"
        assert not (tmp_path / "alpha" / "nodestro.db").exists()
    finally:
        workspace.close()


def test_tenant_tool_code_runs_only_in_containers(database: PostgresDatabase, tmp_path: Path) -> None:
    from nodestro_runtime.schemas import SettingsIn

    # Even a stored (for example imported) subprocess setting does not bring the host subprocess back.
    database.store("alpha").put_raw("settings", "app", {"sandbox": "subprocess"})
    workspace = _workspace(database, tmp_path, "alpha")
    try:
        assert workspace.sandbox_policy.profile == "server"
        assert workspace.settings().sandbox == "docker"
        # Nodestro's runtime image, streamed (no bind mounts), built on first use.
        assert type(workspace.tools.registry.sandbox).__name__ == "RuntimeImageSandbox"
        with pytest.raises(NodestroError) as caught:
            workspace.update_settings(SettingsIn.model_validate({"sandbox": "subprocess"}))
        assert caught.value.code == "sandbox.unavailable"
        assert workspace.store.get_raw("settings", "app") == {"sandbox": "subprocess"}  # unchanged
    finally:
        workspace.close()


def test_the_desktop_is_the_local_tenant(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "desktop", integrations=[])
    try:
        assert workspace.tenant_id == LOCAL_TENANT
        assert workspace.sqlite_store is workspace.store
    finally:
        workspace.close()


def test_tenant_provider_keys_are_encrypted_and_server_variables_are_not_shared(
    database: PostgresDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("cryptography")
    from nodestro_runtime.schemas import SettingsIn
    from nodestro_runtime.server_settings import SecretBox, is_encrypted

    # A key in the server's own environment belongs to no tenant.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-of-the-server")
    box = SecretBox(b"s" * 32)
    alpha = Workspace(tmp_path / "alpha", integrations=[], storage=tenant_storage(database, "alpha", box))
    beta = Workspace(tmp_path / "beta", integrations=[], storage=tenant_storage(database, "beta", box))
    try:
        anthropic = next(item for item in beta.settings().providers if item.id == "anthropic")
        assert not anthropic.configured and anthropic.key_source == "none" and anthropic.env_var is None
        assert "sk-ant-of-the-server" not in beta.secret_keys()
        alpha.update_settings(SettingsIn.model_validate({"providers": {"anthropic": {"api_key": "sk-ant-alpha"}}}))
        stored = database.store("alpha").get_raw("settings", "app")
        sealed = stored["providers"]["anthropic"]["api_key"]
        assert is_encrypted(sealed) and "sk-ant-alpha" not in str(stored)
        assert alpha.connection("anthropic").api_key == "sk-ant-alpha"
        # Unrelated changes keep the sealed key as it is.
        alpha.update_settings(SettingsIn.model_validate({"default_budget_usd": 3.0}))
        assert database.store("alpha").get_raw("settings", "app")["providers"]["anthropic"]["api_key"] == sealed
        # Copied into another tenant, it does not decrypt: the key counts as missing there.
        database.store("beta").put_raw("settings", "app", {"providers": {"anthropic": {"api_key": sealed}}})
        assert not next(item for item in beta.settings().providers if item.id == "anthropic").configured
        # A key in plain text (e.g. written around Nodestro) is never used on a server.
        database.store("beta").put_raw("settings", "app", {"providers": {"openai": {"api_key": "sk-plain"}}})
        assert not next(item for item in beta.settings().providers if item.id == "openai").configured
        beta.update_settings(SettingsIn.model_validate({"default_budget_usd": 1.0}))
        assert "sk-plain" not in str(database.store("beta").get_raw("settings", "app"))
    finally:
        alpha.close()
        beta.close()


def test_a_tenant_reaches_only_the_model_endpoints_the_operator_allows(
    database: PostgresDatabase, tmp_path: Path
) -> None:
    from nodestro_runtime.providers import endpoint_origin
    from nodestro_runtime.schemas import SettingsIn

    assert endpoint_origin("HTTPS://Models.Example.com:443/v1/") == "https://models.example.com"
    assert endpoint_origin("http://[::1]:8000/v1") == "http://[::1]:8000"
    storage = tenant_storage(database, "alpha")
    allowed = Workspace(
        tmp_path / "alpha",
        integrations=[],
        storage=type(storage)(**{**storage.__dict__, "endpoints": ("https://models.example.com",)}),
    )
    try:
        for url in ("http://postgres:5432/v1", "http://169.254.169.254/latest", "https://models.example.com.evil/v1"):
            with pytest.raises(NodestroError) as caught:
                allowed.update_settings(SettingsIn.model_validate({"providers": {"compatible": {"base_url": url}}}))
            assert caught.value.code == "provider.endpoint_not_allowed"
        assert "base_url" not in (allowed.store.get_raw("settings", "app") or {}).get("providers", {}).get(
            "compatible", {}
        )
        allowed.update_settings(
            SettingsIn.model_validate({"providers": {"compatible": {"base_url": "https://models.example.com:443/v1"}}})
        )
        assert allowed.connection("compatible").base_url == "https://models.example.com:443/v1"
        # A URL stored before the operator changed the list (or written around Nodestro) is refused when used.
        database.store("alpha").put_raw(
            "settings", "app", {"providers": {"ollama": {"base_url": "http://localhost:11434/v1"}}}
        )
        with pytest.raises(NodestroError) as caught:
            allowed.connection("ollama")
        assert caught.value.code == "provider.endpoint_not_allowed"
    finally:
        allowed.close()


def test_the_desktop_reaches_any_endpoint(tmp_path: Path) -> None:
    from nodestro_runtime.schemas import SettingsIn

    with Workspace(tmp_path / "desktop", integrations=[]) as desktop:
        desktop.update_settings(
            SettingsIn.model_validate({"providers": {"ollama": {"base_url": "http://localhost:11434/v1"}}})
        )
        assert desktop.connection("ollama").base_url == "http://localhost:11434/v1"
