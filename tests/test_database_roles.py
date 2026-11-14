"""Database roles of the server profile and the one connection pool Nodestro and Axentra share.

A server runs ``migrate`` as the **owner** role and ``api``/``worker`` as a **runtime** role that
:func:`~nodestro_runtime.storage_postgres.grant_runtime_role` set up: no superuser, no ``BYPASSRLS``, owns no
table. These tests create both roles (and a schema owned by the owner) themselves, so they prove the setup in every
test mode:

- negative: the runtime role cannot read another tenant's Axentra rows without a scope, cannot switch row-level
  security off, change policies, alter, drop or truncate tables, fake the schema versions or become the owner;
- positive: Nodestro's stores and Axentra's services work under it;
- the shared pool: Axentra borrows Nodestro's connections (``AxentraServices(database=...)``); its scope settings are
  transaction-local, so the very same physical connection carries nothing from one tenant to the next, and the
  joined transactions nest without changing the tenant boundary.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
import secrets
import threading
from typing import Any

import postgres_support
from pydantic import BaseModel
import pytest

from nodestro_runtime.problems import NodestroError

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("axentra")

from psycopg import sql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

from nodestro_runtime.axentra_platform import AxentraConfig, AxentraLink, AxentraServices  # noqa: E402
from nodestro_runtime.storage_postgres import (  # noqa: E402
    PostgresDatabase,
    grant_runtime_role,
    inspect_role,
)


class Doc(BaseModel):
    id: str
    text: str = ""


def _dsn(user: str, password: str) -> str:
    parts = conninfo_to_dict(postgres_support.DSN)
    parts.update(user=user, password=password)
    return make_conninfo("", **parts)


def _key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


class Roles:
    def __init__(self, schema: str, owner: str, runtime: str, password: str) -> None:
        self.schema, self.owner, self.runtime, self.password = schema, owner, runtime, password
        self.owner_dsn = _dsn(owner, password)
        self.runtime_dsn = _dsn(runtime, password)

    def services(self, database: PostgresDatabase, **config: Any) -> AxentraServices:
        return AxentraServices(
            AxentraConfig(enabled=True, keyring=f"k1:{_key()}", **config),
            database_url=self.runtime_dsn,
            migration_url=self.owner_dsn,
            public_origin="http://localhost:8000",
            schema=self.schema,
            database=database,
        )

    def runtime_database(self, **kwargs: Any) -> PostgresDatabase:
        return _open(self.runtime_dsn, self.schema, **kwargs)


def _open(dsn: str, schema: str, **kwargs: Any) -> PostgresDatabase:
    """A database on exactly ``dsn`` (not remapped by NODESTRO_TEST_RUNTIME_ROLE, which only maps the test DSN)."""
    return PostgresDatabase(dsn, schema=schema, **kwargs)


@pytest.fixture(scope="module")
def roles() -> Iterator[Roles]:
    """Owner and runtime role, a schema owned by the owner, Nodestro's and Axentra's migrations run as the owner, and
    the runtime role's grants."""
    postgres_support.require_postgres()
    suffix = secrets.token_hex(4)
    found = Roles(postgres_support.new_schema(), f"nd_owner_{suffix}", f"nd_runtime_{suffix}", secrets.token_hex(16))
    with psycopg.connect(postgres_support.DSN, autocommit=True) as admin:
        for role in (found.owner, found.runtime):
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(found.password)
                )
            )
        admin.execute(
            sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                sql.Identifier(found.schema), sql.Identifier(found.owner)
            )
        )
    owner = _open(found.owner_dsn, found.schema)
    try:
        owner.migrate()
        services = AxentraServices(
            AxentraConfig(enabled=True, keyring=f"k1:{_key()}"),
            database_url=found.owner_dsn,
            public_origin="http://localhost:8000",
            schema=found.schema,
        )
        try:
            assert services.migrate()  # Axentra's tables, owned by the owner role as well
        finally:
            services.close()
        report = grant_runtime_role(owner, found.runtime)
        assert report["restricted"] and report["connect"] and not report["can_create"], report
    finally:
        owner.close()
    yield found
    with psycopg.connect(postgres_support.DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(found.schema)))
        for role in (found.runtime, found.owner):
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.fixture
def runtime(roles: Roles) -> Iterator[PostgresDatabase]:
    database = roles.runtime_database()
    yield database
    database.close()


@pytest.fixture(scope="module")
def linked(roles: Roles) -> Iterator[tuple[PostgresDatabase, AxentraLink, dict[str, str]]]:
    """The runtime role's database with Axentra started on its pool, and two linked tenants."""
    database = roles.runtime_database()
    services = roles.services(database)
    services.start()
    link = AxentraLink(services, database)
    from nodestro_runtime.identity_store import IdentityStore

    tenants: dict[str, str] = {}
    identity = IdentityStore(database)
    for tenant in ("alpha", "beta"):
        if identity.tenant(tenant) is None:
            identity.create_tenant(tenant, tenant.title())
        tenants[tenant] = link.ensure_tenant(tenant, tenant.title())
    try:
        yield database, link, tenants
    finally:
        services.close()
        database.close()


# ---- grant_runtime_role and inspect_role ------------------------------------------------------------------------


def test_grant_refuses_roles_row_level_security_would_not_bind(roles: Roles) -> None:
    owner = _open(roles.owner_dsn, roles.schema)
    try:
        for role, reason in ((roles.owner, "owner role itself"), ("postgres", "superuser")):
            with pytest.raises(NodestroError) as caught:
                grant_runtime_role(owner, role)
            assert caught.value.code == "storage.role_unsafe" and reason in str(caught.value)
        with pytest.raises(NodestroError) as caught:
            grant_runtime_role(owner, "no_such_role_at_all")
        assert caught.value.code == "storage.role_missing"
        again = grant_runtime_role(owner, roles.runtime)  # idempotent
        assert again["restricted"] and again["owned_tables"] == [] and again["tables"] > 20
        assert inspect_role(owner, roles.owner)["owned_tables"]  # the owner owns every table
    finally:
        owner.close()


@pytest.fixture
def extra_roles() -> Iterator[list[str]]:
    """Roles (and schemas named like them) a test creates; dropped afterwards."""
    created: list[str] = []
    yield created
    with psycopg.connect(postgres_support.DSN, autocommit=True) as admin:
        for name in reversed(created):
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


def _create_role(extra_roles: list[str], attributes: str = "") -> str:
    name = f"nd_extra_{secrets.token_hex(4)}"
    with psycopg.connect(postgres_support.DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} NOSUPERUSER " + attributes).format(sql.Identifier(name)))
    extra_roles.append(name)
    return name


def _may_write(owner: PostgresDatabase, role: str) -> bool:
    rows = owner._query("SELECT has_table_privilege(%s, 'nodestro_documents', 'INSERT')", (role,))
    return bool(rows[0][0])


def test_grant_refuses_a_member_of_a_bypassrls_role(roles: Roles, extra_roles: list[str]) -> None:
    """TABRION MIG-NOD-5 (contract I-20): a role that can ``SET ROLE`` to a ``BYPASSRLS`` role is not restricted;
    before Tabrion it was reported restricted and got the grants."""
    bypass = _create_role(extra_roles, "BYPASSRLS")
    member = _create_role(extra_roles)
    with psycopg.connect(postgres_support.DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(bypass), sql.Identifier(member)))
    owner = _open(roles.owner_dsn, roles.schema)
    try:
        report = inspect_role(owner, member)
        assert report["bypass_rls"] and not report["superuser"] and not report["restricted"]
        with pytest.raises(NodestroError) as caught:
            grant_runtime_role(owner, member)
        assert caught.value.code == "storage.role_unsafe" and "has BYPASSRLS" in str(caught.value)
        assert caught.value.params == {"role": member}
        assert not _may_write(owner, member)  # nothing granted
    finally:
        owner.close()


def test_grant_refuses_a_role_that_can_become_the_owner_of_the_schema(roles: Roles, extra_roles: list[str]) -> None:
    """The schema's owner may drop or alter every object in it: a role that can become it owns no table yet, but is
    refused all the same (Tabrion's check, with Nodestro's code)."""
    schema_owner = _create_role(extra_roles)
    member = _create_role(extra_roles)
    with psycopg.connect(postgres_support.DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(schema_owner), sql.Identifier(member)))
        admin.execute(
            sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                sql.Identifier(schema_owner), sql.Identifier(schema_owner)
            )
        )
        admin.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema_owner), sql.Identifier(roles.owner))
        )
    owner = _open(roles.owner_dsn, schema_owner)
    try:
        report = inspect_role(owner, member)
        assert report["restricted"] and report["tables"] == 0  # nothing the report shows
        with pytest.raises(NodestroError) as caught:
            grant_runtime_role(owner, member)
        assert caught.value.code == "storage.role_unsafe" and "owner of the schema" in str(caught.value)
        assert caught.value.params == {"role": member}
        rows = owner._query("SELECT has_schema_privilege(%s, %s, 'USAGE')", (member, schema_owner))
        assert rows[0][0]  # through its membership only: no grant of its own was written
        acl = owner._query("SELECT nspacl::text FROM pg_namespace WHERE nspname = %s", (schema_owner,))
        assert member not in str(acl[0][0] or "")
    finally:
        owner.close()


def test_grant_runtime_role_needs_a_transaction_of_its_own(roles: Roles) -> None:
    owner = _open(roles.owner_dsn, roles.schema)
    try:
        with owner.transaction():
            assert inspect_role(owner, roles.runtime)["restricted"]  # reads work inside one
            with pytest.raises(RuntimeError, match="inside a transaction"):
                grant_runtime_role(owner, roles.runtime)
        assert grant_runtime_role(owner, roles.runtime)["restricted"]
    finally:
        owner.close()


def test_role_info_tells_owner_from_runtime_role(roles: Roles, runtime: PostgresDatabase) -> None:
    assert runtime.role_info() == {
        "role": roles.runtime,
        "superuser": False,
        "bypass_rls": False,
        "table_owner": False,
    }
    owner = _open(roles.owner_dsn, roles.schema)
    try:
        assert owner.role_info()["table_owner"] is True
    finally:
        owner.close()


# ---- negative: what the runtime role cannot do ------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE axentra_tenants NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE axentra_tenants DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE axentra_audit_events DISABLE ROW LEVEL SECURITY",
        "DROP POLICY scope ON axentra_tenants",
        "CREATE POLICY open ON axentra_tenants USING (true)",
        "ALTER TABLE nodestro_documents ADD COLUMN forged text",
        "ALTER TABLE nodestro_documents ENABLE ROW LEVEL SECURITY",
        "DROP TABLE nodestro_documents",
        "DROP TABLE axentra_tenants",
        "TRUNCATE nodestro_documents",
        "TRUNCATE axentra_audit_events",
        "CREATE TABLE forged (id int)",
        "CREATE FUNCTION forged() RETURNS int LANGUAGE sql AS 'SELECT 1'",
        "INSERT INTO nodestro_schema_migrations (version, name) VALUES (999, 'forged.sql')",
        "UPDATE nodestro_schema_migrations SET checksum = 'x'",
        "DELETE FROM axentra_schema_migrations",
        "ALTER ROLE CURRENT_USER BYPASSRLS",
        "SET ROLE {owner}",
        "GRANT {owner} TO CURRENT_USER",
    ],
)
def test_the_runtime_role_cannot_change_the_schema_or_switch_rls_off(roles: Roles, statement: str) -> None:
    with psycopg.connect(roles.runtime_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(roles.schema)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(statement.format(owner=roles.owner))
        flags = connection.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = 'axentra_tenants'::regclass"
        ).fetchone()
        assert flags == (True, True)
        # It still reads the schema versions (the serving gate needs them).
        assert connection.execute("SELECT count(*) > 0 FROM nodestro_schema_migrations").fetchone() == (True,)


def test_the_runtime_role_reads_no_axentra_rows_without_a_scope(roles: Roles, linked: Any) -> None:
    _database, _link, tenants = linked
    with psycopg.connect(roles.runtime_dsn) as connection:
        connection.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(roles.schema)))
        for table in ("axentra_tenants", "axentra_products", "axentra_audit_events", "axentra_memberships"):
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone() == (
                0,
            )
        # row_security = off does not bypass anything: the query fails instead.
        connection.execute("SET row_security = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
            connection.execute("SELECT count(*) FROM axentra_tenants")
    owner = _open(roles.owner_dsn, roles.schema)
    try:  # the rows are there: the owner sees them through a platform scope
        with owner._transaction() as connection:
            connection.execute("SELECT set_config('axentra.scope', 'platform', true)")
            ids = {row[0] for row in connection.execute("SELECT id FROM axentra_tenants").fetchall()}
        assert set(tenants.values()) <= ids
    finally:
        owner.close()


def test_a_tenant_scope_reaches_only_that_tenant(linked: Any) -> None:
    from axentra.data.database import DbScope

    _database, link, tenants = linked
    product = link.services.product_id
    with link.services.platform.db.transaction(DbScope.tenant(product, tenants["alpha"])) as tx:
        assert {row["id"] for row in tx.all("SELECT id FROM axentra_tenants")} == {tenants["alpha"]}
        # A forged write into the other tenant is refused by the policy, not only by the services' filters.
        with pytest.raises(psycopg.errors.InsufficientPrivilege), tx.conn.transaction():
            tx.execute("UPDATE axentra_tenants SET name = 'x' WHERE id = %s RETURNING id", (tenants["beta"],))
            tx.execute(
                "INSERT INTO axentra_audit_events (id, product_id, tenant_id, actor_kind, action, outcome)"
                " VALUES ('aud_forged', %s, %s, 'system', 'forged', 'success')",
                (product, tenants["beta"]),
            )
        assert tx.execute("UPDATE axentra_tenants SET name = 'x' WHERE id = %s", (tenants["beta"],)) == 0


def test_axentra_refuses_the_owner_role_at_runtime(roles: Roles) -> None:
    owner = _open(roles.owner_dsn, roles.schema)
    services = roles.services(owner)
    try:
        with pytest.raises(NodestroError) as caught:
            services.start()
        assert caught.value.code == "axentra.unavailable" and "owns tables" in str(caught.value)
        # NODESTRO_AXENTRA_ALLOW_SUPERUSER=1 is the explicit opt-in (development only).
        allowed = roles.services(owner, allow_superuser=True)
        allowed.start()
        allowed.close()
    finally:
        services.close()
        owner.close()


# ---- positive: every Nodestro and Axentra path works under the runtime role --------------------------------------


def test_nodestro_stores_work_under_the_runtime_role(runtime: PostgresDatabase) -> None:
    runtime.require_current_schema()
    store = runtime.store("alpha")
    store.put("notes", "n1", Doc(id="n1", text="hello"))
    with store.transaction():
        store.lock("notes", "n1")
        store.put("notes", "n2", Doc(id="n2"))
    assert [key for key, _doc in store.items("notes", Doc)] == ["n1", "n2"]
    assert runtime.store("beta").items("notes", Doc) == []
    assert store.delete_prefix("notes", "n") == 2


def test_axentra_services_work_on_the_shared_pool(linked: Any) -> None:
    from axentra.core.context import Principal

    database, link, _tenants = linked
    platform = link.services.platform
    assert platform.db.owns_pool is False
    assert platform.db.pool.max_size == database.pool_size
    system = Principal.system("test")
    names = {tenant.name for tenant in platform.tenancy.list_tenants(system, link.services.product_id).items}
    assert {"Alpha", "Beta"} <= names
    assert link.services.schema_problems() == []  # the serving gate reads through the runtime role's pool


def test_axentra_never_starts_on_a_pool_of_its_own(roles: Roles) -> None:
    services = AxentraServices(
        AxentraConfig(enabled=True, keyring=f"k1:{_key()}"),
        database_url=roles.runtime_dsn,
        migration_url=roles.owner_dsn,
        public_origin="http://localhost:8000",
        schema=roles.schema,
    )
    try:
        with pytest.raises(NodestroError) as caught:
            services.start()
        assert caught.value.code == "axentra.unavailable" and "database pool" in str(caught.value)
        assert services.platform.db.pool.closed  # Axentra opened no pool of its own
    finally:
        services.close()


# ---- one pool: no scope crosses from one tenant to the next on the same connection --------------------------------


def _scope(connection: Any) -> tuple[str, str, str]:
    row = connection.execute(
        "SELECT coalesce(current_setting('axentra.scope', true), ''),"
        " coalesce(current_setting('axentra.product_id', true), ''),"
        " coalesce(current_setting('axentra.tenant_id', true), '')"
    ).fetchone()
    return tuple(row)  # type: ignore[return-value]


def test_one_physical_connection_carries_no_scope_between_tenants(roles: Roles, linked: Any) -> None:
    """Pool size 1: Nodestro and Axentra take turns on the very same backend; nothing of a scope survives a turn."""
    from axentra.data.database import DbScope, join

    database = roles.runtime_database(pool_size=1)
    services = roles.services(database)
    try:
        services.start()
        link = AxentraLink(services, database)
        product = services.product_id
        alpha, beta = link.tenant("alpha"), link.tenant("beta")
        assert alpha and beta
        backends: set[int] = set()
        for tenant in (alpha, beta, alpha, beta):
            with services.platform.db.transaction(DbScope.tenant(product, tenant), read_only=True) as tx:
                backends.add(tx.scalar("SELECT pg_backend_pid()"))
                assert {row["id"] for row in tx.all("SELECT id FROM axentra_tenants")} == {tenant}
            # Nodestro's next statement on the same connection: no scope, so no Axentra row is visible.
            with database._connection() as connection:
                backends.add(connection.execute("SELECT pg_backend_pid()").fetchone()[0])
                assert _scope(connection) == ("", "", "")
                assert connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            assert database._query("SELECT count(*) FROM axentra_tenants") == [(0,)]
            database.store("alpha").put("notes", "x", Doc(id="x"))
        # A failing Axentra transaction leaves nothing behind either.
        with pytest.raises(RuntimeError), services.platform.db.transaction(DbScope.tenant(product, alpha)):
            raise RuntimeError("boom")
        with database._connection() as connection:
            assert _scope(connection) == ("", "", "")
        # Joined: Nodestro's transaction, Axentra nested inside with two scopes, the host's scope afterwards.
        with link.joined() as connection:
            backends.add(connection.execute("SELECT pg_backend_pid()").fetchone()[0])
            with services.platform.db.transaction(DbScope.tenant(product, alpha)) as outer:
                assert outer.conn is connection
                with join(connection, DbScope.tenant(product, beta)) as inner:
                    assert {row["id"] for row in inner.all("SELECT id FROM axentra_tenants")} == {beta}
                    services.platform.authz.require(
                        axentra_platform_system(), "tenants:read", product, beta
                    )  # a joined savepoint in between
                    assert _scope(connection) == ("tenant", product, beta)
                assert _scope(connection) == ("tenant", product, alpha)
            assert _scope(connection) == ("", "", "")
            database.store("beta").put("notes", "joined", Doc(id="joined"))
        assert database.store("beta").get("notes", "joined", Doc) is not None
        assert backends and len(backends) == 1, backends
    finally:
        services.close()
        database.close()


def axentra_platform_system() -> Any:
    from axentra.core.context import Principal

    return Principal.system("test")


def test_threads_on_a_shared_pool_never_see_another_tenant(roles: Roles, linked: Any) -> None:
    """Six threads, two connections: Axentra reads in alternating tenant scopes mixed with Nodestro reads and writes.
    Every Axentra read sees exactly its tenant, and the process never opens more than its pool (+1 session lock)."""
    from axentra.data.database import DbScope

    database = roles.runtime_database(pool_size=2)
    services = roles.services(database)
    errors: list[BaseException] = []
    backends: set[int] = set()
    try:
        services.start()
        link = AxentraLink(services, database)
        product = services.product_id
        scopes = {"alpha": link.tenant("alpha"), "beta": link.tenant("beta")}

        def work(tenant: str) -> None:
            try:
                store = database.store(tenant)
                for index in range(20):
                    with services.platform.db.transaction(
                        DbScope.tenant(product, scopes[tenant]), read_only=True
                    ) as tx:
                        backends.add(tx.scalar("SELECT pg_backend_pid()"))
                        seen = {row["id"] for row in tx.all("SELECT id FROM axentra_tenants")}
                        assert seen == {scopes[tenant]}, seen
                    store.put("load", f"{threading.get_ident()}-{index}", Doc(id=str(index)))
                    assert database._query("SELECT count(*) FROM axentra_tenants") == [(0,)]
                    backends.add(database._query("SELECT pg_backend_pid()")[0][0])
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(tenant,)) for tenant in ("alpha", "beta") * 3]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert errors == []
        stats = database._pool.get_stats()
        assert stats["pool_max"] == 2 and stats.get("pool_size", 0) <= 2
        assert 1 <= len(backends) <= 2, backends  # Nodestro and Axentra together: one budget of two connections
    finally:
        services.close()
        database.close()
