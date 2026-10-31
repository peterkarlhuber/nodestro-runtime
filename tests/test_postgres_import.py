"""Moving a desktop database into a server tenant (needs ``NODESTRO_TEST_POSTGRES``)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import sqlite3

import postgres_support
import pytest

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.workspace import DATABASE, Workspace

pytest.importorskip("psycopg")

from nodestro_runtime.knowledge import KnowledgeBase
from nodestro_runtime.memory_spaces import MemorySpaces
from nodestro_runtime.postgres_import import import_desktop_database
from nodestro_runtime.postgres_tables import PostgresMemoryTables, knowledge_tables
from nodestro_runtime.storage_postgres import PostgresDatabase


@pytest.fixture
def server() -> Iterator[PostgresDatabase]:
    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    database = PostgresDatabase(dsn, schema=schema)
    database.migrate()
    yield database
    database.close()
    postgres_support.drop_schema(schema)


@pytest.fixture
def desktop(tmp_path: Path) -> Path:
    """A closed desktop data directory with documents, knowledge, memory and a provenance record."""
    data_dir = tmp_path / "desktop"
    workspace = Workspace(data_dir, integrations=[])
    try:
        workspace.store.put_raw("workflows", "wf-1", {"id": "wf-1", "name": "Angebot \x00 prüfen"})
        workspace.store.put_raw("settings", "main", {"theme": "dark"})
        workspace.store.put_raw("tool_provenance", "my_tool", {"name": "my_tool", "versions": [1]})
        workspace.knowledge.add("Die Haftung ist begrenzt. Café".encode(), name="vertrag.md")
        workspace.knowledge.add(b"\x89PNG\r\n\x1a\nimage", name="bild.png")
        workspace.memory.open("Team").write("/memories/notes/a.md", "Peter mag Tee")
        (data_dir / "tools").mkdir(exist_ok=True)
        (data_dir / "tools" / "my_tool.py").write_text("def run(): pass\n")
    finally:
        workspace.close()
    return data_dir / DATABASE


def test_a_desktop_database_moves_into_an_empty_tenant(desktop: Path, server: PostgresDatabase) -> None:
    report = import_desktop_database(desktop, server, "acme")
    assert report.files == 2 and report.memory_files == 1 and report.chunks >= 1
    assert report.skipped == {"tool_provenance": 1}
    assert any(item.startswith("tools/") for item in report.not_imported)
    assert any(item.startswith("tool_provenance") for item in report.not_imported)

    store = server.store("acme")
    assert store.get_raw("workflows", "wf-1") == {"id": "wf-1", "name": "Angebot \x00 prüfen"}
    assert store.get_raw("settings", "main") == {"theme": "dark"}
    assert store.get_raw("tool_provenance", "my_tool") is None  # imported tools are untrusted

    knowledge = KnowledgeBase(knowledge_tables(server, "acme"))
    assert sorted(item.name for item in knowledge.files()) == ["bild.png", "vertrag.md"]
    contract = next(item for item in knowledge.files() if item.name == "vertrag.md")
    assert contract.searchable
    assert [hit.source for hit in knowledge.search("haftung")] == [contract.id]
    assert [hit.source for hit in knowledge.search("cafe")] == [contract.id]
    memory = MemorySpaces(PostgresMemoryTables(server, "acme"))
    assert memory.open("Team").read("/memories/notes/a.md") == "Peter mag Tee"
    assert [(info.name, info.files) for info in memory.all()] == [("Team", 1)]

    # Nothing reached another tenant.
    assert server.store("other").get_raw("workflows", "wf-1") is None
    assert KnowledgeBase(knowledge_tables(server, "other")).files() == []


def test_a_tenant_with_data_is_refused(desktop: Path, server: PostgresDatabase) -> None:
    server.store("acme").put_raw("workflows", "existing", {"id": "existing"})
    with pytest.raises(NodestroError) as caught:
        import_desktop_database(desktop, server, "acme")
    assert caught.value.code == "storage.import_target_not_empty"
    assert server.store("acme").get_raw("workflows", "wf-1") is None


def test_a_failing_import_leaves_nothing_behind(desktop: Path, server: PostgresDatabase) -> None:
    with sqlite3.connect(desktop) as connection:
        connection.execute("INSERT INTO nodestro_documents (collection, id, data) VALUES ('zzz', 'broken', 'not json')")
    connection.close()
    with pytest.raises(Exception):  # noqa: B017 - the driver's error for the invalid JSON
        import_desktop_database(desktop, server, "acme")
    for table in ("nodestro_documents", "nodestro_files", "nodestro_chunks", "nodestro_memory_files"):
        assert server._query(f"SELECT count(*) FROM {table}")[0][0] == 0, table
    # The connection is still usable.
    server.store("acme").put_raw("workflows", "after", {"id": "after"})


def test_sources_that_are_not_desktop_databases_are_refused(tmp_path: Path, server: PostgresDatabase) -> None:
    with pytest.raises(NodestroError) as caught:
        import_desktop_database(tmp_path / "missing.db", server, "acme")
    assert caught.value.code == "storage.import_source"
    (tmp_path / "text.db").write_text("hello")
    with pytest.raises(NodestroError) as caught:
        import_desktop_database(tmp_path / "text.db", server, "acme")
    assert caught.value.code == "storage.import_source"
    other = tmp_path / "other.db"
    with sqlite3.connect(other) as connection:
        connection.execute("CREATE TABLE something (x)")
    connection.close()
    with pytest.raises(NodestroError) as caught:
        import_desktop_database(other, server, "acme")
    assert caught.value.code == "storage.import_source"


def test_a_newer_desktop_database_is_refused(desktop: Path, server: PostgresDatabase) -> None:
    with sqlite3.connect(desktop) as connection:
        connection.execute("PRAGMA user_version = 99")
    connection.close()
    with pytest.raises(NodestroError) as caught:
        import_desktop_database(desktop, server, "acme")
    assert caught.value.code == "storage.import_source"
    assert caught.value.params["found"] == 99


def test_the_source_is_not_changed(desktop: Path, server: PostgresDatabase) -> None:
    before = desktop.read_bytes()
    import_desktop_database(desktop, server, "acme")
    assert desktop.read_bytes() == before


def test_invalid_tenants_are_refused(desktop: Path, server: PostgresDatabase) -> None:
    with pytest.raises(ValueError):
        import_desktop_database(desktop, server, "Not Valid")
