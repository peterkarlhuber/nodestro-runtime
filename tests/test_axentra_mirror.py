"""The audit and usage mirrors into Axentra (M3): one transaction with Nodestro's record, idempotent, reconcilable.

Needs ``NODESTRO_TEST_POSTGRES`` for the database cases and the ``axentra`` package (Python 3.11+).
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
import secrets
from typing import Any

import postgres_support
import pytest

from nodestro_runtime import axentra_platform
from nodestro_runtime.axentra_platform import AxentraConfig, AxentraEvents, usage_key, usage_quantities
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import Usage, now
from nodestro_runtime.server_settings import ServerSettings
from nodestro_runtime.tool_approvals import AUDIT, ToolApprovalEvent, record_audit
from nodestro_runtime.usage_log import UsageEntry, UsageLog

pytest.importorskip("axentra", reason="Axentra needs Python 3.11+ and nodestro-runtime[postgres]")


def _key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _env(**extra: str) -> dict[str, str]:
    return {
        "NODESTRO_DATABASE_URL": "postgresql://nodestro@db/nodestro",
        "NODESTRO_PUBLIC_ORIGIN": "https://studio.example",
        "NODESTRO_OIDC_ISSUER": "https://id.example",
        "NODESTRO_OIDC_CLIENT_ID": "studio",
        "NODESTRO_OIDC_CLIENT_SECRET": "s3cret",
        "NODESTRO_SECRET_KEY": base64.b64encode(b"k" * 32).decode(),
        **extra,
    }


# ---- settings and quantities ----------------------------------------------------------------------------


def test_the_mirrors_are_off_by_default_and_need_axentra() -> None:
    config = ServerSettings.from_env(_env()).axentra
    assert not config.audit and not config.usage
    with pytest.raises(NodestroError) as caught:
        ServerSettings.from_env(_env(NODESTRO_AXENTRA_AUDIT="1", NODESTRO_AXENTRA_USAGE="maybe"))
    fields = [item["field"] for item in caught.value.params["problems"]]
    assert fields == ["NODESTRO_AXENTRA_AUDIT", "NODESTRO_AXENTRA_USAGE"]
    on = ServerSettings.from_env(
        _env(NODESTRO_AXENTRA="on", NODESTRO_AXENTRA_KEYRING=f"k1:{_key()}", NODESTRO_AXENTRA_USAGE="1")
    ).axentra
    assert on.usage and not on.audit


def test_only_a_complete_cost_is_mirrored() -> None:
    usage = Usage(requests=2, input_tokens=100, output_tokens=0, estimated_cost_usd=0.00123456789, cost_complete=True)
    assert usage_quantities(usage) == {
        "nodestro.ai.requests": 2,
        "nodestro.ai.input_tokens": 100,
        "nodestro.ai.cost_usd": Decimal("0.001235"),
    }
    partial = Usage(requests=1, input_tokens=5, estimated_cost_usd=0.5, known_cost_usd=0.2, cost_complete=False)
    assert "nodestro.ai.cost_usd" not in usage_quantities(partial)
    unknown = Usage(requests=1, estimated_cost_usd=None)
    assert usage_quantities(unknown) == {"nodestro.ai.requests": 1}
    legacy = Usage(requests=1, estimated_cost_usd=0.25)  # recorded before price references: cost_complete None
    assert usage_quantities(legacy)["nodestro.ai.cost_usd"] == Decimal("0.250000")
    assert usage_key("use_1", "nodestro.ai.requests") == "nodestro:usage:use_1:nodestro.ai.requests"


# ---- PostgreSQL ------------------------------------------------------------------------------------------


@pytest.fixture
def database() -> Iterator[Any]:
    pytest.importorskip("psycopg")
    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    opened.migrate()
    opened.test_schema = schema  # type: ignore[attr-defined]
    yield opened
    opened.close()
    postgres_support.drop_schema(schema)


def _link(database: Any, *, audit: bool = True, usage: bool = True) -> Any:
    services = axentra_platform.AxentraServices(
        AxentraConfig(enabled=True, keyring=f"k1:{_key()}", allow_superuser=True, audit=audit, usage=usage),
        database_url=postgres_support.DSN,
        public_origin="http://localhost:8000",
        schema=database.test_schema,
    )
    services.migrate()
    services.start()
    return axentra_platform.AxentraLink(services, database)


@pytest.fixture
def mirrored(database: Any) -> Iterator[tuple[Any, AxentraEvents]]:
    from identity_support import create_tenants

    create_tenants(database, [("alpha", "Alpha"), ("beta", "Beta")])
    link = _link(database)
    events = axentra_platform.attach_platform_events(database, link)
    assert events is not None
    try:
        yield database, events
    finally:
        database.platform_events = None
        link.services.close()


def _audit_rows(database: Any) -> list[tuple[Any, ...]]:
    return database._query(
        "SELECT action, outcome, actor_kind, actor_display, target_type, target_id, details, tenant_id"
        " FROM axentra_audit_events WHERE action LIKE %s ORDER BY occurred_at, id",
        ("nodestro.%",),
    )


def _usage_rows(database: Any) -> dict[str, Decimal]:
    rows = database._query(
        "SELECT m.key, sum(e.quantity) FROM axentra_usage_events e JOIN axentra_usage_meters m ON m.id = e.meter_id"
        " GROUP BY m.key"
    )
    return {str(key): Decimal(value) for key, value in rows}


def _event(**extra: Any) -> ToolApprovalEvent:
    values: dict[str, Any] = {
        "kind": "decided",
        "actor": "usr_1",
        "tool": "send_mail",
        "run_id": "run_1",
        "request_id": "tar_1",
        "outcome": "approved",
        "arguments_hash": "sha256:abc",
    }
    values.update(extra)
    return ToolApprovalEvent(**values)


def test_an_approval_event_and_its_mirror_commit_together(mirrored: tuple[Any, AxentraEvents]) -> None:
    database, events = mirrored
    store = database.store("alpha")
    event = record_audit(store, _event())
    record_audit(store, _event(kind="decision_refused", outcome="tool_approvals.stale"))
    assert len(store.items(AUDIT, ToolApprovalEvent)) == 2
    rows = _audit_rows(database)
    assert [(row[0], row[1]) for row in rows] == [
        ("nodestro.tool_approval.decided", "success"),
        ("nodestro.tool_approval.decision_refused", "denied"),
    ]
    _, _, kind, display, target_type, target_id, details, tenant = rows[0]
    assert (kind, display, target_type, target_id) == ("system", "usr_1", "run", "run_1")  # no linked user yet
    assert details["nodestro_event"] == event.id and details["arguments_hash"] == "sha256:abc"
    assert tenant == events.link.tenant("alpha") and events.stats.audit == 2 and events.stats.failed == 0


def test_a_rolled_back_transaction_keeps_neither_the_event_nor_its_mirror(mirrored: tuple[Any, AxentraEvents]) -> None:
    database, _ = mirrored
    store = database.store("alpha")

    class Stop(Exception):
        pass

    with pytest.raises(Stop), store.transaction():
        record_audit(store, _event())
        raise Stop
    assert store.items(AUDIT, ToolApprovalEvent) == [] and _audit_rows(database) == []


def test_a_linked_user_is_the_actor_of_the_mirror(mirrored: tuple[Any, AxentraEvents]) -> None:
    database, events = mirrored
    with events.link.joined() as connection:
        events.link.link(connection, "user", "usr_9", "usr_axentra9")
    record_audit(database.store("alpha"), _event(actor="usr_9"))
    assert _audit_rows(database)[0][2:4] == ("user", "usr_9")
    assert database._query("SELECT actor_id FROM axentra_audit_events WHERE action LIKE %s", ("nodestro.%",)) == [
        ("usr_axentra9",)
    ]


def test_mcp_events_are_mirrored_with_redacted_addresses(mirrored: tuple[Any, AxentraEvents]) -> None:
    from nodestro_runtime.mcp.models import McpEvent
    from nodestro_runtime.mcp.service import mcp_event_details

    database, events = mirrored
    store = database.store("beta")
    event = McpEvent(kind="server_changed", server_id="mcp_1", details={"url": "https://mcp.example/x?token=abc"})
    with store.transaction():
        store.put("mcp_events", f"{event.at.isoformat()}_{event.id}", event)
        events.audit(
            "beta",
            event_id=event.id,
            at=event.at,
            action="nodestro.mcp.server_changed",
            actor=None,
            target_type="mcp_server",
            target_id="mcp_1",
            outcome="success",
            details=mcp_event_details(event),
        )
    (row,) = _audit_rows(database)
    assert row[0] == "nodestro.mcp.server_changed" and "token" not in str(row[6]) and row[6]["url"].startswith("https")


def _record(log: UsageLog, cost: Decimal | None = Decimal("0.002")) -> list[UsageEntry]:
    from orkexa import UsageRecord

    record = UsageRecord(
        agent="assistant",
        model="claude-sonnet-5-5",
        input_tokens=120,
        output_tokens=30,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_creation_5m_input_tokens=0,
        cache_creation_1h_input_tokens=0,
        thinking_tokens=0,
        web_search_requests=0,
        web_fetch_requests=0,
        estimated_cost_usd=cost,
        provider="anthropic",
    )
    return log.record("assistant", [record], label="question")


def test_usage_is_mirrored_once_into_the_meters(mirrored: tuple[Any, AxentraEvents]) -> None:
    database, events = mirrored
    log = UsageLog(database.store("alpha"))
    (entry,) = _record(log)
    totals = _usage_rows(database)
    assert totals["nodestro.ai.requests"] == 1 and totals["nodestro.ai.input_tokens"] == 120
    assert totals["nodestro.ai.output_tokens"] == 30
    # Replaying the entry (as reconcile does) never counts it twice.
    with database.store("alpha").transaction():
        events.usage("alpha", entry)
    assert _usage_rows(database) == totals
    assert events.reconcile()["usage_missing"] == 0


def test_a_failing_mirror_keeps_nodestros_record_and_reconcile_writes_it(mirrored: tuple[Any, AxentraEvents]) -> None:
    database, events = mirrored
    events._meters_ready = False  # the meters look missing: the mirror fails inside its savepoint
    log = UsageLog(database.store("alpha"))
    (entry,) = _record(log)
    assert [item.id for item in log.entries()] == [entry.id]  # Nodestro's ledger is intact
    assert events.stats.failed == 1 and _usage_rows(database) == {}
    report = events.reconcile(dry_run=True)
    assert report["usage_missing"] == 1 and report["written"] == 0 and _usage_rows(database) == {}
    report = events.reconcile()
    assert report["usage_missing"] == 1 and report["failed"] == 0 and report["written"] == 1
    assert _usage_rows(database)["nodestro.ai.requests"] == 1
    assert events.reconcile()["usage_missing"] == 0


def test_records_from_before_the_switch_are_reconciled(database: Any) -> None:
    from identity_support import create_tenants

    create_tenants(database, [("alpha", "Alpha")])
    store = database.store("alpha")
    old = record_audit(store, _event())  # no sink yet: Nodestro only
    old_usage = UsageLog(store)
    (entry,) = _record(old_usage)
    stale = UsageEntry(source="assistant", model="m", usage=Usage(requests=1), at=now() - timedelta(days=500))
    store.put("usage_log", stale.id, stale)
    link = _link(database)
    try:
        events = axentra_platform.attach_platform_events(database, link)
        assert events is not None
        report = events.reconcile(dry_run=True)
        assert (report["audit_missing"], report["usage_missing"], report["usage_too_old"]) == (1, 1, 1)
        report = events.reconcile()
        assert report["written"] == 2 and report["failed"] == 0
        assert [row[6]["nodestro_event"] for row in _audit_rows(database)] == [old.id]
        assert _usage_rows(database)["nodestro.ai.requests"] == 1  # the stale entry is reported, not written
        again = events.reconcile()
        assert (again["audit_missing"], again["usage_missing"], again["written"]) == (0, 0, 0)
        assert entry.id in {
            str(row[0]).split(":")[2] for row in database._query("SELECT idempotency_key FROM axentra_usage_events")
        }
    finally:
        database.platform_events = None
        link.services.close()


def test_tenants_without_a_nodestro_tenant_row_are_not_mirrored(mirrored: tuple[Any, AxentraEvents]) -> None:
    database, events = mirrored
    record_audit(database.store("nodestro-price-reference"), _event())
    assert _audit_rows(database) == [] and events.stats.skipped == 1
    assert database._query("SELECT count(*) FROM axentra_tenants WHERE slug LIKE %s", ("nodestro-price%",))[0][0] == 0


def test_the_mirrors_stay_detached_when_switched_off(database: Any) -> None:
    link = _link(database, audit=False, usage=False)
    try:
        assert axentra_platform.attach_platform_events(database, link) is None
        assert database.platform_events is None and database.store("alpha").platform_events is None
    finally:
        link.services.close()


def test_older_mcp_events_are_reconciled_without_their_full_address(mirrored: tuple[Any, AxentraEvents]) -> None:
    from nodestro_runtime.mcp.models import McpEvent

    database, events = mirrored
    store = database.store("beta")
    database.platform_events = None  # stored before the mirror was switched on
    event = McpEvent(
        kind="server_changed",
        server_id="mcp_2",
        details={"url": "https://mcp.example/k/abcdefabcdefabcdefabcdef12?x=1"},
    )
    store.put("mcp_events", f"{event.at.isoformat()}_{event.id}", event)
    database.platform_events = events
    report = events.reconcile(tenant="beta")
    assert (report["audit_missing"], report["written"]) == (1, 1)
    (row,) = _audit_rows(database)
    assert row[0] == "nodestro.mcp.server_changed" and row[6]["url"] == "https://mcp.example"
    assert row[6]["nodestro_event"] == event.id and events.reconcile(tenant="beta")["audit_missing"] == 0
