"""FX-RANK-0, the fixed ranking corpus of MUP-ONA (``tests/fixtures/ranking/``, a copy of orkexa's).

The fixture holds the results of the code before MP-B1/B3, among them PostgreSQL without pgvector (``pg-a``, the
runtime ranks the vectors) and with pgvector (``pg-b``); they stay as they are (the "before" of the G1 report).
MP-B3 changes the ranking on purpose, so the results of the code as it is now are recorded beside the fixture in
``PG-CURRENT.json`` (``pg_current.py``); measuring the corpus on the test database (``NODESTRO_TEST_POSTGRES``) gives
exactly those: ``pg-a`` on every server (the runtime ranks), ``pg-b`` where pgvector is installed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
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


def _pg_current() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pg_current", RANKING / "pg_current.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# MUP-ONA T-RANK-0
def test_current_results_belong_to_the_fixture() -> None:
    current = _pg_current().load_current()
    assert current["fixture_sha256"] == hashlib.sha256((RANKING / "FX-RANK-0.json").read_bytes()).hexdigest()
    assert set(current["results"]) == {"pg-a", "pg-b"}


# MUP-ONA T-RANK-0
def test_postgres_reproduces_the_recorded_results(fixture: dict[str, Any]) -> None:
    dsn = postgres_support.require_postgres()
    pg_current = _pg_current()
    recorded = pg_current.load_current()["results"]
    backends = ["pg-a"]
    if postgres_support.has_pgvector():
        backends.append("pg-b")
    elif os.environ.get("NODESTRO_REQUIRE_PGVECTOR") == "1":
        pytest.fail("NODESTRO_REQUIRE_PGVECTOR=1 but the test server has no pgvector")
    for backend in backends:
        found = pg_current.measure_backend(dsn, backend, fixture)
        assert found["notes"] == recorded[backend]["notes"], backend
        for identity, runs in found["runs"].items():
            for query_id, by_mode in runs.items():
                expected = recorded[backend]["runs"][identity][query_id]
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
