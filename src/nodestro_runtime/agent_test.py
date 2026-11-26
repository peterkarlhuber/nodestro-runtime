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

from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
import threading
from typing import Any, Literal

from orkexa import Denial, EphemeralMemory, MaxToolCallsError, MaxToolRoundsError, MemoryBackend, ToolCall
from orkexa.dynamic import BudgetTracker, Sandbox
from pydantic import BaseModel, Field

from . import offline, tool_policy
from .agent_builder import AgentBuilder, SpaceCopy, run_own_tool
from .cost_tracking import PricedTracker, PriceMode, request_regions
from .engine import (
    access_error,
    execute_agent,
    granted_source,
    is_person,
    node_problem,
    render_task,
    start_sources,
    tool_rounds_problem,
    usage_of,
)
from .memory_consents import agent_digest
from .memory_spaces import NOBODY, consent_required
from .pricing import PricingService
from .problems import Problem
from .providers import Connection, check_model, provider_of
from .reasoning import ReasoningTrace, trace_hook
from .schemas import AgentConfig, EvaluationRecord, ToolCallApproval, ToolCallRecord, Usage, new_id, now
from .tool_approvals import CallBinding, ToolGate, bind_call, record_refusal, tool_facts
from .value_checks import checked_fields, computing_call, unverified_fields

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


class ValueWarning(BaseModel):
    """A key figure (``source="tool"``) that the agent's own computing tool calls in the test do not back (RT-054).

    A workflow run would ask the agent once to correct it and then fail the step (``run.value_unverified``); the
    test only warns, because a step that copies a figure an earlier step computed has no earlier steps here.
    ``reason`` as :class:`~nodestro_runtime.value_checks.Unverified`: ``missing``, ``not_from_tool``,
    ``table_incomplete`` or ``table_changed``."""

    field: str
    value: Any = None
    reason: str
    rows_passed: int | None = None
    rows_in_input: int | None = None


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
    #: ``cpu_saving`` when the local model answered without thinking in the CPU saving mode (as a run's step).
    reasoning_reduced: str | None = None
    #: The reasoning the provider transmitted during the test (text only where the provider sends it).
    reasoning: ReasoningTrace = Field(default_factory=ReasoningTrace)
    evaluation: EvaluationRecord | None = None
    memory: dict[str, str] | None = None
    #: True when the agent worked on a copy of its persistent memory space.
    memory_isolated: bool = False
    #: Key figures no computing tool call of the test backs: shown as warnings, the test still succeeds (RT-054).
    value_warnings: list[ValueWarning] = Field(default_factory=list)
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
        #: What an agent uses outside its configuration, for the digest of a memory consent (RT-089d); the workspace
        #: hands in the engine's (:attr:`~nodestro_runtime.engine.RunManager.consent_means`).
        self.consent_means: Callable[[AgentConfig], Any] = lambda config: sorted(set(config.tools))

    def _memory(
        self, config: AgentConfig, actor: str = "system"
    ) -> tuple[AgentConfig, Callable[[], MemoryBackend], bool]:
        """Persistent memory becomes a writable copy, so a test never changes the space. The space must be the one of
        the person who runs the test where spaces have owners (RT-089c); a test of nobody gets none there. RT-089d: and
        its owner must have allowed exactly this configuration (its digest) to use it: a test of a shared workflow's
        agent runs that agent's tools with the owner's memory, so without the consent it fails with
        ``memory.consent_required`` before anything is read."""
        if config.memory.mode != "persistent" or not config.memory.space:
            return config, EphemeralMemory, False
        owner = actor if is_person(actor) else NOBODY
        spaces = self.builder.memory_spaces
        if spaces.needs_consent(config.memory.space, owner) and not spaces.consented(
            config.memory.space, owner, "agent", None, agent_digest(config, self.consent_means)
        ):
            raise consent_required(config.memory.space, subject="agent")
        space = spaces.open_for(config.memory.space, owner)
        files = {entry.path: space.read(entry.path) for entry in space.entries()}
        isolated = config.model_copy(deep=True)
        isolated.memory.mode = "run"
        # N-08: the copy holds the space's content, so reading it marks the test like the space would.
        return isolated, lambda: SpaceCopy(files), True

    def _gate(
        self,
        gate: ToolGate | None,
        config: AgentConfig,
        call: ToolCall,
        record: ToolCallRecord,
        test_id: str,
        *,
        shared_content: bool = False,
    ) -> str | None:
        """The per-call rules for one call of a test: ``None`` runs it, a string denies it (never waits).

        Like a run, the agent's own memory tool is not asked about: in a test it writes only to an ephemeral copy
        (:meth:`_memory`). Whether a call really reaches it is decided by the agent's configuration, not by the name
        (:func:`~nodestro_runtime.agent_builder.run_own_tool`, re-review R-2). The test passes no other run-own tools
        (``extra_tools``). ``shared_content``: the test holds content of a granted source (N-08)."""
        bind_call(None)
        if gate is None or run_own_tool(config, call.name):
            return None
        capability, version, kind, definition = tool_facts(self.builder.tools, call.name)
        situation = gate.call_context(
            run_id=test_id, node_id="agent-test", trigger="manual", trigger_ref=None, shared_content=shared_content
        )
        verdict = tool_policy.decide(
            tool=call.name,
            kind=kind,
            capability=capability,
            binding=config.tool_policies.get(call.name),
            context=situation,
            at=now(),
            version=version,
            definition_hash=definition,
            related=gate.related(self.builder.tools, call.name, config.tool_settings),
        )
        shown = capability.model_dump()
        if verdict.action == "allow" and verdict.access_source is not None:
            # N-06: a stored grant stands in for a person in a run, not in a test: a test agent has no identity of
            # its own (a grant for all agents would match it) and its calls must not have effects nobody approved.
            verdict = tool_policy.ASK
        if verdict.action == "allow":
            record.approval = ToolCallApproval(status="allowed", pre_grant_id=verdict.pre_grant_id, capability=shown)
            bind_call(CallBinding(tool=call.name, capability=capability, definition_hash=definition))
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

    def _leaves_computer(self, provider: str, connection: Connection | None = None) -> bool:
        """N-08: a cloud provider, or a local model server whose address is not on this computer (only while the
        provenance switch is on; see :meth:`RunManager.leaves_computer`)."""
        if provider != "ollama":
            return True
        gate = self.tool_gate()
        if gate is None or not gate.provenance:
            return False
        found = connection if connection is not None else self.connect(provider)
        return offline.remote_model_server(found, docker=self.builder.mode == "local-container")

    def _check_egress(self, gate: ToolGate, model: str, server_tools: Sequence[str], actor: str) -> None:
        """N-08: before a request of a test that holds content of a granted source, a model outside this computer
        (``model``: the agent's or its judge's) needs a grant (audited like a run's refusal), and a server tool that
        reaches outside is refused (a test never acts on a grant for a tool)."""
        provider = provider_of(model)
        if self._leaves_computer(provider):
            resource = tool_policy.AccessResource(type="cloud_provider", id=provider)
            found = gate.site_refusal(resource, attended=True, agent=None, shared_content=True)
            if found is not None:
                record_refusal(gate.approvals.store, found, resource, actor=actor)
                raise access_error(found, resource, show_source=is_person(actor))
        for key in dict.fromkeys(server_tools):
            capability = tool_policy.server_tool_capability(key)
            if capability.reach != "external":
                continue
            resource = tool_policy.AccessResource(type="tool", id=key)
            found_tool = tool_policy.AccessDecision(allowed=False, kind="missing_grant")
            explained = tool_policy.refusal_of(resource, found_tool, effect=tool_policy.effect_of(capability))
            record_refusal(gate.approvals.store, explained, resource, actor=actor)
            raise access_error(explained, resource, show_source=is_person(actor))

    def run(self, request: AgentTestIn, *, actor: str = "system") -> tuple[AgentTestResult, BudgetTracker]:
        """Run one test. ``actor`` is who asked (N-06: the audit of a refused cloud model, and only a person sees its
        source in the result)."""
        config = request.agent
        prompt = task_prompt(config, request.input, raw=request.raw)
        budget = self.default_budget()
        tracker = PricedTracker(
            Decimal(str(budget)) if budget is not None else None, pricing=self.pricing, mode=self.price_mode()
        )
        result = AgentTestResult(agent_name=config.name, model=config.model, prompt=prompt)
        calls: dict[str, ToolCallRecord] = {}
        lock = threading.Lock()
        gate = self.tool_gate()
        #: N-08: content of a granted source entered the test (only while the provenance switch is on).
        granted = gate is not None and gate.provenance and bool(start_sources(config, []))

        def on_text(delta: str) -> None:
            result.text += delta

        def on_request(_args: dict[str, Any]) -> None:
            tracker.check()
            if granted and gate is not None:
                # N-08: the request would carry granted content: a cloud model needs a grant, as in a run.
                self._check_egress(gate, config.model, config.server_tools, actor)

        def on_judge_request(_args: dict[str, Any]) -> None:
            tracker.check()
            if granted and gate is not None:
                # N-08: the judge connected at the start and may use another provider: its own model is checked.
                self._check_egress(gate, check_model(config.quality.judge_model), (), actor)

        def before_tool(call: ToolCall) -> Denial | None:
            record = ToolCallRecord(id=call.id, name=call.name, input=call.input)
            with lock:
                calls[call.id] = record
                result.tool_calls.append(record)
            if config.offline and call.name in config.tools:
                # N-09: an offline agent never calls a tool that is not available offline, as in a run.
                sandbox = self.tool_sandbox(request.workflow_id) if request.workflow_id else None
                state = offline.tool_state(self.builder.tools, call.name, sandbox=sandbox, at_call=True)
                if not state.available:
                    record.approval = ToolCallApproval(status="denied", code="offline.tool_unavailable")
                    return Denial(model_text=f"Tool call denied: {state.text}", code="offline.tool_unavailable")
            denial = self._gate(gate, config, call, record, result.id, shared_content=granted)
            return None if denial is None else Denial(model_text=f"Tool call denied: {denial}")

        def after_tool(call: ToolCall, content: str, is_error: bool) -> None:
            nonlocal granted
            record = calls.get(call.id)
            refused = record is not None and record.approval is not None and record.approval.status == "denied"
            if gate is not None and gate.provenance and granted_source(call.content_source) and not refused:
                granted = True  # N-08: a folder, knowledge, memory or file tool returned its content
            if record is not None:
                record.output = content[:_MAX_TOOL_OUTPUT]
                record.is_error = is_error
                record.finished_at = now()

        hooks: dict[str, Any] = {
            "on_text": on_text,
            "on_request": on_request,
            "before_tool": before_tool,
            "after_tool": after_tool,
            "on_event": trace_hook(result.reasoning),
        }
        connect = self.connect
        if gate is not None and gate.access is not None:
            # N-06: a cloud model the access model v2 blocks never gets the test's prompt (no agent: a test agent has
            # no identity, so a deny for some agents holds here too). Audited like a run's refusal; only a person sees
            # the source in the result.
            def refuse(provider: str) -> None:
                resource = tool_policy.AccessResource(type="cloud_provider", id=provider)
                found = gate.site_refusal(resource, attended=True, agent=None)
                if found is not None:
                    record_refusal(gate.approvals.store, found, resource, actor=actor)
                    raise access_error(found, resource, show_source=is_person(actor))

            def connect(provider: str) -> Connection:
                if provider != "ollama":
                    refuse(provider)
                    return self.connect(provider)
                connection = self.connect(provider)  # the local model server: its address decides (N-08)
                if self._leaves_computer(provider, connection):
                    refuse(provider)
                return connection

        if config.offline:
            # N-09: an offline agent gets local connections only, also in its test.
            access_connect = connect

            def connect(provider: str) -> Connection:
                offline.check_provider(provider, "agent")
                return offline.check_connection(
                    access_connect(provider), "agent", docker=self.builder.mode == "local-container"
                )

        try:
            if config.offline and config.server_tools:
                raise offline.server_tools_error(list(dict.fromkeys(config.server_tools)), "agent")
            if config.offline and offline.shared_memory(config):
                raise offline.shared_memory_error(offline.shared_memory(config), "agent")
            built_config, run_memory, isolated = self._memory(config, actor)
            result.memory_isolated = isolated
            outcome = execute_agent(
                self.builder,
                built_config,
                prompt,
                connect=connect,
                tracker=tracker,
                name="agent-test",
                hooks=hooks,
                run_memory=run_memory,
                sandbox=self.tool_sandbox(request.workflow_id) if request.workflow_id else None,
                # before_tool applies the per-call rules to every call while the gate exists.
                gated=gate is not None,
                on_built=lambda built: setattr(result, "reasoning_reduced", built.reasoning_reduced),
                judge_hooks={"on_request": on_judge_request},
            )
            result.output = outcome.output
            result.evaluation = outcome.evaluation
            result.memory = outcome.memory
            if checked_fields(config.output):
                with lock:
                    computed = [call for call in result.tool_calls if computing_call(call, config.tools)]
                result.value_warnings = [
                    ValueWarning(
                        field=item.field,
                        value=item.value,
                        reason=item.reason,
                        rows_passed=item.rows_passed,
                        rows_in_input=item.rows_in_input,
                    )
                    for item in unverified_fields(config.output, outcome.output, computed, prompt)
                ]
        except Exception as exc:  # API errors, budget, invalid configuration: shown in the inspector
            found: Problem = node_problem(exc)
            if isinstance(exc, MaxToolRoundsError) and not isinstance(exc, MaxToolCallsError):
                with lock:
                    found = tool_rounds_problem(found, list(result.tool_calls), config)
            result.status = "failed"
            result.error, result.error_code, result.error_params = found.message, found.code, found.params
        finally:
            result.reasoning.active = False
            result.usage = usage_of(
                tracker.usage,
                tracker.records,
                stale_requests=tracker.stale_requests(),
                regions=request_regions(tracker, tracker.records),
            )
            result.finished_at = now()
        return result, tracker
