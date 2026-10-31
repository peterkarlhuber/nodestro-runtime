"""Regressions found by the platform integration run: approval comments in results, damaged knowledge PDFs."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import io
from pathlib import Path
from typing import Any

from pypdf import PdfWriter
import pytest

from nodestro_runtime.documents import DocumentError
from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text
from nodestro_runtime.workspace import Workspace

Make = Callable[..., Workspace]
#: A reviewer's comment that must never reach a result, a result file or a decision.
SECRET = "INTERNAL-REVIEW-NOTE-7f3a"


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]] | None = None, **options: Any) -> Workspace:
        fake = ScriptedClient(script or {})
        workspace = Workspace(
            tmp_path / f"data{len(opened)}", client_factory=lambda _provider: fake, integrations=[], **options
        )
        workspace.fake = fake  # type: ignore[attr-defined]
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def agent(node_id: str, prompt: str, **config: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "agent",
        "label": node_id.title(),
        "agent": {"name": node_id.title(), "system_prompt": prompt, **config},
    }


def edge(source: str, target: str, handle: str | None = None) -> dict[str, Any]:
    return {"id": f"{source}-{target}", "source": source, "target": target, "source_handle": handle}


def approval_flow(after: list[dict[str, Any]], edges_after: list[dict[str, Any]]) -> WorkflowIn:
    nodes = [
        {"id": "start", "type": "start"},
        agent("draft", "DRAFT"),
        {"id": "check", "type": "approval", "label": "Review", "approval": {"require_comment_on_reject": True}},
        *after,
    ]
    edges = [edge("start", "draft"), edge("draft", "check"), *edges_after]
    return WorkflowIn.model_validate({"name": "Report", "nodes": nodes, "edges": edges})


async def decided(workspace: Workspace, flow: WorkflowIn, decision: str, comment: str) -> Any:
    saved = workspace.save_workflow(flow)
    started = await workspace.run(saved, RunIn(input="Customer complaint"))
    paused = await workspace.wait(started.id, timeout=20)
    assert paused.status == "paused"
    workspace.decide(paused.id, "check", decision, comment)
    return await workspace.wait(paused.id, timeout=20)


@pytest.mark.parametrize("comment", ["", SECRET])
async def test_a_result_node_after_an_approval_keeps_its_shape(make: Make, comment: str) -> None:
    workspace = make({"DRAFT": [text("8D report body")]})
    files = [{"format": fmt} for fmt in ("md", "json", "html", "pdf", "docx")]
    flow = approval_flow(
        [{"id": "out", "type": "output", "output": {"files": files}}], [edge("check", "out", "approved")]
    )
    done = await decided(workspace, flow, "approved", comment)
    assert done.status == "succeeded"
    # With or without a comment the result is the draft alone.
    assert done.output == "8D report body"
    assert done.nodes["out"].output == "8D report body"
    # The comment stays where it belongs: the approval record (audit trail).
    assert done.nodes["check"].approval.comment == comment
    written = workspace.files.list(done.id)
    assert len(written) == len(files) and all(item.error_code is None for item in written)
    for item in written:
        _, data = workspace.files.content(done.id, item.id)
        assert SECRET.encode() not in data, item.format
        if item.format == "md":
            assert b"8D report body" in data


async def test_a_rejection_comment_does_not_reach_the_result(make: Make) -> None:
    workspace = make({"DRAFT": [text("draft text"), text("draft text")]})
    flow = approval_flow(
        [{"id": "out", "type": "output", "output": {"files": [{"format": "md"}]}}],
        [edge("check", "out", "rejected")],
    )
    with pytest.raises(Exception, match="reason"):
        await decided(workspace, flow, "rejected", "")
    done = await decided(workspace, flow, "rejected", SECRET)
    assert done.status == "succeeded" and done.output == "draft text"
    (item,) = workspace.files.list(done.id)
    assert SECRET.encode() not in workspace.files.content(done.id, item.id)[1]


async def test_a_later_agent_still_reads_the_comment(make: Make) -> None:
    workspace = make({"DRAFT": [text("draft")], "REVISE": [text("revised")]})
    flow = approval_flow(
        [agent("revise", "REVISE", task_template="{{Draft}}\n{{Review}}"), {"id": "out", "type": "output"}],
        [edge("check", "revise", "approved"), edge("revise", "out")],
    )
    done = await decided(workspace, flow, "approved", "shorten the intro")
    assert done.output == "revised"
    assert "shorten the intro" in (done.nodes["revise"].prompt or "")


async def test_a_decision_after_an_approval_evaluates_the_result_only(make: Make) -> None:
    workspace = make({"DRAFT": [text('{"score": 9}')], "HIGH": [text("high")], "LOW": [text("low")]})
    condition = {
        "rules": [{"id": "high", "label": "High", "field": "score", "operator": "gte", "value": 5}],
        "on_invalid": "fail",
    }
    flow = approval_flow(
        [
            {"id": "branch", "type": "condition", "label": "Score", "condition": condition},
            agent("high", "HIGH"),
            agent("low", "LOW"),
        ],
        [edge("check", "branch", "approved"), edge("branch", "high", "high"), edge("branch", "low", "default")],
    )
    done = await decided(workspace, flow, "approved", SECRET)
    assert done.status == "succeeded", done.nodes["branch"].error
    assert done.nodes["high"].status == "succeeded" and done.nodes["low"].status == "skipped"


# ---- knowledge uploads ----------------------------------------------------------------------


def _pdf(password: str | None = None) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    if password is not None:
        writer.encrypt(password, owner_password="owner")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("name", "data", "code"),
    [
        ("truncated.pdf", _pdf()[:200], "document.unreadable"),
        ("garbage.pdf", b"%PDF-1.7\n" + b"\x00garbage" * 40, "document.unreadable"),
        ("text.pdf", b"plain text, not a PDF", "document.unreadable"),
        ("empty.pdf", b"", "document.unreadable"),
        ("locked.pdf", _pdf("secret"), "document.encrypted"),
    ],
)
def test_an_unreadable_pdf_is_refused_without_leftovers(make: Make, name: str, data: bytes, code: str) -> None:
    knowledge = make().knowledge
    for _attempt in range(2):  # the same broken file again: the same answer, still nothing stored
        with pytest.raises(DocumentError) as raised:
            knowledge.add(data, name=name, media_type="application/pdf")
        assert raised.value.code == code
        assert knowledge.files() == []
        assert knowledge.index.search("garbage", 5) == []


def test_readable_pdfs_are_still_stored(make: Make) -> None:
    knowledge = make().knowledge
    # A blank page is a scan (no text layer); a PDF with an empty user password opens without one.
    scanned = knowledge.add(_pdf(), name="scan.pdf")
    assert scanned.scanned and scanned.ocr in (None, "unavailable")
    assert knowledge.add(_pdf(""), name="owner-only.pdf").name == "owner-only.pdf"
    assert len(knowledge.files()) == 2


def test_a_failure_while_indexing_removes_the_stored_file(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    knowledge = make().knowledge

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(knowledge.index, "add", broken)
    with pytest.raises(OSError, match="disk full"):
        knowledge.add(b"some notes", name="notes.md")
    assert knowledge.files() == []


def test_the_ocr_note_counts_pages_and_words_correctly() -> None:
    from nodestro_runtime.documents import OcrResult, ocr_intro

    single = ocr_intro(OcrResult(text="x", pages=1, uncertain=0, words=1, language="eng"))
    assert "(1 page, 0 of 1 word uncertain" in single
    several = ocr_intro(OcrResult(text="x", pages=3, uncertain=2, words=40, language="eng"))
    assert "(3 pages, 2 of 40 words uncertain" in several


async def test_event_numbering_continues_after_a_restart(tmp_path: Path) -> None:
    flow = approval_flow(
        [agent("send", "SEND"), {"id": "out", "type": "output"}],
        [edge("check", "send", "approved"), edge("send", "out")],
    )
    fake = ScriptedClient({"DRAFT": [text("draft")], "SEND": [text("sent")]})
    first = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
    saved = first.save_workflow(flow)
    started = await first.run(saved, RunIn(input="x"))
    paused = await first.wait(started.id, timeout=20)
    assert paused.status == "paused"
    seen = first.runs._seq[paused.id]
    assert seen > 3
    first.close()

    # A new process: the client still remembers ``seen`` as its Last-Event-ID.
    second = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
    try:
        second.decide(paused.id, "check", "approved", "")
        history, queue = second.runs.subscribe(paused.id)
        done = await second.wait(paused.id, timeout=20)
        assert done.status == "succeeded" and done.output == "sent"
        events = [*history]
        while not queue.empty():
            item = queue.get_nowait()
            if item is not None:
                events.append(item)
        numbers = [event.seq for event in events]
        assert numbers and numbers[0] == seen + 1
        assert numbers == sorted(set(numbers))
        # Finished: the stored counter is gone again.
        assert second.store.get_raw("run_event_seq", paused.id) is None
    finally:
        second.close()


# ---- Python libraries of generated tools -----------------------------------------------------


def test_generated_tools_never_build_a_package_while_resolving(make: Make) -> None:
    from nodestro_runtime.testing import FakeUv

    uv = FakeUv({"onlysource": [("1.0", [], "onlysource\nX = 1\n")]})
    uv.source_only = {"onlysource"}
    workspace = make(python_runner=uv)
    code = '# /// script\n# dependencies = ["onlysource"]\n# ///\nimport onlysource\n'
    outcome = workspace.tools.dependencies.prepare("measure", code, "")
    # Generated code is untrusted (ADR-0006): its libraries would run without isolation, so the tool job
    # does not even resolve them and asks for a standard-library version instead.
    assert uv.builds == [] and outcome.plan is None
    assert outcome.feedback is not None and "standard library" in outcome.feedback
    # The plan a tool job would ask for never builds a package.
    needed = workspace.python.needed_for_sources([("measure", code, "")])
    unbuilt = workspace.python.plan("tool", "measure", needed, builds=False)
    assert uv.builds == [False]
    assert unbuilt.error is not None and unbuilt.error.code == "deps.build_required"
    assert unbuilt.error.params == {"name": "onlysource"}
    # A person who asks for the plan may still resolve it (and then approve the installation).
    plan = workspace.python.plan("tool", "measure", needed)
    assert uv.builds[-1] is True and plan.error is None and plan.lock


def test_uv_no_build_keeps_build_scripts_from_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tarfile

    from nodestro_runtime.python_envs import UvError, UvRunner, explain_uv_error, find_uv

    if find_uv() is None:
        pytest.skip("uv is not installed")
    marker = tmp_path / "BUILD_SCRIPT_RAN"
    source = tmp_path / "src"
    (source / "harmless_marker").mkdir(parents=True)
    (source / "harmless_marker" / "__init__.py").write_text("")
    # A harmless build script: it only writes a marker file, which must never appear.
    (source / "setup.py").write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n"
        "from setuptools import setup\nsetup(name='harmless-marker', version='1.0', packages=['harmless_marker'])\n"
    )
    links = tmp_path / "links"
    links.mkdir()
    with tarfile.open(links / "harmless_marker-1.0.tar.gz", "w:gz") as archive:
        archive.add(source, arcname="harmless_marker-1.0")
    monkeypatch.setenv("UV_NO_INDEX", "1")
    monkeypatch.setenv("UV_FIND_LINKS", str(links))
    runner = UvRunner(tmp_path / "cache")
    with pytest.raises(UvError) as raised:
        runner.compile(["harmless-marker"], python="3.12", previous="", log=lambda _line: None, builds=False)
    assert explain_uv_error(raised.value.output).code == "deps.build_required"
    assert not marker.exists()


# ---- free text, form values and files together -------------------------------------------------


def _form_flow(task: str) -> WorkflowIn:
    fields = [
        {"name": "customer", "label": "Customer", "type": "text"},
        {"name": "notes", "label": "Notes", "type": "textarea"},
        {"name": "docs", "label": "Documents", "type": "file", "multiple": True},
    ]
    return WorkflowIn.model_validate(
        {
            "name": "Intake",
            "nodes": [
                {"id": "start", "type": "start", "start": {"fields": fields}},
                agent("left", "LEFT", task_template=task),
                agent("right", "RIGHT", task_template=task),
                {"id": "out", "type": "output"},
            ],
            "edges": [edge("start", "left"), edge("start", "right"), edge("left", "out"), edge("right", "out")],
        }
    )


@pytest.mark.parametrize("free_text", ["", "FREE-TEXT-REQUEST"])
@pytest.mark.parametrize("customer", ["", "ACME-CORP"])
@pytest.mark.parametrize("files", [0, 1, 2])
async def test_run_input_keeps_text_form_values_and_files(
    make: Make, free_text: str, customer: str, files: int
) -> None:
    workspace = make({"LEFT": [text("l")], "RIGHT": [text("r")]})
    saved = workspace.save_workflow(_form_flow("Request:\n{{run.input}}"))
    names = [f"evidence-{number}.txt" for number in range(files)]
    ids = [workspace.inputs.add(f"file {name}".encode(), name=name, origin="test").id for name in names]
    values: dict[str, Any] = {"customer": customer} if customer else {}
    if ids:
        values["docs"] = ids
    done = await workspace.wait((await workspace.run(saved, RunIn(input=free_text, values=values))).id, timeout=20)
    assert done.status == "succeeded", done.error
    prompts = {node_id: done.nodes[node_id].prompt or "" for node_id in ("left", "right")}
    # Parallel agents receive the same request.
    assert prompts["left"] == prompts["right"]
    prompt = prompts["left"]
    for expected in [free_text, customer, *names]:
        assert expected in prompt, (expected, prompt)
    for node_id in ("left", "right"):
        assert sorted(item.name for item in done.nodes[node_id].files) == names


def test_the_check_points_out_input_after_a_form(make: Make) -> None:
    from nodestro_runtime.preflight import preflight

    workspace = make()
    codes = {item.code for item in preflight(workspace, _form_flow("{{input}}")).problems}
    assert "check.input_form_only" in codes
    codes = {item.code for item in preflight(workspace, _form_flow("{{run.input}}")).problems}
    assert "check.input_form_only" not in codes
    # Without a form, {{input}} is the whole run input: nothing to point out.
    plain = WorkflowIn.model_validate(
        {
            "name": "Plain",
            "nodes": [{"id": "start", "type": "start"}, agent("one", "ONE", task_template="{{input}}")],
            "edges": [edge("start", "one")],
        }
    )
    assert "check.input_form_only" not in {item.code for item in preflight(workspace, plain).problems}


# ---- one process per data directory --------------------------------------------------------------


def test_a_data_directory_is_used_by_one_workspace_at_a_time(tmp_path: Path) -> None:
    import subprocess
    import sys

    from nodestro_runtime.problems import NodestroError

    first = Workspace(tmp_path / "data", integrations=[])
    try:
        with pytest.raises(NodestroError) as raised:
            Workspace(tmp_path / "data", integrations=[])
        assert raised.value.code == "workspace.in_use"
        # Another process is refused the same way, before it touches any run.
        script = (
            "import sys\nfrom nodestro_runtime.workspace import Workspace\nfrom nodestro_runtime.problems import "
            "NodestroError\ntry:\n    Workspace(sys.argv[1], integrations=[])\nexcept NodestroError as error:\n"
            "    print(error.code)\n"
        )
        other = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path / "data")], capture_output=True, text=True, timeout=120
        )
        assert other.stdout.strip() == "workspace.in_use", other.stderr
    finally:
        first.close()
    # Closed: the directory is free again.
    Workspace(tmp_path / "data", integrations=[]).close()
