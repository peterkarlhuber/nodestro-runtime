"""Write the output baseline ``tests/fixtures/pgvector_status_before.json`` of ``pgvector_status`` (TABRION MIG-NOD-7).

MIG-NOD-7 moves the catalog read and the state decision of
:func:`~nodestro_runtime.storage_postgres.pgvector_status` to ``tabrion.postgres.extension_status``; its acceptance
criterion is an unchanged output (doc 09 MIG-NOD-7, review L-6 of QA-13). This script was run once on the code
*before* that change (it feeds the old query's row to the function through a stand-in database) and the file it wrote
is the baseline: ``tests/test_pgvector_status.py`` compares every state's output with it, without a server (stand-in)
and on real servers (mode A without pgvector in pg-a, mode B with pgvector in pg-b), after replacing the server's
pgvector version with ``VERSION`` and the test's schema with ``SCHEMA``.

    python tests/fixtures/make_pgvector_status.py   # only on the code before MIG-NOD-7
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nodestro_runtime.storage_postgres import pgvector_status

VERSION = "<version>"
SCHEMA = "nodestro"
ELSEWHERE = "elsewhere"
OUT = Path(__file__).resolve().parent / "pgvector_status_before.json"

#: The row of the old status query per case: usable, current_schema, extversion, extension schema, default_version.
ROWS: dict[str, tuple[Any, ...]] = {
    "active": (True, SCHEMA, VERSION, SCHEMA, VERSION),
    "other_schema": (False, SCHEMA, VERSION, ELSEWHERE, VERSION),
    "installable": (False, SCHEMA, None, None, VERSION),
    "installable_quoted_schema": (False, 'odd"name', None, None, VERSION),
    "installable_no_current_schema": (False, None, None, None, VERSION),
    "unavailable": (False, SCHEMA, None, None, None),
}


class _Answers:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self.row = row

    def _query(self, statement: str, params: Any = None) -> list[tuple[Any, ...]]:
        assert statement.startswith("SELECT to_regtype('vector')")
        return [self.row]


def main() -> None:
    found = {case: pgvector_status(_Answers(row)) for case, row in ROWS.items()}  # type: ignore[arg-type]
    OUT.write_text(json.dumps(found, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
