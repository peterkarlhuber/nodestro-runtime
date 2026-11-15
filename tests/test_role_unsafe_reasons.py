"""Every refusal reason of ``tabrion.postgres.grant_runtime_role`` has a phrase in Nodestro's ``storage.role_unsafe``.

Tabrion 0.2.2 adds ``predefined_role`` (TB-150, D-124) and ``protected_writable`` (TB-150 item 3, D-125) and
``createrole`` before PostgreSQL 16 (D-127); without a
phrase the message would only name the bare reason code.
"""

from __future__ import annotations

from nodestro_runtime.storage_postgres import _UNSAFE_REASONS


def test_every_known_tabrion_reason_has_a_phrase() -> None:
    assert set(_UNSAFE_REASONS) >= {
        "superuser",
        "bypass_rls",
        "table_owner",
        "predefined_role",
        "protected_writable",
        "createrole",
    }
    assert all(phrase and not phrase.endswith(".") for phrase in _UNSAFE_REASONS.values())
