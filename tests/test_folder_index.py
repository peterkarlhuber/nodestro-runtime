"""Granted folders as a knowledge source (N-12): the search index of a folder grant with ``index``.

Covered: the first index (text and PDF files through the path guard, other types and the default excludes left out),
a change (size, times and content hash), a touch without a change, a deletion, a deny on a subfolder, a revocation
(the chunks and their vectors are dropped at once, a search returns nothing, also one already running and a sync
already running), an expiry, the reconciliation at the start (changes made while Nodestro was closed, grants that ended
meanwhile), the switch turned off, and that the knowledge search never returns a folder's chunks. Provenance (N-08)
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
