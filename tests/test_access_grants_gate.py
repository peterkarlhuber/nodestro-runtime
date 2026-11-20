"""The access model v2 (N-06) in the tool gate: behind its switch, off by default, unchanged S-2 rules when off."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
import itertools
import json
from pathlib import Path
import re
from typing import Any

import pytest

import nodestro_runtime
from nodestro_runtime import tool_policy
from nodestro_runtime.schemas import RunIn, WorkflowIn, now
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import (
    ACCESS_ENV,
    CHECKPOINTS_ENV,
    ENABLE_ENV,
    AccessRules,
    Person,
    ToolApprovals,
    ToolGate,
    ToolPreGrants,
    access_grants_enabled,
)
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.tool_policy import (
    AccessContext,
    AccessResource,
    CallContext,
    Deny,
    Grant,
    ToolBinding,
    ToolCapability,
    ToolPreGrant,
    decide,
)
from nodestro_runtime.workspace import Workspace

AT = now()
ALICE = Person("usr_alice")
TOOL = "celsius_to_fahrenheit"


def cap(access: Any, reach: Any = "internal") -> ToolCapability:
    return ToolCapability(access=access, reach=reach, source="author")


def context(*, attended: bool = True, access: AccessContext | None = None, **extra: Any) -> CallContext:
    return CallContext(
        run_id="run_1",
        node_id="a",
        trigger="manual" if attended else "schedule",
        attended=attended,
        access=access,
        **extra,
    )


def tool_grant(name: str = "tool_x", **extra: Any) -> Grant:
    values: dict[str, Any] = {
        "id": "agr_1",
        "resource": AccessResource(type="tool", id=name),
        "actions": ("execute",),
        "duration": "until_revoked",
        "created_at": AT - timedelta(days=1),
        "granted_by": "usr_alice",
    }
    return Grant(**{**values, **extra})


def tool_deny(name: str = "tool_x", **extra: Any) -> Deny:
    values: dict[str, Any] = {
        "id": "adn_1",
        "resource": AccessResource(type="tool", id=name),
        "actions": ("execute",),
        "created_at": AT - timedelta(days=1),
        "denied_by": "usr_alice",
    }
    return Deny(**{**values, **extra})


# ---- off: the S-2 rules exactly as before ---------------------------------------------------------------------------


def test_without_the_access_model_decide_is_the_s2_rule_for_every_combination() -> None:
    """Regression: ``ToolBinding`` and ``ToolPreGrant`` behave unchanged while the switch is off (``access=None``)."""
    pre = ToolPreGrant(
        id="tpg_1", tool="tool_x", version=1, scope="schedule", scope_id="sch_1", granted_by="usr_1", granted_at=AT
    )
    bindings = [
        None,
        ToolBinding(ask="always"),
        ToolBinding(ask="never"),
        ToolBinding(ask="never", reason="Writes only into the run's own notes"),
        ToolBinding(ask="never", legacy=True, bound_version=1),
    ]
    capabilities = [cap(a, r) for a, r in itertools.product(["read", "write", "destructive"], ["internal", "external"])]
    seen = set()
    for capability, binding, kind, attended, grants in itertools.product(
        capabilities, bindings, ["custom", "mcp", "builtin"], [True, False], [None, frozenset({"other"})]
    ):
        situation = context(attended=attended, grants=grants, scope=("schedule", "sch_1"), pre_grants=(pre,))
        arguments = {"tool": "tool_x", "kind": kind, "capability": capability, "binding": binding, "at": AT}
        verdict = decide(context=situation, version=1, **arguments)
        assert verdict == tool_policy._decide_binding(context=situation, version=1, **arguments)
        assert verdict.access_source is None
        seen.add((verdict.action, verdict.code, verdict.pre_grant_id))
    # The matrix reaches every S-2 outcome, including the pre-grant.
    assert {action for action, _, _ in seen} == {"allow", "ask", "deny"}
    assert ("allow", None, "tpg_1") in seen and ("deny", "tool.not_granted", None) in seen


def test_the_switch_is_off_by_default_and_needs_the_approvals(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (ENABLE_ENV, ACCESS_ENV, CHECKPOINTS_ENV):
        monkeypatch.delenv(name, raising=False)
    assert not access_grants_enabled()
    monkeypatch.setenv(ACCESS_ENV, "1")
    assert not access_grants_enabled()  # only together with the per-call approvals
    monkeypatch.setenv(ENABLE_ENV, "1")
    assert access_grants_enabled()
    gate = ToolGate(ToolApprovals.__new__(ToolApprovals))
    assert gate.access is None
    assert gate.call_context(run_id="r", node_id="n", trigger="manual", trigger_ref=None, agent="wf/n").access is None


def test_a_workspace_wires_the_access_model_only_when_switched_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.delenv(ACCESS_ENV, raising=False)
    off = Workspace(tmp_path / "off", integrations=[])
    try:
        assert off.runs.tool_gate is not None and off.runs.tool_gate.access is None
    finally:
        off.close()
    monkeypatch.setenv(ACCESS_ENV, "1")
    on = Workspace(tmp_path / "on", integrations=[])
    try:
        assert on.runs.tool_gate is not None and on.runs.tool_gate.access is not None
        stored = AccessRules(on.store).deny(resource=AccessResource(type="tool", id="x"), actions=["execute"], by=ALICE)
        found = on.runs.tool_gate.call_context(run_id="r", node_id="n", trigger="manual", trigger_ref=None)
        assert found.access is not None and found.access.denies == (stored,)
    finally:
        on.close()


# ---- on: deny and safety limit first, a grant instead of a question -------------------------------------------------


def test_a_deny_refuses_before_anything_else_and_tells_the_agent_no_source() -> None:
    access = AccessContext(denies=(tool_deny(),), grants=(tool_grant(),), agent="wf_1/a")
    verdict = decide(
        tool="tool_x", kind="builtin", capability=cap("read"), binding=None, context=context(access=access), at=AT
    )
    assert (verdict.action, verdict.code, verdict.access_source) == ("deny", "access.denied", "deny:adn_1")
    assert "adn_1" not in (verdict.reason or "") and "deny:" not in (verdict.reason or "")


def test_a_safety_limit_refuses_whatever_is_granted() -> None:
    access = AccessContext(grants=(tool_grant(mode="unattended", duration="until", expires_at=AT + timedelta(days=1)),))
    verdict = decide(
        tool="tool_x",
        kind="custom",
        capability=cap("destructive"),
        binding=None,
        context=context(attended=False, access=access),
        at=AT,
    )
    assert (verdict.action, verdict.code, verdict.access_source) == (
        "deny",
        "access.safety_limit",
        "limit:destructive_unattended",
    )
    offline = AccessContext(grants=(tool_grant(),), offline=True)
    verdict = decide(
        tool="tool_x",
        kind="custom",
        capability=cap("read", "external"),
        binding=None,
        context=context(access=offline),
        at=AT,
    )
    assert (verdict.code, verdict.access_source) == ("access.safety_limit", "limit:offline")


def test_a_grant_replaces_the_question_but_never_the_narrower_rules() -> None:
    write = cap("write", "external")
    granted = AccessContext(grants=(tool_grant(),))

    def verdict(**extra: Any) -> tool_policy.GateDecision:
        values: dict[str, Any] = {"tool": "tool_x", "kind": "custom", "capability": write, "binding": None, "at": AT}
        values.update(extra)
        values.setdefault("context", context(access=granted))
        return decide(**values)

    assert verdict(context=context(access=AccessContext())).action == "ask"  # missing grant: asked as before
    assert verdict() == tool_policy.GateDecision(action="allow", access_source="grant:agr_1")
    assert verdict(binding=ToolBinding(ask="always")).action == "ask"
    assert verdict(kind="mcp").action == "ask"
    assert verdict(capability=cap("destructive")).action == "ask"
    narrowed = context(access=granted, grants=frozenset({"other"}))
    assert verdict(context=narrowed).code == "tool.not_granted"
    once = AccessContext(grants=(tool_grant(duration="once"),))
    assert verdict(context=context(access=once)).action == "ask"  # one confirmed call is the S-2 approval itself


def test_an_unattended_call_needs_an_unattended_dated_grant() -> None:
    write = cap("write")
    standing = AccessContext(grants=(tool_grant(mode="unattended"),))
    dated = AccessContext(grants=(tool_grant(mode="unattended", duration="until", expires_at=AT + timedelta(hours=1)),))
    expired = AccessContext(
        grants=(tool_grant(mode="unattended", duration="until", expires_at=AT - timedelta(hours=1)),)
    )
    supervised = AccessContext(grants=(tool_grant(),))
    for access, action in ((standing, "deny"), (dated, "allow"), (expired, "deny"), (supervised, "deny")):
        found = decide(
            tool="tool_x",
            kind="custom",
            capability=write,
            binding=None,
            context=context(attended=False, access=access),
            at=AT,
        )
        assert found.action == action, access
        assert found.code in (None, "tool.approval_unattended")


# ---- a grant is never written by a model ---------------------------------------------------------------------------


def test_no_tool_and_no_model_path_can_write_grants() -> None:
    """Only the store class writes grant and deny records, and only the workspace (for the gate, reading) and the
    store module know it: no tool, MCP, orchestrator or engine module can reach the writer."""
    package = Path(nodestro_runtime.__file__).resolve().parent
    writers = re.compile(r"\bAccessRules\b|\baccess_grants\b|\baccess_denies\b|\bGRANTS\b|\bDENIES\b")
    found = sorted(
        str(path.relative_to(package)) for path in package.rglob("*.py") if writers.search(path.read_text("utf-8"))
    )
    assert found == ["tool_approvals.py", "workspace.py"]
    workspace_source = (package / "workspace.py").read_text("utf-8")
    assert re.findall(r"AccessRules\([^)]*\)\.(\w+)", workspace_source) == ["current"]  # reads, never writes


# ---- in a run --------------------------------------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    fake = ScriptedClient({"AGENT": [tool_use(TOOL, {"celsius": 100}), text("done")] * 6})
    opened = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
    result, _ = opened.tools.save_custom(
        CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, TOOL, trust=opened.tools.editor_trust(TOOL, CUSTOM_TOOL_TEMPLATE)
    )
    assert result.ok
    rules = AccessRules(opened.store)
    opened.runs.tool_gate = ToolGate(
        ToolApprovals(opened.store), pre_grants=ToolPreGrants(opened.store).of_scope, access=rules.current
    )
    opened.fake = fake  # type: ignore[attr-defined]
    yield opened
    opened.close()


async def test_grants_and_denies_apply_to_a_running_schedule_at_the_next_call(workspace: Workspace) -> None:
    from nodestro_runtime.launcher import launch

    flow = WorkflowIn.model_validate(
        {
            "name": "Scheduled",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "a", "type": "agent", "agent": {"system_prompt": "AGENT", "tools": [TOOL]}},
            ],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )
    saved = workspace.save_workflow(flow)
    rules = AccessRules(workspace.store)
    resource = AccessResource(type="tool", id=TOOL)

    async def approval() -> Any:
        started = await launch(workspace, saved, RunIn(input="x"), trigger="schedule", trigger_ref="sch_1")
        done = await workspace.wait(started.id, timeout=20)
        return done.nodes["a"].tool_calls[0].approval

    missing = await approval()
    assert (missing.status, missing.code) == ("denied", "tool.approval_unattended")
    granted = rules.grant(
        resource=resource,
        actions=["execute"],
        duration="until",
        expires_at=now() + timedelta(hours=1),
        mode="unattended",
        scope="agent",
        agents=[f"{saved.id}/a"],
        by=ALICE,
    )
    allowed = await approval()
    assert allowed.status == "allowed" and allowed.pre_grant_id is None
    blocked = rules.deny(resource=resource, actions=["execute"], by=ALICE)
    denied = await approval()
    assert (denied.status, denied.code) == ("denied", "access.denied")
    rules.revoke_deny(blocked.id, by=ALICE)
    assert (await approval()).status == "allowed"
    rules.revoke_grant(granted.id, by=ALICE)
    revoked = await approval()
    assert (revoked.status, revoked.code) == ("denied", "tool.approval_unattended")
    sent = json.dumps(workspace.fake.messages.calls, default=str)  # type: ignore[attr-defined]
    assert blocked.id not in sent and granted.id not in sent and "deny:" not in sent
