# nodestro-runtime git 76f9cb2, src/nodestro_runtime/pg_generic/connection.py: the generic PostgreSQL part
# before MUP-ONA MP-B6, unchanged below this header. Part of the "old state" of the rollback scenario U3 after B6
# (tests/test_upgrade_legacy.py, T-UPG-3), loaded only in a subprocess as nodestro_runtime.pg_generic. Never edit.
"""Where the generic PostgreSQL classes get their connections: the :class:`ConnectionSource` protocol (MUP-ONA B4).

The generic part (:mod:`nodestro_runtime.pg_generic`) never opens, pools or locks a connection itself. A host passes
a :class:`ConnectionSource`, and every statement runs inside one of its transactions. The protocol has one method,
:meth:`ConnectionSource.transaction`, a context manager for one transaction that yields a :class:`Connection`
(anything with psycopg's ``execute``; a psycopg connection is one).

``mode`` says what the transaction will hold, so a source can run it the cheapest way it knows without changing its
meaning:

* ``"read"``: reads only. A source may run each statement on its own (autocommit, each with its own snapshot) and
  repeat a read whose connection was lost;
* ``"write"``: one statement that changes rows. It is atomic on its own, so a source may run it in autocommit; it is
  never repeated (it may have been applied);
* ``"transaction"``: several statements, all applied or none (``BEGIN`` ... ``COMMIT``; ``ROLLBACK`` when the block
  raises).

A source that is already inside a transaction of the calling thread joins it in every mode. ``tenant_id`` is the
tenant the statements run for. It is the optional hook for row-level security: a source that sets
``set_config('app.tenant_id', <tenant>, true)`` does so at the start of the transaction (transaction-local, so it
must open a transaction even for ``read`` and ``write``). Nodestro's own tables do not use row-level security yet
(MUP-ONA A9), so Nodestro's adapter does not set it by default.

This module imports nothing but the standard library: the generic part is taken over into orkexa
(``orkexa.backends.postgres.connection``, MUP-ONA B5), where the psycopg adapter lives next to it.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Any, Literal, Protocol, runtime_checkable

__all__ = ["MODES", "Connection", "ConnectionSource", "Cursor", "TransactionMode"]

#: What one transaction of a :class:`ConnectionSource` holds (see the module docstring).
TransactionMode = Literal["read", "write", "transaction"]
MODES: frozenset[str] = frozenset({"read", "write", "transaction"})


class Cursor(Protocol):
    """What :meth:`Connection.execute` returns (a psycopg cursor is one)."""

    @property
    def rowcount(self) -> int:
        """Rows the statement returned or changed."""
        ...

    def fetchone(self) -> Any:
        """The next row, or ``None``."""
        ...

    def fetchall(self) -> Sequence[Any]:
        """The remaining rows."""
        ...


class Connection(Protocol):
    """A connection inside one transaction (a psycopg connection is one)."""

    def execute(self, query: Any, params: Any = None) -> Cursor:
        """Run ``query`` (text or a ``psycopg.sql.Composable``) with ``params`` bound, never interpolated."""
        ...


@runtime_checkable
class ConnectionSource(Protocol):
    """Gives the generic PostgreSQL classes their transactions (see the module docstring)."""

    def transaction(
        self, mode: TransactionMode = "transaction", *, tenant_id: str | None = None
    ) -> AbstractContextManager[Connection]:
        """A context manager for one transaction of the kind ``mode``, for the statements of ``tenant_id``."""
        ...
