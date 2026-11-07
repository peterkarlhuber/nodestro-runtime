"""Upgrade and rollback on the legacy fixtures after the switch to Orkexa's PostgreSQL backends (MUP-ONA B6, plan
section 5.3: T-UPG-1..8).

``tests/test_upgrade_scenarios.py`` (MP-B3) runs U1-U5 and U8 against the current code, which since B6 is Orkexa's
``orkexa.backends.postgres`` under Nodestro's wiring; it stays as it is. This module adds what B6 changes: every start
here is a server's start after B6 -- the migration checksums (``require_current_schema``) **and** the read-only check
of the knowledge and memory tables (:func:`~nodestro_runtime.postgres_tables.require_knowledge_schema`, Orkexa's
``verify_schema`` with ``PostgresLayout("nodestro_")``) -- and the "old state" of U3 is the runtime before B6:

* **U1/U2** (T-UPG-1/2): FX-PG-0 / FX-PG-0v, start without DDL, the check reports the schema usable (and pgvector as
  installed or not), then the searches, writes, deletes and the reindex of MP-B3's ``upgrade`` in ``tenant-a``,
  ``tenant-b`` unchanged to the byte; with FX-PG-0v both ways of ranking agree (plan 10.6);
* **U3** (T-UPG-3): after U1, the runtime before B6 (``tests/fixtures/legacy/pre_b6``: ``postgres_tables`` and the
  generic package ``pg_generic`` of git 76f9cb2, the previous pin, loaded in a separate process) reads, searches and
  writes the data the new state wrote, and the new state reads what the old one wrote;
* **U4** (T-UPG-4): pgvector installed by the operator on a running server: ``verify_schema`` reports it (no
  restart) and an open index ranks with it after ``refresh()``;
* **U5** (T-UPG-5): the start check accepts the legacy zero vector and a NaN vector (they are data, not schema);
  searches leave them out and ``vector_status`` counts them; the rows stay unchanged;
* **U6** (T-UPG-6): the SQLite files themselves are orkexa's (FX-SQ-0 written and read both ways, T-SQL-10, at the
  pinned commit); B6 changes no SQLite code of Nodestro, and the orkexa pin changes ``orkexa.backends.sqlite`` only in
  how ``SQLiteIndex.retrieve`` reads (one read transaction), not in what it stores. Here: a knowledge base on SQLite
  is written and searched without psycopg and never loads the PostgreSQL backends;
* **U7** (T-UPG-7): a schema deviation (an index dropped, a column type changed by hand) passes the migration
  checksums but stops the start with ``storage.schema_mismatch`` naming the deviation, and the check changes
  nothing (no DDL, every row unchanged, the deviation still there);
* **U8** (T-UPG-8): a reindex killed hard; the next start (with the check) finds the old space ``active`` and the
  killed one ``building``, ``tenant-b`` unchanged.

The cases connect as the owner of the restored database (DDL for the event trigger, the extension and U7's damage).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
from typing import Any

import postgres_support
import pytest
import test_upgrade_scenarios as b3

LEGACY = Path(__file__).resolve().parent / "fixtures" / "legacy"
PRE_B6 = LEGACY / "pre_b6"


def start(database: Any) -> Any:
    """What a server does on start after B6: refuse a schema this release does not read (migration checksums, then
    the knowledge and memory tables as Orkexa reads them); no migration, no DDL. Returns the check's report."""
    from nodestro_runtime.postgres_tables import require_knowledge_schema

    database.require_current_schema()
    report = require_knowledge_schema(database)
    assert report.ok and report.schema == "public", report.summary()
    return report


# MUP-ONA T-UPG-1
@pytest.mark.parametrize("ranking", ["database", "runtime"])
def test_u1_upgrade_without_pgvector_starts_with_the_schema_check(ranking: str) -> None:
    with b3.restored("FX-PG-0.sql.gz") as (_, database):
        report = start(database)
        assert report.pgvector is False  # FX-PG-0 has no extension
        assert b3.ddl(database) == [], "the start check is read-only"
        b3.upgrade(database, ranking)
        start(database)
        assert b3.ddl(database) == []


# MUP-ONA T-UPG-2
def test_u2_upgrade_with_pgvector_starts_with_the_schema_check() -> None:
    with b3.restored("FX-PG-0v.sql.gz") as (_, database):
        if not b3.index(database, "tenant-a").pgvector():
            postgres_support.require_pgvector_or_skip("FX-PG-0v restored without a usable pgvector")
        report = start(database)
        assert report.pgvector is True and report.pgvector_version
        pgvector = b3.check_searches(database, "tenant-b", "database")
        runtime = b3.check_searches(database, "tenant-b", "runtime")
        for key, hits in pgvector.items():
            other = runtime[key]
            assert [chunk for chunk, _ in hits] == [chunk for chunk, _ in other], key
            assert all(abs(x - y) <= 1e-5 for (_, x), (_, y) in zip(hits, other, strict=True)), key
        b3.upgrade(database, "database")
        start(database)
        assert b3.ddl(database) == []


#: The runtime before B6 in a process of its own: its generic package as ``nodestro_runtime.pg_generic`` (the module
#: name it had), its ``postgres_tables`` beside it, on the current storage layer (no migration changed since).
_OLD_STATE = textwrap.dedent(
    """
    import importlib.util
    import json
    from pathlib import Path
    import sys

    import nodestro_runtime

    root = Path({root!r})


    def load(name, path, package=None):
        spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=package)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


    load("nodestro_runtime.pg_generic", root / "pg_generic" / "__init__.py", [str(root / "pg_generic")])
    old = load("nodestro_runtime._pre_b6_postgres_tables", root / "postgres_tables.py")
    assert old.PostgresIndex.__mro__[1].__module__ == "nodestro_runtime.pg_generic.index"
    assert "orkexa.backends.postgres" not in sys.modules, "the old state must not use the new backend"
    models = load("make_legacy_pg", root.parent / "make_legacy_pg.py").embedding_models()

    from nodestro_runtime.storage_postgres import PostgresDatabase

    database = PostgresDatabase({conninfo!r})
    database.require_current_schema()  # the old state's start
    found = {{}}
    a = old.PostgresIndex(database, "tenant-a", namespace="nodestro", embeddings=models["fx-embed-b"])
    modes = ("fts", "vector", "hybrid")
    found["modes"] = {{mode: len(a.retrieve("river delta heron", 10, mode=mode)) for mode in modes}}
    found["kept"] = sorted({{hit.source for hit in a.retrieve("river delta", 10, mode="fts")}})
    a.add("Written by the old state after a rollback.", source="u3-old")
    found["old_note"] = sorted({{hit.source for hit in a.retrieve("rollback", 5, mode="hybrid")}})
    found["deleted"] = a.delete("u1-kept")
    status = a.vector_status()
    found["status"] = [status.complete, (status.active or {{}}).get("model")]
    b = old.PostgresIndex(database, "tenant-b", namespace="nodestro", embeddings=models["fx-embed-a"])
    found["b_heron"] = sorted({{hit.title for hit in b.search("heron", 10)}})
    found["b_marker_a"] = [hit.title for mode in modes for hit in b.retrieve("aardvark amethyst", 10, mode=mode)]
    files = old.knowledge_tables(database, "tenant-a").store
    found["files"] = sorted(entry.name for entry in files.list())
    memory = old.PostgresMemoryTables(database, "tenant-a").open_space("nodestro:fixture-notes")
    found["memory"] = memory.view("/memories/notes.md")
    memory.create("/memories/u3-old.md", "Written by the old state.")
    found["totals"] = old.memory_totals(database, "tenant-a", "nodestro:")
    database.close()
    print(json.dumps(found))
    """
)


# MUP-ONA T-UPG-3
def test_u3_the_runtime_before_b6_works_with_data_the_new_state_wrote() -> None:
    from nodestro_runtime.postgres_tables import PostgresMemoryTables

    with b3.restored("FX-PG-0.sql.gz") as (conninfo, database):
        start(database)
        b3.upgrade(database, "runtime")
        script = _OLD_STATE.format(root=str(PRE_B6), conninfo=conninfo)
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=300, env=os.environ, check=False
        )
        assert result.returncode == 0, result.stderr
        found = json.loads(result.stdout.strip().splitlines()[-1])
        assert all(count > 0 for count in found["modes"].values()), found["modes"]
        assert "u1-kept" in found["kept"]
        assert "u3-old" in found["old_note"]
        assert found["deleted"] == 1
        assert found["status"] == [True, "fx-embed-b"]  # the space the new state's reindex switched to
        assert {"handbook.md", "late-note.txt"} <= set(found["b_heron"]) and "marker-a.txt" not in found["b_marker_a"]
        assert set(found["files"]) == b3.SHARED | {b3.MARKERS["tenant-a"][0]}
        assert "The heron is blue." in found["memory"]
        assert found["totals"][0][:2] == ["nodestro:fixture-notes", 4]
        # ... and the new state again, on what the old one wrote.
        start(database)
        new = b3.index(database, "tenant-a", "fx-embed-b")
        assert "u3-old" in {hit.source for hit in new.retrieve("rollback", 5, mode="hybrid")}
        assert "u1-kept" not in new.sources()
        space = PostgresMemoryTables(database, "tenant-a").open_space("nodestro:fixture-notes")
        assert "Written by the old state." in space.view("/memories/u3-old.md")
        assert b3.ddl(database) == []


# MUP-ONA T-UPG-4
def test_u4_the_schema_check_reports_pgvector_installed_later() -> None:
    from nodestro_runtime.postgres_tables import require_knowledge_schema

    if not postgres_support.has_pgvector():
        postgres_support.require_pgvector_or_skip("U4 installs pgvector into a restored FX-PG-0")
    with b3.restored("FX-PG-0.sql.gz") as (conninfo, database):
        assert start(database).pgvector is False
        opened = b3.index(database, "tenant-a")
        before = b3.check_searches(database, "tenant-a", "runtime")
        assert opened.pgvector() is False
        b3._execute(conninfo, "CREATE EXTENSION vector")  # the operator, while the server runs
        report = require_knowledge_schema(database)  # no restart: the next check sees it
        assert report.ok and report.pgvector is True and report.pgvector_version
        opened.refresh()
        assert opened.pgvector() is True
        for key, hits in before.items():
            query, mode = key.rsplit("/", 1)
            now = [(hit.id, hit.score) for hit in opened.retrieve(query, 10, mode=None if mode == "None" else mode)]
            assert [chunk for chunk, _ in now] == [chunk for chunk, _ in hits], key
            assert all(abs(x - y) <= 1e-5 for (_, x), (_, y) in zip(now, hits, strict=True)), key


# MUP-ONA T-UPG-5
@pytest.mark.parametrize("ranking", ["database", "runtime"])
def test_u5_the_start_check_accepts_legacy_zero_and_nan_vectors(ranking: str) -> None:
    with b3.restored("FX-PG-0.sql.gz") as (_, database):
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
        start(database)  # legacy vectors are data: the schema is usable
        for tenant, excluded in (("tenant-a", 2), ("tenant-b", 1)):
            status = b3.index(database, tenant, ranking=ranking).vector_status()
            assert (status.excluded, status.legacy, status.complete) == (excluded, True, True), status
            hits = b3.index(database, tenant, ranking=ranking).retrieve("zero vector chunk carries", 20, mode="vector")
            assert "zero-vector.txt" not in {hit.title for hit in hits}
        assert database._query(rows) == stored
        assert b3.ddl(database) == []


# MUP-ONA T-UPG-6
def test_u6_the_sqlite_knowledge_base_never_loads_the_postgres_backends(tmp_path: Path) -> None:
    """The SQLite side of U6 is orkexa's (T-SQL-10 on FX-SQ-0); B6 changes no SQLite code. What B6 could break here
    is the desktop pulling in the server's backends: a knowledge base on SQLite, written and searched, in a process
    with psycopg blocked, never imports ``orkexa.backends.postgres``."""
    code = textwrap.dedent(
        f"""
        import sys
        sys.modules["psycopg"] = None
        from nodestro_runtime.knowledge import KnowledgeBase
        knowledge = KnowledgeBase({str(tmp_path / "knowledge.db")!r})
        knowledge.add(b"The heron nests by the river.", name="heron.txt", description="u6")
        assert [hit.title for hit in knowledge.index.search("heron")] == ["heron.txt"]
        knowledge.close()
        print(sorted(m for m in sys.modules if m.startswith("orkexa.backends.postgres")))
        """
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


#: U7's deviations: what an operator (or a broken restore) can do to the tables without touching the migration
#: checksums, and what the check must say about it.
DEVIATIONS = {
    "missing index": (
        "DROP INDEX nodestro_chunks_search",
        "missing_index: nodestro_chunks.nodestro_chunks_search",
        "SELECT count(*) FROM pg_indexes WHERE indexname = 'nodestro_chunks_search'",
        0,
    ),
    "column type": (
        "ALTER TABLE nodestro_chunk_vectors ALTER COLUMN embedding TYPE double precision[]",
        "wrong_column: nodestro_chunk_vectors.embedding",
        "SELECT udt_name FROM information_schema.columns WHERE table_name = 'nodestro_chunk_vectors'"
        " AND column_name = 'embedding'",
        "_float8",
    ),
}


# MUP-ONA T-UPG-7
@pytest.mark.parametrize("deviation", list(DEVIATIONS))
def test_u7_a_schema_deviation_stops_the_start_and_changes_nothing(deviation: str) -> None:
    from nodestro_runtime.postgres_tables import require_knowledge_schema
    from nodestro_runtime.problems import NodestroError

    damage, expected, probe, still = DEVIATIONS[deviation]
    with b3.restored("FX-PG-0.sql.gz") as (conninfo, database):
        b3._execute(conninfo, damage)
        database._write("DELETE FROM public.mup_ddl_log", ())
        before = {tenant: b3.snapshot(database, tenant) for tenant in b3.TENANTS}
        database.require_current_schema()  # the migration checksums cannot see it ...
        with pytest.raises(NodestroError) as raised:
            require_knowledge_schema(database)  # ... the start check does
        error = raised.value
        assert error.code == "storage.schema_mismatch"
        assert any(problem.startswith(expected) for problem in error.params["problems"]), error.params
        message = str(error)
        assert expected in message and "Nothing was changed" in message and "nodestro_" in message
        # The check changed nothing: no DDL, every row as before, the deviation still there.
        assert b3.ddl(database) == []
        assert {tenant: b3.snapshot(database, tenant) for tenant in b3.TENANTS} == before
        assert database._query(probe)[0][0] == still


# MUP-ONA T-UPG-8
def test_u8_after_a_reindex_killed_hard_the_next_start_passes_the_check() -> None:
    with b3.restored("FX-PG-0.sql.gz") as (conninfo, database):
        start(database)
        before_b = b3.snapshot(database, "tenant-b")
        active = b3.index(database, "tenant-a").vector_status().active
        process = subprocess.Popen(
            [sys.executable, "-c", b3._KILLED_REINDEX.format(conninfo=conninfo)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ,
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
        database.close()
        from nodestro_runtime.storage_postgres import PostgresDatabase

        database = PostgresDatabase(conninfo)
        try:
            start(database)  # a new start, with the check
            status = b3.index(database, "tenant-a").vector_status()
            assert status.active == active and status.complete
            assert status.building is not None and status.building["model"] == "fx-embed-c"
            assert b3.snapshot(database, "tenant-b") == before_b
            assert b3.ddl(database) == []
        finally:
            database.close()
