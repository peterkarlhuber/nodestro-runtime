"""Write the legacy PostgreSQL fixtures FX-PG-0 and FX-PG-0v (MUP-ONA A0, plan section 5.2).

A legacy fixture is a plain ``pg_dump`` of a database that this release of nodestro-runtime wrote, so later releases
can be tested against data they did not write themselves (upgrade, rollback, schema checks). The database holds:

* migrations 0001-0010, applied by :meth:`PostgresDatabase.migrate`;
* two tenants, ``tenant-a`` and ``tenant-b``, with the same knowledge namespace, the same files (so the same file
  ids and sources), the same chunk texts and the same memory paths; each tenant also has one marker file and one
  marker memory file of its own;
* chunk vectors in two spaces per tenant: an ``active`` space of the embedding model ``fx-embed-a`` (8 dimensions)
  and a ``building`` space of ``fx-embed-b`` (6 dimensions) from a reindex that was cancelled after its first batch;
* one zero vector per tenant in the active space (finite, never NaN).

Everything is written through the public runtime API (:class:`KnowledgeBase`, :class:`PostgresIndex`,
:class:`MemorySpaces`), except the zero vector: the embedding models of a running server never return one, so it
is set with one ``UPDATE`` to stand for data older code may have stored. All data is synthetic.

Usage (the server runs in a container; ``pg_dump`` runs inside it, so its version matches the server)::

    docker run -d --name fx-pg17 -e POSTGRES_PASSWORD=fx -p 55441:5432 postgres:17@sha256:...
    python tests/fixtures/legacy/make_legacy_pg.py --container fx-pg17 \\
        --dsn "host=127.0.0.1 port=55441 user=postgres password=fx dbname=postgres" \\
        --out tests/fixtures/legacy/FX-PG-0.sql.gz

The dump is ``pg_dump --format=plain --no-owner --no-privileges`` compressed with ``gzip -n -9`` (no name, no time
stamp); a ``.sha256`` file in ``sha256sum`` format is written beside it. Regenerating gives other bytes (creation
times of files and memory entries), so a committed fixture is never regenerated without a reason in the PR.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import threading

from orkexa.embeddings import CallableEmbeddings, EmbeddingModel
from orkexa.retrieval import RetrievalError
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from nodestro_runtime.knowledge import KnowledgeBase
from nodestro_runtime.memory_spaces import MemorySpaces
from nodestro_runtime.postgres_tables import PostgresIndex, PostgresMemoryTables, knowledge_tables
from nodestro_runtime.storage_postgres import PostgresDatabase

TENANTS = ("tenant-a", "tenant-b")
#: Files every tenant has (same bytes, so the same file ids and chunk sources in both tenants).
SHARED_FILES = {
    "handbook.md": (
        "# Fixture handbook\n\nThe blue heron nests near the river. Orders ship within two days.\n\n"
        "## Returns\n\nA return label is valid for thirty days. Refunds go to the original payment method.\n"
    ),
    "glossary.txt": "Alpha means the first release. Beta means the second release. Gamma is the third.\n",
    "faq.md": "# FAQ\n\nHow long is shipping? Two days.\n\nWho answers support mail? The fixture team.\n",
}
#: Added after the active space exists (its chunks get vectors on upload, not from a reindex).
LATE_FILE = ("late-note.txt", "A late note about the heron and the river, indexed with vectors on upload.\n")
#: Its vector becomes the zero vector.
ZERO_FILE = ("zero-vector.txt", "This chunk carries a zero vector written by SQL.\n")
#: One file per tenant that only this tenant has (a marker for isolation checks).
MARKERS = {
    "tenant-a": ("marker-a.txt", "Marker of tenant-a: aardvark amethyst.\n"),
    "tenant-b": ("marker-b.txt", "Marker of tenant-b: zebra zircon.\n"),
}
MEMORY_SPACE = "fixture-notes"


def _vector(text: str, dimensions: int, salt: str) -> list[float]:
    digest = hashlib.sha256(f"{salt}:{text}".encode()).digest()
    return [round((digest[i] - 127.5) / 127.5, 4) for i in range(dimensions)]


def embedding_models() -> dict[str, EmbeddingModel]:
    """Deterministic fake embedding models (vectors from SHA-256 of the text)."""
    return {
        "fx-embed-a": CallableEmbeddings(
            lambda texts: [_vector(t, 8, "a") for t in texts], name="fx-embed-a", dimensions=8, provider="fixture"
        ),
        "fx-embed-b": CallableEmbeddings(
            lambda texts: [_vector(t, 6, "b") for t in texts], name="fx-embed-b", dimensions=6, provider="fixture"
        ),
    }


def populate(database: PostgresDatabase, tenant: str) -> None:
    models = embedding_models()
    tables = knowledge_tables(database, tenant)
    index = tables.index
    assert isinstance(index, PostgresIndex)
    knowledge = KnowledgeBase(tables)
    knowledge.embedding_model = models.__getitem__
    knowledge.retrieval = lambda: ("hybrid", "fx-embed-a")
    for name, text in SHARED_FILES.items():
        knowledge.add(text.encode(), name=name, description=f"fixture file {name}", collection="fixture")
    marker_name, marker_text = MARKERS[tenant]
    knowledge.add(marker_text.encode(), name=marker_name, description="tenant marker")
    # The active space: every chunk so far gets a vector of fx-embed-a.
    index.reindex(models["fx-embed-a"])
    for name, text in (LATE_FILE, ZERO_FILE):
        knowledge.add(text.encode(), name=name, description="added with vectors")
    # A building space: a reindex to fx-embed-b, cancelled after its first batch of two chunks.
    cancel = threading.Event()
    try:
        index.reindex(models["fx-embed-b"], batch_size=2, progress=lambda done, total: cancel.set(), cancel=cancel)
    except RetrievalError as exc:
        if exc.code != "cancelled":
            raise
    else:
        raise RuntimeError("the fixture reindex was expected to be cancelled")
    status = index.vector_status()
    assert status.active and status.active["model"] == "fx-embed-a", status
    assert status.building and status.building["model"] == "fx-embed-b", status
    assert status.embedded == status.chunks and 0 < status.building_embedded < status.chunks, status
    # The zero vector (the API never writes one: see the module docstring).
    zero_id = hashlib.sha256(ZERO_FILE[1].encode()).hexdigest()[:16]
    updated = database._write(
        "UPDATE nodestro_chunk_vectors v SET embedding = array_fill(0::real, ARRAY[8]) FROM nodestro_chunks c"
        " WHERE c.id = v.chunk_id AND c.tenant_id = %s AND c.source = %s AND v.tenant_id = %s"
        " AND v.space = %s",
        (tenant, zero_id, tenant, f"nodestro:{models['fx-embed-a'].identity().key}"),
    )
    assert updated == 1, updated
    spaces = MemorySpaces(PostgresMemoryTables(database, tenant))
    memory = spaces.open(MEMORY_SPACE)
    memory.create("/memories/notes.md", "Shared fixture note.\nThe heron is blue.\n")
    memory.create("/memories/projects/plan.md", "Plan of the fixture project.\n")
    memory.create(f"/memories/{tenant}.md", f"Only {tenant} has this memory file.\n")
    spaces.close()
    knowledge.close()


def _database_dsn(dsn: str, name: str) -> str:
    parts = conninfo_to_dict(dsn)
    parts["dbname"] = name
    return make_conninfo("", **parts)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dsn", required=True, help="libpq string of a superuser connection to the server")
    parser.add_argument("--container", required=True, help="the server's Docker container (pg_dump runs there)")
    parser.add_argument("--database", default="nodestro_legacy_fixture", help="database to (re)create and dump")
    parser.add_argument("--out", required=True, type=Path, help="the .sql.gz file to write")
    args = parser.parse_args(argv)

    with psycopg.connect(args.dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(args.database)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(args.database)))
    database = PostgresDatabase(_database_dsn(args.dsn, args.database))
    try:
        assert database.migrate() == 0
        assert database.schema_version == 10, database.schema_version
        for tenant in TENANTS:
            populate(database, tenant)
        database.require_current_schema()
    finally:
        database.close()

    user = conninfo_to_dict(args.dsn).get("user", "postgres")
    dump = subprocess.run(
        [
            "docker",
            "exec",
            args.container,
            "pg_dump",
            "-U",
            str(user),
            "--format=plain",
            "--no-owner",
            "--no-privileges",
            "-d",
            args.database,
        ],
        check=True,
        capture_output=True,
    ).stdout
    compressed = subprocess.run(["gzip", "-n", "-9", "-c"], input=dump, check=True, capture_output=True).stdout
    args.out.write_bytes(compressed)
    digest = hashlib.sha256(compressed).hexdigest()
    Path(f"{args.out}.sha256").write_text(f"{digest}  {args.out.name}\n", encoding="utf-8")
    print(f"{args.out}: {len(dump)} bytes of SQL, {len(compressed)} compressed, sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
