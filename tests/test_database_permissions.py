"""TABRION MIG-NOD-9 (decision E-NOD-PERM): an existing desktop database is restricted to its owner only with consent.

Starting never changes a mode. A database that group or others may write is refused (``storage.unsafe_file``,
MIG-NOD-6) with ``fixable`` and what to do; :func:`repair_desktop_database_modes` sets ``0600`` on ``nodestro.db`` and
its ``-wal``, ``-shm`` and ``-journal`` files only with ``confirmed=True``, only for regular files of the running user,
and changes the mode only, never the content. A symbolic link or a file of another user is never touched.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path
import shlex
import sqlite3
import stat
from typing import Any

import pytest

from nodestro_runtime import storage
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.storage import ModeRepair, SQLiteStore, repair_desktop_database_modes
from nodestro_runtime.workspace import Workspace

POSIX = os.name == "posix"
posix_only = pytest.mark.skipif(not POSIX, reason="file modes and owners are POSIX only (Windows: no-op)")

SIDES = ("-wal", "-shm", "-journal")


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _database(directory: Path, mode: int, *, sides: tuple[str, ...] = ()) -> Path:
    directory.mkdir(mode=0o700, exist_ok=True)
    database = directory / "nodestro.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE t (v TEXT)")
    connection.execute("INSERT INTO t VALUES ('kept')")
    connection.commit()
    connection.close()
    os.chmod(database, mode)
    for suffix in sides:
        side = directory / f"nodestro.db{suffix}"
        side.write_bytes(b"")
        os.chmod(side, mode)
    return database


def _strip(results: list[ModeRepair]) -> list[ModeRepair]:
    """The results without the file identities (``dev``, ``ino``), which a test cannot know beforehand."""
    return [dataclasses.replace(item, dev=None, ino=None) for item in results]


def _workspace(directory: Path) -> Workspace:
    return Workspace(directory, integrations=[])


@posix_only
def test_starting_never_changes_a_mode(tmp_path: Path) -> None:
    database = _database(tmp_path / "data", 0o644)
    _workspace(tmp_path / "data").close()  # 0644 is not writable by others: it opens, and keeps its mode
    assert _mode(database) == 0o644
    database = _database(tmp_path / "other", 0o664)
    with pytest.raises(NodestroError) as caught:
        _workspace(tmp_path / "other")
    assert caught.value.params == {"reason": "writable_by_others", "fixable": True}
    assert _mode(database) == 0o664


@posix_only
def test_without_confirmation_nothing_changes(tmp_path: Path) -> None:
    database = _database(tmp_path / "data", 0o664, sides=SIDES)
    results = repair_desktop_database_modes(database, confirmed=False)
    assert [(r.path.name, r.status, r.old_mode, r.new_mode) for r in results] == [
        (f"nodestro.db{suffix}", "not_confirmed", 0o664, 0o600) for suffix in ("", *SIDES)
    ]
    for suffix in ("", *SIDES):
        assert _mode(tmp_path / "data" / f"nodestro.db{suffix}") == 0o664


@posix_only
def test_an_existing_0644_database_becomes_0600(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    database = _database(tmp_path / "data", 0o644)
    content = database.read_bytes()
    with caplog.at_level(logging.INFO, logger="nodestro_runtime.storage"):
        assert _strip(repair_desktop_database_modes(database, confirmed=True)) == [
            ModeRepair(database, "changed", old_mode=0o644, new_mode=0o600)
        ]
    assert _mode(database) == 0o600
    assert database.read_bytes() == content  # the mode only, never the content
    assert "was 0o644" in caplog.text and str(database) in caplog.text
    assert _strip(repair_desktop_database_modes(database, confirmed=True)) == [
        ModeRepair(database, "unchanged", old_mode=0o600, new_mode=0o600)
    ]


@posix_only
def test_a_refused_0664_database_is_fixable_and_opens_after_the_confirmed_repair(tmp_path: Path) -> None:
    database = _database(tmp_path / "data", 0o664, sides=("-wal",))
    with pytest.raises(NodestroError) as caught:
        _workspace(tmp_path / "data")
    error = caught.value
    assert error.code == "storage.unsafe_file"
    assert error.params == {"reason": "writable_by_others", "fixable": True}
    assert f"chmod 600 {database} {database}-wal" in str(error) and "Confirm the repair" in str(error)
    results = repair_desktop_database_modes(database, confirmed=True)
    assert [r.status for r in results] == ["changed", "changed"]
    workspace = _workspace(tmp_path / "data")
    try:
        assert workspace.store.get_raw("missing", "k") is None
    finally:
        workspace.close()
    check = sqlite3.connect(database)
    try:
        assert check.execute("SELECT v FROM t").fetchall() == [("kept",)]
    finally:
        check.close()


@posix_only
def test_side_files_are_repaired_too(tmp_path: Path) -> None:
    database = _database(tmp_path / "data", 0o644, sides=SIDES)
    os.chmod(tmp_path / "data" / "nodestro.db-shm", 0o600)  # already private
    results = repair_desktop_database_modes(database, confirmed=True)
    assert {r.path.name: r.status for r in results} == {
        "nodestro.db": "changed",
        "nodestro.db-wal": "changed",
        "nodestro.db-shm": "unchanged",
        "nodestro.db-journal": "changed",
    }
    for suffix in ("", *SIDES):
        assert _mode(tmp_path / "data" / f"nodestro.db{suffix}") == 0o600, suffix


@posix_only
def test_a_missing_database_has_nothing_to_repair(tmp_path: Path) -> None:
    assert _strip(repair_desktop_database_modes(tmp_path / "absent.db", confirmed=True)) == []


@posix_only
def test_a_database_of_another_user_stays_unchanged_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path / "data", 0o664)
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)  # the files now belong to "someone else"
    assert _strip(repair_desktop_database_modes(database, confirmed=True)) == [
        ModeRepair(database, "skipped", old_mode=0o664, reason="other_owner")
    ]
    assert _mode(database) == 0o664
    with pytest.raises(NodestroError) as caught:
        SQLiteStore(database)
    assert caught.value.params["fixable"] is False


@posix_only
def test_a_side_file_of_another_user_is_not_touched_and_makes_it_not_fixable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path / "data", 0o664, sides=("-wal",))
    lstat = os.lstat

    def foreign(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        info = lstat(path, *args, **kwargs)
        if Path(path).name == "nodestro.db-wal":
            return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, info.st_uid + 1, *info[5:]))
        return info

    monkeypatch.setattr(storage.os, "lstat", foreign)
    with pytest.raises(NodestroError) as caught:
        SQLiteStore(database)
    assert caught.value.params == {"reason": "writable_by_others", "fixable": False}
    assert "chmod go-w" in str(caught.value)
    results = repair_desktop_database_modes(database, confirmed=True)
    monkeypatch.undo()
    assert [(r.path.name, r.status, r.reason) for r in results] == [
        ("nodestro.db", "skipped", "other_file_refused"),
        ("nodestro.db-wal", "skipped", "other_owner"),
    ]
    assert _mode(database) == 0o664 and _mode(tmp_path / "data" / "nodestro.db-wal") == 0o664


@posix_only
def test_a_linked_database_stays_unchanged_and_refused(tmp_path: Path) -> None:
    target = _database(tmp_path / "elsewhere", 0o664)
    (tmp_path / "data").mkdir(mode=0o700)
    link = tmp_path / "data" / "nodestro.db"
    link.symlink_to(target)
    assert _strip(repair_desktop_database_modes(link, confirmed=True)) == [
        ModeRepair(link, "skipped", reason="symlink")
    ]
    assert _mode(target) == 0o664 and link.is_symlink()
    with pytest.raises(NodestroError) as caught:
        _workspace(tmp_path / "data")
    assert caught.value.params == {"reason": "symlink", "fixable": False}


@posix_only
def test_a_linked_side_file_is_not_followed(tmp_path: Path) -> None:
    database = _database(tmp_path / "data", 0o664)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"")
    os.chmod(elsewhere, 0o644)
    (tmp_path / "data" / "nodestro.db-wal").symlink_to(elsewhere)
    results = repair_desktop_database_modes(database, confirmed=True)
    assert [(r.path.name, r.status, r.reason) for r in results] == [
        ("nodestro.db", "skipped", "other_file_refused"),
        ("nodestro.db-wal", "skipped", "symlink"),
    ]
    assert _mode(elsewhere) == 0o644 and _mode(database) == 0o664  # all or nothing
    with pytest.raises(NodestroError) as caught:
        _workspace(tmp_path / "data")
    assert caught.value.params == {"reason": "symlink", "fixable": False}


@posix_only
def test_a_link_swapped_in_after_the_check_is_not_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path / "data", 0o664)
    victim = tmp_path / "victim"
    victim.write_bytes(b"")
    os.chmod(victim, 0o644)
    lstat = os.lstat

    def swap(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        info = lstat(path, *args, **kwargs)
        # The patch replaces os.lstat for everyone (pathlib included), so the swap uses only the saved lstat and plain
        # strings; Path.is_symlink() here would call back into this function.
        if os.fspath(path) == str(database) and not stat.S_ISLNK(lstat(database).st_mode):
            database.unlink()
            database.symlink_to(victim)
        return info

    monkeypatch.setattr(storage.os, "lstat", swap)
    [result] = repair_desktop_database_modes(database, confirmed=True)
    monkeypatch.undo()
    assert result.status == "failed" and _mode(victim) == 0o644


@posix_only
def test_a_chmod_failure_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    database = _database(tmp_path / "data", 0o664)

    def refused(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(storage.os, "fchmod", refused)
    with caplog.at_level(logging.WARNING, logger="nodestro_runtime.storage"):
        assert _strip(repair_desktop_database_modes(database, confirmed=True)) == [
            ModeRepair(database, "failed", old_mode=0o664, reason="Operation not permitted")
        ]
    assert _mode(database) == 0o664
    assert "Could not restrict" in caplog.text and "0o664" in caplog.text


@posix_only
def test_a_file_system_that_ignores_chmod_is_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path / "data", 0o664)
    monkeypatch.setattr(storage.os, "fchmod", lambda *_args, **_kwargs: None)
    [result] = repair_desktop_database_modes(database, confirmed=True)
    assert result.status == "failed" and result.new_mode == 0o664
    assert result.reason == "the file system did not take the new mode"


def test_windows_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "nodestro.db"
    database.write_bytes(b"")
    monkeypatch.setattr(storage.os, "name", "nt")
    assert _strip(repair_desktop_database_modes(database, confirmed=True)) == []


@posix_only
@pytest.mark.parametrize(
    ("old", "new"), [(0o644, 0o600), (0o664, 0o600), (0o444, 0o400), (0o755, 0o600), (0o4755, 0o600), (0o2700, 0o600)]
)
def test_the_mode_is_only_narrowed(tmp_path: Path, old: int, new: int) -> None:
    database = _database(tmp_path / "data", old)
    assert _strip(repair_desktop_database_modes(database, confirmed=False)) == [
        ModeRepair(database, "not_confirmed", old_mode=old, new_mode=new)
    ]
    assert _mode(database) == old
    assert _strip(repair_desktop_database_modes(database, confirmed=True)) == [
        ModeRepair(database, "changed", old_mode=old, new_mode=new)
    ]
    assert _mode(database) == new


@posix_only
def test_a_private_or_read_only_file_is_unchanged(tmp_path: Path) -> None:
    database = _database(tmp_path / "data", 0o400)
    assert _strip(repair_desktop_database_modes(database, confirmed=True)) == [
        ModeRepair(database, "unchanged", old_mode=0o400, new_mode=0o400)
    ]


@posix_only
def test_a_hard_link_is_skipped_and_not_fixable(tmp_path: Path) -> None:
    database = _database(tmp_path / "data", 0o664)
    other = tmp_path / "elsewhere.db"
    os.link(database, other)
    assert _strip(repair_desktop_database_modes(database, confirmed=True)) == [
        ModeRepair(database, "skipped", old_mode=0o664, reason="hard_link")
    ]
    assert _mode(other) == 0o664
    with pytest.raises(NodestroError) as caught:
        SQLiteStore(database)
    assert caught.value.params == {"reason": "writable_by_others", "fixable": False}


@posix_only
def test_a_hard_link_made_after_the_check_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path / "data", 0o664)
    open_file = os.open

    def link_then_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if Path(path) == database:
            os.link(database, tmp_path / "late.db")
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(storage.os, "open", link_then_open)
    [result] = repair_desktop_database_modes(database, confirmed=True)
    monkeypatch.undo()
    assert (result.status, result.reason) == ("skipped", "hard_link")
    assert _mode(database) == 0o664


@posix_only
def test_a_fifo_or_directory_is_not_a_file(tmp_path: Path) -> None:
    database = _database(tmp_path / "data", 0o664)
    os.mkfifo(tmp_path / "data" / "nodestro.db-wal", 0o666)
    os.chmod(tmp_path / "data" / "nodestro.db-wal", 0o666)
    (tmp_path / "data" / "nodestro.db-journal").mkdir(mode=0o777)
    os.chmod(tmp_path / "data" / "nodestro.db-journal", 0o777)
    results = repair_desktop_database_modes(database, confirmed=True)
    assert [(r.path.name, r.status, r.reason) for r in results] == [
        ("nodestro.db", "skipped", "other_file_refused"),
        ("nodestro.db-wal", "skipped", "not_a_file"),
        ("nodestro.db-journal", "skipped", "not_a_file"),
    ]
    assert _mode(database) == 0o664
    assert _mode(tmp_path / "data" / "nodestro.db-wal") == 0o666
    assert _mode(tmp_path / "data" / "nodestro.db-journal") == 0o777


def _regular_at_check(monkeypatch: pytest.MonkeyPatch, path: Path, like: Path) -> None:
    """``lstat`` of ``path`` answers what ``like`` (a regular file) is: the check sees a file, the open does not."""
    lstat = os.lstat

    def fake(candidate: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if Path(candidate) == path:
            seen = lstat(like)
            real = lstat(path)
            return os.stat_result((seen.st_mode, real.st_ino, *seen[2:]))
        return lstat(candidate, *args, **kwargs)

    monkeypatch.setattr(storage.os, "lstat", fake)


@posix_only
def test_a_fifo_swapped_in_after_the_check_neither_blocks_nor_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O_NONBLOCK: opening a FIFO without a writer returns at once; fstat then sees it is no regular file."""
    _database(tmp_path / "data", 0o664)
    fifo = tmp_path / "data" / "nodestro.db-wal"
    os.mkfifo(fifo, 0o666)
    os.chmod(fifo, 0o666)
    flags_seen: list[int] = []
    open_file = os.open

    def recording(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == fifo:
            flags_seen.append(flags)
        return open_file(path, flags, *args, **kwargs)

    like = tmp_path / "regular"
    like.write_bytes(b"")
    os.chmod(like, 0o664)
    _regular_at_check(monkeypatch, fifo, like)
    monkeypatch.setattr(storage.os, "open", recording)
    results = repair_desktop_database_modes(tmp_path / "data" / "nodestro.db", confirmed=True)
    monkeypatch.undo()
    assert [(r.path.name, r.status, r.reason) for r in results] == [
        ("nodestro.db", "rolled_back", None),
        ("nodestro.db-wal", "skipped", "changed_during_repair"),
    ]
    assert _mode(tmp_path / "data" / "nodestro.db") == 0o664  # changed, then set back: all or nothing
    assert flags_seen and all(flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW for flags in flags_seen)
    assert _mode(fifo) == 0o666


@posix_only
def test_another_owner_seen_by_fstat_is_changed_during_repair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path / "data", 0o664)
    fstat = os.fstat

    def foreign(descriptor: int) -> os.stat_result:
        info = fstat(descriptor)
        return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, info.st_uid + 1, *info[5:]))

    monkeypatch.setattr(storage.os, "fstat", foreign)
    [result] = repair_desktop_database_modes(database, confirmed=True)
    monkeypatch.undo()
    assert (result.status, result.reason) == ("skipped", "changed_during_repair")
    assert _mode(database) == 0o664


@posix_only
def test_the_advice_quotes_paths_with_spaces(tmp_path: Path) -> None:
    database = _database(tmp_path / "my data", 0o664, sides=("-wal",))
    with pytest.raises(NodestroError) as caught:
        SQLiteStore(database)
    assert caught.value.params == {"reason": "writable_by_others", "fixable": True}
    quoted = shlex.join([str(database), f"{database}-wal"])
    assert f"chmod 600 {quoted}," in str(caught.value) and "'" in quoted


# ---- the identities the user confirmed, all or nothing, restore (review of the repair flow) -------------------------


def _identities(results: list[ModeRepair]) -> dict[str | Path, tuple[int, int]]:
    return {item.path.name: item.identity for item in results if item.identity is not None}


@posix_only
def test_the_dry_run_identities_are_accepted(tmp_path: Path) -> None:
    database = _database(tmp_path / "data", 0o664, sides=("-wal",))
    planned = repair_desktop_database_modes(database, confirmed=False)
    assert all(item.identity == (os.lstat(item.path).st_dev, os.lstat(item.path).st_ino) for item in planned)
    results = repair_desktop_database_modes(database, confirmed=True, expected=_identities(planned))
    assert [item.status for item in results] == ["changed", "changed"]
    by_path = {str(item.path): item.identity for item in planned if item.identity is not None}
    database.chmod(0o664)
    assert [r.status for r in repair_desktop_database_modes(database, confirmed=True, expected=by_path)] == [
        "changed",
        "unchanged",
    ]


@posix_only
def test_an_expected_identity_that_does_not_match_leaves_every_mode(tmp_path: Path) -> None:
    database = _database(tmp_path / "data", 0o664, sides=("-wal",))
    planned = repair_desktop_database_modes(database, confirmed=False)
    replacement = tmp_path / "data" / "new-wal"
    replacement.write_bytes(b"")
    os.chmod(replacement, 0o664)
    os.replace(replacement, tmp_path / "data" / "nodestro.db-wal")  # another file under the same name
    results = repair_desktop_database_modes(database, confirmed=True, expected=_identities(planned))
    assert [(r.path.name, r.status, r.reason) for r in results] == [
        ("nodestro.db", "skipped", "other_file_refused"),
        ("nodestro.db-wal", "skipped", "changed_during_repair"),
    ]
    assert _mode(database) == 0o664 and _mode(tmp_path / "data" / "nodestro.db-wal") == 0o664


@posix_only
def test_a_file_missing_from_expected_is_changed_during_repair(tmp_path: Path) -> None:
    database = _database(tmp_path / "data", 0o664)
    planned = repair_desktop_database_modes(database, confirmed=False)
    (tmp_path / "data" / "nodestro.db-shm").write_bytes(b"")
    os.chmod(tmp_path / "data" / "nodestro.db-shm", 0o664)  # appeared after the user saw the list
    results = repair_desktop_database_modes(database, confirmed=True, expected=_identities(planned))
    assert [(r.path.name, r.status, r.reason) for r in results] == [
        ("nodestro.db", "skipped", "other_file_refused"),
        ("nodestro.db-shm", "skipped", "changed_during_repair"),
    ]
    assert _mode(database) == 0o664
    gone = repair_desktop_database_modes(
        database, confirmed=True, expected={**_identities(planned), "nodestro.db-wal": (1, 2)}
    )
    assert ("nodestro.db-wal", "skipped", "changed_during_repair") in [(r.path.name, r.status, r.reason) for r in gone]


@posix_only
def test_an_st_dev_mismatch_is_changed_during_repair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path / "data", 0o664)
    [planned] = repair_desktop_database_modes(database, confirmed=False)
    assert planned.dev is not None and planned.ino is not None
    [result] = repair_desktop_database_modes(
        database, confirmed=True, expected={"nodestro.db": (planned.dev + 1, planned.ino)}
    )
    assert (result.status, result.reason) == ("skipped", "changed_during_repair") and _mode(database) == 0o664
    fstat = os.fstat

    def other_device(descriptor: int) -> os.stat_result:
        info = fstat(descriptor)
        return os.stat_result((info.st_mode, info.st_ino, info.st_dev + 1, *info[3:]))

    monkeypatch.setattr(storage.os, "fstat", other_device)  # same inode number, another file system
    [result] = repair_desktop_database_modes(database, confirmed=True)
    monkeypatch.undo()
    assert (result.status, result.reason) == ("skipped", "changed_during_repair") and _mode(database) == 0o664


@posix_only
def test_a_partial_failure_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path / "data", 0o664, sides=("-wal", "-shm"))
    fchmod = os.fchmod
    calls: list[int] = []

    def third_fails(descriptor: int, mode: int) -> None:
        calls.append(mode)
        if len(calls) == 3:
            raise PermissionError(1, "Operation not permitted")
        fchmod(descriptor, mode)

    monkeypatch.setattr(storage.os, "fchmod", third_fails)
    results = repair_desktop_database_modes(database, confirmed=True)
    monkeypatch.undo()
    assert [(r.path.name, r.status, r.new_mode) for r in results] == [
        ("nodestro.db", "rolled_back", 0o664),
        ("nodestro.db-wal", "rolled_back", 0o664),
        ("nodestro.db-shm", "failed", None),
    ]
    for suffix in ("", "-wal", "-shm"):
        assert _mode(tmp_path / "data" / f"nodestro.db{suffix}") == 0o664, suffix


@posix_only
def test_a_failed_rollback_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    database = _database(tmp_path / "data", 0o664, sides=("-wal",))
    fchmod = os.fchmod

    def only_the_first(descriptor: int, mode: int) -> None:
        if mode != 0o600 or os.fstat(descriptor).st_ino != os.lstat(database).st_ino:
            raise PermissionError(1, "Operation not permitted")
        fchmod(descriptor, mode)

    monkeypatch.setattr(storage.os, "fchmod", only_the_first)
    with caplog.at_level(logging.WARNING, logger="nodestro_runtime.storage"):
        results = repair_desktop_database_modes(database, confirmed=True)
    monkeypatch.undo()
    assert [(r.path.name, r.status, r.new_mode, r.reason) for r in results] == [
        ("nodestro.db", "rollback_failed", 0o600, "Operation not permitted"),
        ("nodestro.db-wal", "failed", None, "Operation not permitted"),
    ]
    assert _mode(database) == 0o600 and "Could not set" in caplog.text


@posix_only
def test_restore_after_a_successful_repair_sets_the_old_modes(tmp_path: Path) -> None:
    database = _database(tmp_path / "data", 0o664, sides=("-wal",))
    os.chmod(tmp_path / "data" / "nodestro.db-wal", 0o644)
    results = repair_desktop_database_modes(database, confirmed=True)
    assert [r.status for r in results] == ["changed", "changed"]
    restored = storage.restore_desktop_database_modes(results)
    assert [(r.path.name, r.status, r.new_mode) for r in restored] == [
        ("nodestro.db", "restored", 0o664),
        ("nodestro.db-wal", "restored", 0o644),
    ]
    assert _mode(database) == 0o664 and _mode(tmp_path / "data" / "nodestro.db-wal") == 0o644


@posix_only
def test_restore_refuses_a_swapped_file(tmp_path: Path) -> None:
    database = _database(tmp_path / "data", 0o664)
    [changed] = repair_desktop_database_modes(database, confirmed=True)
    swapped = tmp_path / "data" / "swapped"
    swapped.write_bytes(b"")
    os.chmod(swapped, 0o600)
    os.replace(swapped, database)
    [result] = storage.restore_desktop_database_modes([changed])
    assert (result.status, result.reason) == ("skipped", "changed_during_repair")
    assert _mode(database) == 0o600
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"")
    os.chmod(elsewhere, 0o600)
    database.unlink()
    database.symlink_to(elsewhere)
    [result] = storage.restore_desktop_database_modes([changed])
    assert result.status == "failed" and _mode(elsewhere) == 0o600  # O_NOFOLLOW: the link is not followed


def _odd_fchmod(monkeypatch: pytest.MonkeyPatch, database: Path, *, always: bool) -> None:
    """``fchmod`` on ``database`` succeeds but leaves 0o640 (neither the old nor the target mode); with ``always``
    also when the old mode is set back."""
    fchmod = os.fchmod
    first = [True]

    def odd(descriptor: int, mode: int) -> None:
        if os.fstat(descriptor).st_ino == os.lstat(database).st_ino and (always or first[0]):
            first[0] = False
            fchmod(descriptor, 0o640)
            return
        fchmod(descriptor, mode)

    monkeypatch.setattr(storage.os, "fchmod", odd)


@posix_only
def test_an_unexpected_mode_after_fchmod_is_rolled_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path / "data", 0o664, sides=("-wal",))
    _odd_fchmod(monkeypatch, database, always=False)
    results = repair_desktop_database_modes(database, confirmed=True)
    monkeypatch.undo()
    assert [(r.path.name, r.status, r.new_mode, r.reason) for r in results] == [
        ("nodestro.db", "rolled_back", 0o664, "the file system did not take the new mode"),
        ("nodestro.db-wal", "skipped", None, "other_file_refused"),
    ]
    assert _mode(database) == 0o664 and _mode(tmp_path / "data" / "nodestro.db-wal") == 0o664


@posix_only
def test_an_unexpected_mode_that_cannot_be_rolled_back_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path / "data", 0o664)
    _odd_fchmod(monkeypatch, database, always=True)
    [result] = repair_desktop_database_modes(database, confirmed=True)
    monkeypatch.undo()
    assert (result.status, result.new_mode) == ("rollback_failed", 0o640)
    assert _mode(database) == 0o640


@posix_only
def test_restore_never_overwrites_a_later_chmod(tmp_path: Path) -> None:
    database = _database(tmp_path / "data", 0o664, sides=("-wal",))
    results = repair_desktop_database_modes(database, confirmed=True)
    os.chmod(database, 0o400)  # the user changed it after the repair
    restored = storage.restore_desktop_database_modes(results)
    assert [(r.path.name, r.status, r.reason) for r in restored] == [
        ("nodestro.db", "skipped", "changed_during_repair"),
        ("nodestro.db-wal", "restored", None),
    ]
    assert _mode(database) == 0o400 and _mode(tmp_path / "data" / "nodestro.db-wal") == 0o664
