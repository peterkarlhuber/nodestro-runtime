"""The identity reads of ``NODESTRO_IDENTITY_BACKEND=axentra`` find Axentra's rows through indexes.

``nodestro_axentra_ids`` keeps ids in collation ``"C"`` (like every Nodestro id), Axentra's id columns use the
database default: compared across that difference PostgreSQL cannot use Axentra's indexes. Session resolution and
``memberships`` (``/api/auth/me``) then read every membership of the product, so their cost grew with the number of
memberships of all tenants (measured with ``nodestro-api/tests/bench_api.py``: ``/api/auth/me`` 20+ ms at 3,500
memberships). Each read is explained here with sequential scans switched off: a read that cannot use an index on
``axentra_memberships`` / ``axentra_tenants`` still shows a scan without an index condition.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import postgres_support
import pytest

pytest.importorskip("psycopg")
pytest.importorskip("axentra", reason="Axentra needs Python 3.11+")

from nodestro_runtime.axentra_identity import AxentraIdentityStore
from nodestro_runtime.axentra_platform import AxentraLink
from nodestro_runtime.storage_postgres import PostgresDatabase

ISSUER = "https://login.example"
INDEXED = ("axentra_memberships", "axentra_tenants")


@pytest.fixture
def database() -> Iterator[PostgresDatabase]:
    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    opened.migrate()
    opened.test_schema = schema  # type: ignore[attr-defined]
    yield opened
    opened.close()
    postgres_support.drop_schema(schema)


@pytest.fixture
def store(database: PostgresDatabase) -> Iterator[AxentraIdentityStore]:
    from identity_support import axentra_services

    services = axentra_services(database)
    mirror = AxentraIdentityStore(database, AxentraLink(services, database), mode="mirror")
    mirror.ensure_roles()
    for index in range(3):
        mirror.create_tenant(f"t{index}", f"Tenant {index}")
    for index in range(6):
        user = mirror.login(ISSUER, f"sub-{index}")
        mirror.set_role(f"t{index % 3}", user.id, "owner" if index < 3 else "editor")
    yield AxentraIdentityStore(database, AxentraLink(services, database), mode="axentra")
    services.close()


def _scans(plan: Any) -> Iterator[dict[str, Any]]:
    if isinstance(plan, dict):
        if plan.get("Relation Name") in INDEXED:
            yield plan
        for child in plan.get("Plans", []):
            yield from _scans(child)


def _captured(store: AxentraIdentityStore, call: Any) -> list[tuple[str, Any]]:
    """The statements ``call`` sends through Axentra's transactions."""
    from axentra.data import database as axdb

    seen: list[tuple[str, Any]] = []
    one, every = axdb.Tx.one, axdb.Tx.all

    def record_one(self: Any, sql: str, params: Any = None) -> Any:
        seen.append((sql, params))
        return one(self, sql, params)

    def record_all(self: Any, sql: str, params: Any = None) -> Any:
        seen.append((sql, params))
        return every(self, sql, params)

    axdb.Tx.one, axdb.Tx.all = record_one, record_all  # type: ignore[method-assign]
    try:
        call()
    finally:
        axdb.Tx.one, axdb.Tx.all = one, every  # type: ignore[method-assign]
    return [(sql, params) for sql, params in seen if any(name in sql for name in INDEXED)]


def _unindexed(store: AxentraIdentityStore, statements: list[tuple[str, Any]]) -> list[str]:
    from axentra.data.database import DbScope

    found = []
    for sql, params in statements:
        with store.platform.db.transaction(DbScope.platform()) as tx:
            tx.execute("SET LOCAL enable_seqscan = off")
            plan = tx.one("EXPLAIN (FORMAT JSON) " + sql, params)
        assert plan is not None
        for scan in _scans(plan["QUERY PLAN"][0]["Plan"]):
            if "Index Cond" not in scan and "Recheck Cond" not in scan:
                found.append(f"{scan['Node Type']} on {scan['Relation Name']} in: {sql[:120]}")
    return found


def test_session_resolution_and_membership_reads_use_axentras_indexes(store: AxentraIdentityStore) -> None:
    user = store.login(ISSUER, "sub-4")
    token = store.create_session(user.id, "t1")
    reads = {
        "session": lambda: store.session(token),
        "memberships": lambda: store.memberships(user.id),
        "members": lambda: store.members("t1"),
        "role": lambda: store.role("t1", user.id),
        "tenant": lambda: store.tenant("t1"),
    }
    problems = []
    for name, call in reads.items():
        statements = _captured(store, call)
        assert statements, name
        problems += [f"{name}: {item}" for item in _unindexed(store, statements)]
    assert problems == []
    # The answers are unchanged.
    session = store.session(token)
    assert session is not None and (session.user_id, session.tenant_id, session.role) == (user.id, "t1", "editor")
    assert [(item.tenant_id, item.role) for item in store.memberships(user.id)] == [("t1", "editor")]
    assert sorted(item.role for item in store.members("t1")) == ["editor", "owner"]
    assert store.role("t1", user.id) == "editor" and store.role("t0", user.id) is None
    found = store.tenant("t1")
    assert found is not None and found.id == "t1" and store.tenant("missing") is None
