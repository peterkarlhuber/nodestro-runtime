"""Internal notifications (NAL-A3): rules, the local inbox, idempotency, notify_user and its limit."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime.notifications import (
    COLLECTION,
    MAX_LISTED,
    MAX_MESSAGES_PER_RUN,
    LocalInbox,
    Notification,
    ReadState,
    RunNotifier,
    local_inbox_enabled,
    notification_id,
    notification_key,
    run_events,
)
from nodestro_runtime.schemas import (
    EvaluationRecord,
    NodeRun,
    NotificationRules,
    Run,
    RunIn,
    Workflow,
    WorkflowIn,
    now,
)
from nodestro_runtime.storage import SQLiteStore
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_policy import BUILTIN_CAPABILITIES
from nodestro_runtime.tool_spec import BUILTIN_SPECS
from nodestro_runtime.workspace import Workspace


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    database = SQLiteStore(tmp_path / "nodestro.db")
    database.migrate(before_migration=lambda current, target: None)
    yield database
    database.close()


def _run(status: str = "failed", **changes: Any) -> Run:
    workflow = Workflow(name="Invoice check")
    data: dict[str, Any] = {
        "workflow_id": workflow.id,
        "workflow_name": workflow.name,
        "workflow": workflow,
        "status": status,
        "error_code": "run.nodes_failed" if status == "failed" else None,
        "started_by": "local",
    }
    data.update(changes)
    return Run(**data)


# ---- rules and events -------------------------------------------------------------------------------------------


def test_rules_default_to_failures_and_waits_and_keep_a_fixed_order() -> None:
    assert NotificationRules().events == ["run_failed", "approval_pending"]
    assert WorkflowIn().notifications.recipients == "members"
    rules = NotificationRules(events=["run_finished", "run_failed", "run_finished"])
    assert rules.events == ["run_failed", "run_finished"]
    # A workflow stored before NAL-A3 reads with the default.
    old = Workflow.model_validate({"name": "Old", "nodes": [], "edges": []})
    assert old.notifications == NotificationRules()


def test_events_of_runs_in_each_state() -> None:
    assert [event for event, _r, _p in run_events(_run("failed"))] == ["run_failed", "run_finished"]
    assert [event for event, _r, _p in run_events(_run("succeeded"))] == ["run_finished"]
    checked = _run(
        "succeeded", nodes={"a": NodeRun(evaluation=EvaluationRecord(passed=False, attempts=2, feedback=""))}
    )
    assert [event for event, _r, _p in run_events(checked)] == ["run_finished", "quality_failed"]
    waiting = _run("paused", nodes={"ok": NodeRun(status="awaiting_approval")})
    assert run_events(waiting) == [("approval_pending", f"{waiting.id}:ok:0", {"node": "ok"})]
    # Runs of sub-workflows and imported runs never notify.
    assert run_events(_run("failed", parent_run_id="run_parent")) == []
    assert run_events(_run("failed", imported_at=now())) == []


# ---- the inbox --------------------------------------------------------------------------------------------------


def test_each_event_notifies_once(store: SQLiteStore) -> None:
    inbox = LocalInbox(store)
    notifier = RunNotifier(inbox)
    run = _run("failed")
    notifier(run)
    notifier(run)  # a retry, a resume or a second worker
    RunNotifier(LocalInbox(store))(run)
    items = inbox.for_user("local")
    assert [item.kind for item in items] == ["run_failed"]
    item = items[0]
    assert item.id == notification_id(notification_key("run_failed", run.id))
    assert item.params == {"error_code": "run.nodes_failed"} and item.workflow_name == "Invoice check"
    # No arguments, outputs or error texts of the run.
    assert item.title == item.text == ""


def test_rules_choose_events_and_recipients(store: SQLiteStore) -> None:
    inbox = LocalInbox(store)
    notifier = RunNotifier(inbox)
    quiet = _run("failed")
    quiet.workflow.notifications = NotificationRules(events=[])
    notifier(quiet)
    assert inbox.for_user("local") == []
    mine = _run("succeeded", started_by="usr_a")
    mine.workflow.notifications = NotificationRules(events=["run_finished"], recipients="starter")
    notifier(mine)
    assert [item.kind for item in inbox.for_user("usr_a")] == ["run_finished"]
    assert inbox.for_user("usr_b") == []
    assert inbox.for_user("usr_a")[0].level == "success"


def test_read_state_is_per_person(store: SQLiteStore) -> None:
    inbox = LocalInbox(store)
    notifier = RunNotifier(inbox)
    first = _run("failed")
    notifier(first)
    assert inbox.unread("usr_a") == inbox.unread("usr_b") == 1
    assert inbox.mark_read("usr_a", ["ntf_unknown"]) == 0
    assert inbox.mark_read("usr_a") == 1
    assert inbox.mark_read("usr_a") == 0
    assert inbox.unread("usr_a") == 0 and inbox.unread("usr_b") == 1
    # Marking all is a time stamp: a newer message is unread again.
    second = _run("failed")
    notifier(second)
    assert inbox.unread("usr_a") == 1
    newest = inbox.for_user("usr_b")[0]
    assert inbox.mark_read("usr_b", [newest.id, newest.id]) == 1 and inbox.unread("usr_b") == 1
    # The messages themselves never change.
    assert inbox.get(newest.id) == newest


def test_a_message_for_one_person_cannot_be_marked_by_another(store: SQLiteStore) -> None:
    inbox = LocalInbox(store)
    mine = _run("failed", started_by="usr_a")
    mine.workflow.notifications = NotificationRules(recipients="starter")
    RunNotifier(inbox)(mine)
    [item] = inbox.for_user("usr_a")
    assert inbox.mark_read("usr_b", [item.id]) == 0 and inbox.read_state("usr_b") == ReadState()


def test_published_api_runs_never_notify(store: SQLiteStore) -> None:
    inbox = LocalInbox(store)
    RunNotifier(inbox)(_run("failed", trigger="api"))
    assert inbox.recent() == []


def test_listing_reads_only_the_newest_messages(store: SQLiteStore) -> None:
    inbox = LocalInbox(store)
    for number in range(MAX_LISTED + 5):
        key = notification_key("run_failed", f"run_{number}")
        store.put_raw_if_absent(
            COLLECTION,
            notification_id(key),
            Notification(id=notification_id(key), key=key, kind="run_failed").model_dump(mode="json"),
        )
    assert len(inbox.recent()) == MAX_LISTED


def test_old_messages_are_purged(store: SQLiteStore) -> None:
    inbox = LocalInbox(store)
    old = Notification(id="ntf_old", key="k-old", kind="run_failed", created_at=now() - timedelta(days=91))
    store.put_raw(COLLECTION, old.id, old.model_dump(mode="json"))
    RunNotifier(inbox)(_run("failed"))
    assert store.get(COLLECTION, "ntf_old", Notification) is None
    assert len(inbox.recent()) == 1


def test_the_axentra_inbox_replaces_the_local_one_only_on_a_server() -> None:
    assert local_inbox_enabled(server=False, environ={"NODESTRO_AXENTRA_NOTIFY": "1"})
    assert local_inbox_enabled(server=True, environ={})
    assert not local_inbox_enabled(server=True, environ={"NODESTRO_AXENTRA_NOTIFY": "1"})


# ---- notify_user -------------------------------------------------------------------------------------------------


def test_notify_user_writes_internally_only() -> None:
    spec = BUILTIN_SPECS["notify_user"]
    capability = BUILTIN_CAPABILITIES["notify_user"]
    assert (capability.access, capability.reach) == ("write", "internal")
    assert spec.network == "none" and spec.offline and spec.connection_type is None
    assert spec.max_calls_per_run == MAX_MESSAGES_PER_RUN == 3


def test_messages_are_plain_text_and_limited_per_run(store: SQLiteStore) -> None:
    notifier = RunNotifier(LocalInbox(store))
    run = _run("running")
    first = notifier.message(run, "a", "Look\u0000 at <b>this</b>", "Line\nnext‮", "loud")
    item = notifier.inbox.get(first)
    assert item is not None
    assert item.title == "Look at <b>this</b>" and item.text == "Line\nnext" and item.level == "info"
    assert item.user_id == "local" and item.kind == "message"
    notifier.message(run, "a", "Two", "", "warning")
    notifier.message(run, "a", "Three", "", "error")
    with pytest.raises(ValueError, match=r"notify\.limit"):
        notifier.message(run, "a", "Four", "", "info")
    # Another process that lost a race takes the next free key; the limit holds across processes.
    other = RunNotifier(LocalInbox(store))
    with pytest.raises(ValueError, match=r"notify\.limit"):
        other.message(run, "a", "Five", "", "info")
    assert notifier.message(_run("running"), None, "Multi\nline\ttitle", "", "info")
    assert [item.title for item in notifier.inbox.recent() if item.title.startswith("Multi")] == ["Multi line title"]
    with pytest.raises(ValueError, match=r"notify\.empty_title"):
        notifier.message(_run("running"), "a", "  ", "", "info")


def _workspace(tmp_path: Path, script: dict[str, Any]) -> Workspace:
    client = ScriptedClient(script)
    return Workspace(tmp_path / "data", client_factory=lambda _p: client, integrations=[])


async def test_a_failed_run_and_an_agent_message_reach_the_inbox(tmp_path: Path) -> None:
    """Acceptance (plan A3 §10): a failed run notifies once; an agent leaves a message, a fourth one is refused."""
    calls = [tool_use("notify_user", {"title": f"Note {n}", "message": "See"}, call_id=f"t{n}") for n in range(4)]
    workspace = _workspace(tmp_path, {"NOTE": [*calls, text("Done.")], "BREAK": [RuntimeError("down")]})
    try:
        assert workspace.inbox is not None
        flow = workspace.save_workflow(
            WorkflowIn.model_validate(
                {
                    "name": "Notes",
                    "nodes": [
                        {"id": "start", "type": "start"},
                        {
                            "id": "a",
                            "type": "agent",
                            "agent": {"name": "A", "system_prompt": "NOTE", "tools": ["notify_user"]},
                        },
                    ],
                    "edges": [{"id": "e", "source": "start", "target": "a"}],
                }
            )
        )
        run = await workspace.wait((await workspace.run(flow, RunIn(input="go"))).id, timeout=10)
        assert run.status == "succeeded", run.error
        messages = [item for item in workspace.inbox.for_user("local") if item.kind == "message"]
        assert sorted(item.title for item in messages) == ["Note 0", "Note 1", "Note 2"]
        results = [call.output for call in run.nodes["a"].tool_calls]
        assert "limit" in str(results[-1]) and len(results) == 4

        broken = workspace.save_workflow(
            WorkflowIn.model_validate(
                {
                    "name": "Broken",
                    "nodes": [
                        {"id": "start", "type": "start"},
                        {"id": "a", "type": "agent", "agent": {"name": "A", "system_prompt": "BREAK"}},
                    ],
                    "edges": [{"id": "e", "source": "start", "target": "a"}],
                }
            )
        )
        failed = await workspace.wait((await workspace.run(broken, RunIn(input="go"))).id, timeout=10)
        assert failed.status == "failed"
        kinds = [item.kind for item in workspace.inbox.for_user("local") if item.run_id == failed.id]
        assert kinds == ["run_failed"]
    finally:
        workspace.close()


async def test_a_step_waiting_for_approval_notifies(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, {"Draft": [text("Draft text")]})
    try:
        assert workspace.inbox is not None
        flow = workspace.save_workflow(
            WorkflowIn.model_validate(
                {
                    "name": "Review",
                    "nodes": [
                        {"id": "start", "type": "start"},
                        {"id": "a", "type": "agent", "agent": {"name": "A", "system_prompt": "Draft"}},
                        {"id": "ok", "type": "approval", "label": "OK?"},
                    ],
                    "edges": [
                        {"id": "e", "source": "start", "target": "a"},
                        {"id": "f", "source": "a", "target": "ok"},
                    ],
                }
            )
        )
        paused = await workspace.wait((await workspace.run(flow, RunIn(input="go"))).id, timeout=10)
        assert paused.status == "paused"
        assert [item.kind for item in workspace.inbox.for_user("local")] == ["approval_pending"]
    finally:
        workspace.close()
