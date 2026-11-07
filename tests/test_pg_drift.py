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


# ---- drift 2: NaN, infinity and wrong lengths ------------------------------------------------------------------


def _set_vector(index: Any, source: str, values: str) -> None:
    """Write a vector by SQL, as older code could store it (``values``: a ``real[]`` literal)."""
    postgres_support.owner_of(index.database)._write(
        "UPDATE nodestro_chunk_vectors SET embedding = %s::real[] WHERE tenant_id = %s AND chunk_id IN"
        " (SELECT id FROM nodestro_chunks WHERE tenant_id = %s AND source = %s)",
        (values, index.tenant_id, index.tenant_id, source),
    )


# MUP-ONA T-PG-1
def test_stored_non_finite_vectors_are_left_out_and_kept(make: Callable[..., Any]) -> None:
    index = make(embeddings=KeywordEmbeddings())
    for source, word in (("a.md", "alpha"), ("nan.md", "alpha beta"), ("inf.md", "alpha gamma")):
        index.add(f"A chunk about {word}.", source=source)
    _set_vector(index, "nan.md", "{NaN,1,0,0,0,0,0,0}")
    _set_vector(index, "inf.md", "{-Infinity,1,0,0,0,0,0,0}")
    for mode in ("vector", "hybrid"):
        hits = index.retrieve("alpha", 10, mode=mode)
        assert hits and hits[0].source == "a.md", (mode, hits)
        found = {hit.source for hit in hits if "vector" in hit.ranks}
        assert found == {"a.md"}, (mode, hits)
    stored = postgres_support.owner_of(index.database)._query(
        "SELECT c.source, v.embedding[1]::text FROM nodestro_chunk_vectors v"
        " JOIN nodestro_chunks c ON c.id = v.chunk_id"
        " WHERE v.tenant_id = %s AND c.tenant_id = %s AND c.source <> 'a.md' ORDER BY c.source",
        (index.tenant_id, index.tenant_id),
    )
    assert stored == [("inf.md", "-Infinity"), ("nan.md", "NaN")]  # left out, not rewritten
    rows = postgres_support.owner_of(index.database)._query(
        "SELECT count(*) FROM nodestro_chunk_vectors WHERE tenant_id = %s", (index.tenant_id,)
    )
    assert rows == [(3,)]


# ---- drift 3: zero vectors -------------------------------------------------------------------------------------


# MUP-ONA T-PG-1
def test_zero_vectors_are_stored_but_never_ranked(make: Callable[..., Any]) -> None:
    index = make(embeddings=KeywordEmbeddings())
    index.add("A chunk about alpha.", source="a.md")
    index.add("The zerovector chunk about alpha gardens.", source="zero.md")
    status = index.vector_status()
    assert (status.chunks, status.embedded) == (2, 2)  # stored: the chunk is not missing a vector
    for mode in ("vector", "hybrid"):
        hits = index.retrieve("alpha", 10, mode=mode)
        assert [hit.source for hit in hits if "vector" in hit.ranks] == ["a.md"], (mode, hits)
    assert index.retrieve("zerovector", 10, mode="vector") == []  # a zero query vector finds nothing
    hybrid = index.retrieve("zerovector gardens", 10, mode="hybrid")
    assert [hit.source for hit in hybrid] == ["zero.md"] and hybrid[0].ranks == {"fts": 1}
    assert all(hit.score == hit.score for hit in hybrid)  # never NaN


# ---- drift 4: the cosine in both paths -------------------------------------------------------------------------


# MUP-ONA T-PG-1
@pytest.mark.parametrize("normalized", [False, True])
def test_both_paths_score_the_cosine(make: Callable[..., Any], normalized: bool) -> None:
    from orkexa.testing.conformance._models import cosine

    model = KeywordEmbeddings("drift-cosine", normalized=normalized)
    index = make(embeddings=model)
    texts = {"loud.md": "A loudvector chunk about alpha.", "two.md": "Alpha and beta.", "three.md": "Alpha beta gamma."}
    for source, text in texts.items():
        index.add(text, source=source)
    hits = index.retrieve("alpha", 3, mode="vector")
    assert [hit.source for hit in hits] == ["loud.md", "two.md", "three.md"]
    for hit in hits:
        assert abs(hit.score - cosine(model.vector("alpha"), model.vector(texts[hit.source]))) <= 1e-6, hit


# ---- drift 5: the contract's scores --------------------------------------------------------------------------


# MUP-ONA T-PG-1
def test_scores_follow_the_contract_in_every_mode(make: Callable[..., Any]) -> None:
    from orkexa.retrieval import fuse_rrf

    index = make(embeddings=KeywordEmbeddings(), fusion="rrf")
    for number, words in enumerate(("alpha", "alpha beta", "alpha beta gamma", "beta gamma delta")):
        index.add(f"Chunk {number}: {words} policy.", source=f"c{number}.md")
    fts = index.retrieve("policy alpha", 4, mode="fts")
    assert [hit.score for hit in fts] == [1 / (60 + rank) for rank in range(1, len(fts) + 1)]
    vector = index.retrieve("alpha beta", 4, mode="vector")
    assert all(hit.score == round(hit.score, 6) for hit in vector)
    hybrid = index.retrieve("alpha beta", 4, mode="hybrid")
    rankings = [
        [hit.id for hit in index.retrieve("alpha beta", 20, mode="fts")],
        [hit.id for hit in index.retrieve("alpha beta", 20, mode="vector")],
    ]
    assert [(hit.id, hit.score) for hit in hybrid] == fuse_rrf(rankings)[:4]


# ---- drift 6: the default mode --------------------------------------------------------------------------------


# MUP-ONA T-PG-1
def test_the_default_mode_follows_the_embedding_model(make: Callable[..., Any]) -> None:
    index = make(embeddings=KeywordEmbeddings())
    index.add("A chunk about alpha.", source="a.md")
    index.add("Another chunk about the first topic.", source="b.md")
    assert {name for hit in index.search("alpha", 2) for name in hit.ranks} == {"fts", "vector"}
    assert index.search("alpha", 2) == index.retrieve("alpha", 2, mode="hybrid")
    plain = make(tenant="drift-plain")
    plain.add("A chunk about alpha.", source="a.md")
    assert [hit.ranks for hit in plain.search("alpha")] == [{"fts": 1}]
    assert make(embeddings=KeywordEmbeddings(), mode="fts").search("alpha") == index.retrieve("alpha", 5, mode="fts")


# ---- drift 7: the source limit ---------------------------------------------------------------------------------


# MUP-ONA T-PG-1
def test_a_search_names_at_most_50000_sources(make: Callable[..., Any]) -> None:
    from nodestro_runtime.postgres_tables import MAX_SOURCES

    index = make(embeddings=KeywordEmbeddings())
    index.add("A chunk about alpha.", source="a.md")
    many = [f"s{number}.md" for number in range(MAX_SOURCES)]
    for mode in ("fts", "vector", "hybrid"):
        with pytest.raises(ValueError):
            index.retrieve("alpha", 3, mode=mode, sources=[*many, "a.md"])
        hits = index.retrieve("alpha", 3, mode=mode, sources=[*many[:-1], "a.md"])
        assert [hit.source for hit in hits] == ["a.md"], mode
