"""The statement catalog of the generic PostgreSQL part (:mod:`nodestro_runtime.pg_generic._sql`, MUP-ONA B4).

The tenant SQL check reads Nodestro's SQL from the source (``tests/test_tenant_filters.py``); the generic part's SQL is
its catalog, whose templates name the tables only as ``{chunks}`` ... So the catalog is checked here, rendered:

* **T-CAT-1** every entry rendered with Nodestro's prefix ``nodestro_`` is bound to one tenant by the rule engine
  (:func:`nodestro_runtime.sqlcheck_rules.check_statement` with Nodestro's tenant tables from the migrations); no
  ``leak``, ``unparsed`` or ``unsupported``.
* **T-CAT-2** mutation probe: for every partitioned entry, each ``tenant_id`` predicate removed, each ``AND`` next to
  it turned into ``OR``, and (``INSERT ... VALUES``) the ``tenant_id`` column dropped, is a ``leak``. With
  ``NODESTRO_CATALOG_REPORTS`` set, the probe's outcome is written to ``<dir>/mutation-probe.json`` (a CI artifact).
* **T-CAT-3** completeness: the catalog's names are exactly :data:`EXPECTED` (with their partition flag); a new entry
  fails until it is listed here, and every partitioned entry gets the mutation probe and the guard cases of
  ``tests/test_partition_guard.py`` by parametrization.
* **T-CAT-4** AST checks: outside ``_sql.py`` no module of the package calls ``.execute``; no ``sql.Literal`` and no
  private API of Nodestro's ``PostgresDatabase``; the package imports nothing of Nodestro.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

import pytest

import nodestro_runtime
from nodestro_runtime import pg_generic
from nodestro_runtime.pg_generic._sql import CATALOG, PARTITION, TABLES, Statement, render
from nodestro_runtime.sqlcheck_rules import check_statement
from nodestro_runtime.tenant_sql_check import tenant_tables

PACKAGE = Path(pg_generic.__file__).parent
TENANT_TABLES = frozenset(tenant_tables(Path(nodestro_runtime.__file__).parent / "migrations" / "postgres"))
PREFIX = "nodestro_"

#: T-CAT-3: every statement of the catalog and whether it is partitioned by tenant. An unpartitioned entry names no
#: table (it says why here).
EXPECTED: dict[str, bool] = {
    "files_update_entry": True,
    "files_upsert": True,
    "files_entry": True,
    "files_data": True,
    "files_entries": True,
    "files_delete": True,
    "chunks_count": True,
    "chunks_sources": True,
    "chunks_insert": True,
    "chunks_delete_source": True,
    "chunks_max_id": True,
    "chunks_missing_up_to": True,
    "chunks_missing": True,
    "chunks_count_missing": True,
    "spaces_lock_shared": True,
    "spaces_lock_exclusive": True,
    "spaces_by_state": True,
    "spaces_insert_active": True,
    "spaces_insert_building": True,
    "spaces_activate": True,
    "spaces_delete": True,
    "spaces_delete_others": True,
    "spaces_delete_all": True,
    "vectors_insert": True,
    "vectors_store": True,
    "vectors_count": True,
    "vectors_count_excluded": True,
    "vectors_pgvector": False,  # whether pgvector is installed: to_regtype('vector'), no table
    "search_fts": True,
    "search_fts_sources": True,
    "search_pgvector": True,
    "search_pgvector_sources": True,
    "search_candidates": True,
    "search_candidates_sources": True,
    "search_hits": True,
    "search_word_counts": True,
    "search_word_hits": True,
    "memory_read": True,
    "memory_write": True,
    "memory_files": True,
    "memory_delete": True,
    "memory_rename": True,
    "memory_clear": True,
}
PARTITIONED = sorted(name for name, partitioned in EXPECTED.items() if partitioned)

#: A predicate that binds a reference to the tenant (``c.tenant_id = %(tenant_id)s``).
_PREDICATE = re.compile(r"(?:\b\w+\.)?tenant_id = %\(tenant_id\)s")


def _text(template: str) -> str:
    """A template as text, with Nodestro's tables (as rendering would name them, without quotes)."""
    return template.format(**{k: PREFIX + v for k, v in TABLES.items()})


def _findings(text: str) -> list[str]:
    return [f.kind for f in check_statement(text, tenant_tables=TENANT_TABLES)]


# ---- T-CAT-1 --------------------------------------------------------------------------------------------------------


# MUP-ONA T-CAT-1
@pytest.mark.parametrize("name", sorted(CATALOG))
def test_every_rendered_entry_is_bound_to_one_tenant(name: str) -> None:
    rendered = render(name, PREFIX).as_string(None)
    assert check_statement(rendered, tenant_tables=TENANT_TABLES) == []
    # Rendering only quotes the table names: the text is the template with Nodestro's tables.
    assert rendered.replace('"', "") == _text(CATALOG[name].template).replace('"', "")
    if CATALOG[name].partitioned:
        assert {PREFIX + TABLES[table] for table in CATALOG[name].tables} <= TENANT_TABLES
    else:
        assert not CATALOG[name].tables


def test_the_rule_engine_sees_the_tables_of_the_catalog() -> None:
    """The catalog's tables are Nodestro's tenant tables (else T-CAT-1 would pass for want of a tenant table)."""
    assert {PREFIX + table for table in TABLES.values()} <= TENANT_TABLES


# ---- T-CAT-2: mutation probe ----------------------------------------------------------------------------------------


def mutations(entry: Statement) -> list[tuple[str, str]]:
    """``(what, text)`` of each mutation of a partitioned entry that must leak."""
    text = _text(entry.template)
    found: list[tuple[str, str]] = []
    for match in _PREDICATE.finditer(text):
        found.append(
            (f"remove {match.group(0)!r} at {match.start()}", text[: match.start()] + "TRUE" + text[match.end() :])
        )
        after = text.find(" AND ", match.end())
        if after == match.end():
            found.append(
                (f"AND->OR after {match.group(0)!r} at {match.start()}", text[:after] + " OR " + text[after + 5 :])
            )
        elif text[: match.start()].endswith(" AND "):
            before = match.start() - 5
            found.append(
                (
                    f"AND->OR before {match.group(0)!r} at {match.start()}",
                    text[:before] + " OR " + text[match.start() :],
                )
            )
    values = re.match(r"INSERT INTO (\S+) \(tenant_id, (.*?)\) VALUES \(%\(tenant_id\)s, ", text)
    if values:
        dropped = text.replace("(tenant_id, ", "(", 1).replace("VALUES (%(tenant_id)s, ", "VALUES (", 1)
        found.append(("drop the tenant_id column", dropped))
    return found


_PROBE: dict[str, list[dict[str, Any]]] = {}


# MUP-ONA T-CAT-2
@pytest.mark.parametrize("name", PARTITIONED)
def test_every_mutation_of_a_partitioned_entry_leaks(name: str) -> None:
    probe = mutations(CATALOG[name])
    assert probe, f"{name}: no mutation to probe"
    outcome = []
    for what, text in probe:
        kinds = _findings(text)
        outcome.append({"mutation": what, "findings": sorted(set(kinds)), "text": text})
        assert "leak" in kinds and set(kinds) <= {"leak"}, (name, what, text, kinds)
    _PROBE[name] = outcome
    _write_probe()


def _write_probe() -> None:
    """The probe's outcome so far, as ``<NODESTRO_CATALOG_REPORTS>/mutation-probe.json`` (CI uploads it)."""
    target = os.environ.get("NODESTRO_CATALOG_REPORTS")
    if not target:
        return
    path = Path(target) / "mutation-probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "test": "T-CAT-2 (tests/test_sql_catalog.py)",
        "prefix": PREFIX,
        "entries": len(_PROBE),
        "mutations": sum(len(found) for found in _PROBE.values()),
        "all_leak": all("leak" in m["findings"] for found in _PROBE.values() for m in found),
        "probe": dict(sorted(_PROBE.items())),
    }
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def test_the_probe_mutates_every_tenant_predicate() -> None:
    """Each mutation set covers every ``tenant_id`` predicate of its entry (a predicate the regex misses would be
    left out of the probe silently)."""
    for name in PARTITIONED:
        text = _text(CATALOG[name].template)
        assert text.count("tenant_id = %(tenant_id)s") == len(_PREDICATE.findall(text)), name
        removals = [what for what, _ in mutations(CATALOG[name]) if what.startswith("remove")]
        assert len(removals) == len(_PREDICATE.findall(text)), name


# ---- T-CAT-3: completeness ------------------------------------------------------------------------------------------


# MUP-ONA T-CAT-3
def test_the_catalog_is_the_checked_in_list() -> None:
    assert {name: entry.partitioned for name, entry in CATALOG.items()} == EXPECTED
    assert set(PARTITIONED) == {name for name, entry in CATALOG.items() if entry.partitioned}


def test_a_new_entry_without_its_partition_case_is_caught() -> None:
    """What T-CAT-3 compares: an entry missing from :data:`EXPECTED` makes the comparison unequal."""
    extra = Statement("files_extra", "SELECT 1 FROM {files} WHERE tenant_id = %(tenant_id)s", (PARTITION,), "read")
    grown = {**CATALOG, extra.name: extra}
    assert {name: entry.partitioned for name, entry in grown.items()} != EXPECTED


@pytest.mark.parametrize(
    ("template", "params", "partitioned"),
    [
        ("SELECT 1 FROM {files} WHERE tenant_id = %(tenant_id)s AND id = %(id)s", (PARTITION,), True),
        ("SELECT 1 FROM {files} WHERE tenant_id = %(tenant_id)s", (PARTITION, "id"), True),
        ("SELECT 1 FROM {files} WHERE tenant_id = %s", (), False),
        ("SELECT 1 FROM {files} WHERE id = %(id)s", ("id",), True),
        ("SELECT 1 FROM {files} WHERE tenant_id = %(tenant_id)s", (PARTITION,), False),
        ("SELECT 1 FROM {other} WHERE tenant_id = %(tenant_id)s", (PARTITION,), True),
        ("SELECT 1 FROM {files} WHERE tenant_id = %(tenant_id)s", (PARTITION, PARTITION), True),
    ],
)
def test_an_inconsistent_entry_is_refused(template: str, params: tuple[str, ...], partitioned: bool) -> None:
    with pytest.raises(ValueError):
        Statement("x_y", template, params, "read", partitioned=partitioned)


def test_every_entry_binds_its_values() -> None:
    for name, entry in CATALOG.items():
        assert entry.kind in {"read", "write", "delete"}, name
        assert entry.rows == (entry.kind == "read" or "RETURNING" in entry.template), name
        words = entry.template.split()[0].upper()
        expected = {"read": {"SELECT"}, "write": {"INSERT", "UPDATE"}, "delete": {"DELETE"}}[entry.kind]
        assert words in expected, name


# ---- T-CAT-4: no free execute, no literal, no Nodestro ----------------------------------------------------------


def _modules() -> list[Path]:
    found = sorted(PACKAGE.glob("*.py"))
    assert {path.name for path in found} >= {"__init__.py", "_sql.py", "connection.py", "index.py", "files.py"}
    return found


# MUP-ONA T-CAT-4
def test_no_module_outside_the_catalog_runs_sql() -> None:
    """Outside ``_sql.py`` the only ``execute`` is the executor's, with a catalog name as a constant (so every
    statement that runs is one T-CAT-1/2 check), and every catalog entry is used."""
    offenders = []
    used: set[str] = set()
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in {"executemany", "copy", "cursor", "stream"}:
                offenders.append(f"{path.name}:{node.lineno} .{node.attr}")
            if path.name == "_sql.py" or not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Attribute) and function.attr in {"execute", "_rows"}:
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Name) and (path.name, first.id, function.attr) == (
                    "index.py",
                    "name",
                    "execute",
                ):
                    continue  # PostgresIndex._rows(name, ...), whose callers pass constants (checked here)
                if not (isinstance(first, ast.Constant) and first.value in CATALOG):
                    offenders.append(f"{path.name}:{node.lineno} .{function.attr} without a catalog name")
                elif function.attr == "execute" and ast.unparse(function.value) != "self._sql":
                    offenders.append(f"{path.name}:{node.lineno} .execute outside the executor")
                else:
                    used.add(first.value)
    assert offenders == []
    assert used == set(CATALOG)


def test_the_generic_part_binds_values_and_uses_no_private_database_api() -> None:
    offenders = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                if node.attr in {"Literal", "_query", "_write", "_encode", "_connection", "_pool"}:
                    offenders.append(f"{path.name}:{node.lineno} .{node.attr}")
                if node.attr in {"_lock", "_transaction"} and not (
                    isinstance(node.value, ast.Name) and node.value.id == "self"
                ):
                    offenders.append(f"{path.name}:{node.lineno} .{node.attr} of another object")
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("psycopg"):
                offenders.extend(f"{path.name}:{node.lineno} {a.name}" for a in node.names if a.name == "Literal")
    assert offenders == []


def test_the_generic_part_imports_nothing_of_nodestro() -> None:
    """import-linter (pyproject.toml) does not see an import of the package root; this does."""
    allowed = set(sys.stdlib_module_names) | {"__future__", "orkexa", "psycopg"}
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                tops = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative: only within the package
                    assert node.level == 1, f"{path.name}:{node.lineno}"
                    continue
                tops = {(node.module or "").split(".")[0]}
            else:
                continue
            assert tops <= allowed, f"{path.name}:{node.lineno} imports {tops - allowed}"


def test_psycopg_is_imported_only_to_render() -> None:
    """The generic part imports without psycopg (a host without the driver gets a clear error only on use)."""
    import subprocess

    code = (
        "import sys; sys.modules['psycopg'] = None\n"
        "import nodestro_runtime.pg_generic as g\n"
        "print(sorted(m for m in sys.modules if m.startswith('psycopg') and sys.modules[m] is not None))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120)
    assert result.stdout.strip() == "[]"
