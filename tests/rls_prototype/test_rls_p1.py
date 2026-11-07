"""T-RLS-P1 (MUP-ONA MP-A9, prototype branch only, never merged): with the policies of ``policies.sql`` and
``app.tenant_id`` set transaction-locally, a statement without a tenant filter returns no foreign row.

Also checked: the restricted runtime role, fail-closed behavior without the setting, FORCE for the owner, Axentra's
tables in the same database, the connection pool and both ``ConnectionSource`` implementations (orkexa's
``PsycopgSource`` and Nodestro's ``DatabaseSource``), and what the code on master does under the policies. Some tests
assert a behavior that is a finding for the A9 report (marked FINDING), not a desired behavior.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from typing import Any

from orkexa.backends import postgres as orkexa_postgres
from orkexa.embeddings import CallableEmbeddings
from orkexa.retrieval import Chunk
import psycopg
from psycopg import sql
import pytest
import rls_support
from rls_support import POLICY_TABLES, SETTING

from nodestro_runtime.postgres_tables import LAYOUT, PostgresIndex, PostgresMemoryTables, memory_totals
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.storage_postgres import DatabaseSource, PostgresDatabase, grant_runtime_role, inspect_role

A, B = "tenant-a", "tenant-b"
KNOWLEDGE = "nodestro"
MEMORY = "nodestro:fixture-notes"


def _vector(text: str, dimensions: int, salt: str) -> list[float]:  # the fixture's fake model fx-embed-a
    digest = hashlib.sha256(f"{salt}:{text}".encode()).digest()
    return [round((digest[i] - 127.5) / 127.5, 4) for i in range(dimensions)]


FX_EMBED_A = CallableEmbeddings(
    lambda texts: [_vector(t, 8, "a") for t in texts], name="fx-embed-a", dimensions=8, provider="fixture"
)


@contextmanager
def tenant_tx(conninfo: str, tenant: str | None) -> Iterator[psycopg.Connection]:
    """A connection inside one transaction, with ``app.tenant_id`` set transaction-locally (``None``: not set);
    rolled back at the end."""
    with psycopg.connect(conninfo) as connection:
        if tenant is not None:
            connection.execute("SELECT set_config(%s, %s, true)", (SETTING, tenant))
        try:
            yield connection
        finally:
            connection.rollback()


def _truth(setup: rls_support.Setup) -> dict[str, dict[str, int]]:
    """Rows per tenant of every policy table, read by the superuser (not subject to row-level security)."""
    found: dict[str, dict[str, int]] = {}
    with psycopg.connect(setup.admin, autocommit=True) as admin:
        for table in POLICY_TABLES:
            rows = admin.execute(f"SELECT tenant_id, count(*) FROM {table} GROUP BY 1").fetchall()
            found[table] = {str(tenant): int(count) for tenant, count in rows}
    return found


@pytest.fixture
def setup(prototype: dict) -> rls_support.Setup:
    return prototype["setup"]


# ---- the fixture is meaningful ------------------------------------------------------------------------------


def test_before_the_policies_the_runtime_role_sees_both_tenants(prototype: dict) -> None:
    for table in POLICY_TABLES:
        if table == "nodestro_documents":
            continue  # FX-PG-0 has no documents
        assert prototype["before_tenants"][table] == [A, B], table


def test_the_policies_are_enabled_and_forced(prototype: dict) -> None:
    for table in POLICY_TABLES:
        assert prototype["before_state"][table] == (False, False, 0), table
        assert prototype["after_state"][table] == (True, True, 1), table


# ---- T-RLS-P1 ------------------------------------------------------------------------------------------------


# MUP-ONA T-RLS-P1
def test_p1_a_statement_without_tenant_filter_returns_no_foreign_row(setup: rls_support.Setup) -> None:
    truth = _truth(setup)
    for tenant, other in ((A, B), (B, A)):
        with tenant_tx(setup.runtime, tenant) as connection:
            for table in POLICY_TABLES:
                seen = dict(connection.execute(f"SELECT tenant_id, count(*) FROM {table} GROUP BY 1").fetchall())
                expected = {tenant: truth[table][tenant]} if truth[table].get(tenant) else {}
                assert seen == expected, (table, tenant, seen)
                foreign = connection.execute(f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (other,))
                assert foreign.fetchone() == (0,), (table, tenant)


# MUP-ONA T-RLS-P1
def test_p1_full_text_and_joins_without_tenant_filter_stay_in_the_tenant(setup: rls_support.Setup) -> None:
    with tenant_tx(setup.runtime, A) as connection:
        own = connection.execute(
            "SELECT count(*) FROM nodestro_chunks WHERE search @@ to_tsquery('simple', 'aardvark')"
        ).fetchone()
        other = connection.execute(
            "SELECT count(*) FROM nodestro_chunks WHERE search @@ to_tsquery('simple', 'zebra')"
        ).fetchone()
        assert (own, other) == ((1,), (0,))
        joined = connection.execute(
            "SELECT DISTINCT c.tenant_id, v.tenant_id FROM nodestro_chunk_vectors v JOIN nodestro_chunks c"
            " ON c.id = v.chunk_id"
        ).fetchall()
        assert joined == [(A, A)]


# MUP-ONA T-RLS-P1
def test_p1_writes_without_tenant_filter_reach_only_own_rows(setup: rls_support.Setup) -> None:
    truth = _truth(setup)
    with tenant_tx(setup.runtime, A) as connection:
        updated = connection.execute("UPDATE nodestro_memory_files SET content = content").rowcount
        assert updated == truth["nodestro_memory_files"][A]
        deleted = connection.execute("DELETE FROM nodestro_chunks").rowcount
        assert deleted == truth["nodestro_chunks"][A]
    assert _truth(setup) == truth  # rolled back; and tenant-b was never touched


# MUP-ONA T-RLS-P1
@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO nodestro_memory_files (tenant_id, namespace, path, content) VALUES ('tenant-b', 'x', 'p', 'c')",
        "INSERT INTO nodestro_files (tenant_id, namespace, id, entry, data) VALUES ('tenant-b', 'x', 'i', '{}', '')",
        "UPDATE nodestro_memory_files SET tenant_id = 'tenant-b'",
        "UPDATE nodestro_chunks SET tenant_id = 'tenant-b' WHERE tenant_id = 'tenant-a'",
    ],
)
def test_p1_a_write_with_a_foreign_tenant_is_refused(setup: rls_support.Setup, statement: str) -> None:
    with tenant_tx(setup.runtime, A) as connection, pytest.raises(psycopg.errors.InsufficientPrivilege) as raised:
        connection.execute(statement)
    assert "row-level security" in str(raised.value)


# ---- fail closed ---------------------------------------------------------------------------------------------


# MUP-ONA T-RLS-P1
def test_p1_without_the_setting_nothing_is_visible_or_writable(setup: rls_support.Setup) -> None:
    with psycopg.connect(setup.runtime, autocommit=True) as connection:
        # Never set in this session: current_setting(..., true) is NULL.
        assert connection.execute("SELECT current_setting('app.tenant_id', true)").fetchone() == (None,)
        for table in POLICY_TABLES:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "INSERT INTO nodestro_memory_files (tenant_id, namespace, path, content) VALUES (%s, 'x', 'p', 'c')",
                (A,),
            )
        # Set in an earlier transaction of the same session (a pooled connection): '' afterwards, still nothing.
        with connection.transaction():
            connection.execute("SELECT set_config(%s, %s, true)", (SETTING, A))
            assert connection.execute("SELECT count(*) FROM nodestro_chunks").fetchone()[0] > 0
        assert connection.execute("SELECT current_setting('app.tenant_id', true)").fetchone() == ("",)
        for table in POLICY_TABLES:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table
        # Explicitly empty.
        with connection.transaction():
            connection.execute("SELECT set_config(%s, '', true)", (SETTING,))
            assert connection.execute("SELECT count(*) FROM nodestro_chunks").fetchone() == (0,)


# ---- roles -----------------------------------------------------------------------------------------------------


def test_p1_the_runtime_role_is_restricted(setup: rls_support.Setup) -> None:
    owner = PostgresDatabase(setup.owner, pool_size=1)
    try:
        report = inspect_role(owner, rls_support.RUNTIME)
        assert report["restricted"] and not report["superuser"] and not report["bypass_rls"]
        assert report["owned_tables"] == []
    finally:
        owner.close()


def test_p1_a_bypassrls_role_is_refused_as_runtime_role(setup: rls_support.Setup) -> None:
    with psycopg.connect(setup.admin, autocommit=True) as admin:
        admin.execute("DROP ROLE IF EXISTS a9_bypass")
        admin.execute("CREATE ROLE a9_bypass LOGIN BYPASSRLS PASSWORD 'x'")
    owner = PostgresDatabase(setup.owner, pool_size=1)
    try:
        with pytest.raises(NodestroError) as raised:
            grant_runtime_role(owner, "a9_bypass")
        assert raised.value.code == "storage.role_unsafe"
    finally:
        owner.close()
        with psycopg.connect(setup.admin, autocommit=True) as admin:
            admin.execute("DROP ROLE a9_bypass")


def test_p1_force_applies_to_the_owner_and_not_to_a_superuser(setup: rls_support.Setup) -> None:
    truth = _truth(setup)
    with tenant_tx(setup.owner, None) as connection:
        for table in POLICY_TABLES:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table
    with tenant_tx(setup.owner, A) as connection:
        assert connection.execute("SELECT DISTINCT tenant_id FROM nodestro_chunks").fetchall() == [(A,)]
    with tenant_tx(setup.admin, None) as connection:  # superuser: policies never apply
        seen = connection.execute("SELECT count(*) FROM nodestro_chunks").fetchone()[0]
        assert seen == sum(truth["nodestro_chunks"].values())


def test_p1_finding_owner_data_migration_without_setting_changes_nothing(setup: rls_support.Setup) -> None:
    """FINDING: a later data migration (owner role, no tenant set) would silently update no row under FORCE."""
    with tenant_tx(setup.owner, None) as connection:
        assert connection.execute("UPDATE nodestro_chunks SET title = title").rowcount == 0


def test_p1_finding_backup_needs_bypassrls_already_today(setup: rls_support.Setup) -> None:
    """pg_dump reads with row_security = off and fails on a table whose policies would filter. As the owner without
    BYPASSRLS that already fails on Axentra's forced tables today; the Nodestro policies add tables, not a new
    requirement (nodestro-api's backup already needs an owner with BYPASSRLS or a superuser)."""
    for table in ("axentra_products", "nodestro_chunks"):
        with tenant_tx(setup.owner, None) as connection:
            connection.execute("SET LOCAL row_security = off")
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as raised:
                connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
            assert "row-level security" in str(raised.value), table


# ---- Axentra's tables ------------------------------------------------------------------------------------------


def test_p1_axentra_tables_are_unaffected(prototype: dict, setup: rls_support.Setup) -> None:
    before, after = prototype["before_state"], prototype["after_state"]
    axentra = {name for name in before if name.startswith("axentra_")}
    assert len(axentra) > 30
    assert {name: after[name] for name in axentra} == {name: before[name] for name in axentra}
    changed = {name for name in before if before[name] != after[name]}
    assert changed == set(POLICY_TABLES)
    # Axentra's own scope still works for the runtime role, with or without app.tenant_id in the same transaction.
    for tenant in (None, A):
        with tenant_tx(setup.runtime, tenant) as connection:
            connection.execute("SELECT set_config('axentra.scope', 'platform', true)")
            connection.execute(
                "INSERT INTO axentra_products (id, slug, name) VALUES ('prd_a9', 'a9-proto', 'A9 prototype')"
            )
            assert connection.execute("SELECT count(*) FROM axentra_products WHERE id = 'prd_a9'").fetchone() == (1,)
            # Axentra's platform scope is no bypass of Nodestro's policies.
            if tenant is None:
                assert connection.execute("SELECT count(*) FROM nodestro_chunks").fetchone() == (0,)
    # Nodestro's tenant setting alone does not open Axentra's tables.
    with tenant_tx(setup.runtime, A) as connection:
        connection.execute("SELECT set_config('axentra.scope', 'platform', true)")
        connection.execute("INSERT INTO axentra_products (id, slug, name) VALUES ('prd_a9', 'a9-proto', 'A9')")
        connection.execute("SELECT set_config('axentra.scope', '', true)")
        assert connection.execute("SELECT count(*) FROM axentra_products").fetchone() == (0,)


# ---- the ConnectionSource paths -------------------------------------------------------------------------------


@pytest.fixture
def runtime_database(setup: rls_support.Setup) -> Iterator[PostgresDatabase]:
    database = PostgresDatabase(setup.runtime, pool_size=4)
    yield database
    database.close()


def _orkexa_index(source: Any, tenant: str) -> orkexa_postgres.PostgresIndex:
    return orkexa_postgres.PostgresIndex(source, tenant, layout=LAYOUT, namespace=KNOWLEDGE, embeddings=FX_EMBED_A)


# MUP-ONA T-RLS-P1
def test_p1_orkexa_classes_work_through_the_hook_of_database_source(runtime_database: PostgresDatabase) -> None:
    source = DatabaseSource(runtime_database, tenant_setting=SETTING)
    for tenant, own, other in ((A, "aardvark", "zebra"), (B, "zebra", "aardvark")):
        index = _orkexa_index(source, tenant)
        for mode in ("fts", "vector", "hybrid"):
            hits = index.retrieve("heron river", k=50, mode=mode)
            assert hits, (tenant, mode)
        assert index.retrieve(own, mode="fts") and not index.retrieve(other, mode="fts")
        status = index.vector_status()
        assert status.active and status.active["model"] == "fx-embed-a"
        files = orkexa_postgres.PostgresFileStore(source, tenant, layout=LAYOUT, namespace=KNOWLEDGE)
        names = {entry.name for entry in files.list()}
        mine, theirs = ("marker-a.txt", "marker-b.txt") if tenant == A else ("marker-b.txt", "marker-a.txt")
        assert mine in names and theirs not in names
        memory = orkexa_postgres.PostgresMemory(source, tenant, layout=LAYOUT, namespace=MEMORY)
        assert f"Only {tenant} has this memory file." in memory.view(f"/memories/{tenant}.md")
    # A write through the hook passes WITH CHECK and is visible only to its tenant.
    index = _orkexa_index(source, A)
    ids = index.add_chunks([Chunk(text="Prototype quokka chunk.", source="a9-proto")])
    try:
        assert len(ids) == 1
        assert [hit.id for hit in index.retrieve("quokka", mode="fts")] == ids
        assert _orkexa_index(source, B).retrieve("quokka", mode="fts") == []
    finally:
        assert index.delete("a9-proto") == 1


# MUP-ONA T-RLS-P1
def test_p1_orkexa_psycopg_source_with_the_hook_over_a_shared_pool(setup: rls_support.Setup) -> None:
    """Two connections, eight threads, two tenants: every unfiltered read sees only its own tenant, and a connection
    given back to the pool carries no tenant."""
    with orkexa_postgres.PsycopgSource(setup.runtime, max_connections=2, tenant_setting=SETTING) as source:

        def work(n: int) -> set[str]:
            tenant = (A, B)[n % 2]
            with source.transaction("read", tenant_id=tenant) as connection:
                seen = {row[0] for row in connection.execute("SELECT DISTINCT tenant_id FROM nodestro_chunks")}
            index = orkexa_postgres.PostgresIndex(source, tenant, layout=LAYOUT, namespace=KNOWLEDGE)
            other = "zebra" if tenant == A else "aardvark"
            assert index.retrieve(other, mode="fts") == []
            return seen | {f"=>{tenant}"}

        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(work, range(200)))
        for result in results:
            tenant = next(item[2:] for item in result if item.startswith("=>"))
            assert result - {f"=>{tenant}"} == {tenant}
        with source.transaction("read") as connection:  # tenant_id None: PsycopgSource sets ''
            assert connection.execute("SELECT count(*) FROM nodestro_chunks").fetchone() == (0,)
        idle = source._idle[0]
        assert idle.execute("SELECT current_setting('app.tenant_id', true)").fetchone() == ("",)


def test_p1_finding_master_code_paths_see_nothing_under_the_policies(runtime_database: PostgresDatabase) -> None:
    """FINDING: on master Nodestro builds DatabaseSource without the hook, and its own SQL beside Orkexa's
    (counts, text, memory totals, the document store) runs through PostgresDatabase._query/_write directly. Under
    the policies all of it fails closed: it sees nothing. Enabling RLS therefore needs code changes first."""
    index = PostgresIndex(runtime_database, A, namespace=KNOWLEDGE)  # master wiring, no tenant_setting
    assert index.sources() == []
    assert index.counts() == {}
    assert memory_totals(runtime_database, A, "nodestro:") == []
    assert PostgresMemoryTables(runtime_database, A).totals("") == []


def test_p1_finding_joined_transactions_and_the_tenant_setting(
    runtime_database: PostgresDatabase, setup: rls_support.Setup
) -> None:
    """FINDING: in a joined transaction, Nodestro's DatabaseSource sets the tenant again for every store call, so
    each Orkexa statement runs under its own tenant; the setting of the last call then stays for the rest of the
    transaction, so a raw statement after it runs under that tenant. Orkexa's PsycopgSource sets it only when the
    outer transaction opens: a store of another tenant inside it reads nothing. Neither leaks a row of a tenant that
    no call in the transaction named."""
    source = DatabaseSource(runtime_database, tenant_setting=SETTING)
    a, b = _orkexa_index(source, A), _orkexa_index(source, B)
    with runtime_database.transaction():
        assert a.retrieve("aardvark", mode="fts")
        assert b.retrieve("zebra", mode="fts")
        assert a.retrieve("aardvark", mode="fts")  # set again for tenant-a
        assert b.retrieve("aardvark", mode="fts") == []
        # The last call was tenant-b's: a raw unfiltered statement now runs as tenant-b.
        assert runtime_database._query("SELECT DISTINCT tenant_id FROM nodestro_chunks") == [(B,)]
    with orkexa_postgres.PsycopgSource(setup.runtime, max_connections=1, tenant_setting=SETTING) as plain:
        a2 = orkexa_postgres.PostgresIndex(plain, A, layout=LAYOUT, namespace=KNOWLEDGE)
        b2 = orkexa_postgres.PostgresIndex(plain, B, layout=LAYOUT, namespace=KNOWLEDGE)
        with plain.transaction("transaction", tenant_id=A):
            assert a2.retrieve("aardvark", mode="fts")
            assert b2.retrieve("zebra", mode="fts") == []  # joined: still tenant-a's setting
