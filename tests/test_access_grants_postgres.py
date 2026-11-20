"""Grants and denies of the access model v2 (N-06) on PostgreSQL: stored per tenant, revoked, used once.

Needs ``NODESTRO_TEST_POSTGRES``.
"""

from __future__ import annotations

from collections.abc import Iterator

import postgres_support
import pytest

pytest.importorskip("psycopg")

from nodestro_runtime.storage_postgres import PostgresDatabase
from nodestro_runtime.tool_approvals import AccessRules, Person
from nodestro_runtime.tool_policy import AccessResource

ALICE = Person("usr_alice")


@pytest.fixture
def database() -> Iterator[PostgresDatabase]:
    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    try:
        opened.migrate()
        yield opened
    finally:
        opened.close()
        postgres_support.drop_schema(schema)


def test_grants_and_denies_are_kept_per_tenant(database: PostgresDatabase) -> None:
    alpha, beta = AccessRules(database.store("alpha")), AccessRules(database.store("beta"))
    docs = AccessResource(type="folder", id="/home/alice/docs")
    granted = alpha.grant(resource=docs, actions=["read"], duration="until_revoked", by=ALICE)
    blocked = alpha.deny(resource=docs, actions=["write"], by=ALICE)
    assert alpha.current() == ([granted], [blocked])
    assert beta.current() == ([], [])
    revoked = alpha.revoke_grant(granted.id, by=ALICE)
    assert alpha.grants() == [revoked] and revoked.revoked_at is not None


def test_a_once_grant_is_used_exactly_once_on_postgres(database: PostgresDatabase) -> None:
    rules = AccessRules(database.store("alpha"))
    once = rules.grant(
        resource=AccessResource(type="workflow", id="wf_1"), actions=["execute"], duration="once", by=ALICE
    )
    assert rules.use(once.id) is True
    assert rules.use(once.id) is False
    assert rules.grants()[0].status == "used"
