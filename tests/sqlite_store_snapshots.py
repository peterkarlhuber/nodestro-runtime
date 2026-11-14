"""Before/after evidence of the desktop's SQLite connections (TABRION MIG-NOD-6).

MIG-NOD-6 moves :class:`~nodestro_runtime.storage.SQLiteStore` (open, transaction, migration, snapshot) and the own
SQLite connections of ``sqlite_tables.memory_totals`` and ``postgres_import.import_desktop_database`` to
``tabrion.sqlite``. This module records what the desktop shows of them, without a server:

- ``pragmas``: the settings of the store's connection on a new file;
- ``files``: the modes of a new ``nodestro.db`` and its ``-wal``/``-shm`` and of a :meth:`SQLiteStore.snapshot` copy
  (umask 022 and 002), the snapshot's journal mode (file header), and whether a symbolic link is followed;
- ``migrate``: code, parameters and message of every refusal and failure of :meth:`SQLiteStore.migrate`, and the
  database state after each;
- ``memory_totals`` and ``import_source``: the answer of ``memory_totals`` and the refusals of
  ``import_desktop_database`` for sources that are no Nodestro database.

``tests/fixtures/sqlite_store_before.json`` was written by the code *before* MIG-NOD-6::

    python tests/sqlite_store_snapshots.py write   # only on the code before MIG-NOD-6

``tests/test_sqlite_store_snapshots.py`` compares the current code with it; the intended differences are listed
there one by one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from typing import Any

BEFORE = Path(__file__).resolve().parent / "fixtures" / "sqlite_store_before.json"
PRAGMAS = ("journal_mode", "synchronous", "foreign_keys", "busy_timeout", "secure_delete", "trusted_schema")


@contextmanager
def _umask(mask: int) -> Iterator[None]:
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> str | None:
    try:
        return oct(stat.S_IMODE(os.lstat(path).st_mode))
    except FileNotFoundError:
        return None


def _journal_of_file(path: Path) -> str:
    """The journal mode a closed database file says it has (header bytes 18 and 19: 1 rollback, 2 WAL)."""
    header = path.read_bytes()[18:20]
    return {b"\x01\x01": "rollback", b"\x02\x02": "wal"}.get(header, header.hex())


def _outcome(call: Callable[[], Any], directory: Path) -> dict[str, Any]:
    from nodestro_runtime.problems import NodestroError

    try:
        value = call()
    except NodestroError as exc:
        cause = exc.__cause__
        return {
            "code": exc.code,
            "params": {
                k: (str(v).replace(str(directory), "<dir>") if k == "path" else v) for k, v in exc.params.items()
            },
            "message": str(exc).replace(str(directory), "<dir>"),
            "cause": type(cause).__name__ if cause is not None else None,
        }
    except Exception as exc:
        return {"raised": type(exc).__name__}
    return {"ok": value}


def pragmas(directory: Path) -> dict[str, Any]:
    from nodestro_runtime.storage import SQLiteStore

    store = SQLiteStore(directory / "pragmas.db")
    try:
        connection = store._connection
        return {name: connection.execute(f"PRAGMA {name}").fetchone()[0] for name in PRAGMAS}
    finally:
        store.close()


def files(directory: Path) -> dict[str, Any]:
    from nodestro_runtime.storage import SQLiteStore

    found: dict[str, Any] = {}
    if os.name != "posix":
        return found
    for mask in (0o022, 0o002):
        with _umask(mask):
            base = directory / f"umask{mask:03o}"
            base.mkdir()
            store = SQLiteStore(base / "nodestro.db")
            try:
                store.put_raw("c", "k", {"a": 1})
                modes = {name: _mode(base / name) for name in ("nodestro.db", "nodestro.db-wal", "nodestro.db-shm")}
                store.snapshot(base / "copy.db")
            finally:
                store.close()
            found[f"umask_{mask:03o}"] = {
                "store": modes,
                "snapshot": _mode(base / "copy.db"),
                "snapshot_journal": _journal_of_file(base / "copy.db"),
                "snapshot_side_files": sorted(
                    p.name for p in base.iterdir() if p.name.startswith("copy.db") and p.name != "copy.db"
                ),
            }
    real = directory / "real.db"
    SQLiteStore(real).close()
    link = directory / "link.db"
    link.symlink_to(real)
    found["symlink"] = _outcome(lambda: (SQLiteStore(link).close(), "opened")[1], directory)
    return found


def _steps(fail_at: int | None = None) -> dict[int, Callable[[sqlite3.Connection], None]]:
    def step(version: int) -> Callable[[sqlite3.Connection], None]:
        def run(db: sqlite3.Connection) -> None:
            db.execute(f"CREATE TABLE t{version} (x)")
            if version == fail_at:
                raise RuntimeError("step failed with a secret value 4711")

        return run

    return {version: step(version) for version in (1, 2, 3)}


def migrate(directory: Path) -> dict[str, Any]:
    from nodestro_runtime.storage import SQLiteStore

    cases: dict[str, Any] = {}

    def fresh(name: str, version: int = 0, *, data: bool = False) -> SQLiteStore:
        store = SQLiteStore(directory / f"{name}.db")
        if version:
            store._connection.execute(f"PRAGMA user_version = {version}")
        if data:
            store.put_raw("c", "k", {"a": 1})
        return store

    def tables(store: SQLiteStore) -> list[str]:
        rows = store._connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        return [str(row[0]) for row in rows]

    def record(name: str, store: SQLiteStore, call: Callable[[], Any]) -> None:
        try:
            outcome = _outcome(call, directory)
            outcome["after"] = {"version": store.schema_version, "tables": tables(store)}
            cases[name] = outcome
        finally:
            store.close()

    calls: list[tuple[int, int]] = []
    s = fresh("fresh")
    record("fresh_no_backup", s, lambda: s.migrate(before_migration=None, migrations=_steps(), target=3))
    s = fresh("current", 3)
    record("current", s, lambda: s.migrate(before_migration=None, migrations=_steps(), target=3))
    s = fresh("newer", 5)
    record("schema_too_new", s, lambda: s.migrate(before_migration=None, migrations=_steps(), target=3))
    s = fresh("missing", 0)
    record("migration_missing", s, lambda: s.migrate(before_migration=None, migrations={1: _steps()[1]}, target=3))
    s = fresh("required", 1, data=True)
    record("backup_required", s, lambda: s.migrate(before_migration=None, migrations=_steps(), target=3))

    def failing_backup(current: int, target: int) -> None:
        raise OSError(28, "No space left on device")

    s = fresh("backup_failed", 1, data=True)
    record("backup_failed", s, lambda: s.migrate(before_migration=failing_backup, migrations=_steps(), target=3))
    s = fresh("step_failed", 1, data=True)
    record(
        "migration_failed",
        s,
        lambda: s.migrate(before_migration=lambda c, t: calls.append((c, t)), migrations=_steps(fail_at=3), target=3),
    )
    s = fresh("backed_up", 1, data=True)
    record(
        "with_backup",
        s,
        lambda: s.migrate(before_migration=lambda c, t: calls.append((c, t)), migrations=_steps(), target=3),
    )
    cases["backup_calls"] = [list(call) for call in calls]
    s = fresh("in_transaction")

    def inside() -> Any:
        with s.transaction():
            return s.migrate(before_migration=None, migrations=_steps(), target=3)

    record("inside_transaction", s, inside)
    return cases


def memory_totals(directory: Path) -> dict[str, Any]:
    from nodestro_runtime.sqlite_tables import memory_totals as totals
    from nodestro_runtime.storage import SQLiteStore

    database = directory / "memory.db"
    SQLiteStore(database).close()
    first = totals(str(database), "space:")
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO memory_files (namespace, path, content, updated_at) VALUES"
            " ('space:a', 'x.md', 'hello', 't'), ('space:a', 'y.md', 'abc', 't'), ('other', 'z.md', 'q', 't')"
        )
        connection.commit()
        columns = [row[1] for row in connection.execute("PRAGMA table_info(memory_files)")]
    finally:
        connection.close()
    return {"empty": first, "filled": totals(str(database), "space:"), "columns": columns}


class _NoServer:
    """``import_desktop_database`` checks the source before it touches the server database."""

    def require_current_schema(self) -> None:
        return None


def import_source(directory: Path) -> dict[str, Any]:
    try:
        from nodestro_runtime.postgres_import import import_desktop_database
    except ImportError:  # pragma: no cover - the server extra is part of the dev install
        return {}
    from nodestro_runtime.storage import SQLiteStore

    junk = directory / "junk.db"
    junk.write_bytes(b"this is not a database at all, but long enough to have a header" * 4)
    empty = directory / "empty.db"
    empty.write_bytes(b"")
    old = directory / "old.db"
    SQLiteStore(old).close()  # user_version 0: no Nodestro database of this release
    plain = directory / "plain.db"
    sqlite3.connect(plain).close()
    database: Any = _NoServer()
    return {
        name: _outcome(lambda path=path: import_desktop_database(path, database, "t1"), directory)
        for name, path in (
            ("not_a_database", junk),
            ("empty_file", empty),
            ("old_schema", old),
            ("no_documents", plain),
            ("missing", directory / "none.db"),
        )
    }


def collect() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        found: dict[str, Any] = {}
        for name, part in (
            ("pragmas", pragmas),
            ("files", files),
            ("migrate", migrate),
            ("memory_totals", memory_totals),
            ("import_source", import_source),
        ):
            directory = root / name
            directory.mkdir()
            found[name] = part(directory)
        return found


def dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    if argv != ["write"]:
        print("usage: python tests/sqlite_store_snapshots.py write", file=sys.stderr)
        return 2
    BEFORE.write_text(dumps(collect()), encoding="utf-8")
    print(f"wrote {BEFORE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
