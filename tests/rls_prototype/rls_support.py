"""Set-up of the A9 RLS prototype database (MUP-ONA MP-A9, test databases only; not merged).

``A9_RLS_ADMIN`` is a superuser connection string of a TEST server (its ``postgres`` database). :func:`prepare`
creates a new database (default ``a9_rls``; only names starting with ``a9_`` are accepted, so no existing database
is ever touched) owned by the role ``a9_owner`` (no superuser, no ``BYPASSRLS``), restores a legacy fixture
(FX-PG-0 or FX-PG-0v) into it, hands the restored tables to ``a9_owner``, applies Axentra's migrations as the owner
(Axentra's tables live in the same database, as on a server with ``NODESTRO_AXENTRA=on``) and grants the runtime
role ``a9_runtime`` (no superuser, no ``BYPASSRLS``, owns nothing) its data privileges with the runtime's own
:func:`~nodestro_runtime.storage_postgres.grant_runtime_role`.

The policies are ``policies.sql`` (applied by :func:`apply_policies` as the owner) and removed again by
``drop_policies.sql``. Neither is a migration.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

HERE = Path(__file__).resolve().parent
ADMIN = os.environ.get("A9_RLS_ADMIN", "")
DATABASE = os.environ.get("A9_RLS_DATABASE", "a9_rls")
OWNER, OWNER_PASSWORD = "a9_owner", "a9-owner-pw"
RUNTIME, RUNTIME_PASSWORD = "a9_runtime", "a9-runtime-pw"
#: The tables policies.sql covers (knowledge and memory tables of migrations 0002 and 0010, plus documents).
POLICY_TABLES = (
    "nodestro_documents",
    "nodestro_files",
    "nodestro_chunks",
    "nodestro_memory_files",
    "nodestro_vector_spaces",
    "nodestro_chunk_vectors",
)
#: The tenant setting the policies read (the hook name of orkexa's PsycopgSource and Nodestro's DatabaseSource).
SETTING = "app.tenant_id"


@dataclass(frozen=True)
class Setup:
    admin: str  # superuser, in the prototype database
    owner: str  # a9_owner, in the prototype database
    runtime: str  # a9_runtime, in the prototype database
    database: str


def _dsn(base: str, **changes: str) -> str:
    parts = conninfo_to_dict(base)
    parts.update(changes)
    return make_conninfo("", **parts)


def _ensure_role(admin: psycopg.Connection, role: str, password: str) -> None:
    if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    flags = admin.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    assert flags == (False, False), f"{role} must be no superuser and have no BYPASSRLS: {flags}"


def setup_for(admin_dsn: str = ADMIN, database: str = DATABASE) -> Setup:
    return Setup(
        admin=_dsn(admin_dsn, dbname=database),
        owner=_dsn(admin_dsn, dbname=database, user=OWNER, password=OWNER_PASSWORD),
        runtime=_dsn(admin_dsn, dbname=database, user=RUNTIME, password=RUNTIME_PASSWORD),
        database=database,
    )


def prepare(dump: str, admin_dsn: str = ADMIN, database: str = DATABASE) -> Setup:
    """A new prototype database with ``dump`` restored (see the module docstring)."""
    import postgres_support  # tests/postgres_support.py (restore_dump of the legacy fixtures)

    from nodestro_runtime.storage_postgres import PostgresDatabase, grant_runtime_role

    if not admin_dsn:
        raise RuntimeError("A9_RLS_ADMIN is not set")
    if not database.startswith("a9_"):
        raise RuntimeError(f"Refusing database {database!r}: the prototype only uses its own a9_* databases")
    setup = setup_for(admin_dsn, database)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        _ensure_role(admin, OWNER, OWNER_PASSWORD)
        _ensure_role(admin, RUNTIME, RUNTIME_PASSWORD)
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
        admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(OWNER)))
    postgres_support.restore_dump(setup.admin, dump)
    with psycopg.connect(setup.admin, autocommit=True) as admin:
        tables = [row[0] for row in admin.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
        for table in tables:
            admin.execute(
                sql.SQL("ALTER TABLE public.{} OWNER TO {}").format(sql.Identifier(table), sql.Identifier(OWNER))
            )
        admin.execute(
            sql.SQL("ALTER FUNCTION public.nodestro_created_order(text) OWNER TO {}").format(sql.Identifier(OWNER))
        )
    from axentra.data.migrations import MigrationRunner

    MigrationRunner(setup.owner).migrate()
    owner = PostgresDatabase(setup.owner, pool_size=2)
    try:
        owner.require_current_schema()
        report = grant_runtime_role(owner, RUNTIME)
        assert report["restricted"], report
    finally:
        owner.close()
    return setup


def _run_file(conninfo: str, name: str) -> None:
    with psycopg.connect(conninfo) as connection:
        connection.execute((HERE / name).read_text())  # one transaction; committed on exit


def apply_policies(setup: Setup) -> None:
    _run_file(setup.owner, "policies.sql")


def drop_policies(setup: Setup) -> None:
    _run_file(setup.owner, "drop_policies.sql")


def rls_state(conninfo: str) -> dict[str, tuple[bool, bool, int]]:
    """``table -> (row security enabled, forced, number of policies)`` of every table of the public schema."""
    with psycopg.connect(conninfo, autocommit=True) as connection:
        rows = connection.execute(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
            " (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)"
            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY c.relname"
        ).fetchall()
    return {str(name): (bool(enabled), bool(forced), int(count)) for name, enabled, forced, count in rows}
