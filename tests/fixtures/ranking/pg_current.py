"""The current PostgreSQL ranking of FX-RANK-0, recorded beside the fixture (MUP-ONA MP-B3).

``FX-RANK-0.json`` (a copy of orkexa's) keeps the results of the code **before** MP-B1/B3; those stay fixed, for the
ranking report of gate G1. ``PG-CURRENT.json`` keeps the results of the code as it is now, measured with
:mod:`measure_pg`'s functions, so ``tests/test_rank_fixture.py`` notices every change of the ranking and every commit
that changes it on purpose shows the change in this file's diff:

* ``pg-a``: the runtime ranks the vectors (``PostgresIndex._pgvector = False``; measurable on any server);
* ``pg-b``: pgvector ranks them (needs a server with pgvector).

Usage (from the repository root, a role that may create schemas; the second server must have pgvector)::

    python tests/fixtures/ranking/pg_current.py --dsn-a "host=... port=55521 ..." --dsn-b "host=... port=55522 ..."
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Any
import uuid

HERE = Path(__file__).resolve().parent
CURRENT = HERE / "PG-CURRENT.json"
BACKENDS = ("pg-a", "pg-b")


def measure_pg() -> ModuleType:
    """``measure_pg.py`` (orkexa's copy) as a module."""
    spec = importlib.util.spec_from_file_location("measure_pg", HERE / "measure_pg.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def measure_backend(dsn: str, backend: str, data: dict[str, Any]) -> dict[str, Any]:
    """``{"runs": {identity: ...}, "notes": {identity: ...}}`` of ``backend`` on ``dsn``, a fresh schema per
    identity (chunk ids 1..200; ``DROP SCHEMA ... CASCADE`` also drops a pgvector extension migration 0010 installed
    into it). ``pg-b`` fails unless pgvector ranks."""
    import psycopg
    from psycopg import sql

    from nodestro_runtime.postgres_tables import PostgresIndex
    from nodestro_runtime.storage_postgres import PostgresDatabase

    measure = measure_pg()
    runs: dict[str, Any] = {}
    notes: dict[str, Any] = {}
    for identity in data["identities"]:
        schema = f"fx_rank_{identity}_{uuid.uuid4().hex[:8]}"
        database = PostgresDatabase(dsn, schema=schema)
        try:
            database.migrate()
            index = PostgresIndex(
                database, measure.TENANT, namespace=measure.NAMESPACE, embeddings=measure.embeddings(data, identity)
            )
            if backend == "pg-a":
                index._pgvector = False  # the runtime ranks, as on a server without pgvector
            elif not index.pgvector():
                raise RuntimeError(f"{backend} needs pgvector, the server at hand has none")
            ids = index.add_chunks(measure.chunks(data))
            assert ids == list(range(1, len(data["chunks"]) + 1)), "chunk ids must follow the corpus order"
            notes[identity] = {
                "index_mode": index.mode,
                "pgvector": index.pgvector(),
                "vector_status": index.vector_status().model_dump(mode="json"),
            }
            runs[identity] = measure.measure_queries(index.retrieve, data)
        finally:
            database.close()
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    return {"runs": runs, "notes": notes}


def dumps(value: Any, indent: str = "") -> str:
    """JSON with one line per hit (a list of scalars stays on one line), so a ranking change is a readable diff."""
    inner = indent + " "
    if isinstance(value, dict) and value:
        items = [f"{inner}{json.dumps(key)}: {dumps(value[key], inner)}" for key in sorted(value)]
        return "{\n" + ",\n".join(items) + "\n" + indent + "}"
    if isinstance(value, list) and any(isinstance(item, dict | list) for item in value):
        return "[\n" + ",\n".join(inner + dumps(item, inner) for item in value) + "\n" + indent + "]"
    return json.dumps(value)


def load_current(path: Path = CURRENT) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--dsn-a", required=True, help="any server (the runtime ranks)")
    parser.add_argument("--dsn-b", required=True, help="a server with pgvector")
    parser.add_argument("--out", type=Path, default=CURRENT)
    args = parser.parse_args(argv)
    measure = measure_pg()
    data = measure.load_fixture()
    results = {
        "pg-a": measure_backend(args.dsn_a, "pg-a", data),
        "pg-b": measure_backend(args.dsn_b, "pg-b", data),
    }
    document = {
        "description": "FX-RANK-0 measured on the current PostgresIndex (tests/fixtures/ranking/pg_current.py).",
        "fixture_sha256": hashlib.sha256(measure.FIXTURE.read_bytes()).hexdigest(),
        "results": results,
    }
    args.out.write_text(dumps(document) + "\n", encoding="utf-8")
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
