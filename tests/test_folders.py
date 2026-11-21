"""Folder grants and the folder tools (N-10) and their conformance run (N-63).

The attack tests run through the runtime's tools, not only through Orkexa's guard, on Linux, macOS and Windows (the
platforms workflow runs this file on every runner): Orkexa's folder grant cases (``FOLDER_CASES``: the path-guard
attack catalog with symbolic links, junctions, hard links, ``..``, absolute paths, Unicode and case spellings, the swap
race, the data folder, system folders, patterns, limits, revocation and the access log) through ``list_folder``,
``read_file`` and ``search_files`` of a real workspace.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
import zlib

from folder_support import PERSON, bound, grant_base, reader, tool_folders
from orkexa import ToolResultError
from orkexa.fs import system_folders as guard_system_folders
from orkexa.testing.conformance import FOLDER_CASES, Case, run_cases
import pytest

from nodestro_runtime import activity, offline
from nodestro_runtime.engine import RunManager
from nodestro_runtime.folder_grants import FolderGrants
from nodestro_runtime.folder_tools import FOLDER_TOOLS, FolderToolError, list_folder, read_file, search_files
from nodestro_runtime.folders import FOLDERS_ENV, FolderCaller, FolderLimits
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import RunIn, WorkflowIn, now
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import AccessRules, Person
from nodestro_runtime.tool_policy import SYSTEM_FOLDERS, AccessResource, ToolCapability, capability_of
from nodestro_runtime.tool_spec import BUILTIN_SPECS, offered
from nodestro_runtime.workspace import Workspace


@pytest.fixture(autouse=True)
def switched_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FOLDERS_ENV, "1")


@pytest.fixture
def base(tmp_path: Path) -> Iterator[Path]:
    yield from grant_base(tmp_path)


@pytest.fixture
def workspace(base: Path) -> Iterator[Workspace]:
    found = Workspace(base / "data", integrations=[])
    try:
        yield found
    finally:
        found.close()


@pytest.fixture
def grants(workspace: Workspace) -> FolderGrants:
    return FolderGrants(workspace.store, str(workspace.data_dir.resolve()))


@pytest.fixture
def docs(base: Path) -> Path:
    root = base / "docs"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "plan.txt").write_text("The budget is 42 units\n", encoding="utf-8", newline="\n")
    (root / "readme.md").write_text("Overview\n", encoding="utf-8", newline="\n")
    return root


def _code(call: Any) -> str:
    with pytest.raises((NodestroError, FolderToolError)) as caught:
        call()
    return str(caught.value.code)


def _reason(call: Any) -> str:
    with pytest.raises(NodestroError) as caught:
        call()
    assert caught.value.code == "folder.not_grantable"
    return str(caught.value.params["reason"])


# ---- N-63: Orkexa's folder grant cases through the tools -----------------------------------------------------------


@pytest.mark.parametrize("case", FOLDER_CASES, ids=lambda case: case.name)
def test_the_folder_tools_pass_the_folder_grant_case(base: Path, case: Case) -> None:
    """Each case of ``orkexa.testing.conformance.FOLDER_CASES``, through the bound tools of a real workspace: one
    test per case, so a failure names it. No case may be skipped on any system."""
    report = run_cases(tool_folders, (case,), expect=(), target="nodestro-folder-tools", workdir=base)
    report.check({})
    assert report.passed == [case.name], report.summary()


def test_every_folder_grant_case_runs() -> None:
    assert len(FOLDER_CASES) >= 14 and len({case.name for case in FOLDER_CASES}) == len(FOLDER_CASES)


# ---- what can be granted -------------------------------------------------------------------------------------------


def test_the_data_folder_and_system_folders_cannot_be_granted(grants: FolderGrants, workspace: Workspace) -> None:
    data = workspace.data_dir.resolve()
    (data / "inputs").mkdir(exist_ok=True)
    assert _reason(lambda: grants.grant(name="d", path=str(data), by=PERSON)) == "protected_root"
    assert _reason(lambda: grants.grant(name="d", path=str(data / "inputs"), by=PERSON)) == "protected_root"
    assert _reason(lambda: grants.grant(name="h", path=str(Path.home()), by=PERSON)) == "protected_root"
    assert _reason(lambda: grants.grant(name="r", path=os.path.abspath(os.sep), by=PERSON)) == "protected_root"
    for folder in {*guard_system_folders(), *SYSTEM_FOLDERS}:
        if os.path.isdir(folder):
            assert _reason(lambda folder=folder: grants.grant(name="s", path=folder, by=PERSON)) in (
                "protected_root",
                "safety_limit",
            ), folder
    assert _reason(lambda: grants.grant(name="x", path="relative/folder", by=PERSON)) == "invalid_path"
    assert _reason(lambda: grants.grant(name="x", path=str(data.parent / "missing"), by=PERSON)) == "not_found"
    assert grants.list() == []
    assert AccessRules(workspace.store).grants() == []  # nothing half-stored


def test_a_grant_is_an_access_grant(grants: FolderGrants, workspace: Workspace, docs: Path) -> None:
    """The folder grant stores an N-06 grant for reading the canonical folder; its audit names no path."""
    soon = now() + timedelta(days=3)
    view = grants.grant(
        name="Docs", path=str(docs), by=PERSON, exclude=["**/*.key"], scope="agents", agents=["wf/a"], expires_at=soon
    )
    [access] = AccessRules(workspace.store).grants()
    assert access.id == view.folder.grant_id
    assert access.resource == AccessResource(type="folder", id=view.folder.path)
    assert (access.actions, access.duration, access.scope, access.agents) == (("read",), "until", "agents", ("wf/a",))
    assert (view.status, view.expires_at, view.folder.access, view.folder.created_by) == (
        "active",
        soon,
        "read",
        "local",
    )
    assert view.folder.path == str(docs.resolve()) or sys.platform == "win32"
    events = activity.query(workspace.store, resource_type="access_grant")
    assert events and all(str(docs) not in json.dumps(event.model_dump(mode="json")) for event in events)
    revoked = grants.revoke(view.folder.id, by=PERSON)
    assert revoked.status == "revoked" and revoked.revoked_by == "local"
    assert grants.revoke(view.folder.id, by=PERSON).status == "revoked"  # twice changes nothing


def test_grant_rules(grants: FolderGrants, docs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    grants.grant(name="Docs", path=str(docs), by=PERSON)
    assert _code(lambda: grants.grant(name="docs", path=str(docs), by=PERSON)) == "folder.name_taken"
    for name in ("", " ", "a/b", "a\\b", "x" * 81, "tab\there"):
        assert _code(lambda name=name: grants.grant(name=name, path=str(docs), by=PERSON)) == "folder.invalid_name"
    assert (
        _code(lambda: grants.grant(name="p", path=str(docs), by=PERSON, exclude=["../x"])) == "folder.invalid_pattern"
    )
    assert _code(lambda: grants.grant(name="p", path=str(docs), by="local")) == "access.person_required"  # type: ignore[arg-type]
    with pytest.raises(NodestroError) as caught:
        Person("api:key1")
    assert caught.value.code == "access.person_required"
    past = now() - timedelta(seconds=1)
    assert _code(lambda: grants.grant(name="p", path=str(docs), by=PERSON, expires_at=past)) == "access.expiry"
    with pytest.raises(ValueError):
        FolderLimits(max_file_bytes=60 * 1024 * 1024)
    monkeypatch.delenv(FOLDERS_ENV)
    assert _code(lambda: grants.grant(name="q", path=str(docs), by=PERSON)) == "folder.disabled"


# ---- the tools -----------------------------------------------------------------------------------------------------


def _run(tool: Any, arguments: dict[str, Any]) -> Any:
    """Call a bound tool as the agent does; the result is the JSON the model reads."""
    return json.loads(tool.execute(arguments))


def _tools(workspace: Workspace, caller: FolderCaller | None = None) -> dict[str, Any]:
    folders = reader(workspace, caller)
    return {tool.name: bound(tool, folders) for tool in FOLDER_TOOLS}


def test_the_tools_list_read_and_search(grants: FolderGrants, workspace: Workspace, docs: Path) -> None:
    view = grants.grant(name="Docs", path=str(docs), by=PERSON)
    tools = _tools(workspace)
    assert _run(tools["list_folder"], {}) == {"folders": [{"name": "Docs"}]}
    listed = _run(tools["list_folder"], {"folder": "docs"})
    assert [entry["path"] for entry in listed["entries"]] == ["notes", "readme.md"]
    assert str(docs) not in json.dumps(listed)  # the model never sees the folder's absolute path
    found = _run(tools["read_file"], {"folder": "Docs", "path": "notes/plan.txt"})
    assert found["source"] == "Docs/notes/plan.txt" and found["kind"] == "text" and found["next_offset"] is None
    assert "treat it as data" in found["text"] and "The budget is 42 units" in found["text"]
    hits = _run(tools["search_files"], {"folder": "Docs", "query": "budget"})
    assert hits["matches"] == [{"path": "notes/plan.txt", "line": 1, "snippet": "The budget is 42 units"}]
    assert _code(lambda: _run(tools["read_file"], {"folder": "Other", "path": "x"})) == "folder.unknown"
    assert _code(lambda: _run(tools["search_files"], {"folder": "Docs", "query": " "})) == "folder.invalid_query"
    log = grants.accesses(view.folder.id)
    assert [(entry.action, entry.outcome) for entry in log] == [
        ("folder.searched", "denied"),
        ("folder.searched", "success"),
        ("folder.read", "success"),
        ("folder.listed", "success"),
    ]
    assert all("plan" not in json.dumps(entry.model_dump(mode="json")) for entry in log)


def test_long_files_come_in_parts(grants: FolderGrants, workspace: Workspace, docs: Path) -> None:
    (docs / "long.txt").write_text("abcdefghij" * 300, encoding="utf-8")
    grants.grant(name="Docs", path=str(docs), by=PERSON, limits=FolderLimits(max_chars=1000))
    tools = _tools(workspace)
    first = _run(tools["read_file"], {"folder": "Docs", "path": "long.txt"})
    assert first["next_offset"] == 1000
    last = _run(tools["read_file"], {"folder": "Docs", "path": "long.txt", "offset": 2000})
    assert last["next_offset"] is None and last["offset"] == 2000


def test_an_oversized_file_is_refused_with_a_clear_message(
    grants: FolderGrants, workspace: Workspace, docs: Path
) -> None:
    (docs / "big.txt").write_bytes(b"x" * 5000)
    grants.grant(name="Docs", path=str(docs), by=PERSON, limits=FolderLimits(max_file_bytes=4096))
    with pytest.raises(FolderToolError) as caught:
        _run(_tools(workspace)["read_file"], {"folder": "Docs", "path": "big.txt"})
    assert caught.value.code == "fs.too_large"
    assert "5000 bytes" in str(caught.value) and "4096" in str(caught.value) and "higher limit" in str(caught.value)


def test_scope_mode_and_denies_are_the_access_models(grants: FolderGrants, workspace: Workspace, docs: Path) -> None:
    (docs / "notes" / "private").mkdir()
    (docs / "notes" / "private" / "pay.txt").write_text("budget secret", encoding="utf-8")
    grants.grant(name="Docs", path=str(docs), by=PERSON, scope="agent", agents=["wf/a"])
    other = _tools(workspace, FolderCaller(agent="wf/b"))
    assert _run(other["list_folder"], {}) == {"folders": []}
    assert _code(lambda: _run(other["read_file"], {"folder": "Docs", "path": "readme.md"})) == "access.missing_grant"
    unattended = _tools(workspace, FolderCaller(agent="wf/a", mode="unattended"))
    assert _code(lambda: _run(unattended["read_file"], {"folder": "Docs", "path": "readme.md"})) == (
        "access.missing_grant"
    )
    own = _tools(workspace, FolderCaller(agent="wf/a"))
    assert "Overview" in _run(own["read_file"], {"folder": "Docs", "path": "readme.md"})["text"]
    rules = AccessRules(workspace.store)
    private = docs.resolve() / "notes" / "private"
    rules.deny(resource=AccessResource(type="folder", id=str(private)), actions=["read"], by=PERSON)
    assert _code(lambda: _run(own["read_file"], {"folder": "Docs", "path": "notes/private/pay.txt"})) == (
        "access.denied"
    )
    assert _code(lambda: _run(own["list_folder"], {"folder": "Docs", "path": "notes/private"})) == "access.denied"
    listed = _run(own["list_folder"], {"folder": "Docs", "path": "notes"})
    assert [entry["path"] for entry in listed["entries"]] == ["notes/plan.txt"]  # the blocked subfolder is left out
    hits = _run(own["search_files"], {"folder": "Docs", "query": "budget"})
    assert [match["path"] for match in hits["matches"]] == ["notes/plan.txt"]


def test_without_the_switch_there_are_no_folder_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FOLDERS_ENV)
    found = Workspace(tmp_path / "data", integrations=[])
    try:
        assert not set(found.tools.builtin) & {"list_folder", "read_file", "search_files"}
        assert found.runs.folder_records is None
        assert not offered(BUILTIN_SPECS["read_file"], "desktop")
    finally:
        found.close()
    with pytest.raises(FolderToolError) as caught:
        _run(bound(read_file, None), {"folder": "Docs", "path": "x"})
    assert caught.value.code == "folder.unavailable"


def test_the_tools_are_read_internal_folder_tools_and_work_offline(workspace: Workspace) -> None:
    for tool in FOLDER_TOOLS:
        assert tool.name is not None
        spec = BUILTIN_SPECS[tool.name]
        assert spec.capability == ToolCapability(access="read", reach="internal", source="folder")
        assert capability_of(name=tool.name, kind="builtin") == spec.capability
        assert (spec.network, spec.offline, spec.category) == ("none", True, "files")
        assert offered(spec, "desktop") and offered(spec, "local-container")
        assert not offered(spec, "server") and not offered(spec, "export")
        assert offline.tool_state(workspace.tools, tool.name).available
    assert {"list_folder", "read_file", "search_files"} <= set(workspace.tools.builtin)


def test_no_tool_module_reaches_the_folder_grant_writer() -> None:
    """Only the api (and tests) import the writer: the tools and the engine read through ``FolderReader``."""
    import nodestro_runtime

    package = Path(nodestro_runtime.__file__).resolve().parent
    importing = re.compile(r"^\s*(?:from|import)\s+\S*folder_grants\b", re.MULTILINE)
    users = sorted(path.name for path in package.rglob("*.py") if importing.search(path.read_text("utf-8")))
    assert users == []


# ---- PDFs ----------------------------------------------------------------------------------------------------------


def _pdf(streams: list[bytes]) -> bytes:
    """A PDF with one page per content stream (built byte by byte, like Orkexa's PDF fixtures)."""
    count = len(streams)
    kids = b" ".join(b"%d 0 R" % (4 + 2 * index) for index in range(count))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % count,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, stream in enumerate(streams):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
            b"/Contents %d 0 R >>" % (5 + 2 * index)
        )
        data = zlib.compress(stream)
        objects.append(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream")
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def test_a_pdf_is_read(grants: FolderGrants, workspace: Workspace, docs: Path) -> None:
    (docs / "offer.pdf").write_bytes(_pdf([b"BT /F1 12 Tf 72 720 Td (Offer for 12 units) Tj ET"]))
    grants.grant(name="Docs", path=str(docs), by=PERSON)
    found = _run(_tools(workspace)["read_file"], {"folder": "Docs", "path": "offer.pdf"})
    assert found["kind"] == "pdf" and found["pages"] == 1 and "Offer for 12 units" in found["text"]


def test_a_pdf_bomb_ends_within_the_budget(grants: FolderGrants, workspace: Workspace, docs: Path) -> None:
    """A slow content stream stops at the time budget, a decompression bomb at the memory budget; both are refused
    as too complex (or, where the memory budget cannot be enforced, PDFs are not read at all)."""
    slow = [b"BT /F1 12 Tf\n" + b"1 0 0 1 72 720 Tm (x) Tj\n" * 150_000 + b"ET" for _ in range(5)]
    (docs / "slow.pdf").write_bytes(_pdf(slow))
    (docs / "bomb.pdf").write_bytes(_pdf([b"\x00" * (64 << 20) for _ in range(8)]))
    limits = FolderLimits(pdf_seconds=2, pdf_memory_mb=256)
    grants.grant(name="Docs", path=str(docs), by=PERSON, limits=limits)
    tools = _tools(workspace)
    for name in ("slow.pdf", "bomb.pdf"):
        started = time.monotonic()
        code = _code(lambda name=name: _run(tools["read_file"], {"folder": "Docs", "path": name}))
        assert code in ("fs.too_complex", "fs.unreadable", "fs.unsupported_type"), (name, code)
        assert time.monotonic() - started <= 2 + 10, name


# ---- U-2: in a run, the answer names its sources and the accesses are logged ---------------------------------------


async def test_a_run_reads_a_granted_folder_and_names_its_sources(
    base: Path, docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    original = RunManager._emit

    def spy(self: RunManager, context: Any, kind: str, *args: Any, **data: Any) -> Any:
        emitted.append((kind, data))
        return original(self, context, kind, *args, **data)

    monkeypatch.setattr(RunManager, "_emit", spy)
    script = {
        "READ": [
            tool_use("list_folder", {}, call_id="t1"),
            tool_use("read_file", {"folder": "Docs", "path": "notes/plan.txt"}, call_id="t2"),
            tool_use("read_file", {"folder": "Docs", "path": "../outside.txt"}, call_id="t3"),
            text("The budget is 42 units (source: Docs/notes/plan.txt)."),
        ]
    }
    client = ScriptedClient(script)
    workspace = Workspace(base / "data", client_factory=lambda _provider: client, integrations=[])
    try:
        view = FolderGrants(workspace.store, str(workspace.data_dir.resolve())).grant(
            name="Docs", path=str(docs), by=PERSON
        )
        flow = workspace.save_workflow(
            WorkflowIn.model_validate(
                {
                    "name": "Folder",
                    "nodes": [
                        {"id": "start", "type": "start"},
                        {
                            "id": "a",
                            "type": "agent",
                            "agent": {"name": "Reader", "system_prompt": "READ", "tools": ["list_folder", "read_file"]},
                        },
                    ],
                    "edges": [{"id": "e", "source": "start", "target": "a"}],
                }
            )
        )
        run = await workspace.wait((await workspace.run(flow, RunIn(input="go"))).id, timeout=20)
        assert run.status == "succeeded", run.error
        outputs = [str(call.output) for call in run.nodes["a"].tool_calls]
        assert "Docs" in outputs[0] and "Docs/notes/plan.txt" in outputs[1] and "fs.parent_reference" in outputs[2]
        assert "Docs/notes/plan.txt" in str(run.nodes["a"].output)
        events = [data for kind, data in emitted if kind == "folder_file_read"]
        assert [(data["folder"], data["grant_id"], data["path"]) for data in events] == [
            ("Docs", view.folder.id, "notes/plan.txt")
        ]
        logged = activity.query(workspace.store, resource_type="folder_grant", resource_id=view.folder.id)
        assert [(event.action, event.outcome, event.agent, event.actor) for event in logged] == [
            ("folder.read", "denied", "Reader", "system"),
            ("folder.read", "success", "Reader", "system"),
        ]
        assert all(event.run_id == run.id and event.node_id == "a" for event in logged)
        assert all("plan" not in json.dumps(event.model_dump(mode="json")) for event in logged)
    finally:
        workspace.close()


def test_a_tool_result_error_is_what_the_model_reads() -> None:
    error = FolderToolError("fs.link", "Links are never followed")
    assert isinstance(error, ToolResultError) and str(error) == "Error: Links are never followed (fs.link)"
    assert {list_folder.name, read_file.name, search_files.name} == {"list_folder", "read_file", "search_files"}
