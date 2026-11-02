"""What FORCE ROW LEVEL SECURITY on ``nodestro_documents`` would cost (M10 evaluation). Not a test.

    NODESTRO_TEST_POSTGRES=postgresql://postgres:pg@127.0.0.1:5432/postgres python tests/bench_nodestro_rls.py

A copy of Nodestro's schema with 20,000 documents of 20 tenants, read and written by a runtime role (no superuser, no
``BYPASSRLS``, owns nothing), in four variants:

- ``today``: the store's statements as they are (reads and single writes in autocommit, one round trip);
- ``rls_tx``: FORCE RLS keyed on a per-transaction tenant setting; every statement becomes ``BEGIN``,
  ``set_config('nodestro.tenant_id', ..., true)``, the statement, ``COMMIT`` (four round trips);
- ``rls_pipeline``: the same four statements sent in one psycopg pipeline (one network flight);
- ``rls_policy_only``: the policy evaluated, the setting already set (isolates the cost of the policy itself).

Prints the median and p99 of a point read (``get``), a listing (``items`` of 1,000 rows) and an upsert (``put``) per
variant, in microseconds.
"""

from __future__ import annotations

import json
import os
import secrets
import statistics
import sys
import time
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from nodestro_runtime.storage_postgres import PostgresDatabase, grant_runtime_role

GET = "SELECT coalesce(raw, data::text) FROM nodestro_documents WHERE tenant_id = %s AND collection = %s AND id = %s"
ITEMS = (
    "SELECT id, coalesce(raw, data::text) FROM nodestro_documents WHERE tenant_id = %s AND collection = %s ORDER BY id"
)
PUT = (
    "INSERT INTO nodestro_documents (tenant_id, collection, id, data) VALUES (%s, %s, %s, %s::jsonb)"
    " ON CONFLICT (tenant_id, collection, id) DO UPDATE SET data = excluded.data"
)
SET = "SELECT set_config('nodestro.tenant_id', %s, true)"
ROUNDS = 400


def _timed(fn: Any, rounds: int = ROUNDS) -> dict[str, float]:
    for _ in range(20):
        fn(0)
    times = []
    for index in range(rounds):
        started = time.perf_counter_ns()
        fn(index)
        times.append((time.perf_counter_ns() - started) / 1000)
    times.sort()
    return {"p50_us": round(statistics.median(times), 1), "p99_us": round(times[int(len(times) * 0.99)], 1)}


def main() -> int:
    dsn = os.environ.get("NODESTRO_TEST_POSTGRES", "")
    if not dsn:
        print("NODESTRO_TEST_POSTGRES is not set", file=sys.stderr)
        return 2
    schema, role, password = (
        f"nodestro_rlsbench_{secrets.token_hex(4)}",
        f"nd_rlsbench_{secrets.token_hex(4)}",
        "x" + secrets.token_hex(12),
    )
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
    owner = PostgresDatabase(dsn, schema=schema)
    results: dict[str, Any] = {}
    try:
        owner.migrate()
        with owner._transaction() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO nodestro_documents (tenant_id, collection, id, data) VALUES (%s, 'docs', %s, %s::jsonb)",
                [
                    (f"t{t:02d}", f"d{i:04d}", json.dumps({"id": i, "text": "x" * 200}))
                    for t in range(20)
                    for i in range(1000)
                ],
            )
            conn.execute("ANALYZE nodestro_documents")
        grant_runtime_role(owner, role)
        parts = conninfo_to_dict(dsn)
        parts.update(user=role, password=password, options=f"-c search_path={schema}")
        runtime = psycopg.connect(make_conninfo("", **parts), autocommit=True)

        def variant(name: str) -> None:
            if name == "rls_policy_only":
                runtime.execute("BEGIN")
                runtime.execute(SET, ("t03",))

                def run(statement: str, params: tuple[Any, ...]) -> list[Any]:
                    return (
                        runtime.execute(statement, params).fetchall()
                        if statement != PUT
                        else [runtime.execute(statement, params)]
                    )
            elif name == "today":

                def run(statement: str, params: tuple[Any, ...]) -> list[Any]:
                    cur = runtime.execute(statement, params)
                    return cur.fetchall() if cur.description else []
            elif name == "rls_tx":

                def run(statement: str, params: tuple[Any, ...]) -> list[Any]:
                    runtime.execute("BEGIN")
                    runtime.execute(SET, (params[0],))
                    cur = runtime.execute(statement, params)
                    rows = cur.fetchall() if cur.description else []
                    runtime.execute("COMMIT")
                    return rows
            else:

                def run(statement: str, params: tuple[Any, ...]) -> list[Any]:
                    with runtime.pipeline():
                        runtime.execute("BEGIN")
                        runtime.execute(SET, (params[0],))
                        cur = runtime.execute(statement, params)
                        runtime.execute("COMMIT")
                    return cur.fetchall() if cur.description else []

            results[name] = {
                "get": _timed(lambda i: run(GET, ("t03", "docs", f"d{i % 1000:04d}"))),
                "items_1000": _timed(lambda i: run(ITEMS, ("t03", "docs")), rounds=100),
                "put": _timed(lambda i: run(PUT, ("t03", "docs", f"d{i % 1000:04d}", '{"n": 1}'))),
            }
            if name == "rls_policy_only":
                runtime.execute("COMMIT")

        variant("today")
        with owner._transaction() as conn:
            conn.execute("ALTER TABLE nodestro_documents ENABLE ROW LEVEL SECURITY")
            conn.execute("ALTER TABLE nodestro_documents FORCE ROW LEVEL SECURITY")
            conn.execute(
                "CREATE POLICY tenant ON nodestro_documents"
                " USING (tenant_id = current_setting('nodestro.tenant_id', true))"
                " WITH CHECK (tenant_id = current_setting('nodestro.tenant_id', true))"
            )
        # Without the setting, the runtime role sees nothing (the policy works):
        assert runtime.execute("SELECT count(*) FROM nodestro_documents").fetchone() == (0,)
        for name in ("rls_tx", "rls_pipeline", "rls_policy_only"):
            variant(name)
        runtime.close()
    finally:
        owner.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
