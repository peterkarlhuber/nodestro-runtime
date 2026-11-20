"""The access model v2 (N-06): grants, denies, global safety limits and their evaluation (spec 7.1-7.3, 7.5, 7.6)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest

from nodestro_runtime import activity
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import now
from nodestro_runtime.storage import SQLiteStore
from nodestro_runtime.tool_approvals import (
    AUDIT,
    AccessRules,
    CallBinding,
    Person,
    ToolApprovalEvent,
    bind_call,
)
from nodestro_runtime.tool_policy import (
    SAFETY_LIMITS,
    AccessDecision,
    AccessRequest,
    AccessResource,
    Deny,
    EffectClass,
    Grant,
    ToolCapability,
    ToolFacts,
    effect_class,
    evaluate,
    folder_parts,
)

AT = datetime(2026, 11, 20, 12, 0, tzinfo=now().tzinfo)
ALICE = Person("usr_alice")
AGENT = "wf_1/writer"
DATA = "/data/nodestro"
#: The search tool as it is (a built-in tool: no version).
SEARCH = ToolFacts(access="read", reach="external")
OTHER = "wf_1/reviewer"


def folder(path: str) -> AccessResource:
    return AccessResource(type="folder", id=path)


def tool(name: str) -> AccessResource:
    return AccessResource(type="tool", id=name)


def grant(resource: AccessResource, *actions: Any, **extra: Any) -> Grant:
    values: dict[str, Any] = {
        "id": extra.pop("id", "agr_1"),
        "resource": resource,
        "actions": actions,
        "duration": "until_revoked",
        "created_at": AT - timedelta(days=1),
        "granted_by": ALICE.id,
        "tool": SEARCH if resource.type == "tool" else None,
    }
    return Grant(**{**values, **extra})


def deny(resource: AccessResource, *actions: Any, **extra: Any) -> Deny:
    values: dict[str, Any] = {
        "id": extra.pop("id", "adn_1"),
        "resource": resource,
        "actions": actions,
        "created_at": AT - timedelta(days=1),
        "denied_by": ALICE.id,
    }
    return Deny(**{**values, **extra})


def check(
    resource: AccessResource,
    action: Any,
    effect: EffectClass,
    *,
    grants: Sequence[Grant] = (),
    denies: Sequence[Deny] = (),
    at: datetime = AT,
    **extra: Any,
) -> AccessDecision:
    if resource.type == "folder":
        extra.setdefault("protected_folders", (DATA,))
    if resource.type == "tool":
        extra.setdefault("tool", SEARCH)
    request = AccessRequest(resource=resource, action=action, effect=effect, **{"agent": AGENT, **extra})
    return evaluate(request, grants=grants, denies=denies, at=at)


def read(path: str, **extra: Any) -> AccessDecision:
    return check(folder(path), "read", "read_internal", **extra)


# ---- effect classes: combinations of the two axes, no second schema -----------------------------------------------


def test_the_five_effect_classes_are_the_two_axes() -> None:
    assert effect_class("read", "internal") == "read_internal"
    assert effect_class("write", "internal") == "write_internal"
    assert effect_class("read", "external") == "read_external"
    assert effect_class("write", "external") == "write_external"
    assert effect_class("destructive", "internal") == effect_class("destructive", "external") == "destructive"
    assert set(ToolCapability.model_fields) == {"access", "reach", "source"}  # no new axis on the capability


# ---- spec 7.2, phase A rows: the default and the relaxation ----------------------------------------------------------


def test_reading_a_folder_needs_a_grant_and_can_be_restricted() -> None:
    """7.2 "read in granted folders": allowed for bound agents (default); restricting it is possible."""
    docs = grant(folder("/home/alice/docs"), "read", scope="agents", agents=(AGENT,))
    assert read("/home/alice/docs/a.txt", grants=[docs]) == AccessDecision(allowed=True, source="grant:agr_1")
    assert read("/home/alice/docs/a.txt", grants=[docs], agent=OTHER).kind == "missing_grant"  # not bound
    assert read("/home/alice/other", grants=[docs]).kind == "missing_grant"  # not granted
    restricted = deny(folder("/home/alice/docs/private"), "read")
    found = read("/home/alice/docs/private/x", grants=[docs], denies=[restricted])
    assert (found.allowed, found.kind, found.source) == (False, "denied", "deny:adn_1")
    assert read("/home/alice/docs/public/x", grants=[docs], denies=[restricted]).allowed


def test_reading_externally_needs_a_grant_only_with_shared_content() -> None:
    """7.2 "read external": allowed without shared content in the conversation, otherwise asked; grant per service."""
    search = tool("search_web")
    plain = check(search, "execute", "read_external")
    assert plain.allowed and plain.source == "default:read_external_without_shared_content"
    shared = check(search, "execute", "read_external", shared_content=True)
    assert (shared.allowed, shared.kind) == (False, "missing_grant")
    service = grant(search, "execute")
    assert check(search, "execute", "read_external", shared_content=True, grants=[service]).source == "grant:agr_1"
    other_service = grant(tool("fetch_web_page"), "execute")
    assert check(search, "execute", "read_external", shared_content=True, grants=[other_service]).kind == (
        "missing_grant"
    )


def test_a_cloud_model_with_shared_content_is_confirmed_per_conversation_or_granted_per_provider() -> None:
    """7.2 "cloud model with shared content": confirmed per conversation; grant per provider and agent."""
    provider = AccessResource(type="cloud_provider", id="anthropic")

    def submit(**extra: Any) -> AccessDecision:
        return check(provider, "submit", "write_external", **extra)

    assert submit().source == "default:cloud_without_shared_content"
    assert submit(shared_content=True).kind == "missing_grant"
    this_chat = grant(provider, "submit", duration="conversation", conversation_id="run_1")
    assert submit(shared_content=True, conversation_id="run_1", grants=[this_chat]).allowed
    assert submit(shared_content=True, conversation_id="run_2", grants=[this_chat]).kind == "missing_grant"
    standing = grant(provider, "submit", scope="agent", agents=(AGENT,))
    assert submit(shared_content=True, grants=[standing]).allowed
    assert submit(shared_content=True, grants=[standing], agent=OTHER).kind == "missing_grant"
    other_provider = grant(AccessResource(type="cloud_provider", id="openai"), "submit")
    assert submit(shared_content=True, grants=[other_provider]).kind == "missing_grant"


# ---- spec 7.3 / 7.5: inheritance, denies, safety limits ---------------------------------------------------------


def test_a_subfolder_grant_works_without_a_parent_grant() -> None:
    sub = grant(folder("/home/alice/docs/projects"), "read")
    assert read("/home/alice/docs/projects/2026/plan.md", grants=[sub]).allowed
    assert read("/home/alice/docs", grants=[sub]).kind == "missing_grant"
    assert read("/home/alice/docs/other", grants=[sub]).kind == "missing_grant"


def test_a_deny_on_the_parent_blocks_a_subfolder_grant() -> None:
    sub = grant(folder("/home/alice/docs/projects"), "read")
    parent = deny(folder("/home/alice/docs"), "read")
    found = read("/home/alice/docs/projects/plan.md", grants=[sub], denies=[parent])
    assert (found.allowed, found.kind, found.source) == (False, "denied", "deny:adn_1")


def test_missing_and_denied_are_reported_differently() -> None:
    missing = read("/home/alice/docs")
    denied = read("/home/alice/docs", denies=[deny(folder("/home/alice"), "read")])
    assert (missing.kind, missing.code, missing.source) == ("missing_grant", "access.missing_grant", "none")
    assert (denied.kind, denied.code, denied.source) == ("denied", "access.denied", "deny:adn_1")
    limited = read("/etc/passwd")
    assert (limited.kind, limited.code, limited.source) == (
        "safety_limit",
        "access.safety_limit",
        "limit:protected_folder",
    )


@pytest.mark.parametrize(
    ("request_values", "limit"),
    [
        ({"resource": folder("/etc"), "action": "read", "effect": "read_internal"}, "protected_folder"),
        ({"resource": folder("/usr/local/bin"), "action": "write", "effect": "write_internal"}, "protected_folder"),
        (
            {"resource": folder("C:\\Windows\\System32"), "action": "read", "effect": "read_internal"},
            "protected_folder",
        ),
        ({"resource": folder("c:/windows/system32"), "action": "read", "effect": "read_internal"}, "protected_folder"),
        (
            {
                "resource": folder("/data/nodestro/db"),
                "action": "read",
                "effect": "read_internal",
                "protected_folders": ("/data/nodestro",),
            },
            "protected_folder",
        ),
        ({"resource": folder("docs/relative"), "action": "read", "effect": "read_internal"}, "unresolvable_path"),
        (
            {
                "resource": folder("/home/alice/old"),
                "action": "delete",
                "effect": "write_internal",
                "mode": "unattended",
            },
            "destructive_unattended",
        ),
        ({"resource": tool("search_web"), "action": "execute", "effect": "read_external", "offline": True}, "offline"),
        (
            {
                "resource": AccessResource(type="cloud_provider", id="anthropic"),
                "action": "submit",
                "effect": "write_external",
                "offline": True,
            },
            "offline",
        ),
    ],
)
def test_safety_limits_are_not_lifted_by_any_grant(request_values: dict[str, Any], limit: str) -> None:
    resource: AccessResource = request_values["resource"]
    everything = [
        grant(
            resource,
            request_values["action"],
            id="agr_exact",
            mode="unattended",
            duration="until",
            expires_at=AT + timedelta(days=1),
        ),
        grant(
            folder("/"),
            "read",
            "write",
            "move",
            "trash",
            id="agr_root",
            mode="unattended",
            duration="until",
            expires_at=AT + timedelta(days=1),
        ),
        grant(folder("C:\\"), "read", id="agr_drive"),
    ]
    found = check(grants=everything, **request_values)
    assert (found.allowed, found.kind, found.source) == (False, "safety_limit", f"limit:{limit}")
    assert limit in SAFETY_LIMITS


def test_deleting_for_good_is_never_granted_and_never_unattended() -> None:
    """7.5: deleting for good is possible neither by a standing grant nor unattended."""
    old = folder("/home/alice/old")
    standing = Grant(
        id="agr_x", resource=old, actions=("delete",), duration="until_revoked", created_at=AT, granted_by="usr_alice"
    )
    attended = check(old, "delete", "destructive", grants=[standing])
    assert (attended.allowed, attended.kind, attended.source) == (False, "missing_grant", "limit:destructive_per_call")
    unattended = check(old, "delete", "destructive", grants=[standing], mode="unattended")
    assert (unattended.kind, unattended.source) == ("safety_limit", "limit:destructive_unattended")
    rules = AccessRules(SQLiteStore(":memory:"))
    with pytest.raises(NodestroError) as error:
        rules.grant(resource=old, actions=["delete"], duration="once", by=ALICE)
    assert error.value.code == "access.grant_destructive"


def test_unattended_access_needs_an_unattended_grant_and_a_date_for_more_than_reading() -> None:
    """7.2 / 7.5: a background task reads within its grant without asking and reads nothing outside of it; writing,
    external calls and cloud models unattended need a narrow, dated grant."""
    reports = folder("/srv/reports")
    task = grant(reports, "read", mode="unattended")
    assert read("/srv/reports/q3.csv", grants=[task], mode="unattended").allowed
    assert read("/srv/other/q3.csv", grants=[task], mode="unattended").kind == "missing_grant"
    supervised_only = grant(reports, "read")
    assert read("/srv/reports/q3.csv", grants=[supervised_only], mode="unattended").kind == "missing_grant"
    standing_write = grant(reports, "write", mode="unattended")
    assert check(reports, "write", "write_internal", grants=[standing_write], mode="unattended").kind == (
        "missing_grant"
    )
    dated = grant(
        reports,
        "write",
        mode="unattended",
        duration="until",
        expires_at=AT + timedelta(hours=1),
        scope="agent",
        agents=(AGENT,),
    )
    assert check(reports, "write", "write_internal", grants=[dated], mode="unattended").allowed


def test_revocation_and_expiry_apply_at_the_next_evaluation() -> None:
    docs = grant(folder("/home/alice/docs"), "read")
    assert read("/home/alice/docs/a", grants=[docs]).allowed
    revoked = docs.model_copy(update={"revoked_at": AT})
    assert read("/home/alice/docs/a", grants=[revoked], at=AT).kind == "missing_grant"
    dated = grant(folder("/home/alice/docs"), "read", duration="until", expires_at=AT + timedelta(minutes=1))
    assert read("/home/alice/docs/a", grants=[dated]).allowed
    assert read("/home/alice/docs/a", grants=[dated], at=AT + timedelta(minutes=1)).kind == "missing_grant"
    block = deny(folder("/home/alice"), "read")
    lifted = block.model_copy(update={"revoked_at": AT})
    assert read("/home/alice/docs/a", grants=[docs], denies=[lifted]).allowed


# ---- folder comparison is by component, never by string prefix ----------------------------------------------------


def test_folders_compare_by_component_not_by_string_prefix() -> None:
    docs = grant(folder("/home/alice/docs"), "read")
    assert read("/home/alice/docs-private/x", grants=[docs]).kind == "missing_grant"
    assert read("/home/alice/docs/../secrets/x", grants=[docs]).kind == "missing_grant"
    assert read("/home/alice//docs/./a", grants=[docs]).allowed
    block = deny(folder("/home/alice/docs"), "read")
    assert read(
        "/home/alice/docs-private/x", grants=[docs, grant(folder("/home/alice"), "read", id="agr_2")], denies=[block]
    ).allowed


def test_folder_names_are_compared_in_unicode_nfc() -> None:
    composed, decomposed = "/home/alice/R\u00e9sum\u00e9", "/home/alice/Re\u0301sume\u0301"
    assert folder_parts(composed) == folder_parts(decomposed)
    assert read(f"{decomposed}/cv.pdf", grants=[grant(folder(composed), "read")]).allowed
    assert read(f"{composed}/cv.pdf", denies=[deny(folder(decomposed), "read")]).kind == "denied"


def test_a_deny_ignores_case_and_a_grant_does_not() -> None:
    """Fail closed both ways on case-insensitive file systems: a deny is broad, a grant narrow."""
    assert read("/home/alice/Docs/a", denies=[deny(folder("/home/alice/docs"), "read")]).kind == "denied"
    assert read("/home/alice/Docs/a", grants=[grant(folder("/home/alice/docs"), "read")]).kind == "missing_grant"


def test_windows_paths_compare_by_component() -> None:
    assert folder_parts("C:\\Users\\Alice\\..\\Bob") == folder_parts("c:/users/BOB") == ("c:", "users", "bob")
    assert folder_parts("\\\\server\\share\\a") == ("//server/share", "a")
    assert read("C:\\Users\\Alice\\a.txt", grants=[grant(folder("C:\\Users\\Alice"), "read")]).allowed
    with pytest.raises(ValueError):
        folder_parts("Users\\Alice")


# ---- scope, mode, duration -------------------------------------------------------------------------------------------


def test_scope_agent_list_and_all() -> None:
    docs = folder("/home/alice/docs")
    some = grant(docs, "read", scope="agents", agents=(AGENT, "wf_2/x"))
    assert read("/home/alice/docs", grants=[some]).allowed
    assert read("/home/alice/docs", grants=[some], agent=OTHER).kind == "missing_grant"
    assert read("/home/alice/docs", grants=[some], agent=None).kind == "missing_grant"  # unknown agent: no grant
    one = deny(docs, "read", scope="agent", agents=(OTHER,))
    assert read("/home/alice/docs", grants=[some], denies=[one]).allowed
    assert read("/home/alice/docs", grants=[grant(docs, "read")], denies=[one], agent=None).kind == "denied"
    with pytest.raises(ValueError):
        grant(docs, "read", scope="agent", agents=())
    with pytest.raises(ValueError):
        grant(docs, "read", scope="all", agents=(AGENT,))


def test_a_deny_can_hold_for_one_mode_only() -> None:
    docs = folder("/home/alice/docs")
    standing = grant(docs, "read", mode="unattended")
    background = deny(docs, "read", mode="unattended")
    assert read("/home/alice/docs", grants=[standing], denies=[background]).allowed
    assert read("/home/alice/docs", grants=[standing], denies=[background], mode="unattended").kind == "denied"


def test_a_once_grant_is_used_last_and_not_after_its_use() -> None:
    docs = folder("/home/alice/docs")
    once = grant(docs, "read", id="agr_once", duration="once", created_at=AT - timedelta(days=2))
    standing = grant(docs, "read", id="agr_standing")
    assert read("/home/alice/docs", grants=[once, standing]).source == "grant:agr_standing"
    assert read("/home/alice/docs", grants=[once]).source == "grant:agr_once"
    used = once.model_copy(update={"status": "used"})
    assert read("/home/alice/docs", grants=[used]).kind == "missing_grant"


# ---- the store: only a person writes, every change is audited --------------------------------------------------------


@pytest.fixture
def rules() -> AccessRules:
    clock = iter(AT + timedelta(seconds=n) for n in range(1000))
    return AccessRules(SQLiteStore(":memory:"), clock=lambda: next(clock))


def test_grants_and_denies_are_stored_revoked_and_audited(rules: AccessRules) -> None:
    docs = folder("/home/alice/docs")
    stored = rules.grant(resource=docs, actions=["read"], duration="until_revoked", by=ALICE)
    blocked = rules.deny(resource=folder("/home/alice/docs/private"), actions=["read"], by=ALICE)
    assert rules.grants() == [stored] and rules.denies() == [blocked]
    assert stored.granted_by == "usr_alice" and blocked.denied_by == "usr_alice"
    revoked = rules.revoke_grant(stored.id, by=Person("usr_bob"))
    assert revoked.revoked_at is not None and revoked.revoked_by == "usr_bob"
    assert rules.revoke_grant(stored.id, by=ALICE) == revoked  # a second revocation changes nothing
    lifted = rules.revoke_deny(blocked.id, by=ALICE)
    grants, denies = rules.current()
    assert grants == [revoked] and denies == [lifted]
    assert read("/home/alice/docs/a", grants=grants, denies=denies, at=AT + timedelta(minutes=1)).kind == (
        "missing_grant"
    )
    with pytest.raises(NodestroError) as error:
        rules.revoke_deny("adn_missing", by=ALICE)
    assert error.value.code == "access.not_found"
    events = [event for _, event in rules.store.items(AUDIT, ToolApprovalEvent)]
    kinds = sorted(event.kind for event in events)
    assert kinds == ["access_denied", "access_deny_revoked", "access_grant_revoked", "access_granted"]
    for event in events:
        assert "/home/alice" not in event.model_dump_json()  # a folder path never reaches the audit trail
    logged = activity.query(rules.store)
    assert sorted(event.action for event in logged) == [
        "tool_approval.access_denied",
        "tool_approval.access_deny_revoked",
        "tool_approval.access_grant_revoked",
        "tool_approval.access_granted",
    ]
    assert {event.resource_type for event in logged} == {"access_grant", "access_deny"}
    assert all("/home" not in event.resource_id for event in logged)


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"duration": "until"}, "access.expiry"),
        ({"duration": "until_revoked", "expires_at": AT + timedelta(days=1)}, "access.expiry"),
        ({"duration": "until", "expires_at": AT - timedelta(days=1)}, "access.expiry"),
        ({"duration": "conversation"}, "access.conversation_required"),
        ({"duration": "until_revoked", "conversation_id": "run_1"}, "access.conversation_required"),
        ({"duration": "until_revoked", "actions": ["write"], "mode": "unattended"}, "access.unattended_expiry"),
        ({"duration": "until_revoked", "resource": folder("relative/path")}, "access.folder_path"),
        ({"duration": "until_revoked", "scope": "agent"}, "access.invalid"),
    ],
)
def test_invalid_grants_are_refused(rules: AccessRules, values: dict[str, Any], code: str) -> None:
    arguments: dict[str, Any] = {"resource": folder("/home/alice/docs"), "actions": ["read"], "by": ALICE, **values}
    with pytest.raises(NodestroError) as error:
        rules.grant(**arguments)
    assert error.value.code == code
    assert rules.grants() == []


def test_a_once_grant_is_used_exactly_once(rules: AccessRules) -> None:
    once = rules.grant(resource=folder("/home/alice/docs"), actions=["read"], duration="once", by=ALICE)
    standing = rules.grant(resource=folder("/home/alice/docs"), actions=["read"], duration="until_revoked", by=ALICE)
    assert rules.use(standing.id) is False
    assert rules.use(once.id) is True
    assert rules.use(once.id) is False
    assert read("/home/alice/docs", grants=[g for g in rules.grants() if g.id == once.id]).kind == "missing_grant"


# ---- a grant is never written by a model ---------------------------------------------------------------------------


def test_a_grant_needs_a_person_object_not_a_name(rules: AccessRules) -> None:
    with pytest.raises(NodestroError) as error:
        rules.grant(resource=tool("search_web"), actions=["execute"], duration="until_revoked", by="usr_alice")  # type: ignore[arg-type]
    assert error.value.code == "access.person_required"
    assert rules.grants() == []


@pytest.mark.parametrize("principal", ["system", "api:key_1", "model:claude", "", "agent:wf_1/a", "a\nb"])
def test_machine_principals_are_not_persons(principal: str) -> None:
    with pytest.raises(NodestroError) as error:
        Person(principal)
    assert error.value.code == "access.person_required"


def test_nothing_running_inside_a_tool_call_writes_grants(rules: AccessRules) -> None:
    """Code that runs for a model (inside a tool call) cannot grant, deny or revoke, even with a person at hand."""
    stored = rules.deny(resource=tool("search_web"), actions=["execute"], by=ALICE)
    bind_call(
        CallBinding(tool="any_tool", capability=ToolCapability(access="read", reach="internal", source="nodestro"))
    )
    try:
        for attempt in (
            lambda: rules.grant(resource=tool("search_web"), actions=["execute"], duration="until_revoked", by=ALICE),
            lambda: rules.deny(resource=tool("search_web"), actions=["execute"], by=ALICE),
            lambda: rules.revoke_deny(stored.id, by=ALICE),
        ):
            with pytest.raises(NodestroError) as error:
                attempt()
            assert error.value.code == "access.in_tool_call"
    finally:
        bind_call(None)
    assert rules.grants() == [] and rules.denies() == [stored]


# ---- review of #201 ------------------------------------------------------------------------------------------------


def tool_request(facts: ToolFacts, **extra: Any) -> AccessRequest:
    effect = effect_class(facts.access, facts.reach)
    return AccessRequest(resource=tool("tool_x"), action="execute", effect=effect, agent=AGENT, tool=facts, **extra)


def test_a_tool_grant_holds_only_for_the_tool_version_classification_and_definition_it_was_given_for() -> None:
    """H-1: like a pre-grant, a tool grant ends with a new version, a reclassification or another MCP definition."""
    given = ToolFacts(version=1, access="write", reach="internal", definition_hash="h1")
    stored = grant(tool("tool_x"), "execute", tool=given)

    def found(facts: ToolFacts) -> AccessDecision:
        return evaluate(tool_request(facts), grants=[stored], denies=[], at=AT)

    assert found(given).source == "grant:agr_1"
    assert found(given.model_copy(update={"version": 7})).kind == "missing_grant"
    assert found(given.model_copy(update={"version": None})).kind == "missing_grant"
    assert found(given.model_copy(update={"reach": "external"})).kind == "missing_grant"
    assert found(given.model_copy(update={"definition_hash": "h2"})).kind == "missing_grant"
    unbound = grant(tool("tool_x"), "execute", tool=None)
    assert evaluate(tool_request(given), grants=[unbound], denies=[], at=AT).kind == "missing_grant"
    # A deny holds for every version.
    blocked = deny(tool("tool_x"), "execute")
    assert evaluate(tool_request(given.model_copy(update={"version": 9})), grants=[], denies=[blocked], at=AT).kind == (
        "denied"
    )


@pytest.mark.parametrize(
    ("path", "limit"),
    [
        ("\\\\?\\C:\\Windows\\System32", "protected_folder"),
        ("\\\\?\\UNC\\localhost\\C$\\Windows", "protected_folder"),
        ("C:\\Windows.\\System32", "protected_folder"),
        ("C:\\Windows \\System32", "protected_folder"),
        ("C:\\WINDOWS\\system32", "protected_folder"),
        ("\\\\localhost\\C$\\Windows", "protected_folder"),
        ("\\\\fileserver\\c$\\Program Files\\x", "protected_folder"),
        ("\\\\.\\C:\\Windows", "unresolvable_path"),
        ("C:\\PROGRA~1\\x", "unresolvable_path"),
        ("C:\\Users\\a\\file.txt:secret", "unresolvable_path"),
        ("/root/.ssh", "protected_folder"),
        ("/private/var/db/x", "protected_folder"),
        ("/lib/x", "protected_folder"),
        # Re-review N-2: forward-slash UNC and device paths are Windows paths too.
        ("//localhost/C$/Windows", "protected_folder"),
        ("//server/c$/Windows/System32", "protected_folder"),
        ("//?/C:/Windows", "protected_folder"),
        ("//./C:/Windows", "unresolvable_path"),
        ("\\/localhost/C$/Windows", "protected_folder"),
    ],
)
def test_other_spellings_of_a_protected_folder_hit_the_safety_limit(path: str, limit: str) -> None:
    """M-1: Windows prefixes, trailing dots and spaces, case, 8.3 names, administrative shares, more POSIX roots."""
    found = read(path, grants=[grant(folder("/"), "read"), grant(folder("C:\\"), "read", id="agr_2")])
    assert (found.kind, found.source) == ("safety_limit", f"limit:{limit}")


def test_system_folders_named_by_the_environment_are_protected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemRoot", "D:\\WinNT")
    monkeypatch.setenv("ProgramFiles", "E:\\Apps")
    assert read("D:\\winnt\\system32").source == "limit:protected_folder"
    assert read("E:\\Apps\\tool").source == "limit:protected_folder"
    monkeypatch.delenv("SystemRoot")
    assert read("D:\\winnt\\system32").kind == "missing_grant"


def test_a_folder_request_names_the_protected_data_folder() -> None:
    """M-1: the data folder limit cannot be forgotten by a caller."""
    with pytest.raises(ValueError):
        AccessRequest(resource=folder("/home/alice"), action="read", effect="read_internal")
    assert read(f"{DATA}/nodestro.db", grants=[grant(folder("/"), "read")]).source == "limit:protected_folder"


@pytest.mark.parametrize("path", ["/home/a/x ", " /home/a/x", "/home/a/x\n", ""])
def test_folder_paths_with_outer_white_space_are_refused(path: str) -> None:
    """L-3: ``/home/a/x `` is another folder than ``/home/a/x``; it is refused, not trimmed."""
    with pytest.raises(ValueError):
        folder_parts(path)
    if path:
        assert read(path, grants=[grant(folder("/home/a"), "read")]).source == "limit:unresolvable_path"


def test_a_revoked_or_expired_once_grant_is_never_used(rules: AccessRules) -> None:
    """M-2."""
    revoked = rules.grant(resource=folder("/home/alice/docs"), actions=["read"], duration="once", by=ALICE)
    rules.revoke_grant(revoked.id, by=ALICE)
    assert rules.use(revoked.id) is False
    used = rules.grant(resource=folder("/home/alice/docs"), actions=["read"], duration="once", by=ALICE)
    assert rules.use(used.id) is True
    assert rules.revoke_grant(used.id, by=ALICE).status == "used"  # nothing left to revoke, nothing audited
    kinds = [event.kind for _, event in rules.store.items(AUDIT, ToolApprovalEvent)]
    assert kinds.count("access_grant_revoked") == 1


def test_a_revocation_between_reading_and_using_a_once_grant_wins(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-2 (review PoC): ``use`` read the grant, a person revoked it, then ``use`` wrote: the revocation stays."""
    store = SQLiteStore(tmp_path / "db.sqlite")
    rules = AccessRules(store)
    once = rules.grant(
        resource=AccessResource(type="workflow", id="wf_1"), actions=["execute"], duration="once", by=ALICE
    )
    real_get = store.get

    def get_then_revoke(collection: str, key: str, model: Any) -> Any:
        found = real_get(collection, key, model)
        monkeypatch.setattr(store, "get", real_get)
        rules.revoke_grant(once.id, by=ALICE)
        return found

    monkeypatch.setattr(store, "get", get_then_revoke)
    assert rules.use(once.id) is False
    stored = real_get("access_grants", once.id, Grant)
    assert stored is not None and stored.status == "revoked" and stored.revoked_at is not None


def _race(rules: AccessRules, grant_id: str) -> list[object]:
    """Four uses and two revocations of one grant, started together."""
    import threading

    results: list[object] = []
    barrier = threading.Barrier(6)

    def use() -> None:
        barrier.wait()
        results.append(rules.use(grant_id))

    def revoke() -> None:
        barrier.wait()
        results.append(rules.revoke_grant(grant_id, by=ALICE).status)

    threads = [threading.Thread(target=use) for _ in range(4)] + [threading.Thread(target=revoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_use_and_revoke_racing_have_exactly_one_winner(tmp_path: Any) -> None:
    """M-2: concurrent uses and revocations of one once grant (one SQLite file, several threads)."""
    store = SQLiteStore(tmp_path / "db.sqlite")
    rules = AccessRules(store)
    for _ in range(10):
        once = rules.grant(
            resource=AccessResource(type="workflow", id="wf_1"), actions=["execute"], duration="once", by=ALICE
        )
        uses = _race(rules, once.id).count(True)
        stored = store.get("access_grants", once.id, Grant)
        assert stored is not None
        if stored.status == "used":
            assert uses == 1 and stored.revoked_at is None
        else:
            assert stored.status == "revoked" and uses == 0


def test_unattended_grants_name_their_agents_and_end_within_the_maximum(rules: AccessRules) -> None:
    """M-3: an unattended grant for more than reading is narrow (named agents) and dated (at most 30 days)."""
    common: dict[str, Any] = {
        "resource": folder("/srv/reports"),
        "actions": ["write"],
        "mode": "unattended",
        "by": ALICE,
    }
    soon = AT + timedelta(days=1)
    for extra, code in (
        ({"duration": "until", "expires_at": soon}, "access.unattended_scope"),
        (
            {"duration": "until", "expires_at": AT + timedelta(days=31), "scope": "agent", "agents": [AGENT]},
            "access.unattended_too_long",
        ),
    ):
        with pytest.raises(NodestroError) as error:
            rules.grant(**common, **extra)
        assert error.value.code == code
    stored = rules.grant(**common, duration="until", expires_at=soon, scope="agent", agents=[AGENT])
    assert stored.expires_at == soon
    far = grant(
        folder("/srv/reports"),
        "write",
        mode="unattended",
        duration="until",
        expires_at=AT + timedelta(days=365),
        scope="agent",
        agents=(AGENT,),
    )
    assert check(folder("/srv/reports"), "write", "write_internal", grants=[far], mode="unattended").kind == (
        "missing_grant"
    )


def test_times_must_carry_a_time_zone(rules: AccessRules) -> None:
    """L-2: naive times are refused with a clear error, never a TypeError at evaluation."""
    naive = datetime(2999, 1, 1)
    with pytest.raises(ValueError):
        grant(folder("/home/alice"), "read", duration="until", expires_at=naive)
    with pytest.raises(NodestroError) as error:
        rules.grant(resource=folder("/home/alice"), actions=["read"], duration="until", expires_at=naive, by=ALICE)
    assert error.value.code == "access.expiry"
    with pytest.raises(ValueError):
        read("/home/alice", at=naive)


def test_tool_grants_name_the_tool_and_only_tool_grants_do(rules: AccessRules) -> None:
    with pytest.raises(NodestroError) as error:
        rules.grant(resource=tool("search_web"), actions=["execute"], duration="until_revoked", by=ALICE)
    assert error.value.code == "access.tool_facts_required"
    with pytest.raises(NodestroError) as error:
        rules.grant(resource=folder("/home/a"), actions=["read"], duration="until_revoked", by=ALICE, tool=SEARCH)
    assert error.value.code == "access.tool_facts_required"
    stored = rules.grant(
        resource=tool("search_web"), actions=["execute"], duration="until_revoked", by=ALICE, tool=SEARCH
    )
    assert stored.tool == SEARCH


def test_run_own_tools_cannot_be_denied(rules: AccessRules) -> None:
    """L-5: the run's own tools never pass the gate, so a deny on them is refused instead of silently ignored."""
    from orkexa.memory import MEMORY_TOOL_NAME

    from nodestro_runtime import file_delivery, skill_tools
    from nodestro_runtime.tool_approvals import RUN_OWN_TOOLS

    assert {
        MEMORY_TOOL_NAME,
        file_delivery.TOOL_NAME,
        "propose_knowledge",
        skill_tools.LOAD_TOOL,
        skill_tools.READ_TOOL,
    } == RUN_OWN_TOOLS
    for name in sorted(RUN_OWN_TOOLS):
        with pytest.raises(NodestroError) as error:
            rules.deny(resource=tool(name), actions=["execute"], by=ALICE)
        assert error.value.code == "access.deny_not_enforced"
    assert rules.denies() == []


def test_a_root_relative_path_names_no_folder_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-review N-2: on Windows ``/Windows`` is relative to the current drive, so it is refused."""
    from nodestro_runtime import tool_policy

    assert folder_parts("/Windows") == ("/", "Windows")
    monkeypatch.setattr(tool_policy.os, "name", "nt")
    for path in ("/Windows", "/Windows/System32"):
        with pytest.raises(ValueError):
            folder_parts(path)
    assert folder_parts("C:/Windows") == ("c:", "windows")


def test_windows_grants_compare_letters_and_denies_fold() -> None:
    """Re-review N-3: ``straße`` and ``STRASSE`` are two folders on Windows; a grant for one does not cover the
    other, while a deny on one still blocks both (fail closed)."""
    assert folder_parts("C:\\Users\\a\\stra\u00dfe") != folder_parts("C:\\Users\\a\\STRASSE")
    assert folder_parts("C:\\Users\\A\\Docs") == folder_parts("c:\\users\\a\\docs")
    granted = grant(folder("C:\\Users\\a\\stra\u00dfe"), "read")
    assert read("C:\\Users\\a\\STRASSE\\x", grants=[granted]).kind == "missing_grant"
    assert read("C:\\Users\\a\\Stra\u00dfe\\x", grants=[granted]).allowed
    blocked = deny(folder("C:\\Users\\a\\stra\u00dfe"), "read")
    assert read("C:\\Users\\a\\STRASSE\\x", denies=[blocked]).kind == "denied"


@pytest.mark.parametrize("collection", ["access_denies", "access_grants"])
def test_a_record_stored_before_status_existed_can_be_revoked(rules: AccessRules, collection: str) -> None:
    """Re-review N-4: a record of the first head of #201 has no ``status`` key; revoking it must not be a no-op."""
    docs = folder("/home/alice/docs")
    if collection == "access_denies":
        stored: Any = rules.deny(resource=docs, actions=["read"], by=ALICE)
    else:
        stored = rules.grant(resource=docs, actions=["read"], duration="until_revoked", by=ALICE)
    raw = rules.store.get_raw(collection, stored.id)
    raw.pop("status")
    rules.store.put_raw(collection, stored.id, raw)
    revoke = rules.revoke_deny if collection == "access_denies" else rules.revoke_grant
    revoked = revoke(stored.id, by=ALICE)
    assert revoked.status == "revoked" and revoked.revoked_at is not None
    assert rules.store.get_raw(collection, stored.id)["status"] == "revoked"
    kinds = [event.kind for _, event in rules.store.items(AUDIT, ToolApprovalEvent)]
    assert kinds.count("access_deny_revoked" if collection == "access_denies" else "access_grant_revoked") == 1
