"""Knowledge files, search and memory spaces behave the same on SQLite (desktop) and PostgreSQL (server).

The PostgreSQL cases need ``NODESTRO_TEST_POSTGRES`` (see ``postgres_support``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from orkexa import Chunk, MemoryToolError
import postgres_support
import pytest

from nodestro_runtime.knowledge import KnowledgeBase
from nodestro_runtime.memory_spaces import MemorySpaces


@dataclass
class Backends:
    knowledge: Callable[[str], KnowledgeBase]
    """A knowledge base of a tenant (SQLite: one database, the tenant is ignored)."""
    memory: Callable[[str], MemorySpaces]
    kind: str


@pytest.fixture(params=["sqlite", "postgres"])
def backends(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backends]:
    opened: list[KnowledgeBase | MemorySpaces] = []

    def keep(item: KnowledgeBase | MemorySpaces) -> KnowledgeBase | MemorySpaces:
        opened.append(item)
        return item

    if request.param == "sqlite":
        database = tmp_path / "nodestro.db"
        yield Backends(
            knowledge=lambda _tenant: keep(KnowledgeBase(database)),  # type: ignore[return-value]
            memory=lambda _tenant: keep(MemorySpaces(database)),  # type: ignore[return-value]
            kind="sqlite",
        )
        for item in opened:
            item.close()
        return

    from nodestro_runtime.postgres_tables import PostgresMemoryTables, knowledge_tables
    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    pg = PostgresDatabase(dsn, schema=schema)
    try:
        pg.migrate()
        yield Backends(
            knowledge=lambda tenant: keep(KnowledgeBase(knowledge_tables(pg, tenant))),  # type: ignore[return-value]
            memory=lambda tenant: keep(MemorySpaces(PostgresMemoryTables(pg, tenant))),  # type: ignore[return-value]
            kind="postgres",
        )
        for item in opened:
            item.close()
    finally:
        pg.close()
        postgres_support.drop_schema(schema)


# ---- knowledge ----------------------------------------------------------------------------------


def test_files_are_stored_indexed_and_found(backends: Backends) -> None:
    knowledge = backends.knowledge("alpha")
    entry = knowledge.add(b"# Vertrag\n\nDie Haftung ist auf 10.000 EUR begrenzt.", name="vertrag.md")
    image = knowledge.add(b"\x89PNG\r\n\x1a\nnot really", name="bild.png", description="A picture")
    assert entry.searchable and entry.chunks == 1
    assert not image.searchable
    assert {item.id for item in knowledge.files()} == {entry.id, image.id}
    assert knowledge.store.get_bytes(image.id) == b"\x89PNG\r\n\x1a\nnot really"
    assert knowledge.text_of(entry.id).startswith("# Vertrag")

    hits = knowledge.search("haftung")
    assert [hit.source for hit in hits] == [entry.id]
    assert hits[0].file_id == entry.id
    assert knowledge.search("Haft")[0].source == entry.id  # word prefixes match
    assert knowledge.search("vertrag begrenzt nichts")[0].source == entry.id  # any word matches
    assert knowledge.search("Überweisung") == []
    assert knowledge.search("  ...  ") == []


def test_search_ignores_case_and_diacritics(backends: Backends) -> None:
    knowledge = backends.knowledge("alpha")
    entry = knowledge.add("Café Müller, Ångström und Straße".encode(), name="notiz.txt")
    for query in ("cafe", "MULLER", "angstrom", "straße", "Müll"):
        assert [hit.source for hit in knowledge.search(query)] == [entry.id], query


def test_better_matches_rank_first(backends: Backends) -> None:
    knowledge = backends.knowledge("alpha")
    weak = knowledge.add(b"apple banana cherry date elderberry fig grape", name="weak.txt")
    strong = knowledge.add(b"invoice refund invoice refund", name="strong.txt")
    knowledge.add(b"nothing relevant here", name="other.txt")
    hits = knowledge.search("refund invoice apple")
    assert [hit.source for hit in hits][:2] == [strong.id, weak.id]
    assert hits[0].score > hits[1].score


def test_updates_and_deletes_reach_the_index(backends: Backends) -> None:
    knowledge = backends.knowledge("alpha")
    entry = knowledge.add(b"Zebra crossing", name="a.txt")
    knowledge.update(entry.id, description="About zebras", collection="Animals")
    assert knowledge.get(entry.id).description == "About zebras"
    assert [info.name for info in knowledge.collections()] == ["Animals"]
    knowledge.delete(entry.id)
    assert knowledge.search("zebra") == []
    assert knowledge.files() == []


def test_scoped_views_show_only_assigned_files(backends: Backends) -> None:
    from nodestro_runtime.schemas import KnowledgeConfig

    knowledge = backends.knowledge("alpha")
    mine = knowledge.add(b"shared word mine", name="mine.txt")
    knowledge.add(b"shared word theirs", name="theirs.txt")
    assert [hit.source for hit in knowledge.search("shared", file_ids=[mine.id])] == [mine.id]
    _store, index = knowledge.views(KnowledgeConfig.model_validate({"mode": "search", "file_ids": [mine.id]}))
    assert index is not None and [hit.source for hit in index.search("shared")] == [mine.id]


def test_chunks_without_text_are_skipped(backends: Backends) -> None:
    knowledge = backends.knowledge("alpha")
    assert knowledge.index.add_chunks([Chunk(text="   ", source="s")]) == []
    ids = knowledge.index.add_chunks([Chunk(text="nul\x00inside", source="s", metadata={"k": "v"})])
    assert len(ids) == 1
    assert knowledge.search("inside")[0].metadata == {"k": "v"}


def test_tenants_have_separate_knowledge(backends: Backends) -> None:
    if backends.kind == "sqlite":
        pytest.skip("the desktop has one tenant")
    alpha, beta = backends.knowledge("alpha"), backends.knowledge("beta")
    entry = alpha.add(b"Geheimnis von alpha", name="geheim.txt")
    assert beta.files() == []
    assert beta.search("geheimnis") == []
    with pytest.raises(KeyError):
        beta.store.get_bytes(entry.id)
    beta.add(b"Geheimnis von alpha", name="kopie.txt")  # same bytes, same id, other tenant
    beta.delete(entry.id)
    assert [item.name for item in alpha.files()] == ["geheim.txt"]
    assert [hit.source for hit in alpha.search("geheimnis")] == [entry.id]


# ---- memory -------------------------------------------------------------------------------------


def test_memory_commands_work(backends: Backends) -> None:
    space = backends.memory("alpha").open("Team")
    assert "created" in space.execute({"command": "create", "path": "/memories/a/notes.md", "file_text": "one\n"})
    space.execute({"command": "str_replace", "path": "/memories/a/notes.md", "old_str": "one", "new_str": "two"})
    space.execute({"command": "insert", "path": "/memories/a/notes.md", "insert_line": 1, "insert_text": "three"})
    assert space.read("/memories/a/notes.md") == "two\nthree\n"
    space.execute({"command": "rename", "old_path": "/memories/a", "new_path": "/memories/b"})
    assert [entry.path for entry in space.entries()] == ["/memories/b/notes.md"]
    assert "notes.md" in space.execute({"command": "view", "path": "/memories"})
    space.write("/memories/c.md", "Grüße")
    assert {entry.path: entry.size for entry in space.entries()}["/memories/c.md"] == len("Grüße".encode())
    space.remove("/memories/b")
    assert [entry.path for entry in space.entries()] == ["/memories/c.md"]
    with pytest.raises(KeyError):
        space.read("/memories/b/notes.md")


def test_memory_spaces_are_listed_and_cleared(backends: Backends) -> None:
    spaces = backends.memory("alpha")
    spaces.open("Team").write("/memories/x.md", "abc")
    spaces.open("Team").write("/memories/y.md", "de")
    spaces.open("Other").write("/memories/z.md", "f")
    assert [(info.name, info.files, info.size) for info in spaces.all()] == [("Other", 1, 1), ("Team", 2, 5)]
    spaces.open("Team").clear()
    assert [(info.name, info.files) for info in spaces.all()] == [("Other", 1)]


def test_a_failing_memory_command_changes_nothing(backends: Backends) -> None:
    space = backends.memory("alpha").open("Team")
    space.write("/memories/a.md", "keep")
    with pytest.raises(MemoryToolError):
        space.execute({"command": "rename", "old_path": "/memories/a.md", "new_path": "/memories/a.md/x"})
    assert space.read("/memories/a.md") == "keep"


def test_tenants_have_separate_memory(backends: Backends) -> None:
    if backends.kind == "sqlite":
        pytest.skip("the desktop has one tenant")
    alpha, beta = backends.memory("alpha"), backends.memory("beta")
    alpha.open("Team").write("/memories/secret.md", "alpha only")
    assert beta.all() == []
    assert beta.open("Team").entries() == []
    beta.open("Team").clear()
    assert alpha.open("Team").read("/memories/secret.md") == "alpha only"


def test_postgres_memory_refuses_nul_characters(backends: Backends) -> None:
    if backends.kind == "sqlite":
        pytest.skip("SQLite stores any character")
    space = backends.memory("alpha").open("Team")
    with pytest.raises(MemoryToolError):
        space.execute({"command": "create", "path": "/memories/a.md", "file_text": "x\x00y"})
    assert space.entries() == []
