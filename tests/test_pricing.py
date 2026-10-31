"""The price catalog (PRICING-1 N1, N2, N4): versioned reference prices, review, overrides, freshness, tenants."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from orkexa.pricing import BUILTIN_SNAPSHOT, MeteredUsage, ModelPrice, PriceSnapshot, TokenRates
import postgres_support
import pytest

from nodestro_runtime import catalog
from nodestro_runtime.pricing import (
    SNAPSHOTS,
    STATE,
    PricingService,
    PricingSettings,
    StoredSnapshot,
    diff_snapshots,
    validate_price,
)
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import PROVIDERS
from nodestro_runtime.storage import DocumentStore, SQLiteStore

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _price(
    model: str,
    input_: str,
    output: str,
    *,
    provider: str = "openai",
    quality: str = "unverified",
    source: str = "litellm",
    **kwargs: object,
) -> ModelPrice:
    return ModelPrice(
        provider=provider,
        model=model,
        rates=TokenRates.of(input_, output),
        source=source,
        quality=quality,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _candidate(version: str, *prices: ModelPrice, source: str = "litellm") -> PriceSnapshot:
    return PriceSnapshot(version, prices, source=source)


def _postgres_pair(cleanup: list[Callable[[], None]]) -> tuple[DocumentStore, DocumentStore, DocumentStore]:
    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    cleanup.append(lambda: postgres_support.drop_schema(schema))
    database = PostgresDatabase(dsn, schema=schema)
    database.migrate()
    cleanup.append(database.close)
    return database.store("reference"), database.store("tenant-a"), database.store("tenant-b")


@pytest.fixture(params=["sqlite", "postgres"])
def stores(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[tuple[DocumentStore, DocumentStore, DocumentStore]]:
    """A reference store and two tenant stores (the same database, separate tenants on PostgreSQL)."""
    cleanup: list[Callable[[], None]] = []
    try:
        if request.param == "sqlite":
            opened = (SQLiteStore(tmp_path / "ref.db"), SQLiteStore(tmp_path / "a.db"), SQLiteStore(tmp_path / "b.db"))
            cleanup.extend(store.close for store in opened)
            yield opened
        else:
            yield _postgres_pair(cleanup)
    finally:
        for step in reversed(cleanup):
            step()


@pytest.fixture
def store(stores: tuple[DocumentStore, DocumentStore, DocumentStore]) -> DocumentStore:
    return stores[0]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(store: DocumentStore, clock: Clock) -> PricingService:
    return PricingService(store, clock=clock, known_providers=PROVIDERS)


# ---- starting point and offline fallback -------------------------------------------------------------


def test_a_new_catalog_uses_orkexas_built_in_prices(service: PricingService, clock: Clock) -> None:
    clock.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert service.reference_snapshot() is BUILTIN_SNAPSHOT
    entry = service.entry("anthropic", "claude-sonnet-5-5")
    assert entry is not None
    assert entry.quality == "verified"
    assert entry.status == "current"
    assert service.entry("openai", "gpt-5.5") is None
    assert service.price_status("openai", "gpt-5.5") == "unknown"


def test_the_active_snapshot_survives_a_restart(store: DocumentStore, service: PricingService, clock: Clock) -> None:
    result = service.submit(_candidate("litellm-1", _price("gpt-5.5", "1.25", "10")))
    assert result.outcome == "activated"
    again = PricingService(store, clock=clock)
    assert again.reference_snapshot().version == result.active_version
    assert again.entry("openai", "gpt-5.5") is not None
    # Anthropic's built-in prices stay: a candidate of one source never removes another source's prices.
    assert again.entry("anthropic", "claude-opus-5-5") is not None


def test_an_unreadable_stored_snapshot_falls_back_to_the_built_in_prices(
    store: DocumentStore, service: PricingService
) -> None:
    result = service.submit(_candidate("litellm-1", _price("gpt-5.5", "1.25", "10")))
    store.put_raw(SNAPSHOTS, result.active_version, {"version": result.active_version, "data": {"format": "x"}})
    assert service.reference_snapshot() is BUILTIN_SNAPSHOT


# ---- validation ------------------------------------------------------------------------------------------


def test_validation_drops_invalid_prices_and_keeps_the_rest(service: PricingService) -> None:
    result = service.submit(
        _candidate(
            "litellm-1",
            _price("gpt-5.5", "1.25", "10"),
            _price("gpt-5.5-mini", "0.25", "2"),
            _price("gpt-euro", "1", "2", currency="EUR"),
        )
    )
    assert result.outcome == "activated"
    assert [issue.code for issue in result.issues if issue.severity == "error"] == ["pricing.currency"]
    assert service.entry("openai", "gpt-euro") is None
    assert service.entry("openai", "gpt-5.5-mini") is not None


def test_validation_checks_ids_providers_and_plausibility() -> None:
    codes = lambda price: {issue.code for issue in validate_price(price, known_providers=PROVIDERS)}  # noqa: E731
    assert "pricing.unknown_provider" in codes(_price("m", "1", "2", provider="bedrock"))
    assert "pricing.implausible_rate" in codes(_price("m", "20000", "2"))
    assert "pricing.zero_price" in codes(_price("m", "0", "0"))
    assert "pricing.cache_read_above_input" in codes(
        ModelPrice(provider="openai", model="m", rates=TokenRates.of("1", "2", "3"), source="x")
    )


def test_a_mostly_invalid_candidate_is_rejected_and_changes_nothing(service: PricingService) -> None:
    active = service.submit(_candidate("litellm-1", _price("gpt-5.5", "1.25", "10"))).active_version
    result = service.submit(
        _candidate(
            "litellm-2",
            _price("a", "1", "2", currency="EUR"),
            _price("b", "1", "2", currency="EUR"),
            _price("gpt-5.5", "1.25", "10"),
        )
    )
    assert result.outcome == "rejected"
    assert result.active_version == active
    assert service.reference_snapshot().version == active
    rejected = service.snapshot("litellm-2")
    assert rejected is not None and rejected.state == "rejected"
    assert service.history()[0].kind == "rejected"


def test_a_truncated_candidate_is_rejected(service: PricingService) -> None:
    prices = [_price(f"model-{n}", "1", "2") for n in range(10)]
    service.submit(_candidate("litellm-1", *prices))
    result = service.submit(_candidate("litellm-2", *prices[:3]))
    assert result.outcome == "rejected"
    assert len([entry for entry in service.entries() if entry.provider == "openai"]) == 10


def test_an_empty_candidate_is_rejected(service: PricingService) -> None:
    assert service.submit(_candidate("litellm-1")).outcome == "rejected"


# ---- changes and review ----------------------------------------------------------------------------------


def test_a_large_unconfirmed_change_waits_for_review(service: PricingService) -> None:
    service.submit(_candidate("litellm-1", _price("gpt-5.5", "1.00", "10"), _price("gpt-5.5-mini", "0.25", "2")))
    result = service.submit(
        _candidate("litellm-2", _price("gpt-5.5", "1.50", "10"), _price("gpt-5.5-mini", "0.26", "2"))
    )
    assert result.outcome == "pending_review"
    assert result.review_version is not None
    # The small change is active, the large one is held back.
    assert service.entry("openai", "gpt-5.5-mini").input_usd_per_mtok == "0.26"  # type: ignore[union-attr]
    held = service.entry("openai", "gpt-5.5")
    assert held is not None and held.input_usd_per_mtok == "1.00" and held.status == "pending_review"
    assert service.status().pending_reviews == [result.review_version]

    approved = service.approve(result.review_version)
    assert approved.outcome == "activated"
    entry = service.entry("openai", "gpt-5.5")
    assert entry is not None and entry.input_usd_per_mtok == "1.50" and entry.status == "current"
    assert service.status().pending_reviews == []
    with pytest.raises(NodestroError) as caught:
        service.approve(result.review_version)
    assert caught.value.code == "pricing.review_not_found"


def test_a_rejected_review_keeps_the_old_price(service: PricingService) -> None:
    service.submit(_candidate("litellm-1", _price("gpt-5.5", "1.00", "10")))
    result = service.submit(_candidate("litellm-2", _price("gpt-5.5", "2.00", "10")))
    assert result.review_version is not None
    service.reject(result.review_version, note="LiteLLM typo")
    assert service.entry("openai", "gpt-5.5").input_usd_per_mtok == "1.00"  # type: ignore[union-attr]
    kinds = [event.kind for event in service.history()]
    assert kinds[0] == "review_rejected"


def test_an_officially_confirmed_change_is_activated_directly(service: PricingService) -> None:
    service.submit(
        _candidate("official-1", _price("gpt-5.5", "1.00", "10", quality="verified", source="openai-official"))
    )
    result = service.submit(
        _candidate("official-2", _price("gpt-5.5", "2.00", "10", quality="verified", source="openai-official"))
    )
    assert result.outcome == "activated"
    assert service.entry("openai", "gpt-5.5").input_usd_per_mtok == "2.00"  # type: ignore[union-attr]


def test_a_vanished_model_needs_review_but_another_sources_models_stay(service: PricingService) -> None:
    service.submit(_candidate("litellm-1", *(_price(f"m-{n}", "1", "2") for n in range(4))))
    result = service.submit(_candidate("litellm-2", *(_price(f"m-{n}", "1", "2") for n in range(3))))
    assert result.outcome == "pending_review"
    assert [(c.model, c.kind) for c in result.changes] == [("m-3", "removed")]
    assert service.entry("openai", "m-3") is not None  # still priced until the review says otherwise
    assert result.review_version is not None
    service.approve(result.review_version)
    assert service.entry("openai", "m-3") is None
    assert service.entry("anthropic", "claude-opus-5-5") is not None


def test_diff_marks_changes_to_unknown_rates_for_review() -> None:
    active = PriceSnapshot(
        "a", (ModelPrice(provider="openai", model="m", rates=TokenRates.of("1", "2", "0.1"), source="s"),)
    )
    candidate = [ModelPrice(provider="openai", model="m", rates=TokenRates.of("1", "2"), source="s")]
    [change] = diff_snapshots(active, candidate, review_change_ratio=Decimal("0.2"), sources={"s"})
    assert (change.field, change.old, change.new, change.needs_review) == ("cache_read", "0.1", None, True)


def test_old_versions_are_kept(service: PricingService) -> None:
    first = service.submit(_candidate("litellm-1", _price("gpt-5.5", "1.00", "10"))).active_version
    second = service.submit(_candidate("litellm-2", _price("gpt-5.5", "1.10", "10"))).active_version
    assert first != second
    old = service.snapshot(first)
    assert old is not None and old.state == "superseded"
    assert old.snapshot().get("openai", "gpt-5.5").rates.input == Decimal("1.00")  # type: ignore[union-attr]


def test_the_same_version_name_with_other_content_gets_a_new_version(service: PricingService) -> None:
    first = service.submit(_candidate("litellm", _price("gpt-5.5", "1.00", "10"))).active_version
    second = service.submit(_candidate("litellm", _price("gpt-5.5", "1.05", "10"))).active_version
    assert first == "litellm" and second.startswith("litellm+")


# ---- freshness -------------------------------------------------------------------------------------------


def test_prices_age_and_a_successful_check_refreshes_them(service: PricingService, clock: Clock) -> None:
    service.submit(_candidate("litellm-1", _price("gpt-5.5", "1.25", "10")))
    assert service.entry("openai", "gpt-5.5").status == "current"  # type: ignore[union-attr]
    clock.now = T0 + timedelta(hours=49)
    entry = service.entry("openai", "gpt-5.5")
    assert entry is not None and (entry.status, entry.staleness) == ("stale", "warning")
    clock.now = T0 + timedelta(days=8)
    assert service.entry("openai", "gpt-5.5").staleness == "critical"  # type: ignore[union-attr]
    assert service.status().staleness == "critical"
    # The same prices again: nothing new is activated, but the check counts.
    result = service.submit(_candidate("litellm-2", _price("gpt-5.5", "1.25", "10")))
    assert result.outcome == "unchanged"
    entry = service.entry("openai", "gpt-5.5")
    assert entry is not None and (entry.status, entry.staleness) == ("current", "ok")


def test_thresholds_are_configurable(store: DocumentStore, clock: Clock) -> None:
    service = PricingService(store, clock=clock, settings=PricingSettings(warn_after=timedelta(hours=1)))
    service.submit(_candidate("litellm-1", _price("gpt-5.5", "1.25", "10")))
    clock.now = T0 + timedelta(hours=2)
    assert service.entry("openai", "gpt-5.5").status == "stale"  # type: ignore[union-attr]


def test_sync_attempts_are_recorded(service: PricingService, store: DocumentStore) -> None:
    service.record_attempt(error="timeout")
    state = service.state()
    assert state.last_error == "timeout" and state.last_success_at is None
    service.record_attempt()
    assert service.state().last_success_at == T0 and service.state().last_error is None
    assert store.get_raw(STATE, "reference") is not None


# ---- overrides and the resolver --------------------------------------------------------------------------


def test_an_override_wins_and_can_be_reset(service: PricingService) -> None:
    resolver = service.resolver()
    usage = MeteredUsage(input_tokens=1_000_000, output_tokens=0)
    reference = resolver.calculate("anthropic", "claude-sonnet-5-5", usage).total
    service.set_override(
        _price("claude-sonnet-5-5", "1", "5", provider="anthropic", quality="contract", source="acme-contract"),
        note="Enterprise discount",
    )
    entry = service.entry("anthropic", "claude-sonnet-5-5")
    assert entry is not None and entry.status == "custom" and entry.overridden
    assert entry.reference is not None and entry.reference.quality == "verified"
    assert service.resolver() is resolver  # the same instance, republished
    assert resolver.calculate("anthropic", "claude-sonnet-5-5", usage).total == Decimal("1")

    assert service.remove_override("anthropic", "claude-sonnet-5-5")
    assert not service.remove_override("anthropic", "claude-sonnet-5-5")
    assert service.resolver().calculate("anthropic", "claude-sonnet-5-5", usage).total == reference
    assert [event.kind for event in service.history()][:2] == ["override_removed", "override_set"]


def test_an_override_prices_a_model_no_source_knows(service: PricingService) -> None:
    service.set_override(_price("my-finetune", "3", "12", source="custom"))
    assert service.price_status("openai", "my-finetune") == "custom"
    cost = service.resolver().calculate("openai", "my-finetune", MeteredUsage(input_tokens=1000, output_tokens=1000))
    assert cost.total == Decimal("0.015")


def test_an_invalid_override_is_refused(service: PricingService) -> None:
    with pytest.raises(NodestroError) as caught:
        service.set_override(_price("m", "1", "2", currency="EUR"))
    assert caught.value.code == "pricing.invalid_price"


def test_unknown_costs_stay_unknown(service: PricingService) -> None:
    cost = service.resolver().calculate("openai", "gpt-unknown", MeteredUsage(input_tokens=10, output_tokens=10))
    assert cost.total is None


def test_the_catalog_shows_prices_and_their_status(service: PricingService) -> None:
    service.set_override(_price("gpt-5.5", "1.25", "10", source="custom"))
    by_id = {info.id: info for info in catalog.models(service)}
    assert by_id["openai:gpt-5.5"].price_status == "custom"
    assert by_id["openai:gpt-5.5"].input_usd_per_mtok == 1.25
    unknown = by_id["gemini:gemini-3-pro"]
    assert unknown.price_status == "unknown" and unknown.input_usd_per_mtok is None
    claude = by_id["anthropic:claude-opus-5-5"]
    assert claude.price_quality == "verified" and claude.price_source
    # Without a service: Orkexa's built-in prices.
    assert {info.id: info for info in catalog.models()}["anthropic:claude-opus-5-5"].price_status == "current"


# ---- tenants ---------------------------------------------------------------------------------------------


def test_tenants_share_reference_prices_but_not_overrides(
    stores: tuple[DocumentStore, DocumentStore, DocumentStore], clock: Clock
) -> None:
    reference, store_a, store_b = stores
    sync = PricingService(reference, clock=clock)
    sync.submit(_candidate("litellm-1", _price("gpt-5.5", "1.25", "10")))
    tenant_a = PricingService(store_a, reference_store=reference, reference_writable=False, clock=clock)
    tenant_b = PricingService(store_b, reference_store=reference, reference_writable=False, clock=clock)
    assert tenant_a.entry("openai", "gpt-5.5") is not None
    assert tenant_b.entry("openai", "gpt-5.5") is not None

    tenant_a.set_override(_price("gpt-5.5", "0.5", "4", source="a-contract", quality="contract"))
    assert tenant_a.price_status("openai", "gpt-5.5") == "custom"
    assert tenant_b.price_status("openai", "gpt-5.5") == "current"
    assert sync.price_status("openai", "gpt-5.5") == "current"
    assert [event.kind for event in tenant_b.history()] == ["activated"]
    assert "override_set" in [event.kind for event in tenant_a.history()]

    with pytest.raises(NodestroError) as caught:
        tenant_a.submit(_candidate("evil", _price("gpt-5.5", "0", "0")))
    assert caught.value.code == "pricing.reference_read_only"


def test_stored_snapshots_use_orkexas_format(service: PricingService) -> None:
    version = service.submit(_candidate("litellm-1", _price("gpt-5.5", "1.25", "10"))).active_version
    record = service.snapshot(version)
    assert isinstance(record, StoredSnapshot)
    assert record.data["format"] == "orkexa.price-snapshot/1"
    assert record.content_hash == record.snapshot().content_hash()
