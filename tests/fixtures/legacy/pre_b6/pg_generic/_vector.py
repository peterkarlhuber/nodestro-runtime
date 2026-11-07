# nodestro-runtime git 76f9cb2, src/nodestro_runtime/pg_generic/_vector.py: the generic PostgreSQL part
# before MUP-ONA MP-B6, unchanged below this header. Part of the "old state" of the rollback scenario U3 after B6
# (tests/test_upgrade_legacy.py, T-UPG-3), loaded only in a subprocess as nodestro_runtime.pg_generic. Never edit.
"""Vector helpers of the generic PostgreSQL index: embedding batches and ranking in Python (MUP-ONA B4).

Without pgvector the index ranks the stored vectors itself (:func:`cosine`), with the same result as pgvector's
``<=>``. Whether the database has pgvector is detected by the index (``PostgresIndex.pgvector``/``refresh``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import math
from typing import Any

from orkexa.embeddings import EmbeddingIdentity, EmbeddingModel, validate_vector
from orkexa.retrieval import RetrievalError

__all__ = ["cosine", "embed_batch", "pgvector_literal", "refresh_identity"]


def refresh_identity(model: EmbeddingModel) -> None:
    """``model.refresh_identity()``; a model that only has the methods of the protocol (no ``refresh_identity``, e.g.
    a test double that does not subclass :class:`~orkexa.embeddings.EmbeddingModel`) caches nothing to forget."""
    refresh: Callable[[], Any] | None = getattr(model, "refresh_identity", None)
    if refresh is not None:
        refresh()


def cosine(query: list[float], query_norm: float, stored: list[float]) -> float:
    """The cosine similarity of a query vector (with its length) and a stored vector (neither is all zeros)."""
    dot = math.fsum(a * b for a, b in zip(query, stored, strict=True))
    return dot / (query_norm * math.sqrt(math.fsum(x * x for x in stored)))


def embed_batch(
    model: EmbeddingModel, identity: EmbeddingIdentity, rows: Sequence[tuple[Any, ...]]
) -> list[list[float]]:
    """The checked vectors of a batch of ``(chunk id, text)`` rows."""
    vectors = [[float(x) for x in vector] for vector in model.embed_documents([str(row[1]) for row in rows])]
    if len(vectors) != len(rows):
        raise RetrievalError("vector_unavailable", "The embedding model returned a wrong number of vectors")
    for vector in vectors:
        if len(vector) != identity.dimensions:
            raise RetrievalError("vector_unavailable", "The embedding model changed its vector size")
        validate_vector(vector)  # NaN or infinity: vector.invalid (drift 2)
    return vectors


def pgvector_literal(vector: list[float]) -> str:
    """``vector`` as the text of a pgvector value (bound as a parameter and cast with ``::vector``)."""
    return "[" + ",".join(repr(x) for x in vector) + "]"
