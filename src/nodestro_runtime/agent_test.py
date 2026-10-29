"""Test one agent on its own, with its current configuration, outside any workflow.

The test uses the same code path as a workflow run (:func:`nodestro_runtime.engine.execute_agent`),
so tools, knowledge, the output format and the quality check behave exactly as in a
run. It is still kept apart from runs:

- The result is returned to the inspector and never stored as a run, so no workflow
  result, run list or "last run" in the editor changes.
- Persistent memory is used read-only: the agent sees a copy of its memory space, and
  whatever it writes stays in the copy (shown in the result, never saved).
- Its tokens and cost go to the usage ledger as an "agent test".
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

from .agent_builder import AgentBuilder
from .engine import execute_agent, node_problem, render_task, usage_of
from .problems import Problem
from .providers import Connection
from .schemas import AgentConfig, EvaluationRecord, ToolCallRecord, Usage, new_id, now

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
    ) -> None:
        self.builder = builder
        self.tool_sandbox = tool_sandbox
        self.connect = connect
        self.default_budget = default_budget

    def _memory(self, config: AgentConfig) -> tuple[AgentConfig, Callable[[], MemoryBackend], bool]:
        """Persistent memory becomes a writable copy, so a test never changes the space."""
        if config.memory.mode != "persistent" or not config.memory.space:
            return config, EphemeralMemory, False
        space = self.builder.memory_spaces.open(config.memory.space)
        files = {entry.path: space.read(entry.path) for entry in space.entries()}
        isolated = config.model_copy(deep=True)
        isolated.memory.mode = "run"
        return isolated, lambda: EphemeralMemory(files), True

    def run(self, request: AgentTestIn) -> tuple[AgentTestResult, BudgetTracker]:
        config = request.agent
        prompt = task_prompt(config, request.input, raw=request.raw)
        budget = self.default_budget()
        tracker = BudgetTracker(Decimal(str(budget)) if budget is not None else None)
        result = AgentTestResult(agent_name=config.name, model=config.model, prompt=prompt)
        calls: dict[str, ToolCallRecord] = {}
        lock = threading.Lock()

        def on_text(delta: str) -> None:
            result.text += delta

        def on_request(_args: dict[str, Any]) -> None:
            tracker.check()

        def before_tool(call: ToolCall) -> None:
            record = ToolCallRecord(id=call.id, name=call.name, input=call.input)
            with lock:
                calls[call.id] = record
                result.tool_calls.append(record)

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
            )
            result.output = outcome.output
            result.evaluation = outcome.evaluation
            result.memory = outcome.memory
        except Exception as exc:  # API errors, budget, invalid configuration: shown in the inspector
            found: Problem = node_problem(exc)
            result.status = "failed"
            result.error, result.error_code, result.error_params = found.message, found.code, found.params
        finally:
            result.usage = usage_of(tracker.usage)
            result.finished_at = now()
        return result, tracker
