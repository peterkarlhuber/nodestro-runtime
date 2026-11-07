"""The rule engine of the tenant SQL check (:mod:`orkexa.testing.sqlcheck`; MUP-ONA B4, B6).

B4 separated the engine from Nodestro's check as ``nodestro_runtime.sqlcheck_rules``, B5 took it over into orkexa
unchanged, and B6 removed Nodestro's copy: the check (:mod:`nodestro_runtime.tenant_sql_check`) now calls Orkexa's
engine. Nodestro's source code check (``tests/test_tenant_filters.py``) runs the engine with ``tenant_id`` and
Nodestro's tables; the cases here run it on its own, against the engine at the pinned orkexa commit. T-SQLC-1 proves
that it is generic: with a foreign partition column (``org_id``) and foreign tables (``t_docs``, ``t_notes``) the same
rules hold, and Nodestro's column and tables mean nothing to it. It imports nothing but ``pglast`` and the standard
library and holds no Nodestro constant.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import subprocess
import sys

from orkexa.testing import sqlcheck
from orkexa.testing.sqlcheck import KINDS, Finding, check_statement
import pytest

ENGINE = Path(sqlcheck.__file__)
BASELINE = Path(__file__).parent / "fixtures" / "sql_parity_before.json"

#: The foreign set of T-SQLC-1.
COLUMN = "org_id"
TABLES = frozenset({"t_docs", "t_notes"})


def _kinds(sql: str, **options: object) -> list[str]:
    options = {"tenant_tables": TABLES, "column": COLUMN, **options}
    return [f.kind for f in check_statement(sql, **options)]  # type: ignore[arg-type]


# --- T-SQLC-1: a foreign column name and table set ---------------------------------------------------------------

GENERIC_BOUND = [
    "SELECT body FROM t_docs WHERE org_id = %s AND id = %s",
    "SELECT d.body FROM t_docs d WHERE d.org_id = %s::text",
    "SELECT d.body, n.text FROM t_docs d JOIN t_notes n ON n.doc = d.id AND n.org_id = d.org_id WHERE d.org_id = %s",
    "SELECT body FROM t_docs WHERE org_id = %s AND id IN (SELECT doc FROM t_notes WHERE org_id = %s)",
    "WITH x AS (SELECT id FROM t_docs WHERE org_id = %s) SELECT * FROM x",
    "UPDATE t_docs SET body = %s WHERE org_id = %s AND id = %s",
    "DELETE FROM t_notes WHERE org_id = %(org)s AND doc = %(doc)s",
    "INSERT INTO t_docs (org_id, id, body) VALUES (%s, %s, %s)",
    "INSERT INTO t_docs (org_id, id, body) VALUES (%s, %s, %s) ON CONFLICT (org_id, id) DO UPDATE SET body = "
    "excluded.body",
    "INSERT INTO t_notes (org_id, doc) SELECT d.org_id, d.id FROM t_docs d WHERE d.org_id = %s",
    # Nodestro's tables and column are nothing to the engine with this set.
    "SELECT data FROM nodestro_documents WHERE collection = %s",
    "SELECT body FROM other WHERE tenant_id = %s",
    "CREATE INDEX t_docs_org ON t_docs (org_id)",
    "ALTER TABLE t_docs ADD COLUMN extra text",
]

GENERIC_LEAKING = [
    "SELECT body FROM t_docs WHERE id = %s",
    "SELECT body FROM t_docs WHERE org_id = %s OR id = %s",
    "SELECT body FROM t_docs WHERE org_id = 'acme'",
    # Nodestro's column binds nothing here.
    "SELECT body FROM t_docs WHERE tenant_id = %s",
    "SELECT d.body FROM t_docs d JOIN t_notes n ON n.doc = d.id WHERE d.org_id = %s",
    "SELECT d.body FROM t_docs d LEFT JOIN t_notes n ON n.doc = d.id AND d.org_id = %s",
    "SELECT body FROM t_docs WHERE org_id = %s UNION SELECT text FROM t_notes",
    "SELECT body FROM t_docs WHERE org_id = %s AND id IN (SELECT doc FROM t_notes)",
    "UPDATE t_docs SET org_id = %s WHERE org_id = %s AND id = %s",
    "UPDATE t_docs SET body = %s",
    "DELETE FROM t_notes",
    "INSERT INTO t_docs (id, body) VALUES (%s, %s)",
    "INSERT INTO t_docs (org_id, id) VALUES ('acme', %s)",
    "INSERT INTO t_docs (org_id, id) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET body = excluded.body",
    "INSERT INTO t_docs (org_id, id) VALUES (%s, %s) ON CONFLICT (org_id, id) DO UPDATE SET org_id = %s",
    "WITH t_docs AS (SELECT 1 AS id) SELECT * FROM t_docs",
    # Table names in any case are the same table.
    "SELECT body FROM T_Docs WHERE id = %s",
]


@pytest.mark.parametrize("sql", GENERIC_BOUND)
def test_sqlc1_a_foreign_column_and_table_set_binds(sql: str) -> None:
    assert check_statement(sql, tenant_tables=TABLES, column=COLUMN) == []


@pytest.mark.parametrize("sql", GENERIC_LEAKING)
def test_sqlc1_a_foreign_column_and_table_set_leaks(sql: str) -> None:
    findings = check_statement(sql, tenant_tables=TABLES, column=COLUMN)
    assert findings and {f.kind for f in findings} == {"leak"}


def test_sqlc1_messages_name_the_column_and_table() -> None:
    [finding] = check_statement("SELECT body FROM t_docs d WHERE id = %s", tenant_tables=TABLES, column=COLUMN)
    assert finding.message == "t_docs (as d) is not bound to one tenant by a top-level `org_id = <param>` conjunct"
    [insert] = check_statement("INSERT INTO t_docs (id) VALUES (%s)", tenant_tables=TABLES, column=COLUMN)
    assert insert.message == "the INSERT into t_docs lists no org_id column"


def test_sqlc1_tenant_tables_are_matched_in_any_case() -> None:
    assert _kinds("SELECT body FROM t_docs WHERE id = %s", tenant_tables=["T_DOCS"]) == ["leak"]
    assert _kinds("SELECT body FROM t_docs WHERE org_id = %s", tenant_tables=("T_Docs",)) == []


def test_sqlc1_the_real_statements_hold_with_another_column_and_prefix() -> None:
    """Every statement of the parity baseline (postgres_tables.py before B4), with the tables renamed to ``t_`` and
    ``tenant_id`` to ``org_id``: bound with the foreign set, and a leak once its ``org_id = %s`` conjuncts are gone."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    statements = [entry["text"] for entry in baseline["statements"]]
    tables = frozenset(table.replace("nodestro_", "t_") for table in baseline["tenant_tables"])
    assert len(statements) > 40
    mutated = 0
    for text in statements:
        sql = text.replace("nodestro_", "t_").replace("tenant_id", COLUMN)
        assert check_statement(sql, tenant_tables=tables, column=COLUMN) == [], sql
        # With Nodestro's own column the renamed statement binds nothing.
        assert check_statement(sql, tenant_tables=tables) != [], sql
        unbound = re.sub(r"\b(?:\w+\.)?org_id = %s", "true", sql)
        if unbound != sql:
            mutated += 1
            assert "leak" in [f.kind for f in check_statement(unbound, tenant_tables=tables, column=COLUMN)], unbound
    assert mutated > 30


# --- ANY ---------------------------------------------------------------------------------------------------------


def test_any_binds_only_with_allow_any() -> None:
    sql = "SELECT body FROM t_docs WHERE org_id = ANY(%s)"
    findings = check_statement(sql, tenant_tables=TABLES, column=COLUMN)
    assert [f.kind for f in findings] == ["leak", "leak"]
    assert findings[1].message == sqlcheck.ANY_REFUSED
    assert check_statement(sql, tenant_tables=TABLES, column=COLUMN, allow_any=True) == []
    # ANY on another column never binds.
    assert _kinds("SELECT body FROM t_docs WHERE id = ANY(%s)", allow_any=True) == ["leak"]
    # ANY of a constant array is no parameter: not even the reason for ANY is given.
    assert _kinds("SELECT body FROM t_docs WHERE org_id = ANY('{a,b}')") == ["leak"]


# --- kinds, positions, placeholders -------------------------------------------------------------------------------


def test_a_text_the_parser_rejects_is_an_unparsed_finding_not_an_error() -> None:
    [finding] = check_statement("SELECT body FROM WHERE org_id = %s", tenant_tables=TABLES, column=COLUMN)
    assert finding.kind == "unparsed" and "syntax error" in finding.message
    assert finding.position == len("SELECT body FROM ")


@pytest.mark.parametrize(
    "sql",
    [
        "TRUNCATE t_docs",
        "COPY t_docs TO STDOUT",
        "MERGE INTO t_docs d USING src s ON d.id = s.id WHEN MATCHED THEN DELETE",
        "DO $$ BEGIN DELETE FROM t_docs; END $$",
        "SELECT query_to_xml('SELECT * FROM t_docs', true, true, '')",
    ],
)
def test_statements_the_rules_cannot_decide_are_unsupported(sql: str) -> None:
    assert "unsupported" in _kinds(sql)


def test_a_string_that_is_only_a_table_name_is_not_sql() -> None:
    assert _kinds("SELECT relname FROM pg_class WHERE relname = 't_docs'") == []


def test_every_finding_has_a_known_kind_and_a_position_in_the_text_as_given() -> None:
    sql = "SELECT 1 FROM t_notes WHERE org_id = %(org)s; SELECT body FROM t_docs WHERE id = %(id)s"
    [finding] = check_statement(sql, tenant_tables=TABLES, column=COLUMN)
    assert isinstance(finding, Finding) and finding.kind in KINDS
    # Named placeholders are longer than $n: the position is still one of the text as given.
    assert sql[finding.position :].startswith("t_docs")
    assert str(finding).startswith(f"[leak] at {finding.position}: t_docs is not bound")


def test_postgres_placeholders() -> None:
    assert _kinds("SELECT body FROM t_docs WHERE org_id = $1", placeholder="postgres") == []
    assert _kinds("SELECT body FROM t_docs WHERE org_id = $1 OR true", placeholder="postgres") == ["leak"]
    # psycopg's %s is no PostgreSQL syntax.
    assert _kinds("SELECT body FROM t_docs WHERE org_id = %s", placeholder="postgres") == ["unparsed"]
    # With psycopg placeholders, %% is a literal percent sign.
    assert _kinds("SELECT body FROM t_docs WHERE org_id = %s AND body LIKE 'a%%'") == []


def test_to_postgres_params() -> None:
    assert sqlcheck.to_postgres_params("a = %s AND b = %(x)s AND c = %(x)s AND d LIKE '5%%' %b %t") == (
        "a = $1 AND b = $2 AND c = $2 AND d LIKE '5%' $3 $4"
    )


@pytest.mark.parametrize(
    ("option", "value"),
    [("placeholder", "qmark"), ("column", ""), ("column", "Org_Id"), ("column", "org id"), ("column", '"org"')],
)
def test_invalid_options_are_refused(option: str, value: str) -> None:
    with pytest.raises(ValueError, match=option):
        check_statement("SELECT 1", tenant_tables=TABLES, **{option: value})  # type: ignore[arg-type]


# --- what the engine may depend on -------------------------------------------------------------------------------


def _code(tree: ast.Module) -> list[ast.AST]:
    """Every node of ``tree`` except docstrings."""
    docstrings = {
        id(node.body[0])
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if id(node) in docstrings:
            skipped.update(id(n) for n in ast.walk(node))
    return [n for n in ast.walk(tree) if id(n) not in skipped]


def test_the_engine_imports_only_pglast_and_the_standard_library() -> None:
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            imported.add(node.module or "")
    tops = {name.split(".")[0] for name in imported}
    assert tops - {"__future__"} <= set(sys.stdlib_module_names), tops
    # pglast is imported late and by name only (importlib).
    names = {n.value for n in _code(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert {"pglast", "parser", "ast", "enums"} <= names


def test_the_engine_holds_no_nodestro_constant() -> None:
    for node in _code(ast.parse(ENGINE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "nodestro" not in node.value.lower(), node.value
        if isinstance(node, ast.Name):
            assert "nodestro" not in node.id.lower(), node.id


def test_importing_the_engine_does_not_import_the_parser() -> None:
    code = "import sys, orkexa.testing.sqlcheck; print('pglast' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120)
    assert result.stdout.strip() == "False"
