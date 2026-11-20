"""The activity log (N-13): one event per file, tool, workflow and agent action, never contents or secrets.

Every source has its own test that counts exactly one event per action; the canary test puts one value into a
prompt, tool arguments, a tool's output, file contents and names, a connection secret and an MCP token, and checks
that neither the stored events nor their exports contain it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
import csv
from datetime import timedelta
import io
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError
import pytest

from nodestro_runtime import activity
from nodestro_runtime.activity import ActivityEvent
from nodestro_runtime.connections import ConnectionIn
from nodestro_runtime.mcp.models import McpServerIn
from nodestro_runtime.schemas import Run, RunIn, WorkflowIn, now
from nodestro_runtime.storage import SQLiteStore
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import ToolApprovalEvent, ToolApprovals, ToolGate, WaitingSlots, record_audit
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.workspace import Workspace

CANARY = "CANARY-7d1e5b0c-never-in-the-activity-log"
Make = Callable[..., Workspace]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    opened = SQLiteStore(tmp_path / "store.db")
    yield opened
    opened.close()


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]] | None = None) -> Workspace:
        fake = ScriptedClient(script or {"AGENT": [text("done")]})
        workspace = Workspace(tmp_path / f"data{len(opened)}", client_factory=lambda _provider: fake, integrations=[])
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def flow(name: str = "Helper", tools: list[str] | None = None) -> WorkflowIn:
    config = {"name": name, "system_prompt": "AGENT", "tools": tools or []}
    return WorkflowIn.model_validate(
        {
            "name": "Activity",
            "nodes": [{"id": "start", "type": "start"}, {"id": "a", "type": "agent", "agent": config}],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )


async def run(workspace: Workspace, workflow: WorkflowIn, prompt: str = "x", **start: Any) -> Run:
    saved = workspace.save_workflow(workflow)
    started = await workspace.run(saved, RunIn(input=prompt), **start)
    done = await workspace.wait(started.id, timeout=20)
    assert done.status in ("succeeded", "failed"), done.error
    return done


def events(store: Any, **filters: Any) -> list[ActivityEvent]:
    return activity.query(store, limit=None, **filters)


def actions(store: Any, **filters: Any) -> list[str]:
    return sorted(event.action for event in events(store, **filters))


# ---- the event --------------------------------------------------------------------------------------------------


def test_the_event_has_the_documented_fields_and_nothing_else() -> None:
    assert tuple(ActivityEvent.model_fields) == activity.FIELDS
    with pytest.raises(ValidationError):
        ActivityEvent(source="tool_call", action="tool.called", arguments={"a": 1})  # type: ignore[call-arg]


def test_free_text_never_becomes_a_code_or_an_identifier() -> None:
    event = ActivityEvent(
        source="workflow",
        action="workflow.finished",
        code="The run failed: secret in here",
        actor="line\nbreak",
        resource_id="x" * 500,
    )
    assert event.code is None and event.actor is None and event.resource_id == ""
    with pytest.raises(ValidationError):
        ActivityEvent(source="workflow", action="not a code")


def test_writing_never_fails_the_action(store: SQLiteStore) -> None:
    store.close()
    assert activity.add(store, "workflow", "workflow.started") is None
    assert activity.add(store, "workflow", "not a code") is None


# ---- query, export, retention ---------------------------------------------------------------------------------


def _fill(store: Any, count: int) -> list[ActivityEvent]:
    start = now() - timedelta(hours=1)
    found = []
    for number in range(count):
        event = activity.add(
            store,
            "tool_call" if number % 2 else "file",
            "tool.called" if number % 2 else "file.uploaded",
            at=start + timedelta(seconds=number),
            actor=f"usr_{number % 3}",
            resource_type="tool",
            resource_id=f"tool_{number}",
            run_id="run_1" if number < 5 else None,
        )
        assert event is not None
        found.append(event)
    return found


def test_query_filters_and_pages_newest_first(store: SQLiteStore) -> None:
    written = _fill(store, 12)
    found = activity.query(store)
    assert [event.id for event in found] == [event.id for event in reversed(written)]
    assert {event.source for event in activity.query(store, sources=["file"])} == {"file"}
    assert len(activity.query(store, actions=["tool.called"])) == 6
    assert {event.actor for event in activity.query(store, actor="usr_1")} == {"usr_1"}
    assert len(activity.query(store, run_id="run_1")) == 5
    assert [event.id for event in activity.query(store, since=written[9].at)] == [e.id for e in written[:8:-1]]
    assert [event.id for event in activity.query(store, until=written[2].at)] == [written[1].id, written[0].id]
    pages: list[ActivityEvent] = []
    before = None
    while True:
        page = activity.query(store, limit=5, before=before)
        if not page:
            break
        pages += page
        before = page[-1].key
    assert [event.id for event in pages] == [event.id for event in found]


def test_an_export_is_complete_in_both_formats(store: SQLiteStore) -> None:
    written = _fill(store, 30)
    selected = activity.query(store, limit=None)
    lines = list(activity.export(selected, "jsonl"))
    assert len(lines) == 30 and all(line.endswith("\n") for line in lines)
    rows = [json.loads(line) for line in lines]
    assert {row["id"] for row in rows} == {event.id for event in written}
    assert all(tuple(row) == activity.FIELDS for row in rows)
    table = list(csv.reader(io.StringIO("".join(activity.export(selected, "csv")))))
    assert tuple(table[0]) == activity.FIELDS and len(table) == 31
    assert {row[0] for row in table[1:]} == {event.id for event in written}
    with pytest.raises(ValueError):
        list(activity.export(selected, "xml"))  # type: ignore[arg-type]


def test_csv_cells_never_start_a_formula(store: SQLiteStore) -> None:
    activity.add(store, "agent", "agent.finished", agent="=HYPERLINK(1)")
    (_, row) = list(csv.reader(io.StringIO("".join(activity.export(activity.query(store), "csv")))))
    assert row[activity.FIELDS.index("agent")] == "'=HYPERLINK(1)"


def test_retention_is_configurable(store: SQLiteStore) -> None:
    assert activity.retention_days({}) == activity.DEFAULT_RETENTION_DAYS
    assert activity.retention_days({activity.RETENTION_ENV: "30"}) == 30
    assert activity.retention_days({activity.RETENTION_ENV: "0"}) is None
    assert activity.retention_days({activity.RETENTION_ENV: "-1"}) == activity.DEFAULT_RETENTION_DAYS
    assert activity.retention_days({activity.RETENTION_ENV: "soon"}) == activity.DEFAULT_RETENTION_DAYS
    old = activity.add(store, "workflow", "workflow.started", at=now() - timedelta(days=40))
    new = activity.add(store, "workflow", "workflow.started", at=now() - timedelta(days=10))
    assert old is not None and new is not None
    assert activity.prune(store, days=None) == 0
    assert activity.prune(store, days=30) == 1
    assert [event.id for event in activity.query(store)] == [new.id]


def test_the_desktop_prunes_when_the_workspace_opens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = Workspace(tmp_path / "data", integrations=[])
    activity.add(workspace.store, "workflow", "workflow.started", at=now() - timedelta(days=8))
    workspace.close()
    monkeypatch.setenv(activity.RETENTION_ENV, "7")
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        assert activity.query(workspace.store, actions=["workflow.started"]) == []
    finally:
        workspace.close()


# ---- mirroring --------------------------------------------------------------------------------------------------


class Sink:
    def __init__(self) -> None:
        self.audits: list[dict[str, Any]] = []

    def audit(self, tenant_id: str, **fields: Any) -> None:
        self.audits.append({"tenant": tenant_id, **fields})

    def usage(self, tenant_id: str, entry: Any) -> None:
        pass


def test_only_events_without_a_mirrored_source_record_are_mirrored(store: SQLiteStore) -> None:
    sink = Sink()
    store.platform_events = sink  # type: ignore[attr-defined]
    own = activity.add(store, "tool_call", "tool.called", actor="usr_1", resource_type="tool", resource_id="calc")
    record_audit(store, ToolApprovalEvent(kind="decided", actor="usr_1", tool="calc", run_id="run_1"))
    assert own is not None
    approval = [item for item in sink.audits if item["action"].startswith("nodestro.tool_approval.")]
    mirrored = [item for item in sink.audits if item["action"] == "nodestro.tool.called"]
    assert len(approval) == 1 and len(mirrored) == 1 and len(sink.audits) == 2
    assert mirrored[0]["event_id"] == own.id and mirrored[0]["details"]["nodestro_event"] == own.id
    assert (mirrored[0]["target_type"], mirrored[0]["target_id"], mirrored[0]["outcome"]) == ("tool", "calc", "success")
    assert len(events(store)) == 2


# ---- one event per action, per source ----------------------------------------------------------------------------


async def test_a_run_logs_its_workflow_agent_and_tool_actions_once(make: Make) -> None:
    workspace = make({"AGENT": [tool_use("calculator", {"expression": "6*7"}), text("42")]})
    done = await run(workspace, flow(tools=["calculator"]), started_by="usr_7")
    assert done.status == "succeeded", done.error
    store = workspace.store
    assert actions(store, run_id=done.id) == ["agent.finished", "tool.called", "workflow.finished", "workflow.started"]
    (tool,) = events(store, actions=["tool.called"])
    assert (tool.agent, tool.resource_type, tool.resource_id, tool.node_id) == ("Helper", "tool", "calculator", "a")
    assert (tool.actor, tool.outcome, tool.code) == ("usr_7", "success", None)
    (agent,) = events(store, sources=["agent"])
    assert (agent.agent, agent.resource_id, agent.outcome, agent.code) == ("Helper", "a", "success", "succeeded")
    (finished,) = events(store, actions=["workflow.finished"])
    assert (finished.resource_type, finished.resource_id) == ("workflow", done.workflow_id)
    assert (finished.outcome, finished.code) == ("success", "succeeded")


async def test_each_tool_call_is_one_event_with_its_result(make: Make) -> None:
    script = {
        "AGENT": [
            tool_use("calculator", {"expression": "1+1"}, call_id="t1"),
            tool_use("calculator", {"expression": "not maths"}, call_id="t2"),
            text("done"),
        ]
    }
    workspace = make(script)
    done = await run(workspace, flow(tools=["calculator"]))
    calls = events(workspace.store, actions=["tool.called"])
    assert len(calls) == 2 == len(done.nodes["a"].tool_calls)
    assert sorted(event.outcome for event in calls) == ["failure", "success"]
    assert {event.actor for event in calls} == {"system"}  # nobody known started it


async def test_a_rejected_call_is_one_denied_event(make: Make) -> None:
    tool = "celsius_to_fahrenheit"
    workspace = make({"AGENT": [tool_use(tool, {"celsius": 100}), text("done")]})
    trust = workspace.tools.editor_trust(tool, CUSTOM_TOOL_TEMPLATE)
    result, _ = workspace.tools.save_custom(CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, tool, trust=trust)
    assert result.ok, result
    approvals = ToolApprovals(workspace.store, poll_seconds=0.02)
    workspace.runs.tool_gate = ToolGate(approvals, slots=WaitingSlots(3))
    saved = workspace.save_workflow(flow(tools=[tool]))
    started = await workspace.run(saved, RunIn(input="x"), started_by="usr_8")
    for _ in range(500):
        waiting = approvals.pending(started.id)
        if waiting:
            break
        await asyncio.sleep(0.02)
    (request,) = waiting
    approvals.decide(started.id, request.id, approved=False, by="usr_9", arguments=request.arguments_hash)
    await workspace.wait(started.id, timeout=20)
    (call,) = events(workspace.store, actions=["tool.called"])
    assert (call.resource_id, call.outcome, call.code) == (tool, "denied", "tool.approval_rejected")
    assert call.actor == "usr_8"
    (decision,) = events(workspace.store, actions=["tool_approval.decided"])
    assert (decision.actor, decision.code, decision.run_id) == ("usr_9", "rejected", started.id)


async def test_a_failed_run_is_one_finished_event_with_its_code(make: Make) -> None:
    workspace = make({"AGENT": []})  # no answer left: the step fails
    done = await run(workspace, flow())
    assert done.status == "failed"
    (finished,) = events(workspace.store, actions=["workflow.finished"])
    assert (finished.outcome, finished.code) == ("failure", done.error_code)
    (agent,) = events(workspace.store, sources=["agent"])
    assert agent.outcome == "failure"


def test_input_files_log_upload_and_deletion(make: Make) -> None:
    workspace = make()
    item = workspace.inputs.add(b"a,b\n1,2\n", name="customers.csv", uploaded_by="usr_2")
    workspace.inputs.delete(item.id, actor="usr_3")
    found = events(workspace.store, sources=["file"])
    assert [(event.action, event.actor, event.resource_type, event.resource_id) for event in reversed(found)] == [
        ("file.uploaded", "usr_2", "input_file", item.id),
        ("file.deleted", "usr_3", "input_file", item.id),
    ]
    assert found[1].code == item.format


def test_the_cleanup_logs_each_removed_file_as_the_system(make: Make) -> None:
    workspace = make()
    item = workspace.inputs.add(b"hello", name="note.txt")
    workspace.inputs.cleanup(at=now() + timedelta(days=3))
    deleted = events(workspace.store, actions=["file.deleted"])
    assert [(event.resource_id, event.actor) for event in deleted] == [(item.id, "system")]


def test_knowledge_files_log_upload_and_deletion(make: Make) -> None:
    workspace = make()
    item = workspace.knowledge.add(b"knowledge text", name="handbook.txt", actor="usr_4")
    workspace.knowledge.delete(item.id, actor="usr_4")
    found = events(workspace.store, sources=["file"])
    assert sorted((event.action, event.resource_type, event.resource_id) for event in found) == [
        ("file.deleted", "knowledge_file", item.id),
        ("file.uploaded", "knowledge_file", item.id),
    ]


def test_every_approval_audit_event_is_one_activity_event(store: SQLiteStore) -> None:
    decided = record_audit(
        store,
        ToolApprovalEvent(
            kind="decided", actor="usr_1", tool="send_mail", run_id="run_1", outcome="rejected", arguments_hash="h"
        ),
    )
    refused = record_audit(
        store, ToolApprovalEvent(kind="decision_refused", actor="usr_2", run_id="run_1", outcome="tool.approval_stale")
    )
    found = {event.source_event: event for event in events(store)}
    assert set(found) == {decided.id, refused.id}
    first = found[decided.id]
    assert (first.action, first.resource_type, first.resource_id) == ("tool_approval.decided", "tool", "send_mail")
    assert (first.outcome, first.code, first.run_id) == ("success", "rejected", "run_1")
    second = found[refused.id]
    assert (second.outcome, second.code, second.resource_type) == ("denied", "tool.approval_stale", "run")


def test_every_mcp_event_is_one_activity_event(make: Make) -> None:
    workspace = make()
    server = workspace.mcp.add(McpServerIn(name="Docs", url="https://mcp.example/mcp"), actor="usr_5")
    workspace.mcp.event("call_refused", actor=None, server_id=server.id, tool="search", code="mcp.not_approved")
    found = events(workspace.store, sources=["mcp"])
    stored = workspace.mcp.events()
    assert {event.source_event for event in found} == {item.id for item in stored} and len(found) == len(stored)
    added = next(event for event in found if event.action == "mcp.server_added")
    assert (added.actor, added.resource_type, added.resource_id, added.outcome) == (
        "usr_5",
        "mcp_server",
        server.id,
        "success",
    )
    refused = next(event for event in found if event.action == "mcp.call_refused")
    assert (refused.resource_type, refused.resource_id) == ("mcp_tool", f"{server.id}/search")
    assert (refused.outcome, refused.code) == ("denied", "mcp.not_approved")


def test_every_connection_event_is_one_activity_event(make: Make) -> None:
    workspace = make()
    settings = {"host": "smtp.example.com", "port": 587, "from_address": "bot@example.com"}
    item = workspace.connections.create(ConnectionIn(type="smtp", name="Mail", settings=settings), actor="usr_6")
    workspace.connections.set_secret(item.id, "pw", actor="usr_6")
    workspace.connections.delete(item.id, actor="usr_6")
    found = events(workspace.store, sources=["connection"])
    stored = workspace.connections.events()
    assert {event.source_event for event in found} == {record.id for record in stored} and len(found) == len(stored)
    assert {(event.actor, event.resource_type, event.resource_id) for event in found} == {
        ("usr_6", "service_connection", item.id)
    }
    assert sorted(event.action for event in found) == sorted(f"connection.{record.kind}" for record in stored)


# ---- no contents, no secrets ------------------------------------------------------------------------------------


async def test_no_secret_and_no_content_reaches_the_log(make: Make) -> None:
    script = {
        "AGENT": [
            tool_use("calculator", {"expression": f"1+1 # {CANARY}"}, call_id="t1"),
            text(f"The answer mentions {CANARY}"),
        ]
    }
    workspace = make(script)
    upload = workspace.inputs.add(f"name,secret\nx,{CANARY}\n".encode(), name=f"{CANARY}.csv", uploaded_by="usr_1")
    knowledge = workspace.knowledge.add(f"text {CANARY}".encode(), name=f"{CANARY}.txt")
    settings = {"host": "smtp.example.com", "port": 587, "from_address": "bot@example.com"}
    connection = workspace.connections.create(ConnectionIn(type="smtp", name="Mail", settings=settings))
    workspace.connections.set_secret(connection.id, CANARY)
    server = workspace.mcp.add(McpServerIn(name="Docs", url="https://mcp.example/mcp", token=CANARY))
    record_audit(
        workspace.store,
        ToolApprovalEvent(kind="decided", actor="usr_1", tool="calculator", run_id="run_1", details={"x": CANARY}),
    )
    done = await run(workspace, flow(tools=["calculator"]), prompt=f"Please use {CANARY}")
    workspace.inputs.delete(upload.id)
    workspace.knowledge.delete(knowledge.id)
    workspace.mcp.remove(server.id)
    # The canary is where it belongs (the run keeps its prompt and the call's arguments) ...
    assert CANARY in done.input and CANARY in json.dumps(done.nodes["a"].tool_calls[0].input)
    # ... and nowhere in the activity log, stored or exported.
    found = events(workspace.store)
    assert {event.source for event in found} >= {"tool_call", "agent", "workflow", "file", "connection", "mcp"}
    assert {"tool_approval"} <= {event.source for event in found}
    stored = [raw for _, raw in workspace.store.search_raw([activity.COLLECTION], "")]
    assert stored and all(CANARY.lower() not in raw.lower() for raw in stored)
    for fmt in ("jsonl", "csv"):
        exported = "".join(activity.export(found, fmt))  # type: ignore[arg-type]
        assert CANARY not in exported and len(exported) > 0
