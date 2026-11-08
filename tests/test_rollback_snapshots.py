"""Rollback evidence of the old code (TABRION QA-13): the snapshots of ``tests/rollback_snapshots.py``.

The golden files in ``tests/fixtures/rollback/`` were written from the old code (``tests/fixtures/rollback/SOURCE.md``).
The TABRION MIG-NOD steps of the server profile and their reverts must leave them unchanged unless the step's pull
request rewrites a file on purpose (reviewed diff; MIG-NOD-3 does so for the messages that carry the canary of
finding B-06-2). Two runs on two fresh schemas must write identical snapshots, so a difference is a change of
behaviour, never noise of the run.
"""

from __future__ import annotations

from collections.abc import Iterator
import json
import os
from typing import Any

import postgres_support
import pytest

pytest.importorskip("psycopg")
pytest.importorskip("axentra")

import rollback_snapshots

#: Prefix of the schemas and roles of the runs (each run drops its own objects again).
PREFIX = os.environ.get("NODESTRO_TEST_EVIDENCE_PREFIX", "nodestro_ev_")


@pytest.fixture(scope="module")
def runs() -> Iterator[list[dict[str, dict[str, Any]]]]:
    dsn = postgres_support.require_postgres()
    yield [rollback_snapshots.collect(dsn, PREFIX), rollback_snapshots.collect(dsn, PREFIX)]


@pytest.mark.parametrize("name", rollback_snapshots.NAMES)
def test_snapshot_equals_the_old_code(runs: list[dict[str, dict[str, Any]]], name: str) -> None:
    text = rollback_snapshots.diff(name, rollback_snapshots.golden(name), runs[0][name])
    assert not text, f"the {name} snapshot differs from the old code:\n{text}"


@pytest.mark.parametrize("name", rollback_snapshots.NAMES)
def test_two_runs_write_identical_snapshots(runs: list[dict[str, dict[str, Any]]], name: str) -> None:
    assert rollback_snapshots.dumps(runs[0][name]) == rollback_snapshots.dumps(runs[1][name])


@pytest.mark.parametrize("name", rollback_snapshots.NAMES)
def test_golden_files_are_canonical(name: str) -> None:
    path = rollback_snapshots.GOLDEN / f"{name}.json"
    assert path.read_text("utf-8") == rollback_snapshots.dumps(json.loads(path.read_text("utf-8")))


def test_no_object_of_a_run_is_left_behind(runs: list[dict[str, dict[str, Any]]]) -> None:
    import psycopg

    with psycopg.connect(postgres_support.DSN, autocommit=True) as conn:
        left = conn.execute(
            "SELECT nspname::text FROM pg_namespace WHERE nspname LIKE %s"
            " UNION ALL SELECT rolname::text FROM pg_roles WHERE rolname LIKE %s",
            (PREFIX + "%", PREFIX + "%"),
        ).fetchall()
    assert left == []


def test_the_role_table_has_eight_variants() -> None:
    table = rollback_snapshots.golden("roles")
    assert len(table["variants"]) == 8 and set(table["table"]) == set(table["variants"])


def test_the_error_snapshot_records_finding_b_06_2() -> None:
    """The old code puts the server's DETAIL value into two migration messages (B-06-2); MIG-NOD-3 closes it and
    rewrites this snapshot, so the change is visible in its diff."""
    errors = rollback_snapshots.golden("errors")["errors"]
    leaking = sorted(label for label, answer in errors.items() if answer["message_has_canary"])
    assert leaking == ["backup_failed", "migration_failed", "migration_failed_after_backup"]
