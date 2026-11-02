"""Every PostgreSQL statement on a tenant-data table names the tenant (defense in depth instead of RLS, see
``docs/AXENTRA.md``, "Row-level security on Nodestro's tables").

Nodestro isolates tenants in code: a store is bound to one tenant and every statement on the tables that hold tenant
data filters on (or writes) ``tenant_id``. This test reads the SQL text of every PostgreSQL statement in the runtime's
source (``%s`` placeholders; the desktop's SQLite statements use ``?`` and have one tenant) and fails when a statement
on such a table does not mention ``tenant_id`` -- the "forgotten WHERE clause" that row-level security would catch,
caught before it ships. Statements that are cross-tenant by design are listed here, each with its reason.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

import nodestro_runtime

#: Tables whose every row belongs to one tenant (``tenant_id`` in the primary key or required).
TENANT_TABLES = re.compile(r"\bnodestro_(documents|files|chunks|memory_files|run_events)\b")
_DML = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b")

#: Cross-tenant by design (statement prefix -> why).
CROSS_TENANT = {
    "DELETE FROM nodestro_run_events WHERE created_at < clock_timestamp() - %s": (
        "housekeeping of the leader: run events older than the retention, of every tenant"
    ),
    "SELECT collection, id, coalesce(raw, data::text) FROM nodestro_documents WHERE collection = ANY(%s)": (
        "start check (axentra_platform.stored_secret_formats): counts stored secrets by format over every tenant,"
        " decrypts nothing and returns counts only"
    ),
}


def _statements(path: Path) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
        else:
            continue
        if TENANT_TABLES.search(text) and _DML.search(text) and "%s" in text and "?" not in text:
            found.append((node.lineno, " ".join(text.split())))
    return found


def test_every_statement_on_tenant_data_names_the_tenant() -> None:
    root = Path(nodestro_runtime.__file__).parent
    checked, missing = 0, []
    for path in sorted(root.rglob("*.py")):
        for line, text in _statements(path):
            checked += 1
            if "tenant_id" in text or any(text.startswith(prefix) for prefix in CROSS_TENANT):
                continue
            missing.append(f"{path.relative_to(root)}:{line}: {text[:120]}")
    assert checked > 20  # the scan finds the store's statements
    assert missing == [], "statements on tenant data without tenant_id:\n" + "\n".join(missing)


def test_the_scan_catches_a_forgotten_tenant_filter(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        'Q = "SELECT data FROM nodestro_documents WHERE collection = %s"\n'
        'OK = "SELECT data FROM nodestro_documents WHERE tenant_id = %s AND collection = %s"\n'
        'LITE = "SELECT data FROM nodestro_documents WHERE collection = ?"\n',
        encoding="utf-8",
    )
    found = _statements(sample)
    assert [line for line, text in found if "tenant_id" not in text] == [1]
    assert len(found) == 2
