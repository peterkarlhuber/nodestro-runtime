"""Comparing Nodestro's cost estimates with the providers' bills (PRICING-1, phase N7; optional).

Nodestro estimates the cost of every request with Orkexa. The providers bill what they actually charged; their
cost reports are the reference. :class:`CostReconciliationService` fetches those reports through adapters and
compares them with Nodestro's estimates per UTC day, provider and, where the report names it, model.

Adapters (official admin APIs, each with its own admin key, separate from the model keys):

- :class:`OpenAICostsAdapter`: ``GET https://api.openai.com/v1/organization/costs`` (admin key, daily buckets,
  ``group_by=line_item,project_id``; amounts in USD).
- :class:`AnthropicCostAdapter`: ``GET https://api.anthropic.com/v1/organizations/analytics/cost`` (Usage and
  Cost Analytics API, beta ``analytics-usage-cost-2026-08-24``; admin key or ``read:analytics`` key; daily
  buckets grouped by model; amounts in cents, post-discount and pre-credit; ``data_refreshed_at`` is the
  watermark of complete data).

What a comparison can and cannot say:

- Reports cover everything billed to the organization (or the configured projects/workspaces), not only
  Nodestro's requests; restrict them to Nodestro's project or workspace for a meaningful comparison.
- Taxes are never part of either side. Credits are not deducted (Anthropic reports pre-credit amounts; OpenAI's
  reports are taken as they are). Batch, service-tier and data-residency surcharges are in both sides as far
  as Orkexa priced them.
- A day whose report is not final yet (after the provider's watermark, or within ``settle_days``) is
  ``pending``; a day where Nodestro's estimate is incomplete is ``incomplete`` and is never presented as a
  deviation; billed lines without a model are reported per provider and day (``unattributed``).

Providers without a billing adapter, such as Amazon Bedrock (AWS bills through Cost Explorer and the Cost and Usage
Report, which need AWS credentials Nodestro does not ask for), are never compared: the report lists Nodestro's
estimate for them under ``not_reconciled`` with ``pricing.reconciliation_not_supported``. Bedrock's estimate is also
never complete: the AWS bill contains charges no usage record reports (Guardrails, model invocation logging and
CloudWatch, data transfer, provisioned throughput, knowledge bases, Marketplace fees).

A deviation is flagged (``warning``) when it is above ``relative_threshold`` *and* above ``absolute_threshold_usd``
(defaults: 5 % and 1 USD).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
from typing import TYPE_CHECKING, Any, Literal, Protocol
import urllib.error
import urllib.parse
import urllib.request

from pydantic import BaseModel, Field

from .pricing_sync import HttpSettings, SourceError
from .schemas import Run, RunSummary, Usage, now

if TYPE_CHECKING:
    from .workspace import Workspace

logger = logging.getLogger(__name__)

OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
ANTHROPIC_COST_URL = "https://api.anthropic.com/v1/organizations/analytics/cost"
ANTHROPIC_ANALYTICS_BETA = "analytics-usage-cost-2026-08-24"
ANTHROPIC_VERSION = "2023-06-01"
_ALLOWED_HOSTS = frozenset({"api.openai.com", "api.anthropic.com"})
_MAX_PAGES = 100
_CENT = Decimal("0.01")


class ReconciliationError(Exception):
    """A provider's cost report could not be fetched or read."""


# ---- provider reports ------------------------------------------------------------------------------------


class BilledLine(BaseModel):
    """One amount a provider billed: per UTC day, and per model and project where the report names them."""

    day: date
    provider: str
    amount_usd: Decimal
    model: str | None = None
    project: str | None = None
    description: str | None = None


class ProviderReport(BaseModel):
    provider: str
    lines: list[BilledLine] = Field(default_factory=list)
    #: Data up to here is final according to the provider (``None``: the provider does not say).
    complete_until: datetime | None = None


JsonGetter = Callable[[str, Mapping[str, str], list[tuple[str, str]]], Any]


def get_json(
    url: str, headers: Mapping[str, str], params: list[tuple[str, str]], *, http: HttpSettings | None = None
) -> Any:
    """One HTTPS GET of a provider's admin API; size- and time-limited. Never logs the headers (keys)."""
    settings = http or HttpSettings()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS:
        raise ReconciliationError(f"Not an allowed billing API: {url}")
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}" if query else url, headers={**headers, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.timeout) as response:
            data: bytes = response.read(settings.max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise ReconciliationError(f"HTTP {exc.code} from {parts.hostname}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReconciliationError(f"Request to {parts.hostname} failed: {exc}") from None
    if len(data) > settings.max_bytes:
        raise ReconciliationError("The cost report is too large")
    try:
        return json.loads(data, parse_float=Decimal)
    except ValueError:
        raise ReconciliationError(f"The cost report of {parts.hostname} is not JSON") from None


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise ReconciliationError(f"Invalid amount {value!r} in a cost report") from None


class CostAdapter(Protocol):
    provider: str

    def fetch(self, start: date, end: date) -> ProviderReport:
        """Billed amounts of the UTC days ``start`` (inclusive) to ``end`` (exclusive)."""
        ...


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=timezone.utc)


@dataclass
class OpenAICostsAdapter:
    """OpenAI's organization Costs API (needs an admin key)."""

    admin_key: str
    #: Only these projects (``proj_...``); empty: the whole organization.
    project_ids: tuple[str, ...] = ()
    get: JsonGetter = field(default=lambda url, headers, params: get_json(url, headers, params))
    provider: str = "openai"

    def fetch(self, start: date, end: date) -> ProviderReport:
        params: list[tuple[str, str]] = [
            ("start_time", str(int(_midnight(start).timestamp()))),
            ("end_time", str(int(_midnight(end).timestamp()))),
            ("bucket_width", "1d"),
            ("limit", str(min(max((end - start).days, 1), 180))),
            ("group_by", "line_item"),
            ("group_by", "project_id"),
            *(("project_ids", project) for project in self.project_ids),
        ]
        headers = {"Authorization": f"Bearer {self.admin_key}"}
        lines: list[BilledLine] = []
        page: str | None = None
        for _ in range(_MAX_PAGES):
            body = self.get(OPENAI_COSTS_URL, headers, [*params, *((("page", page),) if page else ())])
            for bucket in body.get("data") or []:
                day = datetime.fromtimestamp(int(bucket["start_time"]), tz=timezone.utc).date()
                for result in bucket.get("results") or []:
                    amount = result.get("amount") or {}
                    if str(amount.get("currency", "usd")).lower() != "usd":
                        raise ReconciliationError(f"OpenAI reported a cost in {amount.get('currency')}")
                    item = result.get("line_item")
                    model = str(item).split(",", 1)[0].strip() if item else None
                    lines.append(
                        BilledLine(
                            day=day,
                            provider="openai",
                            amount_usd=_decimal(amount.get("value", 0)),
                            model=model or None,
                            project=result.get("project_id"),
                            description=item,
                        )
                    )
            page = body.get("next_page")
            if not page:
                break
        return ProviderReport(provider="openai", lines=lines)


@dataclass
class AnthropicCostAdapter:
    """Anthropic's Usage and Cost Analytics API (admin key or a ``read:analytics`` key)."""

    admin_key: str
    #: Only these workspaces (``wrkspc_...``); empty: the whole organization.
    workspace_ids: tuple[str, ...] = ()
    get: JsonGetter = field(default=lambda url, headers, params: get_json(url, headers, params))
    provider: str = "anthropic"

    def fetch(self, start: date, end: date) -> ProviderReport:
        lines: list[BilledLine] = []
        refreshed: datetime | None = None
        headers = {
            "x-api-key": self.admin_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": ANTHROPIC_ANALYTICS_BETA,
        }
        # The API answers at most 31 days per request.
        window = start
        while window < end:
            window_end = min(window + timedelta(days=31), end)
            params: list[tuple[str, str]] = [
                ("starting_at", _midnight(window).isoformat().replace("+00:00", "Z")),
                ("ending_at", _midnight(window_end).isoformat().replace("+00:00", "Z")),
                ("time_bucket_width", "1d"),
                ("group_by[]", "model"),
                ("limit", "1000"),
                *(("workspace_ids[]", workspace) for workspace in self.workspace_ids),
            ]
            page: str | None = None
            for _ in range(_MAX_PAGES):
                body = self.get(ANTHROPIC_COST_URL, headers, [*params, *((("page", page),) if page else ())])
                stamp = body.get("data_refreshed_at")
                if stamp:
                    moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                    refreshed = moment if refreshed is None else min(refreshed, moment)
                for row in body.get("data") or []:
                    cost = row.get("cost") or {}
                    if str(cost.get("currency", "USD")).upper() != "USD":
                        raise ReconciliationError(f"Anthropic reported a cost in {cost.get('currency')}")
                    group = row.get("group") or {}
                    lines.append(
                        BilledLine(
                            day=datetime.fromisoformat(str(row["starting_at"]).replace("Z", "+00:00")).date(),
                            provider="anthropic",
                            amount_usd=_decimal(cost.get("amount", "0")) / 100,  # cents
                            model=group.get("model"),
                            project=group.get("workspace_id"),
                        )
                    )
                page = body.get("next_page") if body.get("has_more") else None
                if not page:
                    break
            window = window_end
        return ProviderReport(provider="anthropic", lines=lines, complete_until=refreshed)


# ---- Nodestro's estimates --------------------------------------------------------------------------------


class Estimate(BaseModel):
    known_usd: Decimal = Decimal("0")
    requests: int = 0
    #: Requests whose cost is unknown or incomplete: the estimate is then a lower bound.
    unpriced_requests: int = 0


EstimateKey = tuple[date, str, str]  # day, provider, model id (without provider)


def _add_estimate(found: dict[EstimateKey, Estimate], day: date, model: str, usage: Usage) -> None:
    provider, _, model_id = model.partition(":")
    if not model_id:
        provider, model_id = "anthropic", provider  # bare Claude ids of older configurations
    estimate = found.setdefault((day, provider, model_id), Estimate())
    estimate.requests += usage.requests
    if usage.known_cost_usd is not None:
        estimate.known_usd += Decimal(str(usage.known_cost_usd))
        estimate.unpriced_requests += usage.unpriced_requests
    elif usage.estimated_cost_usd is not None:
        estimate.known_usd += Decimal(str(usage.estimated_cost_usd))
    else:
        estimate.unpriced_requests += usage.requests


def nodestro_estimates(ctx: Workspace, start: date, end: date) -> dict[EstimateKey, Estimate]:
    """Nodestro's estimated costs per UTC day, provider and model: workflow runs plus the usage ledger."""
    from .run_views import models_of_run

    found: dict[EstimateKey, Estimate] = {}
    begin, finish = _midnight(start), _midnight(end)
    for summary in ctx.store.list("runs", RunSummary):
        created = summary.created_at.astimezone(timezone.utc)
        # Imported traces were paid elsewhere; sub-workflow runs are counted in their parent run.
        if not begin <= created < finish or summary.imported_at is not None or summary.parent_run_id is not None:
            continue
        run = ctx.store.get("runs", summary.id, Run)
        if run is None:
            continue
        for model, usage in models_of_run(ctx, run).items():
            _add_estimate(found, created.date(), model, usage)
    for entry in ctx.usage_log.entries(begin):
        at = entry.at.astimezone(timezone.utc)
        if at < finish:
            _add_estimate(found, at.date(), entry.model, entry.usage)
    return found


def _billed_model(billed: str, provider: str, estimates: Mapping[EstimateKey, Estimate]) -> str:
    """The model id Nodestro uses for a billed model name: reports often name the dated snapshot
    (``gpt-5-2026-08-07``) of an alias Nodestro requested (``gpt-5``)."""
    known = {key[2] for key in estimates if key[1] == provider}
    if billed in known:
        return billed
    for model in sorted(known, key=len, reverse=True):
        rest = billed[len(model) :]
        if billed.startswith(model) and rest[:1] == "-" and rest[1:2].isdigit():
            return model
    return billed


# ---- the comparison --------------------------------------------------------------------------------------


class ReconciliationSettings(BaseModel):
    relative_threshold: Decimal = Decimal("0.05")
    absolute_threshold_usd: Decimal = Decimal("1")
    #: Days after which a provider's report is taken as final when it gives no watermark.
    settle_days: int = 2


RowStatus = Literal["ok", "warning", "incomplete", "pending", "not_comparable", "unattributed"]


class ReconciliationRow(BaseModel):
    day: date
    provider: str
    #: ``None`` for a provider's total of a day.
    model: str | None = None
    estimated_usd: Decimal | None
    billed_usd: Decimal | None
    difference_usd: Decimal | None = None
    #: ``(estimated - billed) / billed``; ``None`` when nothing was billed.
    relative_difference: Decimal | None = None
    status: RowStatus
    note: str | None = None


class ProviderResult(BaseModel):
    provider: str
    ok: bool
    error: str | None = None
    complete_until: datetime | None = None


#: Providers with a billing adapter (:class:`OpenAICostsAdapter`, :class:`AnthropicCostAdapter`).
RECONCILABLE_PROVIDERS = frozenset({"openai", "anthropic"})
#: Why a provider's estimate cannot be complete compared with its bill, where Nodestro knows it.
_UNREPORTED_CHARGES: Mapping[str, str] = {
    "bedrock": (
        "Amazon Bedrock has no billing adapter; compare with AWS Cost Explorer. The AWS bill also contains charges "
        "usage does not report (Guardrails, model invocation logging and CloudWatch, data transfer, provisioned "
        "throughput, knowledge bases, Marketplace fees), so this estimate is a lower bound"
    ),
}


class UnreconciledProvider(BaseModel):
    """A provider Nodestro estimated costs for in the period but did not compare with a bill."""

    provider: str
    #: ``pricing.reconciliation_not_supported``: no adapter exists (Bedrock); ``pricing.reconciliation_not_configured``:
    #: an adapter exists but no admin key was given.
    code: Literal["pricing.reconciliation_not_supported", "pricing.reconciliation_not_configured"]
    estimated_usd: Decimal
    requests: int
    unpriced_requests: int
    note: str


class ReconciliationReport(BaseModel):
    created_at: datetime
    start: date
    end: date
    providers: list[ProviderResult]
    #: Providers with estimates in the period that were not compared (never presented as matching their bill).
    not_reconciled: list[UnreconciledProvider] = Field(default_factory=list)
    #: One row per provider and day (``model`` is ``None``) and, below it, per model.
    rows: list[ReconciliationRow]
    warnings: int
    relative_threshold: Decimal
    absolute_threshold_usd: Decimal


def _unreconciled(estimates: Mapping[EstimateKey, Estimate], compared: set[str]) -> list[UnreconciledProvider]:
    totals: dict[str, Estimate] = {}
    for (_, provider, _), estimate in estimates.items():
        if provider in compared:
            continue
        total = totals.setdefault(provider, Estimate())
        total.requests += estimate.requests
        total.known_usd += estimate.known_usd
        total.unpriced_requests += estimate.unpriced_requests
    found = []
    for provider in sorted(totals):
        total = totals[provider]
        supported = provider in RECONCILABLE_PROVIDERS
        note = _UNREPORTED_CHARGES.get(provider) or (
            "No admin key of this provider's billing API was given"
            if supported
            else "Nodestro cannot fetch this provider's bill"
        )
        found.append(
            UnreconciledProvider(
                provider=provider,
                code="pricing.reconciliation_not_configured" if supported else "pricing.reconciliation_not_supported",
                estimated_usd=total.known_usd,
                requests=total.requests,
                unpriced_requests=total.unpriced_requests,
                note=note,
            )
        )
    return found


class CostReconciliationService:
    """Fetches the providers' reports and compares them with Nodestro's estimates."""

    def __init__(
        self,
        adapters: Iterable[CostAdapter],
        estimates: Callable[[date, date], Mapping[EstimateKey, Estimate]],
        *,
        settings: ReconciliationSettings | None = None,
        clock: Callable[[], datetime] = now,
    ) -> None:
        self.adapters = list(adapters)
        self.estimates = estimates
        self.settings = settings or ReconciliationSettings()
        self._clock = clock

    def reconcile(self, start: date, end: date) -> ReconciliationReport:
        if end <= start:
            raise ValueError("end must be after start")
        estimates = self.estimates(start, end)
        providers: list[ProviderResult] = []
        rows: list[ReconciliationRow] = []
        for adapter in self.adapters:
            try:
                report = adapter.fetch(start, end)
            except (ReconciliationError, SourceError, KeyError, TypeError, ValueError) as exc:
                logger.warning("Cost report of %s failed: %s", adapter.provider, exc)
                providers.append(ProviderResult(provider=adapter.provider, ok=False, error=str(exc)))
                continue
            providers.append(ProviderResult(provider=adapter.provider, ok=True, complete_until=report.complete_until))
            rows += self._compare(adapter.provider, report, estimates, start, end)
        return ReconciliationReport(
            created_at=self._clock(),
            start=start,
            end=end,
            providers=providers,
            not_reconciled=_unreconciled(estimates, {adapter.provider for adapter in self.adapters}),
            rows=rows,
            warnings=sum(1 for row in rows if row.status == "warning"),
            relative_threshold=self.settings.relative_threshold,
            absolute_threshold_usd=self.settings.absolute_threshold_usd,
        )

    def _pending(self, day: date, report: ProviderReport) -> bool:
        day_end = _midnight(day + timedelta(days=1))
        if report.complete_until is not None:
            return day_end > report.complete_until
        return day_end > self._clock() - timedelta(days=self.settings.settle_days)

    def _row(
        self,
        day: date,
        provider: str,
        model: str | None,
        estimate: Estimate | None,
        billed: Decimal | None,
        pending: bool,
    ) -> ReconciliationRow:
        estimated = estimate.known_usd if estimate is not None else Decimal("0")
        incomplete = estimate is not None and estimate.unpriced_requests > 0
        row = ReconciliationRow(
            day=day, provider=provider, model=model, estimated_usd=estimated, billed_usd=billed, status="ok"
        )
        if billed is None:
            row.status = "not_comparable"
            row.note = "The provider's report names no cost for this model"
            return row
        row.difference_usd = (estimated - billed).quantize(Decimal("0.000001"))
        row.relative_difference = (estimated - billed) / billed if billed else None
        if pending:
            row.status = "pending"
            row.note = "The provider's billing data for this day may still change"
        elif incomplete:
            unpriced = estimate.unpriced_requests if estimate is not None else 0
            row.status = "incomplete"
            row.note = f"{unpriced} requests without a known cost: the estimate is a lower bound"
        elif self._deviates(estimated, billed):
            row.status = "warning"
        return row

    def _deviates(self, estimated: Decimal, billed: Decimal) -> bool:
        difference = abs(estimated - billed)
        if difference <= self.settings.absolute_threshold_usd:
            return False
        if billed == 0:
            return True
        return difference / billed > self.settings.relative_threshold

    def _compare(
        self, provider: str, report: ProviderReport, estimates: Mapping[EstimateKey, Estimate], start: date, end: date
    ) -> list[ReconciliationRow]:
        billed_by_model: dict[tuple[date, str], Decimal] = {}
        unattributed: dict[date, Decimal] = {}
        billed_total: dict[date, Decimal] = {}
        for line in report.lines:
            if not start <= line.day < end:
                continue
            billed_total[line.day] = billed_total.get(line.day, Decimal("0")) + line.amount_usd
            if line.model:
                key = (line.day, _billed_model(line.model, provider, estimates))
                billed_by_model[key] = billed_by_model.get(key, Decimal("0")) + line.amount_usd
            else:
                unattributed[line.day] = unattributed.get(line.day, Decimal("0")) + line.amount_usd
        days = sorted(
            {day for day, _ in billed_by_model}
            | set(billed_total)
            | {key[0] for key in estimates if key[1] == provider}
        )
        rows: list[ReconciliationRow] = []
        for day in days:
            pending = self._pending(day, report)
            day_estimates = {key[2]: value for key, value in estimates.items() if key[0] == day and key[1] == provider}
            total = Estimate(
                known_usd=sum((value.known_usd for value in day_estimates.values()), Decimal("0")),
                requests=sum(value.requests for value in day_estimates.values()),
                unpriced_requests=sum(value.unpriced_requests for value in day_estimates.values()),
            )
            rows.append(self._row(day, provider, None, total, billed_total.get(day, Decimal("0")), pending))
            models = sorted(set(day_estimates) | {model for d, model in billed_by_model if d == day})
            for model in models:
                # A model Nodestro used but the report does not name is not comparable (the report may name it
                # differently or bill it without a model); a model only the report names was used elsewhere.
                billed = billed_by_model.get((day, model))
                rows.append(self._row(day, provider, model, day_estimates.get(model), billed, pending))
            if unattributed.get(day):
                rows.append(
                    ReconciliationRow(
                        day=day,
                        provider=provider,
                        estimated_usd=None,
                        billed_usd=unattributed[day],
                        status="unattributed",
                        note="Billed without a model (for example tools, storage or fine-tuning)",
                    )
                )
        return rows


__all__ = [
    "ANTHROPIC_COST_URL",
    "OPENAI_COSTS_URL",
    "RECONCILABLE_PROVIDERS",
    "AnthropicCostAdapter",
    "BilledLine",
    "CostAdapter",
    "CostReconciliationService",
    "Estimate",
    "OpenAICostsAdapter",
    "ProviderReport",
    "ReconciliationError",
    "ReconciliationReport",
    "ReconciliationRow",
    "ReconciliationSettings",
    "UnreconciledProvider",
    "get_json",
    "nodestro_estimates",
]
