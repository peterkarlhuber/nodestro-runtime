"""Orkexa's statement catalog, checked with Nodestro's tables and rules (MUP-ONA B4: T-CAT-1..4; B6: T-CAT-5, T-PKG-3).

The tenant SQL check reads Nodestro's SQL from the source (``tests/test_tenant_filters.py``). Since MUP-ONA B6 the SQL
of the knowledge index, file store and memory is the statement catalog of Orkexa's PostgreSQL backends
(``orkexa.backends.postgres``, at the pinned orkexa commit), whose templates name the tables only as ``{chunks}`` ...
So the catalog is checked here, rendered with Nodestro's prefix ``nodestro_`` and judged with **Nodestro's** tenant
tables from **Nodestro's** migrations (T-CAT-5); Orkexa's own catalog tests (prefix ``t_``) do not replace this:

* **T-CAT-1/T-CAT-5** every entry rendered with ``nodestro_`` is bound to one tenant by the rule engine
  (:func:`orkexa.testing.sqlcheck.check_statement` with Nodestro's tenant tables); no ``leak``, ``unparsed`` or
  ``unsupported``.
* **T-CAT-2/T-CAT-5** mutation probe: for every partitioned entry, each ``tenant_id`` predicate removed, each ``AND``
  next to it turned into ``OR``, and (``INSERT ... VALUES``) the ``tenant_id`` column dropped, is a ``leak``. With
  ``NODESTRO_CATALOG_REPORTS`` set, the probe's outcome is written to ``<dir>/mutation-probe.json`` (a CI artifact).
* **T-CAT-3** completeness: the catalog's names are exactly :data:`EXPECTED` (with their partition flag), the list
  Nodestro reviewed; an orkexa pin whose catalog has a new entry fails until it is listed here, and every partitioned
  entry gets the mutation probe and the guard cases of ``tests/test_partition_guard.py`` by parametrization.
* **T-CAT-4** (B6 form) Nodestro holds no generic PostgreSQL code any more: no ``pg_generic`` package, no
  ``sqlcheck_rules`` module, no class of Nodestro implements an Orkexa storage interface on PostgreSQL except as a
  subclass of Orkexa's backend, and no module of Nodestro imports a private module of ``orkexa.backends.postgres``.
* **T-PKG-3** the desktop profile (no psycopg, no pglast) imports the runtime and its storage modules, and none of
  them imports Orkexa's PostgreSQL backends or psycopg.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from orkexa.backends.postgres import CATALOG, Statement, render
from orkexa.backends.postgres._sql import PARTITION, TABLES
from orkexa.testing.sqlcheck import check_statement
import pytest

import nodestro_runtime
from nodestro_runtime.tenant_sql_check import tenant_tables

PACKAGE = Path(nodestro_runtime.__file__).parent
TENANT_TABLES = frozenset(tenant_tables(PACKAGE / "migrations" / "postgres"))
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
# MUP-ONA T-CAT-5
@pytest.mark.parametrize("name", sorted(CATALOG))
def test_every_rendered_entry_is_bound_to_one_tenant(name: str) -> None:
    # Rendering uses psycopg.sql (the postgres extra); the desktop profile runs this suite without it.
    pytest.importorskip("psycopg")
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
# MUP-ONA T-CAT-5
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
        "test": "T-CAT-2/T-CAT-5 (tests/test_sql_catalog.py, orkexa's catalog with Nodestro's rules)",
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


# ---- T-CAT-4 (B6): no generic PostgreSQL code in Nodestro ------------------------------------------------------

#: Orkexa's storage interfaces: a Nodestro class may implement one on PostgreSQL only through Orkexa's backend.
_INTERFACES = {"Index", "ReindexableIndex", "FileStore", "MemoryBackend"}


def _modules() -> list[Path]:
    found = sorted(PACKAGE.rglob("*.py"))
    assert PACKAGE / "postgres_tables.py" in found
    return found


# MUP-ONA T-CAT-4
def test_the_generic_part_and_the_rule_engine_are_gone_from_nodestro() -> None:
    """B6 deleted ``nodestro_runtime.pg_generic`` and ``nodestro_runtime.sqlcheck_rules`` (taken over into orkexa in
    B5); a copy that comes back would be checked by nobody."""
    assert importlib.util.find_spec("nodestro_runtime.pg_generic") is None
    assert importlib.util.find_spec("nodestro_runtime.sqlcheck_rules") is None
    assert not (PACKAGE / "pg_generic").exists()


def test_no_class_of_nodestro_implements_a_storage_interface_on_postgres_itself() -> None:
    """The grep evidence of the plan (no ``class PostgresIndex(Index)`` outside orkexa), as an AST check: every class
    of Nodestro named ``Postgres*`` that derives from an Orkexa storage interface does so through
    ``orkexa.backends.postgres``."""
    offenders = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [ast.unparse(base) for base in node.bases]
            direct = any(
                base.split(".")[-1] in _INTERFACES and not base.startswith("orkexa_postgres.") for base in bases
            )
            postgres = "Postgres" in node.name or path.name in {"postgres_tables.py", "storage_postgres.py"}
            if direct and postgres:
                offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno} class {node.name}({', '.join(bases)})")
    assert offenders == []
    from nodestro_runtime import postgres_tables

    for name in ("PostgresIndex", "PostgresFileStore", "PostgresMemory"):
        subclass = getattr(postgres_tables, name)
        assert subclass.__mro__[1].__module__.startswith("orkexa.backends.postgres."), (name, subclass.__mro__)


def test_nodestro_uses_only_the_public_package_of_the_postgres_backend() -> None:
    """``orkexa.backends.postgres._sql`` and ``._vector`` are private to orkexa (they may change in any release); the
    runtime imports the package (or its public modules) only. Tests may look inside."""
    offenders = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
                if node.module == "orkexa.backends.postgres":
                    names += [f"{node.module}.{alias.name}" for alias in node.names]
            for name in names:
                if name.startswith("orkexa.backends.postgres.") and name.split(".")[3].startswith("_"):
                    offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno} {name}")
    assert offenders == []


# ---- T-PKG-3: the desktop profile -------------------------------------------------------------------------------

#: What the desktop imports for its storage: the package and the modules of the SQLite knowledge base, memory and
#: workspace.
DESKTOP_MODULES = (
    "nodestro_runtime",
    "nodestro_runtime.engine",
    "nodestro_runtime.knowledge",
    "nodestro_runtime.memory_spaces",
    "nodestro_runtime.sqlite_tables",
    "nodestro_runtime.storage",
    "nodestro_runtime.workspace",
)


# MUP-ONA T-PKG-3
def test_the_desktop_profile_imports_without_psycopg_and_pglast() -> None:
    """Without the postgres extra (psycopg, psycopg_pool) and the dev parser (pglast) the runtime imports, and none of
    the desktop's modules pulls in Orkexa's PostgreSQL backends, Nodestro's PostgreSQL modules or a driver."""
    code = (
        "import sys\n"
        "for name in ('psycopg', 'psycopg_pool', 'pglast'):\n"
        "    sys.modules[name] = None\n"
        f"for name in {DESKTOP_MODULES!r}:\n"
        "    __import__(name)\n"
        "prefixes = ('orkexa.backends.postgres', 'psycopg', 'pglast', 'nodestro_runtime.postgres',\n"
        "            'nodestro_runtime.storage_postgres')\n"
        "loaded = sorted(m for m in sys.modules if sys.modules[m] is not None and m.startswith(prefixes))\n"
        "print(loaded)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120)
    assert result.stdout.strip() == "[]", result.stdout


def test_the_server_modules_import_without_psycopg_and_fail_only_on_use() -> None:
    """Orkexa's backends import psycopg late (orkexa T-PKG-2): even Nodestro's PostgreSQL wiring imports without the
    driver, so an installation without the extra fails with a clear error on use, not on import."""
    code = (
        "import sys\n"
        "sys.modules['psycopg'] = None\n"
        "import nodestro_runtime.postgres_tables as t\n"
        "drivers = sorted(m for m in sys.modules if m.startswith('psycopg') and sys.modules[m] is not None)\n"
        "print(t.LAYOUT.prefix, drivers)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120)
    assert result.stdout.strip() == "nodestro_ []"
