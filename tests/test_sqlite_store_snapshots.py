"""TABRION MIG-NOD-6: the desktop's SQLite connections on ``tabrion.sqlite`` behave as before, except where intended.

``tests/fixtures/sqlite_store_before.json`` is the behavior of the code before MIG-NOD-6
(``tests/sqlite_store_snapshots.py``). The current code must give the same answers, except the differences listed in
:data:`INTENDED`, each with its reason. Migration refusals and failures (codes, parameters, messages, database state
afterwards), ``memory_totals`` and the import refusals are unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any

import pytest
import sqlite_store_snapshots
from tabrion.errors import UnsafePath

from nodestro_runtime.storage import SQLITE_PROFILE, SQLiteStore

POSIX = os.name == "posix"

#: The intended differences: (path in the snapshot, before, after, reason).
INTENDED: list[tuple[tuple[str, ...], Any, Any, str]] = [
    (("pragmas", "foreign_keys"), 0, 1, "SQLITE_PROFILE: foreign keys enforced (doc 06 MIG-NOD-6 field 4)"),
    (("pragmas", "trusted_schema"), 1, 0, "Tabrion's default: no SQL functions with side effects in the schema"),
]
if POSIX:
    for mask in ("022", "002"):
        for name in ("nodestro.db", "nodestro.db-wal", "nodestro.db-shm"):
            INTENDED.append(
                (("files", f"umask_{mask}", "store", name), "0o644", "0o600", "B-06-5: a new database is owner-only")
            )
        INTENDED.append((("files", f"umask_{mask}", "snapshot"), "0o644", "0o600", "B-06-3: a snapshot is owner-only"))
        INTENDED.append(
            (("files", f"umask_{mask}", "snapshot_journal"), "wal", "rollback", "tabrion.sqlite.backup: one file")
        )
    INTENDED.append((("files", "symlink"), {"ok": "opened"}, {"raised": "UnsafePath"}, "symbolic links refused"))


def _set(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        data = data[key]
    data[path[-1]] = value


def _get(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    for key in path:
        data = data[key]
    return data


@pytest.fixture(scope="module")
def current() -> dict[str, Any]:
    return sqlite_store_snapshots.collect()


def test_the_baseline_has_the_intended_before_values() -> None:
    before = json.loads(sqlite_store_snapshots.BEFORE.read_text(encoding="utf-8"))
    for path, old, _new, reason in INTENDED:
        assert _get(before, path) == old, (path, reason)


def test_everything_else_is_unchanged(current: dict[str, Any]) -> None:
    expected = json.loads(sqlite_store_snapshots.BEFORE.read_text(encoding="utf-8"))
    for path, _old, new, _reason in INTENDED:
        _set(expected, path, new)
    if not POSIX:  # the file modes are POSIX only; elsewhere the baseline's "files" is not comparable
        expected.pop("files"), current.pop("files")
    if not current.get("import_source"):  # without the server extra
        expected.pop("import_source")
    assert sqlite_store_snapshots.dumps(current) == sqlite_store_snapshots.dumps(expected)


def test_the_profile_keeps_durability_and_secure_delete() -> None:
    assert SQLITE_PROFILE.journal_mode == "wal" and SQLITE_PROFILE.synchronous == "full"
    assert SQLITE_PROFILE.busy_timeout == 30.0 and SQLITE_PROFILE.secure_delete is True
    assert SQLITE_PROFILE.file_mode == 0o600 and SQLITE_PROFILE.allow_shared_file is False


@pytest.mark.skipif(not POSIX, reason="file modes are POSIX only")
def test_an_existing_world_readable_database_keeps_its_mode(tmp_path: Path) -> None:
    """Existing files are not changed with chmod (doc 06 MIG-NOD-6 field 10; MIG-NOD-9 tightens them)."""
    database = tmp_path / "nodestro.db"
    sqlite3.connect(database).close()
    os.chmod(database, 0o644)
    store = SQLiteStore(database)
    try:
        store.put_raw("c", "k", {"a": 1})
    finally:
        store.close()
    assert stat.S_IMODE(os.stat(database).st_mode) == 0o644


@pytest.mark.skipif(not POSIX, reason="file modes are POSIX only")
def test_a_database_writable_by_others_is_refused(tmp_path: Path) -> None:
    database = tmp_path / "nodestro.db"
    sqlite3.connect(database).close()
    os.chmod(database, 0o666)
    with pytest.raises(UnsafePath) as caught:
        SQLiteStore(database)
    assert caught.value.code == "sqlite.unsafe_mode" and str(tmp_path) not in str(caught.value)


@pytest.mark.skipif(not POSIX, reason="symbolic links need privileges on Windows")
def test_a_linked_wal_file_is_refused(tmp_path: Path) -> None:
    database = tmp_path / "nodestro.db"
    SQLiteStore(database).close()
    (tmp_path / "elsewhere").write_bytes(b"")
    (tmp_path / "nodestro.db-wal").unlink(missing_ok=True)
    (tmp_path / "nodestro.db-wal").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(UnsafePath):
        SQLiteStore(database)


def test_foreign_key_check_is_empty_after_the_migrations(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nodestro.db")
    try:
        store.migrate(before_migration=None)
        store.put_raw("c", "k", {"a": 1})
        assert store._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        store.close()


def test_a_snapshot_replaces_an_existing_target_and_refuses_a_link(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nodestro.db")
    try:
        store.put_raw("c", "k", {"a": 1})
        target = tmp_path / "copy.db"
        target.write_bytes(b"old")
        store.snapshot(target)
        copy = sqlite3.connect(target)
        try:
            assert copy.execute("SELECT count(*) FROM nodestro_documents").fetchone()[0] == 1
        finally:
            copy.close()
        if POSIX:
            link = tmp_path / "link.db"
            link.symlink_to(tmp_path / "victim.db")
            with pytest.raises(UnsafePath):
                store.snapshot(link)
            assert not (tmp_path / "victim.db").exists()
        assert sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")) == []
    finally:
        store.close()
