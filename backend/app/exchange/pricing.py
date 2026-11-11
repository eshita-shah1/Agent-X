"""Execution-price contract and gate (open decision D4).

Background: Phase 2 research found that Yahoo Finance `Close` values are split-adjusted even
with `auto_adjust=False`, that forming candles are returned, that there is no guaranteed fixed
delay, and that values carry float32 noise. Provider data is therefore stored as
`UNVERIFIED_PROVIDER_NATIVE` and is NOT approved as an execution price.

Two different uses of market data, deliberately separated:

  * **Reference price** (`ExecutionQuote`, from `latest`): the close of the latest finalized bar. It
    is used for decisions, risk sizing, cash reservation, marks and protective-exit triggers. It must
    be fresh. It is **never** a fill price.
  * **Fill bars** (`ExecutionBar`, from `bars_from`): finalized bars that start at or after the
    moment an order was accepted. Orders fill at the *open* of such a bar (or at their limit, when a
    limit was crossed inside the bar). Filling at the close of the bar a decision was based on would
    be a same-bar lookahead; this model makes that impossible.

Acceptance requirements for any observation (reference or fill bar):
  1. a source is explicitly enabled in configuration;
  2. it is for the requested instrument;
  3. timestamps are timezone-aware (UTC instants);
  4. prices are positive;
  5. it is final (no forming candles) and canonical (Phase 2: first observation of a bar);
  6. it is not later than the decision time (bar ended and was observed by `now`): no lookahead;
  7. its price basis is verified (never UNVERIFIED_PROVIDER_NATIVE) and explicitly approved;
  8. its provider is explicitly approved and unique (no silent provider switching).
Reference prices additionally must be fresh (age <= EXECUTION_MAX_PRICE_AGE_SECONDS, provisional).

Default configuration enables no source and approves nothing, so every check fails closed.
"""

import enum
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.config import Settings
from app.db.enums import PriceBasis, Timeframe
from app.db.models import Candle, DataProvider


class QuoteKind(str, enum.Enum):
    LATEST_BAR_CLOSE = "LATEST_BAR_CLOSE"  # reference price only, never a fill price


class FillKind(str, enum.Enum):
    NEXT_BAR_OPEN = "NEXT_BAR_OPEN"  # filled at the open of a bar that started after acceptance
    BAR_LIMIT_CROSS = "BAR_LIMIT_CROSS"  # a resting limit was crossed inside the bar; filled at the limit


@dataclass(frozen=True)
class ExecutionQuote:
    instrument_id: int
    price: Decimal  # close of the latest finalized bar
    as_of: datetime  # tz-aware UTC data time (bar end)
    observed_at: datetime  # tz-aware UTC time Agent-X received it
    provider_code: str
    price_basis: PriceBasis
    is_final: bool
    kind: QuoteKind
    source_ref: str  # stable observation id, e.g. "candle:123"
    volume: int | None = None
    low: Decimal | None = None  # bar range, used for protective-exit triggers
    high: Decimal | None = None

    def provenance(self) -> dict:
        return {
            "instrument_id": self.instrument_id,
            "price": str(self.price),
            "low": str(self.low) if self.low is not None else None,
            "high": str(self.high) if self.high is not None else None,
            "as_of": self.as_of.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "provider_code": self.provider_code,
            "price_basis": self.price_basis.value,
            "is_final": self.is_final,
            "kind": self.kind.value,
            "source_ref": self.source_ref,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class ExecutionBar:
    instrument_id: int
    bar_start: datetime  # tz-aware UTC
    bar_end: datetime
    session_date: date  # exchange-local trading date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    observed_at: datetime
    provider_code: str
    price_basis: PriceBasis
    is_final: bool
    source_ref: str


class ExecutionPriceSource(Protocol):
    """Market-data contract consumed by the exchange. Implementations must not fabricate data."""

    name: str
    enabled: bool

    def latest(self, instrument_id: int) -> ExecutionQuote | None: ...

    def bars_from(self, instrument_id: int, start: datetime) -> list[ExecutionBar]:
        """Finalized bars with bar_start >= start that ended and were observed by now, oldest first."""
        ...


class DisabledExecutionPriceSource:
    """The default: no execution-price source has been approved (decision D4 open)."""

    name = "disabled"
    enabled = False

    def latest(self, instrument_id: int) -> ExecutionQuote | None:
        return None

    def bars_from(self, instrument_id: int, start: datetime) -> list[ExecutionBar]:
        return []


class CandleBarSource:
    """Reads Phase 2's `candles` archive for exactly one configured provider.

    Only final, canonical bars of the configured timeframe that ended and were observed by `now`.
    It never mixes providers, never reads `adjusted_candles` and never fills gaps. It returns whatever
    basis label a row carries; the gate decides whether that basis is acceptable.
    """

    name = "next_bar_open"
    enabled = True
    MAX_BARS = 500

    def __init__(self, db: Session, provider_codes: list[str], timeframe: Timeframe, clock: Clock):
        self._db = db
        self._provider_codes = provider_codes
        self._timeframe = timeframe
        self._clock = clock

    def _query(self, instrument_id: int):
        now = self._clock()
        return (
            select(Candle, DataProvider.code)
            .join(DataProvider, DataProvider.id == Candle.provider_id)
            .where(
                Candle.instrument_id == instrument_id,
                DataProvider.code == self._provider_codes[0],
                Candle.timeframe == self._timeframe,
                Candle.is_final.is_(True),
                # Phase 2 restatement policy: only the first (canonical) observation of a bar is used.
                Candle.is_canonical.is_(True),
                Candle.bar_end_ts <= now,
                Candle.observed_at <= now,
            )
        )

    def latest(self, instrument_id: int) -> ExecutionQuote | None:
        if len(self._provider_codes) != 1:
            return None  # choosing among several providers would be silent provider switching
        row = self._db.execute(self._query(instrument_id).order_by(Candle.bar_start_ts.desc()).limit(1)).first()
        if row is None:
            return None
        c, code = row
        return ExecutionQuote(
            instrument_id=c.instrument_id,
            price=c.close,
            as_of=c.bar_end_ts,
            observed_at=c.observed_at,
            provider_code=code,
            price_basis=c.price_basis,
            is_final=c.is_final,
            kind=QuoteKind.LATEST_BAR_CLOSE,
            source_ref=f"candle:{c.id}",
            volume=c.volume,
            low=c.low,
            high=c.high,
        )

    def bars_from(self, instrument_id: int, start: datetime) -> list[ExecutionBar]:
        if len(self._provider_codes) != 1:
            return []
        rows = self._db.execute(
            self._query(instrument_id)
            .where(Candle.bar_start_ts >= start)
            .order_by(Candle.bar_start_ts.asc())
            .limit(self.MAX_BARS)
        ).all()
        return [
            ExecutionBar(
                instrument_id=c.instrument_id,
                bar_start=c.bar_start_ts,
                bar_end=c.bar_end_ts,
                session_date=c.session_date,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                observed_at=c.observed_at,
                provider_code=code,
                price_basis=c.price_basis,
                is_final=c.is_final,
                source_ref=f"candle:{c.id}",
            )
            for c, code in rows
        ]


class PriceRejection(str, enum.Enum):
    SOURCE_DISABLED = "SOURCE_DISABLED"
    NO_DATA = "NO_DATA"
    INSTRUMENT_MISMATCH = "INSTRUMENT_MISMATCH"
    NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    NOT_FINAL = "NOT_FINAL"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    STALE = "STALE"
    UNVERIFIED_BASIS = "UNVERIFIED_BASIS"
    UNAPPROVED_BASIS = "UNAPPROVED_BASIS"
    UNAPPROVED_PROVIDER = "UNAPPROVED_PROVIDER"


@dataclass(frozen=True)
class PriceCheck:
    instrument_id: int
    quote: ExecutionQuote | None
    rejection: PriceRejection | None
    detail: str

    @property
    def ok(self) -> bool:
        return self.rejection is None and self.quote is not None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "rejection": self.rejection.value if self.rejection else None,
            "detail": self.detail,
            "quote": self.quote.provenance() if self.quote else None,
        }


@dataclass(frozen=True)
class BarCheck:
    """Eligible fill bars (oldest first). If a bar failed a check, iteration stopped there: later bars are
    never used to skip over an ineligible one, and `blocked_by` says why."""

    bars: list[ExecutionBar]
    blocked_by: PriceRejection | None
    detail: str


@dataclass(frozen=True)
class ExecutionPricePolicy:
    approved_bases: frozenset[PriceBasis]
    approved_providers: frozenset[str]
    max_age: timedelta

    @classmethod
    def from_settings(cls, settings: Settings) -> "ExecutionPricePolicy":
        return cls(
            approved_bases=frozenset(settings.execution_approved_price_bases),
            approved_providers=frozenset(settings.execution_approved_providers),
            max_age=timedelta(seconds=settings.execution_max_price_age_seconds),
        )


class ExecutionPriceGate:
    def __init__(self, source: ExecutionPriceSource, policy: ExecutionPricePolicy, clock: Clock):
        self.source = source
        self.policy = policy
        self._clock = clock

    def _common(
        self, instrument_id: int, *, obs_instrument: int, data_times: tuple[datetime, ...], observed_at: datetime,
        prices: tuple[Decimal, ...], is_final: bool, basis: PriceBasis, provider: str,
    ) -> tuple[PriceRejection, str] | None:
        if obs_instrument != instrument_id:
            return PriceRejection.INSTRUMENT_MISMATCH, "observation is for a different instrument"
        if any(t.tzinfo is None for t in (*data_times, observed_at)):
            return PriceRejection.NAIVE_TIMESTAMP, "observation timestamps must be timezone-aware"
        if any(p <= 0 for p in prices):
            return PriceRejection.NON_POSITIVE_PRICE, "observation price is not positive"
        if not is_final:
            return PriceRejection.NOT_FINAL, "observation is not finalized (forming candle)"
        now = self._clock()
        if any(t > now for t in data_times) or observed_at > now:
            return PriceRejection.FUTURE_TIMESTAMP, "observation is later than the decision time"
        if basis is PriceBasis.UNVERIFIED_PROVIDER_NATIVE:
            return PriceRejection.UNVERIFIED_BASIS, "price basis is UNVERIFIED_PROVIDER_NATIVE"
        if basis not in self.policy.approved_bases:
            return PriceRejection.UNAPPROVED_BASIS, f"price basis {basis.value} is not approved"
        if provider not in self.policy.approved_providers:
            return PriceRejection.UNAPPROVED_PROVIDER, f"provider '{provider}' is not approved"
        return None

    def check(self, instrument_id: int) -> PriceCheck:
        """Reference price for decisions, sizing and marks (never a fill price)."""
        if not self.source.enabled:
            return PriceCheck(
                instrument_id, None, PriceRejection.SOURCE_DISABLED,
                "no execution-price source is approved (decision D4); simulated fills are blocked",
            )
        q = self.source.latest(instrument_id)
        if q is None:
            return PriceCheck(
                instrument_id, None, PriceRejection.NO_DATA, f"no finalized observation from source '{self.source.name}'"
            )
        problem = self._common(
            instrument_id, obs_instrument=q.instrument_id, data_times=(q.as_of,), observed_at=q.observed_at,
            prices=(q.price,), is_final=q.is_final, basis=q.price_basis, provider=q.provider_code,
        )
        if problem is None:
            age = self._clock() - q.as_of
            if age > self.policy.max_age:
                problem = (
                    PriceRejection.STALE,
                    f"observation age {int(age.total_seconds())}s exceeds {int(self.policy.max_age.total_seconds())}s",
                )
        if problem:
            return PriceCheck(instrument_id, q, problem[0], problem[1])
        return PriceCheck(instrument_id, q, None, "eligible")

    def fill_bars(self, instrument_id: int, start: datetime) -> BarCheck:
        """Bars an order accepted at `start` may fill against, oldest first."""
        if not self.source.enabled:
            return BarCheck([], PriceRejection.SOURCE_DISABLED, "no execution-price source is approved (decision D4)")
        eligible: list[ExecutionBar] = []
        for bar in self.source.bars_from(instrument_id, start):
            if bar.bar_start < start:
                return BarCheck(eligible, PriceRejection.FUTURE_TIMESTAMP, "bar started before the order was accepted")
            problem = self._common(
                instrument_id, obs_instrument=bar.instrument_id, data_times=(bar.bar_start, bar.bar_end),
                observed_at=bar.observed_at, prices=(bar.open, bar.high, bar.low, bar.close), is_final=bar.is_final,
                basis=bar.price_basis, provider=bar.provider_code,
            )
            if problem:
                return BarCheck(eligible, problem[0], f"{bar.source_ref}: {problem[1]}")
            eligible.append(bar)
        return BarCheck(eligible, None, "eligible")


def build_price_gate(settings: Settings, db: Session, clock: Clock) -> ExecutionPriceGate:
    source: ExecutionPriceSource
    if settings.execution_price_source == "next_bar_open":
        source = CandleBarSource(db, list(settings.execution_approved_providers), settings.execution_candle_timeframe, clock)
    else:
        source = DisabledExecutionPriceSource()
    return ExecutionPriceGate(source, ExecutionPricePolicy.from_settings(settings), clock)
