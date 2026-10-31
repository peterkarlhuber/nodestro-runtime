"""Comparing Nodestro's cost estimates with the providers' bills (PRICING-1 N7)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from orkexa.pricing import ModelPrice, TokenRates
import pytest

from nodestro_runtime.reconciliation import (
    ANTHROPIC_ANALYTICS_BETA,
    ANTHROPIC_COST_URL,
    OPENAI_COSTS_URL,
    AnthropicCostAdapter,
    BilledLine,
    CostReconciliationService,
    Estimate,
    EstimateKey,
    OpenAICostsAdapter,
    ProviderReport,
    ReconciliationError,
    ReconciliationSettings,
    get_json,
    nodestro_estimates,
)
from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text
from nodestro_runtime.workspace import Workspace

DAY = date(2026, 10, 1)
NEXT = date(2026, 10, 2)
LATER = datetime(2026, 10, 20, tzinfo=timezone.utc)


class Recorder:
    """A fake JSON getter that answers pages in order and remembers each request."""

    def __init__(self, *pages: Any) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, dict[str, str], list[tuple[str, str]]]] = []

    def __call__(self, url: str, headers: Mapping[str, str], params: list[tuple[str, str]]) -> Any:
        self.calls.append((url, dict(headers), params))
        return self.pages.pop(0)


def _unix(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp())


def test_openai_costs_are_read_per_day_and_model_across_pages() -> None:
    get = Recorder(
        {
            "data": [
                {
                    "start_time": _unix(DAY),
                    "results": [
                        {"amount": {"value": 1.25, "currency": "usd"}, "line_item": "gpt-5, input", "project_id": "p"},
                        {"amount": {"value": 0.75, "currency": "usd"}, "line_item": "gpt-5, output", "project_id": "p"},
                    ],
                }
            ],
            "next_page": "page_2",
        },
        {
            "data": [
                {
                    "start_time": _unix(NEXT),
                    "results": [{"amount": {"value": "0.5", "currency": "usd"}, "line_item": None}],
                }
            ],
            "next_page": None,
        },
    )
    report = OpenAICostsAdapter("sk-admin-test", project_ids=("p",), get=get).fetch(DAY, date(2026, 10, 3))
    assert [(line.day, line.model, line.amount_usd) for line in report.lines] == [
        (DAY, "gpt-5", Decimal("1.25")),
        (DAY, "gpt-5", Decimal("0.75")),
        (NEXT, None, Decimal("0.5")),
    ]
    url, headers, params = get.calls[0]
    assert url == OPENAI_COSTS_URL and headers["Authorization"] == "Bearer sk-admin-test"
    assert ("start_time", str(_unix(DAY))) in params and ("bucket_width", "1d") in params
    assert ("group_by", "line_item") in params and ("project_ids", "p") in params
    assert ("page", "page_2") in get.calls[1][2]


def test_openai_costs_in_another_currency_are_refused() -> None:
    get = Recorder({"data": [{"start_time": _unix(DAY), "results": [{"amount": {"value": 1, "currency": "eur"}}]}]})
    with pytest.raises(ReconciliationError):
        OpenAICostsAdapter("k", get=get).fetch(DAY, NEXT)


def test_anthropic_costs_are_cents_split_into_31_day_windows_with_a_watermark() -> None:
    def page(day: str, cents: str, refreshed: str, more: bool = False) -> dict[str, Any]:
        return {
            "data": [
                {
                    "starting_at": f"{day}T00:00:00Z",
                    "cost": {"amount": cents, "currency": "USD"},
                    "group": {"model": "claude-sonnet-5-5", "workspace_id": "wrkspc_1"},
                }
            ],
            "has_more": more,
            "next_page": "next" if more else None,
            "data_refreshed_at": refreshed,
        }

    get = Recorder(
        page("2026-09-01", "1234", "2026-10-30T12:00:00Z", more=True),
        page("2026-09-02", "66", "2026-10-30T10:00:00Z"),
        page("2026-10-05", "100", "2026-10-30T11:00:00Z"),
    )
    adapter = AnthropicCostAdapter("sk-ant-admin-test", workspace_ids=("wrkspc_1",), get=get)
    report = adapter.fetch(date(2026, 9, 1), date(2026, 10, 10))
    assert [line.amount_usd for line in report.lines] == [Decimal("12.34"), Decimal("0.66"), Decimal("1")]
    assert report.complete_until == datetime(2026, 10, 30, 10, tzinfo=timezone.utc)
    url, headers, params = get.calls[0]
    assert url == ANTHROPIC_COST_URL and headers["x-api-key"] == "sk-ant-admin-test"
    assert headers["anthropic-beta"] == ANTHROPIC_ANALYTICS_BETA
    assert ("starting_at", "2026-09-01T00:00:00Z") in params and ("ending_at", "2026-10-02T00:00:00Z") in params
    assert ("group_by[]", "model") in params and ("workspace_ids[]", "wrkspc_1") in params
    assert ("page", "next") in get.calls[1][2]
    assert ("starting_at", "2026-10-02T00:00:00Z") in get.calls[2][2]


def test_only_the_providers_https_apis_are_called() -> None:
    with pytest.raises(ReconciliationError):
        get_json("https://example.com/v1/organization/costs", {}, [])
    with pytest.raises(ReconciliationError):
        get_json("http://api.openai.com/v1/organization/costs", {}, [])


class FixedAdapter:
    def __init__(self, provider: str, lines: list[BilledLine], complete_until: datetime | None = None) -> None:
        self.provider = provider
        self.report = ProviderReport(provider=provider, lines=lines, complete_until=complete_until)

    def fetch(self, start: date, end: date) -> ProviderReport:
        return self.report


class FailingAdapter:
    provider = "openai"

    def fetch(self, start: date, end: date) -> ProviderReport:
        raise ReconciliationError("HTTP 401 from api.openai.com")


def _line(amount: str, model: str | None = "claude-sonnet-5-5", day: date = DAY) -> BilledLine:
    return BilledLine(day=day, provider="anthropic", amount_usd=Decimal(amount), model=model)


def _service(
    lines: list[BilledLine],
    estimates: dict[EstimateKey, Estimate],
    *,
    complete_until: datetime | None = None,
    settings: ReconciliationSettings | None = None,
) -> CostReconciliationService:
    return CostReconciliationService(
        [FixedAdapter("anthropic", lines, complete_until)],
        lambda _start, _end: estimates,
        settings=settings,
        clock=lambda: LATER,
    )


def _estimate(known: str, unpriced: int = 0, model: str = "claude-sonnet-5-5") -> dict[EstimateKey, Estimate]:
    return {(DAY, "anthropic", model): Estimate(known_usd=Decimal(known), requests=10, unpriced_requests=unpriced)}


@pytest.mark.parametrize(
    ("estimated", "billed", "status"),
    [
        ("100", "104", "ok"),  # 3.8 %: below the relative threshold
        ("10", "10.9", "ok"),  # 8 %, but only 0.90 USD
        ("100", "110", "warning"),  # 9 % and 10 USD
        ("0", "2", "warning"),
    ],
)
def test_a_deviation_needs_both_thresholds(estimated: str, billed: str, status: str) -> None:
    report = _service([_line(billed)], _estimate(estimated)).reconcile(DAY, NEXT)
    total, model = report.rows
    assert total.model is None and model.model == "claude-sonnet-5-5"
    assert total.status == model.status == status
    assert report.warnings == (2 if status == "warning" else 0)
    assert model.difference_usd == Decimal(estimated) - Decimal(billed)


def test_the_thresholds_are_configurable() -> None:
    settings = ReconciliationSettings(relative_threshold=Decimal("0.01"), absolute_threshold_usd=Decimal("0.1"))
    report = _service([_line("10.5")], _estimate("10"), settings=settings).reconcile(DAY, NEXT)
    assert report.rows[0].status == "warning"


def test_an_incomplete_estimate_is_never_a_deviation() -> None:
    report = _service([_line("50")], _estimate("10", unpriced=3)).reconcile(DAY, NEXT)
    assert {row.status for row in report.rows} == {"incomplete"}
    assert "3 requests" in (report.rows[0].note or "")


def test_days_not_final_yet_are_pending() -> None:
    watermark = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)  # the provider has data up to noon of DAY
    report = _service([_line("50")], _estimate("10"), complete_until=watermark).reconcile(DAY, NEXT)
    assert {row.status for row in report.rows} == {"pending"}
    recent = CostReconciliationService(
        [FixedAdapter("anthropic", [_line("50")])],
        lambda _s, _e: _estimate("10"),
        clock=lambda: datetime(2026, 10, 2, 12, tzinfo=timezone.utc),  # within the two settle days
    )
    assert recent.reconcile(DAY, NEXT).rows[0].status == "pending"


def test_unattributed_and_unknown_models_are_shown_separately() -> None:
    lines = [_line("3"), _line("2", model=None), _line("4", model="claude-haiku-5-5")]
    estimates = {
        **_estimate("3"),
        (DAY, "anthropic", "claude-opus-5-5"): Estimate(known_usd=Decimal("1"), requests=1),
    }
    rows = _service(lines, estimates).reconcile(DAY, NEXT).rows
    by_model = {row.model: row for row in rows if row.status != "unattributed"}
    assert by_model[None].billed_usd == Decimal("9") and by_model[None].estimated_usd == Decimal("4")
    assert by_model["claude-sonnet-5-5"].status == "ok"
    assert by_model["claude-opus-5-5"].status == "not_comparable"  # used by Nodestro, not named in the report
    assert by_model["claude-haiku-5-5"].estimated_usd == Decimal("0")  # billed, but not used by Nodestro
    [unattributed] = [row for row in rows if row.status == "unattributed"]
    assert unattributed.billed_usd == Decimal("2")


def test_dated_snapshot_names_match_the_alias_nodestro_used() -> None:
    lines = [BilledLine(day=DAY, provider="openai", amount_usd=Decimal("5"), model="gpt-5-2026-08-07")]
    estimates = {(DAY, "openai", "gpt-5"): Estimate(known_usd=Decimal("5"), requests=1)}
    service = CostReconciliationService([FixedAdapter("openai", lines)], lambda _s, _e: estimates, clock=lambda: LATER)
    rows = service.reconcile(DAY, NEXT).rows
    assert [(row.model, row.status) for row in rows] == [(None, "ok"), ("gpt-5", "ok")]


def test_a_failing_provider_does_not_hide_the_others() -> None:
    service = CostReconciliationService(
        [FailingAdapter(), FixedAdapter("anthropic", [_line("3")])],
        lambda _s, _e: _estimate("3"),
        clock=lambda: LATER,
    )
    report = service.reconcile(DAY, NEXT)
    assert [(p.provider, p.ok) for p in report.providers] == [("openai", False), ("anthropic", True)]
    assert "401" in (report.providers[0].error or "")
    assert report.rows and all(row.provider == "anthropic" for row in report.rows)


def test_lines_outside_the_period_are_ignored() -> None:
    report = _service([_line("3"), _line("7", day=NEXT)], _estimate("3")).reconcile(DAY, NEXT)
    assert {row.day for row in report.rows} == {DAY}


async def test_nodestro_estimates_cover_runs_and_the_usage_ledger(tmp_path: Path) -> None:
    fake = ScriptedClient({"A": [text("ok")]})
    workspace = Workspace(tmp_path / "data", client_factory=lambda _p: fake, integrations=[])
    try:
        workspace.pricing.set_override(
            ModelPrice(provider="anthropic", model="claude-sonnet-5-5", rates=TokenRates.of("1", "5"), source="x")
        )
        flow = WorkflowIn.model_validate(
            {
                "name": "Cost",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "system_prompt": "A"}},
                    {"id": "out", "type": "output"},
                ],
                "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
            }
        )
        saved = workspace.save_workflow(flow)
        started = await workspace.run(saved, RunIn(input="x"))
        done = await workspace.wait(started.id, timeout=20)
        assert done.status == "succeeded", done.error
        today = done.created_at.astimezone(timezone.utc).date()
        found = nodestro_estimates(workspace, today, date.fromordinal(today.toordinal() + 1))
        [(key, estimate)] = found.items()
        assert key[1] == "anthropic" and estimate.requests == 1
        assert estimate.known_usd == Decimal("0.002") and estimate.unpriced_requests == 0
        assert nodestro_estimates(workspace, date(2020, 1, 1), date(2020, 1, 2)) == {}
    finally:
        workspace.close()
