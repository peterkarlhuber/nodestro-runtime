"""The legacy PostgreSQL fixtures FX-PG-0 and FX-PG-0v (MUP-ONA A0, T-FX-1; see tests/fixtures/legacy/README.md).

The dumps are checked against their committed SHA-256, and FX-PG-0 is restored into an empty database: the migration
runner accepts it (the recorded checksums match the shipped migrations) and the runtime reads both tenants' data.
"""

from __future__ import annotations

from collections.abc import Iterator
import gzip
import hashlib
import json
from pathlib import Path

import postgres_support
import pytest

LEGACY = Path(__file__).resolve().parent / "fixtures" / "legacy"
CHECKSUMS = Path(__file__).resolve().parent / "fixtures" / "migration_checksums.json"
DUMPS = ("FX-PG-0.sql.gz", "FX-PG-0v.sql.gz")
TENANTS = ("tenant-a", "tenant-b")


# MUP-ONA T-FX-1
@pytest.mark.parametrize("name", DUMPS)
def test_dump_matches_its_checksum(name: str) -> None:
    data = (LEGACY / name).read_bytes()
    digest, listed_name = (LEGACY / f"{name}.sha256").read_text(encoding="utf-8").split()
    assert listed_name == name
    assert hashlib.sha256(data).hexdigest() == digest, f"{name} differs from {name}.sha256"
    # gzip -n: no file name (FLG.FNAME) and no time stamp in the header, so compressing the same SQL is reproducible.
    assert data[:2] == b"\x1f\x8b" and not data[3] & 0x08 and data[4:8] == b"\0\0\0\0"
    text = gzip.decompress(data).decode("utf-8")
    assert "PostgreSQL database dump complete" in text


@pytest.fixture
def restored(request: pytest.FixtureRequest) -> Iterator[str]:
    """A new database with ``request.param`` restored into it; dropped afterwards."""
    with postgres_support.restored_database(request.param) as conninfo:
        yield conninfo


# MUP-ONA T-FX-1
@pytest.mark.parametrize("restored", DUMPS, indirect=True)
def test_restored_fixture_passes_the_migration_check_and_keeps_its_data(restored: str) -> None:
    from nodestro_runtime.memory_spaces import MemorySpaces
    from nodestro_runtime.postgres_tables import PostgresIndex, PostgresMemoryTables, knowledge_tables
    from nodestro_runtime.storage_postgres import PostgresDatabase

    listed = json.loads(CHECKSUMS.read_text(encoding="utf-8"))["files"]
    database = PostgresDatabase(restored)
    try:
        recorded = database._query("SELECT version, name, checksum FROM nodestro_schema_migrations ORDER BY version")
        assert [row[0] for row in recorded] == list(range(1, 11))
        for _version, name, checksum in recorded:
            assert checksum == listed[name], name
        # The runner's own check: migrating applies only migrations newer than 0010 (none on the baseline) and
        # refuses a database whose recorded checksums differ (storage.migration_changed).
        assert database.migrate(before_migration=lambda current, target: None) == 10
        database.require_current_schema()

        for tenant in TENANTS:
            tables = knowledge_tables(database, tenant)
            index = tables.index
            assert isinstance(index, PostgresIndex)
            names = sorted(entry.name for entry in tables.store.list())
            marker = "marker-a.txt" if tenant == "tenant-a" else "marker-b.txt"
            assert names == sorted(
                ["faq.md", "glossary.txt", "handbook.md", "late-note.txt", marker, "zero-vector.txt"]
            )
            status = index.vector_status()
            assert status.active is not None and status.active["model"] == "fx-embed-a"
            assert status.building is not None and status.building["model"] == "fx-embed-b"
            assert status.embedded == status.chunks
            assert 0 < status.building_embedded < status.chunks
            assert {hit.title for hit in index.search("heron", k=10)} == {"handbook.md", "late-note.txt"}
            own, other = ("aardvark", "zebra") if tenant == "tenant-a" else ("zebra", "aardvark")
            assert [hit.title for hit in index.search(own)] == [marker]
            assert index.search(other) == []
            memory = MemorySpaces(PostgresMemoryTables(database, tenant)).open("fixture-notes")
            assert "The heron is blue." in memory.view("/memories/notes.md")
            assert f"Only {tenant}" in memory.view(f"/memories/{tenant}.md")
        # The same file ids in both tenants (content-addressed), and one zero vector per tenant.
        ids = database._query(
            "SELECT tenant_id, array_agg(id ORDER BY id) FROM nodestro_files WHERE id IN"
            " (SELECT id FROM nodestro_files GROUP BY id HAVING count(*) = 2) GROUP BY tenant_id ORDER BY tenant_id"
        )
        assert [row[0] for row in ids] == list(TENANTS) and ids[0][1] == ids[1][1] and len(ids[0][1]) == 5
        zero = database._query(
            "SELECT tenant_id, count(*) FROM nodestro_chunk_vectors"
            " WHERE embedding = array_fill(0::real, ARRAY[cardinality(embedding)]) GROUP BY tenant_id ORDER BY 1"
        )
        assert zero == [("tenant-a", 1), ("tenant-b", 1)]
    finally:
        database.close()
