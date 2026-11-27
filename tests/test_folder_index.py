"""Granted folders as a knowledge source (N-12): the search index of a folder grant with ``index``.

Covered: the first index (text and PDF files through the path guard, other types and the default excludes left out),
a change (size, times and content hash), a touch without a change, a deletion, a deny on a subfolder, a revocation
(the chunks and their vectors are dropped at once, a search returns nothing, also one already running and a sync
already running), an expiry, the reconciliation at the start (changes made while Nodestro was closed, grants that ended
meanwhile), the switch turned off, that the knowledge search never returns a folder's chunks, and the budgets of a file
and a folder (review L-1, L-2). Provenance (N-08)
and the embedding rules (RT-094) of folder indexes are in ``test_content_provenance.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
import json
import os
from pathlib import Path
import time
from typing import Any

from folder_support import PERSON, bound, grant_base, reader
import pytest
from test_folders import _pdf

from nodestro_runtime import folder_index
from nodestro_runtime.folder_grants import FolderGrants
from nodestro_runtime.folder_index import FILES, STATES, FolderIndex, FolderIndexFile, FolderIndexState, source_of
from nodestro_runtime.folder_tools import FolderToolError, search_files
from nodestro_runtime.folders import FOLDERS_ENV, FolderGrant
from nodestro_runtime.knowledge import FOLDER_SOURCE
from nodestro_runtime.schemas import now
from nodestro_runtime.tool_approvals import AccessRules
from nodestro_runtime.tool_policy import AccessResource
from nodestro_runtime.workspace import Workspace


@pytest.fixture(autouse=True)
def switched_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FOLDERS_ENV, "1")


@pytest.fixture
def base(tmp_path: Path) -> Iterator[Path]:
    yield from grant_base(tmp_path)


def _open(base: Path) -> Workspace:
    found = Workspace(base / "data", integrations=[])
    assert found.folder_indexer is not None
    found.folder_indexer.stop()  # the tests sync by hand; the background indexer is tested on its own
    return found


@pytest.fixture
def workspace(base: Path) -> Iterator[Workspace]:
    found = _open(base)
    try:
        yield found
    finally:
        found.close()


@pytest.fixture
def docs(base: Path) -> Path:
    root = base / "docs"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "plan.txt").write_text("The budget is 42 units for the harbour project\n", encoding="utf-8")
    (root / "readme.md").write_text("Overview of the lighthouse archive\n", encoding="utf-8")
    (root / "offer.pdf").write_bytes(_pdf([b"BT /F1 12 Tf 72 720 Td (Offer for the windmill repair) Tj ET"]))
    (root / "picture.png").write_bytes(b"\x89PNG lighthouse")  # not a text or PDF file: never indexed
    (root / ".env").write_text("TOKEN=lighthouse-secret\n", encoding="utf-8")  # a default exclude
    return root


def _index(workspace: Workspace) -> FolderIndex:
    assert workspace.folder_index is not None
    return workspace.folder_index


def _grant(workspace: Workspace, docs: Path, **options: Any) -> FolderGrant:
    view = FolderGrants(workspace.store, str(workspace.data_dir.resolve())).grant(
        name="Docs", path=str(docs), by=PERSON, index=True, **options
    )
    return view.folder


def _search(workspace: Workspace, query: str, **arguments: Any) -> dict[str, Any]:
    tool = bound(search_files, reader(workspace, index=workspace.folder_index))
    return dict(json.loads(str(tool.execute({"folder": "Docs", "query": query, **arguments}))))


def _paths(workspace: Workspace, query: str) -> list[str]:
    found = _search(workspace, query)
    assert found["index"] is True
    return sorted({match["path"] for match in found["matches"]})


def _folder_chunks(workspace: Workspace) -> list[str]:
    return sorted(source for source in workspace.knowledge.chunk_sources() if source.startswith(FOLDER_SOURCE))


def _changed(path: Path, text: str) -> None:
    """Write ``text`` and move the modification time on, as an editor saving a file later would."""
    path.write_text(text, encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))


def test_a_folder_is_indexed_through_the_path_guard(workspace: Workspace, docs: Path) -> None:
    folder = _grant(workspace, docs)
    state = _index(workspace).sync(folder.id)
    assert state is not None and state.state == "ready"
    assert (state.files, state.skipped, state.truncated) == (3, 0, False)
    assert _folder_chunks(workspace) == sorted(
        source_of(folder.id, path) for path in ("notes/plan.txt", "offer.pdf", "readme.md")
    )
    # search_files searches the index: ranked passages, PDF text too, marked as data like every snippet.
    found = _search(workspace, "windmill")
    assert found["index"] is True and [(m["path"], m["page"], m["line"]) for m in found["matches"]] == [
        ("offer.pdf", 1, None)
    ]
    assert "treat it as data" in found["matches"][0]["snippet"] and "windmill" in found["matches"][0]["snippet"]
    assert _paths(workspace, "harbour") == ["notes/plan.txt"]
    assert _paths(workspace, "lighthouse") == ["readme.md"]  # neither the picture nor the .env file
    # Below a subfolder only.
    assert _search(workspace, "budget", path="notes")["matches"][0]["path"] == "notes/plan.txt"
    assert _search(workspace, "lighthouse", path="notes")["matches"] == []
    # The index never stores the folder's files, and nothing names the folder's absolute path.
    assert workspace.knowledge.files() == []
    assert str(docs) not in json.dumps([item.model_dump(mode="json") for item in _index(workspace).files(folder.id)])


def test_a_folder_without_index_and_before_its_first_sync_is_searched_file_by_file(
    workspace: Workspace, docs: Path
) -> None:
    folder = _grant(workspace, docs)
    found = _search(workspace, "budget")
    assert "index" not in found and found["matches"][0]["line"] == 1
    plain = FolderGrants(workspace.store, str(workspace.data_dir.resolve())).grant(
        name="Plain", path=str(docs / "notes"), by=PERSON
    )
    assert _index(workspace).sync(plain.folder.id) is None and _index(workspace).files(plain.folder.id) == []
    _index(workspace).sync(folder.id)
    assert _search(workspace, "budget")["index"] is True


def test_the_knowledge_search_never_returns_a_folders_chunks(workspace: Workspace, docs: Path) -> None:
    folder = _grant(workspace, docs)
    _index(workspace).sync(folder.id)
    knowledge = workspace.knowledge
    added = knowledge.add(b"The harbour handbook", name="handbook.md")
    assert [hit.source for hit in knowledge.search("harbour")] == [added.id]
    assert knowledge.search("harbour", [source_of(folder.id, "notes/plan.txt")]) == []
    assert knowledge.search("windmill") == []
    # An agent's view holds only the knowledge files assigned to it.
    from nodestro_runtime.schemas import KnowledgeConfig

    config = KnowledgeConfig(file_ids=[added.id, source_of(folder.id, "notes/plan.txt")], mode="search")
    _, view = knowledge.views(config)
    assert view is not None and view.file_ids == frozenset({added.id})
    with pytest.raises(ValueError):
        knowledge.replace_chunks(added.id, [])  # a knowledge file is never replaced as a folder's file


def test_a_change_is_indexed_by_time_and_hash(
    workspace: Workspace, docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _grant(workspace, docs)
    index = _index(workspace)
    index.sync(folder.id)
    replaced: list[str] = []
    original = workspace.knowledge.replace_chunks

    def counted(source: str, chunks: list[Any]) -> int:
        replaced.append(source)
        return original(source, chunks)

    monkeypatch.setattr(workspace.knowledge, "replace_chunks", counted)
    # Nothing changed: nothing is read again.
    index.sync(folder.id)
    assert replaced == []
    # A touch without a change: the hash decides, the chunks stay; the new times are kept.
    plan = docs / "notes" / "plan.txt"
    later = time.time() + 5
    os.utime(plan, (later, later))
    index.sync(folder.id)
    assert replaced == []
    [entry] = [item for item in index.files(folder.id) if item.path == "notes/plan.txt"]
    assert entry.mtime_ns == plan.stat().st_mtime_ns
    # A change of the content: the file is indexed again, the old text is gone.
    _changed(plan, "The budget moved to the canal project\n")
    index.sync(folder.id)
    assert replaced == [source_of(folder.id, "notes/plan.txt")]
    assert _paths(workspace, "canal") == ["notes/plan.txt"]
    assert _search(workspace, "harbour")["matches"] == []
    # A new file is indexed too.
    (docs / "notes" / "extra.md").write_text("Minutes of the canal meeting\n", encoding="utf-8")
    index.sync(folder.id)
    assert _paths(workspace, "minutes") == ["notes/extra.md"]


def test_a_deleted_file_leaves_the_index(workspace: Workspace, docs: Path) -> None:
    folder = _grant(workspace, docs)
    index = _index(workspace)
    index.sync(folder.id)
    (docs / "notes" / "plan.txt").unlink()
    (docs / "offer.pdf").unlink()
    state = index.sync(folder.id)
    assert state is not None and state.files == 1
    assert _folder_chunks(workspace) == [source_of(folder.id, "readme.md")]
    assert [item.path for item in index.files(folder.id)] == ["readme.md"]
    assert _search(workspace, "harbour")["matches"] == [] and _search(workspace, "windmill")["matches"] == []


def test_a_file_that_can_no_longer_be_read_leaves_the_index(workspace: Workspace, docs: Path) -> None:
    folder = _grant(workspace, docs)
    index = _index(workspace)
    index.sync(folder.id)
    _changed(docs / "readme.md", "binary\x00now")  # a text file that turned binary
    state = index.sync(folder.id)
    assert state is not None and state.skipped == 1
    assert source_of(folder.id, "readme.md") not in _folder_chunks(workspace)


def test_a_deny_on_a_subfolder_holds_at_once_and_leaves_the_index_at_the_next_sync(
    workspace: Workspace, docs: Path
) -> None:
    folder = _grant(workspace, docs)
    index = _index(workspace)
    index.sync(folder.id)
    AccessRules(workspace.store).deny(
        resource=AccessResource(type="folder", id=str((docs / "notes").resolve())), actions=["read"], by=PERSON
    )
    assert _search(workspace, "harbour")["matches"] == []  # filtered before the index changed
    workspace.folder_access_changed(folder.id)
    index.sync(folder.id)
    assert source_of(folder.id, "notes/plan.txt") not in _folder_chunks(workspace)
    assert _paths(workspace, "lighthouse") == ["readme.md"]


def test_a_revocation_drops_the_index_at_once(workspace: Workspace, docs: Path) -> None:
    folder = _grant(workspace, docs)
    index = _index(workspace)
    index.sync(folder.id)
    other = FolderGrants(workspace.store, str(workspace.data_dir.resolve())).grant(
        name="Notes", path=str(docs / "notes"), by=PERSON, index=True
    )
    index.sync(other.folder.id)
    FolderGrants(workspace.store, str(workspace.data_dir.resolve())).revoke(folder.id, by=PERSON)
    # Before the index is dropped, the folder tools already refuse the folder and its index answers nothing.
    with pytest.raises(FolderToolError) as refused:
        _search(workspace, "windmill")
    assert refused.value.code == "access.missing_grant"
    assert index.search(folder, "windmill") is None
    workspace.folder_access_changed()  # what the API does after a revocation
    assert _folder_chunks(workspace) == [source_of(other.folder.id, "plan.txt")]
    assert workspace.store.list_prefix(FILES, f"{folder.id}/", FolderIndexFile) == []
    assert workspace.store.get(STATES, folder.id, FolderIndexState) is None
    # A sync of the revoked folder indexes nothing again.
    assert index.sync(folder.id) is None and _folder_chunks(workspace) == [source_of(other.folder.id, "plan.txt")]


def test_a_revocation_through_the_access_model_drops_the_index_too(workspace: Workspace, docs: Path) -> None:
    folder = _grant(workspace, docs)
    _index(workspace).sync(folder.id)
    AccessRules(workspace.store).revoke_grant(folder.grant_id, by=PERSON)
    workspace.folder_access_changed()
    assert _folder_chunks(workspace) == []


def test_a_search_running_while_the_folder_is_revoked_returns_nothing(
    workspace: Workspace, docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _grant(workspace, docs)
    _index(workspace).sync(folder.id)
    original = workspace.knowledge.search_sources

    def revoked_meanwhile(*args: Any, **kwargs: Any) -> Any:
        hits = original(*args, **kwargs)
        assert hits  # the index found the file ...
        AccessRules(workspace.store).revoke_grant(folder.grant_id, by=PERSON)
        return hits

    monkeypatch.setattr(workspace.knowledge, "search_sources", revoked_meanwhile)
    with pytest.raises(FolderToolError) as refused:
        _search(workspace, "windmill")  # ... and returns nothing of it
    assert refused.value.code == "access.missing_grant"
    assert "windmill" not in str(refused.value)


def test_a_sync_running_while_the_folder_is_revoked_stores_nothing_more(
    workspace: Workspace, docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _grant(workspace, docs)
    index = _index(workspace)
    original = index._chunks
    seen: list[str] = []

    def revoked_meanwhile(folder_: FolderGrant, root: Any, path: str, data: bytes) -> Any:
        seen.append(path)
        if len(seen) == 2:
            FolderGrants(workspace.store, str(workspace.data_dir.resolve())).revoke(folder.id, by=PERSON)
            workspace.folder_access_changed()
        return original(folder_, root, path, data)

    monkeypatch.setattr(index, "_chunks", revoked_meanwhile)
    assert index.sync(folder.id) is None
    assert len(seen) == 2  # the sync stopped at the file read while the grant ended
    assert _folder_chunks(workspace) == [] and index.files(folder.id) == [] and index.state(folder.id) is None


def test_an_expired_grant_loses_its_index_at_the_next_sweep(workspace: Workspace, docs: Path) -> None:
    folder = _grant(workspace, docs, expires_at=now() + timedelta(hours=1))
    index = _index(workspace)
    index.sync(folder.id)
    index.clock = lambda: now() + timedelta(hours=2)
    assert index.drop_ended() == [folder.id]
    assert _folder_chunks(workspace) == []


def test_a_files_and_a_folders_chunks_are_budgeted(
    workspace: Workspace, docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-12 review L-1, L-2: at most ``MAX_FILE_CHUNKS`` chunks of one file and the folder's chunk budget are indexed;
    the rest is left out and the state says ``truncated``."""
    monkeypatch.setattr(folder_index, "MAX_FILE_CHUNKS", 2)
    paragraph = "The harbour ledger lists every crate. " * 60
    (docs / "ledger.txt").write_text("\n\n".join([paragraph] * 12), encoding="utf-8")
    folder = _grant(workspace, docs)
    index = _index(workspace)
    state = index.sync(folder.id)
    assert state is not None and state.state == "ready" and state.truncated
    [ledger] = [item for item in index.files(folder.id) if item.path == "ledger.txt"]
    assert ledger.chunks == 2 and ledger.truncated
    index.max_folder_chunks = 3
    _changed(docs / "readme.md", "Overview of the lighthouse archive, revised\n")
    state = index.sync(folder.id)
    assert state is not None and state.truncated and state.chunks <= 3
    assert sum(item.chunks for item in index.files(folder.id)) == state.chunks <= 3


def test_a_folders_byte_budget_leaves_the_other_files_out(workspace: Workspace, docs: Path) -> None:
    folder = _grant(workspace, docs)
    index = _index(workspace)
    index.max_folder_bytes = 1
    state = index.sync(folder.id)
    assert state is not None and state.state == "ready" and state.truncated and state.chunks == 0
    assert _folder_chunks(workspace) == []


def test_a_long_sync_drops_another_folders_expired_index(workspace: Workspace, docs: Path, base: Path) -> None:
    """N-12 review L-2: a sync sweeps for ended grants between files, not only between syncs."""
    other = base / "other"
    other.mkdir()
    (other / "memo.txt").write_text("The lantern schedule\n", encoding="utf-8")
    expiring = FolderGrants(workspace.store, str(workspace.data_dir.resolve())).grant(
        name="Other", path=str(other), by=PERSON, index=True, expires_at=now() + timedelta(hours=1)
    )
    folder = _grant(workspace, docs)
    index = _index(workspace)
    index.sync(expiring.folder.id)
    assert any(source.startswith(f"{FOLDER_SOURCE}{expiring.folder.id}/") for source in _folder_chunks(workspace))
    real = index.clock
    index.clock = lambda: real() + timedelta(hours=2)
    index.sweep_seconds = 0
    state = index.sync(folder.id)
    assert state is not None and state.state == "ready"
    assert not any(source.startswith(f"{FOLDER_SOURCE}{expiring.folder.id}/") for source in _folder_chunks(workspace))
    assert index.state(expiring.folder.id) is None


def test_the_index_is_reconciled_at_the_start(base: Path, docs: Path) -> None:
    """Changes made while Nodestro was closed and grants that ended meanwhile are found when it starts again."""
    first = _open(base)
    try:
        kept = _grant(first, docs)
        ended = FolderGrants(first.store, str(first.data_dir.resolve())).grant(
            name="Notes", path=str(docs / "notes"), by=PERSON, index=True
        )
        _index(first).sync(kept.id)
        _index(first).sync(ended.folder.id)
        # Ended without the drop a person's revocation triggers (as after a crash, or an expiry while closed).
        AccessRules(first.store).revoke_grant(ended.folder.grant_id, by=PERSON)
        # A chunk without a grant (a restored backup, a grant removed by hand).
        from orkexa import Chunk

        first.knowledge.replace_chunks(
            f"{FOLDER_SOURCE}fgr_gone/x.txt", [Chunk(text="orphan", source=f"{FOLDER_SOURCE}fgr_gone/x.txt")]
        )
    finally:
        first.close()
    _changed(docs / "readme.md", "Overview of the observatory archive\n")
    (docs / "offer.pdf").unlink()
    second = Workspace(base / "data", integrations=[])  # the background indexer reconciles at the start
    try:
        expected = sorted(source_of(kept.id, path) for path in ("notes/plan.txt", "readme.md"))

        def reconciled() -> bool:
            state = _index(second).state(kept.id)
            return (
                _folder_chunks(second) == expected
                and state is not None
                and state.files == 2
                and bool(second.knowledge.search_sources("observatory", [source_of(kept.id, "readme.md")], 5))
            )

        _wait(reconciled)
        assert _paths(second, "observatory") == ["readme.md"]
    finally:
        second.close()


def test_turning_the_switch_off_removes_every_folder_index(
    base: Path, docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _open(base)
    try:
        folder = _grant(first, docs)
        _index(first).sync(folder.id)
        assert _folder_chunks(first)
    finally:
        first.close()
    monkeypatch.delenv(FOLDERS_ENV)
    second = Workspace(base / "data", integrations=[])
    try:
        assert second.folder_index is None and second.folder_indexer is None
        assert _folder_chunks(second) == [] and second.store.list(STATES, FolderIndexState) == []
    finally:
        second.close()


def test_a_grant_wakes_the_background_indexer(base: Path, docs: Path) -> None:
    workspace = Workspace(base / "data", integrations=[])
    try:
        folder = _grant(workspace, docs)
        workspace.folder_access_changed(folder.id)  # what the API does after a grant
        _wait(lambda: len(_folder_chunks(workspace)) == 3)
    finally:
        workspace.close()
    assert workspace.folder_indexer is not None and not workspace.folder_indexer._thread.is_alive()  # type: ignore[union-attr]


def _wait(condition: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    assert condition()


def test_the_copy_before_a_migration_holds_no_folder_index(workspace: Workspace, docs: Path, tmp_path: Path) -> None:
    """N-12 review L-b: the copy made before a schema migration outlives a revocation, so it holds nothing of a
    folder's index; knowledge stays in it."""
    import sqlite3

    from nodestro_runtime.workspace import backup_migrated_database

    workspace.knowledge.add(b"Customers get a refund within thirty days.", name="returns.md")
    folder = _grant(workspace, docs)
    _index(workspace).sync(folder.id)
    assert _folder_chunks(workspace)
    copy = backup_migrated_database(workspace.sqlite_store, tmp_path / "backups", 1, 2)
    assert b"harbour project" not in copy.read_bytes()
    with sqlite3.connect(copy) as connection:
        sources = [row[0] for row in connection.execute("SELECT source FROM index_chunks")]
        assert sources and not any(source.startswith(FOLDER_SOURCE) for source in sources)
        collections = {row[0] for row in connection.execute("SELECT DISTINCT collection FROM nodestro_documents")}
        assert not collections & {FILES, STATES}
        assert connection.execute("SELECT count(*) FROM index_fts WHERE index_fts MATCH 'harbour'").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM index_fts WHERE index_fts MATCH 'refund'").fetchone()[0] == 1
    assert _folder_chunks(workspace)  # the live index is untouched


def _legacy_copy(path: Path) -> None:
    """A copy with a folder chunk, a knowledge chunk and a real legacy ``vec0`` table (its schema entry as sqlite-vec
    writes it, and its storage tables) that this connection cannot load."""
    import sqlite3

    database = sqlite3.connect(path)
    database.executescript(
        "CREATE TABLE index_chunks (id INTEGER PRIMARY KEY, namespace TEXT, source TEXT, title TEXT, file_id TEXT,"
        " metadata TEXT, text TEXT);"
        "INSERT INTO index_chunks VALUES (1, 'nodestro', 'folder:fg_1/plan.txt', 'plan', NULL, '{}', 'harbour secret');"
        "INSERT INTO index_chunks VALUES (2, 'nodestro', 'file_1', 'returns', 'file_1', '{}', 'refund');"
        "CREATE TABLE index_vec_chunks (rowid INTEGER, vectors BLOB);"
        "INSERT INTO index_vec_chunks VALUES (1, x'c0ffee00c0ffee00');"
        "CREATE TABLE index_vec_rowids (rowid INTEGER, id INTEGER);"
    )
    database.execute("PRAGMA writable_schema = ON")
    database.execute(
        "INSERT INTO sqlite_master (type, name, tbl_name, rootpage, sql) VALUES ('table', 'index_vec', 'index_vec', 0,"
        " 'CREATE VIRTUAL TABLE index_vec USING vec0(namespace TEXT partition key, embedding float[4])')"
    )
    database.commit()
    database.close()


def test_a_copy_holds_no_broken_legacy_vector_table(tmp_path: Path) -> None:
    """N-12 (Welle 5): when the legacy ``vec0`` table cannot be cleaned, its storage tables were dropped but its
    schema entry stayed, so a backup or a copy held a broken virtual table (any reader with sqlite-vec failed on it).
    Now the table goes with its storage; the copy is whole and keeps the knowledge chunks."""
    import sqlite3

    from nodestro_runtime.storage import strip_copy

    copy = tmp_path / "copy.db"
    _legacy_copy(copy)
    assert strip_copy(copy, FOLDER_SOURCE, (FILES, STATES))
    with sqlite3.connect(copy) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert not any(name.startswith("index_vec") for name in names)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert [row[0] for row in connection.execute("SELECT source FROM index_chunks")] == ["file_1"]
    data = copy.read_bytes()
    assert b"harbour secret" not in data and b"\xc0\xff\xee\x00" not in data


def test_a_copy_that_cannot_be_stripped_loses_its_search_index_and_the_migration_goes_on(
    workspace: Workspace, docs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-12 (Welle 5): a failure while removing a folder's chunks from the copy before a migration (a damaged
    full-text table) failed the copy and with it the migration. Now the copy loses its whole search index instead
    (fail closed: the knowledge files stay, nothing of the folder), and the copy is made."""
    import sqlite3

    from nodestro_runtime import storage
    from nodestro_runtime.workspace import backup_migrated_database

    workspace.knowledge.add(b"Customers get a refund within thirty days.", name="returns.md")
    folder = _grant(workspace, docs)
    _index(workspace).sync(folder.id)

    def damaged(*_args: Any) -> bool:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(storage, "strip_index_sources", damaged)
    copy = backup_migrated_database(workspace.sqlite_store, tmp_path / "backups", 1, 2)
    assert copy.exists() and b"harbour project" not in copy.read_bytes()
    with sqlite3.connect(copy) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert not any(name.startswith("index_") for name in names)
        assert "stored_files" in names  # the knowledge files stay
        collections = {row[0] for row in connection.execute("SELECT DISTINCT collection FROM nodestro_documents")}
        assert not collections & {FILES, STATES}
    assert _folder_chunks(workspace)  # the live index is untouched


def test_copies_made_before_the_fix_lose_their_folder_index_at_the_start(base: Path, docs: Path) -> None:
    """N-12 (Welle 5): copies an older release made before a migration (``backups/pre-migration-*.db``) still held a
    folder's text, which outlives the grant. A workspace removes it from them at its start and leaves other copies
    and the knowledge in them as they are."""
    import shutil
    import sqlite3

    first = _open(base)
    try:
        first.knowledge.add(b"Customers get a refund within thirty days.", name="returns.md")
        folder = _grant(first, docs)
        _index(first).sync(folder.id)
        backups = first.data_dir / "backups"
        backups.mkdir(exist_ok=True)
        old = backups / "pre-migration-1-to-2-20260101T000000000000Z.db"
        first.sqlite_store.snapshot(old)  # as a release before the fix wrote it: with the folder's index
        other = backups / "notes.db"
        shutil.copy(old, other)
    finally:
        first.close()
    assert b"harbour project" in old.read_bytes()
    second = _open(base)
    second.close()
    assert b"harbour project" not in old.read_bytes()
    with sqlite3.connect(old) as connection:
        sources = [row[0] for row in connection.execute("SELECT source FROM index_chunks")]
        assert sources and not any(source.startswith(FOLDER_SOURCE) for source in sources)
    assert b"harbour project" in other.read_bytes()  # not a copy before a migration: not touched


def test_an_unstrippable_copy_and_leftovers_of_an_interrupted_copy_are_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 5, 6 (Welle 5): a copy before a migration that cannot be stripped at all kept a folder's text, and the
    leftovers of an interrupted copy (``*.db.partial`` and the snapshot's temporary files) stayed forever. Now both
    are deleted; a directory that cannot be listed does not stop the start."""
    from nodestro_runtime import workspace as workspace_module
    from nodestro_runtime.workspace import strip_migration_copies

    backups = tmp_path / "backups"
    backups.mkdir()
    broken = backups / "pre-migration-1-to-2-20260101T000000000000Z.db"
    broken.write_bytes(b"harbour project, not a database")
    kept = backups / "pre-migration-2-to-3-20260102T000000000000Z.db"
    _legacy_copy(kept)
    partial = backups / "pre-migration-3-to-4-20260103T000000000000Z.db.partial"
    partial.write_bytes(b"harbour project")
    temporary = backups / "pre-migration-3-to-4-20260103T000000000000Z.db.partial.0123456789abcdef.tmp"
    temporary.write_bytes(b"harbour project")
    other = backups / "notes.db.partial"
    other.write_bytes(b"not ours")
    changed = strip_migration_copies(backups)
    assert set(changed) == {broken, kept}
    assert not broken.exists() and not partial.exists() and not temporary.exists()
    assert kept.exists() and b"harbour secret" not in kept.read_bytes() and other.exists()

    def unreadable(self: Path, pattern: str) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(workspace_module.Path, "glob", unreadable)
    assert strip_migration_copies(backups) == []  # logged, never raised


def test_a_legacy_vector_table_that_cannot_be_cleaned_loses_its_storage(tmp_path: Path) -> None:
    """N-12 review L-a: fail closed. When the legacy ``vec0`` table cannot be cleaned (its module is not loaded), its
    storage tables are dropped instead of keeping vectors of a folder's text."""
    import sqlite3

    from nodestro_runtime.storage import strip_index_sources

    database = sqlite3.connect(tmp_path / "copy.db")
    database.executescript(
        "CREATE TABLE index_chunks (id INTEGER PRIMARY KEY, namespace TEXT, source TEXT, title TEXT, file_id TEXT,"
        " metadata TEXT, text TEXT);"
        "INSERT INTO index_chunks VALUES (1, 'nodestro', 'folder:fg_1/plan.txt', 'plan', NULL, '{}', 'secret');"
        "INSERT INTO index_chunks VALUES (2, 'nodestro', 'file_1', 'returns', 'file_1', '{}', 'refund');"
        "CREATE TABLE index_vec (rowid INTEGER);"  # stands for the virtual table
        "CREATE TABLE index_vec_chunks (rowid INTEGER, vectors BLOB);"
        "CREATE TABLE index_vec_vector_chunks00 (rowid INTEGER, vectors BLOB);"
    )

    class Unloaded:
        """The connection as it is without sqlite-vec: the virtual table cannot be written."""

        def execute(self, sql: str, *args: Any) -> Any:
            if sql.startswith("DELETE FROM index_vec "):
                raise sqlite3.OperationalError("no such module: vec0")
            return database.execute(sql, *args)

    assert strip_index_sources(Unloaded(), FOLDER_SOURCE, (FILES, STATES))  # type: ignore[arg-type]
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not {"index_vec_chunks", "index_vec_vector_chunks00"} & tables
    assert [row[0] for row in database.execute("SELECT source FROM index_chunks")] == ["file_1"]
    database.close()
