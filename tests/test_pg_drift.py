"""The PostgreSQL index after the ten drift fixes of MUP-ONA MP-B3 (plan annex A), beyond orkexa's generic cases.

``tests/test_orkexa_conformance.py`` runs the public suite; these cases cover what it cannot reach: Nodestro's own
paths (both ways of ranking vectors, legacy rows written by SQL) and details of a fix. They need
``NODESTRO_TEST_POSTGRES``; each runs in a schema of its own.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from orkexa.retrieval import RetrievalError
from orkexa.testing.conformance import KeywordEmbeddings
import postgres_support
import pytest

TENANT = "drift-tenant"


@pytest.fixture
def database() -> Iterator[Any]:
    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    try:
        opened.migrate()
        yield opened
    finally:
        opened.close()
        postgres_support.drop_schema(schema)


@pytest.fixture(params=["database", "runtime"])
def make(request: pytest.FixtureRequest, database: Any) -> Callable[..., Any]:
    """Opens a ``PostgresIndex`` that ranks vectors as the server can (``database``: pgvector where installed, which
    ``NODESTRO_REQUIRE_PGVECTOR=1`` demands) or always in the runtime (``runtime``, a server without pgvector)."""
    from nodestro_runtime.postgres_tables import PostgresIndex

    if request.param == "database" and not PostgresIndex(database, TENANT, namespace="probe").pgvector():
        postgres_support.require_pgvector()

    def open_index(tenant: str = TENANT, **kwargs: Any) -> PostgresIndex:
        index = PostgresIndex(database, tenant, namespace="drift", **kwargs)
        if request.param == "runtime":
            index._pgvector = False
        return index

    return open_index


# ---- drift 1: the embedding identity is read again ------------------------------------------------------------


# MUP-ONA T-PG-1
def test_a_model_updated_during_a_reindex_discards_the_new_space(make: Callable[..., Any]) -> None:
    old = KeywordEmbeddings("drift-old")
    index = make(embeddings=old)
    for number in range(3):
        index.add(f"Chunk {number} about alpha.", source=f"c{number}.md")
    new = KeywordEmbeddings("drift-new")

    def update_on_the_server(_texts: Any) -> None:
        new.served_digest = "v2"  # the cached identity is still v1

    new.on_embed = update_on_the_server
    with pytest.raises(RetrievalError) as error:
        index.reindex(new, batch_size=1)
    assert error.value.code == "reindex_required"
    status = index.vector_status()
    assert status.active == old.identity().to_dict() and status.building is None
    assert status.embedded == 3
    new.on_embed = None
    status = index.reindex(new)
    assert status.active is not None and status.active["digest"] == "v2" and status.complete
