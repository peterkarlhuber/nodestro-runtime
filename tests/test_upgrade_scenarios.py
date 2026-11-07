"""Upgrade and rollback on the legacy fixtures, MUP-ONA plan section 5.3 (MP-B3: T-UPG-1..5, T-UPG-8).

FX-PG-0 (``postgres:17``) and FX-PG-0v (``pgvector/pgvector:pg17``) hold what nodestro-runtime wrote before MP-B1/B3
(``tests/fixtures/legacy/README.md``). Each case restores one into a new database and starts the current code on it
as a server does (``require_current_schema``, the tenants' knowledge tables), then:

* **U1/U2** (T-UPG-1/2): searches in every mode, writes, deletes and a reindex in ``tenant-a``; no DDL (an event
  trigger counts every DDL command), all legacy data found, ``tenant-b`` unchanged to the byte; with FX-PG-0v
  both ways of ranking vectors agree (plan 10.6);
* **U3** (T-UPG-3): after U1, the old code (``tests/fixtures/legacy/postgres_tables_pre_b3.py``, the module as it
  was before MP-B1/B3, on the current storage layer: no migration changed) reads, searches and writes the data;
* **U4** (T-UPG-4): pgvector installed by the operator on a running server: :func:`pgvector_status` reports it and
  an open index ranks with it after :meth:`PostgresIndex.refresh`, without a restart (``verify_schema`` of the plan
  comes with MP-B5; ``pgvector_status`` is today's check);
* **U5** (T-UPG-5): the legacy zero vector, and a NaN vector added by SQL, are left out of every search and counted
  in ``vector_status`` (``excluded``, ``legacy``); the stored rows stay unchanged;
* **U8** (T-UPG-8): a reindex in ``tenant-a`` killed hard (``SIGKILL``) in the middle: after a new start the old
  space is ``active``, the new one ``building`` with its vectors so far, ``tenant-b`` unchanged; the reindex resumes.

The cases connect as the owner of the restored database (DDL for the event trigger and the extension).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
from types import ModuleType
from typing import Any

import postgres_support
import pytest

LEGACY = Path(__file__).resolve().parent / "fixtures" / "legacy"
TENANTS = ("tenant-a", "tenant-b")
NAMESPACE = "nodestro"
MODES = ("fts", "vector", "hybrid")
#: The files of each tenant (tests/fixtures/legacy/make_legacy_pg.py).
SHARED = {"faq.md", "glossary.txt", "handbook.md", "late-note.txt", "zero-vector.txt"}
MARKERS = {"tenant-a": ("marker-a.txt", "aardvark"), "tenant-b": ("marker-b.txt", "zebra")}


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def models() -> dict[str, Any]:
    """The fixture's embedding models ``fx-embed-a`` (its active space) and ``fx-embed-b`` (its building space)."""
    module = sys.modules.get("make_legacy_pg") or _load("make_legacy_pg", LEGACY / "make_legacy_pg.py")
    found: dict[str, Any] = module.embedding_models()
    return found


def old_tables() -> ModuleType:
    """``postgres_tables`` as it was before MP-B1/B3 (U3), on the current storage layer."""
    name = "nodestro_runtime._pre_b3_postgres_tables"
    return sys.modules.get(name) or _load(name, LEGACY / "postgres_tables_pre_b3.py")


@contextmanager
def restored(dump: str) -> Iterator[tuple[str, Any]]:
    """The connection string of a new database with ``dump`` restored, and its :class:`PostgresDatabase`. An event
    trigger logs every DDL command into ``mup_ddl_log`` from here on."""
    from nodestro_runtime.storage_postgres import PostgresDatabase

    with postgres_support.restored_database(dump) as conninfo:
        _execute(
            conninfo,
            "CREATE TABLE public.mup_ddl_log (tag text NOT NULL)",
            "CREATE FUNCTION public.mup_log_ddl() RETURNS event_trigger LANGUAGE plpgsql AS"
            " $$ BEGIN INSERT INTO public.mup_ddl_log VALUES (tg_tag); END $$",
            "CREATE EVENT TRIGGER mup_ddl ON ddl_command_start EXECUTE FUNCTION public.mup_log_ddl()",
        )
        database = PostgresDatabase(conninfo)
        try:
            yield conninfo, database
        finally:
            database.close()


def _execute(conninfo: str, *statements: str) -> None:
    import psycopg

    with psycopg.connect(conninfo, autocommit=True) as connection:
        for statement in statements:
            connection.execute(statement)


def ddl(database: Any) -> list[str]:
    return [str(row[0]) for row in database._query("SELECT tag FROM public.mup_ddl_log")]


def snapshot(database: Any, tenant: str) -> dict[str, str]:
    """An md5 per table over every row of ``tenant`` (each table with a ``tenant_id`` column)."""
    from psycopg import sql

    tables = database._query(
        "SELECT table_name FROM information_schema.columns WHERE table_schema = 'public' AND column_name = 'tenant_id'"
        " ORDER BY table_name"
    )
    found: dict[str, str] = {}
    for (table,) in tables:
        query = sql.SQL(
            "SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM {} t WHERE tenant_id = %s"
        )
        found[str(table)] = str(database._query(query.format(sql.Identifier(table)), (tenant,))[0][0])
    return found


def start(database: Any) -> None:
    """What a server does on start: refuse a schema this release does not read (no migration, no DDL)."""
    database.require_current_schema()


def index(database: Any, tenant: str, model: str | None = "fx-embed-a", *, ranking: str = "database") -> Any:
    from nodestro_runtime.postgres_tables import PostgresIndex

    opened = PostgresIndex(database, tenant, namespace=NAMESPACE, embeddings=models()[model] if model else None)
    if ranking == "runtime":
        opened._pgvector = False
    return opened


def check_legacy_data(database: Any, tenant: str) -> None:
    """Every legacy file of ``tenant`` is there and found; the other tenant's marker never is."""
    from nodestro_runtime.postgres_tables import knowledge_tables

    tables = knowledge_tables(database, tenant)
    marker, word = MARKERS[tenant]
    assert {entry.name for entry in tables.store.list()} == SHARED | {marker}
    plain = tables.index
    assert {hit.title for hit in plain.search("heron", 10)} == {"handbook.md", "late-note.txt"}
    assert [hit.title for hit in plain.search(word)] == [marker]
    other = next(w for t, (_, w) in MARKERS.items() if t != tenant)
    assert plain.search(other) == []


def check_searches(database: Any, tenant: str, ranking: str) -> dict[str, list[tuple[str, float]]]:
    """Searches in every mode (and the default) with the fixture's model; the zero vector is never a vector hit."""
    opened = index(database, tenant, ranking=ranking)
    found: dict[str, list[tuple[str, float]]] = {}
    for query in ("heron river", "return label refund", "release alpha beta"):
        for mode in (*MODES, None):
            hits = opened.retrieve(query, 10, mode=mode)
            assert hits, (query, mode)
            assert all(hit.score == hit.score and -1 <= hit.score <= 1 for hit in hits), hits
            if mode in ("vector", "hybrid"):
                assert all(not (hit.title == "zero-vector.txt" and "vector" in hit.ranks) for hit in hits), (
                    f"zero vector ranked: {hits}"
                )
            found[f"{query}/{mode}"] = [(hit.id, hit.score) for hit in hits]
    return found


def upgrade(database: Any, ranking: str) -> dict[str, str]:
    """U1/U2: start, search, write, delete and reindex in tenant-a; returns tenant-b's snapshot from before."""
    before_b = snapshot(database, "tenant-b")
    start(database)
    for tenant in TENANTS:
        check_legacy_data(database, tenant)
        check_searches(database, tenant, ranking)
    a = index(database, "tenant-a", ranking=ranking)
    a.add("A new note about the heron's winter quarters.", source="u1-note")
    assert "u1-note" in {hit.source for hit in a.retrieve("winter quarters", 5, mode="hybrid")}
    assert a.delete("u1-note") == 1
    a.add("Another note, kept, about the river delta.", source="u1-kept")
    status = a.vector_status()
    assert status.active is not None and status.active["model"] == "fx-embed-a"
    assert status.building is not None and status.building["model"] == "fx-embed-b"
    # The cancelled reindex of the fixture resumes and switches tenant-a to fx-embed-b; the zero vector goes with it.
    status = a.reindex(models()["fx-embed-b"], batch_size=2)
    assert status.active is not None and status.active["model"] == "fx-embed-b" and status.building is None
    assert (status.complete, status.excluded, status.legacy) == (True, 0, False)
    b = index(database, "tenant-a", "fx-embed-b", ranking=ranking)
    assert "u1-kept" in {hit.source for hit in b.retrieve("river delta", 10, mode="hybrid")}
    assert ddl(database) == [], "the new state started and worked without DDL"
    assert snapshot(database, "tenant-b") == before_b
    after_b = index(database, "tenant-b", ranking=ranking).vector_status()
    assert after_b.active is not None and after_b.active["model"] == "fx-embed-a" and after_b.building is not None
    # An operator's migrate finds nothing to apply (MP-B3 adds no migration); the checksums stay green.
    assert database.migrate(before_migration=lambda current, target: None) == 10
    database.require_current_schema()
    # Its only DDL is the runner's own CREATE TABLE IF NOT EXISTS of nodestro_schema_migrations (a no-op here).
    assert ddl(database) == ["CREATE TABLE"]
    database._write("DELETE FROM public.mup_ddl_log", ())
    return before_b


# MUP-ONA T-UPG-1
@pytest.mark.parametrize("ranking", ["database", "runtime"])
def test_u1_upgrade_without_pgvector(ranking: str) -> None:
    with restored("FX-PG-0.sql.gz") as (_, database):
        assert index(database, "tenant-a").pgvector() is False  # FX-PG-0 has no extension
        upgrade(database, ranking)


# MUP-ONA T-UPG-2
def test_u2_upgrade_with_pgvector_ranks_alike_in_both_paths() -> None:
    with restored("FX-PG-0v.sql.gz") as (_, database):
        if not index(database, "tenant-a").pgvector():
            postgres_support.require_pgvector_or_skip("FX-PG-0v restored without a usable pgvector")
        # tenant-b keeps its legacy space: both paths rank it alike (plan 10.6: order, scores within 1e-5).
        start(database)
        pgvector = check_searches(database, "tenant-b", "database")
        runtime = check_searches(database, "tenant-b", "runtime")
        for key, hits in pgvector.items():
            other = runtime[key]
            assert [chunk for chunk, _ in hits] == [chunk for chunk, _ in other], key
            assert all(abs(x - y) <= 1e-5 for (_, x), (_, y) in zip(hits, other, strict=True)), key
        upgrade(database, "database")


# MUP-ONA T-UPG-3
def test_u3_the_old_state_works_with_data_the_new_state_wrote() -> None:
    old = old_tables()
    with restored("FX-PG-0.sql.gz") as (_, database):
        upgrade(database, "runtime")
        start(database)
        # The old code on the same data: tenant-a's space is fx-embed-b now (written by the new state).
        a = old.PostgresIndex(database, "tenant-a", namespace=NAMESPACE, embeddings=models()["fx-embed-b"])
        for mode in MODES:
            assert a.retrieve("river delta heron", 10, mode=mode), mode
        assert "u1-kept" in {hit.source for hit in a.retrieve("river delta", 10, mode="fts")}
        a.add("Written by the old state after a rollback.", source="u3-old")
        assert "u3-old" in {hit.source for hit in a.retrieve("rollback", 5, mode="hybrid")}
        assert a.delete("u1-kept") == 1
        status = a.vector_status()
        assert status.complete and status.active is not None and status.active["model"] == "fx-embed-b"
        b = old.PostgresIndex(database, "tenant-b", namespace=NAMESPACE, embeddings=models()["fx-embed-a"])
        assert {hit.title for hit in b.search("heron", 10)} >= {"handbook.md", "late-note.txt"}
        assert b.search("aardvark") == []
        # ... and the new state again on what the old one wrote.
        new = index(database, "tenant-a", "fx-embed-b")
        assert "u3-old" in {hit.source for hit in new.retrieve("rollback", 5, mode="hybrid")}
        assert ddl(database) == []


# MUP-ONA T-UPG-4
def test_u4_pgvector_installed_later_is_used_after_refresh() -> None:
    from nodestro_runtime.storage_postgres import pgvector_status

    if not postgres_support.has_pgvector():
        postgres_support.require_pgvector_or_skip("U4 installs pgvector into a restored FX-PG-0")
    with restored("FX-PG-0.sql.gz") as (conninfo, database):
        start(database)
        opened = index(database, "tenant-a")
        before = check_searches(database, "tenant-a", "runtime")
        assert opened.pgvector() is False and pgvector_status(database)["state"] == "installable"
        _execute(conninfo, "CREATE EXTENSION vector")  # the operator, while the server runs
        assert pgvector_status(database)["state"] == "active"
        assert opened.pgvector() is False  # detected once ...
        opened.refresh()
        assert opened.pgvector() is True  # ... and again after refresh(), without a restart
        for key, hits in before.items():
            query, mode = key.rsplit("/", 1)
            now = [(hit.id, hit.score) for hit in opened.retrieve(query, 10, mode=None if mode == "None" else mode)]
            assert [chunk for chunk, _ in now] == [chunk for chunk, _ in hits], key
            assert all(abs(x - y) <= 1e-5 for (_, x), (_, y) in zip(now, hits, strict=True)), key


# MUP-ONA T-UPG-5
@pytest.mark.parametrize("ranking", ["database", "runtime"])
def test_u5_legacy_zero_and_nan_vectors_are_left_out_and_counted(ranking: str) -> None:
    with restored("FX-PG-0.sql.gz") as (_, database):
        start(database)
        # One NaN vector more in tenant-a, as code before MP-B3 could store it (the glossary's chunk).
        assert (
            database._write(
                "UPDATE nodestro_chunk_vectors v SET embedding[1] = 'NaN' FROM nodestro_chunks c"
                " WHERE c.id = v.chunk_id AND c.tenant_id = v.tenant_id AND c.title = 'glossary.txt'"
                " AND v.tenant_id = 'tenant-a' AND v.space = (SELECT space FROM nodestro_vector_spaces"
                " WHERE tenant_id = 'tenant-a' AND namespace = 'nodestro' AND state = 'active')",
                (),
            )
            == 1
        )
        rows = "SELECT tenant_id, chunk_id, space, embedding::text FROM nodestro_chunk_vectors ORDER BY 1, 2, 3"
        stored = database._query(rows)
        for tenant, excluded in (("tenant-a", 2), ("tenant-b", 1)):
            opened = index(database, tenant, ranking=ranking)
            status = opened.vector_status()
            assert (status.excluded, status.legacy, status.complete) == (excluded, True, True), status
            left_out = {"zero-vector.txt", "glossary.txt"} if tenant == "tenant-a" else {"zero-vector.txt"}
            for query in ("zero vector chunk carries", "alpha release beta gamma", "heron river"):
                for mode in ("vector", "hybrid"):
                    hits = opened.retrieve(query, 20, mode=mode)
                    ranked = {hit.title for hit in hits if "vector" in hit.ranks}
                    assert not ranked & left_out, (tenant, query, mode, hits)
                    assert all(hit.score == hit.score for hit in hits)
            assert {hit.title for hit in opened.retrieve("zero vector chunk carries", 5, mode="fts")} >= {
                "zero-vector.txt"
            }
        assert database._query(rows) == stored, "searches and the status change no vector"
        assert ddl(database) == []


_KILLED_REINDEX = textwrap.dedent(
    """
    import time

    from orkexa.embeddings import CallableEmbeddings
    from nodestro_runtime.postgres_tables import PostgresIndex
    from nodestro_runtime.storage_postgres import PostgresDatabase

    def embed(texts):
        time.sleep(0.2)
        return [[float(len(t) % 7 + 1), 1.0, float(i % 3)] for i, t in enumerate(texts)]

    model = CallableEmbeddings(embed, name="fx-embed-c", dimensions=3, provider="fixture")
    index = PostgresIndex(PostgresDatabase({conninfo!r}), "tenant-a", namespace="nodestro")

    def progress(done, total):
        print(f"progress {{done}} {{total}}", flush=True)
        time.sleep(30)  # killed here, between two batches

    index.reindex(model, batch_size=2, progress=progress)
    """
)


# MUP-ONA T-UPG-8
def test_u8_a_reindex_killed_hard_keeps_the_active_space() -> None:
    with restored("FX-PG-0.sql.gz") as (conninfo, database):
        start(database)
        before_b = snapshot(database, "tenant-b")
        active = index(database, "tenant-a").vector_status().active
        script = _KILLED_REINDEX.format(conninfo=conninfo)
        process = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ
        )
        try:
            assert process.stdout is not None
            line = process.stdout.readline()
            assert line.startswith("progress 2 "), (line, process.stderr.read() if process.poll() is not None else "")
            process.send_signal(signal.SIGKILL)
        finally:
            process.kill()
            process.wait(30)
        assert process.returncode == -signal.SIGKILL
        # A new start: the old space is active, the killed reindex is a recognizable building space.
        database.close()
        from nodestro_runtime.storage_postgres import PostgresDatabase

        database = PostgresDatabase(conninfo)
        try:
            start(database)
            status = index(database, "tenant-a").vector_status()
            assert status.active == active and status.complete
            assert status.building is not None and status.building["model"] == "fx-embed-c"
            assert status.building_embedded == 2
            for mode in MODES:
                assert index(database, "tenant-a").retrieve("heron river", 5, mode=mode), mode
            assert snapshot(database, "tenant-b") == before_b
            b = index(database, "tenant-b").vector_status()
            assert b.active is not None and b.active["model"] == "fx-embed-a"
            assert b.building is not None and b.building["model"] == "fx-embed-b"
            assert ddl(database) == []
        finally:
            database.close()
