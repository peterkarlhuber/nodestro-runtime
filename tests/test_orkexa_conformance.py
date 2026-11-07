"""Orkexa's public conformance suite against Nodestro's PostgreSQL classes (MUP-ONA MP-B1 step 10, T-PART-0).

``PostgresIndex``, ``PostgresFileStore`` and ``PostgresMemory`` run every case of
``orkexa.testing.conformance`` (index, vector, reindex, file store, memory and the partition cases of plan chapter
10.4), with the tenant as the partition and a fresh namespace per case. Each case is one test, so a failure names
the case and its detail.

The index runs twice: ``pg-index`` ranks vectors the way the database can (pgvector where it is installed), and
``pg-index-runtime`` always ranks them in the runtime (the path of a server without pgvector), so both paths meet the
same contract on a server with pgvector.

Observing run (MP-B1): the cases of the ten documented PostgreSQL drifts (plan annex A, MP-B3) are strict expected
failures with the reason ``MUP-DRIFT-<n>``; a drift that disappears turns its test red until its mark is removed
(MP-B3 removes one drift per commit). Partition cases are never expected to fail. ``NODESTRO_CONFORMANCE_REPORTS``
names a directory for the JSON report of each target (a CI artifact).
"""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
from typing import Any

from orkexa.testing.conformance import (
    ALL_FILE_STORE_CASES,
    ALL_INDEX_CASES,
    ALL_MEMORY_CASES,
    Case,
    Outcome,
    Report,
    Request,
    load_expected_skips,
    run_cases,
)
import postgres_support
import pytest

EXPECTED_SKIPS = Path(__file__).resolve().parent / "ci" / "conformance-expected-skips.json"
#: The CI job whose expected skips apply (``tests/ci/conformance-expected-skips.json``).
JOB = os.environ.get("NODESTRO_CONFORMANCE_JOB", "test")
ALL = frozenset({"vectors", "reindex", "partitioned", "concurrent_writers"})
STORAGE = frozenset({"partitioned", "concurrent_writers"})

#: target -> (the cases it runs, the capabilities it must report)
TARGETS: dict[str, tuple[tuple[Case, ...], frozenset[str]]] = {
    "pg-index": (ALL_INDEX_CASES, ALL),
    "pg-index-runtime": (ALL_INDEX_CASES, ALL),
    "pg-file-store": (ALL_FILE_STORE_CASES, STORAGE),
    "pg-memory": (ALL_MEMORY_CASES, STORAGE),
}

#: The documented drifts of the PostgreSQL index (plan annex A, fixed by MP-B3): case -> drift number, per way of
#: ranking vectors (``pgvector``: the database ranks; ``runtime``: the runtime ranks, a server without pgvector).
#: Drift 10 (``VectorStatus.legacy``) has no generic case; see test_legacy_vectors_are_reported. MP-B3 fixed all ten
#: drifts: no case is an expected failure any more (MUP-ONA T-PG-1); a new entry needs a drift of plan annex A.
DRIFTS: dict[str, dict[str, int]] = {"pgvector": {}, "runtime": {}}


def _database() -> Any:
    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    database = PostgresDatabase(dsn, schema=schema)
    database.migrate()
    return database, schema


def _factory(target: str, database: Any) -> Callable[[Request], Any]:
    from nodestro_runtime.postgres_tables import PostgresFileStore, PostgresIndex, PostgresMemory

    def index(request: Request) -> PostgresIndex:
        opened = PostgresIndex(database, request.partition, namespace=request.scope, embeddings=request.embeddings)
        if target == "pg-index-runtime":
            opened._pgvector = False  # rank in the runtime, as a server without pgvector does
        return opened

    def files(request: Request) -> PostgresFileStore:
        return PostgresFileStore(database, request.partition, namespace=request.scope)

    def memory(request: Request) -> PostgresMemory:
        return PostgresMemory(database, request.partition, namespace=request.scope)

    return {"pg-index": index, "pg-index-runtime": index, "pg-file-store": files, "pg-memory": memory}[target]


@pytest.fixture(scope="module")
def reports() -> Callable[[str], tuple[Report, str | None]]:
    """The report of a target and how its index ranks vectors (``pgvector``, ``runtime``; ``None``: no index), run
    once per module. Each target gets its own schema, dropped right after its run: migration 0010 installs pgvector
    into the first schema that migrates, so only one test schema may exist at a time on a server with pgvector."""
    from nodestro_runtime.postgres_tables import PostgresIndex

    found: dict[str, tuple[Report, str | None]] = {}

    def report(target: str) -> tuple[Report, str | None]:
        if target not in found:
            database, schema = _database()
            try:
                mode = None
                if target.startswith("pg-index"):
                    database_ranks = PostgresIndex(database, "conformance-a", namespace="probe").pgvector()
                    if os.environ.get("NODESTRO_REQUIRE_PGVECTOR") == "1" and not database_ranks:
                        pytest.fail("NODESTRO_REQUIRE_PGVECTOR=1 but the test schema has no pgvector")
                    mode = "pgvector" if database_ranks and target == "pg-index" else "runtime"
                cases, expected = TARGETS[target]
                result = run_cases(_factory(target, database), cases, expect=expected, target=target)
            finally:
                database.close()
                postgres_support.drop_schema(schema)
            found[target] = (result, mode)
            folder = os.environ.get("NODESTRO_CONFORMANCE_REPORTS")
            if folder:
                Path(folder).mkdir(parents=True, exist_ok=True)
                data = {**result.to_dict(), "vector_ranking": mode}
                (Path(folder) / f"{target}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
        return found[target]

    return report


def _outcome(report: Report, name: str) -> Outcome:
    return next(outcome for outcome in report.outcomes if outcome.name == name)


# MUP-ONA T-PART-0
# MUP-ONA T-CONF-1
@pytest.mark.parametrize(
    ("target", "name"),
    [
        pytest.param(target, case.name, id=f"{target}:{case.name}")
        for target, (cases, _) in TARGETS.items()
        for case in cases
    ],
)
def test_conformance_case(
    request: pytest.FixtureRequest, reports: Callable[[str], tuple[Report, str | None]], target: str, name: str
) -> None:
    report, mode = reports(target)
    drift = DRIFTS[mode].get(name) if mode is not None else None
    if drift is not None:
        # The ranking path is known only once the target ran, so the mark is added here; it is a strict mark all
        # the same: a drift case that passes fails this test until its entry above is removed.
        request.applymarker(pytest.mark.xfail(strict=True, reason=f"MUP-DRIFT-{drift}"))
    outcome = _outcome(report, name)
    if outcome.status == "skipped":
        expected = load_expected_skips(EXPECTED_SKIPS, JOB, target)
        assert name in expected, f"{name} was skipped ({outcome.detail}), which is not expected"
        pytest.skip(f"{outcome.detail} (expected: {expected[name]})")
    if outcome.status != "passed":
        pytest.fail(f"{name}: {outcome.detail}", pytrace=False)


# MUP-ONA T-CONF-7
@pytest.mark.parametrize("target", list(TARGETS))
def test_target_reports_its_capabilities_and_skips_as_expected(
    reports: Callable[[str], tuple[Report, str | None]], target: str
) -> None:
    report, _ = reports(target)
    assert report.errors == [], report.summary()
    assert report.skip_differences(load_expected_skips(EXPECTED_SKIPS, JOB, target)) == [], report.summary()
    assert report.capabilities >= TARGETS[target][1]


def test_drift_marks_name_index_cases_only() -> None:
    # MUP-ONA T-PART-0: no partition case is ever an expected failure, and every drift mark names a real case.
    names = {case.name for case in ALL_INDEX_CASES}
    for marks in DRIFTS.values():
        assert set(marks) <= names
        assert not any(name.startswith("partition.") for name in marks)
        assert set(marks.values()) <= set(range(1, 11))


# MUP-ONA T-PG-3
def test_legacy_vectors_are_reported() -> None:
    """FX-PG-0 holds one zero vector per tenant, stored before vectors were checked: ``vector_status`` marks such a
    space ``legacy`` (drift 10; the suite cannot write such vectors, so this runs on the legacy fixture)."""
    from nodestro_runtime.postgres_tables import PostgresIndex
    from nodestro_runtime.storage_postgres import PostgresDatabase

    with postgres_support.restored_database("FX-PG-0.sql.gz") as conninfo:
        database = PostgresDatabase(conninfo)
        try:
            for tenant in ("tenant-a", "tenant-b"):
                status = PostgresIndex(database, tenant, namespace="nodestro").vector_status()
                assert status.legacy and status.excluded == 1, f"{tenant}: {status}"
        finally:
            database.close()
