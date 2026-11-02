"""Every PostgreSQL statement on a tenant table is bound to one tenant (defense in depth instead of RLS, see
``docs/AXENTRA.md``, "Row-level security on Nodestro's tables"; security review SR-01).

The check itself is :mod:`nodestro_runtime.tenant_sql_check`: it rebuilds every string expression of the runtime's and
the API's source, parses each candidate with PostgreSQL's grammar and requires ``tenant_id = <param>`` as a top-level
``AND`` conjunct for every reference to a table with a ``tenant_id`` column (taken from the migrations). Statements that
are cross-tenant by design are listed in :mod:`nodestro_runtime.tenant_sql_allowlist` (and the API's own list), each
with its reason, matched on the whole normalized text.

The check reads source only; it runs the same with and without ``NODESTRO_TEST_RUNTIME_ROLE``.
"""

from __future__ import annotations

from collections.abc import Mapping
import importlib
import importlib.util
from pathlib import Path
import textwrap
from types import ModuleType

import pytest

import nodestro_runtime
from nodestro_runtime import tenant_sql_allowlist
from nodestro_runtime.tenant_sql_check import Report, check_sql, scan_package, statements_of, tenant_tables

RUNTIME = Path(nodestro_runtime.__file__).parent
TABLES = tenant_tables(RUNTIME / "migrations" / "postgres")


def _api_package() -> tuple[Path, ModuleType] | None:
    """nodestro_api's source (next to this runtime's checkout, else the installed package) and its allowlist;
    ``None`` when neither exists."""
    root = RUNTIME.parents[2] / "nodestro-api" / "src" / "nodestro_api"
    if not (root / "__init__.py").is_file():
        try:
            root = Path(importlib.import_module("nodestro_api").__file__ or "").parent
        except ImportError:
            return None
    path = root / "tenant_sql_allowlist.py"
    if not path.is_file():
        pytest.fail(f"{root} has no tenant_sql_allowlist.py: the API's statements cannot be checked")
    spec = importlib.util.spec_from_file_location("_api_tenant_sql_allowlist", path)
    assert spec is not None and spec.loader is not None
    allowlist = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(allowlist)
    return root, allowlist


def _scan(
    source: str,
    tmp_path: Path,
    *,
    package: str = "sample",
    module: str = "sample.py",
    allowlist: Mapping[tuple[str, str], str] | None = None,
    skip: Mapping[str, str] | None = None,
) -> Report:
    root = tmp_path / package
    path = root / module
    path.parent.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return scan_package(root, TABLES, allowlist or {}, skip or {})


def _assert_clean(report: Report) -> None:
    assert [str(f) for f in report.findings] == []


# --- the code base ---------------------------------------------------------------------------------------------


def test_tenant_tables_come_from_the_migrations(tmp_path: Path) -> None:
    assert {
        "nodestro_documents",
        "nodestro_files",
        "nodestro_chunks",
        "nodestro_memory_files",
        "nodestro_run_events",
        "nodestro_jobs",
        "nodestro_schedule_firings",
        "nodestro_sessions",
        "nodestro_memberships",
        "nodestro_bootstrap_tokens",
        "nodestro_login_states",
    } <= TABLES
    # Tables without tenant_id are not tenant tables; the migrations' own table is not in the migrations.
    assert not {"nodestro_tenants", "nodestro_users", "nodestro_axentra_ids"} & TABLES
    # A table of a new migration (also a column added later) is covered without changing the test.
    for path in (RUNTIME / "migrations" / "postgres").glob("*.sql"):
        (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "0999_new.sql").write_text(
        "CREATE TABLE nodestro_widgets (tenant_id text NOT NULL, id text);\n"
        "CREATE TABLE nodestro_gadgets (id text);\n"
        "ALTER TABLE nodestro_gadgets ADD COLUMN tenant_id text;\n",
        encoding="utf-8",
    )
    assert tenant_tables(tmp_path) == TABLES | {"nodestro_widgets", "nodestro_gadgets"}


def test_every_runtime_statement_on_tenant_data_is_bound_to_one_tenant() -> None:
    report = scan_package(RUNTIME, TABLES, tenant_sql_allowlist.ALLOWLIST, tenant_sql_allowlist.SKIP_MODULES)
    assert len(report.checked) > 60  # the scan finds the stores' statements
    _assert_clean(report)


def test_every_api_statement_on_tenant_data_is_bound_to_one_tenant() -> None:
    found = _api_package()
    if found is None:
        pytest.skip("nodestro_api is neither next to this checkout nor importable: its own test suite checks it")
    root, allowlist = found
    report = scan_package(root, TABLES, allowlist.ALLOWLIST, allowlist.SKIP_MODULES)
    _assert_clean(report)


def test_every_allowlist_entry_has_a_reason() -> None:
    for key, reason in tenant_sql_allowlist.ALLOWLIST.items():
        assert len(reason) > 30, key
        assert key[0].startswith("nodestro_runtime/"), key
    for module, reason in tenant_sql_allowlist.SKIP_MODULES.items():
        assert len(reason) > 30, module


# --- the review's leaking samples (SR-01): each one passed the old test -------------------------------------------

REVIEW_SAMPLES = {
    "ORed predicate": 'Q = "SELECT data FROM nodestro_documents WHERE tenant_id = %s OR collection = %s"',
    "tenant_id only in select list": 'Q = "SELECT tenant_id, data FROM nodestro_documents WHERE collection = %s"',
    "tenant_id only in ORDER BY": 'Q = "SELECT data FROM nodestro_documents WHERE id = %s ORDER BY tenant_id"',
    "no %s placeholder": "Q = \"SELECT data FROM nodestro_documents WHERE collection = 'runs'\"",
    "jsonb ? operator": 'Q = "SELECT data FROM nodestro_documents WHERE data ? %s"',
    "split concatenation": 'Q = "SELECT data FROM nodestro_documents" + " WHERE collection = %s"',
    "table via variable": 'T = "nodestro_documents"\nQ = f"SELECT data FROM {T} WHERE collection = %s"',
    "psycopg sql.SQL with Identifier": (
        'Q = sql.SQL("SELECT data FROM {} WHERE id = %s").format(sql.Identifier("nodestro_documents"))'
    ),
    "tenant_id IS NOT NULL": (
        'Q = "SELECT data FROM nodestro_documents WHERE tenant_id IS NOT NULL AND collection = %s"'
    ),
    "tenant_id = tenant_id": 'Q = "SELECT data FROM nodestro_documents WHERE tenant_id = tenant_id AND id = %s"',
    "table not in the old list (nodestro_jobs)": (
        'Q = "UPDATE nodestro_jobs SET cancel_requested = true WHERE ref = %s"'
    ),
    "table not in the old list (nodestro_schedule_firings)": (
        'Q = "SELECT run_id FROM nodestro_schedule_firings WHERE schedule_id = %s"'
    ),
}


@pytest.mark.parametrize("name", sorted(REVIEW_SAMPLES))
def test_the_review_samples_are_caught(name: str, tmp_path: Path) -> None:
    report = _scan(REVIEW_SAMPLES[name] + "\n", tmp_path)
    assert report.findings, name


def test_the_review_sample_extending_an_allowlisted_statement_is_caught(tmp_path: Path) -> None:
    allowed = "DELETE FROM nodestro_run_events WHERE created_at < clock_timestamp() - %s"
    assert ("nodestro_runtime/jobs.py", allowed) in tenant_sql_allowlist.ALLOWLIST
    report = _scan(
        f'Q = "{allowed} OR true"\n',
        tmp_path,
        package="nodestro_runtime",
        module="jobs.py",
        allowlist=tenant_sql_allowlist.ALLOWLIST,
    )
    leaks = [f for f in report.findings if f.kind == "leak"]
    assert len(leaks) == 1 and leaks[0].text.endswith("OR true")
    # The real statement still matches (and nothing else of the list does in this sample module).
    report = _scan(
        f'Q = "{allowed}"\n',
        tmp_path,
        package="nodestro_runtime",
        module="jobs.py",
        allowlist=tenant_sql_allowlist.ALLOWLIST,
    )
    assert [f.kind for f in report.findings if f.kind != "stale"] == []


def test_the_review_sample_in_the_api_package_is_caught(tmp_path: Path) -> None:
    # The old test scanned only the runtime. The API is scanned too (test above); a leak there is reported.
    if _api_package() is None:
        pytest.skip("nodestro_api is neither next to this checkout nor importable")
    report = _scan(
        'Q = "SELECT data FROM nodestro_documents WHERE collection = %s"\n', tmp_path, package="nodestro_api"
    )
    assert [f.kind for f in report.findings] == ["leak"]
    assert report.findings[0].module == "nodestro_api/sample.py"


# --- dynamic SQL: rebuilt and checked, or reported (never skipped) ------------------------------------------------


def test_conditional_where_fragments_are_checked_in_every_variant(tmp_path: Path) -> None:
    # The pattern of axentra_platform.reencrypt_secrets and settled_runs: a WHERE fragment extended by `+=`.
    report = _scan(
        """
        def runs(db, tenant, since=None):
            where = "tenant_id = %s AND collection = 'runs'"
            if since is not None:
                where += " AND updated_at >= %s"
            return db._query(f"SELECT id FROM nodestro_documents WHERE {where} ORDER BY id", ())

        def secrets(db, tenant=None):
            where = "collection = ANY(%s)"
            if tenant is not None:
                where += " AND tenant_id = %s"
            return db._query(f"SELECT id FROM nodestro_documents WHERE {where}", ())
        """,
        tmp_path,
    )
    assert len(report.checked) == 4
    assert [f.text for f in report.findings] == ["SELECT id FROM nodestro_documents WHERE collection = ANY(%s)"]


def test_unknown_parts_are_reported_as_dynamic(tmp_path: Path) -> None:
    report = _scan(
        """
        from psycopg import sql

        def a(db, where):
            db.execute(f"SELECT data FROM nodestro_documents WHERE {where}")

        def b(db, where):
            db.execute("SELECT data FROM nodestro_documents WHERE {}".format(where))

        def c(db, table):
            db.execute(f"SELECT data FROM {table} WHERE id = %s")

        def d(db, kind):
            db.execute(f"DELETE FROM nodestro_{kind} WHERE id = %s")

        def e(db, name):
            db.execute(sql.SQL("SELECT data FROM {} WHERE id = %s").format(sql.Identifier(name)))

        def f(db, parts):
            db.execute(sql.Composed([sql.SQL("SELECT data FROM nodestro_files WHERE "), parts]))

        def g(db, cond):
            db.execute(sql.SQL("UPDATE nodestro_jobs SET status = 'done' WHERE {}").format(sql.SQL(cond)))
        """,
        tmp_path,
    )
    assert sorted(f.line for f in report.findings) == [5, 8, 11, 14, 17, 20, 23]
    assert {f.kind for f in report.findings} == {"dynamic"}


def test_psycopg_compositions_are_rebuilt_and_checked(tmp_path: Path) -> None:
    report = _scan(
        """
        from psycopg import sql

        GOOD = sql.SQL("SELECT data FROM {} WHERE {} = %s AND id = %s").format(
            sql.Identifier("nodestro_documents"), sql.Identifier("tenant_id")
        )
        BAD = sql.Composed(
            [sql.SQL("SELECT data FROM "), sql.Identifier("nodestro_files"), sql.SQL(" WHERE id = "), sql.Placeholder()]
        )
        JOINED = sql.SQL(" ").join([sql.SQL("DELETE FROM nodestro_chunks"), sql.SQL("WHERE source = %s")])
        NAMED = sql.SQL("SELECT 1 FROM nodestro_jobs WHERE tenant_id = {t} AND id = %s").format(t=sql.Placeholder("t"))
        """,
        tmp_path,
    )
    assert sorted((f.line, f.kind) for f in report.findings) == [(7, "leak"), (10, "leak")]
    assert len(report.checked) == 4


def test_loops_over_tables_and_module_constants_are_resolved(tmp_path: Path) -> None:
    report = _scan(
        """
        TABLES = ("nodestro_documents", "nodestro_files")
        COLUMNS = "tenant_id, id"

        def empty(db, tenant):
            for table in TABLES:
                db.execute(f"SELECT {COLUMNS} FROM {table} WHERE tenant_id = %s LIMIT 1", (tenant,))

        def wipe(db):
            for table in TABLES:
                db.execute(f"DELETE FROM {table}")
        """,
        tmp_path,
    )
    assert sorted(f.text for f in report.findings) == ["DELETE FROM nodestro_documents", "DELETE FROM nodestro_files"]
    assert len(report.checked) == 4  # the bare table names are part of the statements, not reported on their own


def test_a_table_name_outside_a_statement_is_reported(tmp_path: Path) -> None:
    report = _scan('NAME = "nodestro_documents"\nprint(NAME)\n', tmp_path)
    assert [f.kind for f in report.findings] == ["unparsed"]


def test_docstrings_are_not_statements(tmp_path: Path) -> None:
    report = _scan('"""Reads nodestro_documents."""\n\ndef f():\n    """SELECT * FROM nodestro_jobs"""\n', tmp_path)
    assert report.checked == [] and report.findings == []


# --- the allowlist and the skipped modules ----------------------------------------------------------------------


def test_allowlist_matches_whole_statements_and_reports_stale_entries(tmp_path: Path) -> None:
    allowed = "SELECT count(*) FROM nodestro_jobs"
    allowlist = {("sample/sample.py", allowed): "metrics", ("sample/sample.py", "SELECT 1 FROM nodestro_files"): "x"}
    report = _scan(f'A = "{allowed}"\nB = "{allowed} WHERE kind = %s"\n', tmp_path, allowlist=allowlist)
    assert sorted((f.kind, f.text) for f in report.findings) == [
        ("leak", f"{allowed} WHERE kind = %s"),
        ("stale", "SELECT 1 FROM nodestro_files"),
    ]
    # The same text in another module is not allowed by an entry of this one.
    report = _scan(f'A = "{allowed}"\n', tmp_path, module="other.py", allowlist=allowlist)
    assert "leak" in [f.kind for f in report.findings]


def test_a_skipped_module_must_not_use_postgresql(tmp_path: Path) -> None:
    skip = {"sample/sample.py": "desktop SQLite store"}
    report = _scan('import sqlite3\nQ = "SELECT data FROM nodestro_documents WHERE id = ?"\n', tmp_path, skip=skip)
    assert report.findings == [] and report.checked == []
    report = _scan(
        'import sqlite3\nimport psycopg\nQ = "SELECT data FROM nodestro_documents WHERE id = %s"\n',
        tmp_path,
        skip=skip,
    )
    assert [f.kind for f in report.findings] == ["skip"]


# --- the SQL rules ------------------------------------------------------------------------------------------------

BOUND = [
    "SELECT data FROM nodestro_documents WHERE tenant_id = %s AND id = %s",
    "SELECT data FROM nodestro_documents WHERE id = %s AND %s = tenant_id",
    "SELECT data FROM nodestro_documents WHERE tenant_id = %s::text AND id = %s",
    "SELECT data FROM nodestro_documents WHERE tenant_id = ANY(%s)",
    "SELECT data FROM nodestro_documents WHERE tenant_id = %(tenant)s AND id = %(id)s",
    "SELECT data FROM public.nodestro_documents WHERE nodestro_documents.tenant_id = %s",
    "SELECT d.data FROM nodestro_documents d JOIN nodestro_jobs j ON j.tenant_id = d.tenant_id AND j.ref = d.id"
    " WHERE d.tenant_id = %s",
    "SELECT d.data FROM nodestro_documents d LEFT JOIN nodestro_jobs j ON j.tenant_id = d.tenant_id"
    " WHERE d.tenant_id = %s",
    "SELECT d.data FROM nodestro_documents d WHERE d.tenant_id = %s AND NOT EXISTS"
    " (SELECT 1 FROM nodestro_jobs j WHERE j.tenant_id = d.tenant_id AND j.ref = d.id)",
    "SELECT data FROM nodestro_documents WHERE tenant_id = %s AND id IN"
    " (SELECT ref FROM nodestro_jobs WHERE tenant_id = %s)",
    "WITH r AS (SELECT id FROM nodestro_documents WHERE tenant_id = %s) SELECT id FROM r",
    "SELECT data FROM nodestro_documents WHERE tenant_id = %s UNION ALL SELECT entry FROM nodestro_files"
    " WHERE tenant_id = %s",
    "UPDATE nodestro_jobs SET cancel_requested = true WHERE tenant_id = %s AND ref = %s RETURNING id",
    "DELETE FROM nodestro_chunks WHERE tenant_id = %s AND namespace = %s",
    "INSERT INTO nodestro_run_events (tenant_id, run_id, seq, event) VALUES (%s, %s, %s, %s)",
    "INSERT INTO nodestro_files (tenant_id, namespace, id, entry, data) VALUES (%s, %s, %s, %s, %s)"
    " ON CONFLICT (tenant_id, namespace, id) DO UPDATE SET entry = excluded.entry",
    "SELECT count(*) FROM pg_class WHERE relname = 'nodestro_documents'",  # a name in a literal reads no rows
    "CREATE INDEX x ON nodestro_documents (tenant_id, collection)",
    "SELECT id FROM nodestro_tenants WHERE status = 'active'",  # no tenant_id column: not a tenant table
]

LEAKING = [
    "SELECT data FROM nodestro_documents WHERE (tenant_id = %s OR id = %s)",
    "SELECT data FROM nodestro_documents WHERE NOT (tenant_id = %s)",
    "SELECT data FROM nodestro_documents WHERE tenant_id <> %s",
    "SELECT data FROM nodestro_documents WHERE tenant_id = 'acme'",
    "SELECT data FROM nodestro_documents WHERE tenant_id = (SELECT %s)",
    "SELECT data FROM nodestro_documents WHERE tenant_id = %s OR true",
    "SELECT data FROM nodestro_documents WHERE coalesce(tenant_id = %s, true)",
    "SELECT data FROM nodestro_documents HAVING tenant_id = %s",
    "SELECT d.data FROM nodestro_documents d, nodestro_files f WHERE tenant_id = %s",  # ambiguous
    "SELECT d.data FROM nodestro_documents d, nodestro_files f WHERE d.tenant_id = %s",  # f unbound
    "SELECT d.data FROM nodestro_documents d JOIN nodestro_jobs j ON j.ref = d.id WHERE d.tenant_id = %s",
    "SELECT j.ref FROM nodestro_documents d LEFT JOIN nodestro_jobs j ON d.tenant_id = %s",  # binds the wrong side
    "SELECT d.data FROM nodestro_documents d FULL JOIN nodestro_jobs j ON j.tenant_id = %s AND d.tenant_id = %s",
    "SELECT data FROM nodestro_documents WHERE tenant_id = %s AND id IN (SELECT ref FROM nodestro_jobs)",
    "SELECT (SELECT count(*) FROM nodestro_jobs) FROM nodestro_documents WHERE tenant_id = %s",
    "SELECT data FROM nodestro_documents WHERE tenant_id = %s UNION SELECT entry FROM nodestro_files",
    "WITH r AS (SELECT id FROM nodestro_documents) SELECT id FROM r",
    "SELECT x.id FROM (SELECT id FROM nodestro_documents) x",
    "UPDATE nodestro_jobs SET status = 'done' WHERE ref = %s",
    "UPDATE nodestro_jobs j SET status = 'done' FROM nodestro_documents d WHERE j.tenant_id = %s AND j.ref = d.id",
    "DELETE FROM nodestro_chunks USING nodestro_files f WHERE nodestro_chunks.tenant_id = %s AND f.id = file_id",
    "DELETE FROM nodestro_memory_files WHERE namespace = %s",
    "INSERT INTO nodestro_run_events (run_id, seq, event) VALUES (%s, %s, %s)",
    "INSERT INTO nodestro_run_events SELECT * FROM nodestro_run_events",
    "INSERT INTO nodestro_jobs (tenant_id, kind, ref, status) SELECT tenant_id, kind, ref, 'queued' FROM nodestro_jobs",
    "TRUNCATE nodestro_documents",
    "COPY nodestro_documents TO STDOUT",
    "SELECT 1; DELETE FROM nodestro_sessions",
    "WITH nodestro_documents AS (SELECT 1) SELECT * FROM nodestro_documents",
]


@pytest.mark.parametrize("text", BOUND)
def test_statements_bound_to_one_tenant_pass(text: str) -> None:
    assert check_sql(text, TABLES) == []


@pytest.mark.parametrize("text", LEAKING)
def test_statements_not_bound_to_one_tenant_fail(text: str) -> None:
    assert check_sql(text, TABLES) != []


def test_a_statement_is_found_through_implicit_concatenation_and_plus() -> None:
    source = (
        'Q = (\n    "SELECT id, " + COLS + " FROM nodestro_documents"\n    " WHERE collection = %s"\n)\nCOLS = "x"\n'
    )
    found = statements_of(source, "m.py", TABLES)
    assert [s.text for s in found] == ["SELECT id, x FROM nodestro_documents WHERE collection = %s"]


def test_a_fragment_used_on_its_own_is_checked_on_its_own(tmp_path: Path) -> None:
    report = _scan(
        """
        BASE = "SELECT data FROM nodestro_documents"

        def one(db, tenant):
            return db.execute(f"{BASE} WHERE tenant_id = %s", (tenant,))

        def every(db):
            return db.execute(BASE)
        """,
        tmp_path,
    )
    assert [f.text for f in report.findings] == ["SELECT data FROM nodestro_documents"]


def test_a_conditional_reassignment_does_not_hide_a_later_fragment(tmp_path: Path) -> None:
    report = _scan(
        """
        def f(db, tenant, alt):
            where = "tenant_id = %s"
            if alt:
                where = "id = %s"
            where += " AND collection = 'runs'"
            return db.execute(f"SELECT id FROM nodestro_documents WHERE {where}")
        """,
        tmp_path,
    )
    assert [f.text for f in report.findings] == [
        "SELECT id FROM nodestro_documents WHERE id = %s AND collection = 'runs'"
    ]
