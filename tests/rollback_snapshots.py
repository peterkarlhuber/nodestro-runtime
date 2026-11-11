"""Snapshot tools for rollback evidence (TABRION QA-13): what the MIG-NOD steps of the Tabrion adoption must keep.

The server-profile steps of the Tabrion plan (MIG-NOD-3 error texts, MIG-NOD-4 session configuration, MIG-NOD-5
roles, MIG-NOD-7 extension status; document 06, document 11 section 3.2) replace generic code of
``storage_postgres.py`` with Tabrion's. Their rollback is a revert plus the pin cascade, verified by "snapshots
identical". These tools write the "before" picture from the old code:

``catalog``
    Row-level security and the runtime role's privileges on every table of the schema after
    :func:`~nodestro_runtime.storage_postgres.grant_runtime_role` (Nodestro's and Axentra's migrations run as the
    owner, as on a server): Nodestro's tables and, per table, Axentra's (names, RLS and FORCE flags, privileges,
    counts), every policy, every function (owner, ``SECURITY DEFINER``, ``proconfig``, runtime ``EXECUTE``), the
    schema and database privileges (including ``TEMPORARY``), the default privileges, every access control entry of
    the schema's objects (``aclexplode`` of ``relacl``, ``attacl``, ``proacl``, ``nspacl``: grantor, grantee including
    ``PUBLIC``, privilege, grant option), the database ACL entries of the run's roles and their memberships. Axentra's
    tables are named, so an Axentra pin that changes them rewrites this file (a reviewed diff, like a migration).
``roles``
    The role decision table: eight role variants through ``inspect_role``, ``grant_runtime_role`` and
    ``PostgresDatabase.role_info`` (decision, error code, parameters and message).
``errors``
    The error answers (``NodestroError`` code, parameters, message) of the database paths: migration failures, a
    failing backup, a changed or missing migration, a too new schema, a missing role, invalid settings; whether a
    canary value of the server's ``DETAIL`` reaches the message (finding B-06-2, closed by MIG-NOD-3, which
    rewrites this file on purpose), and the JSON schema of :class:`~nodestro_runtime.problems.Problem`.
``settings``
    The ``SHOW`` extract: the session settings of each kind of connection (pooled transaction, after it, the session
    lock connection, the pool lent to Axentra with an Axentra transaction, ``DatabaseSource`` with a tenant
    setting), as the difference to a plain connection of the same role.
``schema``
    A schema dump of Nodestro's tables from the catalog (columns, constraints, indexes, triggers, functions, applied
    migrations), without server version or extension objects (pgvector), so it is the same with and without
    pgvector and across PostgreSQL versions.

Every run creates a schema and roles of its own (names start with ``--prefix``, ``[a-z][a-z0-9_]{0,19}``; the login
roles get a random password per run, never a fixed one) in the given database, drops them again, and writes names as
placeholders (``<schema>``, ``<owner>``, ``<runtime>``, ``<role:superuser>``), so two runs write identical files
(``tests/test_rollback_snapshots.py``). The golden files in ``tests/fixtures/rollback/`` are from the old code
(``tests/fixtures/rollback/SOURCE.md``). Usage, from the repository root, with a superuser connection string (as
``NODESTRO_TEST_POSTGRES``)::

    python tests/rollback_snapshots.py --dsn "host=127.0.0.1 user=postgres password=... dbname=postgres" check
    python tests/rollback_snapshots.py --dsn ... write            # rewrite the golden files
    python tests/rollback_snapshots.py --dsn ... write --dir out  # write elsewhere (e.g. the old SHA's checkout)

``check`` prints a unified diff per snapshot that differs and exits with 1. A step that changes a snapshot on purpose
rewrites the golden file in the same pull request, so the diff is reviewed; a new migration does the same. A run that
was killed (SIGKILL, CI timeout) cannot drop its objects; ``check`` and ``write`` report such leftovers of the same
prefix first, and ``cleanup`` drops them::

    python tests/rollback_snapshots.py --dsn ... cleanup          # leftovers of crashed runs (own prefix only)

``cleanup`` matches only the exact names a run creates (``<prefix><8 hex>``, ``..._scratch``, ``..._roles``,
``..._owner``, ``..._runtime``, ``..._v_<variant>``), in the connected database; do not run it while another run with
the same prefix is still going.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
import difflib
import json
import os
from pathlib import Path
import re
import secrets
import sys
from typing import Any
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "rollback"
NAMES = ("catalog", "roles", "errors", "settings", "schema")
DEFAULT_PREFIX = "nodestro_ev_"
#: Allowed prefixes: short enough that no generated name exceeds PostgreSQL's 63 bytes (L-2 of the QA-13 review).
_PREFIX = re.compile(r"[a-z][a-z0-9_]{0,19}")
#: The canary value of the error snapshot: it must never be part of a message once B-06-2 is closed.
CANARY = "canary-qa13@example.com"
#: The tenant of the transactions of the settings snapshot (any id: no row is read).
TENANT_ID = "00000000-0000-4000-8000-000000000002"
PRODUCT_ID = "00000000-0000-4000-8000-000000000001"
ROLE_VARIANTS = (
    "restricted",
    "superuser",
    "bypass_rls",
    "superuser_bypass_rls",
    "table_owner",
    "owner_member",
    "connecting_owner",
    "missing",
)


def validate_prefix(prefix: str) -> str:
    """``prefix`` when it is a valid run prefix, else :class:`ValueError`."""
    if not _PREFIX.fullmatch(prefix):
        raise ValueError(f"invalid prefix {prefix!r}: expected [a-z][a-z0-9_]{{0,19}}")
    return prefix


def _run_names(prefix: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """The exact schema and role names a run with ``prefix`` creates."""
    base = re.escape(prefix) + r"[0-9a-f]{8}"
    variants = "|".join(re.escape(v) for v in ROLE_VARIANTS)
    return re.compile(base + r"(?:_scratch|_roles)?"), re.compile(base + rf"_(?:owner|runtime|v_(?:{variants}))")


def leftovers(admin_dsn: str, prefix: str = DEFAULT_PREFIX, bases: list[str] | None = None) -> list[str]:
    """Schemas (of the connected database) and roles of runs with ``prefix`` that still exist (only ``bases``'s)."""
    schema_name, role_name = _run_names(validate_prefix(prefix))
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        found = [
            f"schema {r[0]}"
            for r in _rows(conn, "SELECT nspname::text FROM pg_namespace")
            if schema_name.fullmatch(r[0])
        ]
        found += [
            f"role {r[0]}" for r in _rows(conn, "SELECT rolname::text FROM pg_roles") if role_name.fullmatch(r[0])
        ]
    if bases is not None:
        found = [item for item in found if any(item.split(" ", 1)[1].startswith(b) for b in bases)]
    return sorted(found)


def cleanup(admin_dsn: str, prefix: str = DEFAULT_PREFIX) -> list[str]:
    """Drop the leftovers of crashed runs with ``prefix`` (schemas first, then roles); what was dropped."""
    left = leftovers(admin_dsn, prefix)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        for item in left:
            kind, name = item.split(" ", 1)
            if kind == "schema":
                conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))
        for item in left:
            kind, name = item.split(" ", 1)
            if kind == "role":
                conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
    return left


def with_user(dsn: str, user: str, password: str) -> str:
    parts = conninfo_to_dict(dsn)
    parts.update(user=user, password=password)
    return make_conninfo("", **parts)


@dataclass
class EvidenceSchema:
    """A schema of its own with Nodestro's and Axentra's migrations run as the owner and a granted runtime role."""

    admin_dsn: str
    base: str
    names: dict[str, str] = field(default_factory=dict)
    #: The password of every login role of this run: random per run, never stored (M-2 of the QA-13 review).
    password: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)

    @property
    def schema(self) -> str:
        return self.base

    @property
    def owner(self) -> str:
        return f"{self.base}_owner"

    @property
    def runtime(self) -> str:
        return f"{self.base}_runtime"

    @property
    def schemas(self) -> tuple[str, ...]:
        """The run's schema, a scratch schema (error snapshot) and a schema with Nodestro's tables only (roles)."""
        return (self.schema, f"{self.base}_scratch", f"{self.base}_roles")

    def variant(self, name: str) -> str:
        return f"{self.base}_v_{name}"

    def dsn(self, role: str) -> str:
        return with_user(self.admin_dsn, role, self.password)

    def normalize(self, value: Any) -> Any:
        if isinstance(value, str):
            for name, placeholder in sorted(self.names.items(), key=lambda item: -len(item[0])):
                value = value.replace(name, placeholder)
            return value
        if isinstance(value, dict):
            return {self.normalize(k): self.normalize(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.normalize(v) for v in value]
        return value


def _open(dsn: str, schema: str, **kwargs: Any) -> Any:
    from nodestro_runtime.storage_postgres import PostgresDatabase

    return PostgresDatabase(dsn, schema=schema, **kwargs)


def _key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


@contextmanager
def evidence_schema(
    admin_dsn: str, prefix: str = DEFAULT_PREFIX, bases: list[str] | None = None
) -> Iterator[EvidenceSchema]:
    """Create the roles and schema, migrate as the owner, grant the runtime role; drop everything afterwards.

    The run's base name is appended to ``bases`` when given (the tests check that nothing of it is left)."""
    from nodestro_runtime.axentra_platform import AxentraConfig, AxentraServices
    from nodestro_runtime.storage_postgres import grant_runtime_role

    ev = EvidenceSchema(admin_dsn, f"{validate_prefix(prefix)}{uuid.uuid4().hex[:8]}")
    if bases is not None:
        bases.append(ev.base)
    ev.names = {ev.owner: "<owner>", ev.runtime: "<runtime>", ev.schema: "<schema>"}
    ev.names.update({ev.variant(v): f"<role:{v}>" for v in ROLE_VARIANTS})
    ev.names.update(
        {
            f"{ev.base}_owned": "<owned_table>",
            f"{ev.base}_scratch": "<scratch_schema>",
            f"{ev.base}_roles": "<roles_schema>",
        }
    )
    ev.names.update({PRODUCT_ID: "<product_id>", TENANT_ID: "<tenant_id>"})
    roles = [ev.owner, ev.runtime]
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            for role in roles:
                admin.execute(
                    sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(ev.password)
                    )
                )
            for schema in ev.schemas:
                admin.execute(
                    sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                        sql.Identifier(schema), sql.Identifier(ev.owner)
                    )
                )
        owner = _open(ev.dsn(ev.owner), ev.schema)
        try:
            owner.migrate()
            services = AxentraServices(
                AxentraConfig(enabled=True, keyring=f"k1:{_key()}"),
                database_url=ev.dsn(ev.owner),
                public_origin="http://localhost:8000",
                schema=ev.schema,
            )
            try:
                services.migrate()
            finally:
                services.close()
            report = grant_runtime_role(owner, ev.runtime)
            if not report["restricted"]:
                raise RuntimeError(f"the runtime role is not restricted: {report}")
        finally:
            owner.close()
        yield ev
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            for schema in ev.schemas:
                admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            for role in [*(ev.variant(v) for v in ROLE_VARIANTS), *roles]:
                exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
                if exists:
                    admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                    admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def _rows(conn: psycopg.Connection[Any], query: str, params: Any = None) -> list[tuple[Any, ...]]:
    return list(conn.execute(query, params).fetchall())


def _is_nodestro(name: str) -> bool:
    return name.startswith("nodestro_")


# -- catalog -------------------------------------------------------------------------------------------------------


def catalog_snapshot(ev: EvidenceSchema) -> dict[str, Any]:
    role = ev.runtime
    with psycopg.connect(ev.admin_dsn, autocommit=True) as conn:
        nodestro: list[dict[str, Any]] = []
        axentra: list[dict[str, Any]] = []
        for name, kind, rls, force, owner, oid in _rows(
            conn,
            "SELECT c.relname, c.relkind::text, c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner),"
            " c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f') ORDER BY c.relname",
            (ev.schema,),
        ):
            privileges = [
                p
                for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
                if _rows(conn, "SELECT has_table_privilege(%s, %s::oid, %s)", (role, oid, p))[0][0]
            ]
            entry = {
                "table": name,
                "kind": kind,
                "row_security": rls,
                "force_row_security": force,
                "owner": owner,
                "runtime_privileges": privileges,
            }
            if _is_nodestro(name):
                nodestro.append(entry)
            elif name == "axentra_schema_migrations":
                nodestro.append({"table": name, "owner": owner, "runtime_privileges": privileges})
            else:
                axentra.append(entry)
        policies = [
            {"table": t, "policy": p, "permissive": perm, "roles": sorted(r), "command": c, "using": q, "with_check": w}
            for t, p, perm, r, c, q, w in _rows(
                conn,
                "SELECT tablename::text, policyname::text, permissive, roles::text[], cmd, qual, with_check"
                " FROM pg_policies WHERE schemaname = %s ORDER BY tablename, policyname",
                (ev.schema,),
            )
        ]
        functions = [
            {
                "function": f"{name}({args})",
                "owner": owner,
                "security_definer": secdef,
                "config": sorted(config or []),
                "runtime_execute": execute,
            }
            for name, args, owner, secdef, config, execute in _rows(
                conn,
                "SELECT p.proname, pg_get_function_identity_arguments(p.oid), pg_get_userbyid(p.proowner),"
                " p.prosecdef, p.proconfig, has_function_privilege(%s, p.oid, 'EXECUTE')"
                " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s"
                " AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass"
                "   AND d.objid = p.oid AND d.deptype = 'e')"
                " ORDER BY 1, 2",
                (role, ev.schema),
            )
        ]
        sequences = sorted(
            {
                tuple(
                    p
                    for p in ("USAGE", "SELECT", "UPDATE")
                    if _rows(conn, "SELECT has_sequence_privilege(%s, %s::oid, %s)", (role, oid, p))[0][0]
                )
                for (oid,) in _rows(
                    conn,
                    "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = %s AND c.relkind = 'S'",
                    (ev.schema,),
                )
            }
        )
        defaults = sorted(
            list(row)
            for row in _rows(
                conn,
                "SELECT pg_get_userbyid(d.defaclrole), d.defaclobjtype::text,"
                " CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END, a.privilege_type,"
                " a.is_grantable FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace,"
                " LATERAL aclexplode(d.defaclacl) a WHERE n.nspname = %s",
                (ev.schema,),
            )
        )
        schema = {
            p.lower(): _rows(conn, "SELECT has_schema_privilege(%s, %s, %s)", (role, ev.schema, p))[0][0]
            for p in ("USAGE", "CREATE")
        }
        database = {
            p.lower(): _rows(conn, "SELECT has_database_privilege(%s, current_database(), %s)", (role, p))[0][0]
            for p in ("CONNECT", "CREATE", "TEMPORARY")
        }
        run_roles = [ev.owner, ev.runtime]
        database_acl = sorted(
            list(row)
            for row in _rows(
                conn,
                "SELECT pg_get_userbyid(a.grantee), a.privilege_type, a.is_grantable FROM pg_database d,"
                " LATERAL aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a"
                " WHERE d.datname = current_database() AND pg_get_userbyid(a.grantee) = ANY(%s)",
                (run_roles,),
            )
        )
        memberships = sorted(
            list(row)
            for row in _rows(
                conn,
                "SELECT pg_get_userbyid(m.roleid), pg_get_userbyid(m.member), m.admin_option FROM pg_auth_members m"
                " WHERE pg_get_userbyid(m.member) = ANY(%s) OR pg_get_userbyid(m.roleid) = ANY(%s)",
                (run_roles, run_roles),
            )
        )
        acl = _acl_entries(conn, ev.schema)
    return ev.normalize(
        {
            "nodestro_tables": nodestro,
            "axentra_tables": axentra,
            "axentra_summary": {
                "tables": len(axentra),
                "row_security": sum(1 for t in axentra if t["row_security"]),
                "force_row_security": sum(1 for t in axentra if t["force_row_security"]),
            },
            "policies": policies,
            "functions": functions,
            "sequence_privilege_sets": [list(s) for s in sequences],
            "default_privileges": defaults,
            "runtime_role": {
                "schema": schema,
                "database": database,
                "database_acl": database_acl,
                "memberships": memberships,
            },
            "acl": acl,
        }
    )


#: Every access control entry of the schema's objects; ``NULL`` ACLs are read as their default (``acldefault``). An
#: owner's entries for its own object (grantor = grantee = owner) are left out: they are implicit, and PostgreSQL 17
#: adds ``MAINTAIN`` to them. Extension members (pgvector) are left out, so the file is the same with and without it.
_ACL_SQL = """
SELECT kind, object, grantor, grantee, privilege, grantable FROM (
  SELECT CASE WHEN c.relkind = 'S' THEN 'sequence' ELSE 'relation' END AS kind, c.relname::text AS object,
         c.relowner AS owner, a.grantor, a.grantee, a.privilege_type AS privilege, a.is_grantable AS grantable
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace,
         LATERAL aclexplode(coalesce(c.relacl, acldefault(CASE WHEN c.relkind = 'S' THEN 's' ELSE 'r' END::"char",
                                                          c.relowner))) a
   WHERE n.nspname = %(schema)s AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
     AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                     AND d.deptype = 'e')
  UNION ALL
  SELECT 'column', c.relname || '.' || at.attname, c.relowner, a.grantor, a.grantee, a.privilege_type,
         a.is_grantable
    FROM pg_attribute at JOIN pg_class c ON c.oid = at.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace,
         LATERAL aclexplode(at.attacl) a
   WHERE n.nspname = %(schema)s AND at.attacl IS NOT NULL AND at.attnum > 0 AND NOT at.attisdropped
  UNION ALL
  SELECT 'function', p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')', p.proowner,
         a.grantor, a.grantee, a.privilege_type, a.is_grantable
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace,
         LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
   WHERE n.nspname = %(schema)s
     AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid
                     AND d.deptype = 'e')
  UNION ALL
  SELECT 'schema', '<schema>', n.nspowner, a.grantor, a.grantee, a.privilege_type, a.is_grantable
    FROM pg_namespace n, LATERAL aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a
   WHERE n.nspname = %(schema)s
) e
WHERE NOT (e.grantee = e.owner AND e.grantor = e.owner)
"""


def _acl_entries(conn: psycopg.Connection[Any], schema: str) -> list[list[Any]]:
    """``[kind, object, grantor, grantee, privilege, grantable]`` of every entry; ``PUBLIC`` and ``<admin>`` named."""
    admin = str(_rows(conn, "SELECT current_user")[0][0])
    names: dict[int, str] = {0: "PUBLIC"}

    def name(oid: int) -> str:
        if oid not in names:
            found = str(_rows(conn, "SELECT pg_get_userbyid(%s)", (oid,))[0][0])
            names[oid] = "<admin>" if found == admin else found
        return names[oid]

    return sorted(
        [kind, obj, name(grantor), name(grantee), privilege, grantable]
        for kind, obj, grantor, grantee, privilege, grantable in _rows(conn, _ACL_SQL, {"schema": schema})
    )


# -- role decision table --------------------------------------------------------------------------------------------


def _outcome(call: Callable[[], Any]) -> Any:
    try:
        result = call()
    except Exception as exc:
        return {
            "refused": type(exc).__name__,
            "code": getattr(exc, "code", None),
            "params": getattr(exc, "params", None),
            "message": str(exc),
        }
    if isinstance(result, dict) and "database" in result:
        result = {**result, "database": "<database>"}  # the test server's database name
    return {"ok": result}


def _create_variant(ev: EvidenceSchema, name: str) -> str:
    if name == "connecting_owner":
        return ev.owner
    role = ev.variant(name)
    if name == "missing":
        return role
    attributes = {
        "restricted": "NOSUPERUSER NOBYPASSRLS",
        "superuser": "SUPERUSER NOBYPASSRLS",
        "bypass_rls": "NOSUPERUSER BYPASSRLS",
        "superuser_bypass_rls": "SUPERUSER BYPASSRLS",
        "table_owner": "NOSUPERUSER NOBYPASSRLS",
        "owner_member": "NOSUPERUSER NOBYPASSRLS",
    }[name]
    with psycopg.connect(ev.admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN " + attributes + " PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(ev.password)
            )
        )
        if name == "owner_member":
            admin.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(ev.owner), sql.Identifier(role)))
        if name == "table_owner":
            schema = f"{ev.base}_roles"
            table = sql.Identifier(schema, f"{ev.base}_owned")
            admin.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role))
            )
            admin.execute(sql.SQL("CREATE TABLE {} (id integer)").format(table))
            admin.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(table))
            admin.execute(sql.SQL("ALTER TABLE {} OWNER TO {}").format(table, sql.Identifier(role)))
    return role


def roles_snapshot(ev: EvidenceSchema) -> dict[str, Any]:
    """The decision table of the eight role variants (adds roles and a table: runs after the other snapshots).

    It runs on a schema with Nodestro's tables only, so the table names and counts in it do not change with the
    Axentra pin."""
    from nodestro_runtime.storage_postgres import grant_runtime_role, inspect_role

    table: dict[str, Any] = {}
    schema = f"{ev.base}_roles"
    owner = _open(ev.dsn(ev.owner), schema, pool_size=1)
    try:
        owner.migrate()
        for name in ROLE_VARIANTS:
            role = _create_variant(ev, name)
            row = {
                "inspect_role": _outcome(lambda: inspect_role(owner, role)),  # noqa: B023 - called at once
                "grant_runtime_role": _outcome(lambda: grant_runtime_role(owner, role)),  # noqa: B023
            }
            if name != "missing":
                database = _open(ev.dsn(role), schema, pool_size=1)
                try:
                    row["role_info"] = _outcome(database.role_info)
                finally:
                    database.close()
            table[name] = row
    finally:
        owner.close()
    return ev.normalize({"variants": list(ROLE_VARIANTS), "table": table})


# -- error answers ----------------------------------------------------------------------------------------------------


def _error(call: Callable[[], Any]) -> dict[str, Any]:
    from nodestro_runtime.problems import NodestroError

    try:
        call()
    except NodestroError as exc:
        return {
            "code": exc.code,
            "params": exc.params,
            "message": str(exc),
            "message_has_canary": CANARY in str(exc),
            "problem_fields": sorted(exc.problem.model_dump()),
        }
    raise AssertionError("no NodestroError was raised")


def errors_snapshot(ev: EvidenceSchema) -> dict[str, Any]:
    """The error answers of the database paths, on a scratch schema of the run (as the owner)."""
    from nodestro_runtime.problems import Problem
    from nodestro_runtime.storage_postgres import inspect_role

    scratch = f"{ev.base}_scratch"
    first = ("0001_canary", "CREATE TABLE nodestro_canary (email text UNIQUE)")

    def unique_violation() -> str:
        return f"INSERT INTO nodestro_canary VALUES ('{CANARY}'); INSERT INTO nodestro_canary VALUES ('{CANARY}')"

    def failing_backup(current: int, target: int) -> None:
        with psycopg.connect(ev.dsn(ev.owner), autocommit=True) as conn:
            conn.execute("CREATE TEMPORARY TABLE canary_backup (email text UNIQUE)")
            conn.execute(f"INSERT INTO canary_backup VALUES ('{CANARY}')")
            conn.execute(f"INSERT INTO canary_backup VALUES ('{CANARY}')")

    found: dict[str, Any] = {}
    database = _open(ev.dsn(ev.owner), scratch, pool_size=1)
    try:
        found["migration_failed"] = _error(
            lambda: database.migrate(migrations={1: ("0001_canary", first[1] + "; " + unique_violation())})
        )
        database.migrate(migrations={1: first})
        second = {1: first, 2: ("0002_next", "SELECT 1")}
        found["backup_required"] = _error(lambda: database.migrate(migrations=second))
        found["backup_failed"] = _error(lambda: database.migrate(migrations=second, before_migration=failing_backup))
        found["migration_failed_after_backup"] = _error(
            lambda: database.migrate(
                migrations={1: first, 2: ("0002_next", unique_violation())}, before_migration=lambda c, t: None
            )
        )
        found["migration_changed"] = _error(
            lambda: database.migrate(migrations={1: ("0001_canary", "CREATE TABLE nodestro_other (id int)")})
        )
        found["migration_missing"] = _error(lambda: database.migrate(migrations={1: first, 3: ("0003_x", "SELECT 1")}))
        found["schema_too_new"] = _error(lambda: database.migrate(migrations={}))
        found["schema_mismatch"] = _error(database.require_current_schema)
        found["role_missing"] = _error(lambda: inspect_role(database, ev.variant("missing")))
    finally:
        database.close()
    previous = os.environ.get("NODESTRO_DATABASE_STATEMENT_TIMEOUT")
    os.environ["NODESTRO_DATABASE_STATEMENT_TIMEOUT"] = "soon"
    try:
        found["settings_invalid"] = _error(lambda: _open(ev.dsn(ev.owner), scratch))
    finally:
        if previous is None:
            del os.environ["NODESTRO_DATABASE_STATEMENT_TIMEOUT"]
        else:
            os.environ["NODESTRO_DATABASE_STATEMENT_TIMEOUT"] = previous
    return ev.normalize({"canary": CANARY, "errors": found, "problem_schema": Problem.model_json_schema()})


# -- session settings (SHOW extract) ----------------------------------------------------------------------------------


def _show_all(conn: Any) -> dict[str, str]:
    from psycopg.rows import tuple_row

    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT name, setting FROM pg_settings")
        return {str(row[0]): str(row[1]) for row in cur.fetchall()}


SCOPE_SETTINGS = ("axentra.scope", "axentra.product_id", "axentra.tenant_id", "app.tenant_id")


def _state(conn: Any, plain: dict[str, str]) -> dict[str, Any]:
    from psycopg.rows import tuple_row

    status = conn.info.transaction_status.name
    current = _show_all(conn)
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(
            "SELECT " + ", ".join("coalesce(current_setting(%s, true), '')" for _ in SCOPE_SETTINGS), SCOPE_SETTINGS
        )
        scope = dict(zip(SCOPE_SETTINGS, (str(v) for v in cur.fetchone() or ()), strict=True))
    return {
        "differs_from_plain": {k: current[k] for k in sorted(current) if plain.get(k) != current[k]},
        "scope": scope,
        "transaction_status": status,
    }


def settings_snapshot(ev: EvidenceSchema) -> dict[str, Any]:
    """The ``SHOW`` extract of each kind of connection (as the runtime role, default limits)."""
    from axentra.data.database import Database, DbScope

    from nodestro_runtime.storage_postgres import DatabaseSource

    dsn = ev.dsn(ev.runtime)
    with psycopg.connect(dsn, autocommit=True) as conn:
        plain = _show_all(conn)
    kinds: dict[str, Any] = {}
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("NODESTRO_DATABASE_")}
    try:
        database = _open(dsn, ev.schema, pool_size=1)
        try:
            with database._transaction() as conn:
                inside = _state(conn, plain)
            with database._connection() as conn:
                kinds["transaction"] = {"inside": inside, "after": _state(conn, plain)}
            key = secrets.randbits(62)  # any key: not part of the snapshot
            if not database.try_session_lock(key):
                raise RuntimeError("the session lock was not taken")
            try:
                kinds["session_lock"] = {"held": _state(database._session, plain)}
            finally:
                database.release_session_lock(key)
            source = DatabaseSource(database, tenant_setting="app.tenant_id")
            with source.transaction("transaction", tenant_id=TENANT_ID) as conn:
                inside = _state(conn, plain)
            with database._connection() as conn:
                kinds["database_source_tenant"] = {"inside": inside, "after": _state(conn, plain)}
            axentra = Database.on_pool(database.shared_pool())
            with axentra.transaction(DbScope.tenant(PRODUCT_ID, TENANT_ID)) as tx:
                inside = _state(tx.conn, plain)
            with database._connection() as conn:
                kinds["shared_pool_axentra"] = {"inside": inside, "after": _state(conn, plain)}
        finally:
            database.close()
        unscoped = _open(dsn, None, pool_size=1)
        try:
            with unscoped._connection() as conn:
                kinds["pool_without_schema"] = {"idle": _state(conn, plain)}
        finally:
            unscoped.close()
    finally:
        os.environ.update(saved)
    return ev.normalize({"kinds": kinds})


# -- schema dump ---------------------------------------------------------------------------------------------------


def schema_snapshot(ev: EvidenceSchema) -> dict[str, Any]:
    """Nodestro's tables of the run's schema from the catalog (extension objects left out)."""
    s = ev.schema
    with psycopg.connect(ev.admin_dsn, autocommit=True) as conn:
        relations: dict[str, Any] = {}
        for name, kind in _rows(
            conn,
            "SELECT c.relname, c.relkind::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') AND NOT EXISTS (SELECT 1 FROM"
            " pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')"
            " ORDER BY c.relname",
            (s,),
        ):
            if _is_nodestro(name):
                relations[name] = {"kind": kind}
        for table, column, typ, notnull, default, identity, generated in _rows(
            conn,
            "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,"
            " pg_get_expr(ad.adbin, ad.adrelid), a.attidentity::text, a.attgenerated::text"
            " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
            " LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum"
            " WHERE n.nspname = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY c.relname, a.attnum",
            (s,),
        ):
            if table in relations:
                relations[table].setdefault("columns", []).append(
                    {
                        "name": column,
                        "type": typ,
                        "not_null": notnull,
                        "default": default,
                        "identity": identity,
                        "generated": generated,
                    }
                )
        for table, name, kind, definition in _rows(
            conn,
            "SELECT c.relname, k.conname, k.contype::text, pg_get_constraintdef(k.oid) FROM pg_constraint k"
            " JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
            # NOT NULL constraints are catalogued from PostgreSQL 18 on (contype 'n'); ``columns`` holds them already.
            " WHERE n.nspname = %s AND k.contype <> 'n' ORDER BY c.relname, k.conname",
            (s,),
        ):
            if table in relations:
                relations[table].setdefault("constraints", []).append(
                    {"name": name, "type": kind, "definition": definition}
                )
        for table, name, definition in _rows(
            conn,
            "SELECT tablename::text, indexname::text, indexdef FROM pg_indexes WHERE schemaname = %s"
            " ORDER BY tablename, indexname",
            (s,),
        ):
            if table in relations:
                relations[table].setdefault("indexes", []).append({"name": name, "definition": definition})
        for table, definition in _rows(
            conn,
            "SELECT c.relname, pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND NOT t.tgisinternal"
            " ORDER BY c.relname, t.tgname",
            (s,),
        ):
            if table in relations:
                relations[table].setdefault("triggers", []).append(definition)
        functions = [
            {"function": f"{name}({args})", "returns": returns, "language": lang, "volatility": vol, "body_md5": body}
            for name, args, returns, lang, vol, body in _rows(
                conn,
                "SELECT p.proname, pg_get_function_identity_arguments(p.oid), pg_get_function_result(p.oid),"
                " l.lanname, p.provolatile::text, md5(p.prosrc) FROM pg_proc p"
                " JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang"
                " WHERE n.nspname = %s AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass"
                " AND d.objid = p.oid AND d.deptype = 'e') ORDER BY 1, 2",
                (s,),
            )
            if _is_nodestro(name)
        ]
        migrations = [
            {"version": v, "name": n, "checksum": c}
            for v, n, c in _rows(
                conn,
                sql.SQL("SELECT version, name, checksum FROM {}.nodestro_schema_migrations ORDER BY version")
                .format(sql.Identifier(s))
                .as_string(conn),
            )
        ]
    return ev.normalize({"relations": relations, "functions": functions, "migrations": migrations})


# -- running -------------------------------------------------------------------------------------------------------

#: The role decision table adds roles and a table, so it runs last.
COLLECTORS: dict[str, Callable[[EvidenceSchema], dict[str, Any]]] = {
    "catalog": catalog_snapshot,
    "schema": schema_snapshot,
    "settings": settings_snapshot,
    "errors": errors_snapshot,
    "roles": roles_snapshot,
}


def collect(
    admin_dsn: str, prefix: str = DEFAULT_PREFIX, names: tuple[str, ...] = NAMES, bases: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """All snapshots of one run (its own schema and roles, dropped afterwards)."""
    with evidence_schema(admin_dsn, prefix, bases) as ev:
        found = {name: COLLECTORS[name](ev) for name in COLLECTORS if name in names}
    return {name: found[name] for name in names}


def dumps(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"


def golden(name: str, directory: Path = GOLDEN) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((directory / f"{name}.json").read_text("utf-8"))
    return loaded


def diff(name: str, expected: dict[str, Any], actual: dict[str, Any]) -> str:
    return "".join(
        difflib.unified_diff(
            dumps(expected).splitlines(keepends=True),
            dumps(actual).splitlines(keepends=True),
            f"golden/{name}.json",
            f"current/{name}.json",
        )
    )


def _prefix_argument(value: str) -> str:
    try:
        return validate_prefix(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python tests/rollback_snapshots.py", description=__doc__.split("\n")[0])
    parser.add_argument("--dsn", required=True, help="a superuser connection string (as NODESTRO_TEST_POSTGRES)")
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        type=_prefix_argument,
        help="prefix of the schema and role names of the run ([a-z][a-z0-9_]{0,19})",
    )
    parser.add_argument("--dir", type=Path, default=GOLDEN, help="directory of the snapshot files")
    parser.add_argument("--only", action="append", choices=NAMES, help="only this snapshot (repeatable)")
    parser.add_argument("action", choices=("write", "check", "cleanup"))
    args = parser.parse_args(argv)
    if args.action == "cleanup":
        dropped = cleanup(args.dsn, args.prefix)
        for item in dropped:
            print(f"dropped {item}")
        print(f"cleanup: {len(dropped)} leftover(s) of prefix {args.prefix} dropped")
        return 0
    left = leftovers(args.dsn, args.prefix)
    if left:
        print(f"leftovers of earlier runs with prefix {args.prefix} (a killed run?): {', '.join(left)}")
        print(f"  drop them with: python tests/rollback_snapshots.py --dsn ... --prefix {args.prefix} cleanup")
    snapshots = collect(args.dsn, args.prefix, tuple(args.only or NAMES))
    if args.action == "write":
        args.dir.mkdir(parents=True, exist_ok=True)
        for name, snapshot in snapshots.items():
            (args.dir / f"{name}.json").write_text(dumps(snapshot), "utf-8")
            print(f"wrote {args.dir / f'{name}.json'}")
        return 0
    failed = False
    for name, snapshot in snapshots.items():
        try:
            text = diff(name, golden(name, args.dir), snapshot)
        except FileNotFoundError:
            text = f"missing: {args.dir / f'{name}.json'}\n"
        if text:
            failed = True
            sys.stdout.write(text)
    print("snapshots differ" if failed else "snapshots identical")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
