"""Ranking compatibility of the PostgreSQL index on FX-RANK-0 (MUP-ONA MP-B3: T-RANK-1, T-RANK-2; plan 10.6, 7.2).

* **T-RANK-1**: PostgreSQL without pgvector (``pg-a``, the runtime ranks) against PostgreSQL with pgvector (``pg-b``)
  after B3 meets the equivalence criteria of plan 10.6 for every query and mode: the same top-k in the same order
  where adjacent scores differ by more than 1e-6 (relative), scores within 1e-5, equal scores ordered by chunk id.
  The recorded results (``PG-CURRENT.json``, which ``tests/test_rank_fixture.py`` proves the code reproduces) are
  checked everywhere; on a server with pgvector both paths are also measured live.
* **T-RANK-2**: the results before MP-B1/B3 (``FX-RANK-0.json``) against now (``PG-CURRENT.json``), per backend,
  embedding identity, mode and query: Overlap@10, Kendall's tau of the common hits and whether the best hit changed.
  The metrics are written as JSON to ``NODESTRO_RANKING_REPORTS`` (a CI artifact) and feed the G1 report. The test
  checks that every changed query names a drift point, and the limits of plan 7.3 (Overlap@10 median >= 0.7, no
  query below 0.4): FX-RANK-0 was built to show the drifts, so the fixes miss them in known places
  (:data:`KNOWN_BELOW_LIMITS`, each with its drift). Those are reported to gate G1, not hidden: any other place
  below a limit, or a known one that recovers, fails the test until the list is changed on purpose.
"""

from __future__ import annotations

from collections.abc import Sequence
import importlib.util
import itertools
import json
import math
import os
from pathlib import Path
import statistics
from types import ModuleType
from typing import Any

import postgres_support
import pytest

RANKING = Path(__file__).parent / "fixtures" / "ranking"
MODES = ("default", "fts", "vector", "hybrid")
#: Plan 10.6: relative score difference below which two adjacent hits count as tied, and the score tolerance.
TIE_RELATIVE = 1e-6
SCORE_TOLERANCE = 1e-5
#: Plan 7.3: Overlap@10 before against after, median and minimum per query.
MEDIAN_OVERLAP = 0.7
MIN_OVERLAP = 0.4
_DEFAULT_MODE = (
    "drift 6: the default mode is hybrid with an embedding model, before fts; the text-only queries of drift 8 and the"
    " no-match queries now rank by vectors too"
)
_DEFAULT_QUERIES = ("q12", "q13", "q14", "q16", "q17", "q18", "q19", "q20", "q22", "q23", "q26", "q27")
_ZERO_QUERY = "drift 3: q11's query vector is zero, so vector search finds nothing (before: arbitrary hits)"
#: Where the fixed ranking misses the limits of plan 7.3, by design: ``backend/identity/mode`` -> (median below 0.7,
#: the queries below 0.4, the cause). ``raw`` vectors are not normalized, so drift 4 (cosine instead of the dot product
#: in the runtime path, pg-a) reorders them; pg-b ranked by cosine before.
KNOWN_BELOW_LIMITS: dict[str, tuple[bool, tuple[str, ...], str]] = {
    **{
        f"{backend}/{identity}/default": (True, _DEFAULT_QUERIES, _DEFAULT_MODE)
        for backend in ("pg-a", "pg-b")
        for identity in ("norm", "raw")
    },
    "pg-a/norm/vector": (False, ("q11",), _ZERO_QUERY),
    "pg-a/raw/vector": (
        True,
        ("q06", "q11", "q19", "q20"),
        f"drift 4: cosine instead of the dot product; {_ZERO_QUERY}",
    ),
    "pg-a/raw/hybrid": (True, (), "drift 4: the vector half of the fusion ranks by cosine instead of the dot product"),
    "pg-b/norm/vector": (False, ("q11",), _ZERO_QUERY),
    "pg-b/raw/vector": (False, ("q11",), _ZERO_QUERY),
    "pg-b/norm/hybrid": (False, ("q11",), f"{_ZERO_QUERY}; hybrid keeps only its full-text hits"),
    "pg-b/raw/hybrid": (False, ("q11",), f"{_ZERO_QUERY}; hybrid keeps only its full-text hits"),
}


def _module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, RANKING / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fixture() -> dict[str, Any]:
    data: dict[str, Any] = _module("measure_pg").load_fixture()
    return data


@pytest.fixture(scope="module")
def current() -> dict[str, Any]:
    data: dict[str, Any] = _module("pg_current").load_current()["results"]
    return data


# ---- comparing two result lists -------------------------------------------------------------------------------


def _hits(result: dict[str, Any]) -> list[tuple[str, int, float]] | None:
    """``[(chunk key, chunk id, score)]`` of one query and mode, or ``None`` for an error."""
    if "hits" not in result:
        return None
    return [(row[0], int(row[1]), float(row[2])) for row in result["hits"]]


def _tied(left: float, right: float) -> bool:
    return abs(left - right) <= TIE_RELATIVE * max(abs(left), abs(right), 1e-12)


def equivalence_problems(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Where the results of one query and mode in pg-a and pg-b differ beyond plan 10.6 (empty: equivalent)."""
    hits_a, hits_b = _hits(a), _hits(b)
    if hits_a is None or hits_b is None:
        same_error = hits_a is None and hits_b is None and a["error"].get("code") == b["error"].get("code")
        return [] if same_error else [f"error in one path only: {a.get('error')} / {b.get('error')}"]
    problems: list[str] = []
    for name, hits in (("pg-a", hits_a), ("pg-b", hits_b)):
        for (_, left_id, left), (_, right_id, right) in itertools.pairwise(hits):
            if any(math.isnan(score) for score in (left, right)):
                problems.append(f"{name}: NaN score")
            elif left < right:
                problems.append(f"{name}: scores increase ({left} < {right})")
            elif left == right and left_id > right_id:
                problems.append(f"{name}: equal scores not ordered by chunk id ({left_id}, {right_id})")
    if len(hits_a) != len(hits_b):
        return [*problems, f"{len(hits_a)} hits against {len(hits_b)}"]
    scores_a = {key: score for key, _, score in hits_a}
    scores_b = {key: score for key, _, score in hits_b}
    # The last group of tied scores may be cut at k differently; every other key must be in both.
    cut = len(hits_a)
    while cut > 1 and _tied(hits_a[cut - 2][2], hits_a[-1][2]):
        cut -= 1
    for key, _, _ in hits_a[: cut - 1]:
        if key not in scores_b:
            problems.append(f"{key} only in pg-a")
    for key in scores_a.keys() & scores_b.keys():
        if abs(scores_a[key] - scores_b[key]) > SCORE_TOLERANCE:
            problems.append(f"{key}: score {scores_a[key]} against {scores_b[key]}")
    position_b = {key: position for position, (key, _, _) in enumerate(hits_b)}
    for (key_x, _, x), (key_y, _, y) in itertools.combinations(hits_a, 2):
        both = key_x in position_b and key_y in position_b
        if both and not _tied(x, y) and position_b[key_x] > position_b[key_y]:
            problems.append(f"{key_x} before {key_y} in pg-a ({x} > {y}), not in pg-b")
    return problems


def overlap(before: Sequence[str], after: Sequence[str]) -> float:
    """Overlap@k: the share of hits both lists have (two empty lists: 1)."""
    if not before and not after:
        return 1.0
    return len(set(before) & set(after)) / max(len(before), len(after))


def kendall_tau(before: Sequence[str], after: Sequence[str]) -> float | None:
    """Kendall's tau (tau-a) of the hits both lists have, by their positions; ``None`` below two common hits."""
    common = [key for key in before if key in set(after)]
    if len(common) < 2:
        return None
    position = {key: index for index, key in enumerate(after)}
    pairs = list(itertools.combinations(common, 2))
    concordant = sum(1 for x, y in pairs if position[x] < position[y])
    return (2 * concordant - len(pairs)) / len(pairs)


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Overlap@10, Kendall's tau and a changed best hit for one query and mode (errors count as no hits)."""
    keys_before = [hit[0] for hit in _hits(before) or []]
    keys_after = [hit[0] for hit in _hits(after) or []]
    return {
        "overlap": round(overlap(keys_before[:10], keys_after[:10]), 4),
        "tau": None if (tau := kendall_tau(keys_before, keys_after)) is None else round(tau, 4),
        "rank1_changed": (keys_before[:1] != keys_after[:1]),
        "error_before": before.get("error", {}).get("code") if "error" in before else None,
        "error_after": after.get("error", {}).get("code") if "error" in after else None,
        "before": keys_before[:10],
        "after": keys_after[:10],
    }


def metrics(fixture: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """T-RANK-2: per backend, identity and mode, the per-query metrics and their summary."""
    drifts = {query["id"]: query["drift"] for query in fixture["queries"]}
    result: dict[str, Any] = {}
    for backend in ("pg-a", "pg-b"):
        before_runs = fixture["results"][backend]["runs"]
        after_runs = current[backend]["runs"]
        for identity in fixture["identities"]:
            for mode in MODES:
                queries = {
                    query_id: {
                        **compare(before_runs[identity][query_id][mode], after_runs[identity][query_id][mode]),
                        "drift": drifts[query_id],
                    }
                    for query_id in drifts
                }
                overlaps = [entry["overlap"] for entry in queries.values()]
                taus = [entry["tau"] for entry in queries.values() if entry["tau"] is not None]
                result[f"{backend}/{identity}/{mode}"] = {
                    "median_overlap": statistics.median(overlaps),
                    "min_overlap": min(overlaps),
                    "below_0_4": sorted(q for q, entry in queries.items() if entry["overlap"] < MIN_OVERLAP),
                    "mean_tau": round(statistics.fmean(taus), 4) if taus else None,
                    "rank1_changed": sorted(q for q, entry in queries.items() if entry["rank1_changed"]),
                    "queries": queries,
                }
    return result


def _write(name: str, data: Any) -> None:
    folder = os.environ.get("NODESTRO_RANKING_REPORTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder) / name).write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")


# ---- T-RANK-1 -------------------------------------------------------------------------------------------------


def _equivalence(runs_a: dict[str, Any], runs_b: dict[str, Any]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for identity, queries in runs_a.items():
        for query_id, by_mode in queries.items():
            for mode in MODES:
                problems = equivalence_problems(by_mode[mode], runs_b[identity][query_id][mode])
                if problems:
                    found[f"{identity}/{query_id}/{mode}"] = problems
    return found


# MUP-ONA T-RANK-1
def test_postgres_with_and_without_pgvector_rank_alike(current: dict[str, Any]) -> None:
    problems = _equivalence(current["pg-a"]["runs"], current["pg-b"]["runs"])
    _write("t-rank-1-recorded.json", problems)
    assert problems == {}


# MUP-ONA T-RANK-1
def test_both_paths_rank_alike_on_the_test_server(fixture: dict[str, Any]) -> None:
    dsn = postgres_support.require_postgres()
    if not postgres_support.has_pgvector():
        if os.environ.get("NODESTRO_REQUIRE_PGVECTOR") == "1":
            pytest.fail("NODESTRO_REQUIRE_PGVECTOR=1 but the test server has no pgvector")
        return  # pg-a: only the runtime path exists here; the recorded comparison above covers both
    pg_current = _module("pg_current")
    runs = {backend: pg_current.measure_backend(dsn, backend, fixture)["runs"] for backend in ("pg-a", "pg-b")}
    problems = _equivalence(runs["pg-a"], runs["pg-b"])
    _write("t-rank-1-live.json", problems)
    assert problems == {}


# ---- T-RANK-2 -------------------------------------------------------------------------------------------------


# MUP-ONA T-RANK-2
def test_before_and_after_metrics(fixture: dict[str, Any], current: dict[str, Any]) -> None:
    found = metrics(fixture, current)
    below: dict[str, tuple[bool, tuple[str, ...]]] = {}
    for name, summary in found.items():
        for query_id, entry in summary["queries"].items():
            if entry["overlap"] < 1 or entry["rank1_changed"] or entry["error_before"] != entry["error_after"]:
                assert entry["drift"], f"{name} {query_id} changed without a documented drift point"
        median_below = summary["median_overlap"] < MEDIAN_OVERLAP
        if median_below or summary["below_0_4"]:
            below[name] = (median_below, tuple(summary["below_0_4"]))
    _write(
        "t-rank-2-metrics.json",
        {
            "limits_7_3": {"median_overlap": MEDIAN_OVERLAP, "min_overlap": MIN_OVERLAP},
            "below_limits": {
                name: {
                    "median_below": median,
                    "queries": list(queries),
                    "cause": KNOWN_BELOW_LIMITS[name][2] if name in KNOWN_BELOW_LIMITS else None,
                }
                for name, (median, queries) in below.items()
            },
            "results": found,
        },
    )
    assert below == {name: (median, queries) for name, (median, queries, _) in KNOWN_BELOW_LIMITS.items()}
