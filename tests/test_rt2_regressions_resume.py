"""RT2 probe: a tool call that ran and then reported an error (raised after its effect, e.g. a timeout after the
request was sent) is not counted as a side effect by resume.plan, so continuing re-runs it without confirmation."""

from __future__ import annotations

from pathlib import Path

from nodestro_runtime import resume
from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.workspace import Workspace

TOOL = "send_payment"
SOURCE = (
    "def send_payment(amount: str) -> str:\n"
    '    """Send a payment.\n\n    Args:\n        amount: The amount.\n\n'
    '    Returns:\n        A confirmation.\n    """\n'
    "    # the payment was sent here; then the confirmation times out\n"
    "    raise TimeoutError('confirmation timed out after the request was sent')\n"
)
TESTS = "def test_nothing():\n    assert True\n"


async def test_rt2_01_a_call_that_ran_and_failed_needs_confirmation(tmp_path: Path) -> None:
    fake = ScriptedClient({"ACT": [tool_use(TOOL, {"amount": "100"}), text("x", stop_reason="max_tokens")]})
    ws = Workspace(tmp_path / "d", client_factory=lambda _p: fake, integrations=[])
    try:
        result, _ = ws.tools.save_custom(SOURCE, TESTS, TOOL, trust=ws.tools.editor_trust(TOOL, SOURCE))
        assert result.ok, result
        wf = ws.save_workflow(
            WorkflowIn.model_validate(
                {
                    "name": "Pay",
                    "nodes": [
                        {"id": "start", "type": "start"},
                        {
                            "id": "act",
                            "type": "agent",
                            "agent": {"name": "Act", "system_prompt": "ACT", "tools": [TOOL]},
                        },
                    ],
                    "edges": [{"id": "e", "source": "start", "target": "act"}],
                }
            )
        )
        started = await ws.run(wf, RunIn(input="go"))
        failed = await ws.wait(started.id, timeout=30)
        calls = failed.nodes["act"].tool_calls
        print("status", failed.status, "calls", [(c.name, c.is_error, (c.output or "")[:70]) for c in calls])
        plan = resume.plan(ws, failed.id)
        node = next(n for n in plan.nodes if n.node_id == "act")
        print(
            "plan action", node.action, "side_effects", node.side_effects, "needs_confirmation", plan.needs_confirmation
        )
        assert failed.status == "failed" and calls and calls[0].is_error
        assert node.action != "reuse"
        # Desired (RT2-01): a call that ran and then failed may have had its effect; continuing must ask.
        assert plan.needs_confirmation and node.side_effects == [TOOL]
    finally:
        ws.close()
