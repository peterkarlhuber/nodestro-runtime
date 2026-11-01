"""Regression tests of the reliability round: events, lifecycle, recovery, approvals, storage and security."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
import io
from pathlib import Path
import sqlite3
import sys
import threading
from typing import Any
import zipfile

import orkexa
from orkexa import Tool
import pytest

from nodestro_runtime import engine
from nodestro_runtime.documents import xlsx_sheets
from nodestro_runtime.engine import check_workflow, node_problem
from nodestro_runtime.extensions import ExtensionError, Extensions, Integration
from nodestro_runtime.knowledge import ScopedIndex
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.python_envs import EnvChange, InstallState, PythonEnvironment, UvError, UvRunner, uv_environment
from nodestro_runtime.schemas import Run, RunIn, Usage, Workflow, WorkflowIn
from nodestro_runtime.testing import FakeUv, ScriptedClient, message, text
from nodestro_runtime.tool_library import ToolJob, _Decision
from nodestro_runtime.workspace import Workspace

Make = Callable[..., Workspace]


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]] | None = None, *, data: Path | None = None, **options: Any) -> Workspace:
        fake = ScriptedClient(script or {})
        workspace = Workspace(
            data or tmp_path / f"data{len(opened)}", client_factory=lambda _provider: fake, integrations=[], **options
        )
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


def workflow(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], **extra: Any) -> WorkflowIn:
    return WorkflowIn.model_validate({"name": "Test", "nodes": nodes, "edges": edges, **extra})


def approval_flow() -> WorkflowIn:
    return workflow(
        [
            {"id": "start", "type": "start"},
            agent("draft", "DRAFT"),
            {"id": "check", "type": "approval", "label": "Check"},
            agent("send", "SEND"),
        ],
        [edge("start", "draft"), edge("draft", "check"), edge("check", "send", "approved")],
    )


async def until(condition: Callable[[], bool], timeout: float = 10) -> None:
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def blocking(release: threading.Event, answer: str = "late") -> Callable[[dict[str, Any]], Any]:
    def respond(_request: dict[str, Any]) -> Any:
        release.wait(10)
        return text(answer)

    return respond


# ---- events --------------------------------------------------------------------------


async def test_event_numbers_stay_unique_beyond_the_history_limit(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_MAX_EVENT_HISTORY", 10)
    deltas = message([{"type": "text", "text": f"part {index} "} for index in range(40)])
    workspace = make({"LONG": [deltas]})
    saved = workspace.save_workflow(
        workflow([{"id": "start", "type": "start"}, agent("long", "LONG")], [edge("start", "long")])
    )
    started = await workspace.run(saved, RunIn(input="x"))
    history, queue = workspace.runs.subscribe(started.id)
    received = []
    while (event := await asyncio.wait_for(queue.get(), 10)) is not None:
        received.append(event)
    numbers = [event.seq for event in [*history, *received]]
    assert sum(event.type == "text" for event in received) == 40
    # Strictly increasing by one: a reconnecting client never skips or repeats an event.
    assert numbers == list(range(1, len(numbers) + 1))
    assert started.id not in workspace.runs._seq and started.id not in workspace.runs._events


async def test_a_subscriber_that_stops_reading_is_ended(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_MAX_SUBSCRIBER_BACKLOG", 5)
    runs = make().runs
    _history, idle = runs.subscribe("run_x")
    _history, reader = runs.subscribe("run_x")
    received = []
    for index in range(20):
        runs._publish("run_x", "text", "node", {"delta": str(index)})
        received.append(reader.get_nowait())
    assert [event.seq for event in received] == list(range(1, 21))
    # The idle subscriber was ended: it holds the end of its stream only, not the whole run.
    assert idle.qsize() == 1 and idle.get_nowait() is None
    assert runs._subscribers["run_x"] == [reader]


# ---- approvals -----------------------------------------------------------------------


async def test_an_approval_is_decided_only_once(make: Make) -> None:
    workspace = make({"DRAFT": [text("draft")], "SEND": [text("sent")]})
    saved = workspace.save_workflow(approval_flow())
    started = await workspace.run(saved, RunIn(input="x"))
    paused = await workspace.wait(started.id, timeout=10)
    assert paused.status == "paused"
    workspace.decide(paused.id, "check", "approved")
    with pytest.raises(NodestroError) as caught:
        workspace.decide(paused.id, "check", "rejected")
    assert caught.value.code == "approval.not_pending"
    done = await workspace.wait(paused.id, timeout=10)
    assert done.status == "succeeded" and done.nodes["check"].approval.decision == "approved"  # type: ignore[union-attr]


async def test_a_cancelled_approval_cannot_be_decided(make: Make) -> None:
    workspace = make({"DRAFT": [text("draft")]})
    saved = workspace.save_workflow(approval_flow())
    paused = await workspace.wait((await workspace.run(saved, RunIn(input="x"))).id, timeout=10)
    assert workspace.cancel(paused.id)
    stored = workspace.runs.get(paused.id)
    assert stored is not None and stored.status == "cancelled" and stored.nodes["check"].status == "cancelled"
    with pytest.raises(NodestroError) as caught:
        workspace.decide(paused.id, "check", "approved")
    assert caught.value.code == "approval.not_pending"


async def test_a_decision_from_another_thread_continues_the_run(make: Make) -> None:
    workspace = make({"DRAFT": [text("draft")], "SEND": [text("sent")]})
    saved = workspace.save_workflow(approval_flow())
    paused = await workspace.wait((await workspace.run(saved, RunIn(input="x"))).id, timeout=10)
    # E.g. a synchronous web handler in a worker thread: the call is carried out on the run's loop.
    await asyncio.to_thread(workspace.decide, paused.id, "check", "approved", "ok")
    done = await workspace.wait(paused.id, timeout=10)
    assert done.status == "succeeded" and done.output == "sent"


def test_a_decision_without_an_event_loop_is_refused_before_it_is_recorded(tmp_path: Path) -> None:
    fake = ScriptedClient({"DRAFT": [text("draft")], "SEND": [text("sent")]})
    workspace = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
    try:
        saved = workspace.save_workflow(approval_flow())

        async def start() -> Run:
            return await workspace.wait((await workspace.run(saved, RunIn(input="x"))).id, timeout=10)

        paused = asyncio.run(start())
        with pytest.raises(NodestroError) as caught:
            workspace.decide(paused.id, "check", "approved")
        assert caught.value.code == "run.no_event_loop"
        stored = workspace.runs.get(paused.id)
        assert stored is not None and stored.nodes["check"].approval is None

        async def decide() -> Run:
            workspace.decide(paused.id, "check", "approved")
            return await workspace.wait(paused.id, timeout=10)

        assert asyncio.run(decide()).status == "succeeded"
    finally:
        workspace.close()


# ---- lifecycle -----------------------------------------------------------------------


async def test_shutdown_stores_a_run_that_does_not_stop_in_time_as_cancelled(make: Make, tmp_path: Path) -> None:
    release = threading.Event()
    workspace = make({"SLOW": [blocking(release)]}, data=tmp_path / "data")
    saved = workspace.save_workflow(workflow([agent("slow", "SLOW")], []))
    started = await workspace.run(saved, RunIn(input="x"))
    await until(lambda: started.nodes["slow"].status == "running")
    try:
        await workspace.runs.shutdown(timeout=0.2)
    finally:
        release.set()
    assert not workspace.runs.has_live_runs() and not workspace.runs._tasks
    workspace.close()
    reopened = make(data=tmp_path / "data")
    stored = reopened.runs.get(started.id)
    assert stored is not None and stored.status == "cancelled" and stored.error_code == "run.cancelled"
    assert stored.nodes["slow"].status == "cancelled"


async def test_the_workspace_closes_cleanly_as_a_context_manager(tmp_path: Path) -> None:
    fake = ScriptedClient({"ONE": [text("one")]})
    async with Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[]) as workspace:
        saved = workspace.save_workflow(workflow([agent("one", "ONE")], []))
        done = await workspace.wait((await workspace.run(saved, RunIn(input="x"))).id, timeout=10)
        assert done.status == "succeeded"
    assert workspace.store.closed
    workspace.close()  # closing again is harmless
    with Workspace(tmp_path / "data", integrations=[]) as again:
        assert again.runs.get(done.id) is not None


def test_runs_need_an_event_loop_and_store_nothing_without_one(make: Make) -> None:
    workspace = make()
    saved = workspace.save_workflow(workflow([agent("one", "ONE")], []))
    with pytest.raises(NodestroError) as caught:
        workspace.runs.start(saved, RunIn(input="x"))
    assert caught.value.code == "run.no_event_loop"
    assert workspace.store.list("runs", Run) == []


async def test_an_interrupted_run_is_recovered_and_continued_without_paying_twice(make: Make, tmp_path: Path) -> None:
    release = threading.Event()
    first = make({"ONE": [text("one")], "TWO": [blocking(release)]}, data=tmp_path / "before")
    flow = first.save_workflow(
        workflow(
            [{"id": "start", "type": "start"}, agent("one", "ONE"), agent("two", "TWO")],
            [edge("start", "one"), edge("one", "two")],
        )
    )
    started = await first.run(flow, RunIn(input="x"))
    await until(lambda: started.nodes["two"].status == "running")
    # The process "dies" now: a copy of the database file as it is on disk at this moment.
    (tmp_path / "after").mkdir()
    with sqlite3.connect(tmp_path / "after" / "nodestro.db") as copy:
        first.store._connection.backup(copy)
    release.set()
    await first.wait(started.id, timeout=10)

    second = make({"TWO": [text("two")]}, data=tmp_path / "after")
    interrupted = second.runs.get(started.id)
    assert interrupted is not None and interrupted.status == "failed"
    assert interrupted.error_code == "run.interrupted" and interrupted.nodes["one"].status == "succeeded"
    continued = await second.wait((await second.resume(started.id)).id, timeout=10)
    assert continued.status == "succeeded" and continued.output == "two"
    assert continued.nodes["one"].reused_from == started.id
    # Only the step that ran again is paid in the continued run.
    assert continued.usage.requests == 1


async def test_recovery_finds_interrupted_runs_among_many_newer_ones(make: Make, tmp_path: Path) -> None:
    workspace = make(data=tmp_path / "data")
    saved = workspace.save_workflow(workflow([agent("one", "ONE")], []))
    stale = Run(workflow_id=saved.id, workflow_name=saved.name, workflow=saved, status="running")
    workspace.store.put("runs", stale.id, stale)
    newer = Run(workflow_id=saved.id, workflow_name=saved.name, workflow=saved, status="succeeded")
    rows = [
        ("runs", f"run_newer{index:05d}", newer.model_copy(update={"id": f"run_newer{index:05d}"}).model_dump_json())
        for index in range(5100)
    ]
    with workspace.store.transaction():
        workspace.store._connection.executemany(
            "INSERT INTO nodestro_documents (collection, id, data, updated_at) VALUES (?, ?, ?, '2999-01-01')", rows
        )
    workspace.close()
    reopened = make(data=tmp_path / "data")
    recovered = reopened.runs.get(stale.id)
    assert recovered is not None and recovered.status == "failed" and recovered.error_code == "run.interrupted"


# ---- workflows and storage -----------------------------------------------------------


def test_duplicate_node_ids_are_refused() -> None:
    flow = Workflow.model_validate(
        {
            "name": "x",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "a", "type": "agent", "agent": {"name": "first"}},
                {"id": "a", "type": "agent", "agent": {"name": "second"}},
            ],
            "edges": [edge("start", "a")],
        }
    )
    assert "workflow.node_duplicate" in {issue.code for issue in check_workflow(flow)}


def test_concurrent_saves_get_distinct_version_numbers(make: Make) -> None:
    workspace = make()
    saved = workspace.save_workflow(workflow([agent("one", "ONE")], []))
    ready = threading.Barrier(8)

    def save(index: int) -> None:
        ready.wait(5)
        workspace.save_workflow(saved.model_copy(update={"description": f"variant {index}"}))

    threads = [threading.Thread(target=save, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    numbers = [version.number for version in workspace.versions.all(saved.id)]
    assert numbers == list(range(1, 10))


def test_a_failed_save_stores_neither_workflow_nor_version(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = make()

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(workspace.versions, "_record", broken)
    flow = Workflow.model_validate(workflow([agent("one", "ONE")], []).model_dump())
    with pytest.raises(OSError):
        workspace.save_workflow(flow)
    assert workspace.store.get("workflows", flow.id, Workflow) is None


def test_continued_runs_count_the_known_part_of_an_unknown_cost() -> None:
    run = Run.model_validate(
        {
            "workflow_id": "wf",
            "workflow_name": "x",
            "workflow": {"name": "x"},
            "usage": Usage(estimated_cost_usd=None).model_dump(),
            "nodes": {
                "a": {"usage": {"estimated_cost_usd": 0.4}},
                "b": {"usage": {"estimated_cost_usd": None}},
            },
        }
    )
    assert engine._known_cost(run) == pytest.approx(0.4)


# ---- security ------------------------------------------------------------------------


def test_uv_gets_no_secrets_but_keeps_proxy_certificates_and_its_own_settings() -> None:
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/home/me",
        "LC_ALL": "en_US.UTF-8",
        "HTTPS_PROXY": "http://proxy:3128",
        "no_proxy": "localhost",
        "SSL_CERT_FILE": "/etc/ca.pem",
        "UV_INDEX_URL": "https://index.example/simple",
        "OPENAI_API_KEY": "sk-secret",
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "GEMINI_API_KEY": "secret",
        "GITHUB_TOKEN": "ghp_secret",
        "NODESTRO_READ": "secret",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "MY_PASSWORD": "secret",
        "VIRTUAL_ENV": "/venv",
        "PYTHONPATH": "/src",
    }
    kept = uv_environment(environ)
    assert set(kept) == {"PATH", "HOME", "LC_ALL", "HTTPS_PROXY", "no_proxy", "SSL_CERT_FILE", "UV_INDEX_URL"}


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script as uv")
def test_the_uv_process_really_runs_without_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dump = tmp_path / "env.txt"
    fake = tmp_path / "uv"
    fake.write_text(
        f'#!/bin/sh\nenv > "{dump}"\necho "fetching https://user:hunter2@index.example/simple" >&2\nexit 1\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-0123456789abcdefghijklmnop")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_0123456789abcdefghijklmnopqrstuvwxyz")
    runner = UvRunner(tmp_path / "cache", binary=str(fake))
    log: list[str] = []
    with pytest.raises(UvError) as caught:
        runner.prune_cache(log=log.append)
    seen = dump.read_text()
    assert "OPENAI_API_KEY" not in seen and "GITHUB_TOKEN" not in seen
    assert f"UV_CACHE_DIR={tmp_path / 'cache'}" in seen and "PATH=" in seen
    # Credentials in uv's output reach neither the log nor the stored error.
    assert "hunter2" not in caught.value.output and not any("hunter2" in line for line in log)


def test_stored_error_texts_carry_no_credentials() -> None:
    found = node_problem(
        RuntimeError("request to https://user:hunter2@api.example failed with sk-ant-api03-abcdef0123456789")
    )
    assert "hunter2" not in found.message and "sk-ant-api03" not in found.message


# ---- Python environments -------------------------------------------------------------


class _SlowUv(FakeUv):
    def __init__(self) -> None:
        super().__init__({"wcwidth": [("0.2.0", [], "wcwidth\nWIDTH = 1\n")]})
        self.release = threading.Event()

    def sync(self, interpreter: Path, lock: str, *, log: Callable[[str], None]) -> None:
        self.release.wait(10)
        super().sync(interpreter, lock, log=log)


def test_a_second_installation_of_the_same_environment_is_refused(make: Make) -> None:
    uv = _SlowUv()
    workspace = make(python_runner=uv)
    code = '# /// script\n# dependencies = ["wcwidth"]\n# ///\nimport wcwidth\n'
    needed = workspace.python.needed_for_sources([("measure", code, "")])
    plan = workspace.python.plan("tool", "measure", needed)
    assert plan.error is None and plan.lock
    workspace.python.approve(plan.id)
    try:
        with pytest.raises(NodestroError) as caught:
            workspace.python.approve(plan.id)
        assert caught.value.code == "deps.installing"
    finally:
        uv.release.set()
    for _ in range(500):
        env = workspace.python.get(plan.scope)
        if env is not None and env.install.status != "installing":
            break
        threading.Event().wait(0.02)
    assert env is not None and env.install.status == "installed" and uv.syncs == 1


def test_an_interrupted_installation_is_recovered_at_the_next_start(make: Make, tmp_path: Path) -> None:
    workspace = make(data=tmp_path / "data")
    scope = "workflow:wf_1"
    target = workspace.python.env_dir(scope)
    # Nodestro stopped between moving the working environment aside and moving the new one in.
    old = target.with_name(target.name + ".old")
    old.mkdir(parents=True)
    (old / "nodestro-env.json").write_text("{}")
    target.with_name(target.name + ".staging").mkdir()
    workspace.python._put(
        PythonEnvironment(
            scope=scope,
            kind="workflow",
            ref="wf_1",
            lock="x==1\n",
            install=InstallState(status="installing"),
            history=[EnvChange(kind="approved", details=["x 1"])],
        )
    )
    workspace.close()
    reopened = make(data=tmp_path / "data")
    assert (target / "nodestro-env.json").exists() and not old.exists()
    assert not target.with_name(target.name + ".staging").exists()
    env = reopened.python.get(scope)
    assert env is not None and env.install.status == "failed"
    assert env.install.error is not None and env.install.error.code == "deps.install_interrupted"


# ---- integrations, knowledge, documents, tool jobs -----------------------------------


def _tool(name: str) -> Tool:
    def function(value: str) -> str:
        """Echo.

        Args:
            value: Anything.

        Returns:
            The value.
        """
        return value

    return Tool(function, name=name)


def test_installed_integrations_that_do_not_fit_are_left_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = Integration(name="good", tools=[_tool("echo_good")])
    found = [
        Integration(name="old", contract=0),
        Integration(name="clash", tools=[_tool("calculator")]),
        good,
        Integration(name="twice", tools=[_tool("echo_good")]),
    ]
    monkeypatch.setattr("nodestro_runtime.workspace.discover", lambda: found)
    with Workspace(tmp_path / "data") as workspace:
        assert workspace.extensions.integrations == [good]
        assert "echo_good" in workspace.tools.builtin
    # Integrations passed explicitly must fit.
    with pytest.raises(ExtensionError):
        Extensions(found)


def test_a_scoped_search_finds_assigned_files_in_a_large_knowledge_base(make: Make) -> None:
    workspace = make()
    for index in range(120):
        workspace.knowledge.add(f"refund refund refund {index}".encode(), name=f"noise{index}.txt")
    target = workspace.knowledge.add(b"Our refund policy: 30 days.", name="policy.txt")
    hits = ScopedIndex(workspace.knowledge.index, [target.id]).search("refund", 3)
    assert [hit.source for hit in hits] == [target.id]


def test_a_tool_job_takes_only_the_first_decision(make: Make) -> None:
    workspace = make()
    job = ToolJob(requirement="x", status="awaiting_approval")
    decision = _Decision()
    workspace.tools.jobs[job.id], workspace.tools._decisions[job.id] = job, decision
    workspace.tools.decide(job.id, True)
    with pytest.raises(NodestroError):
        workspace.tools.decide(job.id, False)
    assert decision.approved is True


def test_a_worksheet_with_odd_row_references_is_still_read() -> None:
    sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        '<row r="x"><c r="A1" t="inlineStr"><is><t>first</t></is></c></row>'
        '<row><c t="inlineStr"><is><t>second</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    workbook = (
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Data" r:id="rId1"/></sheets></workbook>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    [found] = xlsx_sheets(buffer.getvalue())
    assert [row[0] for row in found.grid()] == ["first", "second"]


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script as uv")
def test_the_same_requirements_give_the_same_lock_in_every_process(tmp_path: Path) -> None:
    """S-2 pilot F5: the lock named its temporary directory (``# via -r /tmp/nodestro-resolve-…/requirements.in``),
    so every worker start computed another image tag for the runtime image and rebuilt it, which needs the network.
    """
    fake = tmp_path / "uv"
    # Like ``uv pip compile <source> -o <output>``: the output names the source as it was given.
    fake.write_text('#!/bin/sh\nprintf "six==1.17.0\\n    # via -r %s (in %s)\\n" "$3" "$(pwd)" > "$5"\n')
    fake.chmod(0o755)
    runner = UvRunner(tmp_path / "cache", binary=str(fake))
    first, second = (runner.compile(["six"], python="3.13", previous="", log=lambda _: None) for _ in range(2))
    assert "# via -r requirements.in (in " in first
    assert first.split(" (in ")[0] == second.split(" (in ")[0]
    assert "nodestro-resolve-" not in first.split(" (in ")[0]


# ---- provider failures (AD-01) --------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code", "params"),
    [
        (
            orkexa.AuthenticationError("anthropic: AuthenticationError: no", provider="anthropic", status_code=401),
            "run.provider_auth",
            {"provider": "anthropic", "status": 401},
        ),
        (
            orkexa.AuthenticationError("openai: PermissionDeniedError: no", provider="openai", status_code=403),
            "run.provider_auth",
            {"provider": "openai", "status": 403},
        ),
        (
            orkexa.ProviderConnectionError("anthropic: APIConnectionError: Connection error.", provider="anthropic"),
            "run.provider_unreachable",
            {"provider": "anthropic"},
        ),
        (
            orkexa.RateLimitError("openai: RateLimitError: slow down", provider="openai", status_code=429),
            "run.provider_rate_limited",
            {"provider": "openai", "status": 429},
        ),
        (
            orkexa.ProviderServerError("anthropic: OverloadedError: Overloaded", provider="anthropic", status_code=529),
            "run.provider_overloaded",
            {"provider": "anthropic", "status": 529},
        ),
        (
            orkexa.ProviderServerError("gemini: ServerError: unavailable", provider="gemini", status_code=503),
            "run.provider_overloaded",
            {"provider": "gemini", "status": 503},
        ),
        (
            orkexa.ProviderError(
                "anthropic: APIStatusError: {'type': 'overloaded_error'}", provider="anthropic", status_code=200
            ),
            "run.provider_overloaded",
            {"provider": "anthropic", "status": 200},
        ),
        (
            orkexa.ProviderServerError("openai: InternalServerError: boom", provider="openai", status_code=500),
            "run.api_error",
            {"provider": "openai", "status": 500},
        ),
        (
            orkexa.InvalidRequestError("openai: BadRequestError: bad", provider="openai", status_code=400),
            "run.api_error",
            {"provider": "openai", "status": 400},
        ),
    ],
)
def test_provider_failures_get_distinct_codes(error: Exception, code: str, params: dict[str, object]) -> None:
    found = node_problem(error)
    assert (found.code, found.params) == (code, params)
