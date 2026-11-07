"""The generic PostgreSQL part of Nodestro's knowledge index, file store and memory (MUP-ONA B4).

Everything here is meant to be taken over into orkexa unchanged (``orkexa.backends.postgres``, MUP-ONA B5): it imports
nothing of Nodestro (an import-linter contract in ``pyproject.toml`` and ``tests/test_sql_catalog.py`` keep it so) and
gets its connections only through a :class:`~nodestro_runtime.pg_generic.connection.ConnectionSource`.
"""

from __future__ import annotations

from .connection import MODES, Connection, ConnectionSource, Cursor, TransactionMode

__all__ = ["MODES", "Connection", "ConnectionSource", "Cursor", "TransactionMode"]
