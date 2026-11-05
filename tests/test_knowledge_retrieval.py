"""Knowledge retrieval modes (fts, vector, hybrid), the semantic index and its reindex, on SQLite and PostgreSQL.

An agent's view searches only its files, limited before ranking in every mode. The PostgreSQL cases need
``NODESTRO_TEST_POSTGRES``; they run with pgvector where it is installed and with the runtime's own ranking too.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import math
from pathlib import Path
import time
from typing import Any

from orkexa.embeddings import EmbeddingIdentity, EmbeddingModel
from orkexa.local import OllamaError
import postgres_support
import pytest

from nodestro_runtime.knowledge import KnowledgeBase, ScopedIndex
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import KnowledgeConfig

CONCEPTS = [
    {"money", "refund", "back", "reimbursed", "return"},
    {"guarantee", "warranty", "defect", "broken"},
    {"parcel", "parcels", "shipping", "delivery", "warehouse"},
    {"vacation", "holiday", "leave", "employees"},
]


class ConceptEmbeddings(EmbeddingModel):
    """Deterministic vectors: one dimension per concept (texts that share no word can still be close)."""

    def __init__(self, name: str = "concepts:1", digest: str = "d1") -> None:
        self.name = name
        self.digest = digest
        self.down = False
        self.calls = 0

    def _vector(self, text: str) -> list[float]:
        words = {word.strip(".,?!").lower() for word in text.split()}
        vector = [float(len(words & concept)) for concept in CONCEPTS] + [0.01]
        norm = math.sqrt(sum(x * x for x in vector))
        return [x / norm for x in vector]

    def embed_documents(self, texts):  # type: ignore[no-untyped-def]
        if self.down:
            raise OllamaError("unreachable", "The model server does not answer")
        self.calls += 1
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        if self.down:
            raise OllamaError("unreachable", "The model server does not answer")
        return self._vector(text)

    def identity(self) -> EmbeddingIdentity:
        if self.down:
            raise OllamaError("unreachable", "The model server does not answer")
        return EmbeddingIdentity(provider="test", model=self.name, dimensions=len(CONCEPTS) + 1, digest=self.digest)


DOCS = {
    "returns.md": "Customers get their money back within 30 days.",
    "warranty.md": "The guarantee covers every defect for two years.",
    "shipping.md": "Parcels leave the warehouse within three days.",
    "hr.md": "Employees have 30 days of vacation per year.",
}


@pytest.fixture(params=["sqlite", "postgres-pgvector", "postgres-runtime"])
def make(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Callable[[str], KnowledgeBase]]:
    opened: list[KnowledgeBase] = []

    def configure(knowledge: KnowledgeBase, models: dict[str, ConceptEmbeddings]) -> KnowledgeBase:
        settings = {"mode": "fts", "model": "concepts:1"}
        knowledge.retrieval = lambda: (settings["mode"], settings["model"])  # type: ignore[assignment,return-value]
        knowledge.embedding_model = lambda name: models.setdefault(name, ConceptEmbeddings(name))
        knowledge.settings = settings  # type: ignore[attr-defined]
        knowledge.models = models  # type: ignore[attr-defined]
        opened.append(knowledge)
        return knowledge

    if request.param == "sqlite":
        database = tmp_path / "nodestro.db"
        yield lambda _tenant: configure(KnowledgeBase(database), {})
        for item in opened:
            item.close()
        return
    from nodestro_runtime.postgres_tables import knowledge_tables
    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    pg = PostgresDatabase(dsn, schema=schema)
    try:
        pg.migrate()

        def open_tenant(tenant: str) -> KnowledgeBase:
            knowledge = configure(KnowledgeBase(knowledge_tables(pg, tenant)), {})
            if request.param == "postgres-runtime":
                knowledge.index._pgvector = False  # type: ignore[attr-defined]
            elif not knowledge.index.pgvector():  # type: ignore[attr-defined]
                pytest.skip("pgvector is not installed in the test database")
            return knowledge

        yield open_tenant
        for item in opened:
            item.close()
    finally:
        pg.close()
        postgres_support.drop_schema(schema)


def fill(knowledge: KnowledgeBase) -> dict[str, str]:
    return {name: knowledge.add(text.encode(), name=name).id for name, text in DOCS.items()}


def wait(knowledge: KnowledgeBase) -> None:
    deadline = time.monotonic() + 20
    while knowledge.index_status().job is not None and knowledge.index_status().job.state == "running":  # type: ignore[union-attr]
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_full_text_stays_the_default_and_vectors_need_the_index(make: Callable[[str], KnowledgeBase]) -> None:
    knowledge = make("alpha")
    ids = fill(knowledge)
    assert knowledge.index_status().state == "off"
    assert [hit.source for hit in knowledge.search("vacation", k=1)] == [ids["hr.md"]]
    with pytest.raises(NodestroError) as error:
        knowledge.search("vacation", mode="vector")
    assert error.value.code == "knowledge.vectors_missing"  # never a silent fallback to full text


def test_the_semantic_index_finds_meaning_and_scopes_before_ranking(make: Callable[[str], KnowledgeBase]) -> None:
    knowledge = make("alpha")
    ids = fill(knowledge)
    status = knowledge.start_reindex()
    wait(knowledge)
    status = knowledge.index_status()
    assert status.state == "ready" and status.embedded == status.chunks == 4
    assert status.active is not None and status.active["model"] == "concepts:1"
    # No shared word with "reimbursed broken": only vectors find these.
    assert knowledge.search("reimbursed", mode="fts") == []
    assert [hit.source for hit in knowledge.search("reimbursed", mode="vector", k=1)] == [ids["returns.md"]]
    hybrid = knowledge.search("guarantee broken", mode="hybrid", k=2)
    assert hybrid[0].source == ids["warranty.md"] and set(hybrid[0].ranks) == {"fts", "vector"}
    # An agent with only the HR file never sees the better matching returns file, in any mode.
    view = ScopedIndex(knowledge.index, [ids["hr.md"]], "vector")
    assert [hit.source for hit in view.search("money back", 3)] == [ids["hr.md"]]
    for mode in ("fts", "hybrid"):
        hits = knowledge.search("30 days", [ids["hr.md"]], 3, mode=mode)  # type: ignore[arg-type]
        assert {hit.source for hit in hits} == {ids["hr.md"]}
    # New files get vectors right away.
    added = knowledge.add(b"Delivery of the parcel takes a week.", name="delivery.md")
    assert knowledge.index_status().state == "ready"
    assert ids["shipping.md"] in {hit.source for hit in knowledge.search("warehouse", mode="vector", k=2)}
    assert added.searchable


def test_a_weak_full_text_hit_no_longer_outranks_the_vector_hit(make: Callable[[str], KnowledgeBase]) -> None:
    """The offline proof's failure, on SQLite and PostgreSQL: the query shares one word ("days") with a document of
    its own language, and nothing with the German document vectors rank first."""
    knowledge = make("alpha")
    hr = knowledge.add(b"Jeder hat 30 Arbeitstage holiday pro Kalenderjahr.", name="urlaub.md").id
    security = knowledge.add(b"Passwords are rotated every 180 days.", name="security.md").id
    knowledge.add(b"Pakete verlassen das Lager in drei Werktagen.", name="versand.md")
    knowledge.start_reindex()
    wait(knowledge)
    query = "How many vacation days do employees get?"
    assert [hit.source for hit in knowledge.search(query, mode="fts", k=1)] == [security]
    assert [hit.source for hit in knowledge.search(query, mode="vector", k=1)] == [hr]
    hits = knowledge.search(query, mode="hybrid", k=3)
    assert [hit.source for hit in hits][:2] == [hr, security]
    assert hits[0].ranks == {"vector": 1} and hits[1].ranks == {"fts": 1, "vector": 2}
    assert all(0.0 <= hit.score <= 1.0 for hit in hits)
    knowledge.index.fusion = "rrf"  # type: ignore[attr-defined]
    assert knowledge.search(query, mode="hybrid", k=1)[0].source == security  # the earlier fusion


def test_hybrid_keeps_exact_codes_that_vectors_cannot_tell_apart(make: Callable[[str], KnowledgeBase]) -> None:
    knowledge = make("alpha")
    ids = {
        code: knowledge.add(f"Delivery of valve KH-{code} within three days.".encode(), name=f"kh{code}.md").id
        for code in ("2201", "2102", "2210", "2120")
    }
    knowledge.start_reindex()
    wait(knowledge)
    assert knowledge.search("KH-2210 delivery", mode="fts", k=1)[0].source == ids["2210"]
    assert knowledge.search("KH-2210 delivery", mode="hybrid", k=1)[0].source == ids["2210"]
    scoped = knowledge.search("KH-2210 delivery", [ids["2201"], ids["2120"]], 4, mode="hybrid")
    assert {hit.source for hit in scoped} == {ids["2201"], ids["2120"]}  # the scope still limits before ranking
    assert knowledge.search("KH-2120", [ids["2201"], ids["2120"]], 1, mode="hybrid")[0].source == ids["2120"]


def test_an_agent_view_follows_its_retrieval_mode(make: Callable[[str], KnowledgeBase]) -> None:
    knowledge = make("alpha")
    ids = fill(knowledge)
    config = KnowledgeConfig(file_ids=[ids["returns.md"]], mode="search", retrieval="vector")
    with pytest.raises(NodestroError):
        knowledge.views(config)  # vector search without a semantic index is refused before the run
    knowledge.start_reindex()
    wait(knowledge)
    _, index = knowledge.views(config)
    assert index is not None and index.mode == "vector"
    assert [hit.source for hit in index.search("reimbursed", 2)] == [ids["returns.md"]]
    knowledge.settings["mode"] = "hybrid"  # type: ignore[attr-defined]
    _, default = knowledge.views(KnowledgeConfig(file_ids=[ids["hr.md"]], mode="search"))
    assert default is not None and default.mode == "hybrid"


def test_another_embedding_model_needs_a_reindex(make: Callable[[str], KnowledgeBase]) -> None:
    knowledge = make("alpha")
    ids = fill(knowledge)
    knowledge.start_reindex()
    wait(knowledge)
    knowledge.settings["model"] = "concepts:2"  # type: ignore[attr-defined]
    assert knowledge.index_status().state == "reindex_required"
    # Until the switch, searches keep using the complete vectors of the first model.
    assert [hit.source for hit in knowledge.search("reimbursed", mode="vector", k=1)] == [ids["returns.md"]]
    knowledge.start_reindex()
    wait(knowledge)
    status = knowledge.index_status()
    assert status.state == "ready" and status.active["model"] == "concepts:2"  # type: ignore[index]
    assert status.building is None


def test_uploads_work_while_the_model_server_is_down(make: Callable[[str], KnowledgeBase]) -> None:
    knowledge = make("alpha")
    fill(knowledge)
    knowledge.start_reindex()
    wait(knowledge)
    model = knowledge.models["concepts:1"]  # type: ignore[attr-defined]
    model.down = True
    entry = knowledge.add(b"Holiday rules for employees.", name="holiday.md")
    assert entry.searchable  # indexed for full text, its vectors follow with the next reindex
    status = knowledge.index_status()
    assert status.state == "incomplete" and status.embedded == status.chunks - 1
    model.down = False
    knowledge.start_reindex()
    wait(knowledge)
    assert knowledge.index_status().state == "ready"


def test_a_failed_or_cancelled_reindex_keeps_the_current_vectors(make: Callable[[str], KnowledgeBase]) -> None:
    knowledge = make("alpha")
    ids = fill(knowledge)
    knowledge.start_reindex()
    wait(knowledge)
    knowledge.settings["model"] = "concepts:2"  # type: ignore[attr-defined]
    broken = knowledge.embedding_model("concepts:2")  # type: ignore[misc]
    broken.down = True  # type: ignore[attr-defined]
    knowledge.start_reindex()
    wait(knowledge)
    status = knowledge.index_status()
    assert status.job is not None and status.job.state == "failed" and status.job.error_code == "local.unreachable"
    assert status.active is not None and status.active["model"] == "concepts:1"
    assert [hit.source for hit in knowledge.search("reimbursed", mode="vector", k=1)] == [ids["returns.md"]]
    assert knowledge.drop_vectors().state == "off"


def test_tenants_never_see_each_others_vectors(
    make: Callable[[str], KnowledgeBase], request: pytest.FixtureRequest
) -> None:
    if "postgres" not in request.node.callspec.params["make"]:
        pytest.skip("tenants exist on PostgreSQL only")
    alpha, beta = make("alpha"), make("beta")
    fill(alpha)
    alpha.start_reindex()
    wait(alpha)
    beta.add(b"Beta keeps a single note about money.", name="beta.md")
    assert beta.index_status().state == "off" and beta.index_status().chunks == 1
    beta.start_reindex()
    wait(beta)
    hits = beta.search("refund money back", mode="vector", k=10)
    assert len(hits) == 1 and hits[0].text.startswith("Beta")


LIVE = __import__("os").environ.get("NODESTRO_TEST_OLLAMA_URL", "")


@pytest.mark.skipif(not LIVE, reason="set NODESTRO_TEST_OLLAMA_URL to a real Ollama with qwen3-embedding:0.6b")
def test_semantic_search_with_a_real_embedding_model(tmp_path: Path) -> None:
    from nodestro_runtime.schemas import SettingsIn
    from nodestro_runtime.workspace import Workspace

    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        workspace.update_settings(
            SettingsIn(providers={"ollama": {"base_url": f"{LIVE}/v1"}}, knowledge_retrieval="hybrid")
        )
        assert workspace.settings().embedding_model == "qwen3-embedding:0.6b"
        ids = {name: workspace.knowledge.add(text.encode(), name=name).id for name, text in DOCS.items()}
        workspace.knowledge.start_reindex()
        deadline = time.monotonic() + 300
        while workspace.knowledge.index_status().state != "ready":
            job = workspace.knowledge.index_status().job
            assert job is None or job.state != "failed", job
            assert time.monotonic() < deadline
            time.sleep(0.5)
        status = workspace.knowledge.index_status()
        assert status.active is not None and status.active["dimensions"] == 1024
        assert status.active["digest"].startswith("ac6da0dfba84")
        # German query, English documents, no shared word: only the meaning connects them.
        hits = workspace.knowledge.search("Wann bekomme ich mein Geld zurück?", mode="vector", k=1)
        assert hits[0].source == ids["returns.md"]
        hybrid = workspace.knowledge.search("Urlaubstage der Mitarbeiter", [ids["hr.md"], ids["shipping.md"]], 2)
        assert hybrid[0].source == ids["hr.md"]


def test_the_postgres_index_checks_its_fusion_options() -> None:
    import threading
    from types import SimpleNamespace

    from nodestro_runtime.postgres_tables import PostgresIndex

    database: Any = SimpleNamespace(_lock=threading.RLock())  # the checks run before any query
    with pytest.raises(ValueError, match="fusion"):
        PostgresIndex(database, "alpha", fusion="max")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="lexical_weight"):
        PostgresIndex(database, "alpha", lexical_weight=2.0)
    index = PostgresIndex(database, "alpha", fusion="rrf", lexical_weight=0.25)
    assert (index.fusion, index.lexical_weight) == ("rrf", 0.25)


def test_a_rebuilt_embedding_model_needs_a_reindex_but_uploads_keep_working(
    make: Callable[[str], KnowledgeBase],
) -> None:
    """RT3-08: the embedding model got a new digest on the server (a pull or rebuild): the status says
    ``reindex_required``, an upload is indexed without vectors instead of failing, and a reindex fixes both."""
    knowledge = make("t1")
    knowledge.settings["mode"] = "hybrid"  # type: ignore[attr-defined]
    knowledge.add(DOCS["returns.md"].encode(), name="returns.md")
    knowledge.start_reindex()
    wait(knowledge)
    assert knowledge.index_status().state == "ready"
    knowledge.models["concepts:1"].digest = "d2"  # type: ignore[attr-defined]
    assert knowledge.index_status().state == "reindex_required"
    with pytest.raises(NodestroError) as error:
        knowledge.search("refund money", mode="hybrid")
    assert error.value.code == "knowledge.reindex_required"
    added = knowledge.add(DOCS["shipping.md"].encode(), name="shipping.md")  # raised RetrievalError before
    assert added.id in {item.id for item in knowledge.files()}
    knowledge.start_reindex()
    wait(knowledge)
    status = knowledge.index_status()
    assert status.state == "ready" and status.embedded == status.chunks
    assert knowledge.search("refund money", mode="hybrid")


def test_without_sqlite_vec_the_index_is_unavailable_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sqlite-vec (Windows on ARM has no wheel): the index used to report ``off`` and a reindex failed inside
    Orkexa with an ImportError. Now it is ``unavailable`` with the reason, and a reindex is refused up front."""
    import importlib.util

    from nodestro_runtime import knowledge as module

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "sqlite_vec" else real(name, *a))
    knowledge = KnowledgeBase(tmp_path / "nodestro.db")
    try:
        knowledge.retrieval = lambda: ("vector", "concepts:1")  # type: ignore[assignment,return-value]
        knowledge.embedding_model = lambda name: ConceptEmbeddings(name)
        knowledge.add(b"Employees have 30 days of vacation per year.", name="hr.md")
        status = knowledge.index_status()
        assert status.state == "unavailable" and "sqlite-vec" in status.detail
        assert module.sqlite_vectors_missing() is not None
        with pytest.raises(NodestroError) as refused:
            knowledge.start_reindex()
        assert refused.value.code == "knowledge.vectors_unavailable"
        with pytest.raises(NodestroError) as search:
            knowledge.search("vacation", mode="vector")
        assert search.value.code == "knowledge.vectors_missing"
        assert knowledge.search("vacation", k=1, mode="fts")  # full-text search keeps working
    finally:
        knowledge.close()
