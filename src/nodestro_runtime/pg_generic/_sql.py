"""The statement catalog of the generic PostgreSQL part, its executor and the partition guard (MUP-ONA B4).

Every SQL text of the index, the file store and the memory is a named :class:`Statement` of :data:`CATALOG`: its
template names the tables as ``{chunks}``, ``{vectors}``, ``{spaces}``, ``{files}`` and ``{memory}`` and binds every
value as a named psycopg placeholder (``%(tenant_id)s``); variants (with or without a source filter, ranked by
pgvector or in Python) are separate entries. A template is rendered with ``psycopg.sql.Identifier(prefix + table)``
for a prefix that matches :data:`PREFIX` (``nodestro_`` for Nodestro's tables), never with a value in the text: values
are only bound (no ``sql.Literal``).

:class:`Executor` is the only place that runs a statement (``tests/test_sql_catalog.py`` checks that no other module
of the package calls ``execute``). It runs catalog names only, with exactly the parameters the entry lists, and its
**partition guard** refuses a ``partitioned`` statement unless ``tenant_id`` is bound to a non-empty string of at most
:data:`MAX_TENANT_CHARS` characters that is the executor's own tenant: :class:`~orkexa.retrieval.RetrievalError`
``partition.missing``, raised before any database access.

The tenant SQL check proves every rendered entry bound to one tenant (``tests/test_sql_catalog.py``, T-CAT-1/2) and
that no statement of the source before the catalog was lost (``tests/test_sql_parity.py``, T-PAR-1).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
import re
import string
import threading
from typing import TYPE_CHECKING, Any, Literal

from orkexa.retrieval import RetrievalError

from .connection import Connection, ConnectionSource, TransactionMode

if TYPE_CHECKING:
    from psycopg import sql

__all__ = [
    "CATALOG",
    "MAX_TENANT_CHARS",
    "PREFIX",
    "TABLES",
    "Executor",
    "Result",
    "Statement",
    "check_namespace",
    "check_prefix",
    "check_tenant",
    "jsonb_text",
    "render",
]

#: A table prefix: a lower-case ASCII letter, then up to 30 lower-case letters, digits or ``_``, ending in ``_``.
PREFIX = re.compile(r"^[a-z][a-z0-9_]{0,30}_$")
#: The table of each placeholder of a template (the table's name is the prefix plus this).
TABLES: Mapping[str, str] = {
    "chunks": "chunks",
    "vectors": "chunk_vectors",
    "spaces": "vector_spaces",
    "files": "files",
    "memory": "memory_files",
}
#: The longest tenant id the partition guard lets through.
MAX_TENANT_CHARS = 128
#: The partition column; a ``partitioned`` statement binds it as the parameter of the same name.
PARTITION = "tenant_id"

#: What a statement does: reads rows, writes (inserts or updates) rows, or deletes rows.
Kind = Literal["read", "write", "delete"]
#: A statement name: ``<group>_<what>`` in snake case (no dot, so it never looks like a problem code).
_NAME = re.compile(r"[a-z][a-z0-9]*_[a-z0-9_]+")
_PARAM = re.compile(r"%\((\w+)\)s")
_POSITIONAL = re.compile(r"%[sbt]")

#: Vectors a search may use: no NaN or infinity (stored before vectors were checked; pgvector refuses to cast them),
#: and not all zeros (no direction, no cosine). Braces are doubled for ``psycopg.sql.SQL.format``.
_USABLE = (
    "NOT (v.embedding && '{{NaN,Infinity,-Infinity}}'::real[])"
    " AND v.embedding <> array_fill(0::real, ARRAY[cardinality(v.embedding)])"
)
#: Chunks of a namespace without a vector in a space.
_MISSING = (
    "SELECT c.id, c.text FROM {chunks} c WHERE c.tenant_id = %(tenant_id)s AND c.namespace = %(namespace)s AND NOT"
    " EXISTS (SELECT 1 FROM {vectors} v WHERE v.tenant_id = %(tenant_id)s AND v.space = %(space)s"
    " AND v.chunk_id = c.id)"
)
_PGVECTOR = (
    "SELECT c.id, c.source, c.title, c.file_id, c.metadata::text, c.text,"
    " round((1 - (v.embedding::vector <=> %(vector)s::vector))::numeric, 6)::float8 AS score"
    " FROM {vectors} v JOIN {chunks} c ON c.id = v.chunk_id"
    " WHERE v.tenant_id = %(tenant_id)s AND v.space = %(space)s AND c.tenant_id = %(tenant_id)s"
    " AND c.namespace = %(namespace)s"
)
_CANDIDATES = (
    "SELECT c.id, v.embedding FROM {vectors} v JOIN {chunks} c ON c.id = v.chunk_id"
    " WHERE v.tenant_id = %(tenant_id)s AND v.space = %(space)s AND c.tenant_id = %(tenant_id)s"
    " AND c.namespace = %(namespace)s"
)
_FTS = (
    "SELECT id, source, title, file_id, metadata::text, text FROM {chunks}, to_tsquery('simple', %(query)s) AS query"
    " WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s AND search @@ query"
)
_SPACE_ROW = (
    "INSERT INTO {spaces} (tenant_id, namespace, space, identity, dimensions, state)"
    " VALUES (%(tenant_id)s, %(namespace)s, %(space)s, %(identity)s::jsonb, %(dimensions)s, "
)


@dataclass(frozen=True)
class Statement:
    """One entry of the catalog: a SQL template with named parameters.

    ``params`` lists every placeholder of the template (each bound by name, so a parameter used twice is bound
    once); ``kind`` says whether it reads, writes or deletes; ``partitioned`` whether it touches tenant rows (then
    ``tenant_id`` is one of its parameters and the partition guard checks it). ``rows``: whether it returns rows
    (every read, and a write with ``RETURNING``)."""

    name: str
    template: str
    params: tuple[str, ...]
    kind: Kind
    partitioned: bool = True
    rows: bool = False

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"Invalid statement name {self.name!r}")
        used = _PARAM.findall(self.template)
        if set(used) != set(self.params) or len(set(self.params)) != len(self.params):
            raise ValueError(
                f"Statement {self.name!r}: the parameters {self.params} are not the template's {sorted(set(used))}"
            )
        if _POSITIONAL.search(_PARAM.sub("", self.template.replace("%%", ""))):
            raise ValueError(f"Statement {self.name!r}: positional placeholders are not allowed")
        if self.partitioned != (PARTITION in self.params):
            raise ValueError(f"Statement {self.name!r}: {PARTITION} is a parameter exactly when it is partitioned")
        if not set(self.tables) <= set(TABLES):
            raise ValueError(f"Statement {self.name!r}: unknown table placeholder in {sorted(self.tables)}")
        if self.kind == "read" and not self.rows:
            object.__setattr__(self, "rows", True)

    @property
    def tables(self) -> frozenset[str]:
        """The table placeholders of the template (``chunks``, ``vectors`` ...)."""
        return frozenset(name for _, name, _, _ in string.Formatter().parse(self.template) if name)

    @property
    def mode(self) -> TransactionMode:
        """How the statement runs on its own (outside :meth:`Executor.transaction`): a read as ``read``, a write that
        returns rows in a ``transaction``, any other write as ``write``."""
        if self.kind == "read":
            return "read"
        return "transaction" if self.rows else "write"


def _p(*names: str) -> tuple[str, ...]:
    return (PARTITION, "namespace", *names)


_ENTRIES = (
    # ---- file store (files) -------------------------------------------------------------------------------------
    Statement(
        "files_update_entry",
        "UPDATE {files} SET entry = %(entry)s::jsonb"
        " WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s AND id = %(id)s",
        _p("entry", "id"),
        "write",
    ),
    Statement(
        "files_upsert",
        "INSERT INTO {files} (tenant_id, namespace, id, entry, data)"
        " VALUES (%(tenant_id)s, %(namespace)s, %(id)s, %(entry)s::jsonb, %(data)s)"
        " ON CONFLICT (tenant_id, namespace, id) DO UPDATE SET entry = excluded.entry, data = excluded.data",
        _p("id", "entry", "data"),
        "write",
    ),
    Statement(
        "files_entry",
        "SELECT entry::text FROM {files} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s AND id = %(id)s",
        _p("id"),
        "read",
    ),
    Statement(
        "files_data",
        "SELECT data FROM {files} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s AND id = %(id)s",
        _p("id"),
        "read",
    ),
    Statement(
        "files_entries",
        "SELECT entry::text FROM {files} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s",
        _p(),
        "read",
    ),
    Statement(
        "files_delete",
        "DELETE FROM {files} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s AND id = %(id)s",
        _p("id"),
        "delete",
    ),
    # ---- index: chunks ------------------------------------------------------------------------------------------
    Statement(
        "chunks_count",
        "SELECT count(*) FROM {chunks} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s",
        _p(),
        "read",
    ),
    Statement(
        "chunks_sources",
        "SELECT DISTINCT source FROM {chunks} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s"
        " ORDER BY source",
        _p(),
        "read",
    ),
    Statement(
        "chunks_insert",
        "INSERT INTO {chunks} (tenant_id, namespace, source, title, file_id, metadata, text, search_text)"
        " VALUES (%(tenant_id)s, %(namespace)s, %(source)s, %(title)s, %(file_id)s, %(metadata)s::jsonb, %(text)s,"
        " %(search_text)s) RETURNING id",
        _p("source", "title", "file_id", "metadata", "text", "search_text"),
        "write",
        rows=True,
    ),
    Statement(
        "chunks_delete_source",
        "DELETE FROM {chunks} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s AND source = %(source)s",
        _p("source"),
        "delete",
    ),
    Statement(
        "chunks_max_id",
        "SELECT coalesce(max(id), 0) FROM {chunks} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s",
        _p(),
        "read",
    ),
    Statement(
        "chunks_missing_up_to",
        _MISSING + " AND c.id <= %(high)s ORDER BY c.id LIMIT %(limit)s",
        _p("space", "high", "limit"),
        "read",
    ),
    Statement("chunks_missing", _MISSING + " ORDER BY c.id LIMIT %(limit)s", _p("space", "limit"), "read"),
    Statement(
        "chunks_count_missing",
        "SELECT count(*) FROM {chunks} c WHERE c.tenant_id = %(tenant_id)s AND c.namespace = %(namespace)s AND NOT"
        " EXISTS (SELECT 1 FROM {vectors} v WHERE v.tenant_id = %(tenant_id)s AND v.space = %(space)s"
        " AND v.chunk_id = c.id)",
        _p("space"),
        "read",
    ),
    # ---- index: vector spaces -----------------------------------------------------------------------------------
    Statement(
        "spaces_lock_shared",
        "SELECT space FROM {spaces} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s ORDER BY space"
        " FOR SHARE",
        _p(),
        "read",
    ),
    Statement(
        "spaces_lock_exclusive",
        "SELECT space FROM {spaces} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s ORDER BY space"
        " FOR UPDATE",
        _p(),
        "read",
    ),
    Statement(
        "spaces_by_state",
        "SELECT space, identity::text FROM {spaces}"
        " WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s AND state = %(state)s",
        _p("state"),
        "read",
    ),
    Statement(
        "spaces_insert_active",
        _SPACE_ROW + "'active')",
        _p("space", "identity", "dimensions"),
        "write",
    ),
    Statement(
        "spaces_insert_building",
        _SPACE_ROW + "'building') ON CONFLICT (tenant_id, namespace, space) DO NOTHING",
        _p("space", "identity", "dimensions"),
        "write",
    ),
    Statement(
        "spaces_activate",
        "UPDATE {spaces} SET state = 'active' WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s"
        " AND space = %(space)s",
        _p("space"),
        "write",
    ),
    Statement(
        "spaces_delete",
        "DELETE FROM {spaces} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s AND space = %(space)s",
        _p("space"),
        "delete",
    ),
    Statement(
        "spaces_delete_others",
        "DELETE FROM {spaces} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s AND space <> %(space)s",
        _p("space"),
        "delete",
    ),
    Statement(
        "spaces_delete_all",
        "DELETE FROM {spaces} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s",
        _p(),
        "delete",
    ),
    # ---- index: vectors -----------------------------------------------------------------------------------------
    Statement(
        "vectors_insert",
        "INSERT INTO {vectors} (tenant_id, namespace, space, chunk_id, embedding)"
        " VALUES (%(tenant_id)s, %(namespace)s, %(space)s, %(chunk_id)s, %(embedding)s)",
        _p("space", "chunk_id", "embedding"),
        "write",
    ),
    Statement(
        "vectors_store",
        "INSERT INTO {vectors} (tenant_id, namespace, space, chunk_id, embedding)"
        " SELECT c.tenant_id, c.namespace, %(space)s, c.id, %(embedding)s FROM {chunks} c"
        " WHERE c.tenant_id = %(tenant_id)s AND c.namespace = %(namespace)s AND c.id = %(chunk_id)s"
        " ON CONFLICT (tenant_id, space, chunk_id) DO UPDATE SET embedding = excluded.embedding",
        _p("space", "embedding", "chunk_id"),
        "write",
    ),
    Statement(
        "vectors_count",
        "SELECT count(*) FROM {vectors} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s"
        " AND space = %(space)s",
        _p("space"),
        "read",
    ),
    Statement(
        "vectors_count_excluded",
        "SELECT count(*) FROM {vectors} v WHERE v.tenant_id = %(tenant_id)s AND v.namespace = %(namespace)s"
        " AND v.space = %(space)s AND NOT (" + _USABLE + ")",
        _p("space"),
        "read",
    ),
    Statement("vectors_pgvector", "SELECT to_regtype('vector') IS NOT NULL", (), "read", partitioned=False),
    # ---- index: searches ----------------------------------------------------------------------------------------
    Statement(
        "search_fts",
        _FTS + " ORDER BY ts_rank(search, query, 1) DESC, id LIMIT %(limit)s",
        _p("query", "limit"),
        "read",
    ),
    Statement(
        "search_fts_sources",
        _FTS + " AND source = ANY(%(sources)s) ORDER BY ts_rank(search, query, 1) DESC, id LIMIT %(limit)s",
        _p("query", "sources", "limit"),
        "read",
    ),
    Statement(
        "search_pgvector",
        _PGVECTOR + " AND " + _USABLE + " ORDER BY score DESC, c.id LIMIT %(limit)s",
        _p("vector", "space", "limit"),
        "read",
    ),
    Statement(
        "search_pgvector_sources",
        _PGVECTOR + " AND c.source = ANY(%(sources)s) AND " + _USABLE + " ORDER BY score DESC, c.id LIMIT %(limit)s",
        _p("vector", "space", "sources", "limit"),
        "read",
    ),
    Statement("search_candidates", _CANDIDATES + " AND " + _USABLE, _p("space"), "read"),
    Statement(
        "search_candidates_sources",
        _CANDIDATES + " AND c.source = ANY(%(sources)s) AND " + _USABLE,
        _p("space", "sources"),
        "read",
    ),
    Statement(
        "search_hits",
        "SELECT id, source, title, file_id, metadata::text, text FROM {chunks}"
        " WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s AND id = ANY(%(ids)s)",
        _p("ids"),
        "read",
    ),
    Statement(
        "search_word_counts",
        "SELECT t.w, (SELECT count(*) FROM {chunks} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s"
        " AND search @@ to_tsquery('simple', t.q)) FROM unnest(%(words)s::text[], %(queries)s::text[]) AS t(w, q)",
        _p("words", "queries"),
        "read",
    ),
    Statement(
        "search_word_hits",
        "SELECT c.id, t.w FROM {chunks} c, unnest(%(words)s::text[], %(queries)s::text[]) AS t(w, q)"
        " WHERE c.tenant_id = %(tenant_id)s AND c.namespace = %(namespace)s AND c.id = ANY(%(ids)s)"
        " AND c.search @@ to_tsquery('simple', t.q)",
        _p("words", "queries", "ids"),
        "read",
    ),
    # ---- memory (memory) ----------------------------------------------------------------------------------------
    Statement(
        "memory_read",
        "SELECT content FROM {memory} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s"
        " AND path = %(path)s",
        _p("path"),
        "read",
    ),
    Statement(
        "memory_write",
        "INSERT INTO {memory} (tenant_id, namespace, path, content)"
        " VALUES (%(tenant_id)s, %(namespace)s, %(path)s, %(content)s)"
        " ON CONFLICT (tenant_id, namespace, path) DO UPDATE SET content = excluded.content,"
        " updated_at = clock_timestamp()",
        _p("path", "content"),
        "write",
    ),
    Statement(
        "memory_files",
        "SELECT path, octet_length(content) FROM {memory}"
        " WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s AND starts_with(path, %(start)s)",
        _p("start"),
        "read",
    ),
    Statement(
        "memory_delete",
        "DELETE FROM {memory} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s"
        " AND (path = %(path)s OR starts_with(path, %(under)s))",
        _p("path", "under"),
        "delete",
    ),
    Statement(
        "memory_rename",
        "UPDATE {memory} SET path = %(new)s || substr(path, %(cut)s), updated_at = clock_timestamp()"
        " WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s"
        " AND (path = %(old)s OR starts_with(path, %(under)s))",
        _p("new", "cut", "old", "under"),
        "write",
    ),
    Statement(
        "memory_clear",
        "DELETE FROM {memory} WHERE tenant_id = %(tenant_id)s AND namespace = %(namespace)s",
        _p(),
        "delete",
    ),
)


def _catalog(entries: tuple[Statement, ...]) -> Mapping[str, Statement]:
    found: dict[str, Statement] = {}
    for entry in entries:
        if entry.name in found:
            raise ValueError(f"Duplicate statement {entry.name!r}")
        found[entry.name] = entry
    return found


#: Every statement of the generic part, by name.
CATALOG: Mapping[str, Statement] = _catalog(_ENTRIES)


def check_prefix(prefix: object) -> str:
    """``prefix`` if it is a valid table prefix (:data:`PREFIX`), else :class:`ValueError`."""
    if not isinstance(prefix, str) or not PREFIX.fullmatch(prefix):
        raise ValueError(f"Invalid table prefix {prefix!r}: it must match {PREFIX.pattern}")
    return prefix


def check_tenant(tenant_id: object) -> str:
    """``tenant_id`` if it is a tenant the guard lets through (a string, not empty or only whitespace, at most
    :data:`MAX_TENANT_CHARS` characters), else :class:`ValueError`."""
    if not isinstance(tenant_id, str) or not _usable_tenant(tenant_id):
        raise ValueError(f"Invalid tenant id {tenant_id!r}")
    return tenant_id


def check_namespace(namespace: str) -> str:
    """``namespace`` if it is not empty, else :class:`ValueError`."""
    if not namespace:
        raise ValueError("namespace must not be empty")
    return namespace


def _usable_tenant(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_TENANT_CHARS


@lru_cache(maxsize=512)
def _rendered(name: str, prefix: str) -> sql.Composed:
    from psycopg import sql as psql

    entry = CATALOG[name]
    tables = {key: psql.Identifier(prefix + TABLES[key]) for key in entry.tables}
    return psql.SQL(entry.template).format(**tables)


def render(name: str, prefix: str) -> sql.Composed:
    """The statement ``name`` for the tables of ``prefix``: ``psycopg.sql`` with the table names as identifiers."""
    if name not in CATALOG:
        raise ValueError(f"Unknown statement {name!r}")
    return _rendered(name, check_prefix(prefix))


def jsonb_text(text: str) -> str:
    """JSON text for a ``jsonb`` column: jsonb cannot hold U+0000, so an escaped one (``\\u0000``) becomes U+FFFD."""
    if "\\u0000" not in text:
        return text
    import json

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("\x00", "�")
        if isinstance(value, dict):
            return {clean(key): clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return json.dumps(clean(json.loads(text)))


@dataclass(frozen=True)
class Result:
    """What a statement gave: its rows (a statement that returns rows) and how many rows it returned or changed."""

    rows: list[tuple[Any, ...]]
    rowcount: int


class Executor:
    """Runs the catalog's statements for one tenant and one table prefix on a :class:`ConnectionSource`.

    :meth:`execute` runs one statement on its own (in the mode of :attr:`Statement.mode`), or on the connection of an
    open :meth:`transaction` of the calling thread. Nothing else of the generic part runs SQL."""

    def __init__(self, source: ConnectionSource, *, prefix: str, tenant_id: str) -> None:
        self.source = source
        self.prefix = check_prefix(prefix)
        self.tenant_id = check_tenant(tenant_id)
        self._local = threading.local()

    def _guard(self, entry: Statement, params: Mapping[str, Any]) -> None:
        value = params.get(PARTITION)
        if not _usable_tenant(value) or value != self.tenant_id:
            raise RetrievalError(
                "partition.missing", f"Statement {entry.name!r} is not bound to the tenant of this instance"
            )

    def execute(self, name: str, params: Mapping[str, Any]) -> Result:
        """Run the catalog statement ``name`` with ``params`` (exactly the entry's parameters, bound by name)."""
        entry = CATALOG.get(name)
        if entry is None:
            raise ValueError(f"Unknown statement {name!r}")
        if entry.partitioned:
            self._guard(entry, params)
        if set(params) != set(entry.params):
            raise ValueError(
                f"Statement {name!r}: expected the parameters {sorted(entry.params)}, got {sorted(params)}"
            )
        query = _rendered(name, self.prefix)
        connection: Connection | None = getattr(self._local, "connection", None)
        if connection is not None:
            return self._run(entry, connection, query, params)
        with self.source.transaction(entry.mode, tenant_id=self.tenant_id) as opened:
            return self._run(entry, opened, query, params)

    @staticmethod
    def _run(entry: Statement, connection: Connection, query: Any, params: Mapping[str, Any]) -> Result:
        cursor = connection.execute(query, dict(params))
        rows = list(cursor.fetchall()) if entry.rows else []
        return Result(rows, int(cursor.rowcount))

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the statements of the block in one transaction of the source (reentrant within the thread)."""
        if getattr(self._local, "connection", None) is not None:
            yield
            return
        with self.source.transaction("transaction", tenant_id=self.tenant_id) as connection:
            self._local.connection = connection
            try:
                yield
            finally:
                self._local.connection = None
