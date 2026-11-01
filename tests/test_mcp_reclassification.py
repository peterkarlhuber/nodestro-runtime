"""A tool classified differently while a call waits for its approval (independent re-review R-1).

An approval covers the call as it was shown: arguments, tool version or MCP definition, **and** classification. A
reclassification in either direction while the call waits closes the pending request (``tool.reclassified``), an
approval that was given before the change is refused right before the call runs (``tool.approval_stale``), and a
change that lands between the gate's last check and the request to the MCP server stops the request before it is
sent. The normal path (the run waits in a thread) and the M6b path (the run pauses at an agent checkpoint) give the
same guarantees. Every case counts the requests that reached the MCP server.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
import threading
from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp_support import LoopbackServer, serve

from nodestro_runtime.mcp.models import McpServerIn
from nodestro_runtime.mcp.service import McpService
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import Run, RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import (
    CHECKPOINTS_ENV,
    ENABLE_ENV,
    RECLASSIFIED,
    CallBinding,
    ToolApprovalRequest,
    ToolApprovals,
    audit_events,
    bind_call,
)
from nodestro_runtime.workspace import Workspace

DELETE = "mcp__catalog__delete_page"
SEARCH = "mcp__catalog__search"


@pytest.fixture
def server() -> Iterator[LoopbackServer]:
    handle = serve()
    yield handle
    handle.stop()


def sent(server: LoopbackServer, name: str = "delete_page") -> int:
    """Requests that called ``name`` on the MCP server (the client names the method and tool in its headers)."""
    return len(
        [
            r
            for r in server.requests
            if r["headers"].get("mcp-method") == "tools/call" and r["headers"].get("mcp-name") == name
        ]
    )


def flow(tool: str = DELETE) -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Reclassify",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "a", "type": "agent", "agent": {"name": "A", "system_prompt": "LOOKUP", "tools": [tool]}},
            ],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )


def classify(workspace: Workspace, name: str, access: str) -> None:
    catalog = workspace.mcp.servers()[0]
    tool = workspace.mcp.tool(catalog.id, name)
    workspace.mcp.decide(catalog.id, name, approve=True, access=access, expected_hash=tool.hash, actor="admin")  # type: ignore[arg-type]


def make_workspace(
    tmp_path: Path, server: LoopbackServer, *, access: str = "write", call: tuple[str, dict[str, Any]] | None = None
) -> Workspace:
    name, arguments = call or (DELETE, {"page_id": "7"})
    # One scripted model for every agent build: a resumed turn (M6b) goes on where the script stopped.
    model = ScriptedClient({"LOOKUP": [tool_use(name, arguments), text("done")] * 3})
    workspace = Workspace(tmp_path / "data", client_factory=lambda _p: model, integrations=[])
    added = workspace.mcp.add(McpServerIn(name="Catalog", url=server.url, allow_loopback=True), actor="admin")
    workspace.mcp.discover(added.id)
    classify(workspace, "delete_page", access)
    classify(workspace, "search", "read")
    assert workspace.mcp.write_gate is True
    return workspace


async def started(workspace: Workspace, tool: str = DELETE) -> str:
    saved = workspace.save_workflow(flow(tool))
    run = await workspace.run(saved, RunIn(input="x"))
    return run.id


async def pending(workspace: Workspace, run_id: str) -> ToolApprovalRequest:
    approvals = ToolApprovals(workspace.store)
    for _ in range(1000):
        if found := approvals.pending(run_id):
            return found[0]
        await asyncio.sleep(0.02)
    raise AssertionError("no approval request")


async def finished(workspace: Workspace, run_id: str) -> Run:
    return await workspace.wait(run_id, timeout=30)


def the_call(run: Run) -> Any:
    [call] = run.nodes["a"].tool_calls
    return call


def code(action: Any) -> str:
    try:
        action()
    except NodestroError as exc:
        return exc.code
    raise AssertionError("no refusal")


# ---- the normal path: the run waits in a thread ------------------------------------------------------------------


@pytest.mark.parametrize(("asked", "now"), [("write", "destructive"), ("destructive", "write")])
async def test_a_reclassification_while_waiting_closes_the_request(
    tmp_path: Path, server: LoopbackServer, monkeypatch: pytest.MonkeyPatch, asked: str, now: str
) -> None:
    """Either direction: the pending request is closed at once and nobody can approve it any more."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = make_workspace(tmp_path, server, access=asked)
    try:
        run_id = await started(workspace)
        request = await pending(workspace, run_id)
        assert request.capability.access == asked
        classify(workspace, "delete_page", now)
        closed = ToolApprovals(workspace.store).get(run_id, request.id)
        assert closed is not None and closed.status == "expired" and closed.closed_because == RECLASSIFIED
        refused = code(
            lambda: workspace.runs.decide_tool_call(
                run_id, request.id, approved=True, by="admin", arguments=request.arguments_hash
            )
        )
        assert refused == "tool.approval_stale"
        done = await finished(workspace, run_id)
        call = the_call(done)
        assert call.is_error and call.approval.code == "tool.approval_stale"
        assert sent(server) == 0
        stale = [e for e in audit_events(workspace.store) if e.kind == "stale" and e.request_id == request.id]
        assert [e.outcome for e in stale] == ["reclassified"]
    finally:
        workspace.close()


async def test_an_approval_given_before_the_reclassification_is_stale(
    tmp_path: Path, server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approved as ``write``, then reclassified as ``destructive`` before the waiting run read the decision: the
    engine compares the classification right before the call and refuses it."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = make_workspace(tmp_path, server)
    gate = workspace.runs.tool_gate
    assert gate is not None
    gate.approvals.poll_seconds = 0.5  # the decision is read only at the next look
    try:
        run_id = await started(workspace)
        request = await pending(workspace, run_id)
        ToolApprovals(workspace.store).decide(
            run_id, request.id, approved=True, by="operator", arguments=request.arguments_hash
        )
        classify(workspace, "delete_page", "destructive")  # the approved request is not pending any more
        done = await finished(workspace, run_id)
        call = the_call(done)
        assert call.is_error and call.approval.code == "tool.approval_stale"
        assert sent(server) == 0
        outcomes = [e.outcome for e in audit_events(workspace.store) if e.kind == "stale"]
        assert outcomes == ["reclassified"]
    finally:
        workspace.close()


async def test_a_rejection_after_a_reclassification_is_refused_too(
    tmp_path: Path, server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = make_workspace(tmp_path, server)
    try:
        run_id = await started(workspace)
        request = await pending(workspace, run_id)
        classify(workspace, "delete_page", "destructive")
        refused = code(
            lambda: workspace.runs.decide_tool_call(
                run_id, request.id, approved=False, by="admin", arguments=request.arguments_hash
            )
        )
        assert refused == "tool.approval_stale"
        assert the_call(await finished(workspace, run_id)).approval.code == "tool.approval_stale"
        assert sent(server) == 0
    finally:
        workspace.close()


async def test_a_decision_naming_another_classification_is_refused(
    tmp_path: Path, server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API passes the tool's classification now; if it differs from the asked one (a change the service did not
    record, e.g. an own tool's isolation), an approval is refused and closes the request, a rejection still works."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = make_workspace(tmp_path, server)
    try:
        run_id = await started(workspace)
        request = await pending(workspace, run_id)
        other = request.capability.model_copy(update={"access": "destructive"})
        refused = code(
            lambda: workspace.runs.decide_tool_call(
                run_id, request.id, approved=True, by="operator", arguments=request.arguments_hash, current=other
            )
        )
        assert refused == "tool.approval_stale"
        assert the_call(await finished(workspace, run_id)).approval.code == "tool.approval_stale"
        # Same classification: deciding works as before.
        run_id = await started(workspace)
        request = await pending(workspace, run_id)
        decided = workspace.runs.decide_tool_call(
            run_id,
            request.id,
            approved=False,
            by="operator",
            arguments=request.arguments_hash,
            current=request.capability,
        )
        assert decided.status == "rejected"
        assert the_call(await finished(workspace, run_id)).approval.code == "tool.approval_rejected"
        assert sent(server) == 0
    finally:
        workspace.close()


async def test_after_a_reclassification_a_new_call_is_asked_with_the_new_classification(
    tmp_path: Path, server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stale call is not made; the next run asks again, now as ``destructive``, and runs after the approval."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = make_workspace(tmp_path, server)
    try:
        run_id = await started(workspace)
        await pending(workspace, run_id)
        classify(workspace, "delete_page", "destructive")
        await finished(workspace, run_id)
        run_id = await started(workspace)
        request = await pending(workspace, run_id)
        assert request.capability.access == "destructive"
        workspace.runs.decide_tool_call(
            run_id, request.id, approved=True, by="admin", arguments=request.arguments_hash, current=request.capability
        )
        done = await finished(workspace, run_id)
        assert done.status == "succeeded", done.error
        assert not the_call(done).is_error and sent(server) == 1
    finally:
        workspace.close()


async def test_a_reclassification_right_before_the_send_stops_the_request(
    tmp_path: Path, server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race between the gate's last check and the request: the classification changes after the engine let the
    call run (here: while the service fetches the credential, right before it connects). The service checks again
    after listing the server's tools, right before it sends, and does not send."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = make_workspace(tmp_path, server)
    original = McpService.credential
    armed = threading.Event()

    def credential(self: McpService, found: Any) -> Any:
        if armed.is_set():
            armed.clear()
            classify(workspace, "delete_page", "destructive")
        return original(self, found)

    monkeypatch.setattr(McpService, "credential", credential)
    try:
        run_id = await started(workspace)
        request = await pending(workspace, run_id)
        armed.set()
        workspace.runs.decide_tool_call(
            run_id, request.id, approved=True, by="operator", arguments=request.arguments_hash
        )
        done = await finished(workspace, run_id)
        call = the_call(done)
        assert not armed.is_set(), "the change was not made inside the call"
        assert call.is_error and "tool.approval_stale" in (call.output or "")
        assert sent(server) == 0
    finally:
        workspace.close()


async def test_a_read_tool_reclassified_after_the_gate_allowed_it_is_not_called(
    tmp_path: Path, server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read tool runs without a question; reclassified as ``write`` between the allow and the request, the call
    is refused instead of running as a changing call nobody approved."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = make_workspace(tmp_path, server, call=(SEARCH, {"query": "q"}))
    original = McpService.credential

    def credential(self: McpService, found: Any) -> Any:
        classify(workspace, "search", "write")
        return original(self, found)

    monkeypatch.setattr(McpService, "credential", credential)
    try:
        done = await finished(workspace, await started(workspace, SEARCH))
        call = the_call(done)
        assert call.approval.status == "allowed"
        assert call.is_error and "tool.approval_stale" in (call.output or "")
        assert sent(server, "search") == 0
    finally:
        workspace.close()


def test_the_service_runs_a_gated_call_only_as_bound(tmp_path: Path, server: LoopbackServer) -> None:
    """Directly at the service: no binding, another classification or another definition never send the call."""
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        added = workspace.mcp.add(McpServerIn(name="Catalog", url=server.url, allow_loopback=True), actor="admin")
        workspace.mcp.discover(added.id)
        classify(workspace, "delete_page", "write")
        workspace.mcp.write_gate = True
        tool = workspace.mcp.tool(added.id, "delete_page")
        call = lambda: workspace.mcp.call(DELETE, {"page_id": "7"}, gated=True)  # noqa: E731
        assert code(call) == "tool.approval_stale"
        for binding in (
            CallBinding(tool=DELETE, capability=tool.capability().model_copy(update={"access": "destructive"})),
            CallBinding(tool=DELETE, capability=tool.capability(), definition_hash="sha256:other"),
            CallBinding(tool=SEARCH, capability=tool.capability(), definition_hash=tool.approved_hash),
        ):
            bind_call(binding)
            assert code(call) == "tool.approval_stale", binding
        assert sent(server) == 0
        bind_call(CallBinding(tool=DELETE, capability=tool.capability(), definition_hash=tool.approved_hash))
        assert workspace.mcp.call(DELETE, {"page_id": "7"}, gated=True)[2].text == "deleted 7"
        assert sent(server) == 1
    finally:
        bind_call(None)
        workspace.close()


async def test_parallel_decisions_decide_once_and_the_call_runs_at_most_once(
    tmp_path: Path, server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several people decide the same request at the same time: exactly one decision is stored, every other one is
    refused (``tool.approval_decided``), and the call runs at most once."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = make_workspace(tmp_path, server)
    try:
        run_id = await started(workspace)
        request = await pending(workspace, run_id)
        barrier = threading.Barrier(6)
        outcomes: list[str] = []

        def decide(approved: bool, by: str) -> None:
            barrier.wait()
            try:
                found = workspace.runs.decide_tool_call(
                    run_id, request.id, approved=approved, by=by, arguments=request.arguments_hash
                )
                outcomes.append(found.status)
            except NodestroError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=decide, args=(i % 2 == 0, f"usr_{i}")) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        won = [item for item in outcomes if item in ("approved", "rejected")]
        assert len(won) == 1, outcomes
        assert sorted(set(outcomes) - set(won)) == ["tool.approval_decided"], outcomes
        done = await finished(workspace, run_id)
        assert sent(server) == (1 if won == ["approved"] else 0)
        assert the_call(done).approval.status == won[0]
    finally:
        workspace.close()


@pytest.mark.parametrize("attempt", range(4))
async def test_a_reclassification_racing_an_approval_never_runs_the_call(
    tmp_path: Path, server: LoopbackServer, monkeypatch: pytest.MonkeyPatch, attempt: int
) -> None:
    """An operator approves while an administrator reclassifies the tool as ``destructive``, at the same moment.
    Whichever wins, the call is never made: either the approval is refused, or the engine finds it stale."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = make_workspace(tmp_path, server)
    try:
        run_id = await started(workspace)
        request = await pending(workspace, run_id)
        barrier = threading.Barrier(2)

        def approve() -> None:
            barrier.wait()
            try:
                workspace.runs.decide_tool_call(
                    run_id, request.id, approved=True, by="operator", arguments=request.arguments_hash
                )
            except NodestroError as exc:
                assert exc.code == "tool.approval_stale"

        def reclassify() -> None:
            barrier.wait()
            classify(workspace, "delete_page", "destructive")

        threads = [threading.Thread(target=approve), threading.Thread(target=reclassify)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        call = the_call(await finished(workspace, run_id))
        assert call.is_error and call.approval.code == "tool.approval_stale"
        assert sent(server) == 0
    finally:
        workspace.close()


# ---- the M6b path: the run pauses at an agent checkpoint ---------------------------------------------------------


async def paused(workspace: Workspace) -> tuple[str, ToolApprovalRequest]:
    run_id = await started(workspace)
    run = await workspace.wait(run_id, timeout=20)
    assert run.status == "paused", run.status
    [request] = ToolApprovals(workspace.store).pending(run_id)
    assert request.checkpointed
    return run_id, request


@pytest.mark.parametrize(("asked", "now"), [("write", "destructive"), ("destructive", "write")])
async def test_a_paused_call_reclassified_is_not_made(
    tmp_path: Path, server: LoopbackServer, monkeypatch: pytest.MonkeyPatch, asked: str, now: str
) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(CHECKPOINTS_ENV, "1")
    workspace = make_workspace(tmp_path, server, access=asked)
    try:
        run_id, request = await paused(workspace)
        classify(workspace, "delete_page", now)
        refused = code(
            lambda: workspace.runs.decide_tool_call(
                run_id, request.id, approved=True, by="admin", arguments=request.arguments_hash
            )
        )
        assert refused == "tool.approval_stale"
        workspace.runs.sweep_tool_waits()  # the sweep continues a paused run with no open question
        done = await finished(workspace, run_id)
        call = the_call(done)
        assert call.is_error and call.approval.code == "tool.approval_stale"
        assert sent(server) == 0
    finally:
        workspace.close()


async def test_a_paused_call_approved_before_the_reclassification_is_not_made(
    tmp_path: Path, server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approved, then reclassified before the paused run continued: the approved, unused request is marked
    ``stale`` by the reclassification, and the resumed turn refuses the call."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(CHECKPOINTS_ENV, "1")
    workspace = make_workspace(tmp_path, server)
    try:
        run_id, request = await paused(workspace)
        ToolApprovals(workspace.store).decide(
            run_id, request.id, approved=True, by="operator", arguments=request.arguments_hash
        )  # stored, but the run is not continued yet
        classify(workspace, "delete_page", "destructive")
        marked = ToolApprovals(workspace.store).get(run_id, request.id)
        assert marked is not None and marked.status == "approved" and marked.use == "stale"
        workspace.runs.sweep_tool_waits()
        done = await finished(workspace, run_id)
        call = the_call(done)
        assert call.is_error and call.approval.code == "tool.approval_stale"
        assert sent(server) == 0
    finally:
        workspace.close()


async def test_a_paused_call_reclassified_right_before_the_send_is_not_made(
    tmp_path: Path, server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race on the M6b path: the resumed turn released the call (``executed``), then the classification changed
    before the request was sent. The service refuses it before sending; the call is audited stale."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(CHECKPOINTS_ENV, "1")
    workspace = make_workspace(tmp_path, server)
    original = McpService.credential
    armed = threading.Event()

    def credential(self: McpService, found: Any) -> Any:
        if armed.is_set():
            armed.clear()
            classify(workspace, "delete_page", "destructive")
        return original(self, found)

    monkeypatch.setattr(McpService, "credential", credential)
    try:
        run_id, request = await paused(workspace)
        armed.set()
        workspace.runs.decide_tool_call(
            run_id, request.id, approved=True, by="operator", arguments=request.arguments_hash
        )
        done = await finished(workspace, run_id)
        call = the_call(done)
        assert not armed.is_set(), "the change was not made inside the call"
        assert call.is_error and "tool.approval_stale" in (call.output or "")
        assert sent(server) == 0
        outcomes = [e.outcome for e in audit_events(workspace.store) if e.kind == "stale"]
        assert "tool.reclassified" in outcomes
    finally:
        workspace.close()
