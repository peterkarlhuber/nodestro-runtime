"""Costs priced with the workspace's catalog (PRICING-1 N5/N6): runs, agent tests, ledger, history, strict mode."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

from orkexa import UsageRecord
from orkexa.pricing import BUILTIN_VERSION, ModelPrice, PriceSnapshot, TokenRates
import pytest

from nodestro_runtime.agent_test import AgentTestIn
from nodestro_runtime.cost_tracking import PricedTracker
from nodestro_runtime.engine import _ChildTracker, add_usage
from nodestro_runtime.schemas import AgentConfig, RunIn, SettingsIn, Usage, WorkflowIn, now
from nodestro_runtime.testing import ScriptedClient, text
from nodestro_runtime.workspace import Workspace

Make = Callable[..., Workspace]
#: 1,000 input and 200 output tokens (the scripted answers' usage) at $1 / $5 per million tokens.
OVERRIDE_COST = 0.002


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]]) -> Workspace:
        fake = ScriptedClient(script)
        workspace = Workspace(tmp_path / f"data{len(opened)}", client_factory=lambda _p: fake, integrations=[])
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def _flow(model: str = "anthropic:claude-sonnet-5-5") -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Cost",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "a",
                    "type": "agent",
                    "label": "A",
                    "agent": {"name": "A", "system_prompt": "A", "model": model},
                },
                {"id": "out", "type": "output"},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "a"},
                {"id": "e2", "source": "a", "target": "out"},
            ],
        }
    )


async def _run(workspace: Workspace, flow: WorkflowIn) -> Any:
    saved = workspace.save_workflow(flow)
    started = await workspace.run(saved, RunIn(input="x"))
    return await workspace.wait(started.id, timeout=20)


def _override(model: str = "claude-sonnet-5-5") -> ModelPrice:
    return ModelPrice(
        provider="anthropic",
        model=model,
        rates=TokenRates.of("1", "5"),
        source="acme-contract",
        quality="contract",
        version="2026-11",
    )


async def test_a_run_records_its_price_references(make: Make) -> None:
    workspace = make({"A": [text("ok")]})
    done = await _run(workspace, _flow())
    assert done.status == "succeeded", done.error
    usage = done.usage
    assert usage.cost_complete is True and usage.unpriced_requests == 0
    assert usage.known_cost_usd == usage.estimated_cost_usd and (usage.estimated_cost_usd or 0) > 0
    assert usage.pricing_versions == [BUILTIN_VERSION]
    assert usage.pricing_sources == ["builtin"] and usage.pricing_quality == "verified"
    assert done.nodes["a"].usage.pricing_versions == [BUILTIN_VERSION]


async def test_an_override_prices_runs_and_later_changes_keep_history(make: Make) -> None:
    workspace = make({"A": [text("ok"), text("again")]})
    workspace.pricing.set_override(_override())
    first = await _run(workspace, _flow())
    assert first.usage.estimated_cost_usd == pytest.approx(OVERRIDE_COST)
    assert first.usage.pricing_sources == ["acme-contract"] and first.usage.pricing_quality == "contract"

    workspace.pricing.remove_override("anthropic", "claude-sonnet-5-5")
    second = await _run(workspace, _flow())
    assert second.usage.estimated_cost_usd != pytest.approx(OVERRIDE_COST)
    # The first run keeps the cost it was priced with.
    stored = workspace.runs.get(first.id)
    assert stored is not None and stored.usage.estimated_cost_usd == pytest.approx(OVERRIDE_COST)


async def test_an_unknown_price_is_unknown_not_zero(make: Make) -> None:
    workspace = make({"A": [text("ok", model="claude-unknown-9")]})
    done = await _run(workspace, _flow("anthropic:claude-unknown-9"))
    assert done.status == "succeeded", done.error
    usage = done.usage
    assert usage.estimated_cost_usd is None and usage.cost_complete is False
    assert usage.unpriced_requests == 1 and usage.known_cost_usd == 0
    assert usage.missing_cost_components


async def test_strict_mode_refuses_requests_without_a_price(make: Make) -> None:
    workspace = make({"A": [text("ok", model="claude-unknown-9")]})
    workspace.update_settings(SettingsIn(price_mode="strict"))
    assert workspace.settings().price_mode == "strict"
    done = await _run(workspace, _flow("anthropic:claude-unknown-9"))
    assert done.status == "failed"
    assert done.usage.requests == 0  # nothing was sent


async def test_stale_prices_are_counted_and_strict_mode_refuses_critical_ones(make: Make) -> None:
    workspace = make({"A": [text("ok"), text("again")]})
    later = now() + timedelta(days=3)
    workspace.pricing._clock = lambda: later  # three days without a successful check
    done = await _run(workspace, _flow())
    assert done.status == "succeeded", done.error
    assert done.usage.stale_price_requests == 1

    workspace.pricing._clock = lambda: now() + timedelta(days=9)
    workspace.update_settings(SettingsIn(price_mode="strict"))
    refused = await _run(workspace, _flow())
    assert refused.status == "failed"
    assert refused.nodes["a"].error_code == "pricing.stale_price"
    assert refused.usage.requests == 0


def test_the_agent_test_prices_with_the_catalog(make: Make) -> None:
    workspace = make({"T": [text("ok")]})
    workspace.pricing.set_override(_override())
    result, tracker = workspace.tester.run(AgentTestIn(agent=AgentConfig(name="T", system_prompt="T")))
    assert result.status == "succeeded", result.error
    assert result.usage.estimated_cost_usd == pytest.approx(OVERRIDE_COST)
    assert result.usage.pricing_sources == ["acme-contract"]
    assert isinstance(tracker, PricedTracker)


def test_the_usage_ledger_keeps_price_references(make: Make) -> None:
    workspace = make({})
    tracker = workspace.tracker()
    record = UsageRecord(
        agent="assistant",
        model="claude-sonnet-5-5",
        input_tokens=10,
        output_tokens=10,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_creation_5m_input_tokens=0,
        cache_creation_1h_input_tokens=0,
        thinking_tokens=0,
        web_search_requests=0,
        web_fetch_requests=0,
        estimated_cost_usd=None,
        provider="anthropic",
        pricing_version="v1",
        pricing_source="litellm",
        pricing_quality="unverified",
    )
    [entry] = workspace.usage_log.record("assistant", [record], tracker=tracker)
    assert entry.usage.pricing_versions == ["v1"] and entry.usage.pricing_quality == "unverified"
    assert entry.usage.cost_complete is False and entry.usage.unpriced_requests == 1


def test_sub_workflows_price_with_the_parents_catalog(make: Make) -> None:
    workspace = make({})
    parent = workspace.tracker(5)
    child = _ChildTracker(parent, "sub")
    assert child.price_resolver is parent.price_resolver is workspace.pricing.resolver()
    assert child.mode == parent.mode


def test_usage_of_older_releases_still_adds_up() -> None:
    legacy = Usage.model_validate({"requests": 2, "estimated_cost_usd": None})  # stored before PRICING-1
    legacy_priced = Usage.model_validate({"requests": 1, "estimated_cost_usd": 0.5})
    current = Usage(requests=1, estimated_cost_usd=0.25, known_cost_usd=0.25, cost_complete=True)
    total = add_usage(add_usage(legacy, current), legacy_priced)
    assert total.requests == 4 and total.estimated_cost_usd is None
    assert total.known_cost_usd == pytest.approx(0.75)
    assert total.unpriced_requests == 2
    assert total.cost_complete is False
    assert add_usage(legacy_priced, current).cost_complete is None  # nobody knows whether the old one was complete


def test_a_submitted_snapshot_is_used_by_new_trackers(make: Make) -> None:
    workspace = make({})
    workspace.pricing.submit(
        PriceSnapshot(
            "litellm-1",
            (ModelPrice(provider="openai", model="gpt-test", rates=TokenRates.of("2", "8"), source="litellm"),),
        )
    )
    tracker = workspace.tracker()
    assert tracker.price_resolver is not None
    assert tracker.price_resolver.resolve("openai", "gpt-test").found


def test_the_pre_check_knows_the_workspaces_prices(make: Make) -> None:
    from nodestro_runtime.preflight import preflight

    workspace = make({})
    saved = workspace.save_workflow(_flow("openai:gpt-custom"))

    def unpriced() -> bool:
        check = preflight(workspace, workspace.workflow(saved.id))
        return any(issue.code == "check.model_unpriced" for issue in check.problems)

    assert unpriced()
    workspace.pricing.set_override(
        ModelPrice(provider="openai", model="gpt-custom", rates=TokenRates.of("1", "2"), source="custom")
    )
    assert not unpriced()
