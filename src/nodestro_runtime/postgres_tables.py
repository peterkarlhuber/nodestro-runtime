"""Knowledge files, their search index and memory spaces on PostgreSQL (ADR-0002, server profile).

The Orkexa backends Nodestro uses on the desktop (``SQLiteFileStore``, ``SQLiteIndex``, ``SQLiteMemory``) keep
their data in SQLite tables. These classes implement the same Orkexa interfaces (``FileStore``, ``Index``,
``MemoryBackend``) on the tables of migration ``0002`` for one tenant and one Orkexa namespace, on the connection
and lock of a :class:`~nodestro_runtime.storage_postgres.PostgresDatabase`. Every query filters on ``tenant_id``.

Search: the words of a chunk's title and text (letters and digits; lower case, diacritics removed, as SQLite's
``unicode61 remove_diacritics 2`` tokenizer does) are kept in ``search_text`` and indexed as a ``tsvector``. A query
matches any of its words or word prefixes, like :class:`orkexa.SQLiteIndex`; results are ranked by ``ts_rank``
instead of BM25, so the order of equally good hits can differ from the desktop.

Hybrid search fuses both rankings as :class:`orkexa.SQLiteIndex` does (:func:`orkexa.retrieval.fuse_hybrid`): the
min-max normalized cosine similarity plus the full-text evidence of each candidate, the squared IDF-weighted share of
the query's words it contains (word weights count the chunks of the tenant's namespace). ``ts_rank`` only picks the
full-text candidates, so the fused order does not depend on it. ``fusion="rrf"`` keeps the earlier reciprocal rank
fusion.

Vectors (migration ``0010``): like :class:`orkexa.SQLiteIndex`, each namespace keeps its vectors in a *space* bound to
one embedding model identity (``nodestro_vector_spaces``, ``nodestro_chunk_vectors``). With pgvector installed the
database ranks them (an exact cosine distance); without it the runtime does, over the same rows. Searches are
limited to the allowed sources before ranking.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
import json
import math
import re
from typing import TYPE_CHECKING, Any
import unicodedata

from orkexa import Chunk, FileStore, Hit, Index, MemoryToolError, StoredFile
from orkexa.embeddings import EmbeddingIdentity, EmbeddingModel, validate_vector
from orkexa.memory import MemoryBackend
from orkexa.retrieval import (
    HYBRID_FUSIONS,
    LEXICAL_WEIGHT,
    HybridFusion,
    RetrievalError,
    RetrievalMode,
    VectorStatus,
    check_mode,
    fuse_hybrid,
    fuse_rrf,
    lexical_evidence,
    word_weight,
)

from .bedrock import BedrockOperatorConfig
from .knowledge import KnowledgeTables
from .memory_spaces import BrowsableMemory
from .storage_postgres import TENANT_ID, PostgresDatabase, _encode

if TYPE_CHECKING:
    from .server_settings import TenantSecrets
    from .workspace import WorkspaceStorage

#: Words as SQLite's unicode61 tokenizer sees them: runs of letters and digits.
_WORD = re.compile(r"[^\W_]+")
#: Stored vectors a search may use. A zero vector has no direction, so no cosine: it is stored (its chunk counts as
#: embedded, as in ``orkexa.SQLiteIndex``) but never ranked. Vectors stored before vectors were checked can also hold
#: NaN or infinity, which no ranking can use (pgvector refuses to cast them). Searches leave both out; the rows stay.
_USABLE = (
    "NOT (v.embedding && '{NaN,Infinity,-Infinity}'::real[])"
    " AND v.embedding <> array_fill(0::real, ARRAY[cardinality(v.embedding)])"
)


def _check(tenant_id: str, namespace: str) -> None:
    if not TENANT_ID.fullmatch(tenant_id):
        raise ValueError(f"Invalid tenant id {tenant_id!r}")
    if not namespace:
        raise ValueError("namespace must not be empty")


def search_words(text: str) -> builtins.list[str]:
    """The words of ``text`` for the search index: lower case, without diacritics."""
    folded = "".join(char for char in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(char))
    return _WORD.findall(folded)


def _cosine(query: builtins.list[float], query_norm: float, stored: builtins.list[float]) -> float:
    """The cosine similarity of a query vector (with its length) and a stored vector (neither is all zeros)."""
    dot = math.fsum(a * b for a, b in zip(query, stored, strict=True))
    return dot / (query_norm * math.sqrt(math.fsum(x * x for x in stored)))


def _text(value: str) -> str:
    """PostgreSQL text cannot hold U+0000; the index keeps U+FFFD in its place."""
    return value.replace("\x00", "\ufffd")


def _tsquery(query: str) -> str:
    """Any word or word prefix of ``query`` (``refund`` finds ``refunds``); empty: nothing to search for."""
    words = dict.fromkeys(search_words(query))
    return " | ".join(f"'{word}':*" for word in words)


class PostgresFileStore(FileStore):
    """An Orkexa :class:`~orkexa.FileStore` of one tenant and namespace (table ``nodestro_files``)."""

    def __init__(
        self,
        database: PostgresDatabase,
        tenant_id: str,
        *,
        namespace: str = "default",
        files_api: Any | None = None,
        max_image_bytes: int = 5_000_000,
        max_document_bytes: int = 20_000_000,
    ) -> None:
        super().__init__(files_api=files_api, max_image_bytes=max_image_bytes, max_document_bytes=max_document_bytes)
        _check(tenant_id, namespace)
        self.database = database
        self.tenant_id = tenant_id
        self.namespace = namespace
        self._lock = database._lock  # one lock for every user of the connection

    @property
    def capabilities(self) -> frozenset[str]:
        """``partitioned`` (by tenant) and ``concurrent_writers`` (:attr:`orkexa.FileStore.capabilities`)."""
        return frozenset({"partitioned", "concurrent_writers"})

    def close(self) -> None:
        """Nothing to do: the connection belongs to the database."""

    def _save(self, entry: StoredFile, data: bytes | None) -> None:
        if data is None:
            self.database._write(
                "UPDATE nodestro_files SET entry = %s::jsonb WHERE tenant_id = %s AND namespace = %s AND id = %s",
                (_encode(entry.model_dump_json())[0], self.tenant_id, self.namespace, entry.id),
            )
        else:
            self.database._write(
                "INSERT INTO nodestro_files (tenant_id, namespace, id, entry, data) VALUES (%s, %s, %s, %s::jsonb, %s)"
                " ON CONFLICT (tenant_id, namespace, id) DO UPDATE SET entry = excluded.entry, data = excluded.data",
                (self.tenant_id, self.namespace, entry.id, _encode(entry.model_dump_json())[0], data),
            )

    def _entry(self, file_id: str) -> StoredFile | None:
        rows = self.database._query(
            "SELECT entry::text FROM nodestro_files WHERE tenant_id = %s AND namespace = %s AND id = %s",
            (self.tenant_id, self.namespace, file_id),
        )
        return StoredFile.model_validate_json(rows[0][0]) if rows else None

    def _data(self, file_id: str) -> bytes | None:
        rows = self.database._query(
            "SELECT data FROM nodestro_files WHERE tenant_id = %s AND namespace = %s AND id = %s",
            (self.tenant_id, self.namespace, file_id),
        )
        return bytes(rows[0][0]) if rows else None

    def _entries(self) -> builtins.list[StoredFile]:
        rows = self.database._query(
            "SELECT entry::text FROM nodestro_files WHERE tenant_id = %s AND namespace = %s",
            (self.tenant_id, self.namespace),
        )
        return [StoredFile.model_validate_json(row[0]) for row in rows]

    def _remove(self, file_id: str) -> None:
        self.database._write(
            "DELETE FROM nodestro_files WHERE tenant_id = %s AND namespace = %s AND id = %s",
            (self.tenant_id, self.namespace, file_id),
        )


class PostgresIndex(Index):
    """An Orkexa :class:`~orkexa.Index` of one tenant and namespace with full-text search (``nodestro_chunks``).

    Also gives what Nodestro reads beside the Orkexa interface (``KnowledgeChunks`` on SQLite): chunk counts per
    source, pointing a source's chunks to its file, and a source's indexed text."""

    def __init__(
        self,
        database: PostgresDatabase,
        tenant_id: str,
        *,
        namespace: str = "default",
        max_chars: int = 1500,
        overlap: int = 200,
        embeddings: EmbeddingModel | None = None,
        mode: RetrievalMode | None = None,
        fusion: HybridFusion = "evidence",
        lexical_weight: float = LEXICAL_WEIGHT,
    ) -> None:
        super().__init__(max_chars=max_chars, overlap=overlap)
        _check(tenant_id, namespace)
        if fusion not in HYBRID_FUSIONS:
            raise ValueError(f"fusion must be one of {', '.join(HYBRID_FUSIONS)}, not {fusion!r}")
        if not 0.0 <= lexical_weight <= 1.0:
            raise ValueError("lexical_weight must be between 0 and 1")
        #: How ``hybrid`` combines full text and vectors (``evidence`` or ``rrf``), and the full-text share.
        self.fusion: HybridFusion = fusion
        self.lexical_weight = lexical_weight
        self.database = database
        self.tenant_id = tenant_id
        self.namespace = namespace
        self._lock = database._lock
        #: The embedding model new chunks are embedded with (``None``: full-text only).
        self.embeddings = embeddings
        self.mode = check_mode(mode) if mode is not None else None
        self._pgvector: bool | None = None

    @property
    def capabilities(self) -> frozenset[str]:
        """``partitioned`` (by tenant) and ``concurrent_writers`` always; ``vectors`` and ``reindex`` with an
        embedding model, as :class:`orkexa.SQLiteIndex` (:attr:`orkexa.Index.capabilities`)."""
        found = {"partitioned", "concurrent_writers"}
        if self.embeddings is not None:
            found |= {"vectors", "reindex"}
        return frozenset(found)

    def close(self) -> None:
        """Nothing to do: the connection belongs to the database."""

    def __len__(self) -> int:
        rows = self.database._query(
            "SELECT count(*) FROM nodestro_chunks WHERE tenant_id = %s AND namespace = %s",
            (self.tenant_id, self.namespace),
        )
        return int(rows[0][0])

    def sources(self) -> builtins.list[str]:
        rows = self.database._query(
            "SELECT DISTINCT source FROM nodestro_chunks WHERE tenant_id = %s AND namespace = %s ORDER BY source",
            (self.tenant_id, self.namespace),
        )
        return [str(row[0]) for row in rows]

    def add_chunks(self, chunks: Iterable[Chunk]) -> builtins.list[int]:
        kept = [chunk for chunk in chunks if chunk.text.strip()]
        ids: builtins.list[int] = []
        if not kept:
            return ids
        identity = None
        vectors: builtins.list[builtins.list[float]] | None = None
        if self.embeddings is not None:
            self.embeddings.refresh_identity()  # the model's current build, not a cached one (MUP-ONA drift 1)
            identity = self.embeddings.identity()
            vectors = [[float(x) for x in vector] for vector in self.embeddings.embed_documents([c.text for c in kept])]
            if len(vectors) != len(kept):
                raise ValueError("The embedding model must return one vector per text")
            for vector in vectors:
                # NaN or infinity: vector.invalid; another length than the model's: vector.dimension (drift 2).
                validate_vector(vector, identity)
        with self.database._transaction() as db:
            space = self._active_for(identity) if identity is not None else None
            for position, chunk in enumerate(kept):
                row = db.execute(
                    "INSERT INTO nodestro_chunks"
                    " (tenant_id, namespace, source, title, file_id, metadata, text, search_text)"
                    " VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s) RETURNING id",
                    (
                        self.tenant_id,
                        self.namespace,
                        _text(chunk.source),
                        None if chunk.title is None else _text(chunk.title),
                        chunk.file_id,
                        _encode(json.dumps(chunk.metadata))[0],
                        _text(chunk.text),
                        " ".join(search_words(f"{chunk.text} {chunk.title or ''}")),
                    ),
                ).fetchone()
                ids.append(int(row[0]))
                if vectors is not None and space is not None:
                    db.execute(
                        "INSERT INTO nodestro_chunk_vectors (tenant_id, namespace, space, chunk_id, embedding)"
                        " VALUES (%s, %s, %s, %s, %s)",
                        (self.tenant_id, self.namespace, space, int(row[0]), vectors[position]),
                    )
        return ids

    def delete(self, source: str) -> int:
        return self.database._write(
            "DELETE FROM nodestro_chunks WHERE tenant_id = %s AND namespace = %s AND source = %s",
            (self.tenant_id, self.namespace, source),
        )

    def search(self, query: str, k: int = 5) -> builtins.list[Hit]:
        """:meth:`retrieve` in the index's default mode: ``hybrid`` with an embedding model, else ``fts``."""
        return self.retrieve(query, k)

    # ---- vectors (migration 0010) -----------------------------------------------------------------------------

    def _space(self, state: str) -> tuple[str, EmbeddingIdentity] | None:
        rows = self.database._query(
            "SELECT space, identity::text FROM nodestro_vector_spaces"
            " WHERE tenant_id = %s AND namespace = %s AND state = %s",
            (self.tenant_id, self.namespace, state),
        )
        return (str(rows[0][0]), EmbeddingIdentity.from_dict(json.loads(rows[0][1]))) if rows else None

    def _active_for(self, identity: EmbeddingIdentity) -> str:
        """The active space for ``identity``, created when there is none and no chunk lacks a vector."""
        space = f"{self.namespace}:{identity.key}"
        current = self._space("active")
        if current is not None:
            if current[1].key != identity.key:
                raise RetrievalError(
                    "reindex_required",
                    f"The vectors belong to {current[1].model}, not to {identity.model}; reindex first",
                )
            return current[0]
        building = self._space("building")
        if building is not None:
            if building[0] == space:
                return space
            raise RetrievalError("reindex_required", "A reindex for another embedding model is running")
        if len(self):
            raise RetrievalError("reindex_required", "The index holds chunks without vectors; reindex first")
        self.database._write(
            "INSERT INTO nodestro_vector_spaces (tenant_id, namespace, space, identity, dimensions, state)"
            " VALUES (%s, %s, %s, %s::jsonb, %s, 'active')",
            (self.tenant_id, self.namespace, space, json.dumps(identity.to_dict()), identity.dimensions),
        )
        return space

    def _count_vectors(self, space: str) -> int:
        rows = self.database._query(
            "SELECT count(*) FROM nodestro_chunk_vectors WHERE tenant_id = %s AND namespace = %s AND space = %s",
            (self.tenant_id, self.namespace, space),
        )
        return int(rows[0][0])

    def pgvector(self) -> bool:
        """Whether the database ranks vectors itself (pgvector is installed)."""
        if self._pgvector is None:
            rows = self.database._query("SELECT to_regtype('vector') IS NOT NULL")
            self._pgvector = bool(rows and rows[0][0])
        return self._pgvector

    def vector_status(self) -> VectorStatus:
        active = self._space("active")
        building = self._space("building")
        return VectorStatus(
            chunks=len(self),
            active=active[1].to_dict() if active else None,
            embedded=self._count_vectors(active[0]) if active else 0,
            building=building[1].to_dict() if building else None,
            building_embedded=self._count_vectors(building[0]) if building else 0,
        )

    def _discard(self, space: str) -> None:
        self.database._write(
            "DELETE FROM nodestro_vector_spaces WHERE tenant_id = %s AND namespace = %s AND space = %s",
            (self.tenant_id, self.namespace, space),
        )

    def discard_reindex(self) -> None:
        building = self._space("building")
        if building is not None:
            self._discard(building[0])

    def drop_vectors(self) -> int:
        status = self.vector_status()
        self.database._write(
            "DELETE FROM nodestro_vector_spaces WHERE tenant_id = %s AND namespace = %s",
            (self.tenant_id, self.namespace),
        )
        return status.embedded + status.building_embedded

    def reindex(
        self,
        embeddings: EmbeddingModel | None = None,
        *,
        batch_size: int = 32,
        progress: Any = None,
        cancel: Any = None,
    ) -> VectorStatus:
        """Embed every chunk into the space of ``embeddings`` and switch to it (as ``orkexa.SQLiteIndex.reindex``).

        The model's identity is read again (``refresh_identity``) before and after embedding: a model updated on its
        server is never filed under its old identity, and one updated meanwhile discards the new space."""
        model = embeddings or self.embeddings
        if model is None:
            raise RetrievalError("vector_unavailable", "reindex() needs an embedding model")
        model.refresh_identity()  # the model's current build, not a cached one (MUP-ONA drift 1)
        identity = model.identity()
        space = f"{self.namespace}:{identity.key}"
        active = self._space("active")
        if active is None or active[0] != space:
            building = self._space("building")
            if building is not None and building[0] != space:
                self._discard(building[0])
            self.database._write(
                "INSERT INTO nodestro_vector_spaces (tenant_id, namespace, space, identity, dimensions, state)"
                " VALUES (%s, %s, %s, %s::jsonb, %s, 'building') ON CONFLICT (tenant_id, namespace, space) DO NOTHING",
                (self.tenant_id, self.namespace, space, json.dumps(identity.to_dict()), identity.dimensions),
            )
        total = len(self)
        while True:
            if cancel is not None and cancel.is_set():
                raise RetrievalError("cancelled", "The reindex was cancelled; start it again to continue")
            rows = self.database._query(
                "SELECT c.id, c.text FROM nodestro_chunks c WHERE c.tenant_id = %s AND c.namespace = %s AND NOT EXISTS"
                " (SELECT 1 FROM nodestro_chunk_vectors v WHERE v.tenant_id = %s AND v.space = %s"
                " AND v.chunk_id = c.id) ORDER BY c.id LIMIT %s",
                (self.tenant_id, self.namespace, self.tenant_id, space, batch_size),
            )
            if not rows:
                break
            vectors = model.embed_documents([str(row[1]) for row in rows])
            if len(vectors) != len(rows):
                raise RetrievalError("vector_unavailable", "The embedding model returned a wrong number of vectors")
            with self.database._transaction() as db:
                for (chunk_id, _), vector in zip(rows, vectors, strict=True):
                    if len(vector) != identity.dimensions:
                        raise RetrievalError("vector_unavailable", "The embedding model changed its vector size")
                    validate_vector(vector)  # NaN or infinity: vector.invalid (drift 2)
                    db.execute(
                        "INSERT INTO nodestro_chunk_vectors (tenant_id, namespace, space, chunk_id, embedding)"
                        " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (tenant_id, space, chunk_id)"
                        " DO UPDATE SET embedding = excluded.embedding",
                        (self.tenant_id, self.namespace, space, int(chunk_id), [float(x) for x in vector]),
                    )
            if progress is not None:
                progress(self._count_vectors(space), total)
        model.refresh_identity()
        if model.identity().key != identity.key:
            # The model changed on its server while it embedded: the new space may mix two builds.
            self._discard(space)
            raise RetrievalError(
                "reindex_required",
                f"{identity.model} changed during the reindex; its new vectors were discarded, start it again",
            )
        with self.database._transaction() as db:
            missing = db.execute(
                "SELECT count(*) FROM nodestro_chunks c WHERE c.tenant_id = %s AND c.namespace = %s AND NOT EXISTS"
                " (SELECT 1 FROM nodestro_chunk_vectors v WHERE v.tenant_id = %s AND v.space = %s"
                " AND v.chunk_id = c.id)",
                (self.tenant_id, self.namespace, self.tenant_id, space),
            ).fetchone()[0]
            if missing:
                raise RetrievalError("vector_incomplete", f"{missing} chunks were added during the reindex")
            db.execute(
                "DELETE FROM nodestro_vector_spaces WHERE tenant_id = %s AND namespace = %s AND space <> %s",
                (self.tenant_id, self.namespace, space),
            )
            db.execute(
                "UPDATE nodestro_vector_spaces SET state = 'active' WHERE tenant_id = %s AND namespace = %s"
                " AND space = %s",
                (self.tenant_id, self.namespace, space),
            )
        self.embeddings = model
        return self.vector_status()

    def _hits(self, rows: Iterable[tuple[Any, ...]]) -> builtins.list[Hit]:
        return [
            Hit(
                id=int(chunk_id),
                score=float(score),
                source=source,
                title=title,
                file_id=file_id,
                metadata=json.loads(metadata),
                text=text,
            )
            for chunk_id, source, title, file_id, metadata, text, score in rows
        ]

    def _fts(self, query: str, k: int, sources: builtins.list[str] | None) -> builtins.list[Hit]:
        expression = _tsquery(query)
        if not expression:
            return []
        scope = " AND source = ANY(%s)" if sources is not None else ""
        params: tuple[Any, ...] = (expression, self.tenant_id, self.namespace)
        if sources is not None:
            params += (sources,)
        rows = self.database._query(
            "SELECT id, source, title, file_id, metadata::text, text FROM nodestro_chunks,"
            " to_tsquery('simple', %s) AS query"
            f" WHERE tenant_id = %s AND namespace = %s AND search @@ query{scope}"
            " ORDER BY ts_rank(search, query, 1) DESC, id LIMIT %s",
            (*params, k),
        )
        # ts_rank only orders the hits; the score is the contract's reciprocal rank 1 / (60 + rank) (drift 5).
        scores = dict(fuse_rrf([[int(row[0]) for row in rows]]))
        return self._hits((*row, scores[int(row[0])]) for row in rows)

    def _vector(self, query: str, k: int, sources: builtins.list[str] | None) -> builtins.list[Hit]:
        model = self.embeddings
        active = self._space("active")
        if model is None or active is None:
            raise RetrievalError("vector_unavailable", "This knowledge base has no semantic index yet")
        space, stored = active
        if stored.key != model.identity().key:
            raise RetrievalError("reindex_required", f"The vectors belong to {stored.model}; reindex first")
        vector = [float(x) for x in model.embed_query(query)]
        # NaN or infinity: vector.invalid; another length than the stored vectors: vector.dimension (drift 2). A zero
        # query vector has no direction: it finds nothing (drift 3).
        if validate_vector(vector, stored) is None:
            return []
        scope = " AND c.source = ANY(%s)" if sources is not None else ""
        base: tuple[Any, ...] = (self.tenant_id, space, self.tenant_id, self.namespace)
        if sources is not None:
            base += (sources,)
        if self.pgvector():
            literal = "[" + ",".join(repr(x) for x in vector) + "]"
            rows = self.database._query(
                "SELECT c.id, c.source, c.title, c.file_id, c.metadata::text, c.text,"
                " round((1 - (v.embedding::vector <=> %s::vector))::numeric, 6)::float8 AS score"
                " FROM nodestro_chunk_vectors v JOIN nodestro_chunks c ON c.id = v.chunk_id"
                f" WHERE v.tenant_id = %s AND v.space = %s AND c.tenant_id = %s AND c.namespace = %s{scope}"
                f" AND {_USABLE} ORDER BY score DESC, c.id LIMIT %s",
                (literal, *base, k),
            )
            return self._hits(rows)
        candidates = self.database._query(
            "SELECT c.id, v.embedding FROM nodestro_chunk_vectors v JOIN nodestro_chunks c ON c.id = v.chunk_id"
            f" WHERE v.tenant_id = %s AND v.space = %s AND c.tenant_id = %s AND c.namespace = %s{scope} AND {_USABLE}",
            base,
        )
        # The cosine similarity, as pgvector's <=> gives it, whatever the length of the vectors: a model whose
        # identity is not ``normalized`` returns vectors of any length, where the dot product is no cosine (drift 4).
        # math.fsum is exactly rounded, so the result is the same on every Python version.
        query_norm = math.sqrt(math.fsum(x * x for x in vector))
        # Rounded to 6 digits as the contract says (drift 5); equal scores go to the lower chunk id, as in the SQL.
        similarity = [
            (round(_cosine(vector, query_norm, [float(x) for x in embedding]), 6), int(chunk_id))
            for chunk_id, embedding in candidates
        ]
        scored = sorted(similarity, key=lambda item: (-item[0], item[1]))[:k]
        if not scored:
            return []
        rows = self.database._query(
            "SELECT id, source, title, file_id, metadata::text, text FROM nodestro_chunks"
            " WHERE tenant_id = %s AND namespace = %s AND id = ANY(%s)",
            (self.tenant_id, self.namespace, [chunk_id for _, chunk_id in scored]),
        )
        by_id = {int(row[0]): row for row in rows}
        return self._hits((*by_id[chunk_id], score) for score, chunk_id in scored if chunk_id in by_id)

    def retrieve(
        self,
        query: str,
        k: int = 5,
        *,
        mode: RetrievalMode | None = None,
        sources: Iterable[str] | None = None,
    ) -> builtins.list[Hit]:
        """The ``k`` best chunks by full text, vectors or both, limited to ``sources`` before ranking. Without ``mode``
        (and no ``mode`` of the index) ``hybrid`` with an embedding model, else ``fts``. ``vector`` and
        ``hybrid`` never fall back to full text. ``hybrid`` fuses ``max(4 * k, 20)`` candidates of each ranking by
        :func:`orkexa.retrieval.fuse_hybrid` (scores in [0, 1]), or by reciprocal rank fusion with ``fusion="rrf"``."""
        if k < 1:
            raise ValueError("k must be positive")
        # The contract's default (drift 6): hybrid with an embedding model, full text without one.
        chosen = check_mode(mode or self.mode or ("hybrid" if self.embeddings is not None else "fts"))
        allowed = None if sources is None else builtins.list(dict.fromkeys(sources))
        if allowed is not None and not allowed:
            return []
        if chosen == "fts":
            hits = self._fts(query, k, allowed)
            for rank, hit in enumerate(hits):
                hit.ranks = {"fts": rank + 1}
            return hits
        if chosen == "vector":
            hits = self._vector(query, k, allowed)
            for rank, hit in enumerate(hits):
                hit.ranks = {"vector": rank + 1}
            return hits
        depth = max(k * 4, 20)
        rankings = (("fts", self._fts(query, depth, allowed)), ("vector", self._vector(query, depth, allowed)))
        if self.fusion == "evidence":
            found: dict[int, Hit] = {}
            positions: dict[int, dict[str, int]] = {}
            for name, ranked in rankings:
                for rank, hit in enumerate(ranked):
                    found.setdefault(hit.id, hit)
                    positions.setdefault(hit.id, {})[name] = rank + 1
            vector = [(hit.id, hit.score) for hit in rankings[1][1]]
            lexical = self._lexical_evidence(query, builtins.list(found))
            result = []
            for chunk_id, score in fuse_hybrid(vector, lexical, lexical_weight=self.lexical_weight)[:k]:
                hit = found[chunk_id]
                hit.score = score
                hit.ranks = positions[chunk_id]
                result.append(hit)
            return result
        found = {}
        positions = {}
        for name, ranked in rankings:
            for rank, hit in enumerate(ranked):
                found.setdefault(hit.id, hit)
                positions.setdefault(hit.id, {})[name] = rank + 1
        result = []
        for chunk_id, score in fuse_rrf([[hit.id for hit in ranked] for _, ranked in rankings])[:k]:
            hit = found[chunk_id]
            hit.score = score
            hit.ranks = positions[chunk_id]
            result.append(hit)
        return result

    def _lexical_evidence(self, query: str, chunk_ids: builtins.list[int]) -> dict[int, float]:
        """:func:`orkexa.retrieval.lexical_evidence` of ``chunk_ids``: which query words (as prefixes, like
        :func:`_tsquery`) each contains, weighted by how many chunks of this namespace contain them."""
        words = builtins.list(dict.fromkeys(search_words(query)))
        found: dict[int, builtins.list[str]] = {chunk_id: [] for chunk_id in chunk_ids}
        if not words:
            return lexical_evidence({}, found)
        word_query = "to_tsquery('simple', quote_literal(w) || ':*')"
        counts = self.database._query(
            f"SELECT w, (SELECT count(*) FROM nodestro_chunks WHERE tenant_id = %s AND namespace = %s"
            f" AND search @@ {word_query}) FROM unnest(%s::text[]) AS w",
            (self.tenant_id, self.namespace, words),
        )
        total = len(self)
        weights = {str(word): word_weight(int(count), total) for word, count in counts}
        if chunk_ids:
            rows = self.database._query(
                "SELECT c.id, w FROM nodestro_chunks c, unnest(%s::text[]) AS w"
                " WHERE c.tenant_id = %s AND c.namespace = %s AND c.id = ANY(%s)"
                f" AND c.search @@ {word_query}",
                (words, self.tenant_id, self.namespace, chunk_ids),
            )
            for chunk_id, word in rows:
                found[int(chunk_id)].append(str(word))
        return lexical_evidence(weights, found)

    # ---- what KnowledgeChunks does on SQLite -------------------------------------------------------

    def counts(self) -> dict[str, int]:
        """Number of chunks per source."""
        rows = self.database._query(
            "SELECT source, count(*) FROM nodestro_chunks WHERE tenant_id = %s AND namespace = %s GROUP BY source",
            (self.tenant_id, self.namespace),
        )
        return {str(source): int(count) for source, count in rows}

    def attach_file(self, source: str) -> None:
        """Let every chunk of ``source`` point to the file with the same id."""
        self.database._write(
            "UPDATE nodestro_chunks SET file_id = %s WHERE tenant_id = %s AND namespace = %s AND source = %s",
            (source, self.tenant_id, self.namespace, source),
        )

    def text(self, source: str) -> str:
        """The indexed text of ``source``: its chunks in the order they were added, one per line."""
        rows = self.database._query(
            "SELECT text FROM nodestro_chunks WHERE tenant_id = %s AND namespace = %s AND source = %s ORDER BY id",
            (self.tenant_id, self.namespace, source),
        )
        return "\n".join(str(row[0]) for row in rows)


class PostgresMemory(MemoryBackend):
    """An Orkexa memory (``/memories``) of one tenant and namespace (table ``nodestro_memory_files``).

    Every command runs in one transaction, as with ``SQLiteMemory``."""

    def __init__(
        self,
        database: PostgresDatabase,
        tenant_id: str,
        *,
        namespace: str = "default",
        max_file_chars: int = 100_000,
        max_view_chars: int = 16_000,
    ) -> None:
        super().__init__(max_file_chars=max_file_chars, max_view_chars=max_view_chars)
        _check(tenant_id, namespace)
        self.database = database
        self.tenant_id = tenant_id
        self.namespace = namespace
        self._lock = database._lock

    @property
    def capabilities(self) -> frozenset[str]:
        """``partitioned`` (by tenant) and ``concurrent_writers`` (:attr:`orkexa.memory.MemoryBackend.capabilities`):
        the commands of one process are serialized on the database's lock."""
        return frozenset({"partitioned", "concurrent_writers"})

    def close(self) -> None:
        """Nothing to do: the connection belongs to the database."""

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self.database._transaction():
            yield

    def _read(self, key: str) -> str | None:
        rows = self.database._query(
            "SELECT content FROM nodestro_memory_files WHERE tenant_id = %s AND namespace = %s AND path = %s",
            (self.tenant_id, self.namespace, key),
        )
        return str(rows[0][0]) if rows else None

    def _write(self, key: str, text: str) -> None:
        if "\x00" in text or "\x00" in key:
            raise MemoryToolError("Error: memory files cannot contain the character U+0000")
        self.database._write(
            "INSERT INTO nodestro_memory_files (tenant_id, namespace, path, content) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (tenant_id, namespace, path) DO UPDATE SET content = excluded.content,"
            " updated_at = clock_timestamp()",
            (self.tenant_id, self.namespace, key, text),
        )

    def _files(self, prefix: str) -> dict[str, int]:
        start = f"{prefix}/" if prefix else ""
        rows = self.database._query(
            "SELECT path, octet_length(content) FROM nodestro_memory_files"
            " WHERE tenant_id = %s AND namespace = %s AND starts_with(path, %s)",
            (self.tenant_id, self.namespace, start),
        )
        return {str(path): int(size) for path, size in rows}

    def _delete(self, key: str) -> None:
        self.database._write(
            "DELETE FROM nodestro_memory_files WHERE tenant_id = %s AND namespace = %s"
            " AND (path = %s OR starts_with(path, %s))",
            (self.tenant_id, self.namespace, key, key + "/"),
        )

    def _rename(self, old: str, new: str) -> None:
        self.database._write(
            "UPDATE nodestro_memory_files SET path = %s || substr(path, %s), updated_at = clock_timestamp()"
            " WHERE tenant_id = %s AND namespace = %s AND (path = %s OR starts_with(path, %s))",
            (new, len(old) + 1, self.tenant_id, self.namespace, old, old + "/"),
        )

    def clear(self) -> None:
        """Delete every file of this memory."""
        self.database._write(
            "DELETE FROM nodestro_memory_files WHERE tenant_id = %s AND namespace = %s",
            (self.tenant_id, self.namespace),
        )


class PostgresMemorySpace(PostgresMemory, BrowsableMemory):
    """A memory space of the memory browser on PostgreSQL."""


class PostgresMemoryTables:
    """The memory spaces of one tenant (:class:`~nodestro_runtime.memory_spaces.MemoryTables`)."""

    def __init__(self, database: PostgresDatabase, tenant_id: str) -> None:
        _check(tenant_id, "-")
        self.database = database
        self.tenant_id = tenant_id

    def open_space(self, namespace: str) -> BrowsableMemory:
        return PostgresMemorySpace(self.database, self.tenant_id, namespace=namespace)

    def totals(self, prefix: str) -> builtins.list[tuple[str, int, int]]:
        return memory_totals(self.database, self.tenant_id, prefix)


def knowledge_tables(database: PostgresDatabase, tenant_id: str, *, namespace: str = "nodestro") -> KnowledgeTables:
    """The knowledge storage of one tenant for :class:`~nodestro_runtime.knowledge.KnowledgeBase`."""
    index = PostgresIndex(database, tenant_id, namespace=namespace)
    return KnowledgeTables(PostgresFileStore(database, tenant_id, namespace=namespace), index, index)


def tenant_storage(
    database: PostgresDatabase,
    tenant_id: str,
    secrets: TenantSecrets | None = None,
    endpoints: tuple[str, ...] = (),
    bedrock: BedrockOperatorConfig | None = None,
) -> WorkspaceStorage:
    """Everything a :class:`~nodestro_runtime.workspace.Workspace` stores, for one tenant of ``database``.

    ``secrets`` encrypts the tenant's provider keys at rest (a server always passes its box); ``endpoints`` are the
    model endpoints the operator allows (none by default). ``bedrock`` is the operator's Amazon Bedrock configuration
    (:meth:`~nodestro_runtime.bedrock.BedrockOperatorConfig.from_env`); the workspace gets only this tenant's own
    entry (``for_tenant``), never the roles of other tenants. ``None``: Bedrock is not available."""
    from .workspace import WorkspaceStorage

    return WorkspaceStorage(
        tenant_id=tenant_id,
        store=database.store(tenant_id),
        knowledge=knowledge_tables(database, tenant_id),
        memory=PostgresMemoryTables(database, tenant_id),
        secrets=secrets,
        endpoints=tuple(endpoints),
        bedrock=bedrock.for_tenant(tenant_id) if bedrock is not None else None,
    )


def memory_totals(database: PostgresDatabase, tenant_id: str, prefix: str) -> builtins.list[tuple[str, int, int]]:
    """``(namespace, files, bytes)`` of every memory namespace of the tenant starting with ``prefix``."""
    _check(tenant_id, "-")
    rows = database._query(
        "SELECT namespace, count(*), sum(octet_length(content)) FROM nodestro_memory_files"
        " WHERE tenant_id = %s AND starts_with(namespace, %s) GROUP BY namespace ORDER BY namespace",
        (tenant_id, prefix),
    )
    return [(str(namespace), int(count), int(size or 0)) for namespace, count, size in rows]


__all__ = [
    "PostgresFileStore",
    "PostgresIndex",
    "PostgresMemory",
    "PostgresMemorySpace",
    "PostgresMemoryTables",
    "knowledge_tables",
    "memory_totals",
    "search_words",
    "tenant_storage",
]
