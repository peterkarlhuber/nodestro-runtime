"""The storage contract: every DocumentStore operation, schema versions and migrations, and the SQL seams.

The contract cases run against every implementation in ``STORES``: SQLite on a file and in memory, and PostgreSQL
(one tenant in a fresh schema) when ``NODESTRO_TEST_POSTGRES`` names a test database (see ``postgres_support``).
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from orkexa import SQLiteIndex, SQLiteMemory
import postgres_support
from pydantic import BaseModel
import pytest

import nodestro_runtime
from nodestro_runtime import storage
from nodestro_runtime.memory_spaces import MemorySpaces
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.sqlite_tables import KnowledgeChunks, clear_memory, memory_totals
from nodestro_runtime.storage import SCHEMA_VERSION, DocumentStore, SQLiteStore, Store
from nodestro_runtime.workspace import DATABASE, LOCK_FILE, Workspace


class Doc(BaseModel):
    id: str
    kind: str = ""
    n: int = 0


StoreFactory = Callable[[Path, list[Callable[[], None]]], DocumentStore]


def _postgres_store(_tmp: Path, cleanup: list[Callable[[], None]]) -> DocumentStore:
    from nodestro_runtime.storage_postgres import PostgresDatabase, PostgresStore

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    cleanup.append(lambda: postgres_support.drop_schema(schema))
    database = PostgresDatabase(dsn, schema=schema)
    database.migrate()
    return PostgresStore(database, "local", owns_database=True)


STORES: dict[str, StoreFactory] = {
    "sqlite-file": lambda tmp, _cleanup: SQLiteStore(tmp / "documents.db"),
    "sqlite-memory": lambda _tmp, _cleanup: SQLiteStore(":memory:"),
    "postgres": _postgres_store,
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


# ---- the DocumentStore contract ---------------------------------------------------------------


def test_every_store_implements_the_protocol(store: DocumentStore) -> None:
    assert isinstance(store, DocumentStore)
    assert Store is SQLiteStore  # the name existing imports use


def test_documents_round_trip(store: DocumentStore) -> None:
    store.put("docs", "a", Doc(id="a", kind="x", n=1))
    store.put_raw("raw", "r", {"list": [1, 2], "text": "ä"})
    assert store.get("docs", "a", Doc) == Doc(id="a", kind="x", n=1)
    assert store.get_raw("docs", "a") == {"id": "a", "kind": "x", "n": 1}
    assert store.get_raw("raw", "r") == {"list": [1, 2], "text": "ä"}
    assert store.get("docs", "missing", Doc) is None
    assert store.get_raw("raw", "missing") is None
    # Collections are separate: the same id in another collection is another document.
    assert store.get("raw", "a", Doc) is None
    store.put("docs", "a", Doc(id="a", n=2))
    assert store.get("docs", "a", Doc) == Doc(id="a", n=2)


def test_documents_may_hold_any_character(store: DocumentStore) -> None:
    text = 'nul \x00 tab \t emoji \U0001f600 rtl \u05d0 \\u0000 quote "'
    store.put("docs", "a", Doc(id="a", kind=text))
    store.put_raw("raw", "r", {"k\x00": [text], "plain": "x"})
    assert store.get("docs", "a", Doc) == Doc(id="a", kind=text)
    assert store.get_raw("raw", "r") == {"k\x00": [text], "plain": "x"}
    assert store.find("docs", "id", ["a"], Doc) == [Doc(id="a", kind=text)]
    assert [collection for collection, _raw in store.search_raw(["docs", "raw"], "emoji")] in (
        ["docs", "raw"],
        ["raw", "docs"],
    )
    store.put("docs", "a", Doc(id="a", kind="plain again"))
    assert store.get("docs", "a", Doc) == Doc(id="a", kind="plain again")


def test_list_is_newest_first_and_honors_the_limit(store: DocumentStore) -> None:
    for key in ("a", "b", "c"):
        store.put("docs", key, Doc(id=key))
        time.sleep(0.002)  # update times have millisecond resolution
    assert [doc.id for doc in store.list("docs", Doc)] == ["c", "b", "a"]
    assert [doc.id for doc in store.list("docs", Doc, limit=2)] == ["c", "b"]
    store.put("docs", "a", Doc(id="a", n=1))  # put marks it as updated now
    assert next(doc.id for doc in store.list("docs", Doc)) == "a"
    time.sleep(0.002)
    store.put_raw("docs", "b", {"id": "b", "n": 5})  # put_raw keeps the update time
    assert next(doc.id for doc in store.list("docs", Doc)) == "a"
    assert store.list("empty", Doc) == []


def test_find_matches_a_top_level_field(store: DocumentStore) -> None:
    store.put("docs", "a", Doc(id="a", kind="x"))
    time.sleep(0.002)
    store.put("docs", "b", Doc(id="b", kind="y"))
    store.put("docs", "c", Doc(id="c", kind="z"))
    assert [doc.id for doc in store.find("docs", "kind", ["y", "x", "x"], Doc)] == ["a", "b"]
    assert store.find("docs", "kind", [], Doc) == []
    assert store.find("docs", "kind", ["none"], Doc) == []
    with pytest.raises(ValueError):
        store.find("docs", "kind') OR 1=1 --", ["x"], Doc)


def test_items_and_prefixes_are_in_id_order_and_literal(store: DocumentStore) -> None:
    for key in ("run_b", "run_a", "runXa", "run%x", "other"):
        store.put("docs", key, Doc(id=key))
    # Id order is the order of the characters' code points ("%" < "X" < "_").
    assert [key for key, _doc in store.items("docs", Doc)] == ["other", "run%x", "runXa", "run_a", "run_b"]
    # "_" and "%" are ordinary characters in a prefix, not wildcards.
    assert [doc.id for doc in store.list_prefix("docs", "run_", Doc)] == ["run_a", "run_b"]
    assert [doc.id for doc in store.list_prefix("docs", "run%", Doc)] == ["run%x"]
    assert store.delete_prefix("docs", "run_") == 2
    assert store.delete_prefix("docs", "run_") == 0
    assert [key for key, _doc in store.items("docs", Doc)] == ["other", "run%x", "runXa"]


def test_delete_says_whether_something_was_deleted(store: DocumentStore) -> None:
    store.put("docs", "a", Doc(id="a"))
    assert store.delete("docs", "a") is True
    assert store.delete("docs", "a") is False
    assert store.get("docs", "a", Doc) is None


def test_search_raw_finds_text_inside_documents(store: DocumentStore) -> None:
    store.put_raw("runs", "r1", {"input": "file in_0123456789ab", "status": "done"})
    store.put_raw("runs", "r2", {"input": "nothing"})
    store.put_raw("batches", "b1", {"rows": ["IN_upper"]})
    store.put_raw("other", "o1", {"input": "in_0123456789ab"})
    store.put_raw("runs", "r3", {"input": "inXstill no underscore"})
    found = store.search_raw(["runs", "batches", "runs"], "in_")
    assert sorted(collection for collection, _raw in found) == ["batches", "runs"]
    assert any("in_0123456789ab" in raw for _collection, raw in found)
    # "_" is literal (r3 does not match); ASCII letters match without case (b1).
    assert all("r3" not in raw and "inXstill" not in raw for _collection, raw in found)
    assert store.search_raw([], "in_") == []
    assert store.search_raw(["runs"], "100%") == []


def test_transactions_are_all_or_nothing_and_reentrant(store: DocumentStore) -> None:
    with pytest.raises(RuntimeError), store.transaction():
        store.put("docs", "a", Doc(id="a"))
        with store.transaction():  # nested blocks join the outer one
            store.put("docs", "b", Doc(id="b"))
        raise RuntimeError("stop")
    assert store.get("docs", "a", Doc) is None
    assert store.get("docs", "b", Doc) is None
    with store.transaction():
        store.put("docs", "a", Doc(id="a"))
        with store.transaction():
            store.delete("docs", "a")
            store.put("docs", "b", Doc(id="b"))
    assert store.get("docs", "a", Doc) is None
    assert store.get("docs", "b", Doc) == Doc(id="b")


def test_other_threads_wait_for_a_transaction(store: DocumentStore) -> None:
    order: list[str] = []
    inside = threading.Event()

    def writer() -> None:
        inside.wait(5)
        store.put("docs", "other", Doc(id="other"))
        order.append("other thread wrote")

    thread = threading.Thread(target=writer)
    thread.start()
    with store.transaction():
        store.put("docs", "mine", Doc(id="mine"))
        inside.set()
        time.sleep(0.1)
        order.append("transaction ends")
    thread.join(5)
    assert order == ["transaction ends", "other thread wrote"]


def test_concurrent_writes_are_all_kept(store: DocumentStore) -> None:
    def write(start: int) -> None:
        for number in range(start, start + 50):
            store.put("docs", f"d{number:04d}", Doc(id=f"d{number:04d}", n=number))

    threads = [threading.Thread(target=write, args=(start,)) for start in range(0, 400, 50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(store.items("docs", Doc)) == 400


def test_close_is_idempotent(store: DocumentStore) -> None:
    assert store.closed is False
    store.close()
    store.close()
    assert store.closed is True


# ---- SQLite: format, WAL, snapshot, maintenance -----------------------------------------------------

#: The table as every Nodestro release since 0.1 created it (cid, name, type, notnull, default, pk).
DOCUMENT_COLUMNS = [
    (0, "collection", "TEXT", 1, None, 1),
    (1, "id", "TEXT", 1, None, 2),
    (2, "data", "TEXT", 1, None, 0),
    (3, "updated_at", "TEXT", 1, "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')", 0),
]


def test_the_database_format_is_unchanged(tmp_path: Path) -> None:
    with_store = SQLiteStore(tmp_path / "nodestro.db")
    with_store.put("docs", "a", Doc(id="a"))
    with_store.close()
    connection = sqlite3.connect(tmp_path / "nodestro.db")
    try:
        assert connection.execute("PRAGMA table_info(nodestro_documents)").fetchall() == DOCUMENT_COLUMNS
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0  # versions are set by migrate()
        assert connection.execute("SELECT data FROM nodestro_documents").fetchone()[0] == Doc(id="a").model_dump_json()
    finally:
        connection.close()


def test_readers_see_committed_data_while_a_transaction_runs(tmp_path: Path) -> None:
    """WAL: another connection (Orkexa's tables, a backup) keeps reading the last committed state."""
    store = SQLiteStore(tmp_path / "nodestro.db")
    store.put("docs", "a", Doc(id="a", n=1))
    reader = sqlite3.connect(tmp_path / "nodestro.db", timeout=1)
    try:
        with store.transaction():
            store.put("docs", "a", Doc(id="a", n=2))
            row = reader.execute("SELECT data FROM nodestro_documents WHERE id = 'a'").fetchone()
            assert Doc.model_validate_json(row[0]).n == 1
        row = reader.execute("SELECT data FROM nodestro_documents WHERE id = 'a'").fetchone()
        assert Doc.model_validate_json(row[0]).n == 2
    finally:
        reader.close()
        store.close()


def test_snapshot_copies_a_consistent_database(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nodestro.db")
    with store.transaction():
        store.put("docs", "a", Doc(id="a"))
        store.put("docs", "b", Doc(id="b"))
    store.snapshot(tmp_path / "copy.db")
    copy = SQLiteStore(tmp_path / "copy.db")
    try:
        assert [key for key, _doc in copy.items("docs", Doc)] == ["a", "b"]
        with store.transaction(), pytest.raises(RuntimeError), store.maintenance():
            pass
    finally:
        copy.close()
        store.close()


def test_maintenance_holds_the_store(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nodestro.db")
    done: list[str] = []

    def writer() -> None:
        store.put("docs", "late", Doc(id="late"))
        done.append("write")

    try:
        with store.maintenance() as connection:
            thread = threading.Thread(target=writer)
            thread.start()
            time.sleep(0.05)
            assert done == []  # the write waits for the maintenance block
            assert connection.execute("SELECT count(*) FROM nodestro_documents").fetchone()[0] == 0
        thread.join(5)
        assert done == ["write"]
    finally:
        store.close()


# ---- schema versions and migrations --------------------------------------------------------------


def _version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _add_table(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE extra (x)")


def _fill_table(connection: sqlite3.Connection) -> None:
    connection.execute("INSERT INTO extra VALUES (1)")


def _broken(connection: sqlite3.Connection) -> None:
    connection.execute("INSERT INTO extra VALUES (1)")
    raise RuntimeError("step failed")


@pytest.fixture
def database(tmp_path: Path) -> Iterator[SQLiteStore]:
    opened = SQLiteStore(tmp_path / "nodestro.db")
    opened.put("docs", "keep", Doc(id="keep", n=7))
    yield opened
    opened.close()


def _unchanged(store: SQLiteStore) -> None:
    assert store.schema_version == 0
    assert store.get("docs", "keep", Doc) == Doc(id="keep", n=7)
    with store.maintenance() as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'extra'").fetchone() is None


def test_the_current_version_needs_no_migration_and_no_backup(database: SQLiteStore) -> None:
    calls: list[tuple[int, int]] = []
    assert database.migrate(before_migration=lambda *versions: calls.append(versions)) == 0
    assert calls == [(0, SCHEMA_VERSION)]  # this release's migration (the list and find indexes), after a backup
    assert database.migrate(before_migration=lambda *versions: calls.append(versions)) == SCHEMA_VERSION
    assert database.migrate(before_migration=None) == SCHEMA_VERSION  # nothing to do, so no backup needed
    assert calls == [(0, SCHEMA_VERSION)]
    assert sorted(storage.MIGRATIONS) == [1] == [SCHEMA_VERSION]


def test_a_migration_backs_up_first_and_runs_every_step(database: SQLiteStore) -> None:
    seen: list[Any] = []

    def backup(current: int, target: int) -> None:
        # The hook runs before anything changed.
        seen.append((current, target, database.schema_version))

    previous = database.migrate(before_migration=backup, migrations={1: _add_table, 2: _fill_table}, target=2)
    assert previous == 0
    assert seen == [(0, 2, 0)]
    assert database.schema_version == 2
    with database.maintenance() as connection:
        assert connection.execute("SELECT count(*) FROM extra").fetchone()[0] == 1
    assert database.get("docs", "keep", Doc) == Doc(id="keep", n=7)
    # Opening again: already current.
    assert database.migrate(before_migration=None, migrations={1: _add_table, 2: _fill_table}, target=2) == 2


def test_a_failing_step_leaves_the_database_unchanged(database: SQLiteStore) -> None:
    with pytest.raises(NodestroError) as error:
        database.migrate(before_migration=lambda *_: None, migrations={1: _add_table, 2: _broken}, target=2)
    assert error.value.code == "storage.migration_failed"
    assert error.value.params == {"found": 0, "supported": 2, "step": 2}
    _unchanged(database)
    database.put("docs", "after", Doc(id="after"))  # the store keeps working
    assert database.get("docs", "after", Doc) is not None


def test_a_failing_backup_stops_the_migration(database: SQLiteStore) -> None:
    def backup(_current: int, _target: int) -> None:
        raise OSError("disk full")

    with pytest.raises(NodestroError) as error:
        database.migrate(before_migration=backup, migrations={1: _add_table}, target=1)
    assert error.value.code == "storage.backup_failed"
    assert "disk full" in str(error.value)
    _unchanged(database)


def test_a_migration_without_a_backup_is_refused(database: SQLiteStore) -> None:
    with pytest.raises(NodestroError) as error:
        database.migrate(before_migration=None, migrations={1: _add_table}, target=1)
    assert error.value.code == "storage.backup_required"
    _unchanged(database)


def test_a_missing_step_is_refused_before_the_backup(database: SQLiteStore) -> None:
    calls: list[Any] = []
    with pytest.raises(NodestroError) as error:
        database.migrate(before_migration=lambda *v: calls.append(v), migrations={2: _fill_table}, target=2)
    assert error.value.code == "storage.migration_missing"
    assert calls == []
    _unchanged(database)


def test_a_database_of_a_newer_release_is_refused(database: SQLiteStore) -> None:
    with database.maintenance() as connection:
        connection.execute("PRAGMA user_version = 5")
    calls: list[Any] = []
    with pytest.raises(NodestroError) as error:
        database.migrate(before_migration=lambda *v: calls.append(v))
    assert error.value.code == "storage.schema_too_new"
    assert error.value.params == {"found": 5, "supported": SCHEMA_VERSION}
    assert calls == []
    assert database.get("docs", "keep", Doc) == Doc(id="keep", n=7)


def test_migrate_refuses_to_run_inside_a_transaction(database: SQLiteStore) -> None:
    with database.transaction(), pytest.raises(RuntimeError):
        database.migrate(before_migration=None)


# ---- the workspace: existing data directories, refusal, the default backup ----------------------------


def _old_data_directory(directory: Path) -> Path:
    """A data directory as Nodestro 0.1 and 0.2 wrote it: same statements, user_version 0."""
    directory.mkdir()
    database = directory / DATABASE
    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS nodestro_documents ("
        " collection TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,"
        " updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
        " PRIMARY KEY (collection, id))"
    )
    connection.execute(
        "INSERT INTO nodestro_documents (collection, id, data, updated_at) VALUES"
        " ('settings', 'app', '{\"default_budget_usd\": 2.5}', '2026-10-01T10:00:00.000Z')"
    )
    connection.close()
    index = SQLiteIndex(database, namespace="nodestro")
    index.add("Die Haftung ist begrenzt.", source="file_1", title="vertrag.md")
    index.close()
    memory = SQLiteMemory(database, namespace="nodestro:Team")
    memory.create("/memories/p.md", "Peter")
    memory.close()
    return directory


def _rows(database: Path) -> dict[str, list[Any]]:
    connection = sqlite3.connect(database)
    try:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            for table in ("nodestro_documents", "index_chunks", "memory_files")
        }
    finally:
        connection.close()


def test_an_existing_data_directory_opens_unchanged(tmp_path: Path) -> None:
    directory = _old_data_directory(tmp_path / "old")
    before = _rows(directory / DATABASE)
    workspace = Workspace(directory, integrations=[])
    try:
        assert workspace.settings().default_budget_usd == 2.5
        assert [hit.source for hit in workspace.knowledge.index.search("Haftung")] == ["file_1"]
        assert workspace.memory.open("Team").read("/memories/p.md") == "Peter"
        assert [space.name for space in workspace.memory.all()] == ["Team"]
    finally:
        workspace.close()
    after = _rows(directory / DATABASE)
    # Opening wrote nothing but the runtime's own bookkeeping documents; old rows are byte for byte the same.
    assert [row for row in after["nodestro_documents"] if row[0] == "settings"] == before["nodestro_documents"]
    assert after["index_chunks"] == before["index_chunks"]
    assert after["memory_files"] == before["memory_files"]
    # Schema version 1 only added indexes, after a backup of the database as it was.
    assert _version(directory / DATABASE) == SCHEMA_VERSION == 1
    backups = list((directory / "backups").glob("pre-migration-0-to-1-*.db"))
    assert len(backups) == 1 and _version(backups[0]) == 0
    assert _rows(backups[0]) == before


def test_a_workspace_refuses_a_newer_database_and_releases_the_directory(tmp_path: Path) -> None:
    directory = _old_data_directory(tmp_path / "newer")
    connection = sqlite3.connect(directory / DATABASE)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()
    with pytest.raises(NodestroError) as error:
        Workspace(directory, integrations=[])
    assert error.value.code == "storage.schema_too_new"
    # The lock is released: after the database is fixed (here: by hand), the directory opens at once.
    connection = sqlite3.connect(directory / DATABASE)
    connection.execute("PRAGMA user_version = 0")
    connection.close()
    with Workspace(directory, integrations=[]) as workspace:
        assert workspace.settings().default_budget_usd == 2.5
    assert (directory / LOCK_FILE).exists()


def test_the_workspace_backs_up_before_a_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _old_data_directory(tmp_path / "migrate")
    monkeypatch.setattr(storage, "MIGRATIONS", {1: _add_table})
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 1)
    with Workspace(directory, integrations=[]) as workspace:
        assert workspace.store.schema_version == 1
    backups = sorted((directory / "backups").glob("pre-migration-0-to-1-*.db"))
    assert len(backups) == 1
    assert not list((directory / "backups").glob("*.partial"))
    # The backup is the database before the migration.
    assert _version(backups[0]) == 0
    assert _rows(backups[0])["index_chunks"] == _rows(directory / DATABASE)["index_chunks"]


def test_a_failing_backup_keeps_the_workspace_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _old_data_directory(tmp_path / "nobackup")
    before = _rows(directory / DATABASE)
    monkeypatch.setattr(storage, "MIGRATIONS", {1: _add_table})
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 1)

    def refuse(_current: int, _target: int) -> None:
        raise OSError("read-only file system")

    with pytest.raises(NodestroError) as error:
        Workspace(directory, integrations=[], before_migration=refuse)
    assert error.value.code == "storage.backup_failed"
    assert _rows(directory / DATABASE) == before
    assert _version(directory / DATABASE) == 0
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 0)
    with Workspace(directory, integrations=[]):  # the directory lock was released
        pass


# ---- Orkexa's tables: knowledge chunks (FTS5) and memory ------------------------------------------


def test_knowledge_chunks_are_counted_attached_and_read(tmp_path: Path) -> None:
    database = str(tmp_path / "nodestro.db")
    index = SQLiteIndex(database, namespace="nodestro")
    other = SQLiteIndex(database, namespace="elsewhere")
    try:
        index.add("Erster Teil über Haftung.", source="file_a", title="a.md")
        index.add("Zweiter Text ohne das Wort.", source="file_b", title="b.md")
        other.add("Haftung in einem anderen Namensraum.", source="file_a", title="x.md")
        chunks = KnowledgeChunks(index)
        assert chunks.counts() == {"file_a": 1, "file_b": 1}
        assert chunks.text("file_a") == "Erster Teil über Haftung."
        assert chunks.text("missing") == ""
        hits = index.search("Haftung")  # FTS5 finds the chunk of this namespace only
        assert [(hit.source, hit.file_id) for hit in hits] == [("file_a", None)]
        chunks.attach_file("file_a")
        assert [(hit.source, hit.file_id) for hit in index.search("Haftung")] == [("file_a", "file_a")]
        assert [hit.file_id for hit in other.search("Haftung")] == [None]
    finally:
        index.close()
        other.close()


def test_memory_totals_and_clearing(tmp_path: Path) -> None:
    database = str(tmp_path / "nodestro.db")
    assert memory_totals(database, "nodestro:") == []  # works before any memory table exists
    spaces = MemorySpaces(database)
    try:
        team = spaces.open("Team")
        team.write("/memories/a.md", "äb")
        team.write("/memories/b.md", "c")
        spaces.open("Solo").write("/memories/x.md", "x")
        foreign = SQLiteMemory(database, namespace="other")
        foreign.create("/memories/y.md", "y")
        foreign.close()
        assert memory_totals(database, "nodestro:") == [("nodestro:Solo", 1, 1), ("nodestro:Team", 2, 4)]
        clear_memory(team)
        assert team.entries() == []
        assert [(space.name, space.files) for space in spaces.all()] == [("Solo", 1)]
    finally:
        spaces.close()


# ---- the seams: no SQL outside the storage modules ----------------------------------------------------

#: Modules allowed to use a database connection: the document store and the SQL on Orkexa's tables.
STORAGE_MODULES = {
    "storage.py",
    "sqlite_tables.py",
    "storage_postgres.py",
    "postgres_tables.py",
    "postgres_import.py",
    "identity_store.py",
}


def test_no_module_outside_storage_touches_a_connection() -> None:
    package = Path(nodestro_runtime.__file__).parent
    offenders = []
    for path in sorted(package.rglob("*.py")):
        if path.name in STORAGE_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "_connection":
                offenders.append(f"{path.name}:{node.lineno} ._connection")
            if isinstance(node, ast.Attribute) and node.attr == "connect" and _is_sqlite3(node.value):
                offenders.append(f"{path.name}:{node.lineno} sqlite3.connect")
            if isinstance(node, ast.Import | ast.ImportFrom) and _imports_sqlite3(node):
                offenders.append(f"{path.name}:{node.lineno} import sqlite3")
    assert offenders == []


def _is_sqlite3(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "sqlite3"


def _imports_sqlite3(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.ImportFrom):
        return node.module == "sqlite3"
    return any(alias.name == "sqlite3" for alias in node.names)


class Request(BaseModel):
    id: str
    status: str = "pending"
    deadline: str | None = None
    by: str = ""


def _later(seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def test_replace_if_changes_only_the_expected_state(store: DocumentStore) -> None:
    store.put("requests", "r1", Request(id="r1", deadline=_later(60)))
    approved = Request(id="r1", status="approved", deadline=_later(60), by="anna")
    assert store.replace_if("requests", "r1", approved, field="status", expected="pending", live_until="deadline")
    # The second decision finds the state changed and loses; nothing is overwritten.
    rejected = Request(id="r1", status="rejected", by="ben")
    assert not store.replace_if("requests", "r1", rejected, field="status", expected="pending")
    assert store.get("requests", "r1", Request) == approved
    assert not store.replace_if("requests", "missing", approved, field="status", expected="pending")


def test_replace_if_respects_the_deadline(store: DocumentStore) -> None:
    store.put("requests", "past", Request(id="past", deadline=_later(-1)))
    store.put("requests", "none", Request(id="none"))
    store.put_raw("requests", "bad", {"id": "bad", "status": "pending", "deadline": "soon"})
    for key in ("past", "none", "bad"):
        late = Request(id=key, status="approved")
        assert not store.replace_if("requests", key, late, field="status", expected="pending", live_until="deadline")
    # Without the deadline check the state transition itself (e.g. to "expired") still works.
    expired = Request(id="past", status="expired")
    assert store.replace_if("requests", "past", expired, field="status", expected="pending")


def test_replace_if_has_one_winner_across_threads(store: DocumentStore) -> None:
    store.put("requests", "r", Request(id="r", deadline=_later(60)))
    wins: list[str] = []
    barrier = threading.Barrier(8)

    def decide(who: str) -> None:
        barrier.wait()
        if store.replace_if(
            "requests", "r", Request(id="r", status="approved", by=who), field="status", expected="pending"
        ):
            wins.append(who)

    threads = [threading.Thread(target=decide, args=(f"p{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(wins) == 1
    stored = store.get("requests", "r", Request)
    assert stored is not None and stored.by == wins[0]
