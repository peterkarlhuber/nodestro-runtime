"""Query plans of the knowledge searches with and without the prototype policies (MUP-ONA A9, T-RLS-P2 annex).

Run after ``bench_rls_p2.py`` on its database (the policies are applied and dropped again here):

    A9_RLS_ADMIN="host=... dbname=postgres" python tests/rls_prototype/explain_plans.py --database a9_rls_bench \\
        --out tests/rls_prototype/results/plans-pgb.json

Each statement is the catalog entry as Nodestro renders it (``nodestro_`` prefix), run by the runtime role for tenant
``t03`` with ``EXPLAIN (ANALYZE, BUFFERS)``. Under row-level security PostgreSQL may evaluate a qual that is not
leakproof (``@@``, the vector distance) only after the policy's qual; that can change the plan in either direction.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

from bench_rls_p2 import NAMESPACE, QUERIES, TENANT, model  # noqa: E402
from orkexa.backends.postgres import _sql  # noqa: E402
from orkexa.backends.postgres._vector import pgvector_literal  # noqa: E402
from orkexa.backends.postgres.index import _tsquery  # noqa: E402
import psycopg  # noqa: E402
import rls_support  # noqa: E402


def plans(setup: rls_support.Setup, dims: int) -> dict[str, str]:
    space = f"{NAMESPACE}:{model(dims).identity().key}"
    vector = pgvector_literal([float(x) for x in model(dims).embed_query(QUERIES[0])])
    cases = {
        "search_fts (benchmark query 0)": ("search_fts", {"query": _tsquery(QUERIES[0]), "limit": 40}),
        "search_fts (its rarer word only)": ("search_fts", {"query": _tsquery(QUERIES[0].split()[1]), "limit": 40}),
        "search_pgvector": ("search_pgvector", {"vector": vector, "space": space, "limit": 40}),
        "search_candidates": ("search_candidates", {"space": space}),
    }
    found = {}
    with psycopg.connect(setup.runtime) as connection:
        connection.execute("SELECT set_config(%s, %s, true)", (rls_support.SETTING, TENANT))
        for label, (name, extra) in cases.items():
            query = _sql._rendered(name, "nodestro_")
            params = {"tenant_id": TENANT, "namespace": NAMESPACE, **extra}
            explain = psycopg.sql.SQL("EXPLAIN (ANALYZE, BUFFERS) ") + query
            try:
                rows = connection.execute(explain, params).fetchall()
                found[label] = "\n".join(row[0] for row in rows)
            except psycopg.Error as exc:
                connection.rollback()
                connection.execute("SELECT set_config(%s, %s, true)", (rls_support.SETTING, TENANT))
                found[label] = f"error: {exc}"
        connection.rollback()
    return found


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="a9_rls_bench")
    parser.add_argument("--dims", type=int, default=64)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    setup = rls_support.setup_for(os.environ["A9_RLS_ADMIN"], args.database)
    result = {"without_policies": plans(setup, args.dims)}
    rls_support.apply_policies(setup)
    try:
        result["with_policies"] = plans(setup, args.dims)
    finally:
        rls_support.drop_policies(setup)
    args.out.write_text(json.dumps(result, indent=1) + "\n")
    for variant, found in result.items():
        for label, text in found.items():
            print(f"== {variant}: {label}\n{text}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
