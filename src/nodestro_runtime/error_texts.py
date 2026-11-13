"""Value-free texts of failed database work (TABRION MIG-NOD-3, finding B-06-2).

:func:`failure_text` ends the messages of ``storage.backup_failed`` and ``storage.migration_failed`` of both stores
(:mod:`nodestro_runtime.storage`, :mod:`nodestro_runtime.storage_postgres`). It uses only Tabrion's standard-library
core (:mod:`tabrion.errors`), never a driver: the desktop installs no PostgreSQL driver (doc 06 N-2).
"""

from __future__ import annotations

import errno
import os
import re

from tabrion.errors import TabrionError, safe_message

from .problems import NodestroError

__all__ = ["failure_text"]

#: Packages whose exceptions are database driver errors (Tabrion's list plus ``sqlite3``, whose errors are matched by
#: their module so that no module outside the stores imports ``sqlite3``). A SQLAlchemy error is followed to its
#: driver error through ``orig``.
_DRIVER_PACKAGES = frozenset({"sqlite3", "psycopg", "psycopg2", "psycopg_pool"})
#: How many explicit links (``orig``, ``__cause__``) :func:`failure_text` follows, like :func:`tabrion.errors.classify`.
_MAX_CAUSE_DEPTH = 5
#: Signs of a database server's or pg_dump's text in a message (``ERROR:``, ``DETAIL:``, ``HINT:`` ..., ``pg_dump:``,
#: also after an escaped ``\n`` and in lower case, and SQLAlchemy's ``[parameters: ...]``): a :class:`NodestroError`
#: whose message shows one gives only its code. Defense in depth, not a guarantee: a Nodestro message may still quote
#: a value or a path without such a sign (nodestro-api's ``pg_dump failed: {stderr}`` and storage path messages are
#: tracked as the api part of MIG-NOD-3).
_SERVER_TEXT = re.compile(
    r"(?:ERROR|DETAIL|HINT|CONTEXT|QUERY|LINE \d+):|pg_dump:|\[(?:parameters|SQL): ", re.IGNORECASE
)


_SQLSTATE = re.compile(r"[0-9A-Z]{5}")


def _is_database_error(exc: BaseException) -> bool:
    """A driver error, or one that carries a well-formed SQLSTATE: Tabrion classifies exactly this error then, never
    an error that was only being handled (``__context__``)."""
    if any(_SQLSTATE.fullmatch(str(getattr(exc, name, None) or "")) for name in ("sqlstate", "pgcode")):
        return True
    return any(
        (getattr(cls, "__module__", "") or "").partition(".")[0] in _DRIVER_PACKAGES for cls in type(exc).__mro__
    )


def _explicit_chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and what it wraps through ``orig`` and ``__cause__`` (breadth first, each once, depth limited)."""
    found: list[BaseException] = []
    level = [exc]
    for _ in range(_MAX_CAUSE_DEPTH + 1):
        following: list[BaseException] = []
        for link in level:
            if any(link is seen for seen in found):
                continue
            found.append(link)
            for name in ("orig", "__cause__"):
                linked = getattr(link, name, None)
                if isinstance(linked, BaseException):
                    following.append(linked)
        level = following
    return found


def failure_text(exc: BaseException) -> str:
    """Why a backup or migration step failed, without data values (TABRION MIG-NOD-3, finding B-06-2).

    The text goes into a :class:`NodestroError` message, which reaches clients and logs. Only the explicit chain counts
    (the error, ``orig``, ``__cause__``; an error that was merely being handled is not the cause):

    * a Tabrion error or a database error in it gives its value-free text (Tabrion's
      :func:`~tabrion.errors.safe_message`: kind, SQLSTATE or SQLite code, object names; never ``DETAIL``,
      parameters or paths);
    * otherwise a :class:`NodestroError` keeps its own message, which would reach the client unchanged if raised on
      its own; Nodestro messages are meant to hold no values, but that is the raising code's duty, not guaranteed
      here (only its code when the message shows database server or pg_dump text, :data:`_SERVER_TEXT`);
    * anything else gives its class name (an :class:`OSError` also its system error name, never its file name),
      because its text may quote data or paths.

    The original error stays chained (``raise ... from exc``) for the server's redacting log filter.
    """
    try:
        for link in _explicit_chain(exc):
            if isinstance(link, TabrionError):
                return str(link)
            if _is_database_error(link):
                return safe_message(link)
        if isinstance(exc, NodestroError):
            text = str(exc)
            return exc.code if _SERVER_TEXT.search(text) else text
        name = type(exc).__name__
        if isinstance(exc, OSError) and isinstance(exc.errno, int) and exc.errno in errno.errorcode:
            return f"{name} ({errno.errorcode[exc.errno]}: {os.strerror(exc.errno)})"
        return name
    except Exception:  # pragma: no cover - a broken exception object; never fail the error path
        return "unknown error"
