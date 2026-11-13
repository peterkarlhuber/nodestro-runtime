"""The service connection contract of ADR-0001 (NAL-A1), shared with CR-DB (owner decision E-2).

Each test names the contract rule it holds. PostgreSQL tenant separation runs with ``NODESTRO_TEST_POSTGRES``.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
import json
import logging
import os
from pathlib import Path

from pydantic import ValidationError
import pytest

from nodestro_runtime import connections
from nodestro_runtime.connections import (
    CONNECTIONS,
    CONTRACT_VERSION,
    EVENTS,
    RESERVED_TYPES,
    ConnectionIn,
    ConnectionPatch,
    ConnectionService,
    ConnectionType,
    ServiceConnection,
    connection_types,
    register_connection_type,
)
from nodestro_runtime.mcp.vault import TokenVault
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.server_settings import SecretBox
from nodestro_runtime.storage import SQLiteStore
from nodestro_runtime.workspace import Workspace

MARKER = "SECRET-MARKER-7f3c9a2e-do-not-leak"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    opened = SQLiteStore(tmp_path / "store.db")
    yield opened
    opened.close()


def service(
    store: SQLiteStore, *, tenant: str = "local", box: SecretBox | None = None, mode: str = "desktop"
) -> ConnectionService:
    vault = TokenVault(box if box is not None else SecretBox(os.urandom(32)), tenant, namespace="connection")
    return ConnectionService(store, vault, mode=mode)  # type: ignore[arg-type]


def smtp(name: str = "Mail") -> ConnectionIn:
    return ConnectionIn(
        type="smtp", name=name, settings={"host": "smtp.example.com", "port": 587, "from_address": "bot@example.com"}
    )


# ---- fields and versioning ---------------------------------------------------------------------------------------


def test_contract_fields_are_the_documented_ones() -> None:
    """ADR-0001 D-3 field table: a field is only added with a contract decision."""
    assert set(ServiceConnection.model_fields) == {
        "id",
        "contract",
        "type",
        "name",
        "provider",
        "description",
        "settings",
        "egress",
        "secret_storage",
        "secret_sealed",
        "check",
        "tenant_id",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
    }
    assert CONTRACT_VERSION == 1


def test_a_new_connection_carries_contract_tenant_and_audit_fields(store: SQLiteStore) -> None:
    item = service(store).create(smtp(), actor="alice")
    assert item.id.startswith("conn_") and item.contract == 1 and item.tenant_id == "local"
    assert item.created_by == item.updated_by == "alice"
    assert item.check.status == "unchecked" and item.secret_storage == "none"


def test_readers_ignore_unknown_fields_of_a_newer_minor_version(store: SQLiteStore) -> None:
    """Versioning: additive changes keep contract 1 and older readers keep working."""
    item = service(store).create(smtp())
    raw = dict(store.get_raw(CONNECTIONS, item.id) or {})
    raw["added_later"] = {"anything": 1}
    store.put_raw(CONNECTIONS, item.id, raw)
    assert service(store).get(item.id).name == "Mail"


def test_types_shipped_and_database_reserved_for_cr_db(store: SQLiteStore) -> None:
    assert {"search", "smtp", "webhook"} <= set(connection_types())
    assert "database" in RESERVED_TYPES and "database" not in connection_types()
    with pytest.raises(NodestroError) as caught:
        service(store).create(ConnectionIn(type="database", name="DB"))
    assert caught.value.code == "connection.type_unavailable"
    with pytest.raises(NodestroError) as caught:
        service(store).create(ConnectionIn(type="ftp", name="X"))
    assert caught.value.code == "connection.type_unknown"


def test_cr_db_can_register_the_database_type_once(store: SQLiteStore) -> None:
    class DatabaseSettings(connections._Settings):
        host: str
        port: int = 5432
        database: str

    item = ConnectionType(
        name="database",
        settings=DatabaseSettings,
        providers=("postgresql",),
        secret_label="password",
        egress=lambda _provider, settings: [f"{settings.host}:{settings.port}"],  # type: ignore[attr-defined]
    )
    register_connection_type(item)
    try:
        with pytest.raises(ValueError):
            register_connection_type(item)
        created = service(store).create(
            ConnectionIn(
                type="database",
                name="Sales",
                provider="postgresql",
                settings={"host": "db.example.com", "database": "sales"},
            )
        )
        assert created.egress == ["db.example.com:5432"]
    finally:
        connections._TYPES.pop("database", None)


# ---- settings and egress -----------------------------------------------------------------------------------------


def test_settings_refuse_unknown_fields_and_never_echo_values(store: SQLiteStore) -> None:
    """A secret typed into the settings by mistake is refused, and the error does not repeat it."""
    data = smtp()
    data.settings["password"] = MARKER
    with pytest.raises(NodestroError) as caught:
        service(store).create(data)
    assert caught.value.code == "connection.settings_invalid"
    assert MARKER not in json.dumps(caught.value.params) and MARKER not in str(caught.value)


def test_egress_is_derived_from_the_settings(store: SQLiteStore) -> None:
    svc = service(store)
    mail = svc.create(smtp())
    hook = svc.create(ConnectionIn(type="webhook", name="Hook", settings={"url": "https://hooks.example.com:8443/in"}))
    search = svc.create(ConnectionIn(type="search", name="Test search", provider="test"))
    assert mail.egress == ["smtp.example.com:587"]
    assert hook.egress == ["hooks.example.com:8443"]
    assert search.egress == []
    moved = svc.update(mail.id, ConnectionPatch(settings={**mail.settings, "host": "mail.example.org", "port": 465}))
    assert moved.egress == ["mail.example.org:465"]


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://hooks.example.com/in", "not_https"),
        ("https://user:pw@hooks.example.com/in", "userinfo"),
        ("https://hooks.example.com/in?token=abc", "secret_in_query"),
        ("https://10.0.0.5/in", "private"),
        ("https://169.254.169.254/latest", "metadata"),
        ("ftp://hooks.example.com/in", "scheme"),
    ],
)
def test_addresses_without_credentials_https_and_no_internal_literals(
    store: SQLiteStore, url: str, reason: str
) -> None:
    with pytest.raises(NodestroError) as caught:
        service(store, mode="server").create(ConnectionIn(type="webhook", name="Hook", settings={"url": url}))
    assert caught.value.code == "connection.address_invalid"
    assert caught.value.params["problems"] == [{"field": "url", "reason": reason}]


def test_loopback_only_outside_the_server_profile(store: SQLiteStore) -> None:
    local = ConnectionIn(type="webhook", name="Local", settings={"url": "http://127.0.0.1:9000/in"})
    assert service(store, mode="desktop").create(local).egress == ["127.0.0.1:9000"]
    with pytest.raises(NodestroError):
        service(store, tenant="t2", mode="server").create(local.model_copy(update={"name": "Local 2"}))


def test_names_are_unique_per_tenant_and_the_type_cannot_change(store: SQLiteStore) -> None:
    svc = service(store)
    svc.create(smtp())
    with pytest.raises(NodestroError) as caught:
        svc.create(smtp("mail"))
    assert caught.value.code == "connection.name_taken"
    with pytest.raises(ValidationError):
        ConnectionPatch.model_validate({"type": "webhook"})


# ---- secret rules ----------------------------------------------------------------------------------------------


def test_the_secret_is_write_only_and_sealed(store: SQLiteStore) -> None:
    svc = service(store)
    item = svc.set_secret(svc.create(smtp()).id, MARKER, actor="alice")
    assert item.secret_storage == "vault" and item.secret_sealed and MARKER not in item.secret_sealed
    view = svc.view(item)
    assert view.secret_set is True and "secret_sealed" not in view.model_dump()
    assert MARKER not in view.model_dump_json()
    assert MARKER not in json.dumps(store.get_raw(CONNECTIONS, item.id))
    resolved = svc.resolve(item.id)
    assert resolved.secret == MARKER and MARKER not in repr(resolved)


def test_removing_the_secret_and_changing_targets_resets_the_check(store: SQLiteStore) -> None:
    svc = service(store)
    item = svc.create(ConnectionIn(type="search", name="S", provider="test"))
    item = svc.set_secret(item.id, "key")
    assert svc.check(item.id).check.status == "ok"
    assert svc.set_secret(item.id, "").secret_storage == "none"
    assert svc.get(item.id).check.status == "unchecked"


def test_without_a_key_store_the_secret_stays_in_memory_only(store: SQLiteStore) -> None:
    svc = ConnectionService(store, TokenVault(None, "local", namespace="connection"))
    item = svc.set_secret(svc.create(smtp()).id, MARKER)
    assert item.secret_storage == "session" and item.secret_sealed is None
    assert svc.resolve(item.id).secret == MARKER
    assert MARKER not in json.dumps(store.get_raw(CONNECTIONS, item.id))
    # A new process (a new service) has no secret: never a plain-text fallback.
    fresh = ConnectionService(store, TokenVault(None, "local", namespace="connection"))
    with pytest.raises(NodestroError) as caught:
        fresh.resolve(item.id)
    assert caught.value.code == "connection.secret_missing"


def test_a_sealed_secret_opens_only_for_its_tenant_namespace_and_connection(store: SQLiteStore) -> None:
    box = SecretBox(os.urandom(32))
    alpha = service(store, tenant="alpha", box=box)
    item = alpha.set_secret(alpha.create(smtp()).id, MARKER)
    assert item.secret_sealed
    assert TokenVault(box, "beta", namespace="connection").open(item.id, item.secret_sealed) is None
    assert TokenVault(box, "alpha", namespace="mcp").open(item.id, item.secret_sealed) is None
    assert TokenVault(box, "alpha", namespace="connection").open("conn_other", item.secret_sealed) is None


def test_secrets_are_bounded_and_without_control_characters(store: SQLiteStore) -> None:
    svc = service(store)
    item = svc.create(smtp())
    for value, code in (("x" * 9000, "connection.secret_too_long"), ("a\nb", "connection.secret_invalid")):
        with pytest.raises(NodestroError) as caught:
            svc.set_secret(item.id, value)
        assert caught.value.code == code


def test_events_never_hold_secrets_or_settings_values(store: SQLiteStore, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    svc = service(store)
    item = svc.create(smtp(), actor="alice")
    svc.update(item.id, ConnectionPatch(name="Mail 2"), actor="alice")
    svc.set_secret(item.id, MARKER, actor="alice")
    svc.check(item.id, actor="alice")
    svc.delete(item.id, actor="alice")
    kinds = [event.kind for event in svc.events()]
    assert kinds == ["deleted", "checked", "secret_set", "updated", "created"]
    dump = json.dumps([raw for _, raw in store.items_raw(EVENTS)]) if hasattr(store, "items_raw") else ""
    dump += "".join(event.model_dump_json() for event in svc.events())
    assert MARKER not in dump and "smtp.example.com" not in dump
    assert MARKER not in caplog.text


def test_checks_store_status_and_code_only(store: SQLiteStore) -> None:
    svc = service(store)
    mail = svc.create(smtp())
    checked = svc.check(mail.id)
    assert checked.check.status == "failed" and checked.check.code == "connection.secret_missing"
    search = svc.create(ConnectionIn(type="search", name="S", provider="test"))
    assert svc.check(svc.set_secret(search.id, "k").id).check.model_dump(exclude={"checked_at"}) == {
        "status": "ok",
        "code": None,
        "kind": "service",
    }


def test_a_connection_in_use_is_not_deleted(store: SQLiteStore) -> None:
    svc = service(store)
    item = svc.create(smtp())
    svc.used_by = lambda _id: ["Weekly report / Writer"]
    with pytest.raises(NodestroError) as caught:
        svc.delete(item.id)
    assert caught.value.code == "connection.in_use"
    svc.delete(item.id, force=True)
    with pytest.raises(NodestroError):
        svc.get(item.id)


def test_resolve_checks_the_expected_type(store: SQLiteStore) -> None:
    svc = service(store)
    item = svc.set_secret(svc.create(smtp()).id, "pw")
    with pytest.raises(NodestroError) as caught:
        svc.resolve(item.id, expected_type="search")
    assert caught.value.code == "connection.type_mismatch"


# ---- tenant binding --------------------------------------------------------------------------------------------


def test_a_document_of_another_tenant_is_treated_as_missing(store: SQLiteStore) -> None:
    box = SecretBox(os.urandom(32))
    alpha, beta = service(store, tenant="alpha", box=box), service(store, tenant="beta", box=box)
    item = alpha.set_secret(alpha.create(smtp()).id, MARKER)
    assert beta.connections() == [] and beta.find(item.id) is None


def test_desktop_workspace_wires_connections_into_the_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODESTRO_VAULT_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    workspace = Workspace(tmp_path / "ws", integrations=[])
    try:
        item = workspace.connections.create(smtp())
        workspace.connections.set_secret(item.id, MARKER)
        assert workspace.connections.resolve(item.id).secret == MARKER
        # The secret is in no file of the data directory in plain text (database, backups, logs).
        for path in (tmp_path / "ws").rglob("*"):
            if path.is_file():
                assert MARKER.encode() not in path.read_bytes(), path
    finally:
        workspace.close()


def test_postgres_tenants_never_see_each_others_connections(tmp_path: Path) -> None:
    postgres_support = pytest.importorskip("postgres_support")
    pytest.importorskip("psycopg")
    from nodestro_runtime.postgres_tables import tenant_storage
    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    database = PostgresDatabase(dsn, schema=schema)
    database.migrate()
    box = SecretBox(os.urandom(32))
    alpha = Workspace(tmp_path / "alpha", integrations=[], storage=tenant_storage(database, "alpha", secrets=box))
    beta = Workspace(tmp_path / "beta", integrations=[], storage=tenant_storage(database, "beta", secrets=box))
    try:
        assert alpha.connections.mode == "server"
        item = alpha.connections.create(smtp())
        alpha.connections.set_secret(item.id, MARKER)
        assert beta.connections.connections() == [] and beta.connections.find(item.id) is None
        # A copied document stays unusable: another tenant's id, and a secret sealed for the other tenant.
        beta.store.put_raw(CONNECTIONS, item.id, alpha.store.get_raw(CONNECTIONS, item.id))
        assert beta.connections.find(item.id) is None
        raw = dict(alpha.store.get_raw(CONNECTIONS, item.id) or {})
        raw["tenant_id"] = "beta"
        beta.store.put_raw(CONNECTIONS, item.id, raw)
        with pytest.raises(NodestroError) as caught:
            beta.connections.resolve(item.id)
        assert caught.value.code == "connection.secret_unreadable"
        assert MARKER not in json.dumps(raw)
    finally:
        alpha.close()
        beta.close()
        database.close()
        postgres_support.drop_schema(schema)


def test_exported_packages_read_connections_from_their_own_file_and_environment() -> None:
    """Export rule: connections come from the target computer, the environment replaces the secret."""
    from nodestro_runtime.connections import StaticConnections, secret_variable

    entries = {
        "conn_aaaaaaaaaaaa": {"type": "search", "provider": "test", "name": "Search", "settings": {}, "secret": "file"},
        "conn_bbbbbbbbbbbb": {"type": "webhook", "settings": {"url": "https://user:pw@example.com/x"}},
        "conn_cccccccccccc": {"type": "smtp", "settings": {"host": "mail.example.com", "unknown": 1}},
        "conn_dddddddddddd": "not an object",
    }
    variable = secret_variable("conn_aaaaaaaaaaaa")
    assert variable == "NODESTRO_CONNECTION_CONN_AAAAAAAAAAAA_SECRET"
    static = StaticConnections(entries, environ={variable: MARKER})
    resolved = static.resolve("conn_aaaaaaaaaaaa", expected_type="search")
    assert resolved.secret == MARKER and MARKER not in repr(resolved)
    assert static.problems == {
        "conn_bbbbbbbbbbbb": "connection.address_invalid",
        "conn_cccccccccccc": "connection.settings_invalid",
        "conn_dddddddddddd": "connection.settings_invalid",
    }
    required = {"conn_aaaaaaaaaaaa": "search:test", "conn_bbbbbbbbbbbb": "webhook:generic", "conn_x": "smtp:generic"}
    assert static.missing(required) == ["conn_bbbbbbbbbbbb", "conn_x"]
    assert static.missing({"conn_aaaaaaaaaaaa": "smtp:generic"}) == ["conn_aaaaaaaaaaaa"]
    with pytest.raises(NodestroError) as caught:
        static.resolve("conn_x")
    assert caught.value.code == "connection.not_found"
