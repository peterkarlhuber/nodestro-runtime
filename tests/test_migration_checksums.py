"""The shipped PostgreSQL migrations stay byte-identical (MUP-ONA A0, requirement K1).

``tests/fixtures/migration_checksums.json`` lists the SHA-256 of every file in
``src/nodestro_runtime/migrations/postgres``. An applied migration is never edited (the runner refuses a database
whose recorded checksums differ, ``storage.migration_changed``); a new migration is added to the list in the PR that
adds the file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nodestro_runtime.storage_postgres import migration_checksum, postgres_migrations

ROOT = Path(__file__).resolve().parent.parent
LIST = ROOT / "tests" / "fixtures" / "migration_checksums.json"
MIGRATIONS = ROOT / "src" / "nodestro_runtime" / "migrations" / "postgres"


def _listed() -> dict[str, str]:
    files = json.loads(LIST.read_text(encoding="utf-8"))["files"]
    assert isinstance(files, dict) and files
    return {str(name): str(digest) for name, digest in files.items()}


def _shipped() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(MIGRATIONS.iterdir())
        if path.is_file()
    }


# MUP-ONA T-MIG-1
def test_every_migration_file_matches_the_fixed_checksum_list() -> None:
    listed, shipped = _listed(), _shipped()
    new = sorted(set(shipped) - set(listed))
    assert not new, (
        f"New migration file(s) {new} are not in {LIST.relative_to(ROOT)}: add each with its SHA-256 "
        "(sha256sum <file>) in the PR that adds the migration"
    )
    missing = sorted(set(listed) - set(shipped))
    assert not missing, f"Listed migration file(s) {missing} are gone: a released migration is never removed"
    changed = sorted(name for name in listed if listed[name] != shipped[name])
    assert not changed, (
        f"Migration file(s) {changed} differ from {LIST.relative_to(ROOT)}: an applied migration is never edited; "
        "write a new migration instead"
    )


# MUP-ONA T-MIG-1
def test_the_baseline_migrations_0001_to_0010_are_listed() -> None:
    listed = _listed()
    versions = sorted(int(name[:4]) for name in listed)
    assert versions[:10] == list(range(1, 11))
    assert versions == list(range(1, len(versions) + 1)), "migration versions must have no gaps"


# MUP-ONA T-MIG-1
def test_the_runner_checksums_are_the_listed_file_checksums() -> None:
    """The runner records the SHA-256 of the migration text it reads; for these files it is the file checksum."""
    listed = _listed()
    for name, step in postgres_migrations().values():
        assert migration_checksum(step) == listed.get(name), f"{name} is not listed or its checksum differs"
