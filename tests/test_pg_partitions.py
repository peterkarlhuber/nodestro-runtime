"""Tenant isolation of the PostgreSQL classes, MUP-ONA plan chapter 10.4 (MP-B3: T-PART-1..9).

T-PART-1..9 are orkexa's partition cases (``orkexa.testing.conformance``, :data:`T_PART`), which
``tests/test_orkexa_conformance.py`` runs against ``PostgresIndex`` (both ways of ranking vectors),
``PostgresFileStore`` and ``PostgresMemory`` with the tenant as the partition, never as expected failures. The CI
jobs ``pg-a`` and ``pg-b`` run them as superuser and with the restricted runtime role
(``NODESTRO_TEST_RUNTIME_ROLE=1``).

Orkexa's T-PART-CC case is short (nine threads, full text only). The plan asks for 3 x 8 threads over one pool,
mixed writes, searches, deletes and reindexes for 60 seconds: :func:`test_three_tenants_with_eight_threads_each`
(``NODESTRO_TEST_PART_SECONDS`` changes the duration). Its psycopg-backed imports happen after the PostgreSQL guard.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import os
import threading
import time
from typing import Any

from orkexa.retrieval import RetrievalError
from orkexa.testing.conformance import ALL_FILE_STORE_CASES, ALL_INDEX_CASES, ALL_MEMORY_CASES, KeywordEmbeddings
import postgres_support
import pytest

#: T-PART-n (plan 10.4, in its order R, W, U, D, RX, TX, CC, V, J) -> the orkexa cases that prove it.
T_PART: dict[int, tuple[str, ...]] = {
    1: ("partition.index_reads", "partition.files_reads", "partition.memory_reads"),
    2: ("partition.index_writes", "partition.files_writes_and_deletes"),
    3: ("partition.memory_updates_and_deletes",),
    4: ("partition.index_deletes", "partition.files_writes_and_deletes", "partition.memory_updates_and_deletes"),
    5: ("partition.index_reindex",),
    6: ("partition.index_failed_write_rolls_back",),
    7: ("partition.index_concurrent_use",),
    8: ("partition.index_hostile_values", "partition.files_hostile_values", "partition.memory_hostile_values"),
    9: ("partition.index_vector_joins",),
}
TENANTS = ("part-a", "part-b", "part-c")
MARKER = "zebramarker"
THREADS = 8


# MUP-ONA T-PART-1
# MUP-ONA T-PART-2
# MUP-ONA T-PART-3
# MUP-ONA T-PART-4
# MUP-ONA T-PART-5
# MUP-ONA T-PART-6
# MUP-ONA T-PART-7
# MUP-ONA T-PART-8
# MUP-ONA T-PART-9
def test_every_t_part_case_runs_against_the_postgres_classes() -> None:
    import test_orkexa_conformance as conformance

    names = {case.name for case in (*ALL_INDEX_CASES, *ALL_FILE_STORE_CASES, *ALL_MEMORY_CASES)}
    assert set(T_PART) == set(range(1, 10))
    for number, cases in T_PART.items():
        assert set(cases) <= names, f"T-PART-{number}: {set(cases) - names}"
    targets = {target: {case.name for case in cases} for target, (cases, _) in conformance.TARGETS.items()}
    proven = {name for cases in T_PART.values() for name in cases}
    for name in proven:
        if name.startswith("partition.index_"):
            assert {"pg-index", "pg-index-runtime"} <= {t for t, found in targets.items() if name in found}, name
        else:
            assert any(name in found for found in targets.values()), name
    for marks in conformance.DRIFTS.values():
        assert not proven & set(marks)  # never an expected failure


@pytest.fixture
def database() -> Iterator[Any]:
    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    try:
        opened.migrate()
        yield opened
    finally:
        opened.close()
        postgres_support.drop_schema(schema)


class _Models:
    """The embedding model the tenants' vectors currently belong to (a reindex switches it)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current = {tenant: KeywordEmbeddings("part-x") for tenant in TENANTS}

    def get(self, tenant: str) -> KeywordEmbeddings:
        with self.lock:
            return self.current[tenant]

    def switch(self, tenant: str, model: KeywordEmbeddings) -> None:
        with self.lock:
            self.current[tenant] = model


# MUP-ONA T-PART-7
@pytest.mark.parametrize("ranking", ["database", "runtime"])
def test_three_tenants_with_eight_threads_each(database: Any, ranking: str) -> None:
    from nodestro_runtime.postgres_tables import PostgresIndex

    if ranking == "database" and not PostgresIndex(database, TENANTS[0], namespace="probe").pgvector():
        postgres_support.require_pgvector()  # pg-b; elsewhere the case runs in the runtime as well
    seconds = float(os.environ.get("NODESTRO_TEST_PART_SECONDS", "60"))
    models = _Models()

    def make(tenant: str) -> PostgresIndex:
        index = PostgresIndex(database, tenant, namespace="cc", embeddings=models.get(tenant))
        if ranking == "runtime":
            index._pgvector = False
        return index

    # The same sources and texts in every tenant (the same chunk texts, sources and space keys); B and C hold a marker.
    for tenant in TENANTS:
        index = make(tenant)
        for number in range(4):
            index.add(f"Base note {number} about alpha policy.", source=f"base-{number}.md")
        if tenant != "part-a":
            index.add(f"The {MARKER} note about alpha policy.", source="marker.md")
    kept: dict[str, set[str]] = {tenant: set() for tenant in TENANTS}
    counts = {"writes": 0, "searches": 0, "deletes": 0, "reindexes": 0, "retries": 0}
    lock = threading.Lock()
    errors: list[str] = []
    deadline = time.monotonic() + seconds

    def current(tenant: str, action: Callable[[PostgresIndex], Any]) -> Any:
        """``action`` on an index with the tenant's current model; a reindex that switched the model meanwhile makes
        a write or search with the old one raise ``reindex_required`` (never a fallback): then again with the new."""
        for _attempt in range(20):
            try:
                return action(make(tenant))
            except RetrievalError as exc:
                if exc.code != "reindex_required":
                    raise
                with lock:
                    counts["retries"] += 1
        raise AssertionError(f"{tenant}: still reindex_required after 20 attempts")

    def add(tenant: str, text: str, source: str) -> None:
        current(tenant, lambda index: index.add(text, source=source))

    def reindex(tenant: str, generation: int) -> None:
        model = KeywordEmbeddings(f"part-{tenant}-{generation}")
        for _attempt in range(20):
            try:
                make(tenant).reindex(model, batch_size=8)
                models.switch(tenant, model)
                with lock:
                    counts["reindexes"] += 1
                return
            except RetrievalError as exc:  # chunks added meanwhile: continue the same reindex
                if exc.code != "vector_incomplete":
                    raise
        raise AssertionError(f"{tenant}: reindex {generation} did not complete")

    def work(tenant: str, worker: int) -> None:
        number = 0
        try:
            while time.monotonic() < deadline:
                source = f"w{worker}-{number}.md"
                add(tenant, f"Worker {worker} note {number} about beta policy.", source)
                with lock:
                    counts["writes"] += 1
                    kept[tenant].add(source)
                for mode in ("fts", "vector", "hybrid"):
                    hits = current(
                        tenant, lambda index, mode=mode: index.retrieve(f"alpha policy {MARKER}", 50, mode=mode)
                    )
                    marker = [hit.source for hit in hits if hit.source == "marker.md" or MARKER in hit.text]
                    if tenant == "part-a" and marker:
                        errors.append(f"{tenant} thread {worker} {mode}: marker visible {marker}")
                with lock:
                    counts["searches"] += 3
                if number % 2 == 1:
                    deleted = make(tenant).delete(source)
                    if deleted != 1:
                        errors.append(f"{tenant} thread {worker}: delete of {source} gave {deleted}")
                    with lock:
                        counts["deletes"] += 1
                        kept[tenant].discard(source)
                if worker == 0 and number % 5 == 4:
                    reindex(tenant, number)
                number += 1
        except BaseException as exc:  # reported below
            errors.append(f"{tenant} thread {worker}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=work, args=(tenant, n)) for tenant in TENANTS for n in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(seconds + 300)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert counts["writes"] >= len(threads) and counts["reindexes"] >= len(TENANTS), counts

    owner = postgres_support.owner_of(database)
    for tenant in TENANTS:
        index = make(tenant)
        own = {f"base-{number}.md" for number in range(4)} | ({"marker.md"} if tenant != "part-a" else set())
        expected = own | kept[tenant]
        assert set(index.sources()) == expected, tenant
        assert len(index) == len(expected), tenant
        status = index.reindex(models.get(tenant))  # completes vectors of chunks written during the last switch
        assert (status.chunks, status.embedded, status.excluded) == (len(expected), len(expected), 0), status
        assert status.active is not None and status.active["model"] == models.get(tenant).name
        found = {hit.source for hit in index.retrieve(MARKER, 50, mode="hybrid")}
        assert ("marker.md" in found) is (tenant != "part-a"), (tenant, found)
    stray = owner._query(
        "SELECT count(*) FROM nodestro_chunk_vectors v JOIN nodestro_chunks c ON c.id = v.chunk_id"
        " WHERE v.tenant_id <> c.tenant_id OR v.namespace <> c.namespace"
    )
    assert stray == [(0,)]
    spaces = owner._query("SELECT tenant_id, state, count(*) FROM nodestro_vector_spaces GROUP BY 1, 2 ORDER BY 1, 2")
    assert spaces == [(tenant, "active", 1) for tenant in TENANTS]
