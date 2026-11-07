"""An Orkexa :class:`~orkexa.Index` of one tenant and namespace on PostgreSQL, with full text and vectors (MUP-ONA B4).

Tables (for a prefix ``p_``): ``p_chunks`` (chunks and their search words), ``p_vector_spaces`` and
``p_chunk_vectors`` (vectors, one space per embedding model identity, like :class:`orkexa.SQLiteIndex`).

Search: the words of a chunk's title and text (letters and digits; lower case, diacritics removed, as SQLite's
``unicode61 remove_diacritics 2`` tokenizer does) are kept in ``search_text`` and indexed as a ``tsvector``. A query
matches any of its words or word prefixes, like :class:`orkexa.SQLiteIndex`; results are ranked by ``ts_rank``
instead of BM25, so the order of equally good hits can differ from SQLite.

Hybrid search fuses both rankings as :class:`orkexa.SQLiteIndex` does (:func:`orkexa.retrieval.fuse_hybrid`): the
min-max normalized cosine similarity plus the full-text evidence of each candidate, the squared IDF-weighted share of
the query's words it contains (word weights count the chunks of the tenant's namespace). ``ts_rank`` only picks the
full-text candidates, so the fused order does not depend on it. ``fusion="rrf"`` keeps the earlier reciprocal rank
fusion.

Vectors: with pgvector installed the database ranks them (an exact cosine distance); without it the runtime does,
over the same rows. Searches are limited to the allowed sources before ranking.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable
import json
import math
import re
import threading
from typing import Any
import unicodedata

from orkexa import Chunk, Hit, Index
from orkexa.embeddings import EmbeddingIdentity, EmbeddingModel, validate_vector
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
    query_words,
    word_weight,
)

from ._sql import Executor, check_namespace, check_tenant, jsonb_text
from ._vector import cosine, embed_batch, pgvector_literal, refresh_identity
from .connection import ConnectionSource

__all__ = ["MAX_SOURCES", "PostgresIndex", "PostgresVectorStatus", "search_words"]

#: Words as SQLite's unicode61 tokenizer sees them: runs of letters and digits.
_WORD = re.compile(r"[^\W_]+")
#: Unlocked rounds of a reindex before it switches (PostgresIndex.reindex).
_REINDEX_ROUNDS = 5
#: At most this many chunks (or one batch, if larger) are embedded at the switch, while writes to the namespace wait.
#: The unlocked rounds leave about what writers add during one round; more than this means writes outpace the model.
_REINDEX_SWITCH_CHUNKS = 256
#: Most sources one search may be limited to (``retrieve(sources=...)``), as in ``orkexa.SQLiteIndex``.
MAX_SOURCES = 50_000


def search_words(text: str) -> builtins.list[str]:
    """The words of ``text`` for the search index: lower case, without diacritics."""
    folded = "".join(char for char in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(char))
    return _WORD.findall(folded)


def _text(value: str) -> str:
    """PostgreSQL text cannot hold U+0000; the index keeps U+FFFD in its place."""
    return value.replace("\x00", "�")


def _word_queries(query: str) -> dict[str, str]:
    """``tsquery`` text per query word of ``query`` (:func:`orkexa.retrieval.query_words`), as SQLite matches it.

    A query word is matched as a prefix (``refund`` finds ``refunds``). The search column holds
    :func:`search_words`, which split a word at an underscore and drop diacritics (as SQLite's ``unicode61
    remove_diacritics 2`` tokenizer does for documents and queries alike), so ``omega_psi`` finds ``omega psi`` in
    sequence (``'omega' <-> 'psi':*``), never ``omega`` or ``psi`` alone. A word without letters or digits
    (``_``) matches nothing."""
    queries: dict[str, str] = {}
    for word in query_words(query):
        parts = search_words(word)
        if parts:
            queries[word] = " <-> ".join(f"'{part}'" for part in parts) + ":*"
    return queries


def _tsquery(query: str) -> str:
    """Any query word of ``query`` (:func:`_word_queries`); empty: nothing to search for."""
    return " | ".join(f"({expression})" for expression in _word_queries(query).values())


class PostgresVectorStatus(VectorStatus):
    """:class:`orkexa.retrieval.VectorStatus` of a :class:`PostgresIndex`, with the vectors searches leave out.

    ``excluded`` counts the vectors of the active space that no search ranks: NaN or infinity (stored before vectors
    were checked) and zero vectors (no direction, no cosine). They stay stored; their chunks count as ``embedded``.
    ``legacy`` is set while there is any (vectors stored before the checks of MUP-ONA MP-B3, or zero vectors a model
    returned); a reindex with a model that returns usable vectors replaces them. Orkexa's ``SQLiteIndex`` uses
    ``legacy`` for the vectors of an ``embedder=`` callable, which a PostgreSQL index never holds."""

    excluded: int = 0


class PostgresIndex(Index):
    """An Orkexa :class:`~orkexa.Index` of one tenant and namespace with full-text search (``<prefix>chunks``).

    ``source`` gives the connections, ``prefix`` names the tables, ``lock`` as for
    :class:`~nodestro_runtime.pg_generic.files.PostgresFileStore` (the index's own statements do not take it: its
    transactions are the source's)."""

    def __init__(
        self,
        source: ConnectionSource,
        tenant_id: str,
        *,
        prefix: str,
        namespace: str = "default",
        lock: threading.RLock | None = None,
        max_chars: int = 1500,
        overlap: int = 200,
        embeddings: EmbeddingModel | None = None,
        mode: RetrievalMode | None = None,
        fusion: HybridFusion = "evidence",
        lexical_weight: float = LEXICAL_WEIGHT,
    ) -> None:
        super().__init__(max_chars=max_chars, overlap=overlap)
        self.tenant_id = check_tenant(tenant_id)
        self.namespace = check_namespace(namespace)
        if fusion not in HYBRID_FUSIONS:
            raise ValueError(f"fusion must be one of {', '.join(HYBRID_FUSIONS)}, not {fusion!r}")
        if not 0.0 <= lexical_weight <= 1.0:
            raise ValueError("lexical_weight must be between 0 and 1")
        #: How ``hybrid`` combines full text and vectors (``evidence`` or ``rrf``), and the full-text share.
        self.fusion: HybridFusion = fusion
        self.lexical_weight = lexical_weight
        self.source = source
        self._sql = Executor(source, prefix=prefix, tenant_id=tenant_id)
        self._lock = lock if lock is not None else threading.RLock()
        #: The embedding model new chunks are embedded with (``None``: full-text only).
        self.embeddings = embeddings
        self.mode = check_mode(mode) if mode is not None else None
        self._pgvector: bool | None = None

    def _key(self, **params: Any) -> dict[str, Any]:
        return {"tenant_id": self.tenant_id, "namespace": self.namespace, **params}

    def _rows(self, name: str, **params: Any) -> builtins.list[tuple[Any, ...]]:
        return self._sql.execute(name, self._key(**params)).rows

    @property
    def capabilities(self) -> frozenset[str]:
        """``partitioned`` (by tenant) and ``concurrent_writers`` always; ``vectors`` and ``reindex`` with an
        embedding model, as :class:`orkexa.SQLiteIndex` (:attr:`orkexa.Index.capabilities`)."""
        found = {"partitioned", "concurrent_writers"}
        if self.embeddings is not None:
            found |= {"vectors", "reindex"}
        return frozenset(found)

    def close(self) -> None:
        """Nothing to do: the connections belong to the source."""

    def __len__(self) -> int:
        return int(self._rows("chunks_count")[0][0])

    def sources(self) -> builtins.list[str]:
        return [str(row[0]) for row in self._rows("chunks_sources")]

    def add_chunks(self, chunks: Iterable[Chunk]) -> builtins.list[int]:
        kept = [chunk for chunk in chunks if chunk.text.strip()]
        ids: builtins.list[int] = []
        if not kept:
            return ids
        identity = None
        vectors: builtins.list[builtins.list[float]] | None = None
        if self.embeddings is not None:
            refresh_identity(self.embeddings)  # the model's current build, not a cached one (MUP-ONA drift 1)
            identity = self.embeddings.identity()
            vectors = [[float(x) for x in vector] for vector in self.embeddings.embed_documents([c.text for c in kept])]
            if len(vectors) != len(kept):
                raise ValueError("The embedding model must return one vector per text")
            for vector in vectors:
                # NaN or infinity: vector.invalid; another length than the model's: vector.dimension (drift 2).
                validate_vector(vector, identity)
        with self._sql.transaction():
            # The lock a reindex's switch waits for (FOR UPDATE): no chunk is added while it embeds the last ones.
            self._rows("spaces_lock_shared")
            space = self._active_for(identity) if identity is not None else None
            for position, chunk in enumerate(kept):
                row = self._rows(
                    "chunks_insert",
                    source=_text(chunk.source),
                    title=None if chunk.title is None else _text(chunk.title),
                    file_id=chunk.file_id,
                    metadata=jsonb_text(json.dumps(chunk.metadata)),
                    text=_text(chunk.text),
                    search_text=" ".join(search_words(f"{chunk.text} {chunk.title or ''}")),
                )[0]
                ids.append(int(row[0]))
                if vectors is not None and space is not None:
                    self._sql.execute(
                        "vectors_insert", self._key(space=space, chunk_id=int(row[0]), embedding=vectors[position])
                    )
        return ids

    def delete(self, source: str) -> int:
        return self._sql.execute("chunks_delete_source", self._key(source=source)).rowcount

    def search(self, query: str, k: int = 5) -> builtins.list[Hit]:
        """:meth:`retrieve` in the index's default mode: ``hybrid`` with an embedding model, else ``fts``."""
        return self.retrieve(query, k)

    # ---- vectors ------------------------------------------------------------------------------------------------

    def _space(self, state: str) -> tuple[str, EmbeddingIdentity] | None:
        rows = self._rows("spaces_by_state", state=state)
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
        self._sql.execute(
            "spaces_insert_active",
            self._key(space=space, identity=json.dumps(identity.to_dict()), dimensions=identity.dimensions),
        )
        return space

    def _count_vectors(self, space: str) -> int:
        return int(self._rows("vectors_count", space=space)[0][0])

    def pgvector(self) -> bool:
        """Whether the database ranks vectors itself (pgvector is installed). Detected once; :meth:`refresh` detects
        it again."""
        if self._pgvector is None:
            rows = self._sql.execute("vectors_pgvector", {}).rows
            self._pgvector = bool(rows and rows[0][0])
        return self._pgvector

    def refresh(self) -> None:
        """Forget what was detected about the database (pgvector), so the next search detects it again: after an
        administrator installs (``CREATE EXTENSION vector``) or removes pgvector while the server runs."""
        self._pgvector = None

    def vector_status(self) -> PostgresVectorStatus:
        """The vector state of this namespace, with the active space's vectors searches leave out (drift 10)."""
        active = self._space("active")
        building = self._space("building")
        excluded = self._count_excluded(active[0]) if active else 0
        return PostgresVectorStatus(
            chunks=len(self),
            active=active[1].to_dict() if active else None,
            embedded=self._count_vectors(active[0]) if active else 0,
            building=building[1].to_dict() if building else None,
            building_embedded=self._count_vectors(building[0]) if building else 0,
            legacy=excluded > 0,
            excluded=excluded,
        )

    def _count_excluded(self, space: str) -> int:
        """Vectors of ``space`` that searches leave out (NaN, infinity, all zeros)."""
        return int(self._rows("vectors_count_excluded", space=space)[0][0])

    def _discard(self, space: str) -> None:
        self._sql.execute("spaces_delete", self._key(space=space))

    def discard_reindex(self) -> None:
        building = self._space("building")
        if building is not None:
            self._discard(building[0])

    def drop_vectors(self) -> int:
        status = self.vector_status()
        self._sql.execute("spaces_delete_all", self._key())
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
        refresh_identity(model)  # the model's current build, not a cached one (MUP-ONA drift 1)
        identity = model.identity()
        space = f"{self.namespace}:{identity.key}"
        active = self._space("active")
        if active is None or active[0] != space:
            building = self._space("building")
            if building is not None and building[0] != space:
                self._discard(building[0])
            self._sql.execute(
                "spaces_insert_building",
                self._key(space=space, identity=json.dumps(identity.to_dict()), dimensions=identity.dimensions),
            )
        total = len(self)
        # Unlocked rounds: embed what exists at the start of each round (high-water mark), so writers that keep
        # adding chunks cannot keep a round from ending. Stop once at most one batch is left (or after a few
        # rounds); the switch below embeds the rest.
        for _round in range(_REINDEX_ROUNDS):
            high = self._rows("chunks_max_id")[0][0]
            while True:
                if cancel is not None and cancel.is_set():
                    raise RetrievalError("cancelled", "The reindex was cancelled; start it again to continue")
                rows = self._rows("chunks_missing_up_to", space=space, high=int(high), limit=batch_size)
                if not rows:
                    break
                vectors = embed_batch(model, identity, rows)
                with self._sql.transaction():
                    self._store_batch(space, rows, vectors)
                if progress is not None:
                    progress(self._count_vectors(space), total)
            if self._count_missing(space) <= batch_size:
                break
        # The switch: under a lock on the namespace's spaces that every write takes too (add_chunks, FOR SHARE), so no
        # chunk can be added between embedding the last ones and switching. Before, a chunk written in that moment
        # made the switch fail (vector_incomplete) and a reindex under steady writes could fail every time.
        changed = False
        with self._sql.transaction():
            self._rows("spaces_lock_exclusive")
            limit = max(batch_size, _REINDEX_SWITCH_CHUNKS)
            rows = self._rows("chunks_missing", space=space, limit=limit + 1)
            if len(rows) > limit:
                # Writes outpace the model: embedding more while every write waits would stall them; try later.
                missing = self._count_missing(space)
                raise RetrievalError("vector_incomplete", f"{missing} chunks were added during the reindex")
            # What the last round left (what writers added meanwhile), embedded while writes wait. One batch was not
            # enough: under load writers add more than a batch during a round, and every attempt failed (T-PART-CC).
            for start in range(0, len(rows), batch_size):
                if cancel is not None and cancel.is_set():
                    raise RetrievalError("cancelled", "The reindex was cancelled; start it again to continue")
                part = rows[start : start + batch_size]
                self._store_batch(space, part, embed_batch(model, identity, part))
            refresh_identity(model)
            changed = model.identity().key != identity.key
            if not changed:
                self._sql.execute("spaces_delete_others", self._key(space=space))
                self._sql.execute("spaces_activate", self._key(space=space))
        if changed:
            # The model changed on its server while it embedded: the new space may mix two builds.
            self._discard(space)
            raise RetrievalError(
                "reindex_required",
                f"{identity.model} changed during the reindex; its new vectors were discarded, start it again",
            )
        if progress is not None:
            progress(self._count_vectors(space), total)
        self.embeddings = model
        return self.vector_status()

    def _count_missing(self, space: str) -> int:
        return int(self._rows("chunks_count_missing", space=space)[0][0])

    def _store_batch(
        self, space: str, rows: builtins.list[tuple[Any, ...]], vectors: builtins.list[builtins.list[float]]
    ) -> None:
        """Store a batch of vectors (inside a transaction of the caller)."""
        for (chunk_id, _), vector in zip(rows, vectors, strict=True):
            # Only while the chunk still exists: one deleted while its batch was embedded gets no vector (no foreign
            # key error that ends the reindex, drift 9).
            self._sql.execute("vectors_store", self._key(space=space, embedding=vector, chunk_id=int(chunk_id)))

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
        if sources is None:
            rows = self._rows("search_fts", query=expression, limit=k)
        else:
            rows = self._rows("search_fts_sources", query=expression, sources=sources, limit=k)
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
        if self.pgvector():
            literal = pgvector_literal(vector)
            if sources is None:
                rows = self._rows("search_pgvector", vector=literal, space=space, limit=k)
            else:
                rows = self._rows("search_pgvector_sources", vector=literal, space=space, sources=sources, limit=k)
            return self._hits(rows)
        if sources is None:
            candidates = self._rows("search_candidates", space=space)
        else:
            candidates = self._rows("search_candidates_sources", space=space, sources=sources)
        # The cosine similarity, as pgvector's <=> gives it, whatever the length of the vectors: a model whose
        # identity is not ``normalized`` returns vectors of any length, where the dot product is no cosine (drift 4).
        # math.fsum is exactly rounded, so the result is the same on every Python version.
        query_norm = math.sqrt(math.fsum(x * x for x in vector))
        # Rounded to 6 digits as the contract says (drift 5); equal scores go to the lower chunk id, as in the SQL.
        similarity = [
            (round(cosine(vector, query_norm, [float(x) for x in embedding]), 6), int(chunk_id))
            for chunk_id, embedding in candidates
        ]
        scored = sorted(similarity, key=lambda item: (-item[0], item[1]))[:k]
        if not scored:
            return []
        rows = self._rows("search_hits", ids=[chunk_id for _, chunk_id in scored])
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
        if allowed is not None and len(allowed) > MAX_SOURCES:
            raise ValueError(f"At most {MAX_SOURCES} sources per search")  # as orkexa.SQLiteIndex (drift 7)
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
        :func:`_word_queries`) each contains, weighted by how many chunks of this namespace contain them."""
        queries = _word_queries(query)
        words, expressions = builtins.list(queries), builtins.list(queries.values())
        found: dict[int, builtins.list[str]] = {chunk_id: [] for chunk_id in chunk_ids}
        if not words:
            return lexical_evidence({}, found)
        counts = self._rows("search_word_counts", words=words, queries=expressions)
        total = len(self)
        weights = {str(word): word_weight(int(count), total) for word, count in counts}
        if chunk_ids:
            rows = self._rows("search_word_hits", words=words, queries=expressions, ids=chunk_ids)
            for chunk_id, word in rows:
                found[int(chunk_id)].append(str(word))
        return lexical_evidence(weights, found)
