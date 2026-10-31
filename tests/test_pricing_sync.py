"""Price sources and the sync job (PRICING-1 N3): LiteLLM mapping, downloads, retries, locks, offline fallback."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading
from typing import Any, ClassVar

from orkexa.pricing import MeteredUsage, ModelPrice, PriceSnapshot, TokenRates
import pytest

from nodestro_runtime.pricing import PricingService
from nodestro_runtime.pricing_sync import (
    HttpSettings,
    LiteLLMSource,
    OrkexaBuiltinSource,
    PricingSyncService,
    SnapshotFileSource,
    SourceError,
    fetch_with_retries,
    https_get,
    litellm_price,
)
from nodestro_runtime.providers import PROVIDERS
from nodestro_runtime.storage import SQLiteStore

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

#: A small cost map in LiteLLM's format (the values are test data, not real prices).
COST_MAP: dict[str, Any] = {
    "sample_spec": {"litellm_provider": "one of ...", "input_cost_per_token": 0.0},
    "gpt-test": {
        "litellm_provider": "openai",
        "mode": "chat",
        "input_cost_per_token": 1.25e-06,
        "output_cost_per_token": 1e-05,
        "cache_read_input_token_cost": 1.25e-07,
        "input_cost_per_token_flex": 6.25e-07,
        "output_cost_per_token_flex": 5e-06,
        "input_cost_per_token_batches": 6.25e-07,
        "output_cost_per_token_batches": 5e-06,
        "input_cost_per_token_priority": 2.5e-06,  # no output price for priority: that tier stays unknown
    },
    "gpt-long": {
        "litellm_provider": "openai",
        "mode": "responses",
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 3e-05,
        "input_cost_per_token_above_272k_tokens": 1e-05,
        "output_cost_per_token_above_272k_tokens": 4.5e-05,
        "input_cost_per_token_flex": 2.5e-06,
        "output_cost_per_token_flex": 1.5e-05,  # flex has no long-context rates: dropped
    },
    "gemini/gemini-test": {
        "litellm_provider": "gemini",
        "mode": "chat",
        "input_cost_per_token": 1.25e-06,
        "output_cost_per_token": 1e-05,
    },
    "gemini-test": {"litellm_provider": "vertex_ai-language-models", "mode": "chat", "input_cost_per_token": 9e-06},
    "claude-sonnet-5-5": {
        "litellm_provider": "anthropic",
        "mode": "chat",
        "input_cost_per_token": 9e-06,  # differs from Orkexa's verified price on purpose
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_creation_input_token_cost_above_1hr": 6e-06,
    },
    "dall-e-test": {"litellm_provider": "openai", "mode": "image_generation", "input_cost_per_token": 1e-06},
    "bedrock-model": {
        "litellm_provider": "bedrock",
        "mode": "chat",
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 1e-06,
    },
    "gpt-incomplete": {"litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 1e-06},
}


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _source(document: Any = None, *, fail: int = 0) -> LiteLLMSource:
    calls = {"n": 0}

    def fetch(url: str, settings: HttpSettings) -> bytes:
        calls["n"] += 1
        if calls["n"] <= fail:
            raise SourceError("connection reset")
        return json.dumps(COST_MAP if document is None else document).encode()

    return LiteLLMSource(fetch_bytes=fetch, sleep=lambda _s: None)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def pricing(tmp_path: Path, clock: Clock) -> Iterator[PricingService]:
    store = SQLiteStore(tmp_path / "prices.db")
    yield PricingService(store, clock=clock, known_providers=PROVIDERS)
    store.close()


# ---- LiteLLM -------------------------------------------------------------------------------------------


def test_litellm_entries_become_orkexa_prices() -> None:
    price = litellm_price("gpt-test", COST_MAP["gpt-test"], checked_at=T0)
    assert price is not None
    assert (price.provider, price.model, price.quality, price.source) == ("openai", "gpt-test", "unverified", "litellm")
    assert price.rates.input == Decimal("1.25") and price.rates.output == Decimal("10")
    assert price.rates.cache_read == Decimal("0.125")
    assert [option.value for tariff in price.tariffs for option in tariff.options] == ["default", "flex", "batch"]
    assert str(price.rates.output) == "10"  # plain notation, never "1E+1"


def test_long_context_tiers_and_tariffs_must_fit_together() -> None:
    price = litellm_price("gpt-long", COST_MAP["gpt-long"], checked_at=T0)
    assert price is not None
    assert [tier.above_input_tokens for tier in price.long_context] == [272000]
    assert price.long_context[0].rates.input == Decimal("10")
    assert price.tariffs == ()  # flex without long-context rates is left unpriced, never guessed


def test_litellm_skips_what_nodestro_cannot_price() -> None:
    skip = lambda key: litellm_price(key, COST_MAP[key], checked_at=T0)  # noqa: E731
    assert skip("gemini-test") is None  # Vertex AI
    assert skip("dall-e-test") is None  # images
    assert skip("bedrock-model") is None  # not a Nodestro provider
    assert skip("gpt-incomplete") is None  # no output price
    gemini = litellm_price("gemini/gemini-test", COST_MAP["gemini/gemini-test"], checked_at=T0)
    assert gemini is not None and (gemini.provider, gemini.model) == ("gemini", "gemini-test")
    claude = litellm_price("claude-sonnet-5-5", COST_MAP["claude-sonnet-5-5"], checked_at=T0)
    assert claude is not None and claude.rates.cache_write_1h == Decimal("6")


def test_the_litellm_source_builds_one_snapshot() -> None:
    snapshot = _source().fetch(T0)
    assert snapshot.version == "litellm-20261101T120000Z"
    assert {(p.provider, p.model) for p in snapshot.prices} == {
        ("openai", "gpt-test"),
        ("openai", "gpt-long"),
        ("gemini", "gemini-test"),
        ("anthropic", "claude-sonnet-5-5"),
    }


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", json.dumps({"x": {"mode": "chat"}}).encode()])
def test_broken_cost_maps_are_refused(body: bytes) -> None:
    source = LiteLLMSource(fetch_bytes=lambda _u, _s: body, sleep=lambda _s: None)
    with pytest.raises(SourceError):
        source.fetch(T0)


# ---- downloads -----------------------------------------------------------------------------------------


def test_downloads_retry_with_exponential_backoff() -> None:
    waits: list[float] = []
    attempts = {"n": 0}

    def flaky(url: str, settings: HttpSettings) -> bytes:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise SourceError("timeout")
        return b"{}"

    assert (
        fetch_with_retries("https://example.org/x", HttpSettings(backoff=2), fetch=flaky, sleep=waits.append) == b"{}"
    )
    assert waits == [2, 4]


def test_downloads_give_up_after_the_last_attempt() -> None:
    waits: list[float] = []

    def broken(url: str, settings: HttpSettings) -> bytes:
        raise SourceError("HTTP 503")

    with pytest.raises(SourceError, match="503"):
        fetch_with_retries("https://example.org/x", HttpSettings(attempts=3), fetch=broken, sleep=waits.append)
    assert len(waits) == 2


def test_downloads_only_from_allowed_hosts_and_over_https() -> None:
    with pytest.raises(SourceError, match="not an allowed"):
        fetch_with_retries(
            "https://evil.example/x",
            HttpSettings(),
            fetch=lambda *_: b"",
            allowed_hosts=frozenset({"raw.githubusercontent.com"}),
        )
    with pytest.raises(SourceError, match="HTTPS"):
        https_get("http://raw.githubusercontent.com/x", HttpSettings())


def test_downloads_are_limited_in_size(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        headers: ClassVar[dict[str, str | None]] = {"Content-Length": None}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def geturl(self) -> str:
            return "https://raw.githubusercontent.com/x"

        def read(self, limit: int) -> bytes:
            return b"x" * limit

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: Response())
    with pytest.raises(SourceError, match="larger"):
        https_get("https://raw.githubusercontent.com/x", HttpSettings(max_bytes=10))


# ---- sources from files --------------------------------------------------------------------------------


def test_a_reviewed_snapshot_file_is_a_source(tmp_path: Path) -> None:
    snapshot = PriceSnapshot(
        "reviewed-2026-11",
        (
            ModelPrice(
                provider="openai",
                model="gpt-test",
                rates=TokenRates.of("1", "8"),
                source="openai-price-list",
                quality="verified",
            ),
        ),
    )
    path = tmp_path / "prices.json"
    path.write_text(snapshot.to_json())
    assert SnapshotFileSource(path).fetch(T0) == snapshot
    path.write_text("{}")
    with pytest.raises(SourceError, match="invalid"):
        SnapshotFileSource(path).fetch(T0)
    with pytest.raises(SourceError, match="Cannot read"):
        SnapshotFileSource(tmp_path / "missing.json").fetch(T0)


# ---- the sync job --------------------------------------------------------------------------------------


def test_a_sync_activates_prices_and_records_the_check(pricing: PricingService, clock: Clock) -> None:
    sync = PricingSyncService(pricing, [OrkexaBuiltinSource(), _source()], clock=clock)
    assert sync.due()
    report = sync.sync()
    assert report.ran and all(source.ok for source in report.sources)
    assert pricing.state().last_success_at == T0
    assert not sync.due()
    entry = pricing.entry("openai", "gpt-test")
    assert entry is not None and entry.quality == "unverified" and entry.source == "litellm"
    # LiteLLM's different Anthropic price never replaces Orkexa's verified one.
    claude = pricing.entry("anthropic", "claude-sonnet-5-5")
    assert claude is not None and claude.quality == "verified" and claude.input_usd_per_mtok != "9"
    cost = pricing.resolver().calculate("openai", "gpt-test", MeteredUsage(input_tokens=1_000_000, output_tokens=0))
    assert cost.total == Decimal("1.25")


def test_a_sync_runs_only_when_due_unless_forced(pricing: PricingService, clock: Clock) -> None:
    sync = PricingSyncService(pricing, [_source()], clock=clock)
    sync.sync()
    clock.now = T0 + timedelta(hours=1)
    assert sync.sync().skipped_reason == "not_due"
    assert sync.sync(force=True).ran
    clock.now = T0 + timedelta(hours=26)
    assert sync.due()


def test_a_failing_source_keeps_the_last_valid_prices(pricing: PricingService, clock: Clock) -> None:
    PricingSyncService(pricing, [_source()], clock=clock).sync()
    active = pricing.reference_snapshot().version
    clock.now = T0 + timedelta(days=3)
    report = PricingSyncService(pricing, [_source(fail=99)], clock=clock).sync()
    assert report.ran and not report.sources[0].ok
    assert pricing.reference_snapshot().version == active
    state = pricing.state()
    assert state.last_success_at == T0 and state.last_error is not None and "connection reset" in state.last_error
    assert pricing.entry("openai", "gpt-test").status == "stale"  # type: ignore[union-attr]


def test_a_transient_failure_is_retried(pricing: PricingService, clock: Clock) -> None:
    report = PricingSyncService(pricing, [_source(fail=2)], clock=clock).sync()
    assert report.sources[0].ok


def test_a_broken_source_does_not_stop_the_others(pricing: PricingService, clock: Clock) -> None:
    class Exploding:
        name = "exploding"

        def fetch(self, at: datetime) -> PriceSnapshot:
            raise RuntimeError("bug")

    report = PricingSyncService(pricing, [Exploding(), _source()], clock=clock).sync()
    assert [source.ok for source in report.sources] == [False, True]
    assert pricing.entry("openai", "gpt-test") is not None


def test_only_one_sync_runs_at_a_time(pricing: PricingService, clock: Clock) -> None:
    entered, release = threading.Event(), threading.Event()

    class Slow:
        name = "slow"

        def fetch(self, at: datetime) -> PriceSnapshot:
            entered.set()
            release.wait(5)
            raise SourceError("done")

    sync = PricingSyncService(pricing, [Slow()], clock=clock)
    worker = threading.Thread(target=sync.sync)
    worker.start()
    assert entered.wait(5)
    assert sync.sync(force=True).skipped_reason == "already_running"
    release.set()
    worker.join(5)


def test_another_process_holding_the_lock_skips_the_sync(pricing: PricingService, clock: Clock) -> None:
    @contextmanager
    def held() -> Iterator[bool]:
        yield False

    report = PricingSyncService(pricing, [_source()], lock=held, clock=clock).sync()
    assert report.skipped_reason == "already_running"
    assert pricing.entry("openai", "gpt-test") is None


def test_the_background_loop_syncs_when_due_and_stops(pricing: PricingService, clock: Clock) -> None:
    sync = PricingSyncService(pricing, [_source()], clock=clock)

    async def main() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(sync.run(stop, check_every=timedelta(milliseconds=10)))
        for _ in range(200):
            if pricing.state().last_success_at is not None:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(main())
    assert pricing.state().last_success_at == T0
