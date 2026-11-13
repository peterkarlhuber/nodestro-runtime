"""Rollback evidence of the old code (TABRION QA-13): the snapshots of ``tests/rollback_snapshots.py``.

The golden files in ``tests/fixtures/rollback/`` were written from the old code (``tests/fixtures/rollback/SOURCE.md``).
The TABRION MIG-NOD steps of the server profile and their reverts must leave them unchanged unless the step's pull
request rewrites a file on purpose (reviewed diff; MIG-NOD-3 does so for the messages that carry the canary of
finding B-06-2). Two runs on two fresh schemas must write identical snapshots, so a difference is a change of
behaviour, never noise of the run.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import json
import os
from pathlib import Path
import secrets
from typing import Any

import postgres_support
import pytest

pytest.importorskip("psycopg")
pytest.importorskip("axentra")

import rollback_snapshots

#: Prefix of the schemas and roles of the runs (each run drops its own objects again).
PREFIX = os.environ.get("NODESTRO_TEST_EVIDENCE_PREFIX", rollback_snapshots.DEFAULT_PREFIX)
#: The base names of this module's runs (the leftover check looks only at these, not at parallel runs).
BASES: list[str] = []
#: The base names of the two runs of the ``runs`` fixture (a subset of ``BASES``).
RUN_BASES: list[str] = []


@pytest.fixture(scope="module")
def runs() -> Iterator[list[dict[str, dict[str, Any]]]]:
    dsn = postgres_support.require_postgres()
    collected = [rollback_snapshots.collect(dsn, PREFIX, bases=RUN_BASES) for _ in range(2)]
    BASES.extend(RUN_BASES)
    yield collected


@pytest.mark.parametrize("name", rollback_snapshots.NAMES)
def test_snapshot_equals_the_old_code(runs: list[dict[str, dict[str, Any]]], name: str) -> None:
    text = rollback_snapshots.diff(name, rollback_snapshots.golden(name), runs[0][name])
    assert not text, f"the {name} snapshot differs from the old code:\n{text}"


@pytest.mark.parametrize("name", rollback_snapshots.NAMES)
def test_two_runs_write_identical_snapshots(runs: list[dict[str, dict[str, Any]]], name: str) -> None:
    assert rollback_snapshots.dumps(runs[0][name]) == rollback_snapshots.dumps(runs[1][name])


@pytest.mark.parametrize("name", rollback_snapshots.NAMES)
def test_golden_files_are_canonical(name: str) -> None:
    path = rollback_snapshots.GOLDEN / f"{name}.json"
    assert path.read_text("utf-8") == rollback_snapshots.dumps(json.loads(path.read_text("utf-8")))


def test_no_object_of_a_run_is_left_behind(runs: list[dict[str, dict[str, Any]]]) -> None:
    assert len(RUN_BASES) == 2  # independent of which other fixtures ran before (review D-2)
    assert rollback_snapshots.leftovers(postgres_support.DSN, PREFIX, bases=BASES) == []


def test_every_axentra_table_with_row_level_security_forces_it() -> None:
    """The invariant of Axentra's tables in Nodestro's schema (review ``qa-13-25-review.md``, M-6)."""
    catalog = rollback_snapshots.golden("catalog")
    tables = catalog["axentra_tables"]
    assert tables and all(t["force_row_security"] for t in tables if t["row_security"])
    summary = catalog["axentra_summary"]
    assert summary["tables"] == len(tables) and summary["row_security"] == summary["force_row_security"] > 0


# -- what the catalog snapshot detects (M-1, M-6 of the QA-13 review) -----------------------------------------------


@pytest.fixture(scope="module")
def evidence() -> Iterator[tuple[rollback_snapshots.EvidenceSchema, dict[str, Any]]]:
    dsn = postgres_support.require_postgres()
    with rollback_snapshots.evidence_schema(dsn, PREFIX, bases=BASES) as ev:
        yield ev, rollback_snapshots.catalog_snapshot(ev)


def _acl(catalog: dict[str, Any], kind: str, obj: str | None, grantee: str, privilege: str, grantable: bool) -> bool:
    return any(
        e[0] == kind and (obj is None or e[1] == obj) and e[3] == grantee and e[4] == privilege and e[5] is grantable
        for e in catalog["acl"]
    )


def _axentra(catalog: dict[str, Any], table: str) -> dict[str, Any]:
    return next(t for t in catalog["axentra_tables"] if t["table"] == table)


#: Changes that grant_runtime_role (or MIG-NOD-5, which replaces it) could make; each must change the snapshot.
#: Run in the run's schema as the admin. (change, revert, what the snapshot must then show)
CATALOG_MUTANTS: dict[str, tuple[list[str], list[str], Callable[[dict[str, Any]], bool]]] = {
    "column_update_grant": (
        ["GRANT UPDATE (checksum) ON nodestro_schema_migrations TO {runtime}"],
        ["REVOKE UPDATE (checksum) ON nodestro_schema_migrations FROM {runtime}"],
        lambda c: _acl(c, "column", "nodestro_schema_migrations.checksum", "<runtime>", "UPDATE", False),
    ),
    "execute_with_grant_option": (
        ["GRANT EXECUTE ON FUNCTION {function} TO {runtime} WITH GRANT OPTION"],
        ["REVOKE GRANT OPTION FOR EXECUTE ON FUNCTION {function} FROM {runtime}"],
        lambda c: _acl(c, "function", None, "<runtime>", "EXECUTE", True),
    ),
    "table_grant_to_public": (
        ["GRANT SELECT ON nodestro_schema_migrations TO PUBLIC"],
        ["REVOKE SELECT ON nodestro_schema_migrations FROM PUBLIC"],
        lambda c: _acl(c, "relation", "nodestro_schema_migrations", "PUBLIC", "SELECT", False),
    ),
    "membership_of_the_runtime_role": (
        ["GRANT {owner} TO {runtime}"],
        ["REVOKE {owner} FROM {runtime}"],
        lambda c: ["<owner>", "<runtime>", False] in c["runtime_role"]["memberships"],
    ),
    "axentra_tenants_without_row_level_security": (
        [
            "ALTER TABLE axentra_tenants NO FORCE ROW LEVEL SECURITY",
            "ALTER TABLE axentra_tenants DISABLE ROW LEVEL SECURITY",
        ],
        [
            "ALTER TABLE axentra_tenants ENABLE ROW LEVEL SECURITY",
            "ALTER TABLE axentra_tenants FORCE ROW LEVEL SECURITY",
        ],
        lambda c: not _axentra(c, "axentra_tenants")["row_security"],
    ),
    "axentra_tenants_without_force": (
        ["ALTER TABLE axentra_tenants NO FORCE ROW LEVEL SECURITY"],
        ["ALTER TABLE axentra_tenants FORCE ROW LEVEL SECURITY"],
        lambda c: not _axentra(c, "axentra_tenants")["force_row_security"],
    ),
}


@pytest.mark.parametrize("mutant", sorted(CATALOG_MUTANTS))
def test_the_catalog_snapshot_detects_a_privilege_or_row_security_change(
    evidence: tuple[rollback_snapshots.EvidenceSchema, dict[str, Any]], mutant: str
) -> None:
    import psycopg
    from psycopg import sql

    ev, before = evidence
    change, revert, shows = CATALOG_MUTANTS[mutant]
    with psycopg.connect(ev.admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(ev.schema)))
        row = conn.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = %s AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid"
            " AND d.deptype = 'e') ORDER BY 1 LIMIT 1",
            (ev.schema,),
        ).fetchone()
        assert row is not None
        names = {
            "runtime": sql.Identifier(ev.runtime),
            "owner": sql.Identifier(ev.owner),
            "function": sql.SQL(str(row[0])),
        }
        assert not shows(before)
        for statement in change:
            conn.execute(sql.SQL(statement).format(**names))
        try:
            after = rollback_snapshots.catalog_snapshot(ev)
        finally:
            for statement in revert:
                conn.execute(sql.SQL(statement).format(**names))
    assert rollback_snapshots.diff("catalog", before, after), f"{mutant} did not change the catalog snapshot"
    assert shows(after), rollback_snapshots.diff("catalog", before, after)
    assert rollback_snapshots.diff("catalog", before, rollback_snapshots.catalog_snapshot(ev)) == ""


def test_the_login_roles_get_a_random_password_per_run() -> None:
    first, second = rollback_snapshots.EvidenceSchema("", "a"), rollback_snapshots.EvidenceSchema("", "b")
    assert first.password != second.password and len(first.password) >= 24
    assert first.password not in repr(first)
    source = Path(rollback_snapshots.__file__).read_text("utf-8")
    assert "evidence-pw" not in source and "PASSWORD =" not in source


@pytest.mark.parametrize("prefix", ["", "Nodestro_", "nd-ev_", "nd%", "nd_ev_" + "x" * 15, "1nd"])
def test_an_invalid_prefix_is_refused(prefix: str) -> None:
    with pytest.raises(ValueError, match="invalid prefix"):
        rollback_snapshots.validate_prefix(prefix)
    with pytest.raises(SystemExit):
        rollback_snapshots.main(["--dsn", "host=127.0.0.1", "--prefix", prefix, "check"])


def test_leftovers_match_only_the_names_a_run_creates() -> None:
    schema, role = rollback_snapshots._run_names("nodestro_ev_")
    assert schema.fullmatch("nodestro_ev_0123abcd_scratch") and role.fullmatch("nodestro_ev_0123abcd_v_superuser")
    for other in ("nodestro_evx0123abcd", "nodestro_ev_0123abcd_other", "nodestro_ev_0123abc", "xnodestro_ev_0123abcd"):
        assert not schema.fullmatch(other) and not role.fullmatch(other), other


def test_cleanup_drops_the_leftovers_of_a_killed_run_and_nothing_else() -> None:
    import psycopg
    from psycopg import sql

    dsn = postgres_support.require_postgres()
    prefix = PREFIX + "kill_"  # a prefix of its own: no other run's objects
    base = prefix + secrets.token_hex(4)  # unique per run: concurrent suites on one server never share it (D-1)
    roles = [f"{base}_owner", f"{base}_v_superuser"]
    untouched = f"{base}_other"  # same start, not a name a run creates
    with psycopg.connect(dsn, autocommit=True) as conn:
        for name in (*roles, untouched):
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(name)))
        conn.execute(
            sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(sql.Identifier(base), sql.Identifier(roles[0]))
        )
        conn.execute(sql.SQL("CREATE TABLE {}.t (id int)").format(sql.Identifier(base)))
    try:
        expected = sorted([f"schema {base}", *(f"role {r}" for r in roles)])
        assert rollback_snapshots.leftovers(dsn, prefix) == expected
        assert rollback_snapshots.main(["--dsn", dsn, "--prefix", prefix, "cleanup"]) == 0
        assert rollback_snapshots.leftovers(dsn, prefix) == []
        with psycopg.connect(dsn, autocommit=True) as conn:
            assert conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (untouched,)).fetchone()
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(base)))
            for name in (*roles, untouched):
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


def test_the_role_table_has_eight_variants() -> None:
    table = rollback_snapshots.golden("roles")
    assert len(table["variants"]) == 8 and set(table["table"]) == set(table["variants"])


def test_the_error_snapshot_records_that_finding_b_06_2_is_closed() -> None:
    """The old code put the server's DETAIL value into the backup and migration messages (B-06-2); since MIG-NOD-3 no
    message of the snapshot holds the canary, and those three end with Tabrion's value-free text."""
    errors = rollback_snapshots.golden("errors")["errors"]
    leaking = sorted(label for label, answer in errors.items() if answer["message_has_canary"])
    assert leaking == []
    for label in ("backup_failed", "migration_failed", "migration_failed_after_backup"):
        message = errors[label]["message"]
        assert ": unique violation (SQLSTATE 23505, constraint " in message
        assert "DETAIL" not in message and "Key (" not in message
