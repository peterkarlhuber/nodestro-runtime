"""Helpers for tests that need identity rows in a Nodestro PostgreSQL database."""

from __future__ import annotations

from typing import Any


def create_tenants(database: Any, tenants: list[tuple[str, str]]) -> None:
    from nodestro_runtime.identity_store import IdentityStore

    store = IdentityStore(database)
    for tenant_id, name in tenants:
        store.create_tenant(tenant_id, name)
