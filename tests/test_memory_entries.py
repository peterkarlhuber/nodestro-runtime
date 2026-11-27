"""Memory entries with their time and origin (N-24) on the desktop's SQLite database, including a database written
before origins were kept."""

from __future__ import annotations

from pathlib import Path
import sqlite3

from nodestro_runtime.memory_spaces import ORIGIN_IMPORT, ORIGIN_USER, MemorySpaces


def test_a_space_written_before_origins_were_kept_keeps_working(tmp_path: Path) -> None:
    """A ``memory_files`` table without the ``origin`` column (every database before N-24): opening the space adds
    the column, the old file keeps its content and time and lists no origin, and new writes record theirs."""
    database = tmp_path / "nodestro.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE memory_files (namespace TEXT NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL,"
        " updated_at TEXT NOT NULL, PRIMARY KEY (namespace, path))"
    )
    connection.execute(
        "INSERT INTO memory_files VALUES ('nodestro:Team', 'old.md', 'from before', '2026-01-02T03:04:05+00:00')"
    )
    connection.commit()
    connection.close()
    database.chmod(0o600)

    spaces = MemorySpaces(database)
    try:
        assert [(info.name, info.files, info.scope) for info in spaces.all()] == [("Team", 1, "user")]
        space = spaces.open("Team")
        [old] = space.entries()
        assert (old.path, old.size, old.origin, old.scope) == ("/memories/old.md", 11, None, "user")
        assert old.updated_at is not None and old.updated_at.isoformat() == "2026-01-02T03:04:05+00:00"
        assert space.read("/memories/old.md") == "from before"
        space.write("/memories/new.md", "now")
        space.write("/memories/seed.md", "seeded", origin=ORIGIN_IMPORT)
        assert {entry.path: entry.origin for entry in space.entries()} == {
            "/memories/new.md": ORIGIN_USER,
            "/memories/old.md": None,
            "/memories/seed.md": ORIGIN_IMPORT,
        }
    finally:
        spaces.close()
    # A second opening finds the column and changes nothing.
    reopened = MemorySpaces(database)
    try:
        assert len(reopened.open("Team").entries()) == 3
    finally:
        reopened.close()
