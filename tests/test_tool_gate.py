"""``tool_policy.decide``: when a call is allowed, denied or asked about (S-2a, decision D10). No I/O."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nodestro_runtime.tool_policy import (
    UNKNOWN,
    CallContext,
    ToolBinding,
    ToolCapability,
    ToolPreGrant,
    decide,
    wants_question,
)

AT = datetime(2026, 10, 31, 20, 0, tzinfo=timezone.utc)
READ = ToolCapability(access="read", reach="external", source="author")
WRITE = ToolCapability(access="write", reach="internal", source="author")
DESTRUCTIVE = ToolCapability(access="destructive", reach="external", source="author")
REASON = "Writes only into the team's scratch table"


def attended(**extra: object) -> CallContext:
    return CallContext(run_id="run_1", node_id="a", trigger="manual", attended=True, **extra)  # type: ignore[arg-type]


def unattended(**extra: object) -> CallContext:
    return CallContext(run_id="run_1", node_id="a", trigger="schedule", attended=False, **extra)  # type: ignore[arg-type]


def grant(**extra: object) -> ToolPreGrant:
    values: dict[str, object] = {
        "id": "pg_1",
        "tool": "mailer",
        "version": 3,
        "scope": "schedule",
        "scope_id": "sch_1",
        "granted_by": "usr_1",
        "granted_at": AT - timedelta(days=1),
    }
    values.update(extra)
    return ToolPreGrant.model_validate(values)


def gate(capability: ToolCapability, binding: ToolBinding | None, context: CallContext, **extra: object) -> str:
    values: dict[str, object] = {"tool": "mailer", "kind": "custom", "version": 3}
    values.update(extra)
    found = decide(capability=capability, binding=binding, context=context, at=AT, **values)  # type: ignore[arg-type]
    return found.action if found.code is None else f"{found.action}:{found.code}"


@pytest.mark.parametrize(
    ("capability", "binding", "asks"),
    [
        (READ, None, False),
        (WRITE, None, True),
        (UNKNOWN, None, True),
        (DESTRUCTIVE, None, True),
        (READ, ToolBinding(ask="always"), True),
        (READ, ToolBinding(ask="never"), False),
        # never without a reason is not an exemption for a writing tool
        (WRITE, ToolBinding(ask="never"), True),
        (WRITE, ToolBinding(ask="never", reason=REASON), False),
        # never is never allowed for destructive tools, not even with a reason or as a legacy exemption
        (DESTRUCTIVE, ToolBinding(ask="never", reason=REASON), True),
        (DESTRUCTIVE, ToolBinding(ask="never", legacy=True, bound_version=3), True),
        # a legacy exemption holds for the bound version only
        (UNKNOWN, ToolBinding(ask="never", legacy=True, bound_version=3), False),
        (UNKNOWN, ToolBinding(ask="never", legacy=True, bound_version=2), True),
        (UNKNOWN, ToolBinding(ask="never", legacy=True), True),
    ],
)
def test_when_a_binding_asks(capability: ToolCapability, binding: ToolBinding | None, asks: bool) -> None:
    assert wants_question(capability, binding, version=3) is asks
    assert gate(capability, binding, attended()) == ("ask" if asks else "allow")


def test_a_reason_must_say_something() -> None:
    with pytest.raises(ValueError):
        ToolBinding(ask="never", reason="ok")


def test_grants_only_narrow_and_name_who_refused() -> None:
    context = attended(grants=frozenset({"calculator"}), grant_scope="planner")
    found = decide(tool="mailer", kind="custom", capability=READ, binding=None, context=context, at=AT)
    assert found.code == "tool.not_granted" and found.reason is not None and "planner" in found.reason
    # a grant never turns ask into allow
    granted = attended(grants=frozenset({"mailer"}))
    assert gate(WRITE, None, granted) == "ask"


def test_unattended_runs_deny_what_would_ask() -> None:
    assert gate(WRITE, None, unattended()) == "deny:tool.approval_unattended"
    assert gate(READ, None, unattended()) == "allow"
    assert gate(WRITE, None, unattended(scope=("schedule", "sch_1"))) == "deny:tool.approval_unattended"


def test_a_pre_grant_allows_only_its_tool_version_and_scope() -> None:
    scope = ("schedule", "sch_1")
    found = decide(
        tool="mailer",
        kind="custom",
        version=3,
        capability=WRITE,
        binding=None,
        context=unattended(scope=scope, pre_grants=(grant(),)),
        at=AT,
    )
    assert found.action == "allow" and found.pre_grant_id == "pg_1"
    denied = "deny:tool.approval_unattended"
    assert gate(WRITE, None, unattended(scope=scope, pre_grants=(grant(version=2),))) == denied
    assert gate(WRITE, None, unattended(scope=scope, pre_grants=(grant(version=None),))) == denied
    assert gate(WRITE, None, unattended(scope=("schedule", "sch_2"), pre_grants=(grant(),))) == denied
    assert gate(WRITE, None, unattended(scope=("batch", "sch_1"), pre_grants=(grant(),))) == denied
    assert gate(WRITE, None, unattended(scope=scope, pre_grants=(grant(tool="other"),))) == denied
    assert gate(WRITE, None, unattended(scope=scope, pre_grants=(grant(revoked_at=AT),))) == denied
    assert gate(WRITE, None, unattended(scope=scope, pre_grants=(grant(expires_at=AT),))) == denied
    assert gate(WRITE, None, unattended(pre_grants=(grant(),))) == denied
    # never for destructive tools
    assert gate(DESTRUCTIVE, None, unattended(scope=scope, pre_grants=(grant(),))) == denied
    # a later revocation does not matter yet; an expiry in the future neither
    later = AT + timedelta(hours=1)
    assert gate(WRITE, None, unattended(scope=scope, pre_grants=(grant(revoked_at=later, expires_at=later),))) == (
        "allow"
    )


def test_mcp_tools_are_strict_without_a_person() -> None:
    read = ToolCapability(access="read", reach="external", source="admin")
    assert gate(read, None, attended(), kind="mcp") == "allow"
    assert gate(read, None, unattended(), kind="mcp") == "deny:tool.approval_unattended"
    # ADR-0007 D-7 after the security acceptance: read-only MCP tools unattended
    assert gate(read, None, unattended(mcp_unattended=True), kind="mcp") == "allow"
    write = ToolCapability(access="write", reach="external", source="admin")
    assert gate(write, None, unattended(mcp_unattended=True), kind="mcp") == "deny:tool.approval_unattended"
    # pre-grants never cover MCP tools
    context = unattended(mcp_unattended=True, scope=("schedule", "sch_1"), pre_grants=(grant(tool="mcp__x__y"),))
    assert gate(write, None, context, kind="mcp", tool="mcp__x__y") == "deny:tool.approval_unattended"


def test_a_changing_mcp_call_is_asked_whatever_the_binding_says() -> None:
    write = ToolCapability(access="write", reach="external", source="admin")
    legacy = ToolBinding(ask="never", legacy=True, bound_version=1)
    reasoned = ToolBinding(ask="never", reason=REASON)
    for binding in (None, legacy, reasoned, ToolBinding(ask="auto")):
        assert gate(write, binding, attended(), kind="mcp", version=1) == "ask"
    assert gate(DESTRUCTIVE, reasoned, attended(), kind="mcp", version=1) == "ask"
    # the same bindings still apply to non-MCP tools
    assert gate(write, reasoned, attended(), version=1) == "allow"
