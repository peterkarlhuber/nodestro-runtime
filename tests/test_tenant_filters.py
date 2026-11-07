"""Every PostgreSQL statement on a tenant table is bound to one tenant (defense in depth instead of RLS, see
``docs/AXENTRA.md``, "Row-level security on Nodestro's tables"; security review SR-01).

The check itself is :mod:`nodestro_runtime.tenant_sql_check`: it rebuilds every string expression of the runtime's and
the API's source, parses each candidate with PostgreSQL's grammar and requires ``tenant_id = <param>`` as a top-level
``AND`` conjunct for every reference to a table with a ``tenant_id`` column (taken from the migrations). Statements that
are cross-tenant by design are listed in :mod:`nodestro_runtime.tenant_sql_allowlist` (and the API's own list), each
with its reason, matched on the whole normalized text.

The API's source comes from ``NODESTRO_API_SRC`` (the directory that holds the ``nodestro_api`` package, or a
checkout of nodestro-api), else from a checkout next to this one or the installed package. Without any of them the
API scan (marker ``tenant_api_scan``) is skipped with a fixed reason that the CI skip expectation lists: the CI job
``tenant-sql-api`` and nodestro-api's own job ``tenant-sql`` scan the API. ``NODESTRO_REQUIRE_API_SCAN=1`` (set in
``tenant-sql-api``) turns a missing ``NODESTRO_API_SRC`` into a failure (MUP-ONA A11).

The check reads source only; it runs the same with and without ``NODESTRO_TEST_RUNTIME_ROLE``. What it guarantees and
what it does not: the check's module docstring and ``docs/AXENTRA.md`` ("Remaining isolation risk"). Every bypass of
the security review (rounds 1 to 4; round 3: issue #89) is a regression test below.
"""

from __future__ import annotations

from collections.abc import Mapping
import importlib
import importlib.util
import os
from pathlib import Path
import shutil
import textwrap
from types import ModuleType

import pytest

import nodestro_runtime
from nodestro_runtime import tenant_sql_allowlist
from nodestro_runtime.tenant_sql_check import Report, check_sql, scan_package, statements_of, tenant_tables

RUNTIME = Path(nodestro_runtime.__file__).parent
TABLES = tenant_tables(RUNTIME / "migrations" / "postgres")


#: The skip reason of the API scan where the API is not available; tests/ci/skipped-test.txt lists it.
API_SCAN_SKIP = "API covered by nodestro-api job tenant-sql (MUP-ONA A11)"


def _api_source(environ: Mapping[str, str]) -> Path | None:
    """The ``nodestro_api`` package directory: from ``NODESTRO_API_SRC`` (fails when it names none), else next to
    this runtime's checkout, else the installed package; ``None`` when there is none. With
    ``NODESTRO_REQUIRE_API_SCAN=1`` only ``NODESTRO_API_SRC`` counts, and a missing one fails instead of skipping."""
    given = environ.get("NODESTRO_API_SRC", "")
    required = environ.get("NODESTRO_REQUIRE_API_SCAN") == "1"
    if given:
        for root in (Path(given) / "nodestro_api", Path(given) / "src" / "nodestro_api", Path(given)):
            if root.name == "nodestro_api" and (root / "__init__.py").is_file():
                return root
        pytest.fail(f"NODESTRO_API_SRC={given} holds no nodestro_api package")
    if required:
        pytest.fail("NODESTRO_REQUIRE_API_SCAN=1 but NODESTRO_API_SRC names no nodestro-api source")
    root = RUNTIME.parents[2] / "nodestro-api" / "src" / "nodestro_api"
    if (root / "__init__.py").is_file():
        return root
    try:
        return Path(importlib.import_module("nodestro_api").__file__ or "").parent
    except ImportError:
        return None


def _api_package(environ: Mapping[str, str] = os.environ) -> tuple[Path, ModuleType] | None:
    """nodestro_api's source (see :func:`_api_source`) and its allowlist; ``None`` when there is no source."""
    root = _api_source(environ)
    if root is None:
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


@pytest.mark.tenant_api_scan
def test_every_api_statement_on_tenant_data_is_bound_to_one_tenant(tmp_path: Path) -> None:
    found = _api_package()
    if found is None:
        pytest.skip(API_SCAN_SKIP)
    root, allowlist = found
    report = scan_package(root, TABLES, allowlist.ALLOWLIST, allowlist.SKIP_MODULES)
    # The API's SQL goes through the runtime's stores, so it may hold no statement at all; the scan must see its
    # modules, and the planted leak below proves it reads them.
    assert len(list(root.rglob("*.py"))) > 10, f"{root} holds almost no Python modules"
    _assert_clean(report)
    # MUP-ONA T-CHK-1: the same scan reports a leak planted in a copy of the API's source.
    copy = tmp_path / "nodestro_api"
    shutil.copytree(root, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "_mup_planted_leak.py").write_text(
        'PLANTED = "SELECT * FROM nodestro_chunks WHERE namespace = %s"\n', encoding="utf-8"
    )
    planted = scan_package(copy, TABLES, allowlist.ALLOWLIST, allowlist.SKIP_MODULES)
    assert [(f.module, f.kind) for f in planted.findings] == [("nodestro_api/_mup_planted_leak.py", "leak")]


# MUP-ONA T-CHK-4
def test_a_required_api_scan_fails_instead_of_skipping(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="NODESTRO_API_SRC"):
        _api_package({"NODESTRO_REQUIRE_API_SCAN": "1"})
    with pytest.raises(pytest.fail.Exception, match="holds no nodestro_api package"):
        _api_package({"NODESTRO_REQUIRE_API_SCAN": "1", "NODESTRO_API_SRC": str(tmp_path / "missing")})
    with pytest.raises(pytest.fail.Exception, match="holds no nodestro_api package"):
        _api_package({"NODESTRO_API_SRC": str(tmp_path)})  # a wrong path fails even when not required
    api = tmp_path / "checkout" / "src" / "nodestro_api"
    api.mkdir(parents=True)
    (api / "__init__.py").write_text("", encoding="utf-8")
    (api / "tenant_sql_allowlist.py").write_text("ALLOWLIST: dict = {}\nSKIP_MODULES: dict = {}\n", encoding="utf-8")
    for given in (tmp_path / "checkout", tmp_path / "checkout" / "src", api):
        found = _api_package({"NODESTRO_REQUIRE_API_SCAN": "1", "NODESTRO_API_SRC": str(given)})
        assert found is not None and found[0] == api


# MUP-ONA T-CHK-4
def test_the_api_scan_is_marked_and_skips_only_with_the_documented_reason() -> None:
    marked = test_every_api_statement_on_tenant_data_is_bound_to_one_tenant
    assert any(m.name == "tenant_api_scan" for m in getattr(marked, "pytestmark", []))
    assert API_SCAN_SKIP == "API covered by nodestro-api job tenant-sql (MUP-ONA A11)"


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


@pytest.mark.tenant_api_scan
def test_the_review_sample_in_the_api_package_is_caught(tmp_path: Path) -> None:
    # The old test scanned only the runtime. The API is scanned too (test above); a leak there is reported.
    if _api_package() is None:
        pytest.skip(API_SCAN_SKIP)
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
    # MUP-ONA A11 (O12): `tenant_id = ANY(%s)` binds only with allow_any=True; by default it needs an allowlist entry.
    assert check_sql(text, TABLES, allow_any=True) == []
    if "ANY(" not in text:
        assert check_sql(text, TABLES) == []


@pytest.mark.parametrize("text", LEAKING)
def test_statements_not_bound_to_one_tenant_fail(text: str) -> None:
    assert check_sql(text, TABLES) != []


ANY_SAMPLES = [
    "SELECT data FROM nodestro_documents WHERE tenant_id = ANY(%s)",
    "SELECT data FROM nodestro_documents WHERE tenant_id = ANY(%s::text[]) AND collection = %s",
    "DELETE FROM nodestro_jobs WHERE tenant_id = ANY(%(tenants)s)",
    "SELECT d.data FROM nodestro_documents d WHERE d.tenant_id = ANY(%s)",
]


# MUP-ONA T-CHK-2
@pytest.mark.parametrize("text", ANY_SAMPLES)
def test_tenant_id_any_is_a_leak_unless_allowed(text: str) -> None:
    problems = check_sql(text, TABLES)
    assert len(problems) == 2 and "is not bound" in problems[0] and "array of tenant ids" in problems[1]
    assert check_sql(text, TABLES, allow_any=True) == []
    # ANY on another column binds nothing either way; bound by tenant_id = %s it passes without allow_any.
    assert check_sql("SELECT data FROM nodestro_documents WHERE tenant_id = %s AND id = ANY(%s)", TABLES) == []
    assert check_sql("SELECT data FROM nodestro_documents WHERE id = ANY(%s)", TABLES, allow_any=True) != []


# MUP-ONA T-CHK-2
def test_tenant_id_any_passes_only_through_an_allowlist_entry(tmp_path: Path) -> None:
    source = 'Q = "SELECT data FROM nodestro_documents WHERE tenant_id = ANY(%s)"\n'
    key = ("sample/sample.py", "SELECT data FROM nodestro_documents WHERE tenant_id = ANY(%s)")
    reason = "the array always holds exactly the one tenant of the calling store (test sample)"
    report = _scan(source, tmp_path / "plain")
    assert {(f.kind, f.module, f.text) for f in report.findings} == {("leak", *key)}
    _assert_clean(_scan(source, tmp_path / "listed", allowlist={key: reason}))
    stale = _scan(
        'Q = "SELECT data FROM nodestro_documents WHERE tenant_id = %s"\n', tmp_path / "stale", allowlist={key: reason}
    )
    assert [(f.kind, f.module, f.text) for f in stale.findings] == [("stale", *key)]
    # allow_any=True is the old rule: the statement passes without an entry.
    _assert_clean(scan_package(tmp_path / "plain" / "sample", TABLES, {}, {}, allow_any=True))


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


# --- names read on their own (security re-review SR-01a) -----------------------------------------------------------

BARE_NAME_LEAKS = {
    "unconditional += OR": """
        def f(db):
            q = "SELECT data FROM nodestro_documents WHERE tenant_id = %s"
            q += " OR true"
            db._query(q)
        """,
    "q = q + OR": """
        def f(db):
            q = "SELECT data FROM nodestro_documents WHERE tenant_id = %s"
            q = q + " OR true"
            db._query(q)
        """,
    "conditional += OR": """
        def f(db, alt):
            q = "SELECT data FROM nodestro_documents WHERE tenant_id = %s"
            if alt:
                q += " OR id = %s"
            db._query(q, ())
        """,
    "elif reassignment": """
        def f(db, a):
            if a == 1:
                w = "tenant_id = %s"
            elif a == 2:
                w = "true"
            else:
                w = "tenant_id = %s AND id = %s"
            q = "SELECT data FROM nodestro_documents WHERE " + w
            db._query(q)
        """,
    "assigned after the read in a loop": """
        def f(db, xs):
            q = "SELECT data FROM nodestro_documents WHERE tenant_id = %s"
            for x in xs:
                db._query(q)
                q = "SELECT data FROM nodestro_documents WHERE true"
        """,
    "set by a function that declares it global": """
        Q = "SELECT data FROM nodestro_documents WHERE tenant_id = %s"

        def widen():
            global Q
            Q = "SELECT data FROM nodestro_documents"

        def f(db):
            db._query(Q)
        """,
    "returned": """
        def f():
            q = "SELECT data FROM nodestro_documents WHERE tenant_id = %s"
            q += " OR true"
            return q
        """,
    "the value assigned before a later string reads the name": """
        def f(db):
            w = "tenant_id = %s OR true"
            q = f"SELECT data FROM nodestro_documents WHERE {w}"
            w = "tenant_id = %s"
            db._query(q)
        """,
}


@pytest.mark.parametrize("name", sorted(BARE_NAME_LEAKS))
def test_a_name_read_on_its_own_is_checked_with_every_value(name: str, tmp_path: Path) -> None:
    report = _scan(BARE_NAME_LEAKS[name], tmp_path)
    assert [f.kind for f in report.findings if f.kind == "leak"], name


def test_every_branch_of_a_bare_name_is_checked(tmp_path: Path) -> None:
    report = _scan(
        """
        def f(db, limit=None, live=None):
            q = "SELECT data FROM nodestro_documents WHERE tenant_id = %s"
            if live is not None:
                q += " OR live"
            if limit is not None:
                q += " LIMIT %s"
            db._query(q)
        """,
        tmp_path,
    )
    assert sorted(s.text.removeprefix("SELECT data FROM nodestro_documents WHERE ") for s in report.checked) == [
        "tenant_id = %s",
        "tenant_id = %s LIMIT %s",
        "tenant_id = %s OR live",
        "tenant_id = %s OR live LIMIT %s",
    ]
    assert sorted(f.text.split("WHERE ")[1] for f in report.findings) == [
        "tenant_id = %s OR live",
        "tenant_id = %s OR live LIMIT %s",
    ]


def test_a_bound_statement_completed_by_plus_equals_passes(tmp_path: Path) -> None:
    # Only the whole value is read: the base alone (no tenant filter yet) is not a statement of its own.
    report = _scan(
        """
        def f(db, tenant):
            q = "SELECT data FROM nodestro_documents"
            q += " WHERE tenant_id = %s"
            return db._query(q, (tenant,))
        """,
        tmp_path,
    )
    _assert_clean(report)
    assert [s.text for s in report.checked] == ["SELECT data FROM nodestro_documents WHERE tenant_id = %s"]


def test_a_bare_name_that_a_loop_extends_or_with_too_many_variants_is_dynamic(tmp_path: Path) -> None:
    branches = "".join(f"    if a{i}:\n        q += ' AND c{i} = 1'\n" for i in range(10))
    report = _scan(
        "def loop(db, ids):\n"
        "    q = 'SELECT data FROM nodestro_documents WHERE tenant_id = %s'\n"
        "    for i in ids:\n"
        "        q += ' OR id = %s'\n"
        "    db._query(q)\n"
        "\n"
        f"def many(db, {', '.join(f'a{i}' for i in range(10))}):\n"
        "    q = 'SELECT data FROM nodestro_documents WHERE tenant_id = %s'\n" + branches + "    db._query(q)\n",
        tmp_path,
    )
    assert sorted((f.line, f.kind) for f in report.findings) == [(5, "dynamic"), (29, "dynamic")]


def test_the_real_shapes_of_storage_postgres_are_checked_at_the_bare_sink(tmp_path: Path) -> None:
    # replace_if(): `query += " AND ..."` then `self.database._write(query, params)`. Turned into an OR, it leaks.
    source = (RUNTIME / "storage_postgres.py").read_text(encoding="utf-8")
    line = '            query += " AND (data->>%s)::timestamptz > clock_timestamp()"\n'
    assert source.count(line) == 1
    allow = {k: v for k, v in tenant_sql_allowlist.ALLOWLIST.items() if k[0] == "nodestro_runtime/storage_postgres.py"}
    clean = _scan(source, tmp_path / "a", package="nodestro_runtime", module="storage_postgres.py", allowlist=allow)
    _assert_clean(clean)
    assert any(s.text.endswith("(data->>%s)::timestamptz > clock_timestamp()") for s in clean.checked)
    mutated = _scan(
        source.replace(line, line.replace(" AND ", " OR ")),
        tmp_path / "b",
        package="nodestro_runtime",
        module="storage_postgres.py",
        allowlist=allow,
    )
    leaks = [f for f in mutated.findings if f.kind == "leak"]
    assert len(leaks) == 1 and leaks[0].text.endswith("OR (data->>%s)::timestamptz > clock_timestamp()")


# --- texts that start with an unknown part (security re-review SR-01b) ---------------------------------------------

HOLE_LED = {
    "imported fragment + OR": ("from .frag import BASE\n\ndef f(db):\n    db._query(f'{BASE} OR true')\n", "dynamic"),
    "imported fragment + WHERE": (
        "from .frag import BASE\n\ndef f(db):\n    db._query(BASE + ' WHERE id = %s')\n",
        "dynamic",
    ),
    "class attribute via self": (
        "class S:\n    BASE = 'SELECT data FROM nodestro_documents WHERE tenant_id = %s'\n\n"
        "    def f(self, db):\n        db._query(self.BASE + ' OR true')\n",
        "dynamic",
    ),
    "function result": (
        "def base():\n    return 'SELECT data FROM nodestro_documents WHERE tenant_id = %s'\n\n"
        "def f(db):\n    db._query(base() + ' OR true')\n",
        "dynamic",
    ),
    "parameter": ("def f(db, base):\n    db._query(base + ' AND id = %s')\n", "dynamic"),
    "join of a list that is appended to": (
        "def f(db):\n    parts = ['SELECT data FROM nodestro_documents WHERE tenant_id = %s']\n"
        "    parts.append('OR true')\n    db._query(' '.join(parts))\n",
        "dynamic",
    ),
    "str.replace": (
        "Q = 'SELECT data FROM nodestro_documents WHERE tenant_id = %s AND id = %s'\n\n"
        "def f(db):\n    db._query(Q.replace('tenant_id = %s AND ', ''))\n",
        "dynamic",
    ),
    "bytes SQL": ("def f(db):\n    db._query(b'SELECT data FROM nodestro_documents')\n", "leak"),
}


@pytest.mark.parametrize("name", sorted(HOLE_LED))
def test_a_text_led_by_an_unknown_part_is_reported(name: str, tmp_path: Path) -> None:
    source, kind = HOLE_LED[name]
    root = tmp_path / "sample"
    root.mkdir()
    (root / "frag.py").write_text("BASE = 'SELECT data FROM nodestro_documents WHERE tenant_id = %s'\n")
    report = _scan(source, tmp_path)
    assert kind in [f.kind for f in report.findings], name


def test_a_text_led_by_an_unknown_part_passes_only_when_allowlisted(tmp_path: Path) -> None:
    source = "from .frag import BASE\n\ndef f(db):\n    db._query(f'{BASE} OR true')\n"
    report = _scan(source, tmp_path)
    assert [(f.kind, f.text, f.detail) for f in report.findings] == [
        ("dynamic", "{BASE} OR true", "SQL that starts with a part that is not known")
    ]
    report = _scan(source, tmp_path, allowlist={("sample/sample.py", "{BASE} OR true"): "a reason for the test"})
    assert report.findings == [] and [s.text for s in report.allowed] == ["{BASE} OR true"]


def test_a_text_led_by_an_unknown_part_without_sql_words_is_not_a_statement(tmp_path: Path) -> None:
    report = _scan(
        "def f(name, parts):\n    print(f'{name} has no price for the model')\n    return ', '.join(parts)\n",
        tmp_path,
    )
    assert report.findings == [] and report.checked == []


def test_a_join_of_a_constant_list_is_resolved(tmp_path: Path) -> None:
    report = _scan(
        """
        def f(db):
            where = ["tenant_id = %s", "collection = %s"]
            return db._query("SELECT data FROM nodestro_documents WHERE " + " AND ".join(where))
        """,
        tmp_path,
    )
    _assert_clean(report)
    assert [s.text for s in report.checked] == [
        "SELECT data FROM nodestro_documents WHERE tenant_id = %s AND collection = %s"
    ]


# --- security review round 3 (issue #89): the probes that still passed, each one a regression test ------------------

_BASE = '"SELECT data FROM nodestro_documents WHERE tenant_id = %s"'

#: name -> (source, the kind of finding it must produce). Sources that import ``BASE`` get a ``frag.py`` next to them.
ROUND3_PROBES = {
    # SR-01a: `+=` on a parameter (the parameter's value is unknown, the text after it is SQL)
    "+= on a parameter": ('def f(db, q):\n    q += " OR true"\n    db.execute(q)\n', "dynamic"),
    "conditional += on a parameter": (
        'def run(db, q, wide=False):\n    if wide:\n        q += " OR true"\n    return db.execute(q)\n',
        "dynamic",
    ),
    "+= on a parameter with a SQL default": (
        f'def f(db, q={_BASE}):\n    q += " OR true"\n    db.execute(q)\n',
        "dynamic",
    ),
    # SR-01a: closures and lambdas that read a name of the enclosing function
    "lambda reads the outer name": (
        f'def f(db):\n    q = {_BASE}\n    q += " OR true"\n    run = lambda: db.execute(q)\n    run()\n',
        "leak",
    ),
    "nested function reads the outer name": (
        f'def f(db):\n    q = {_BASE}\n    q += " OR true"\n    def g():\n        db.execute(q)\n    g()\n',
        "leak",
    ),
    "nested function widens the outer name (nonlocal)": (
        f'def f(db):\n    q = {_BASE}\n    def widen():\n        nonlocal q\n        q += " OR true"\n'
        "    widen()\n    db.execute(q)\n",
        "dynamic",
    ),
    # SR-01a: `+=` on an attribute, an item or a class attribute
    "+= on self.q": (
        f"class S:\n    def __init__(self):\n        self.q = {_BASE}\n\n    def f(self, db):\n"
        '        self.q += " OR true"\n        db.execute(self.q)\n',
        "dynamic",
    ),
    "+= on a class attribute": (
        f'class S:\n    Q = {_BASE}\n    Q += " OR true"\n\n    def f(self, db):\n        db.execute(self.Q)\n',
        "dynamic",
    ),
    "+= on a dict item": (
        f'Q = {{"a": {_BASE}}}\n\ndef f(db):\n    Q["a"] += " OR true"\n    db.execute(Q["a"])\n',
        "dynamic",
    ),
    "+= on a list item": (
        f'def f(db):\n    qs = [{_BASE}]\n    qs[0] += " OR true"\n    db.execute(qs[0])\n',
        "dynamic",
    ),
    # SR-01a: % formatting with SQL as the argument
    "% formatting wraps a name": (
        f'def f(db):\n    q = {_BASE}\n    q = "%s OR true" % q\n    db.execute(q)\n',
        "leak",
    ),
    "% formatting with a tuple wraps a name": (
        f'def f(db):\n    q = {_BASE}\n    db.execute("%s OR true" % (q,))\n',
        "leak",
    ),
    "% formatting with a dict wraps a name": (
        f'def f(db):\n    q = {_BASE}\n    db.execute("%(q)s OR true" % {{"q": q}})\n',
        "leak",
    ),
    "% formatting of an unknown value": ('def f(db, q):\n    db.execute("%s OR true" % q)\n', "dynamic"),
    # SR-01b: lower-case SQL or a comment after an unknown part
    "tuple-unpacked alias += lower-case or": (
        f'def f(db, x):\n    q = {_BASE}\n    q2, p = q, x\n    q2 += " or true"\n    db.execute(q2, p)\n',
        "dynamic",
    ),
    "method result += lower-case or": (
        "class S:\n    def _base(self):\n"
        f"        return {_BASE}\n\n"
        '    def f(self, db):\n        q = self._base()\n        q += " or true"\n        db.execute(q)\n',
        "dynamic",
    ),
    "self.BASE + lower-case or": (
        f'class S:\n    BASE = {_BASE}\n\n    def f(self, db):\n        db.execute(self.BASE + " or true")\n',
        "dynamic",
    ),
    "imported fragment + lower-case or": (
        'from .frag import BASE\n\ndef f(db):\n    db.execute(f"{BASE} or 1=1")\n',
        "dynamic",
    ),
    "imported fragment + comment": ('from .frag import BASE\n\ndef f(db):\n    db.execute(BASE + " --")\n', "dynamic"),
    "imported fragment + block comment": (
        'from .frag import BASE\n\ndef f(db):\n    db.execute(BASE + " /* x */")\n',
        "dynamic",
    ),
    # SR-01b: SQL fragments that only unknown parts put together
    "walrus through a call": (
        f'import operator\n\ndef f(db):\n    q = {_BASE}\n    (q := operator.add(q, " OR true"))\n    db.execute(q)\n',
        "dynamic",
    ),
    "two class attributes": (
        f'class S:\n    BASE = {_BASE}\n    WIDE = " OR true"\n\n'
        "    def f(self, db):\n        db.execute(self.BASE + self.WIDE)\n",
        "dynamic",
    ),
    "tuple items by index": (
        f'QS = ({_BASE}, " OR true")\n\ndef f(db):\n    db.execute(QS[0] + QS[1])\n',
        "leak",
    ),
    # SR-01d: a table name split across constant pieces
    "table name split by + of constants": (
        'def f(db):\n    db.execute("SELECT data FROM nodestro_" + "documents WHERE id = %s")\n',
        "leak",
    ),
    "table name completed after a constant prefix": (
        'P = "nodestro_"\n\ndef f(db):\n    db.execute(f"SELECT data FROM {P}documents WHERE id = %s")\n',
        "leak",
    ),
    "table name split by a constant between": (
        'P = "nodestro_"\n\ndef f(db):\n    db.execute("SELECT data FROM " + P + "documents WHERE id = %s")\n',
        "leak",
    ),
    "table name joined from parts": (
        'def f(db):\n    t = "_".join(["nodestro", "documents"])\n'
        '    db.execute(f"SELECT data FROM {t} WHERE id = %s")\n',
        "leak",
    ),
    # A quoted name in another case is another table in PostgreSQL; the check treats it as the tenant table.
    "quoted table name in mixed case": ("Q = 'SELECT data FROM \"Nodestro_documents\" WHERE id = %s'\n", "leak"),
}


def _scan_probe(source: str, tmp_path: Path) -> Report:
    root = tmp_path / "sample"
    root.mkdir(exist_ok=True)
    (root / "frag.py").write_text(f"BASE = {_BASE}\n", encoding="utf-8")
    return _scan(source, tmp_path)


@pytest.mark.parametrize("name", sorted(ROUND3_PROBES))
def test_the_round3_probes_are_caught(name: str, tmp_path: Path) -> None:
    source, kind = ROUND3_PROBES[name]
    report = _scan_probe(source, tmp_path)
    assert kind in [f.kind for f in report.findings], (name, [str(f) for f in report.findings])


ROUND3_CLEAN = {
    "+= of an AND filter": (
        f'def f(db, x):\n    q = {_BASE}\n    if x:\n        q += " AND id = %s"\n    db.execute(q)\n'
    ),
    "alias, then only the original is extended": (
        f'def f(db):\n    q = {_BASE}\n    q2 = q\n    q += " AND id = %s"\n    db.execute(q2)\n    db.execute(q)\n'
    ),
    "% formatting of a bound statement": (
        "def f(db, t):\n"
        '    db.execute("%s AND id = %%s" % "SELECT data FROM nodestro_documents WHERE tenant_id = %s")\n'
    ),
    "messages with lower-case words after an unknown part": (
        "def f(name, exe, parts, n):\n"
        "    print(f'{name} must be yes or no')\n"
        "    print(f'{name} is used by another process; stop it first or choose another data directory')\n"
        "    print(name + ' or enter it in the settings', ' or '.join(parts), f'{exe} --version')\n"
        "    print(f'{name} would call itself (directly or through other sub-workflows)')\n"
        "    print('%s must be 1 or 0' % name)\n"
    ),
    "counters and text built with +=": (
        "class R:\n    def f(self, delta, index):\n        self.count += 1\n        self.text += delta\n"
        "        self.counts['a'] += 1\n        index += 1\n        return index\n"
    ),
    "a closure that reads a value that is not SQL": (
        "def f(items):\n    label = 'x'\n    label += 'y'\n    return sorted(items, key=lambda i: (label, i))\n"
    ),
}


@pytest.mark.parametrize("name", sorted(ROUND3_CLEAN))
def test_the_round3_controls_pass(name: str, tmp_path: Path) -> None:
    _assert_clean(_scan_probe(ROUND3_CLEAN[name], tmp_path))


ROUND3_LEAKING_SQL = [
    # the inner `d` shadows the outer one: `d.tenant_id = d.tenant_id` is true for every row of the inner table
    "SELECT (SELECT count(*) FROM nodestro_jobs d WHERE d.tenant_id = d.tenant_id) FROM nodestro_documents d"
    " WHERE d.tenant_id = %s",
    "SELECT (SELECT count(*) FROM nodestro_jobs j, axentra_memberships d WHERE j.tenant_id = d.tenant_id)"
    " FROM nodestro_documents d WHERE d.tenant_id = %s",
    # SQL that the server runs from a string: not checked, so not passed
    "DO $$ BEGIN DELETE FROM nodestro_documents; END $$",
    "SELECT query_to_xml('SELECT * FROM nodestro_documents', true, true, '')",
    # writes that put a row into, or take it from, another tenant
    "INSERT INTO nodestro_jobs (tenant_id, kind, ref) VALUES ('other', %s, %s)",
    "INSERT INTO nodestro_jobs (tenant_id, kind, ref) VALUES (%s, %s, %s), ('other', %s, %s)",
    "INSERT INTO nodestro_jobs (tenant_id, kind, ref) SELECT 'other', kind, ref FROM nodestro_jobs"
    " WHERE tenant_id = %s",
    "INSERT INTO nodestro_jobs (tenant_id, id, status) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE"
    " SET status = excluded.status",
    "INSERT INTO nodestro_jobs (tenant_id, id, status) VALUES (%s, %s, %s) ON CONFLICT ON CONSTRAINT k DO UPDATE"
    " SET status = excluded.status",
    "INSERT INTO nodestro_jobs (tenant_id, id, status) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE"
    " SET tenant_id = excluded.tenant_id",
    "UPDATE nodestro_jobs SET tenant_id = %s WHERE tenant_id = %s AND id = %s",
]

ROUND3_BOUND_SQL = [
    "SELECT d.id, (SELECT max(ref) FROM nodestro_jobs WHERE tenant_id = d.tenant_id) FROM nodestro_documents d"
    " WHERE d.tenant_id = %s",
    "SELECT d.data FROM nodestro_documents d, LATERAL (SELECT * FROM nodestro_jobs j WHERE j.tenant_id = d.tenant_id)"
    " x WHERE d.tenant_id = %s",
    "INSERT INTO nodestro_run_events (tenant_id, run_id, seq, event) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
    "INSERT INTO nodestro_jobs (tenant_id, kind, ref) VALUES (%s::text, %s, %s), (%s, %s, %s)",
    "INSERT INTO nodestro_jobs (tenant_id, kind, ref) SELECT %s, 'k', x FROM unnest(%s::text[]) AS t(x)",
    "INSERT INTO nodestro_jobs (tenant_id, kind, ref) SELECT d.tenant_id, 'k', d.id FROM nodestro_documents d"
    " WHERE d.tenant_id = %s",
    "INSERT INTO nodestro_files (tenant_id, namespace, id, entry) VALUES (%s, %s, %s, %s)"
    " ON CONFLICT (tenant_id, namespace, id) DO UPDATE SET tenant_id = excluded.tenant_id, entry = excluded.entry",
    "SELECT count(*) FROM pg_class WHERE relname = 'nodestro_documents'",
]


@pytest.mark.parametrize("text", ROUND3_LEAKING_SQL)
def test_the_round3_sql_probes_fail(text: str) -> None:
    assert check_sql(text, TABLES) != []


@pytest.mark.parametrize("text", ROUND3_BOUND_SQL)
def test_the_round3_bound_sql_passes(text: str) -> None:
    assert check_sql(text, TABLES) == []


def test_a_fragment_outside_a_known_statement_passes_only_when_fixed_or_allowlisted(tmp_path: Path) -> None:
    source = "class S:\n    WIDE = ' OR true'\n"
    report = _scan(source, tmp_path)
    assert [(f.kind, f.text, f.detail) for f in report.findings] == [
        ("dynamic", "{statement not known} OR true", "SQL that continues a statement that is not known")
    ]
    allowlist = {("sample/sample.py", "{statement not known} OR true"): "a reason for the test"}
    report = _scan(source, tmp_path, allowlist=allowlist)
    assert report.findings == [] and len(report.allowed) == 1
    # The value of a followed `name += ...` is checked where the name is read, not as a fragment.
    _assert_clean(_scan(f"def f(db):\n    q = {_BASE}\n    q += ' AND id = %s'\n    db.execute(q)\n", tmp_path))


# --- security review round 4: the probes that still passed, each one a regression test ----------------------------

#: name -> (source, the kind of finding it must produce). Sources that import ``BASE`` get a ``frag.py`` next to them.
ROUND4_PROBES = {
    # R4-3: a text changed by ``.replace()`` (or another changing method) on a receiver that is not a name
    "self.BASE.replace": (
        f"class S:\n    BASE = {_BASE}\n\n    def f(self, db):\n"
        '        db.execute(self.BASE.replace("tenant_id = %s", "true"))\n',
        "dynamic",
    ),
    "Q[item].replace": (
        f'Q = {{"a": {_BASE}}}\n\ndef f(db):\n    db.execute(Q["a"].replace("tenant_id = %s", "true"))\n',
        "dynamic",
    ),
    "base().replace": (
        f'def base():\n    return {_BASE}\n\ndef f(db):\n    db.execute(base().replace("tenant_id = %s", "true"))\n',
        "dynamic",
    ),
    "str.replace(BASE, ...)": (
        f'BASE = {_BASE}\n\ndef f(db):\n    db.execute(str.replace(BASE, "tenant_id = %s", "true"))\n',
        "dynamic",
    ),
    "imported fragment .replace with constant arguments": (
        'from .frag import BASE\n\nOLD = "tenant_id = %s"\nNEW = "true"\n\n'
        "def f(db):\n    db.execute(BASE.replace(OLD, NEW))\n",
        "dynamic",
    ),
    "SQL in an argument of .replace on a known text": (
        'def f(db):\n    db.execute("x".replace("x", "DELETE FROM nodestro_documents"))\n',
        "leak",
    ),
    # R4-4: a module-level name read before it is assigned again (the value at the read, not the last one)
    "module-level read before a reassignment": (
        f'Q = {_BASE}\nQ += " OR true"\nWIDE = Q\nQ = {_BASE}\n\ndef f(db):\n    db.execute(WIDE)\n',
        "leak",
    ),
    "module-level call before a reassignment": (
        f'Q = {_BASE}\nQ += " OR true"\nrun(Q)\nQ = {_BASE}\n',
        "leak",
    ),
    "function reads a module name that is reassigned": (
        f'Q = {_BASE}\nQ += " OR true"\n\ndef f(db):\n    db.execute(Q)\n\nf(None)\nQ = {_BASE}\n',
        "leak",
    ),
    # R4-5: a comment in front of an unknown part, and fragments that start with a comment or an operator
    "block comment before an unknown part": (
        'from .frag import BASE\n\ndef f(db):\n    db.execute(f"/* tag */ {BASE} OR true")\n',
        "dynamic",
    ),
    "line comment before an unknown part": (
        'from .frag import BASE\n\ndef f(db):\n    db.execute(f"-- tag\\n{BASE} OR true")\n',
        "dynamic",
    ),
    "fragment that starts with a comment": ('class S:\n    WIDE = "/**/OR true"\n', "dynamic"),
    "fragment that starts with IS": ('class S:\n    WIDE = " IS NOT NULL OR true"\n', "dynamic"),
    "fragment that starts with is, lower case": ('class S:\n    WIDE = " is not null or true"\n', "dynamic"),
    # R4-6: lower-case condition in parentheses after an unknown part
    "lower-case or (1=1)": ('from .frag import BASE\n\ndef f(db):\n    db.execute(f"{BASE} or (1=1)")\n', "dynamic"),
    "lower-case and (true)": (
        'from .frag import BASE\n\ndef f(db):\n    db.execute(BASE + " and (true)")\n',
        "dynamic",
    ),
}


@pytest.mark.parametrize("name", sorted(ROUND4_PROBES))
def test_the_round4_probes_are_caught(name: str, tmp_path: Path) -> None:
    source, kind = ROUND4_PROBES[name]
    report = _scan_probe(source, tmp_path)
    assert kind in [f.kind for f in report.findings], (name, [str(f) for f in report.findings])


ROUND4_CLEAN = {
    "text methods that are not SQL": (
        "class S:\n    def f(self, name, path):\n"
        "        a = self.name.replace('-', '_').lower()\n"
        "        b = str.replace(name, ' ', '')\n"
        "        c = path().removesuffix('.py').upper()\n"
        "        return a, b, c, self.items['x'].strip()\n"
    ),
    "a module name assigned once and read in a function": (
        f"Q = {_BASE}\n\ndef f(db):\n    db.execute(Q)\n    db.execute(Q + ' AND id = %s')\n"
    ),
    "a module name completed before it is read": (
        f'Q = {_BASE}\nQ += " AND id = %s"\nrun(Q)\n\ndef f(db):\n    db.execute(Q)\n'
    ),
    "messages with comments, is and parentheses": (
        "def f(name, exe):\n"
        "    print(f'-- {name} --')\n"
        "    print(f'/* {name} */ is a comment')\n"
        "    print(f'{name} or (optionally) the path')\n"
        "    print(' is not set or empty')\n"
        "    print(' is not allowed in a sandbox container', ' in the settings or (optionally) here')\n"
        "    print(f'{exe} and (if needed) {name}')\n"
    ),
}


@pytest.mark.parametrize("name", sorted(ROUND4_CLEAN))
def test_the_round4_controls_pass(name: str, tmp_path: Path) -> None:
    _assert_clean(_scan_probe(ROUND4_CLEAN[name], tmp_path))
