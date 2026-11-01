"""Amazon Bedrock prices (PRICING-1): its own price source, regional tariffs, strict mode, LiteLLM, overrides.

Bedrock requests run against a real ``bedrock-runtime`` client behind ``botocore.stub.Stubber`` with fake credentials:
nothing reaches the network or AWS. Orkexa records every Bedrock request with the tariff condition ``aws_region`` (the
client's region), so a price has to list the regions it covers.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import boto3
from botocore.stub import Stubber
from orkexa import Agent
from orkexa.dynamic import UnpricedModelError
from orkexa.pricing import ModelPrice, Tariff, TariffOption, TokenRates
import pytest

from nodestro_runtime.cost_tracking import PricedTracker
from nodestro_runtime.pricing import PricingService, missing_conditions, validate_price
from nodestro_runtime.pricing_sync import (
    HttpSettings,
    LiteLLMSource,
    PricingSyncService,
    bedrock_prices,
    litellm_bedrock_rate,
    litellm_price,
)
from nodestro_runtime.problems import NodestroError, library_problem
from nodestro_runtime.providers import PROVIDERS
from nodestro_runtime.reconciliation import (
    BilledLine,
    CostReconciliationService,
    Estimate,
    ProviderReport,
)
from nodestro_runtime.storage import SQLiteStore

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
CLAUDE = "anthropic.claude-sonnet-5-5"
EU_PROFILE = "eu.anthropic.claude-sonnet-5-5"
ARN = "arn:aws:bedrock:eu-central-1:123456789012:application-inference-profile/abc123"
#: ``bedrock`` joins ``providers.PROVIDERS`` with the Bedrock provider itself; the price checks need it known.
KNOWN = frozenset({*PROVIDERS, "bedrock"})
#: 1,000 uncached input, 200 output, 500 cache-read and 100 cache-write (5 min) tokens.
USAGE = {"inputTokens": 1000, "outputTokens": 200, "totalTokens": 1800, "cacheReadInputTokens": 500,
         "cacheWriteInputTokens": 100}  # fmt: skip
#: USAGE at eu-central-1's rates of :func:`_price` (3 / 15 / 0.30 / 3.75 USD per million tokens).
EU_CENTRAL_COST = Decimal("0.006525")
#: USAGE at eu-west-1's own rates (4 / 20 / 0.40 / 5).
EU_WEST_COST = Decimal("0.0087")


@pytest.fixture
def pricing(tmp_path: Path) -> Iterator[PricingService]:
    store = SQLiteStore(tmp_path / "prices.db")
    yield PricingService(store, clock=lambda: T0, known_providers=KNOWN)
    store.close()


def _price(model: str = EU_PROFILE, **kwargs: Any) -> ModelPrice:
    """A Bedrock price for eu-central-1 and eu-west-1 (own rates); values are test data, not AWS prices."""
    return ModelPrice(
        provider="bedrock",
        model=model,
        rates=TokenRates.of("3", "15", "0.30", cache_write_5m="3.75"),
        tariffs=(
            Tariff(
                "aws_region",
                "eu-central-1",
                (
                    TariffOption("eu-central-1"),
                    TariffOption("eu-west-1", rates=TokenRates.of("4", "20", "0.40", cache_write_5m="5")),
                ),
            ),
        ),
        source="acme-aws-contract",
        quality="contract",
        **kwargs,
    )


def _response() -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        "stopReason": "end_turn",
        "usage": USAGE,
        "metrics": {"latencyMs": 12},
        "ResponseMetadata": {"RequestId": "req-1", "HTTPStatusCode": 200},
    }


def _agent(tracker: PricedTracker, *, model: str = EU_PROFILE, region: str = "eu-central-1") -> tuple[Agent, Stubber]:
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        aws_access_key_id="AKIDEXAMPLE",
        aws_secret_access_key="fake-secret-for-tests",
    )
    stubber = Stubber(client)
    stubber.add_response("converse", _response())
    stubber.activate()
    return Agent(model=f"bedrock:{model}", client=client, usage_tracker=tracker), stubber


def _region(region: str | None) -> Any:
    return lambda provider: {"aws_region": region} if provider == "bedrock" and region else {}


# ---- validation and overrides -------------------------------------------------------------------------------


def test_a_bedrock_price_must_name_its_regions(pricing: PricingService) -> None:
    regionless = ModelPrice(provider="bedrock", model=CLAUDE, rates=TokenRates.of("3", "15"), source="x")
    assert [issue.code for issue in validate_price(regionless)] == ["pricing.region_required"]
    wrong = ModelPrice(
        provider="bedrock",
        model=CLAUDE,
        rates=TokenRates.of("3", "15"),
        tariffs=(Tariff.of("aws_region", "eu-central-1", unknown="1"),),  # "unknown" is what Orkexa records without one
        source="x",
    )
    assert [issue.code for issue in validate_price(wrong)] == ["pricing.invalid_region"]
    with pytest.raises(NodestroError) as refused:
        pricing.set_override(regionless)
    assert refused.value.code == "pricing.invalid_price"
    assert validate_price(_price()) == []
    # Other providers keep their rules: no region needed.
    assert validate_price(ModelPrice(provider="openai", model="gpt-x", rates=TokenRates.of("1", "2"), source="x")) == []


def test_overrides_distinguish_model_ids_profiles_and_arns(pricing: PricingService) -> None:
    pricing.set_override(_price(aliases=(ARN,)))
    snapshot = pricing.effective_snapshot()
    assert snapshot.lookup("bedrock", EU_PROFILE).found
    assert snapshot.lookup("bedrock", ARN).found  # an ARN of the same profile, as an explicit alias
    assert not snapshot.lookup("bedrock", CLAUDE).found  # the in-region model id is another price
    assert not snapshot.lookup("bedrock", "global.anthropic.claude-sonnet-5-5").found
    # Model ids with colons are stored and removed by their full id.
    dated = "anthropic.claude-3-5-sonnet-20240620-v1:0"
    pricing.set_override(_price(dated))
    assert pricing.entry("bedrock", dated) is not None
    assert pricing.remove_override("bedrock", dated)
    assert pricing.entry("bedrock", dated) is None


def test_anthropic_list_prices_never_price_claude_on_bedrock(pricing: PricingService) -> None:
    # Orkexa's built-in snapshot prices Anthropic's API; Bedrock has no price until one is registered.
    assert pricing.entry("anthropic", "claude-sonnet-5-5") is not None
    for model in (CLAUDE, EU_PROFILE, "claude-sonnet-5-5"):
        assert pricing.entry("bedrock", model) is None
    tracker = PricedTracker(pricing=pricing)
    agent, _ = _agent(tracker)
    agent.ask("hi")
    (record,) = tracker.records
    assert record.provider == "bedrock" and record.estimated_cost_usd is None  # unknown, never 0


def test_missing_conditions_mirrors_orkexa() -> None:
    price = _price()
    assert missing_conditions(price, {"aws_region": "eu-central-1"}) == []
    assert missing_conditions(price, {"aws_region": "eu-west-1"}) == []
    assert missing_conditions(price, {"aws_region": "eu-west-3"}) == ["tariff:aws_region=eu-west-3"]
    assert missing_conditions(price, {"service_tier": "default"}) == []  # neutral
    assert missing_conditions(price, {"service_tier": "priority"}) == ["tariff:service_tier=priority"]


# ---- costs of Bedrock requests ------------------------------------------------------------------------------


@pytest.mark.parametrize(("region", "cost"), [("eu-central-1", EU_CENTRAL_COST), ("eu-west-1", EU_WEST_COST)])
def test_an_override_prices_its_regions_with_cache_tokens(pricing: PricingService, region: str, cost: Decimal) -> None:
    pricing.set_override(_price())
    tracker = PricedTracker(pricing=pricing)
    agent, _ = _agent(tracker, region=region)
    agent.ask("hi")
    (record,) = tracker.records
    assert record.cost is not None
    assert [reference.conditions for reference in record.cost.references] == [(("aws_region", region),)]
    assert record.cost.total == cost
    assert record.estimated_cost_usd is not None and Decimal(str(record.estimated_cost_usd)) == cost
    assert {component.name for component in record.cost.components} >= {"input", "output", "cache_read"}


def test_a_region_the_price_does_not_list_is_unknown(pricing: PricingService) -> None:
    pricing.set_override(_price())
    tracker = PricedTracker(pricing=pricing)  # standard mode: the request runs, its cost is unknown
    agent, _ = _agent(tracker, region="eu-west-3")
    assert agent.ask("hi") == "ok"
    (record,) = tracker.records
    assert record.estimated_cost_usd is None
    assert "tariff:aws_region=eu-west-3" in record.missing_cost_components


def test_strict_mode_refuses_a_region_without_a_price_before_the_request(pricing: PricingService) -> None:
    pricing.set_override(_price())
    pricing.conditions_source = _region("eu-west-3")
    tracker = PricedTracker(pricing=pricing, mode="strict")
    agent, stubber = _agent(tracker, region="eu-west-3")
    with pytest.raises(UnpricedModelError) as refused:
        agent.ask("hi")
    assert tracker.records == ()
    with pytest.raises(AssertionError):
        stubber.assert_no_pending_responses()  # the scripted answer was never asked for: nothing was sent
    found = library_problem(refused.value)
    assert found.code == "pricing.price_unknown"
    assert found.params == {"model": f"bedrock:{EU_PROFILE}"}
    assert "aws_region=eu-west-3" in found.message


def test_strict_mode_refuses_bedrock_when_the_region_is_not_known(pricing: PricingService) -> None:
    pricing.set_override(_price())
    tracker = PricedTracker(pricing=pricing, mode="strict")  # no conditions_source: the region cannot be checked
    agent, _ = _agent(tracker)
    with pytest.raises(UnpricedModelError, match="aws_region=unknown"):
        agent.ask("hi")
    pricing.conditions_source = lambda _provider: (_ for _ in ()).throw(RuntimeError("broken settings"))
    with pytest.raises(UnpricedModelError):
        PricedTracker(pricing=pricing, mode="strict").check_request(
            agent="A",
            provider="bedrock",
            model=EU_PROFILE,
            lookup=pricing.effective_snapshot().lookup("bedrock", EU_PROFILE),
        )


def test_strict_mode_refuses_bedrock_without_any_price(pricing: PricingService) -> None:
    pricing.conditions_source = _region("eu-central-1")
    tracker = PricedTracker(pricing=pricing, mode="strict")
    agent, _ = _agent(tracker, model=CLAUDE)
    with pytest.raises(UnpricedModelError) as refused:
        agent.ask("hi")
    assert tracker.records == ()
    assert library_problem(refused.value).params == {"model": f"bedrock:{CLAUDE}"}


def test_strict_mode_runs_a_covered_region(pricing: PricingService) -> None:
    pricing.set_override(_price())
    pricing.conditions_source = _region("eu-central-1")
    tracker = PricedTracker(pricing=pricing, mode="strict")
    agent, _ = _agent(tracker)
    agent.ask("hi")
    (record,) = tracker.records
    assert record.cost is not None and record.cost.total == EU_CENTRAL_COST


def test_the_region_check_applies_to_bedrock_only(pricing: PricingService) -> None:
    pricing.conditions_source = _region("eu-west-3")
    tracker = PricedTracker(pricing=pricing, mode="strict")
    lookup = pricing.effective_snapshot().lookup("anthropic", "claude-sonnet-5-5")
    tracker.check_request(agent="A", provider="anthropic", model="claude-sonnet-5-5", lookup=lookup)


# ---- LiteLLM ---------------------------------------------------------------------------------------------------

#: Bedrock entries in LiteLLM's format (values are test data, not AWS prices).
BEDROCK_MAP: dict[str, Any] = {
    "sample_spec": {"litellm_provider": "one of ..."},
    "bedrock/eu-central-1/anthropic.claude-3-sonnet-20240229-v1:0": {
        "litellm_provider": "bedrock",
        "mode": "chat",
        "input_cost_per_token": 3.6e-06,
        "output_cost_per_token": 1.8e-05,
    },
    "bedrock/us-east-1/anthropic.claude-3-sonnet-20240229-v1:0": {
        "litellm_provider": "bedrock",
        "mode": "chat",
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_read_input_token_cost": 3e-07,
        "cache_creation_input_token_cost": 3.75e-06,
    },
    "bedrock/us-west-2/anthropic.claude-3-sonnet-20240229-v1:0": {
        "litellm_provider": "bedrock",
        "mode": "chat",
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_read_input_token_cost": 3e-07,
        "cache_creation_input_token_cost": 3.75e-06,
    },
    # Regionless: a model id and cross-Region profiles without a source region. Never applied to all regions.
    "anthropic.claude-3-sonnet-20240229-v1:0": {
        "litellm_provider": "bedrock",
        "mode": "chat",
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
    },
    "eu.anthropic.claude-3-5-sonnet-20240620-v1:0": {
        "litellm_provider": "bedrock",
        "mode": "chat",
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
    },
    "bedrock_converse/global.anthropic.claude-sonnet-5-5": {
        "litellm_provider": "bedrock_converse",
        "mode": "chat",
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
    },
    # Provisioned throughput: no token price.
    "bedrock/us-east-1/1-month-commitment/anthropic.claude-v2:1": {
        "litellm_provider": "bedrock",
        "mode": "chat",
        "input_cost_per_second": 0.011,
        "output_cost_per_second": 0.011,
    },
    "bedrock/eu-west-1/amazon.nova-pro-v1:0": {
        "litellm_provider": "bedrock_converse",
        "mode": "chat",
        "input_cost_per_token": 9e-07,
        "output_cost_per_token": 3.6e-06,
    },
    "bedrock/eu-west-3/mistral.mistral-large-2402-v1:0": {
        "litellm_provider": "bedrock",
        "mode": "chat",
        "input_cost_per_token": 8e-06,
        "output_cost_per_token": 2.4e-05,
    },
    "bedrock/eu-west-1/mistral.mistral-large-2402-v1:0": {
        "litellm_provider": "bedrock",
        "mode": "chat",
        "input_cost_per_token": 8e-06,
        "output_cost_per_token": 2.4e-05,
    },
    "bedrock/eu-west-1/cohere.embed-v4": {
        "litellm_provider": "bedrock",
        "mode": "embedding",
        "input_cost_per_token": 1e-07,
    },
    "claude-sonnet-5-5": {
        "litellm_provider": "anthropic",
        "mode": "chat",
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
    },
}


def _litellm(document: Mapping[str, Any]) -> LiteLLMSource:
    body = json.dumps(document).encode()
    return LiteLLMSource(fetch_bytes=lambda _url, _settings: body, sleep=lambda _s: None, http=HttpSettings(attempts=1))


def test_only_regional_litellm_entries_become_bedrock_prices() -> None:
    rate = litellm_bedrock_rate(
        "bedrock/us-east-1/anthropic.claude-3-sonnet-20240229-v1:0",
        BEDROCK_MAP["bedrock/us-east-1/anthropic.claude-3-sonnet-20240229-v1:0"],
    )
    assert rate is not None and (rate.model, rate.region) == ("anthropic.claude-3-sonnet-20240229-v1:0", "us-east-1")
    assert rate.rates.cache_read == Decimal("0.3") and rate.rates.cache_write_5m == Decimal("3.75")
    for key in (
        "anthropic.claude-3-sonnet-20240229-v1:0",
        "eu.anthropic.claude-3-5-sonnet-20240620-v1:0",
        "bedrock_converse/global.anthropic.claude-sonnet-5-5",
        "bedrock/us-east-1/1-month-commitment/anthropic.claude-v2:1",
        "bedrock/eu-west-1/cohere.embed-v4",
    ):
        assert litellm_bedrock_rate(key, BEDROCK_MAP[key]) is None, key
        assert litellm_price(key, BEDROCK_MAP[key], checked_at=T0) is None, key  # never another provider's price


def test_regional_entries_become_one_price_with_a_region_tariff() -> None:
    source = _litellm(BEDROCK_MAP)
    snapshot = source.fetch(T0)
    bedrock = {price.model: price for price in snapshot.prices if price.provider == "bedrock"}
    assert set(bedrock) == {
        "anthropic.claude-3-sonnet-20240229-v1:0",
        "amazon.nova-pro-v1:0",
        "mistral.mistral-large-2402-v1:0",
    }
    claude = bedrock["anthropic.claude-3-sonnet-20240229-v1:0"]
    (tariff,) = claude.tariffs
    assert tariff.dimension == "aws_region" and tariff.default == "eu-central-1"
    assert [option.value for option in tariff.options] == ["eu-central-1", "us-east-1", "us-west-2"]
    assert claude.rates.input == Decimal("3.6") and claude.rates.cache_read is None
    us_east = tariff.option("us-east-1")
    assert us_east is not None and us_east.rates is not None and us_east.rates.input == Decimal("3")
    assert claude.quality == "unverified" and claude.source == "litellm"
    assert validate_price(claude, known_providers=KNOWN) == []
    # Regionless entries are listed, not applied; the regional ones of one model are kept apart from them.
    assert {
        "anthropic.claude-3-sonnet-20240229-v1:0",
        "eu.anthropic.claude-3-5-sonnet-20240620-v1:0",
        "bedrock_converse/global.anthropic.claude-sonnet-5-5",
        "bedrock/us-east-1/1-month-commitment/anthropic.claude-v2:1",
    } <= set(source.bedrock_unmapped)
    mistral = bedrock["mistral.mistral-large-2402-v1:0"]
    assert [option.value for tariff in mistral.tariffs for option in tariff.options] == ["eu-west-1", "eu-west-3"]
    # Anthropic's own entry stays an Anthropic price.
    assert any(price.provider == "anthropic" and price.model == "claude-sonnet-5-5" for price in snapshot.prices)


def test_an_ambiguous_model_and_region_is_left_out() -> None:
    entry = {
        "litellm_provider": "bedrock",
        "mode": "chat",
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 2e-06,
    }
    other = {**entry, "input_cost_per_token": 5e-06}
    first = litellm_bedrock_rate("bedrock/eu-west-1/amazon.nova-lite-v1:0", entry)
    second = litellm_bedrock_rate("bedrock/eu-west-1/amazon.nova-lite-v1:0", other)
    kept = litellm_bedrock_rate("bedrock/eu-central-1/amazon.nova-lite-v1:0", entry)
    assert first is not None and second is not None and kept is not None
    prices, ambiguous = bedrock_prices([first, second, kept], checked_at=T0)
    assert ambiguous == ["amazon.nova-lite-v1:0@eu-west-1"]
    (price,) = prices
    assert [option.value for tariff in price.tariffs for option in tariff.options] == ["eu-central-1"]
    only_conflict, _ = bedrock_prices([first, second], checked_at=T0)
    assert only_conflict == []


def test_synced_bedrock_prices_cover_only_their_regions(pricing: PricingService) -> None:
    report = PricingSyncService(pricing, [_litellm(BEDROCK_MAP)], clock=lambda: T0).sync(force=True)
    assert report.sources[0].ok, report
    model = "anthropic.claude-3-sonnet-20240229-v1:0"
    assert pricing.entry("bedrock", model) is not None
    assert pricing.entry("bedrock", "eu.anthropic.claude-3-5-sonnet-20240620-v1:0") is None
    costs = {}
    for region in ("eu-central-1", "us-east-1", "eu-west-3"):
        tracker = PricedTracker(pricing=pricing)
        agent, _ = _agent(tracker, model=model, region=region)
        agent.ask("hi")
        (record,) = tracker.records
        costs[region] = record
    assert costs["us-east-1"].cost is not None and costs["us-east-1"].cost.total == Decimal("0.006525")
    # eu-central-1 lists no cache rates: the cache tokens are unknown, so the cost is incomplete (never 0 for them).
    assert costs["eu-central-1"].estimated_cost_usd is None
    assert costs["eu-central-1"].cost is not None and costs["eu-central-1"].cost.known_total == Decimal("0.0072")
    assert "tariff:aws_region=eu-west-3" in costs["eu-west-3"].missing_cost_components
    assert costs["eu-west-3"].estimated_cost_usd is None


def test_an_override_fills_a_region_litellm_does_not_list(pricing: PricingService) -> None:
    PricingSyncService(pricing, [_litellm(BEDROCK_MAP)], clock=lambda: T0).sync(force=True)
    model = "anthropic.claude-3-sonnet-20240229-v1:0"
    pricing.set_override(_price(model))  # a contract price for eu-central-1 and eu-west-1 wins
    tracker = PricedTracker(pricing=pricing)
    agent, _ = _agent(tracker, model=model, region="eu-west-1")
    agent.ask("hi")
    assert tracker.records[0].cost is not None and tracker.records[0].cost.total == EU_WEST_COST


# ---- reconciliation ------------------------------------------------------------------------------------------

DAY = date(2026, 10, 20)


class _Anthropic:
    provider = "anthropic"

    def fetch(self, start: date, end: date) -> ProviderReport:
        line = BilledLine(day=DAY, provider="anthropic", amount_usd=Decimal("2"), model="claude-sonnet-5-5")
        return ProviderReport(
            provider="anthropic", lines=[line], complete_until=datetime(2026, 10, 30, tzinfo=timezone.utc)
        )


def test_bedrock_is_reported_as_not_reconciled_never_as_complete() -> None:
    estimates = {
        (DAY, "anthropic", "claude-sonnet-5-5"): Estimate(known_usd=Decimal("2"), requests=3),
        (DAY, "bedrock", EU_PROFILE): Estimate(known_usd=Decimal("1.5"), requests=4, unpriced_requests=1),
        (DAY, "bedrock", ARN): Estimate(known_usd=Decimal("0.5"), requests=1),
    }
    later = datetime(2026, 11, 1, tzinfo=timezone.utc)
    report = CostReconciliationService([_Anthropic()], lambda _s, _e: estimates, clock=lambda: later).reconcile(
        DAY, date(2026, 10, 21)
    )
    assert {row.provider for row in report.rows} == {"anthropic"}
    (bedrock,) = report.not_reconciled
    assert bedrock.provider == "bedrock" and bedrock.code == "pricing.reconciliation_not_supported"
    assert bedrock.estimated_usd == Decimal("2.0") and bedrock.requests == 5 and bedrock.unpriced_requests == 1
    assert "Guardrails" in bedrock.note and "lower bound" in bedrock.note
    # Without any adapter nothing crashes either; a provider with an adapter but no key says so.
    alone = CostReconciliationService([], lambda _s, _e: estimates, clock=lambda: later).reconcile(
        DAY, date(2026, 10, 21)
    )
    assert alone.rows == [] and {item.provider: item.code for item in alone.not_reconciled} == {
        "anthropic": "pricing.reconciliation_not_configured",
        "bedrock": "pricing.reconciliation_not_supported",
    }
