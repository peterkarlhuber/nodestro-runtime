"""Run usage mirror, secrets rollback checks, inbox notifications and metrics of the server profile (M7, req. 4.5).

Needs ``NODESTRO_TEST_POSTGRES`` for the database cases and the ``axentra`` package (Python 3.11+).
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
import secrets
import threading
from typing import Any

import postgres_support
import pytest

from nodestro_runtime import axentra_metrics, axentra_platform
from nodestro_runtime.axentra_platform import (
    AxentraConfig,
    AxentraEvents,
    reconciliation_backlog,
    reencrypt_tenant_secrets,
    run_usage_key,
    secrets_rollback_problems,
    stored_secret_formats,
)
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import now
from nodestro_runtime.server_settings import SecretBox, ServerSettings

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


def _problems(environ: dict[str, str]) -> list[str]:
    with pytest.raises(NodestroError) as caught:
        ServerSettings.from_env(environ)
    return [item["field"] for item in caught.value.params["problems"]]


# ---- settings --------------------------------------------------------------------------------------------------


def test_run_usage_and_notifications_are_off_by_default_and_need_their_prerequisites() -> None:
    config = ServerSettings.from_env(_env()).axentra
    assert not config.run_usage and not config.notify
    ring = f"k1:{_key()}"
    assert _problems(_env(NODESTRO_AXENTRA_NOTIFY="1")) == ["NODESTRO_AXENTRA_NOTIFY"]
    assert _problems(_env(NODESTRO_AXENTRA="on", NODESTRO_AXENTRA_KEYRING=ring, NODESTRO_AXENTRA_RUN_USAGE="1")) == [
        "NODESTRO_AXENTRA_RUN_USAGE"
    ]
    on = ServerSettings.from_env(
        _env(
            NODESTRO_AXENTRA="on",
            NODESTRO_AXENTRA_KEYRING=ring,
            NODESTRO_AXENTRA_USAGE="1",
            NODESTRO_AXENTRA_RUN_USAGE="1",
            NODESTRO_AXENTRA_NOTIFY="1",
        )
    ).axentra
    assert on.run_usage and on.notify
    assert axentra_platform.modules(on)[-1] == "notify" and "notify" not in axentra_platform.modules(config)
    settings = axentra_platform.build_settings(
        on, database_url="postgresql://x@db/x", public_origin="https://s.example"
    )
    assert "notify" in settings.modules and settings.notify.provider == "local"


def test_the_metrics_token_is_optional_and_checked() -> None:
    problems: list[dict[str, str]] = []
    assert not axentra_metrics.MetricsConfig.from_env({}, problems).enabled and problems == []
    axentra_metrics.MetricsConfig.from_env({"NODESTRO_METRICS_TOKEN": "short"}, problems)
    assert [item["field"] for item in problems] == ["NODESTRO_METRICS_TOKEN"]
    token = "t" * 40
    config = axentra_metrics.MetricsConfig.from_env({"NODESTRO_METRICS_TOKEN": token}, problems := [])
    assert config.enabled and problems == [] and token not in repr(config)
    assert config.authorized(f"Bearer {token}") and config.authorized(f"bearer {token}")
    assert not config.authorized(None) and not config.authorized("Bearer wrong") and not config.authorized(token)


def test_operation_counts_are_bounded() -> None:
    before = axentra_metrics.operation_counts()
    axentra_metrics.count_operation("secret_decrypt", "ok", format="axs1")
    axentra_metrics.count_operation("secret_decrypt", "ok", format="tenant-acme")  # never a label value of its own
    axentra_metrics.count_operation("made_up", "ok", kind="x")
    after = axentra_metrics.operation_counts()
    assert after[("secret_decrypt", "ok", "axs1")] == before.get(("secret_decrypt", "ok", "axs1"), 0) + 1
    assert ("secret_decrypt", "ok", "other") in after and ("other", "ok", "") in after
    assert not any("acme" in part for key in after for part in key)


# ---- PostgreSQL ------------------------------------------------------------------------------------------------


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


def _services(database: Any, **config: Any) -> Any:
    values: dict[str, Any] = {"enabled": True, "keyring": f"k1:{_key()}", "allow_superuser": True, **config}
    services = axentra_platform.AxentraServices(
        AxentraConfig(**values),
        database_url=postgres_support.DSN,
        public_origin="http://localhost:8000",
        schema=database.test_schema,
    )
    services.migrate()
    services.start()
    return services


@pytest.fixture
def runs(database: Any) -> Iterator[tuple[Any, AxentraEvents]]:
    from identity_support import create_tenants

    create_tenants(database, [("alpha", "Alpha"), ("beta", "Beta")])
    services = _services(database, usage=True, run_usage=True)
    events = axentra_platform.attach_platform_events(database, axentra_platform.AxentraLink(services, database))
    assert events is not None and events.run_usage_enabled
    try:
        yield database, events
    finally:
        database.platform_events = None
        services.close()


def _usage(requests: int, cost: float | None, *, complete: bool | None = True) -> dict[str, Any]:
    return {
        "requests": requests,
        "input_tokens": requests * 100,
        "output_tokens": requests * 10,
        "estimated_cost_usd": cost,
        "cost_complete": complete,
    }


def _run(database: Any, tenant: str, run_id: str, status: str, usage: dict[str, Any], **extra: Any) -> None:
    finished = extra.pop("finished_at", now())
    document = {
        "id": run_id,
        "workflow_id": "wf_1",
        "workflow_name": "Check",
        "status": status,
        "usage": usage,
        "trigger": "manual",
        "finished_at": finished.isoformat() if status in ("succeeded", "failed", "cancelled") else None,
        **extra,
    }
    database.store(tenant).put_raw("runs", run_id, document)


def _owner(database: Any) -> Any:
    """The table owner's view: Axentra's tables have forced row-level security, so a check that reads them across
    tenants (no Axentra scope) runs as the owner (``NODESTRO_TEST_RUNTIME_ROLE=1``; otherwise ``database`` itself)."""
    return postgres_support.owner_of(database)


def _totals(database: Any, prefix: str = "nodestro:run:") -> dict[str, Decimal]:
    rows = _owner(database)._query(
        "SELECT m.key, sum(e.quantity) FROM axentra_usage_events e JOIN axentra_usage_meters m ON m.id = e.meter_id"
        " WHERE e.idempotency_key LIKE %s GROUP BY m.key",
        (prefix + "%",),
    )
    return {str(key): Decimal(value) for key, value in rows}


def _seed_runs(database: Any) -> None:
    _run(database, "alpha", "run_ok", "succeeded", _usage(2, 0.02))
    _run(database, "alpha", "run_fail", "failed", _usage(1, 0.5, complete=False))  # partial cost: not mirrored
    _run(database, "alpha", "run_busy", "running", _usage(5, 1.0))  # not finished: not settled
    _run(database, "alpha", "run_child", "succeeded", _usage(7, 1.0), parent_run_id="run_ok")  # counted in parent
    _run(database, "alpha", "run_import", "succeeded", _usage(9, 1.0), imported_at=now().isoformat())
    _run(database, "alpha", "run_none", "cancelled", _usage(0, 0.0))  # nothing to count
    _run(database, "beta", "run_api", "succeeded", _usage(3, 0.03), trigger="api")


def test_finished_runs_are_settled_once_per_run_and_meter(runs: tuple[Any, AxentraEvents]) -> None:
    database, events = runs
    _seed_runs(database)
    backlog = reconciliation_backlog(events.link.services)
    assert backlog["runs"] == 3
    dry = events.settle_runs(dry_run=True)
    assert (dry["missing"], dry["written"]) == (3, 0) and _totals(database) == {}
    report = events.settle_runs()
    assert (report["missing"], report["written"], report["failed"]) == (3, 3, 0)
    totals = _totals(database)
    assert totals == {
        "nodestro.ai.requests": 6,
        "nodestro.ai.input_tokens": 600,
        "nodestro.ai.output_tokens": 60,
        "nodestro.ai.cost_usd": Decimal("0.05"),
    }
    assert reconciliation_backlog(events.link.services)["runs"] == 0
    # Settling again, reconciling twice: nothing is missing, nothing is counted again.
    assert events.settle_runs(full=True)["missing"] == 0
    first, second = events.reconcile(), events.reconcile()
    assert (first["runs_missing"], second["runs_missing"], second["written"]) == (0, 0, 0)
    assert _totals(database) == totals
    keys = {row[0] for row in _owner(database)._query("SELECT idempotency_key FROM axentra_usage_events")}
    assert run_usage_key("run_ok", "nodestro.ai.requests") in keys
    assert not any("run_child" in key or "run_import" in key or "run_busy" in key for key in keys)
    props = _owner(database)._query(
        "SELECT properties FROM axentra_usage_events WHERE idempotency_key = %s",
        (run_usage_key("run_api", "nodestro.ai.requests"),),
    )
    assert props == [({"source": "api_run", "status": "succeeded", "workflow_id": "wf_1"},)]


def test_a_crash_after_the_write_and_a_replay_count_nothing_twice(runs: tuple[Any, AxentraEvents]) -> None:
    database, events = runs
    _run(database, "alpha", "run_ok", "succeeded", _usage(2, 0.02))
    (run,) = axentra_platform.settled_runs(database, "alpha")
    # The write commits, then the process dies before it notes anything (no watermark, no stats).
    assert events.settle_run("alpha", run) == "ok"
    once = _totals(database)
    # The settlement is replayed by the same process (it did not see its own ack) ...
    assert events.settle_run("alpha", run) == "ok"
    # ... and by a fresh process (another worker, a restart): it finds the run mirrored, or replays the keys.
    fresh = AxentraEvents(events.link, audit=False, usage=True, run_usage=True)
    fresh.ensure_meters()
    assert fresh.settle_runs()["missing"] == 0
    assert fresh.settle_run("alpha", run) == "ok"
    assert _totals(database) == once and once["nodestro.ai.requests"] == 2


def test_a_resumed_run_counts_only_its_own_usage(runs: tuple[Any, AxentraEvents]) -> None:
    database, events = runs
    _run(database, "alpha", "run_1", "failed", _usage(2, 0.02))
    events.settle_runs()
    # Continuing the failed run creates a new run; reused steps count no cost there (nodestro_runtime.resume).
    _run(database, "alpha", "run_2", "succeeded", _usage(1, 0.01), resumed_from="run_1", trigger="resume")
    assert events.settle_runs()["written"] == 1
    assert _totals(database)["nodestro.ai.requests"] == 3


def test_a_finished_run_whose_usage_changed_is_reported_not_counted_again(runs: tuple[Any, AxentraEvents]) -> None:
    database, events = runs
    _run(database, "alpha", "run_ok", "succeeded", _usage(2, 0.02))
    events.settle_runs()
    before = _totals(database)
    _run(database, "alpha", "run_ok", "succeeded", _usage(4, 0.04))  # must not happen; if it does, never twice
    report = events.reconcile()
    assert report["runs_changed"] == 1 and report["runs_missing"] == 0
    assert _totals(database) == before and events.stats.changed == 1


def test_concurrent_settlements_count_each_run_once(runs: tuple[Any, AxentraEvents]) -> None:
    database, events = runs
    for index in range(8):
        _run(database, "alpha", f"run_{index}", "succeeded", _usage(1, 0.01))
    others = [AxentraEvents(events.link, audit=False, usage=True, run_usage=True) for _ in range(3)]
    for other in others:
        other.ensure_meters()
    errors: list[BaseException] = []

    def settle(target: AxentraEvents) -> None:
        try:
            target.settle_runs()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=settle, args=(target,)) for target in others]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert _totals(database)["nodestro.ai.requests"] == 8


def test_runs_axentra_no_longer_accepts_are_reported(runs: tuple[Any, AxentraEvents]) -> None:
    database, events = runs
    _run(database, "alpha", "run_old", "succeeded", _usage(1, 0.01), finished_at=now() - timedelta(days=400))
    report = events.settle_runs(full=True)
    assert (report["too_old"], report["written"]) == (1, 0) and _totals(database) == {}


def test_run_usage_is_not_settled_while_its_switch_is_off(database: Any) -> None:
    from identity_support import create_tenants

    create_tenants(database, [("alpha", "Alpha")])
    services = _services(database, usage=True)
    try:
        events = axentra_platform.attach_platform_events(database, axentra_platform.AxentraLink(services, database))
        assert events is not None and not events.run_usage_enabled
        _run(database, "alpha", "run_ok", "succeeded", _usage(2, 0.02))
        assert events.settle_runs()["runs"] == 0 and events.reconcile()["runs_missing"] == 0
        assert _totals(database) == {}
    finally:
        database.platform_events = None
        services.close()


# ---- secrets rollback --------------------------------------------------------------------------------------------


def _ring(*kids: str) -> tuple[str, dict[str, str]]:
    keys = {kid: f"{kid}:{_key()}" for kid in kids}
    return ",".join(keys.values()), keys


def _keyring_box(ring: str, legacy: SecretBox, write: str = "axs1") -> Any:
    return axentra_platform.keyring_secret_box(
        AxentraConfig(enabled=True, keyring=ring, secrets_format=write),  # type: ignore[arg-type]
        legacy,
    )


def _seed_secrets(database: Any, box: Any) -> None:
    store = database.store("alpha")
    store.put_raw(
        "settings",
        "app",
        {"providers": {"openai": {"api_key": box.encrypt("alpha", "providers.openai.api_key", "sk-1")}}},
    )
    store.put_raw("mcp_servers", "srv1", {"id": "srv1", "token_sealed": box.encrypt("alpha", "mcp:srv1", "tok")})
    store.put_raw("workflows", "w1", {"note": "axs1.k9.not.secret"})  # not a secret field: never counted


def test_stored_secret_formats_and_the_rollback_checks(database: Any) -> None:
    legacy = SecretBox(b"L" * 32)
    assert stored_secret_formats(database) == {}
    database.store("beta").put_raw(
        "pricing_billing", "openai", {"admin_key": legacy.encrypt("beta", "pricing.billing.openai.admin_key", "a")}
    )
    ring, keys = _ring("k1")
    _seed_secrets(database, _keyring_box(ring, legacy))
    formats = stored_secret_formats(database)
    assert formats == {"axs1:k1": 2, "v1": 1}

    off = AxentraConfig()
    (problem,) = secrets_rollback_problems(off, formats)
    assert problem["field"] == "NODESTRO_AXENTRA" and "2 stored tenant secrets" in problem["reason"]
    assert keys["k1"].split(":", 1)[1] not in str(problem)
    assert secrets_rollback_problems(AxentraConfig(enabled=True, keyring=ring, secrets_format="v1"), formats) == []
    other, _ = _ring("k2")
    (missing,) = secrets_rollback_problems(AxentraConfig(enabled=True, keyring=other), formats)
    assert (
        missing["field"] == "NODESTRO_AXENTRA_KEYRING"
        and missing["value"] == "k1"
        and "k1 (2 values)" in missing["reason"]
    )
    assert secrets_rollback_problems(AxentraConfig(enabled=True, keyring=f"{other},{ring}"), formats) == []
    assert secrets_rollback_problems(off, {"v1": 3}) == []
    with pytest.raises(NodestroError) as caught:
        axentra_platform.require_readable_secrets(database, off)
    assert caught.value.code == "profile.invalid"


def test_the_start_refuses_a_key_with_other_bytes_under_a_known_key_id(database: Any) -> None:
    """SR-04: the start compared key ids only; new bytes under the old id "k1" started without an error and made every
    axs1 value unreadable. Now a few stored values per key are decrypted, so the key bytes are compared."""
    legacy = SecretBox(b"L" * 32)
    ring, _keys = _ring("k1")
    _seed_secrets(database, _keyring_box(ring, legacy))
    database.store("beta").put_raw(
        "pricing_billing", "openai", {"admin_key": legacy.encrypt("beta", "pricing.billing.openai.admin_key", "a")}
    )
    config = AxentraConfig(enabled=True, keyring=ring)
    axentra_platform.require_readable_secrets(database, config, _keyring_box(ring, legacy))  # the right keys
    assert axentra_platform.secret_key_problems(database, _keyring_box(ring, legacy)) == []

    impostor, _ = _ring("k1")  # same key id, other bytes
    assert impostor.split(":", 1)[0] == "k1" and impostor != ring
    impostor_config = AxentraConfig(enabled=True, keyring=impostor)
    assert secrets_rollback_problems(impostor_config, stored_secret_formats(database)) == []  # ids alone: passes
    with pytest.raises(NodestroError) as caught:
        axentra_platform.require_readable_secrets(database, impostor_config, _keyring_box(impostor, legacy))
    (problem,) = caught.value.params["problems"]
    assert (problem["field"], problem["value"]) == ("NODESTRO_AXENTRA_KEYRING", "k1")
    assert "alpha/" in problem["reason"] and impostor.split(":", 1)[1] not in str(caught.value.params)
    assert "sk-1" not in str(caught.value.params)

    # Another v1 key (NODESTRO_SECRET_KEY changed) is refused the same way, with Axentra on or off.
    other_legacy = SecretBox(b"M" * 32)
    (v1,) = axentra_platform.secret_key_problems(database, _keyring_box(ring, other_legacy))
    assert (v1["field"], v1["value"]) == ("NODESTRO_SECRET_KEY", "v1") and "beta/pricing_billing/openai" in v1["reason"]
    (v1_off,) = axentra_platform.secret_key_problems(database, other_legacy)
    assert v1_off["field"] == "NODESTRO_SECRET_KEY"

    # One damaged value does not stop the start while another value proves the key.
    store = database.store("alpha")
    store.put_raw("mcp_servers", "srv0", {"id": "srv0", "token_sealed": "axs1.k1.AAAA.BBBB"})
    assert axentra_platform.secret_key_problems(database, _keyring_box(ring, legacy)) == []


def test_reencrypting_with_v1_moves_axs1_values_back_so_axentra_can_be_switched_off(database: Any) -> None:
    legacy = SecretBox(b"L" * 32)
    ring, _ = _ring("k1")
    _seed_secrets(database, _keyring_box(ring, legacy))
    back = _keyring_box(ring, legacy, write="v1")
    dry = reencrypt_tenant_secrets(database, back, dry_run=True)
    assert (dry.pending, dry.reencrypted, dry.active_kid) == (2, 0, "v1")
    report = reencrypt_tenant_secrets(database, back)
    assert (report.reencrypted, report.failed) == (2, [])
    assert stored_secret_formats(database) == {"v1": 2}
    axentra_platform.require_readable_secrets(database, AxentraConfig())  # off is safe now
    settings = database.store("alpha").get_raw("settings", "app")
    assert legacy.decrypt("alpha", "providers.openai.api_key", settings["providers"]["openai"]["api_key"]) == "sk-1"
    assert reencrypt_tenant_secrets(database, back).pending == 0  # idempotent


def test_an_unreadable_secret_is_never_overwritten_by_a_settings_save(database: Any, tmp_path: Any) -> None:
    from nodestro_runtime.postgres_tables import tenant_storage
    from nodestro_runtime.schemas import SettingsIn
    from nodestro_runtime.workspace import Workspace

    legacy = SecretBox(b"L" * 32)
    ring, _ = _ring("k1")
    _seed_secrets(database, _keyring_box(ring, legacy))
    stored = database.store("alpha").get_raw("settings", "app")["providers"]["openai"]["api_key"]
    other, _ = _ring("k2")  # the k1 key is gone: the stored value cannot be read
    box = _keyring_box(other, legacy)
    workspace = Workspace(tmp_path / "alpha", integrations=[], storage=tenant_storage(database, "alpha", secrets=box))
    try:
        assert "api_key" not in workspace._providers().get("openai", {})  # shown as missing, kept as stored
        workspace.update_settings(SettingsIn.model_validate({"providers": {"anthropic": {"api_key": "sk-ant-1"}}}))
        workspace.update_settings(SettingsIn.model_validate({"default_budget_usd": 3.0}))
        after = database.store("alpha").get_raw("settings", "app")["providers"]
        assert after["openai"]["api_key"] == stored  # byte for byte
        report = reencrypt_tenant_secrets(database, box)
        assert sorted(report.failed) == [
            "alpha/mcp_servers/srv1:mcp:srv1",
            "alpha/settings/app:providers.openai.api_key",
        ]
        assert database.store("alpha").get_raw("settings", "app")["providers"]["openai"]["api_key"] == stored
        assert database.store("alpha").get_raw("mcp_servers", "srv1")["token_sealed"].startswith("axs1.k1.")
    finally:
        workspace.close()


def test_decryptions_are_counted_by_format_and_outcome() -> None:
    legacy = SecretBox(b"L" * 32)
    ring, _ = _ring("k1")
    box = _keyring_box(ring, legacy)
    before = axentra_metrics.operation_counts()
    box.decrypt("alpha", "f", box.encrypt("alpha", "f", "v"))
    box.decrypt("alpha", "f", legacy.encrypt("alpha", "f", "v"))
    with pytest.raises(NodestroError):
        box.decrypt("beta", "f", box.encrypt("alpha", "f", "v"))
    after = axentra_metrics.operation_counts()
    for key in (
        ("secret_decrypt", "ok", "axs1"),
        ("secret_decrypt", "ok", "v1"),
        ("secret_decrypt", "unreadable", "axs1"),
    ):
        assert after[key] == before.get(key, 0) + 1


# ---- notifications -----------------------------------------------------------------------------------------------


@pytest.fixture
def notifying(database: Any) -> Iterator[tuple[Any, Any, dict[str, str]]]:
    from axentra.core.context import Principal
    from identity_support import create_tenants

    from nodestro_runtime.axentra_notify import AxentraNotifications

    create_tenants(database, [("alpha", "Alpha")])
    services = _services(database, notify=True)
    link = axentra_platform.AxentraLink(services, database)
    users: dict[str, str] = {}
    for user, role in (
        ("u_owner", "owner"),
        ("u_operator", "operator"),
        ("u_viewer", "viewer"),
        ("u_nolink", "editor"),
    ):
        database._write(
            "INSERT INTO nodestro_users (id, issuer, subject, name) VALUES (%s, 'https://id.example', %s, %s)",
            (user, user, user),
        )
        database._write(
            "INSERT INTO nodestro_memberships (tenant_id, user_id, role) VALUES ('alpha', %s, %s)", (user, role)
        )
        if user == "u_nolink":
            continue
        created = services.platform.identity.create_external_user(
            Principal.system(), provider="https://id.example", subject=user, name=user
        )
        with link.joined() as connection:
            link.link(connection, "user", user, created.id)
        users[user] = created.id
        # The identity backfill gives linked users their Axentra memberships (an inbox is read as a member).
        services.platform.tenancy.set_member(
            Principal.system(),
            services.product_id,
            link.ensure_tenant("alpha", "Alpha"),
            role="member",
            user_id=created.id,
        )
    notifications = AxentraNotifications(link)
    try:
        yield database, notifications, users
    finally:
        services.close()


def test_failed_runs_and_pending_approvals_go_to_the_inbox_once(notifying: tuple[Any, Any, dict[str, str]]) -> None:
    from axentra.core.context import Actor, ActorKind, Principal

    database, notifications, users = notifying
    assert sorted(notifications.ensure_templates()) == [
        "nodestro.approval_pending",
        "nodestro.budget_pending",
        "nodestro.run_failed",
    ]
    assert notifications.ensure_templates() == []  # idempotent
    _run(database, "alpha", "run_bad", "failed", _usage(1, 0.1), error_code="run.interrupted", error="secret output")
    _run(database, "alpha", "run_good", "succeeded", _usage(1, 0.1))
    _run(database, "alpha", "run_sub", "failed", _usage(1, 0.1), parent_run_id="run_good")
    database.store("alpha").put_raw(
        "tool_approvals",
        "run_good:tap_1",
        {"id": "tap_1", "run_id": "run_good", "tool": "send_mail", "status": "pending", "input": {"to": "x@y"}},
    )
    _run(database, "alpha", "run_wait", "paused", _usage(1, 0.1), budget={"pending_request_id": "bud_1"})
    _run(database, "alpha", "run_paused_old", "paused", _usage(1, 0.1))  # no budget field: not matched
    report = notifications.sweep()
    assert report.as_dict() == {
        "sent": 6,
        "replayed": 0,
        "duplicate": 0,
        "no_recipient": 0,
        "failed": 0,
        "events": {"approval_pending": 1, "budget_pending": 1, "run_failed": 1},
    }
    # A retried or resumed worker sweeps the same records again: nothing new.
    again = notifications.sweep(since=now() - timedelta(hours=1))
    assert (again.sent, again.replayed) == (0, 6)
    notify = notifications.link.services.platform.notify
    product = notifications.link.services.product_id
    owner = Principal(actor=Actor(ActorKind.USER, users["u_owner"], "owner"))
    tenant = notifications.link.tenant("alpha")
    inbox = notify.list_inbox(owner, product, tenant).items
    assert sorted(item.subject for item in inbox) == [
        "A run of Check failed",
        "A run of Check waits for a budget decision",
        "send_mail waits for your approval",
    ]
    texts = " ".join(item.text or "" for item in inbox)
    assert "run.interrupted" in texts and "secret output" not in texts and "x@y" not in texts
    viewer = Principal(actor=Actor(ActorKind.USER, users["u_viewer"], "viewer"))
    assert notify.list_inbox(viewer, product, tenant).items == []
    # Nothing was handed to a delivery provider.
    assert notify.dispatcher().run_once(10).claimed == 0
    rows = _owner(database)._query("SELECT DISTINCT channel, status, provider FROM axentra_notify_messages")
    assert rows == [("inbox", "sent", "inbox")]


def test_a_template_switched_to_email_stops_the_notifications(notifying: tuple[Any, Any, dict[str, str]]) -> None:
    from axentra.core.context import Principal

    _, notifications, _ = notifying
    notify = notifications.link.services.platform.notify
    product = notifications.link.services.product_id
    notify.create_template(
        Principal.system(),
        product,
        key="nodestro.run_failed",
        channel="email",
        variables=["workflow", "run_id", "error_code"],
        variants={"en": {"subject": "x", "text": "y"}},
    )
    with pytest.raises(NodestroError) as caught:
        notifications.ensure_templates()
    assert caught.value.code == "axentra.notify_channel"


# ---- metrics -----------------------------------------------------------------------------------------------------


def test_metrics_show_pools_jobs_schema_and_backlog_without_tenant_data(runs: tuple[Any, AxentraEvents]) -> None:
    database, events = runs
    _seed_runs(database)
    database._write(
        "INSERT INTO nodestro_jobs (tenant_id, kind, ref, status) VALUES (%s, %s, %s, %s)",
        ("alpha", "run", "run_busy", "queued"),
    )
    metrics = axentra_metrics.ServerMetrics(database, events.link.services)
    text = metrics.render()
    for line in (
        'nodestro_db_pool_size{pool="nodestro"}',
        'nodestro_db_pool_in_use{pool="axentra"}',
        'nodestro_jobs{kind="run",status="queued"} 1',
        'nodestro_schema_version{runner="nodestro",version="current"}',
        "nodestro_axentra_migrations_pending 0",
        'nodestro_axentra_reconcile_backlog{kind="runs"} 3',
        'nodestro_runs_finished_24h{status="succeeded"} 2',
        'nodestro_axentra_switch{switch="run_usage_mirror"} 1',
        "# TYPE nodestro_integration_operations_total counter",
    ):
        assert line in text, line
    for private in ("alpha", "beta", "run_ok", "wf_1", "Check"):
        assert private not in text
    shared = axentra_metrics.ServerMetrics(
        database, events.link.services, pools=lambda: {"shared": database._pool}
    ).render()
    assert 'pool="shared"' in shared and 'pool="axentra"' not in shared


def test_identity_resolutions_are_counted(database: Any) -> None:
    from nodestro_runtime.identity_store import IdentityStore

    store = axentra_metrics.instrument_identity(IdentityStore(database))
    assert axentra_metrics.instrument_identity(store) is store  # once
    before = axentra_metrics.operation_counts().get(("identity_resolve", "none", ""), 0)
    assert store.session("not-a-token") is None
    assert axentra_metrics.operation_counts()[("identity_resolve", "none", "")] == before + 1
