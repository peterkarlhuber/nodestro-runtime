"""Test one agent on its own, with its current configuration, outside any workflow.

The test uses the same code path as a workflow run (:func:`nodestro_runtime.engine.execute_agent`),
so tools, knowledge, the output format and the quality check behave exactly as in a
run. It is still kept apart from runs:

- The result is returned to the inspector and never stored as a run, so no workflow
  result, run list or "last run" in the editor changes.
- Persistent memory is used read-only: the agent sees a copy of its memory space, and
  whatever it writes stays in the copy (shown in the result, never saved).
- Its tokens and cost go to the usage ledger as an "agent test".
- The per-call approval rules (S-2) apply as in a run: a call they allow runs, a call they deny is denied, and a call
  that would need a person's approval is denied too, because a test cannot wait for one (independent review H-1).
  Without the approval gate, MCP tools that change data are refused when the agent is built, exactly as in a run.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
import threading
from typing import Any, Literal

from orkexa import EphemeralMemory, MemoryBackend, ToolCall
from orkexa.dynamic import BudgetTracker, Sandbox
from pydantic import BaseModel, Field

from . import tool_policy
from .agent_builder import AgentBuilder
from .cost_tracking import PricedTracker, PriceMode
from .engine import execute_agent, node_problem, render_task, usage_of
from .pricing import PricingService
from .problems import Problem
from .providers import Connection
from .schemas import AgentConfig, EvaluationRecord, ToolCallApproval, ToolCallRecord, Usage, new_id, now
from .tool_approvals import ToolGate, tool_facts

#: Test results show at most this much of a tool result (the run view keeps 20,000 too).
_MAX_TOOL_OUTPUT = 20_000


class AgentTestIn(BaseModel):
    agent: AgentConfig
    #: The test task; it fills ``{{input}}`` and ``{{run.input}}`` of the task template.
    input: str = ""
    #: Use the task text as it is instead of filling it into the task template.
    raw: bool = False
    #: The workflow the agent belongs to: its tools run in the workflow's Python environment.
    workflow_id: str | None = None


class AgentTestResult(BaseModel):
    """Everything the inspector shows after a test; never stored as a workflow run."""

    id: str = Field(default_factory=lambda: new_id("test"))
    kind: Literal["agent_test"] = "agent_test"
    agent_name: str
    model: str
    prompt: str
    status: Literal["succeeded", "failed"] = "succeeded"
    output: Any = None
    text: str = ""
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    error_params: dict[str, Any] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    evaluation: EvaluationRecord | None = None
    memory: dict[str, str] | None = None
    #: True when the agent worked on a copy of its persistent memory space.
    memory_isolated: bool = False
    started_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None


def task_prompt(config: AgentConfig, task: str, *, raw: bool = False) -> str:
    """The message the agent gets in a test: the task template filled with the test task."""
    if raw:
        # The same fallback task as a run's (see engine.render_task); part of existing prompts.
        return task.strip() or "Bitte beginne."
    return render_task(config.task_template, run_input=task, inputs=[])


class AgentTester:
    """Runs agent tests; one call blocks until the agent has answered."""

    def __init__(
        self,
        builder: AgentBuilder,
        *,
        connect: Callable[[str], Connection],
        default_budget: Callable[[], float | None] = lambda: None,
        tool_sandbox: Callable[[str], Sandbox | None] = lambda _workflow_id: None,
        tool_gate: Callable[[], ToolGate | None] = lambda: None,
    ) -> None:
        self.builder = builder
        #: The tenant's per-call approval gate (S-2), read at every test; ``None`` while approvals are off.
        self.tool_gate = tool_gate
        self.tool_sandbox = tool_sandbox
        self.connect = connect
        self.default_budget = default_budget
        #: The workspace's price catalog and price mode (PRICING-1).
        self.pricing: PricingService | None = None
        self.price_mode: Callable[[], PriceMode] = lambda: "standard"

    def _memory(self, config: AgentConfig) -> tuple[AgentConfig, Callable[[], MemoryBackend], bool]:
        """Persistent memory becomes a writable copy, so a test never changes the space."""
        if config.memory.mode != "persistent" or not config.memory.space:
            return config, EphemeralMemory, False
        space = self.builder.memory_spaces.open(config.memory.space)
        files = {entry.path: space.read(entry.path) for entry in space.entries()}
        isolated = config.model_copy(deep=True)
        isolated.memory.mode = "run"
        return isolated, lambda: EphemeralMemory(files), True

    def _gate(
        self, gate: ToolGate | None, config: AgentConfig, call: ToolCall, record: ToolCallRecord, test_id: str
    ) -> str | None:
        """The per-call rules for one call of a test: ``None`` runs it, a string denies it (never waits)."""
        if gate is None:
            return None
        capability, version, kind, _definition = tool_facts(self.builder.tools, call.name)
        situation = gate.call_context(run_id=test_id, node_id="agent-test", trigger="manual", trigger_ref=None)
        verdict = tool_policy.decide(
            tool=call.name,
            kind=kind,
            capability=capability,
            binding=config.tool_policies.get(call.name),
            context=situation,
            at=now(),
            version=version,
        )
        shown = capability.model_dump()
        if verdict.action == "allow":
            record.approval = ToolCallApproval(status="allowed", pre_grant_id=verdict.pre_grant_id, capability=shown)
            return None
        if verdict.action == "deny":
            record.approval = ToolCallApproval(
                status="denied", code=verdict.code or "tool.denied_by_policy", capability=shown
            )
            return verdict.reason or "The tool rules do not allow this call"
        record.approval = ToolCallApproval(status="denied", code="tool.approval_not_in_test", capability=shown)
        return (
            "This call needs a person's approval before it runs; an agent test cannot ask for one, so it was not "
            "made. Run the workflow to approve the call."
        )

    def run(self, request: AgentTestIn) -> tuple[AgentTestResult, BudgetTracker]:
        config = request.agent
        prompt = task_prompt(config, request.input, raw=request.raw)
        budget = self.default_budget()
        tracker = PricedTracker(
            Decimal(str(budget)) if budget is not None else None, pricing=self.pricing, mode=self.price_mode()
        )
        result = AgentTestResult(agent_name=config.name, model=config.model, prompt=prompt)
        calls: dict[str, ToolCallRecord] = {}
        lock = threading.Lock()

        def on_text(delta: str) -> None:
            result.text += delta

        def on_request(_args: dict[str, Any]) -> None:
            tracker.check()

        gate = self.tool_gate()

        def before_tool(call: ToolCall) -> str | None:
            record = ToolCallRecord(id=call.id, name=call.name, input=call.input)
            with lock:
                calls[call.id] = record
                result.tool_calls.append(record)
            return self._gate(gate, config, call, record, result.id)

        def after_tool(call: ToolCall, content: str, is_error: bool) -> None:
            record = calls.get(call.id)
            if record is not None:
                record.output = content[:_MAX_TOOL_OUTPUT]
                record.is_error = is_error
                record.finished_at = now()

        hooks: dict[str, Any] = {
            "on_text": on_text,
            "on_request": on_request,
            "before_tool": before_tool,
            "after_tool": after_tool,
        }
        try:
            built_config, run_memory, isolated = self._memory(config)
            result.memory_isolated = isolated
            outcome = execute_agent(
                self.builder,
                built_config,
                prompt,
                connect=self.connect,
                tracker=tracker,
                name="agent-test",
                hooks=hooks,
                run_memory=run_memory,
                sandbox=self.tool_sandbox(request.workflow_id) if request.workflow_id else None,
                # before_tool applies the per-call rules to every call while the gate exists.
                gated=gate is not None,
            )
            result.output = outcome.output
            result.evaluation = outcome.evaluation
            result.memory = outcome.memory
        except Exception as exc:  # API errors, budget, invalid configuration: shown in the inspector
            found: Problem = node_problem(exc)
            result.status = "failed"
            result.error, result.error_code, result.error_params = found.message, found.code, found.params
        finally:
            result.usage = usage_of(tracker.usage, tracker.records, stale_requests=tracker.stale_requests())
            result.finished_at = now()
        return result, tracker
