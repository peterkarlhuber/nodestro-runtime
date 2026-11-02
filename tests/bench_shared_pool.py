"""Connections per process with Axentra on its own pool vs. on Nodestro's pool (M10, risk R-10). Not a test.

    NODESTRO_TEST_POSTGRES=postgresql://postgres:pg@127.0.0.1:5432/postgres \\
        python tests/bench_shared_pool.py [--threads 16] [--workers 2] [--seconds 10] [--pool 8]

Runs the same API-and-worker-like load twice in one process, once per mode:

- ``separate``: Axentra opens its own pool (before M10: ``pool_max`` 10 next to Nodestro's ``--pool``);
- ``shared``: Axentra borrows Nodestro's pool (``AxentraServices(database=...)``).

Load per API thread, in a loop: an Axentra service call (authorization read + tenant listing: two Axentra
transactions), a Nodestro read, a Nodestro write, and an audit mirror (Nodestro transaction with Axentra joined:
``AxentraLink.joined`` + ``join`` + ``AuditLog.record`` + an authorization read inside it). Per worker thread: a
Nodestro transaction with a document lock and two writes, then a Nodestro read. A sampler counts the process's
backends in ``pg_stat_activity`` (by ``application_name``) every 10 ms. Every connection checkout is also counted per
thread, to show whether a thread ever holds two pooled connections at once (the hold-and-wait pattern that could
starve one shared pool).

Prints one JSON object per mode: connections (max, mean), operations per second, p50/p99 latency of the API loop,
errors, and the most connections one thread held at once.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
import secrets
import statistics
import sys
import threading
import time
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import BaseModel

from nodestro_runtime.axentra_platform import AxentraConfig, AxentraLink, AxentraServices
from nodestro_runtime.identity_store import IdentityStore
from nodestro_runtime.storage_postgres import PostgresDatabase


class Doc(BaseModel):
    id: str
    n: int = 0


class Held(threading.local):
    def __init__(self) -> None:
        self.now = 0


def _counting(pool: Any, held: Held, peak: list[int]) -> None:
    """Wrap ``pool.connection`` to count connections each thread holds at once."""
    original = pool.connection

    @contextmanager
    def connection(*args: Any, **kwargs: Any) -> Iterator[Any]:
        with original(*args, **kwargs) as conn:
            held.now += 1
            peak[0] = max(peak[0], held.now)
            try:
                yield conn
            finally:
                held.now -= 1

    pool.connection = connection


def run(mode: str, dsn: str, schema: str, args: argparse.Namespace, key: str) -> dict[str, Any]:
    app = f"ndbench_{mode}_{secrets.token_hex(3)}"
    parts = conninfo_to_dict(dsn)
    parts["application_name"] = app
    url = make_conninfo("", **parts)
    database = PostgresDatabase(url, schema=schema, pool_size=args.pool)
    services = AxentraServices(
        AxentraConfig(enabled=True, keyring=f"k1:{key}", allow_superuser=True, audit=True),
        database_url=url,
        public_origin="http://localhost:8000",
        schema=schema,
        database=database if mode == "shared" else None,
    )
    services.start()
    link = AxentraLink(services, database)
    held, peak = Held(), [0]
    _counting(database._pool, held, peak)
    if mode == "separate":
        _counting(services.platform.db.pool, held, peak)
    from axentra.core.context import Principal
    from axentra.data.database import DbScope, join

    system = Principal.system("bench")
    product = services.product_id
    tenants = {tenant: link.tenant(tenant) for tenant in ("alpha", "beta")}
    stop = threading.Event()
    latencies: list[float] = []
    errors: list[str] = []
    ops = [0]
    samples: list[int] = []
    lock = threading.Lock()

    def api(index: int) -> None:
        tenant = ("alpha", "beta")[index % 2]
        store = database.store(tenant)
        n = 0
        while not stop.is_set():
            started = time.perf_counter()
            try:
                services.platform.tenancy.list_tenants(system, product)  # 2 Axentra transactions
                store.items("bench", Doc)
                store.put("bench", f"api-{index}-{n % 20}", Doc(id=str(n), n=n))
                with link.joined() as connection, connection.transaction():  # the audit mirror's shape
                    services.platform.authz.require(system, "tenants:read", product, tenants[tenant])
                    with join(connection, DbScope.tenant(product, tenants[tenant])) as tx:
                        services.platform.audit.record(
                            tx,
                            system,
                            "nodestro.bench",
                            target_type="run",
                            target_id=str(n),
                            product_id=product,
                            tenant_id=tenants[tenant],
                        )
                n += 1
                with lock:
                    ops[0] += 1
                    latencies.append(time.perf_counter() - started)
            except Exception as exc:
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}"[:200])

    def worker(index: int) -> None:
        store = database.store("alpha")
        n = 0
        while not stop.is_set():
            try:
                with store.transaction():
                    store.lock("jobs", f"w{index}")
                    store.put("jobs", f"w{index}", Doc(id=str(n), n=n))
                    store.put("events", f"w{index}-{n % 50}", Doc(id=str(n)))
                store.get("jobs", f"w{index}", Doc)
                n += 1
                with lock:
                    ops[0] += 1
            except Exception as exc:
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}"[:200])

    def sampler() -> None:
        with psycopg.connect(dsn, autocommit=True) as admin:
            while not stop.is_set():
                row = admin.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", (app,)
                ).fetchone()
                samples.append(int(row[0]) if row else 0)
                time.sleep(0.01)

    threads = [threading.Thread(target=api, args=(i,)) for i in range(args.threads)]
    threads += [threading.Thread(target=worker, args=(i,)) for i in range(args.workers)]
    watcher = threading.Thread(target=sampler)
    watcher.start()
    began = time.perf_counter()
    for thread in threads:
        thread.start()
    time.sleep(args.seconds)
    stop.set()
    for thread in [*threads, watcher]:
        thread.join(60)
    elapsed = time.perf_counter() - began
    stats = {"database": dict(database._pool.get_stats())}
    if mode == "separate":
        stats["axentra"] = dict(services.platform.db.pool.get_stats())
    services.close()
    database.close()
    ordered = sorted(latencies)
    return {
        "mode": mode,
        "pool_max": {
            "nodestro": args.pool,
            "axentra": "shared" if mode == "shared" else services.settings.database.pool_max,
        },
        "threads": {"api": args.threads, "worker": args.workers},
        "connections_max": max(samples) if samples else 0,
        "connections_mean": round(statistics.mean(samples), 2) if samples else 0,
        "ops_per_second": round(ops[0] / elapsed, 1),
        "api_loop_ms_p50": round(1000 * ordered[len(ordered) // 2], 2) if ordered else None,
        "api_loop_ms_p99": round(1000 * ordered[int(len(ordered) * 0.99)], 2) if ordered else None,
        "errors": len(errors),
        "first_errors": sorted(set(errors))[:3],
        "most_connections_one_thread_held": peak[0],
        "pool_timeouts": sum(int(s.get("requests_errors", 0)) for s in stats.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--pool", type=int, default=8)
    args = parser.parse_args()
    dsn = os.environ.get("NODESTRO_TEST_POSTGRES", "")
    if not dsn:
        print("NODESTRO_TEST_POSTGRES is not set", file=sys.stderr)
        return 2
    import base64

    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    schema = f"nodestro_bench_{secrets.token_hex(6)}"
    setup = PostgresDatabase(dsn, schema=schema)
    try:
        setup.migrate()
        services = AxentraServices(
            AxentraConfig(enabled=True, keyring=f"k1:{key}", allow_superuser=True),
            database_url=dsn,
            public_origin="http://localhost:8000",
            schema=schema,
            database=setup,
        )
        services.migrate()
        services.start()
        link = AxentraLink(services, setup)
        identity = IdentityStore(setup)
        for tenant in ("alpha", "beta"):
            identity.create_tenant(tenant, tenant.title())
            link.ensure_tenant(tenant, tenant.title())
        services.close()
        for mode in ("separate", "shared"):
            print(json.dumps(run(mode, dsn, schema, args, key)))
    finally:
        setup.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
