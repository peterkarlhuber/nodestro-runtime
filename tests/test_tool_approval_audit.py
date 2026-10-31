"""The approval audit trail and the reads the approval API needs (S-2b, decision D10): who decided, granted or
revoked what and when, every refused decision with its code, the tenant's open requests, its pre-grants and the
legacy report of the activation step."""

from __future__ import annotations

from pathlib import Path

import pytest

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.storage import SQLiteStore
from nodestro_runtime.tool_approvals import ToolApprovalRequest, ToolApprovals, ToolPreGrants, audit_events
from nodestro_runtime.tool_policy import ToolCapability
from nodestro_runtime.workspace import Workspace

WRITE = ToolCapability(access="write", reach="external", source="default")


def ask(approvals: ToolApprovals, run_id: str, value: str = "x") -> ToolApprovalRequest:
    return approvals.request(
        run_id=run_id, node_id="a", call_id="toolu_1", tool="mailer", arguments={"to": value}, capability=WRITE
    )


def test_decisions_and_refused_decisions_are_audited_without_arguments() -> None:
    store = SQLiteStore(":memory:")
    approvals = ToolApprovals(store)
    first = ask(approvals, "run_1", "secret@example.org")
    with pytest.raises(NodestroError) as error:
        approvals.decide("run_1", first.id, approved=True, by="usr_2", arguments="not-the-shown-hash")
    assert error.value.code == "tool.approval_stale"
    approvals.decide("run_1", first.id, approved=False, by="usr_1", arguments=first.arguments_hash, comment="no")
    with pytest.raises(NodestroError):
        approvals.decide("run_1", first.id, approved=True, by="usr_3", arguments=first.arguments_hash)

    trail = audit_events(store)
    assert [(item.kind, item.actor, item.outcome) for item in reversed(trail)] == [
        ("decision_refused", "usr_2", "tool.approval_stale"),
        ("decided", "usr_1", "rejected"),
        ("decision_refused", "usr_3", "tool.approval_decided"),
    ]
    assert all(item.tool == "mailer" and item.request_id == first.id for item in trail)
    assert "secret@example.org" not in str(
        [item.model_dump() for item in trail]
    )  # the arguments themselves never go there


def test_the_inbox_lists_open_requests_of_every_run_oldest_first() -> None:
    approvals = ToolApprovals(SQLiteStore(":memory:"))
    first = ask(approvals, "run_b")
    second = ask(approvals, "run_a")
    done = ask(approvals, "run_c")
    approvals.decide("run_c", done.id, approved=True, by="usr_1", arguments=done.arguments_hash)
    assert [item.id for item in approvals.open()] == [first.id, second.id]


def test_pre_grants_record_who_granted_and_who_revoked() -> None:
    store = SQLiteStore(":memory:")
    grants = ToolPreGrants(store)
    common = {"kind": "custom", "capability": WRITE, "version": 2, "by": "usr_1"}
    older = grants.grant(tool="mailer", scope="schedule", scope_id="sch_1", **common)  # type: ignore[arg-type]
    newer = grants.grant(tool="mailer", scope="batch", scope_id="bat_1", **common)  # type: ignore[arg-type]
    revoked = grants.revoke("schedule", "sch_1", older.id, by="usr_9")
    assert revoked.revoked_by == "usr_9" and revoked.revoked_at is not None
    assert [item.id for item in grants.all()] == [newer.id, older.id]
    trail = [(item.kind, item.actor, item.pre_grant_id) for item in reversed(audit_events(store))]
    assert trail == [
        ("pre_granted", "usr_1", older.id),
        ("pre_granted", "usr_1", newer.id),
        ("pre_grant_revoked", "usr_9", older.id),
    ]
    # revoking twice changes nothing and is not audited again
    assert grants.revoke("schedule", "sch_1", older.id, by="usr_8").revoked_by == "usr_9"
    assert len(audit_events(store)) == 3


def test_the_activation_report_is_readable_and_audited(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        assert workspace.legacy_tool_bindings() is None
        report = workspace.stamp_legacy_tool_bindings(by="usr_admin")
        assert workspace.legacy_tool_bindings() == report
        [event] = audit_events(workspace.store)
        assert event.kind == "legacy_stamped" and event.actor == "usr_admin"
        assert event.details["bindings"] == len(report.bindings)
    finally:
        workspace.close()
