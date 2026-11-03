"""Keyset paging of runs by creation (``DocumentStore.list_page``, audit RT2-05 / API2-04) on SQLite and PostgreSQL.

The run list of the API pages by creation (``created_at``, then id) while ``list`` answers by update, so every older
page used to read every run of the tenant. ``list_page`` reads one page from an index (SQLite schema version 2,
PostgreSQL migration 0009): the order, the cursor, the top-level filter, the summary projection, the indexes in the
query plans and the upgrade of databases that already hold runs are tested here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import postgres_support
from pydantic import BaseModel, ConfigDict
import pytest

from nodestro_runtime.storage import CREATED_ORDER, DocumentStore, SQLiteStore


class RunDoc(BaseModel):
    id: str
    created_at: datetime
    parent_run_id: str | None = None
    status: str = "succeeded"
    flag: bool = False
    nodes: dict[str, str] = {}


class Summary(BaseModel):
    id: str
    created_at: datetime
    parent_run_id: str | None = None
    status: str = "succeeded"
    flag: bool = False


class Peek(Summary):
    model_config = ConfigDict(extra="allow")


def _postgres(_tmp: Path, cleanup: list[Callable[[], None]]) -> DocumentStore:
    from nodestro_runtime.storage_postgres import PostgresDatabase, PostgresStore

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    cleanup.append(lambda: postgres_support.drop_schema(schema))
    database = PostgresDatabase(dsn, schema=schema)
    database.migrate()
    return PostgresStore(database, "acme", owns_database=True)


def _sqlite(tmp: Path, _cleanup: list[Callable[[], None]]) -> DocumentStore:
    store = SQLiteStore(tmp / "documents.db")
    store.migrate(before_migration=None)
    return store


STORES: dict[str, Callable[[Path, list[Callable[[], None]]], DocumentStore]] = {
    "sqlite": _sqlite,
    "sqlite-unmigrated": lambda _tmp, _cleanup: SQLiteStore(":memory:"),
    "postgres": _postgres,
}


@pytest.fixture(params=list(STORES))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[DocumentStore]:
    cleanup: list[Callable[[], None]] = []
    try:
        opened = STORES[request.param](tmp_path, cleanup)
        yield opened
        opened.close()
    finally:
        for step in cleanup:
            step()


T0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=UTC)


def _fill(store: DocumentStore) -> list[RunDoc]:
    """Runs whose creation order differs from their update order and from the text order of their times."""
    runs = [
        RunDoc(id="r_whole_second", created_at=T0),  # stored as ...T08:00:00Z (no fraction)
        RunDoc(id="r_half", created_at=T0 + timedelta(milliseconds=500)),  # ...T08:00:00.500000Z
        # The same moment as r_half, written with another offset: equal times, ordered by id.
        RunDoc(id="r_offset", created_at=(T0 + timedelta(milliseconds=500)).astimezone(timezone(timedelta(hours=2)))),
        RunDoc(id="r_later", created_at=T0 + timedelta(seconds=3)),
        RunDoc(id="r_child", created_at=T0 + timedelta(seconds=2), parent_run_id="r_whole_second"),
        RunDoc(id="r_earliest", created_at=T0 - timedelta(days=1), flag=True, nodes={"a": "x" * 1000}),
        RunDoc(id="r_tie_a", created_at=T0 + timedelta(seconds=1)),
        RunDoc(id="r_tie_b", created_at=T0 + timedelta(seconds=1)),
    ]
    for run in reversed(runs):  # update order differs from creation order
        store.put("runs", run.id, run)
    store.put("runs", "r_whole_second", runs[0])  # updated last, created early
    return runs


def _expected(runs: list[RunDoc], *, top_level: bool = False) -> list[str]:
    chosen = [run for run in runs if not top_level or run.parent_run_id is None]
    return [run.id for run in sorted(chosen, key=lambda run: (run.created_at, run.id), reverse=True)]


def _all_pages(store: DocumentStore, limit: int, *, top_level: bool = False) -> list[list[str]]:
    pages: list[list[str]] = []
    before: tuple[datetime, str] | None = None
    while True:
        page, more = store.list_page("runs", RunDoc, limit=limit, before=before, top_level=top_level)
        pages.append([run.id for run in page])
        if not more:
            return pages
        assert len(page) == limit
        before = (page[-1].created_at, page[-1].id)


@pytest.mark.parametrize("limit", [1, 2, 3, 100])
def test_pages_follow_the_creation_order_without_gaps_or_repeats(store: DocumentStore, limit: int) -> None:
    runs = _fill(store)
    pages = _all_pages(store, limit)
    assert [run_id for page in pages for run_id in page] == _expected(runs)
    assert all(len(page) <= limit for page in pages)


def test_top_level_pages_leave_out_sub_workflow_runs(store: DocumentStore) -> None:
    runs = _fill(store)
    pages = _all_pages(store, 2, top_level=True)
    assert [run_id for page in pages for run_id in page] == _expected(runs, top_level=True)
    assert "r_child" not in _expected(runs, top_level=True)


def test_a_time_alone_is_a_cursor_and_more_is_exact(store: DocumentStore) -> None:
    runs = _fill(store)
    page, more = store.list_page("runs", RunDoc, limit=10, before=(T0 + timedelta(seconds=1), ""))
    assert [run.id for run in page] == ["r_offset", "r_half", "r_whole_second", "r_earliest"]
    assert len(runs) == 8
    assert not more
    # A naive time counts as UTC.
    naive, _ = store.list_page("runs", RunDoc, limit=10, before=(T0.replace(tzinfo=None), ""))
    assert [run.id for run in naive] == ["r_earliest"]
    page, more = store.list_page("runs", RunDoc, limit=len(runs))
    assert len(page) == len(runs) and not more
    page, more = store.list_page("runs", RunDoc, limit=len(runs) - 1)
    assert len(page) == len(runs) - 1 and more
    assert store.list_page("runs", RunDoc, limit=0) == ([], True)


def test_the_summary_reads_only_the_fields_of_the_model(store: DocumentStore) -> None:
    runs = _fill(store)
    store.put("runs", "r_nul", RunDoc(id="r_nul", created_at=T0 - timedelta(days=2), status="nul \x00 ä"))
    full, _ = store.list_page("runs", RunDoc, limit=100)
    summaries, _ = store.list_page("runs", Summary, limit=100, summary=True)
    assert [item.model_dump() for item in summaries] == [
        Summary.model_validate(item.model_dump()).model_dump() for item in full
    ]
    assert summaries[-1].status == "nul \x00 ä" and summaries[-2].flag is True
    # The summary leaves every other field in the database.
    peeks, _ = store.list_page("runs", Peek, limit=100, summary=True)
    # (PostgreSQL keeps a document with a NUL character as its exact text, and returns that whole.)
    assert all(not item.model_extra for item in peeks if item.id != "r_nul")
    unprojected, _ = store.list_page("runs", Peek, limit=100)
    assert all("nodes" in (item.model_extra or {}) for item in unprojected)
    assert len(full) == len(runs) + 1


def test_documents_without_a_readable_creation_time_are_left_out(store: DocumentStore) -> None:
    _fill(store)
    store.put_raw("runs", "no_time", {"id": "no_time"})
    store.put_raw("runs", "bad_time", {"id": "bad_time", "created_at": "yesterday"})
    store.put_raw("runs", "now_word", {"id": "now_word", "created_at": "now"})
    store.put_raw("runs", "bad_date", {"id": "bad_date", "created_at": "2026-13-45T00:00:00Z"})
    page, _ = store.list_page("runs", RunDoc, limit=100)
    assert {"no_time", "bad_time", "now_word", "bad_date"}.isdisjoint(run.id for run in page)
    assert len(store.list("runs", RunDoc)) == 8  # the old listing is unchanged (it reports what it cannot read)


def test_names_are_checked(store: DocumentStore) -> None:
    with pytest.raises(ValueError):
        store.list_page("runs' OR '1'='1", RunDoc, limit=1)
    with pytest.raises(ValueError):  # no index for it
        store.list_page("workflows", RunDoc, limit=1)
    with pytest.raises(ValueError):
        store.list_page("runs", RunDoc, limit=-1)


# ---- SQLite: schema version 2 -----------------------------------------------------------------------------------


def _plan(store: SQLiteStore, query: str, params: tuple[object, ...]) -> str:
    with store.maintenance() as connection:
        return " | ".join(str(row[-1]) for row in connection.execute("EXPLAIN QUERY PLAN " + query, params))


def test_sqlite_version_1_with_runs_is_upgraded_and_pages_use_the_index(tmp_path: Path) -> None:
    from nodestro_runtime import storage

    path = tmp_path / "nodestro.db"
    old = SQLiteStore(path)
    old.migrate(before_migration=None, migrations={1: storage.MIGRATIONS[1]}, target=1)
    runs = _fill(old)
    for index in range(300):
        parent = None if index % 3 == 0 else "r_whole_second"
        run = RunDoc(id=f"bulk_{index:04d}", created_at=T0 - timedelta(days=3, seconds=index), parent_run_id=parent)
        old.put("runs", run.id, run)
        runs.append(run)
    old.close()
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 1

    store = SQLiteStore(path)
    calls: list[tuple[int, int]] = []
    try:
        assert store.migrate(before_migration=lambda *versions: calls.append(versions)) == 1
        assert calls == [(1, 2)] and store.schema_version == 2
        assert [run_id for page in _all_pages(store, 7) for run_id in page] == _expected(runs)
        top = [run_id for page in _all_pages(store, 7, top_level=True) for run_id in page]
        assert top == _expected(runs, top_level=True)
        cursor = (T0.isoformat(), T0.isoformat(), "r")
        where = (
            f"collection = 'runs' AND json_valid(data) AND {CREATED_ORDER} IS NOT NULL"
            f" AND {CREATED_ORDER} <= strftime('%Y-%m-%dT%H:%M:%f', ?)"
            f" AND ({CREATED_ORDER} < strftime('%Y-%m-%dT%H:%M:%f', ?) OR id < ?)"
        )
        order = f" ORDER BY {CREATED_ORDER} DESC, id DESC LIMIT 11"
        plan = _plan(store, f"SELECT id FROM nodestro_documents WHERE {where}{order}", cursor)
        assert "nodestro_documents_runs_created" in plan and "TEMP B-TREE" not in plan
        top_where = where + " AND json_extract(data, '$.parent_run_id') IS NULL"
        plan = _plan(store, f"SELECT id FROM nodestro_documents WHERE {top_where}{order}", cursor)
        assert "nodestro_documents_runs_created_top" in plan and "TEMP B-TREE" not in plan
        # A damaged run document stops no write (the indexes are partial on valid JSON).
        store.put_raw("runs", "damaged", {"id": "damaged"})
        with store.maintenance() as connection:
            connection.execute("UPDATE nodestro_documents SET data = '{not json' WHERE id = 'damaged'")
        store.put("runs", "after", RunDoc(id="after", created_at=T0 + timedelta(days=1)))
        assert store.list_page("runs", RunDoc, limit=1)[0][0].id == "after"
    finally:
        store.close()


# ---- PostgreSQL: migration 0009 -----------------------------------------------------------------------------------


def test_postgres_0008_with_runs_is_upgraded_and_pages_use_the_index() -> None:
    from nodestro_runtime.storage_postgres import PostgresDatabase, PostgresStore, postgres_migrations

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    database = PostgresDatabase(dsn, schema=schema)
    try:
        steps = postgres_migrations()
        assert max(steps) == 9 and sorted(steps) == list(range(1, 10))  # no gap: the runner refuses one
        assert steps[9][0] == "0009_created_order.sql"
        database.migrate(migrations={version: step for version, step in steps.items() if version <= 8})
        assert database.schema_version == 8
        acme, other = PostgresStore(database, "acme"), PostgresStore(database, "other")
        runs = _fill(acme)
        for index in range(300):
            parent = None if index % 3 == 0 else "r_whole_second"
            created = T0 - timedelta(days=3, seconds=index)
            acme.put(
                "runs", f"bulk_{index:04d}", RunDoc(id=f"bulk_{index:04d}", created_at=created, parent_run_id=parent)
            )
            runs.append(RunDoc(id=f"bulk_{index:04d}", created_at=created, parent_run_id=parent))
        other.put("runs", "foreign", RunDoc(id="foreign", created_at=T0 + timedelta(days=9)))

        with pytest.raises(Exception) as refused:
            database.migrate()
        assert getattr(refused.value, "code", "") == "storage.backup_required"
        assert database.migrate(before_migration=lambda *_: None) == 8
        assert database.schema_version == 9

        # Tenant-scoped like every query; the newest run of another tenant never shows.
        assert [run_id for page in _all_pages(acme, 7) for run_id in page] == _expected(runs)
        top = [run_id for page in _all_pages(acme, 7, top_level=True) for run_id in page]
        assert top == _expected(runs, top_level=True)
        assert [run.id for run in other.list_page("runs", RunDoc, limit=5)[0]] == ["foreign"]

        with database.transaction():
            database._write("SET LOCAL enable_seqscan = off", ())
            database._write("ANALYZE nodestro_documents", ())
            for top_level, index_name in ((False, "nodestro_documents_runs_created"), (True, "_created_top")):
                plan = "\n".join(
                    str(row[0])
                    for row in database._query(
                        "EXPLAIN SELECT id FROM nodestro_documents WHERE tenant_id = %s AND collection = 'runs'"
                        " AND nodestro_created_order(data ->> 'created_at') IS NOT NULL"
                        + (" AND (data ->> 'parent_run_id') IS NULL" if top_level else "")
                        + " AND (nodestro_created_order(data ->> 'created_at'), id) < (%s::timestamptz, %s)"
                        " ORDER BY nodestro_created_order(data ->> 'created_at') DESC, id DESC LIMIT 11",
                        ("acme", T0.isoformat(), "r"),
                    )
                )
                assert index_name in plan and "Sort" not in plan, plan
    finally:
        database.close()
        postgres_support.drop_schema(schema)
