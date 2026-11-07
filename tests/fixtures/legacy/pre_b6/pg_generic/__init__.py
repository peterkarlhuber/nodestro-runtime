# nodestro-runtime git 76f9cb2, src/nodestro_runtime/pg_generic/__init__.py: the generic PostgreSQL part
# before MUP-ONA MP-B6, unchanged below this header. Part of the "old state" of the rollback scenario U3 after B6
# (tests/test_upgrade_legacy.py, T-UPG-3), loaded only in a subprocess as nodestro_runtime.pg_generic. Never edit.
"""The generic PostgreSQL part of Nodestro's knowledge index, file store and memory (MUP-ONA B4).

Everything here is meant to be taken over into orkexa unchanged (``orkexa.backends.postgres``, MUP-ONA B5): it imports
nothing of Nodestro (an import-linter contract in ``pyproject.toml`` and ``tests/test_sql_catalog.py`` keep it so),
gets its connections only through a :class:`~nodestro_runtime.pg_generic.connection.ConnectionSource`, and runs SQL
only through the statement catalog of :mod:`nodestro_runtime.pg_generic._sql` (named statements, tables rendered from
a prefix, the partition guard). psycopg is imported only when a statement is rendered.

Modules: :mod:`.connection` (the protocol), :mod:`._sql` (catalog, executor, guard), :mod:`._vector` (vector helpers),
:mod:`.index` (:class:`PostgresIndex`), :mod:`.files` (:class:`PostgresFileStore`), :mod:`.memory`
(:class:`PostgresMemory`). Nodestro's wiring and its own additions (chunk counts, attaching files, a source's text)
are in :mod:`nodestro_runtime.postgres_tables`.
"""

from __future__ import annotations

from ._sql import CATALOG, PREFIX, Executor, Result, Statement, check_prefix, render
from .connection import MODES, Connection, ConnectionSource, Cursor, TransactionMode
from .files import PostgresFileStore
from .index import MAX_SOURCES, PostgresIndex, PostgresVectorStatus, search_words
from .memory import PostgresMemory

__all__ = [
    "CATALOG",
    "MAX_SOURCES",
    "MODES",
    "PREFIX",
    "Connection",
    "ConnectionSource",
    "Cursor",
    "Executor",
    "PostgresFileStore",
    "PostgresIndex",
    "PostgresMemory",
    "PostgresVectorStatus",
    "Result",
    "Statement",
    "TransactionMode",
    "check_prefix",
    "render",
    "search_words",
]
