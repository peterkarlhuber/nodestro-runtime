"""Measure FX-RANK-0 on :class:`nodestro_runtime.postgres_tables.PostgresIndex` (PostgreSQL A or B).

The corpus, the queries and the expected results live in ``FX-RANK-0.json``; its generator and the SQLite
measurement are in orkexa (``tests/fixtures/ranking/``), this file is a byte-identical copy. This script measures
the server named by ``--dsn``:

* **pg-a**: PostgreSQL without pgvector (``postgres:17``): the runtime ranks the ``real[]`` vectors (Python path).
* **pg-b**: PostgreSQL with pgvector (``pgvector/pgvector:pg17``): migration ``0010`` creates the extension and the
  database ranks with ``<=>``.

Which one it is follows from :meth:`PostgresIndex.pgvector` (what the code does), checked against ``--backend``.
For each identity (``norm``, ``raw``) a fresh schema is migrated and holds the 200 chunks of one tenant and
namespace (added in corpus order in one ``add_chunks`` call, so chunk ids are 1..200); it is dropped afterwards
(``DROP SCHEMA ... CASCADE`` also drops a pgvector extension that ``0010`` installed into it). Every query runs in every
mode with ``k`` hits; ``default`` passes no mode, ``mode_used`` names the explicit modes whose result equals it.

Usage (from the repository root, a role that may create schemas)::

    python tests/fixtures/ranking/measure_pg.py --dsn postgresql://postgres:pw@localhost:55471/postgres \\
        --backend pg-a --image postgres@sha256:... --out /tmp/pg-a.json

Then merge in orkexa: ``python tests/fixtures/ranking/make_rank_corpus.py merge /tmp/pg-a.json /tmp/pg-b.json`` and
copy ``FX-RANK-0.json`` and ``FX-RANK-0.json.sha256`` back here.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
from typing import Any
import uuid

from orkexa import Chunk, Hit
from orkexa.embeddings import CallableEmbeddings

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "FX-RANK-0.json"
TENANT = "fx-rank-tenant"
NAMESPACE = "fx-rank"


def load_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def corpus_digest(data: dict[str, Any]) -> str:
    """SHA-256 of the corpus part, as ``make_rank_corpus.corpus_digest`` in orkexa computes it."""
    part = {key: data[key] for key in ("chunks", "queries", "identities", "k", "modes")}
    return hashlib.sha256(json.dumps(part, sort_keys=True).encode()).hexdigest()


def embeddings(data: dict[str, Any], identity: str) -> CallableEmbeddings:
    """The fixture's embedding model for ``identity``: looks the vector of each text up in the corpus."""
    field = f"vector_{identity}"
    documents = {chunk["text"]: chunk[field] for chunk in data["chunks"]}
    queries = {query["text"]: query[field] for query in data["queries"]}
    spec = data["identities"][identity]
    return CallableEmbeddings(
        lambda texts: [documents[text] for text in texts],
        query_embedder=lambda texts: [queries[text] for text in texts],
        name=spec["name"],
        dimensions=spec["dimensions"],
        provider=spec["provider"],
        normalized=spec["normalized"],
    )


def chunks(data: dict[str, Any]) -> list[Chunk]:
    return [
        Chunk(text=c["text"], source=c["source"], title=c["title"], metadata={"key": c["key"]}) for c in data["chunks"]
    ]


def hit_row(hit: Hit) -> list[Any]:
    """``[chunk key, id, score as returned, score rounded to 6 places, ranks]`` (scores as ``repr``: NaN stays)."""
    ranks = ",".join(f"{name}={rank}" for name, rank in sorted(hit.ranks.items()))
    return [hit.metadata.get("key"), hit.id, repr(hit.score), repr(round(hit.score, 6)), ranks]


def measure_queries(retrieve: Callable[..., list[Hit]], data: dict[str, Any]) -> dict[str, Any]:
    """Every query in every mode: ``{query id: {mode: {"hits": [...], "mode_used": ...} or {"error": ...}}}``."""
    results: dict[str, Any] = {}
    for query in data["queries"]:
        by_mode: dict[str, Any] = {}
        for mode in data["modes"]:
            try:
                hits = retrieve(
                    query["text"], data["k"], mode=None if mode == "default" else mode, sources=query["sources"]
                )
            except Exception as exc:
                by_mode[mode] = {
                    "error": {"code": getattr(exc, "code", None), "message": str(exc), "type": type(exc).__name__}
                }
            else:
                by_mode[mode] = {"hits": [hit_row(hit) for hit in hits], "mode_used": mode}
        default = by_mode["default"]
        if "hits" in default:
            same = [m for m in ("fts", "vector", "hybrid") if by_mode.get(m, {}).get("hits") == default["hits"]]
            default["mode_used"] = "|".join(same) or "unknown"
        results[query["id"]] = by_mode
    return results


def measure_identity(database: Any, data: dict[str, Any], identity: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(results, notes)`` of one identity in the (migrated, empty) schema of ``database``."""
    from nodestro_runtime.postgres_tables import PostgresIndex

    index = PostgresIndex(database, TENANT, namespace=NAMESPACE, embeddings=embeddings(data, identity))
    ids = index.add_chunks(chunks(data))
    assert ids == list(range(1, len(data["chunks"]) + 1)), "chunk ids must follow the corpus order"
    notes: dict[str, Any] = {
        "index_mode": index.mode,
        "pgvector": index.pgvector(),
        "vector_status": index.vector_status().model_dump(mode="json"),
    }
    return measure_queries(index.retrieve, data), notes


def measure(dsn: str, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """``(runs, notes, server)``: every identity measured in a schema of its own on ``dsn``."""
    from psycopg import sql

    from nodestro_runtime.storage_postgres import PostgresDatabase

    runs: dict[str, Any] = {}
    notes: dict[str, Any] = {}
    server: dict[str, Any] = {}
    for identity in data["identities"]:
        schema = f"fx_rank_{identity}_{uuid.uuid4().hex[:8]}"
        database = PostgresDatabase(dsn, schema=schema)
        try:
            database.migrate()
            runs[identity], notes[identity] = measure_identity(database, data, identity)
            rows = database._query(
                "SELECT current_setting('server_version'),"
                " (SELECT extversion FROM pg_extension WHERE extname = 'vector')"
            )
            server = {"server_version": rows[0][0], "pgvector_version": rows[0][1]}
        finally:
            database.close()
            import psycopg

            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    return runs, notes, server


def _orkexa_sha() -> str | None:
    found = importlib.metadata.distribution("orkexa").read_text("direct_url.json")
    if not found:
        return None
    commit: str | None = json.loads(found).get("vcs_info", {}).get("commit_id")
    return commit


def _runtime_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--dsn", required=True, help="libpq connection string of a role that may create schemas")
    parser.add_argument("--backend", choices=["pg-a", "pg-b"], help="expected mode; fails if the server differs")
    parser.add_argument("--image", default=None, help="the server's container image with digest (recorded)")
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--out", type=Path, required=True, help="result file for make_rank_corpus.py merge")
    args = parser.parse_args(argv)
    data = load_fixture(args.fixture)
    runs, notes, server = measure(args.dsn, data)
    detected = {note["pgvector"] for note in notes.values()}
    if len(detected) != 1:
        raise SystemExit(f"pgvector detection differs between identities: {notes}")
    backend = "pg-b" if detected.pop() else "pg-a"
    if args.backend is not None and backend != args.backend:
        raise SystemExit(f"expected {args.backend}, but the server is measured as {backend}")
    import psycopg

    meta = {
        "backend": backend,
        "class": "nodestro_runtime.postgres_tables.PostgresIndex",
        "image": args.image,
        "notes": notes,
        "orkexa_sha": _orkexa_sha(),
        "psycopg": psycopg.__version__,
        "python": platform.python_version(),
        "runtime_sha": _runtime_sha(),
        **server,
    }
    result = {"corpus_sha256": corpus_digest(data), "results": {backend: {"meta": meta, "runs": runs}}}
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{backend}: {args.out}")


if __name__ == "__main__":
    main()
