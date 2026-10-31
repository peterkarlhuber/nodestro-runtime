"""Fetching reference prices: the price sources and the sync job (PRICING-1, phase N3).

:class:`PricingSyncService` asks each :class:`PriceSource` for a candidate snapshot and hands it to
:meth:`PricingService.submit`, which validates it and activates what may be activated. Sources:

- :class:`OrkexaBuiltinSource`: the prices Orkexa ships (Anthropic's list prices, checked against the official
  price list; quality ``verified``). No network access; a new Orkexa pin brings new prices.
- :class:`LiteLLMSource`: LiteLLM's community-maintained model cost map, a JSON file on GitHub. It covers
  OpenAI, Anthropic and Gemini models, so it is the only automatic source for OpenAI and Gemini prices. Its
  prices are ``unverified``: large changes wait for review, and they never replace an officially confirmed
  price (see :meth:`PricingService.submit`).
- :class:`SnapshotFileSource`: a reviewed snapshot an operator supplies (a file in Orkexa's snapshot format),
  for example prices checked by hand against the OpenAI and Google price lists, or contract prices.

The official price pages of OpenAI, Anthropic and Google are HTML pages without a stable machine-readable
interface; Nodestro does not scrape them. Their prices reach Nodestro through Orkexa's built-in snapshot
(Anthropic) or a reviewed snapshot file.

Downloads are HTTPS only, from an allowed host, with timeouts, a size limit and a bounded number of retries with
exponential backoff. A failed source changes nothing: the last valid prices stay active. Syncs never run on the
path of a model request; the desktop runs :meth:`PricingSyncService.run` in the background, a server runs it in
its worker under a lock shared by its processes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any, Protocol
import urllib.error
import urllib.parse
import urllib.request

from orkexa.pricing import (
    BUILTIN_SNAPSHOT,
    LongContextTier,
    ModelPrice,
    PriceSnapshot,
    PricingError,
    Tariff,
    TariffOption,
    TokenRates,
)
from pydantic import BaseModel, Field

from .pricing import PricingService, SubmitResult
from .schemas import now

logger = logging.getLogger(__name__)

#: LiteLLM's model cost map (MIT license), the file LiteLLM itself loads.
LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
LITELLM_SOURCE = "litellm"
#: The largest download accepted (the map is about 3 MB in October 2026).
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
_PER_MILLION = Decimal(1_000_000)


class SourceError(Exception):
    """A source could not deliver a candidate (network, HTTP status, size, format)."""


# ---- downloads -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpSettings:
    timeout: float = 20.0
    max_bytes: int = MAX_DOWNLOAD_BYTES
    #: Attempts per download (the first one included).
    attempts: int = 3
    #: Wait before the second attempt; doubled for every further one.
    backoff: float = 2.0


Fetcher = Callable[[str, HttpSettings], bytes]


def https_get(url: str, settings: HttpSettings) -> bytes:
    """Download ``url`` once: HTTPS only, ``settings.timeout``, at most ``settings.max_bytes``."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise SourceError(f"Only HTTPS downloads are allowed: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "Nodestro price sync", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=settings.timeout) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https":
                raise SourceError(f"Redirected to a non-HTTPS URL: {response.geturl()}")
            length = response.headers.get("Content-Length")
            if length is not None and length.isdigit() and int(length) > settings.max_bytes:
                raise SourceError(f"The download is larger than {settings.max_bytes} bytes")
            data: bytes = response.read(settings.max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise SourceError(f"HTTP {exc.code} from {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"Download of {url} failed: {exc}") from None
    if len(data) > settings.max_bytes:
        raise SourceError(f"The download is larger than {settings.max_bytes} bytes")
    return data


def fetch_with_retries(
    url: str,
    settings: HttpSettings,
    *,
    fetch: Fetcher = https_get,
    sleep: Callable[[float], None] = time.sleep,
    allowed_hosts: frozenset[str] | None = None,
) -> bytes:
    """Download with at most ``settings.attempts`` attempts and exponential backoff between them."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if allowed_hosts is not None and host not in allowed_hosts:
        raise SourceError(f"Host {host!r} is not an allowed price source")
    delay = settings.backoff
    for attempt in range(1, settings.attempts + 1):
        try:
            return fetch(url, settings)
        except SourceError as exc:
            if attempt == settings.attempts:
                raise
            logger.info(
                "Price download attempt %d of %d failed (%s); retrying in %.0f s",
                attempt,
                settings.attempts,
                exc,
                delay,
            )
            sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover


# ---- sources ---------------------------------------------------------------------------------------------


class PriceSource(Protocol):
    """Delivers one candidate snapshot; raises :class:`SourceError` when it cannot."""

    name: str

    def fetch(self, at: datetime) -> PriceSnapshot: ...


@dataclass
class OrkexaBuiltinSource:
    """The prices that ship with the pinned Orkexa release (verified Anthropic list prices)."""

    name: str = "orkexa"

    def fetch(self, at: datetime) -> PriceSnapshot:
        return BUILTIN_SNAPSHOT


@dataclass
class SnapshotFileSource:
    """A reviewed snapshot file (Orkexa's ``orkexa.price-snapshot/1`` JSON) supplied by the operator."""

    path: Path
    name: str = "file"

    def fetch(self, at: datetime) -> PriceSnapshot:
        try:
            text = self.path.read_bytes()
        except OSError as exc:
            raise SourceError(f"Cannot read the price file {self.path}: {exc}") from None
        if len(text) > MAX_DOWNLOAD_BYTES:
            raise SourceError(f"The price file {self.path} is too large")
        try:
            return PriceSnapshot.from_json(text)
        except PricingError as exc:
            raise SourceError(f"The price file {self.path} is invalid: {exc}") from None


#: LiteLLM provider names and the Nodestro provider each one maps to.
LITELLM_PROVIDERS: Mapping[str, str] = {"openai": "openai", "anthropic": "anthropic", "gemini": "gemini"}
#: The modes of LiteLLM entries that are text-generation models (others are images, audio, embeddings, ...).
_LITELLM_MODES = frozenset({"chat", "responses", "completion"})
#: LiteLLM suffixes that are a service tier, and the value providers report for it.
_LITELLM_TIERS: Mapping[str, str] = {"flex": "flex", "priority": "priority", "batches": "batch"}
_ABOVE = re.compile(r"^input_cost_per_token_above_(\d+)k_tokens$")


def _per_million(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value)) * _PER_MILLION
    except InvalidOperation:
        return None
    # Per-token floats such as 1.25e-06 become exact per-million amounts, in plain notation ("30", not "3E+1").
    return Decimal(format(amount.quantize(Decimal("0.000001")).normalize(), "f"))


def _litellm_rates(entry: Mapping[str, Any], suffix: str) -> TokenRates | None:
    """The token rates of an entry for one key suffix (``""``, ``"_flex"``, ``"_above_200k_tokens"``, ...)."""
    input_ = _per_million(entry.get(f"input_cost_per_token{suffix}"))
    output = _per_million(entry.get(f"output_cost_per_token{suffix}"))
    if input_ is None or output is None:
        return None
    hour_suffix = "_above_1hr" + suffix
    return TokenRates(
        input=input_,
        output=output,
        cache_read=_per_million(entry.get(f"cache_read_input_token_cost{suffix}")),
        cache_write_5m=_per_million(entry.get(f"cache_creation_input_token_cost{suffix}")),
        cache_write_1h=_per_million(entry.get(f"cache_creation_input_token_cost{hour_suffix}")),
    )


def _litellm_tiers(entry: Mapping[str, Any], tier: str = "") -> tuple[LongContextTier, ...] | None:
    """Long-context tiers (``..._above_<N>k_tokens[_<tier>]``); ``None`` when one of them is incomplete."""
    thresholds = sorted({int(match.group(1)) for key in entry if (match := _ABOVE.match(key))})
    tiers = []
    for threshold in thresholds:
        rates = _litellm_rates(entry, f"_above_{threshold}k_tokens{tier}")
        if rates is None:
            return None
        tiers.append(LongContextTier(threshold * 1000, rates))
    return tuple(tiers)


def litellm_price(
    key: str, entry: Mapping[str, Any], *, checked_at: datetime, url: str = LITELLM_URL
) -> ModelPrice | None:
    """One LiteLLM entry as an Orkexa price, or ``None`` when it is not a priceable text model of a known provider."""
    provider = LITELLM_PROVIDERS.get(str(entry.get("litellm_provider", "")))
    if provider is None or entry.get("mode") not in _LITELLM_MODES:
        return None
    model = key.split("/", 1)[1] if provider == "gemini" and key.startswith("gemini/") else key
    if "/" in model:
        return None
    rates = _litellm_rates(entry, "")
    tiers = _litellm_tiers(entry)
    if rates is None or tiers is None:
        return None
    options = [TariffOption("default")]
    for suffix, value in _LITELLM_TIERS.items():
        tier_rates = _litellm_rates(entry, f"_{suffix}")
        tier_tiers = _litellm_tiers(entry, f"_{suffix}")
        if tier_rates is None or tier_tiers is None or (tiers and len(tier_tiers) != len(tiers)):
            continue  # unknown or incomplete: that tier stays unpriced (Orkexa reports the cost as unknown)
        options.append(TariffOption(value, rates=tier_rates, long_context=tier_tiers if tiers else None))
    tariffs = (Tariff(dimension="service_tier", default="default", options=tuple(options)),) if len(options) > 1 else ()
    return ModelPrice(
        provider=provider,
        model=model,
        rates=rates,
        long_context=tiers,
        tariffs=tariffs,
        source=LITELLM_SOURCE,
        source_url=url,
        checked_at=checked_at,
        quality="unverified",
    )


@dataclass
class LiteLLMSource:
    """LiteLLM's model cost map: OpenAI, Anthropic and Gemini text models."""

    url: str = LITELLM_URL
    http: HttpSettings = field(default_factory=HttpSettings)
    fetch_bytes: Fetcher = https_get
    sleep: Callable[[float], None] = time.sleep
    allowed_hosts: frozenset[str] = frozenset({"raw.githubusercontent.com"})
    name: str = LITELLM_SOURCE
    #: Entries skipped because Orkexa refused them (logged; the rest is still used).
    skipped: list[str] = field(default_factory=list)

    def fetch(self, at: datetime) -> PriceSnapshot:
        data = fetch_with_retries(
            self.url, self.http, fetch=self.fetch_bytes, sleep=self.sleep, allowed_hosts=self.allowed_hosts
        )
        try:
            document = json.loads(data, parse_float=Decimal)
        except ValueError as exc:
            raise SourceError(f"The LiteLLM cost map is not valid JSON: {exc}") from None
        if not isinstance(document, dict):
            raise SourceError("The LiteLLM cost map is not a JSON object")
        prices: dict[tuple[str, str], ModelPrice] = {}
        self.skipped = []
        for key, entry in document.items():
            if not isinstance(entry, dict) or key == "sample_spec":
                continue
            try:
                price = litellm_price(str(key), entry, checked_at=at, url=self.url)
            except (PricingError, ValueError, TypeError) as exc:
                self.skipped.append(f"{key}: {exc}")
                continue
            if price is not None:
                # The same model listed twice (``gemini/x`` and ``x``): the first entry wins, deterministically.
                prices.setdefault((price.provider, price.model), price)
        if not prices:
            raise SourceError("The LiteLLM cost map contains no usable prices")
        if self.skipped:
            logger.info("Skipped %d LiteLLM entries Orkexa refused", len(self.skipped))
        return PriceSnapshot(
            f"litellm-{at.strftime('%Y%m%dT%H%M%SZ')}",
            tuple(prices[key] for key in sorted(prices)),
            created_at=at,
            source=LITELLM_SOURCE,
            description="LiteLLM model cost map",
        )


# ---- the sync job ----------------------------------------------------------------------------------------


class SourceReport(BaseModel):
    source: str
    ok: bool
    outcome: str | None = None
    version: str | None = None
    error: str | None = None
    changes: int = 0
    held_for_review: int = 0
    invalid: int = 0


class SyncReport(BaseModel):
    started_at: datetime
    finished_at: datetime
    #: ``False`` when nothing was due (and the sync was not forced) or another sync was running.
    ran: bool
    skipped_reason: str | None = None
    sources: list[SourceReport] = Field(default_factory=list)
    active_version: str


def default_sources(*, snapshot_file: Path | None = None, litellm: bool = True) -> list[PriceSource]:
    """The sources in priority order: Orkexa's verified prices, a reviewed file if any, then LiteLLM."""
    sources: list[PriceSource] = [OrkexaBuiltinSource()]
    if snapshot_file is not None:
        sources.append(SnapshotFileSource(snapshot_file))
    if litellm:
        sources.append(LiteLLMSource())
    return sources


class PricingSyncService:
    """Checks the price sources and submits their candidates; one sync at a time.

    ``lock`` guards against concurrent syncs of other processes (a server passes a PostgreSQL advisory lock); it
    returns a context manager that yields ``True`` when it got the lock and ``False`` when another process holds
    it. Within a process a thread lock is always taken.
    """

    def __init__(
        self,
        pricing: PricingService,
        sources: list[PriceSource] | None = None,
        *,
        lock: Callable[[], AbstractContextManager[bool]] | None = None,
        clock: Callable[[], datetime] = now,
    ) -> None:
        self.pricing = pricing
        self.sources = sources if sources is not None else default_sources()
        self._lock = threading.Lock()
        self._external_lock = lock
        self._clock = clock

    def due(self) -> bool:
        """Whether the last successful check is older than the refresh interval (or there was none)."""
        last = self.pricing.state().last_success_at
        return last is None or self._clock() - last >= self.pricing.settings.refresh_interval

    @contextmanager
    def _locked(self) -> Iterator[bool]:
        if not self._lock.acquire(blocking=False):
            yield False
            return
        try:
            with self._external_lock() if self._external_lock is not None else nullcontext(True) as acquired:
                yield bool(acquired)
        finally:
            self._lock.release()

    def sync(self, *, force: bool = False) -> SyncReport:
        """Check every source now (when due, or always with ``force``) and submit what they deliver."""
        started = self._clock()
        if not force and not self.due():
            return self._skipped(started, "not_due")
        with self._locked() as acquired:
            if not acquired:
                return self._skipped(started, "already_running")
            if not force and not self.due():  # another process synced while we waited
                return self._skipped(started, "not_due")
            reports = [self._sync_source(source) for source in self.sources]
            failures = [report for report in reports if not report.ok]
            self.pricing.record_attempt(
                error="; ".join(f"{report.source}: {report.error}" for report in failures) if failures else None
            )
        return SyncReport(
            started_at=started,
            finished_at=self._clock(),
            ran=True,
            sources=reports,
            active_version=self.pricing.reference_snapshot().version,
        )

    def _skipped(self, started: datetime, reason: str) -> SyncReport:
        return SyncReport(
            started_at=started,
            finished_at=self._clock(),
            ran=False,
            skipped_reason=reason,
            active_version=self.pricing.reference_snapshot().version,
        )

    def _sync_source(self, source: PriceSource) -> SourceReport:
        try:
            candidate = source.fetch(self._clock())
            result: SubmitResult = self.pricing.submit(candidate)
        except SourceError as exc:
            logger.warning("Price source %s failed: %s", source.name, exc)
            return SourceReport(source=source.name, ok=False, error=str(exc))
        except Exception as exc:  # a broken source must never stop the others or the active prices
            logger.exception("Price source %s failed unexpectedly", source.name)
            return SourceReport(source=source.name, ok=False, error=f"{type(exc).__name__}: {exc}")
        if result.outcome == "rejected":
            logger.warning("Prices of %s rejected: %s", source.name, result.reason)
        return SourceReport(
            source=source.name,
            ok=result.outcome != "rejected",
            outcome=result.outcome,
            version=result.active_version,
            error=result.reason,
            changes=len(result.changes),
            held_for_review=sum(1 for change in result.changes if change.needs_review),
            invalid=sum(1 for issue in result.issues if issue.severity == "error"),
        )

    async def run(self, stop: asyncio.Event, *, check_every: timedelta = timedelta(hours=1)) -> None:
        """Sync at start when the prices are due, then whenever they become due, until ``stop`` is set.

        Runs the blocking sync in a thread, so the event loop (the API) keeps serving.
        """
        while not stop.is_set():
            if self.due():
                try:
                    await asyncio.to_thread(self.sync)
                except Exception:  # logged; the next round tries again
                    logger.exception("Price sync failed")
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=check_every.total_seconds())


__all__ = [
    "LITELLM_PROVIDERS",
    "LITELLM_SOURCE",
    "LITELLM_URL",
    "HttpSettings",
    "LiteLLMSource",
    "OrkexaBuiltinSource",
    "PriceSource",
    "PricingSyncService",
    "SnapshotFileSource",
    "SourceError",
    "SourceReport",
    "SyncReport",
    "default_sources",
    "fetch_with_retries",
    "https_get",
    "litellm_price",
]
