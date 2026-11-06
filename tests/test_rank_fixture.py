"""FX-RANK-0, the fixed ranking corpus of MUP-ONA (``tests/fixtures/ranking/``, a copy of orkexa's).

The fixture holds the results of the code before MP-B1/B3, among them PostgreSQL without pgvector (``pg-a``, the
runtime ranks the vectors) and with pgvector (``pg-b``). While :class:`~nodestro_runtime.postgres_tables.PostgresIndex`
is unchanged, measuring the corpus on the test database (``NODESTRO_TEST_POSTGRES``) gives exactly the recorded
results of the mode the database is in (pgvector as :meth:`PostgresIndex.pgvector` detects it).
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import postgres_support
import pytest

RANKING = Path(__file__).parent / "fixtures" / "ranking"


def _measure_pg() -> ModuleType:
    spec = importlib.util.spec_from_file_location("measure_pg", RANKING / "measure_pg.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fixture() -> dict[str, Any]:
    data: dict[str, Any] = _measure_pg().load_fixture()
    return data


# MUP-ONA T-RANK-0
def test_fixture_checksum_matches() -> None:
    recorded = (RANKING / "FX-RANK-0.json.sha256").read_text(encoding="utf-8").split()
    assert recorded[1] == "FX-RANK-0.json"
    assert recorded[0] == hashlib.sha256((RANKING / "FX-RANK-0.json").read_bytes()).hexdigest()


# MUP-ONA T-RANK-0
def test_fixture_holds_both_postgres_modes(fixture: dict[str, Any]) -> None:
    assert len(fixture["chunks"]) == 200
    assert len(fixture["queries"]) == 30
    for backend, pgvector in (("pg-a", False), ("pg-b", True)):
        result = fixture["results"][backend]
        assert result["meta"]["class"] == "nodestro_runtime.postgres_tables.PostgresIndex"
        assert set(result["runs"]) == set(fixture["identities"])
        assert {note["pgvector"] for note in result["meta"]["notes"].values()} == {pgvector}


# MUP-ONA T-RANK-0
def test_postgres_reproduces_the_recorded_results(fixture: dict[str, Any]) -> None:
    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    measure = _measure_pg()
    for identity in fixture["identities"]:
        # A schema per identity: chunk ids start at 1 again, and a pgvector extension that migration 0010 installs
        # into the schema goes with it.
        schema = postgres_support.new_schema()
        database = PostgresDatabase(dsn, schema=schema)
        try:
            database.migrate()
            runs, notes = measure.measure_identity(database, fixture, identity)
        finally:
            database.close()
            postgres_support.drop_schema(schema)
        backend = "pg-b" if notes["pgvector"] else "pg-a"
        recorded = fixture["results"][backend]
        assert notes == recorded["meta"]["notes"][identity], backend
        for query_id, by_mode in runs.items():
            expected = recorded["runs"][identity][query_id]
            assert _same(by_mode, expected), (backend, identity, query_id, by_mode, expected)


def _same(found: Any, recorded: Any) -> bool:
    """Equal, except that a full-precision score may differ in its last bits: Python 3.12 made ``sum()`` of floats
    compensated, so the runtime's cosine (pg-a) differs by one ulp between Python versions. Ids, order, modes and the
    scores rounded to six places stay exact."""
    if isinstance(found, dict) and isinstance(recorded, dict):
        return found.keys() == recorded.keys() and all(_same(found[k], recorded[k]) for k in found)
    if isinstance(found, list) and isinstance(recorded, list):
        return len(found) == len(recorded) and all(_same(a, b) for a, b in zip(found, recorded, strict=True))
    if found == recorded:
        return True
    try:
        a, b = float(found), float(recorded)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= 1e-12 * max(1.0, abs(b))
