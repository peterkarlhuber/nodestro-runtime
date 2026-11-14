"""How ``grant_runtime_role`` reports Tabrion's refusals (TABRION MIG-NOD-5 review; no server needed).

The grants run through ``tabrion.postgres.grant_runtime_role``. Its errors become Nodestro problems: one phrase per
refusal reason (``storage.role_unsafe``), and ``storage.schema_missing`` for a missing schema, never a Tabrion error
or a bare ``ValueError``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import threading
from typing import Any

import pytest

pytest.importorskip("psycopg")

from tabrion.errors import RoleUnsafe, UsageError

from nodestro_runtime import storage_postgres
from nodestro_runtime.problems import NodestroError

REPORT = {
    "role": "runtime",
    "owner": "owner",
    "schema": "nodestro",
    "database": "db",
    "tables": 3,
    "superuser": False,
    "bypass_rls": False,
    "owned_tables": [],
    "connect": True,
    "can_create": False,
    "restricted": True,
}


class _Owner:
    in_transaction = False

    def __init__(self) -> None:
        self._lock = threading.RLock()

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        yield object()


def _grant_failing(error: BaseException, monkeypatch: pytest.MonkeyPatch) -> NodestroError:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(storage_postgres, "inspect_role", lambda _database, _role: dict(REPORT))
    monkeypatch.setattr(storage_postgres, "_tabrion_grant_runtime_role", refuse)
    with pytest.raises(NodestroError) as caught:
        storage_postgres.grant_runtime_role(_Owner(), "runtime")  # type: ignore[arg-type]
    assert caught.value.__cause__ is None and caught.value.params == {"role": "runtime"}
    return caught.value


@pytest.mark.parametrize(
    ("reasons", "phrases"),
    [
        ("table_owner", ["can become the owner role or the owner of the schema"]),
        ("superuser", ["can become one"]),
        ("bypass_rls", ["has BYPASSRLS"]),
        ("superuser,bypass_rls,table_owner", ["can become one", "has BYPASSRLS", "owner of the schema"]),
    ],
)
def test_every_refusal_reason_has_its_own_phrase(
    reasons: str, phrases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    error = _grant_failing(RoleUnsafe("postgres.role_unsafe", details={"reasons": reasons}), monkeypatch)
    assert error.code == "storage.role_unsafe"
    message = str(error)
    assert all(phrase in message for phrase in phrases), message
    assert message.count(" and ") == len(phrases) - 1


@pytest.mark.parametrize(
    "error",
    [UsageError("postgres.schema_missing"), ValueError("grant_runtime_role needs a schema")],
    ids=["schema_missing", "no_current_schema"],
)
def test_a_missing_schema_is_a_nodestro_problem(error: BaseException, monkeypatch: pytest.MonkeyPatch) -> None:
    found = _grant_failing(error, monkeypatch)
    assert found.code == "storage.schema_missing" and "NODESTRO_DATABASE_SCHEMA" in str(found)


def test_other_usage_errors_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage_postgres, "inspect_role", lambda _database, _role: dict(REPORT))

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise UsageError("postgres.nested_transaction")

    monkeypatch.setattr(storage_postgres, "_tabrion_grant_runtime_role", refuse)
    with pytest.raises(UsageError):
        storage_postgres.grant_runtime_role(_Owner(), "runtime")  # type: ignore[arg-type]
