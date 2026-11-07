"""SQL parity before and after the statement catalog (MUP-ONA B4, T-PAR-1).

``tests/fixtures/sql_parity_before.json`` lists every statement the tenant SQL check found in ``postgres_tables.py``
before B4 changed code (written by ``tests/fixtures/make_sql_parity.py`` at that commit, never regenerated). B4 moved
most of them into the catalog of :mod:`nodestro_runtime.pg_generic._sql`, which the check sees only rendered
(``tests/test_sql_catalog.py``). T-PAR-1 proves that no statement dropped out of the check: every statement of the
baseline is found again -- in the source the check reads now (``postgres_tables.py`` and the generic package), or in
the catalog rendered with ``nodestro_`` -- compared normalized (PostgreSQL's grammar prints both back, placeholders
as ``$?``: B4 changed positional placeholders into named ones and nothing else), and with no finding the baseline did
not have.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

import nodestro_runtime
from nodestro_runtime import tenant_sql_allowlist
from nodestro_runtime.pg_generic._sql import CATALOG, render
from nodestro_runtime.sqlcheck_rules import check_statement
from nodestro_runtime.tenant_sql_check import statements_of, tenant_tables, to_postgres_params

BASELINE = Path(__file__).parent / "fixtures" / "sql_parity_before.json"
PACKAGE = Path(nodestro_runtime.__file__).parent
TABLES = frozenset(tenant_tables(PACKAGE / "migrations" / "postgres"))
#: The modules whose source the check reads after B4 (relative to the package's parent, as the check names them).
AFTER_MODULES = (
    "nodestro_runtime/postgres_tables.py",
    *sorted(f"nodestro_runtime/pg_generic/{path.name}" for path in (PACKAGE / "pg_generic").glob("*.py")),
)


def normalized(text: str) -> str:
    """``text`` as PostgreSQL's grammar prints it back, every placeholder as ``$?``."""
    from pglast import parse_sql
    from pglast.stream import RawStream

    printed = "; ".join(RawStream()(raw) for raw in parse_sql(to_postgres_params(text)))
    return re.sub(r"\$\d+", "$?", printed)


def _kinds(text: str) -> set[str]:
    return {finding.kind for finding in check_statement(text, tenant_tables=TABLES)}


def after() -> dict[str, tuple[str, set[str]]]:
    """Normalized text -> (where, finding kinds) of every statement the check sees after B4."""
    found: dict[str, tuple[str, set[str]]] = {}
    for module in AFTER_MODULES:
        source = (PACKAGE.parent / module).read_text(encoding="utf-8")
        for statement in statements_of(source, module, TABLES):
            assert not statement.dynamic, (module, statement.text)
            assert (module, statement.text) not in tenant_sql_allowlist.ALLOWLIST, (module, statement.text)
            found[normalized(statement.text)] = (f"{module}:{statement.line}", _kinds(statement.text))
    for name in CATALOG:
        text = render(name, "nodestro_").as_string(None)
        found.setdefault(normalized(text), (f"catalog {name}", _kinds(text)))
    return found


# MUP-ONA T-PAR-1
def test_every_statement_of_the_baseline_is_still_checked() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    entries = baseline["statements"]
    assert len(entries) == 46
    assert baseline["tenant_tables"] == sorted(TABLES)
    now = after()
    missing = []
    for entry in entries:
        assert entry["canonical"] is not None, entry  # the baseline had no dynamic or unparsed statement
        key = re.sub(r"\$\d+", "$?", entry["canonical"])
        if key not in now:
            missing.append(entry["text"])
            continue
        where, kinds = now[key]
        assert kinds <= set(entry["findings"]), (where, kinds, entry)
    assert missing == []


def test_the_baseline_splits_into_catalog_and_nodestro_source() -> None:
    """Of the 46 statements, 42 are the catalog's partitioned entries and 4 stay in Nodestro's source (chunk counts,
    attaching a file, a source's text, the memory totals); the catalog adds only the unpartitioned pgvector probe."""
    baseline = {
        re.sub(r"\$\d+", "$?", entry["canonical"])
        for entry in json.loads(BASELINE.read_text(encoding="utf-8"))["statements"]
    }
    catalog = {normalized(render(name, "nodestro_").as_string(None)): name for name in CATALOG}
    assert set(catalog) - baseline == {normalized(render("vectors.pgvector", "nodestro_").as_string(None))}
    source = PACKAGE / "postgres_tables.py"
    kept = {normalized(s.text) for s in statements_of(source.read_text(encoding="utf-8"), "postgres_tables.py", TABLES)}
    assert len(kept) == 4
    assert baseline == (set(catalog) - {normalized("SELECT to_regtype('vector') IS NOT NULL")}) | kept


@pytest.mark.parametrize(
    "text",
    [
        "SELECT a FROM nodestro_chunks WHERE tenant_id = %s AND id = %s",
        'SELECT a FROM "nodestro_chunks" WHERE tenant_id = %(tenant_id)s AND id = %(id)s',
        "select a from nodestro_chunks where tenant_id = $1 and id = $2",
    ],
)
def test_normalization_ignores_quoting_case_and_placeholder_style(text: str) -> None:
    assert normalized(text) == "SELECT a FROM nodestro_chunks WHERE tenant_id = $? AND id = $?"
