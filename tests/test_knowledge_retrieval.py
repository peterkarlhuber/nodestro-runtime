"""Knowledge retrieval modes (fts, vector, hybrid), the semantic index and its reindex, on SQLite and PostgreSQL.

An agent's view searches only its files, limited before ranking in every mode. The PostgreSQL cases need
``NODESTRO_TEST_POSTGRES``; they run with pgvector where it is installed and with the runtime's own ranking too.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import math
from pathlib import Path
import time

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
