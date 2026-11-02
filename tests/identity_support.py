"""Helpers for tests that need identity rows in a Nodestro PostgreSQL database."""

from __future__ import annotations

from typing import Any


def create_tenants(database: Any, tenants: list[tuple[str, str]]) -> None:
    from nodestro_runtime.identity_store import IdentityStore

    store = IdentityStore(database)
    for tenant_id, name in tenants:
        store.create_tenant(tenant_id, name)


def axentra_services(database: Any) -> Any:
    """Axentra opened on the test database's schema, migrated and started (close it after the test)."""
    import base64
    import secrets

    import postgres_support

    from nodestro_runtime.axentra_platform import AxentraConfig, AxentraServices

    ring = "k1:" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    services = AxentraServices(
        AxentraConfig(enabled=True, keyring=ring, allow_superuser=True),
        database_url=postgres_support.DSN,
        public_origin="http://localhost:8000",
        schema=database.test_schema,
    )
    services.migrate()
    services.start()
    return services


def age_sessions(database: Any, by: Any) -> None:
    """Move the last use of every session back by ``by`` (a ``timedelta``) on both sides."""
    database._write("UPDATE nodestro_sessions SET last_seen_at = clock_timestamp() - %s", (by,))
    exists = database._query("SELECT to_regclass('axentra_sessions') IS NOT NULL")[0][0]
    if exists:
        database._write("UPDATE axentra_sessions SET last_seen_at = now() - %s", (by,))
