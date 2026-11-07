"""The provider region of Bedrock requests in usage data (RT-015).

Bedrock requests run against a real ``bedrock-runtime`` client behind ``botocore.stub.Stubber`` with fake credentials
(nothing reaches AWS); the other cases add Orkexa usage records directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
from botocore.stub import Stubber
from orkexa import Agent, UsageRecord
from orkexa.pricing import ModelPrice, Tariff, TariffOption, TokenRates
from orkexa.providers.bedrock import BedrockOptions
import pytest

from nodestro_runtime.cost_tracking import PricedTracker, request_region, request_regions
from nodestro_runtime.engine import add_usage
from nodestro_runtime.pricing import PricingService
from nodestro_runtime.providers import PROVIDERS
from nodestro_runtime.schemas import Usage
from nodestro_runtime.storage import SQLiteStore
from nodestro_runtime.usage_log import UsageEntry, UsageLog

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
MODEL = "eu.anthropic.claude-sonnet-5-5"
KNOWN = frozenset({*PROVIDERS, "bedrock"})


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    found = SQLiteStore(tmp_path / "nodestro.db")
    yield found
    found.close()


@pytest.fixture
def pricing(store: SQLiteStore) -> PricingService:
    return PricingService(store, clock=lambda: T0, known_providers=KNOWN)


def _price() -> ModelPrice:
    """Test data, not AWS prices: eu-central-1 only."""
    return ModelPrice(
        provider="bedrock",
        model=MODEL,
        rates=TokenRates.of("3", "15"),
        tariffs=(Tariff("aws_region", "eu-central-1", (TariffOption("eu-central-1"),)),),
        source="acme-aws-contract",
        quality="contract",
    )


def _agent(tracker: PricedTracker, region: str, *, name: str = "writer", pin: bool = True) -> Agent:
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        aws_access_key_id="AKIDEXAMPLE",
        aws_secret_access_key="fake-secret-for-tests",
    )
    stubber = Stubber(client)
    stubber.add_response(
        "converse",
        {
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
            "metrics": {"latencyMs": 12},
            "ResponseMetadata": {"RequestId": "req-1", "HTTPStatusCode": 200},
        },
    )
    stubber.activate()
    # Nodestro's connections build Bedrock agents with the region in their options (``Connection.options``).
    options = BedrockOptions(region_name=region)
    agent = Agent(model=f"bedrock:{MODEL}", client=client, usage_tracker=tracker, name=name, options=options)
    if pin:
        tracker.pin_conditions(agent.name, agent.options)
    return agent


def _record(agent: str = "writer", provider: str = "bedrock", **kwargs: Any) -> UsageRecord:
    values: dict[str, Any] = {
        "agent": agent,
        "model": MODEL,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
        "thinking_tokens": 0,
        "web_search_requests": 0,
        "web_fetch_requests": 0,
        "estimated_cost_usd": None,
        "provider": provider,
    }
    values.update(kwargs)
    return UsageRecord(**values)


def test_an_unpriced_bedrock_request_records_the_region_its_agent_was_built_for(
    pricing: PricingService, store: SQLiteStore
) -> None:
    tracker = PricedTracker(pricing=pricing)  # no price at all: Orkexa records no condition
    assert _agent(tracker, "eu-west-3").ask("hi") == "ok"
    (record,) = tracker.records
    assert record.estimated_cost_usd is None and record.pricing_conditions == ()
    assert request_region(tracker, record) == "eu-west-3"
    (entry,) = UsageLog(store).record("agent_test", tracker.records, label="writer", tracker=tracker)
    assert entry.region == "eu-west-3"
    assert entry.usage.regions == ["eu-west-3"]
    assert UsageLog(store).entries()[0].region == "eu-west-3"  # stored and read back


def test_a_priced_request_without_a_pinned_agent_uses_the_priced_region(pricing: PricingService) -> None:
    pricing.set_override(_price())
    tracker = PricedTracker(pricing=pricing)
    _agent(tracker, "eu-central-1", pin=False).ask("hi")
    (record,) = tracker.records
    assert record.estimated_cost_usd is not None
    assert request_region(tracker, record) == "eu-central-1"


def test_without_a_pin_or_a_price_the_connection_region_is_recorded(pricing: PricingService) -> None:
    pricing.conditions_source = lambda provider: {"aws_region": "eu-north-1"} if provider == "bedrock" else {}
    tracker = PricedTracker(pricing=pricing)
    _agent(tracker, "eu-north-1", pin=False).ask("hi")
    assert request_regions(tracker, tracker.records) == ["eu-north-1"]


def test_an_unknown_region_is_none_never_a_guess(pricing: PricingService, store: SQLiteStore) -> None:
    tracker = PricedTracker(pricing=pricing)  # no pin, no price, no connection
    _agent(tracker, "eu-west-3", pin=False).ask("hi")
    (entry,) = UsageLog(store).record("agent_test", tracker.records, tracker=tracker)
    assert entry.region is None and entry.usage.regions == []


def test_a_broken_connection_setting_leaves_the_region_unknown(pricing: PricingService) -> None:
    def broken(provider: str) -> dict[str, str]:
        raise RuntimeError("connection settings unreadable")

    pricing.conditions_source = broken
    tracker = PricedTracker(pricing=pricing)
    tracker.add(_record())  # recording must not fail because of it
    assert request_regions(tracker, tracker.records) == []


def test_other_providers_have_no_region(pricing: PricingService) -> None:
    pricing.conditions_source = lambda provider: {"aws_region": "eu-central-1"}
    tracker = PricedTracker(pricing=pricing)
    tracker.add(_record(provider="anthropic"))
    tracker.add(_record(provider="ollama", pricing_conditions=(("aws_region", "eu-central-1"),)))
    assert request_regions(tracker, tracker.records) == []
    assert request_regions(object(), tracker.records) == []  # a tracker that does not price: nothing known


def test_one_ledger_entry_per_model_and_region(pricing: PricingService, store: SQLiteStore) -> None:
    tracker = PricedTracker(pricing=pricing)
    _agent(tracker, "eu-central-1", name="a").ask("hi")
    _agent(tracker, "eu-west-1", name="b").ask("hi")
    entries = UsageLog(store).record("assistant", tracker.records, tracker=tracker)
    assert sorted((entry.model, entry.region, entry.usage.requests) for entry in entries) == [
        (f"bedrock:{MODEL}", "eu-central-1", 1),
        (f"bedrock:{MODEL}", "eu-west-1", 1),
    ]


def test_a_parent_tracker_knows_the_region_of_a_child_agent(pricing: PricingService) -> None:
    parent = PricedTracker(pricing=pricing)
    child = PricedTracker(pricing=pricing, parent=parent, prefix="node-1")
    _agent(child, "eu-south-2").ask("hi")
    (forwarded,) = parent.records
    assert forwarded.agent == "node-1/writer"
    assert request_region(parent, forwarded) == "eu-south-2"
    assert request_regions(child, child.records) == ["eu-south-2"]


def test_usages_merge_their_regions_and_old_usage_has_none() -> None:
    first = Usage(requests=1, regions=["eu-west-1"])
    second = Usage(requests=2, regions=["eu-central-1", "eu-west-1"])
    assert add_usage(first, second).regions == ["eu-central-1", "eu-west-1"]
    stored = {"requests": 1, "estimated_cost_usd": 0.1}  # usage stored before RT-015
    assert Usage.model_validate(stored).regions == []
    old_entry = UsageEntry.model_validate(
        {"source": "agent_test", "model": "bedrock:x", "usage": stored, "at": T0.isoformat()}
    )
    assert old_entry.region is None
    assert Decimal(str(old_entry.usage.estimated_cost_usd)) == Decimal("0.1")
