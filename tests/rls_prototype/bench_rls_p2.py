"""T-RLS-P2 (MUP-ONA MP-A9, prototype branch only): what the policies of ``policies.sql`` cost. Not a test.

Follows the M10 pattern of ``tests/bench_nodestro_rls.py`` (variants of the same statements, runtime role, median
and tail per variant), extended to the knowledge and memory paths that B6 moved onto Orkexa's PostgreSQL backend:
full-text, vector and hybrid search, chunk inserts, memory reads and writes, and the M10 document statements.

    A9_RLS_ADMIN="host=127.0.0.1 port=55522 user=postgres password=... dbname=postgres" \\
        python tests/rls_prototype/bench_rls_p2.py --database a9_rls_bench --out tests/rls_prototype/results/p2.json

The database (only ``a9_*`` names) is created anew from FX-PG-0v (or FX-PG-0) by :func:`rls_support.prepare` and
seeded with ``--tenants`` synthetic tenants of ``--chunks`` chunks each (a deterministic fake embedding model of
``--dims`` dimensions, a 2,000-word vocabulary), the same number of documents, and 100 memory files per tenant.
Every measurement runs as the restricted runtime role ``a9_runtime`` for tenant ``t03``.

Variants (per run, in this order; the policies are applied before the third and dropped after the fourth):

- ``today``: no policies; Nodestro's ``DatabaseSource`` as on master (no tenant setting: reads and single writes in
  autocommit);
- ``hook_only``: no policies; ``DatabaseSource(tenant_setting="app.tenant_id")``: every read and write becomes
  ``BEGIN`` + ``set_config`` + statement(s) + ``COMMIT`` (the transaction cost alone);
- ``rls_hook``: the policies and the hook (what option (b) would run);
- ``rls_policy_only``: the policies, inside one open transaction whose setting is already set (the policy alone).

A second part measures throughput with 8 threads (full-text search, ``--seconds`` per variant), because the hook of
``DatabaseSource`` opens its transactions under the database's process lock, while Orkexa's ``PsycopgSource`` does
not: ``today``, ``hook_only``, ``rls_hook`` and ``rls_psycopg_source`` (policies, Orkexa's own source with the hook
and 8 connections).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import statistics
import sys
import threading
import time
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

from orkexa.backends import postgres as orkexa_postgres  # noqa: E402
from orkexa.embeddings import CallableEmbeddings  # noqa: E402
from orkexa.retrieval import Chunk  # noqa: E402
import psycopg  # noqa: E402
import rls_support  # noqa: E402

from nodestro_runtime.postgres_tables import LAYOUT  # noqa: E402
from nodestro_runtime.storage_postgres import DatabaseSource, PostgresDatabase  # noqa: E402

TENANT = "t03"
NAMESPACE = "nodestro"
MEMORY = "nodestro:bench"
GET = "SELECT coalesce(raw, data::text) FROM nodestro_documents WHERE tenant_id = %s AND collection = %s AND id = %s"
PUT = (
    "INSERT INTO nodestro_documents (tenant_id, collection, id, data) VALUES (%s, %s, %s, %s::jsonb)"
    " ON CONFLICT (tenant_id, collection, id) DO UPDATE SET data = excluded.data"
)
VARIANTS = ("today", "hook_only", "rls_hook", "rls_policy_only")
WORDS = ["".join(random.Random(n).choice("abcdefghijklmnopqrstuvwxyz") for _ in range(4 + n % 6)) for n in range(2000)]


def model(dims: int) -> CallableEmbeddings:
    def embed(texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            seed = hashlib.sha256(text.encode()).digest()
            rng = random.Random(seed)
            out.append([round(rng.uniform(-1, 1), 4) for _ in range(dims)])
        return out

    return CallableEmbeddings(embed, name="a9-bench", dimensions=dims, provider="a9")


def text(rng: random.Random, words: int = 60) -> str:
    # Zipf-like: low word numbers are frequent, so some queries match many chunks and some few.
    return " ".join(WORDS[min(int(rng.paretovariate(1.1)) - 1, len(WORDS) - 1)] for _ in range(words))


def seed(setup: rls_support.Setup, tenants: int, chunks: int, dims: int) -> dict[str, int]:
    owner = PostgresDatabase(setup.owner, pool_size=2)
    embeddings = model(dims)
    try:
        source = DatabaseSource(owner)
        for t in range(tenants):
            tenant = f"t{t:02d}"
            rng = random.Random(t)
            index = orkexa_postgres.PostgresIndex(
                source, tenant, layout=LAYOUT, namespace=NAMESPACE, embeddings=embeddings
            )
            for start in range(0, chunks, 250):
                index.add_chunks(
                    [
                        Chunk(text=text(rng), source=f"doc-{n // 10:04d}", title=f"Doc {n}")
                        for n in range(start, min(chunks, start + 250))
                    ]
                )
            memory = orkexa_postgres.PostgresMemory(source, tenant, layout=LAYOUT, namespace=MEMORY)
            for n in range(100):
                memory.create(f"/memories/notes/{n:03d}.md", text(rng, 200))
            with owner.transaction():
                for n in range(chunks):
                    owner._write(PUT, (tenant, "docs", f"d{n:04d}", json.dumps({"id": n, "text": "x" * 200})))
        with owner._transaction() as connection:
            for table in rls_support.POLICY_TABLES:
                connection.execute(f"ANALYZE {table}")
        counts = {}
        for table in rls_support.POLICY_TABLES:
            counts[table] = int(owner._query(f"SELECT count(*) FROM {table}")[0][0])
        return counts
    finally:
        owner.close()


def stats(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "n": len(ordered),
        "median_us": round(statistics.median(ordered), 1),
        "p95_us": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
    }


def timed(fn: Callable[[int], Any], rounds: int) -> list[float]:
    for n in range(10):
        fn(n)
    samples = []
    for n in range(rounds):
        started = time.perf_counter_ns()
        fn(n)
        samples.append((time.perf_counter_ns() - started) / 1000)
    return samples


QUERIES = [f"{WORDS[a]} {WORDS[b]}" for a, b in ((n % 40, 40 + (n * 7) % 300) for n in range(50))]


def operations(
    source: Any, database: PostgresDatabase, dims: int, run: int, variant: str
) -> dict[str, Callable[[int], Any]]:
    embeddings = model(dims)
    index = orkexa_postgres.PostgresIndex(source, TENANT, layout=LAYOUT, namespace=NAMESPACE, embeddings=embeddings)
    memory = orkexa_postgres.PostgresMemory(source, TENANT, layout=LAYOUT, namespace=MEMORY)
    rng = random.Random(run)
    tag = f"{variant}-{run}"

    def doc_get(n: int) -> Any:
        with source.transaction("read", tenant_id=TENANT) as connection:
            return connection.execute(GET, (TENANT, "docs", f"d{n % 1000:04d}")).fetchall()

    def doc_put(n: int) -> Any:
        with source.transaction("write", tenant_id=TENANT) as connection:
            return connection.execute(PUT, (TENANT, "docs", f"d{n % 1000:04d}", '{"n": 1}'))

    return {
        "fts": lambda n: index.retrieve(QUERIES[n % len(QUERIES)], k=10, mode="fts"),
        "vector": lambda n: index.retrieve(QUERIES[n % len(QUERIES)], k=10, mode="vector"),
        "hybrid": lambda n: index.retrieve(QUERIES[n % len(QUERIES)], k=10, mode="hybrid"),
        "insert_chunk": lambda n: index.add_chunks([Chunk(text=text(rng), source=f"bench-{tag}-{n}")]),
        "memory_view": lambda n: memory.view(f"/memories/notes/{n % 100:03d}.md"),
        "memory_write": lambda n: memory.create(f"/memories/bench/{tag}-{n % 20}.md", text(rng, 50)),
        "doc_get": doc_get,
        "doc_put": doc_put,
    }


def measure(setup: rls_support.Setup, variant: str, dims: int, run: int, rounds: int) -> dict[str, list[float]]:
    database = PostgresDatabase(setup.runtime, pool_size=8)
    try:
        hook = rls_support.SETTING if variant in ("hook_only", "rls_hook") else None
        source = DatabaseSource(database, tenant_setting=hook)
        found: dict[str, list[float]] = {}
        for name, fn in operations(source, database, dims, run, variant).items():
            count = rounds if name not in ("vector", "hybrid", "insert_chunk") else max(rounds // 2, 20)
            if variant == "rls_policy_only":
                with database._transaction() as connection:
                    connection.execute("SELECT set_config(%s, %s, true)", (rls_support.SETTING, TENANT))
                    found[name] = timed(fn, count)
            else:
                found[name] = timed(fn, count)
        if variant in ("rls_hook", "rls_policy_only"):
            # The policies were really in force for this role (sanity check, not timed).
            assert database._query("SELECT count(*) FROM nodestro_chunks") == [(0,)]
        return found
    finally:
        database.close()


def reset(setup: rls_support.Setup) -> None:
    """Remove what a variant wrote (chunks with their vectors, memory files) and vacuum, so every variant searches
    the same rows (not timed; as the superuser, outside the policies)."""
    with psycopg.connect(setup.admin, autocommit=True) as admin:
        admin.execute("DELETE FROM nodestro_chunks WHERE tenant_id = %s AND source LIKE 'bench-%%'", (TENANT,))
        admin.execute("DELETE FROM nodestro_memory_files WHERE tenant_id = %s AND path LIKE 'bench/%%'", (TENANT,))
        for table in rls_support.POLICY_TABLES:
            admin.execute(f"VACUUM (ANALYZE) {table}")


def throughput(setup: rls_support.Setup, variant: str, seconds: float, threads: int = 8) -> dict[str, Any]:
    with ExitStack() as stack:
        if variant == "rls_psycopg_source":
            source: Any = stack.enter_context(
                orkexa_postgres.PsycopgSource(
                    setup.runtime, max_connections=threads, tenant_setting=rls_support.SETTING
                )
            )
        else:
            database = PostgresDatabase(setup.runtime, pool_size=threads)
            stack.callback(database.close)
            hook = None if variant == "today" else rls_support.SETTING
            source = DatabaseSource(database, tenant_setting=hook)
        index = orkexa_postgres.PostgresIndex(source, TENANT, layout=LAYOUT, namespace=NAMESPACE)
        index.retrieve(QUERIES[0], k=10, mode="fts")
        done = [0] * threads
        stop = time.monotonic() + seconds

        def worker(slot: int) -> None:
            n = slot
            while time.monotonic() < stop:
                index.retrieve(QUERIES[n % len(QUERIES)], k=10, mode="fts")
                done[slot] += 1
                n += threads

        pool = [threading.Thread(target=worker, args=(slot,)) for slot in range(threads)]
        started = time.monotonic()
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join()
        elapsed = time.monotonic() - started
    return {
        "threads": threads,
        "seconds": round(elapsed, 2),
        "ops": sum(done),
        "ops_per_s": round(sum(done) / elapsed, 1),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database", default="a9_rls_bench")
    parser.add_argument("--dump", default="FX-PG-0v.sql.gz")
    parser.add_argument("--tenants", type=int, default=20)
    parser.add_argument("--chunks", type=int, default=1000)
    parser.add_argument("--dims", type=int, default=64)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=200)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reuse", action="store_true", help="measure on the database a previous run seeded")
    args = parser.parse_args(argv)
    admin = os.environ.get("A9_RLS_ADMIN", "")
    if not admin:
        print("A9_RLS_ADMIN is not set", file=sys.stderr)
        return 2
    started = time.monotonic()
    if args.reuse:
        setup = rls_support.setup_for(admin, args.database)
        rls_support.drop_policies(setup)
        reset(setup)
        with psycopg.connect(setup.admin, autocommit=True) as connection:
            counts = {
                table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                for table in rls_support.POLICY_TABLES
            }
    else:
        setup = rls_support.prepare(args.dump, admin, args.database)
        counts = seed(setup, args.tenants, args.chunks, args.dims)
    with psycopg.connect(setup.admin, autocommit=True) as connection:
        server = connection.execute("SHOW server_version").fetchone()[0]
        pgvector = connection.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
    result: dict[str, Any] = {
        "benchmark": "T-RLS-P2",
        "when": datetime.now(UTC).isoformat(timespec="seconds"),
        "server": server,
        "pgvector": pgvector[0] if pgvector else None,
        "client": {"python": platform.python_version(), "machine": platform.machine(), "cpus": os.cpu_count()},
        "connection": "TCP to a local Docker container (127.0.0.1)",
        "args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
        "rows": counts,
        "seed_seconds": round(time.monotonic() - started, 1),
        "samples_us": {variant: {} for variant in VARIANTS},
        "per_run": {variant: {} for variant in VARIANTS},
        "throughput": {},
    }
    for run in range(args.runs):
        for variant in VARIANTS:
            if variant == "rls_hook":
                rls_support.apply_policies(setup)
            found = measure(setup, variant, args.dims, run, args.rounds)
            reset(setup)
            for name, samples in found.items():
                result["samples_us"][variant].setdefault(name, []).extend(round(s, 1) for s in samples)
                result["per_run"][variant].setdefault(name, []).append(stats(samples))
            print(
                f"run {run} {variant}: " + ", ".join(f"{k}={stats(v)['median_us']}" for k, v in found.items()),
                flush=True,
            )
        rls_support.drop_policies(setup)
    for variant in ("today", "hook_only"):
        result["throughput"][variant] = throughput(setup, variant, args.seconds)
    rls_support.apply_policies(setup)
    for variant in ("rls_hook", "rls_psycopg_source"):
        result["throughput"][variant] = throughput(setup, variant, args.seconds)
    rls_support.drop_policies(setup)
    summary: dict[str, Any] = {}
    for variant in VARIANTS:
        for name, samples in result["samples_us"][variant].items():
            summary.setdefault(name, {})[variant] = stats(samples)
    for row in summary.values():
        base = row["today"]["median_us"]
        for variant in VARIANTS:
            row[variant]["x_today_median"] = round(row[variant]["median_us"] / base, 2)
    result["summary"] = summary
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps({"summary": summary, "throughput": result["throughput"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
