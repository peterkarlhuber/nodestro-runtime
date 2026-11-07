"""Knowledge files, their search index and memory spaces on PostgreSQL (ADR-0002, server profile).

The Orkexa backends Nodestro uses on the desktop (``SQLiteFileStore``, ``SQLiteIndex``, ``SQLiteMemory``) keep
their data in SQLite tables. On a server Nodestro uses Orkexa's PostgreSQL backends
(:mod:`orkexa.backends.postgres`: ``PostgresIndex``, ``PostgresFileStore``, ``PostgresMemory``) on the tables of
migrations ``0002`` and ``0010`` for one tenant and one Orkexa namespace. Every statement binds ``tenant_id``.

Since MUP-ONA B6 this module is only Nodestro's part (B4 separated the generic part, B5 moved it into orkexa): it
wires Orkexa's classes to a :class:`~nodestro_runtime.storage_postgres.PostgresDatabase` (through its
``ConnectionSource`` adapter :class:`~nodestro_runtime.storage_postgres.DatabaseSource` and its process lock, with
Nodestro's tables :data:`LAYOUT` and Nodestro's rule for tenant ids), and adds what Nodestro reads beside the Orkexa
interfaces: chunk counts per source, pointing a source's chunks to its file, a source's indexed text, and the memory
totals of a tenant. Their SQL stays here, where the tenant SQL check reads it from the source; Orkexa's SQL is its
statement catalog, which the check renders with Nodestro's prefix (``tests/test_sql_catalog.py``).
:func:`require_knowledge_schema` is the read-only start check of these tables (``verify_schema``); Nodestro's own
migration runner (:meth:`~nodestro_runtime.storage_postgres.PostgresDatabase.migrate`) still creates them.
"""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any

from orkexa.backends import postgres as orkexa_postgres
from orkexa.backends.postgres import (
    MAX_SOURCES,
    PostgresLayout,
    PostgresVectorStatus,
    SchemaReport,
    search_words,
    verify_schema,
)
from orkexa.embeddings import EmbeddingModel
from orkexa.retrieval import LEXICAL_WEIGHT, HybridFusion, RetrievalMode

from .bedrock import BedrockOperatorConfig
from .knowledge import KnowledgeTables
from .memory_spaces import BrowsableMemory
from .problems import NodestroError
from .storage_postgres import TENANT_ID, DatabaseSource, PostgresDatabase

if TYPE_CHECKING:
    from .server_settings import TenantSecrets
    from .workspace import WorkspaceStorage

#: The prefix of Nodestro's tables (``nodestro_chunks`` ...; migrations ``0002`` and ``0010``).
TABLE_PREFIX = "nodestro_"
#: Nodestro's tables as Orkexa names them (``orkexa.backends.postgres.PostgresLayout``).
LAYOUT = PostgresLayout(TABLE_PREFIX)


def _check(tenant_id: str, namespace: str) -> None:
    if not TENANT_ID.fullmatch(tenant_id):
        raise ValueError(f"Invalid tenant id {tenant_id!r}")
    if not namespace:
        raise ValueError("namespace must not be empty")


class PostgresFileStore(orkexa_postgres.PostgresFileStore):
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
        _check(tenant_id, namespace)
        source = DatabaseSource(database)
        super().__init__(
            source,
            tenant_id,
            layout=LAYOUT,
            namespace=namespace,
            lock=source.lock,  # one lock for every user of the database
            files_api=files_api,
            max_image_bytes=max_image_bytes,
            max_document_bytes=max_document_bytes,
        )
        self.database = database


class PostgresIndex(orkexa_postgres.PostgresIndex):
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
        _check(tenant_id, namespace)
        source = DatabaseSource(database)
        super().__init__(
            source,
            tenant_id,
            layout=LAYOUT,
            namespace=namespace,
            lock=source.lock,
            max_chars=max_chars,
            overlap=overlap,
            embeddings=embeddings,
            mode=mode,
            fusion=fusion,
            lexical_weight=lexical_weight,
        )
        self.database = database

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


class PostgresMemory(orkexa_postgres.PostgresMemory):
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
        _check(tenant_id, namespace)
        source = DatabaseSource(database)
        super().__init__(
            source,
            tenant_id,
            layout=LAYOUT,
            namespace=namespace,
            lock=source.lock,
            max_file_chars=max_file_chars,
            max_view_chars=max_view_chars,
        )
        self.database = database


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


def require_knowledge_schema(database: PostgresDatabase) -> SchemaReport:
    """The start check of the knowledge and memory tables (MUP-ONA B6): Orkexa's read-only
    :func:`~orkexa.backends.postgres.verify_schema` with :data:`LAYOUT`.

    A server calls it after :meth:`~nodestro_runtime.storage_postgres.PostgresDatabase.require_current_schema` and
    before it serves a tenant. It changes nothing and needs no DDL privilege (the runtime role is enough). Returns the
    report (whether pgvector is installed, the schema that holds the tables); raises ``storage.schema_mismatch`` with
    every difference (``problems``) when the tables are not the ones Orkexa's backends read, e.g. a missing index or a
    changed column type: then the server must not start, and nothing was changed."""
    report = verify_schema(DatabaseSource(database), LAYOUT)
    if not report.ok:
        raise NodestroError(
            "storage.schema_mismatch",
            "The knowledge and memory tables of the PostgreSQL database are not the ones this release reads:\n"
            + report.summary()
            + "\nNothing was changed. Restore the tables of migrations 0001-0010 (see the server RUNBOOK)",
            problems=[str(problem) for problem in report.problems],
        )
    return report


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
    "LAYOUT",
    "MAX_SOURCES",
    "TABLE_PREFIX",
    "PostgresFileStore",
    "PostgresIndex",
    "PostgresMemory",
    "PostgresMemorySpace",
    "PostgresMemoryTables",
    "PostgresVectorStatus",
    "knowledge_tables",
    "memory_totals",
    "require_knowledge_schema",
    "search_words",
    "tenant_storage",
]
