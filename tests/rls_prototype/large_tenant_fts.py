"""Full-text search of one large tenant with and without the prototype policies (MUP-ONA A9, T-RLS-P2 annex).

``@@`` (``ts_match_vq``) is not leakproof, so under row-level security PostgreSQL does not evaluate it before the
policy's qual and cannot use the GIN index ``nodestro_chunks_search`` for it; the search reads every chunk of the
tenant and filters. This script measures that on a tenant of ``--chunks`` chunks (written with SQL by the
superuser, full-text only) in the benchmark database, for a rare and a frequent word, as the runtime role:

    A9_RLS_ADMIN="host=... dbname=postgres" python tests/rls_prototype/large_tenant_fts.py --database a9_rls_bench \\
        --out tests/rls_prototype/results/large-tenant-fts-pgb.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

from orkexa.backends.postgres import _sql  # noqa: E402
from orkexa.backends.postgres.index import _tsquery  # noqa: E402
import psycopg  # noqa: E402
import rls_support  # noqa: E402

TENANT = "big"
NAMESPACE = "nodestro"
#: ``rare`` appears in 1 of 1,000 chunks, ``common`` in every chunk.
WORDS = {"rare": "quetzal", "common": "lorem"}


def fill(setup: rls_support.Setup, chunks: int) -> None:
    with psycopg.connect(setup.admin, autocommit=True) as admin:
        admin.execute("DELETE FROM nodestro_chunks WHERE tenant_id = %s", (TENANT,))
        admin.execute(
            "INSERT INTO nodestro_chunks (tenant_id, namespace, source, metadata, text, search_text)"
            " SELECT %s, %s, 'big-' || (n / 10), '{}', t, t FROM ("
            "  SELECT n, 'lorem ipsum ' || md5(n::text) || ' ' || md5((n * 7)::text)"
            "   || CASE WHEN n %% 1000 = 0 THEN ' quetzal' ELSE '' END AS t"
            "  FROM generate_series(1, %s) AS n) AS rows",
            (TENANT, NAMESPACE, chunks),
        )
        admin.execute("VACUUM (ANALYZE) nodestro_chunks")


def measure(setup: rls_support.Setup, rounds: int) -> dict[str, dict[str, object]]:
    query = _sql._rendered("search_fts", "nodestro_")
    found: dict[str, dict[str, object]] = {}
    with psycopg.connect(setup.runtime) as connection:
        connection.execute("SELECT set_config(%s, %s, true)", (rls_support.SETTING, TENANT))
        for label, word in WORDS.items():
            params = {"tenant_id": TENANT, "namespace": NAMESPACE, "query": _tsquery(word), "limit": 10}
            times = []
            for _ in range(rounds):
                started = time.perf_counter_ns()
                connection.execute(query, params).fetchall()
                times.append((time.perf_counter_ns() - started) / 1000)
            plan = connection.execute(psycopg.sql.SQL("EXPLAIN (ANALYZE) ") + query, params).fetchall()
            found[label] = {
                "median_us": round(statistics.median(times), 1),
                "p95_us": round(sorted(times)[int(rounds * 0.95)], 1),
                "plan": [row[0] for row in plan],
            }
        connection.rollback()
    return found


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="a9_rls_bench")
    parser.add_argument("--chunks", type=int, default=200_000)
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    setup = rls_support.setup_for(os.environ["A9_RLS_ADMIN"], args.database)
    fill(setup, args.chunks)
    result: dict[str, object] = {"tenant_chunks": args.chunks, "without_policies": measure(setup, args.rounds)}
    rls_support.apply_policies(setup)
    try:
        result["with_policies"] = measure(setup, args.rounds)
    finally:
        rls_support.drop_policies(setup)
        with psycopg.connect(setup.admin, autocommit=True) as admin:
            admin.execute("DELETE FROM nodestro_chunks WHERE tenant_id = %s", (TENANT,))
    args.out.write_text(json.dumps(result, indent=1) + "\n")
    for variant in ("without_policies", "with_policies"):
        for label, row in result[variant].items():  # type: ignore[union-attr]
            print(variant, label, row["median_us"], row["p95_us"])
            print("   ", " | ".join(line.strip() for line in row["plan"] if "Scan" in line or "Filter" in line))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
