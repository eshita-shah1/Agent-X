"""Execution-price contract and gate (decision D4).

Background: Phase 2 research found that Yahoo Finance `Close` values are split-adjusted even with
`auto_adjust=False`; a probe on 2026-11-11 (docs/evidence/) showed daily Open, High, Low, Close and Volume are all
back-adjusted across 2:1 corporate actions. Forming candles are returned, there is no guaranteed fixed delay, and
values carry float32 noise. Provider data is therefore stored as `UNVERIFIED_PROVIDER_NATIVE` and is never
relabelled as verified.

Execution modes (`EXECUTION_MODE`):
  * `disabled` (default): nothing is eligible; no order can fill.
  * `verified_next_bar_open`: only bars whose basis was verified with evidence and approved.
  * `simulation_provider_native`: only `UNVERIFIED_PROVIDER_NATIVE` bars (e.g. Yahoo). Fills are a retrospective
    *simulation* and are recorded with `ExecutionPolicy.SIMULATION_PROVIDER_NATIVE`; they are never presented as
    verified executions. The two modes never mix: each accepts only its own basis.

Two different uses of market data, deliberately separated:
  * **Reference price** (`ExecutionQuote`, from `latest`): the close of the latest finalized bar. Used for
    decisions, risk sizing, cash reservation, marks and protective-exit triggers. Must be fresh. **Never** a fill price.
  * **Fill bars** (`ExecutionBar`, from `bars_from`): finalized bars that start at or after the moment an order was
    accepted. Orders fill at the *open* of such a bar (or at their limit, when a limit was crossed inside the bar).

Acceptance requirements for any observation (reference or fill bar):
  1. execution is enabled and its configuration passes every safeguard (`Settings.execution_config_problems`);
  2. it is for the requested instrument;
  3. timestamps are timezone-aware (UTC instants);
  4. prices are positive and OHLC-consistent; volume is non-negative;
  5. it is final and canonical (Phase 2: first observation of a bar);
  6. it ended and was observed by the decision time: no lookahead;
  7. its basis matches the mode (verified + approved, or exactly UNVERIFIED_PROVIDER_NATIVE for simulation);
  8. its provider is the single approved provider (no silent provider switching);
  9. integrity (database-backed, `BarIntegrity`): no WARNING/ERROR data-quality issue recorded for the bar
     (e.g. a restatement or conflicting duplicate); corporate actions were available when the bar's batch was
     ingested; and no split/bonus/other corporate action has an ex-date after the bar's session but on or before the
     day it was observed (such a bar may already be provider-adjusted: ambiguous basis).
Reference prices must also be fresh (age <= EXECUTION_MAX_PRICE_AGE_SECONDS, provisional). Fill bars must also have
non-zero volume.
"""

import enum
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.config import Settings
from app.db.enums import CorporateActionType, PriceBasis, Timeframe
from app.db.models import Candle, CorporateAction, DataProvider, DataQualityIssue
from app.marketdata.types import IssueType, Severity

EXCHANGE_TZ = ZoneInfo("Asia/Kolkata")


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
    open: Decimal | None = None
    bar_start: datetime | None = None
    session_date: date | None = None
    ingestion_batch_id: int | None = None

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
    ingestion_batch_id: int | None = None


class ExecutionPriceSource(Protocol):
    """Market-data contract consumed by the exchange. Implementations must not fabricate data."""

    name: str
    enabled: bool

    def latest(self, instrument_id: int) -> ExecutionQuote | None: ...

    def bars_from(self, instrument_id: int, start: datetime) -> list[ExecutionBar]:
        """Finalized bars with bar_start >= start that ended and were observed by now, oldest first."""
        ...


class DisabledExecutionPriceSource:
    """No execution mode is enabled, or the configuration failed a safeguard."""

    name = "disabled"
    enabled = False

    def __init__(
        self, reason: str = "no execution mode is enabled (decision D4); simulated fills are blocked",
        config_invalid: bool = False,
    ):
        self.reason = reason
        self.config_invalid = config_invalid

    def latest(self, instrument_id: int) -> ExecutionQuote | None:
        return None

    def bars_from(self, instrument_id: int, start: datetime) -> list[ExecutionBar]:
        return []


class CandleBarSource:
    """Reads Phase 2's `candles` archive for exactly one configured provider.

    Only final, canonical bars of the configured timeframe that ended and were observed by `now`. It never mixes
    providers, never reads `adjusted_candles` and never fills gaps. It returns whatever basis label a row carries;
    the gate decides whether that basis is acceptable for the active mode.
    """

    name = "candles_next_bar_open"
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
            instrument_id=c.instrument_id, price=c.close, as_of=c.bar_end_ts, observed_at=c.observed_at,
            provider_code=code, price_basis=c.price_basis, is_final=c.is_final, kind=QuoteKind.LATEST_BAR_CLOSE,
            source_ref=f"candle:{c.id}", volume=c.volume, low=c.low, high=c.high, open=c.open,
            bar_start=c.bar_start_ts, session_date=c.session_date, ingestion_batch_id=c.ingestion_batch_id,
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
                instrument_id=c.instrument_id, bar_start=c.bar_start_ts, bar_end=c.bar_end_ts,
                session_date=c.session_date, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume,
                observed_at=c.observed_at, provider_code=code, price_basis=c.price_basis, is_final=c.is_final,
                source_ref=f"candle:{c.id}", ingestion_batch_id=c.ingestion_batch_id,
            )
            for c, code in rows
        ]


class PriceRejection(str, enum.Enum):
    SOURCE_DISABLED = "SOURCE_DISABLED"
    CONFIG_INVALID = "CONFIG_INVALID"
    NO_DATA = "NO_DATA"
    INSTRUMENT_MISMATCH = "INSTRUMENT_MISMATCH"
    NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    INVALID_OHLC = "INVALID_OHLC"
    INVALID_VOLUME = "INVALID_VOLUME"
    ZERO_VOLUME = "ZERO_VOLUME"
    NOT_FINAL = "NOT_FINAL"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    STALE = "STALE"
    UNVERIFIED_BASIS = "UNVERIFIED_BASIS"
    UNAPPROVED_BASIS = "UNAPPROVED_BASIS"
    BASIS_NOT_SIMULATION = "BASIS_NOT_SIMULATION"
    UNAPPROVED_PROVIDER = "UNAPPROVED_PROVIDER"
    DATA_QUALITY_ISSUE = "DATA_QUALITY_ISSUE"
    CORPORATE_ACTIONS_UNAVAILABLE = "CORPORATE_ACTIONS_UNAVAILABLE"
    CORPORATE_ACTION_AFTER_BAR = "CORPORATE_ACTION_AFTER_BAR"
    CORPORATE_ACTION_UNADJUSTED_POSITION = "CORPORATE_ACTION_UNADJUSTED_POSITION"


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
    mode: str  # "disabled" | "verified_next_bar_open" | "simulation_provider_native"
    approved_bases: frozenset[PriceBasis]
    approved_providers: frozenset[str]
    max_age: timedelta

    @classmethod
    def from_settings(cls, settings: Settings) -> "ExecutionPricePolicy":
        return cls(
            mode=settings.execution_mode,
            approved_bases=frozenset(settings.execution_approved_price_bases),
            approved_providers=frozenset(settings.execution_approved_providers),
            max_age=timedelta(seconds=settings.execution_max_price_age_seconds),
        )


# Corporate actions that change the price scale of a share. Cash dividends do not (the price drop is real).
_SCALE_CHANGING_ACTIONS = (CorporateActionType.SPLIT, CorporateActionType.BONUS, CorporateActionType.OTHER)
_BLOCKING_SEVERITIES = (Severity.WARNING, Severity.ERROR)


def session_date_of(ts: datetime) -> date:
    return ts.astimezone(EXCHANGE_TZ).date()


def scale_changing_actions_between(db: Session, instrument_id: int, after: date, upto: date) -> list[CorporateAction]:
    """Split/bonus/other corporate actions with after < ex_date <= upto."""
    if upto <= after:
        return []
    return list(
        db.scalars(
            select(CorporateAction).where(
                CorporateAction.instrument_id == instrument_id,
                CorporateAction.action_type.in_(_SCALE_CHANGING_ACTIONS),
                CorporateAction.ex_date > after,
                CorporateAction.ex_date <= upto,
            ).order_by(CorporateAction.ex_date)
        )
    )


class BarIntegrity:
    """Database-backed integrity checks on archived bars (requirement 9 of the D4 simulation decision)."""

    def __init__(self, db: Session):
        self._db = db

    def problem(
        self, instrument_id: int, bar_start: datetime, session_date: date | None, observed_at: datetime,
        batch_id: int | None,
    ) -> tuple[PriceRejection, str] | None:
        issue = self._db.scalar(
            select(DataQualityIssue.issue_type).where(
                DataQualityIssue.instrument_id == instrument_id,
                DataQualityIssue.bar_start_ts == bar_start,
                DataQualityIssue.severity.in_(_BLOCKING_SEVERITIES),
            ).limit(1)
        )
        if issue is not None:
            return PriceRejection.DATA_QUALITY_ISSUE, f"bar has a recorded {issue.value} issue"
        if batch_id is not None and self._db.scalar(
            select(
                exists().where(
                    DataQualityIssue.batch_id == batch_id,
                    DataQualityIssue.issue_type == IssueType.CORPORATE_ACTIONS_UNAVAILABLE,
                )
            )
        ):
            return (
                PriceRejection.CORPORATE_ACTIONS_UNAVAILABLE,
                "corporate actions could not be fetched when this bar was ingested; its basis cannot be established",
            )
        bar_session = session_date or session_date_of(bar_start)
        actions = scale_changing_actions_between(self._db, instrument_id, bar_session, session_date_of(observed_at))
        if actions:
            a = actions[0]
            return (
                PriceRejection.CORPORATE_ACTION_AFTER_BAR,
                f"{a.action_type.value} ex-date {a.ex_date} falls after the bar's session and on or before it was "
                "observed; the provider may already have adjusted it",
            )
        return None


class ExecutionPriceGate:
    def __init__(
        self, source: ExecutionPriceSource, policy: ExecutionPricePolicy, clock: Clock, integrity: BarIntegrity | None = None,
    ):
        self.source = source
        self.policy = policy
        self._clock = clock
        self._integrity = integrity

    def _basis_problem(self, basis: PriceBasis) -> tuple[PriceRejection, str] | None:
        if self.policy.mode == "simulation_provider_native":
            if basis is not PriceBasis.UNVERIFIED_PROVIDER_NATIVE:
                return (
                    PriceRejection.BASIS_NOT_SIMULATION,
                    f"simulation mode accepts only UNVERIFIED_PROVIDER_NATIVE, not {basis.value}",
                )
            return None
        if basis is PriceBasis.UNVERIFIED_PROVIDER_NATIVE:
            return PriceRejection.UNVERIFIED_BASIS, "price basis is UNVERIFIED_PROVIDER_NATIVE"
        if basis not in self.policy.approved_bases:
            return PriceRejection.UNAPPROVED_BASIS, f"price basis {basis.value} is not approved"
        return None

    def _common(
        self, instrument_id: int, *, obs_instrument: int, data_times: tuple[datetime, ...], observed_at: datetime,
        ohlc: tuple[Decimal | None, Decimal | None, Decimal | None, Decimal], volume: int | None, is_final: bool,
        basis: PriceBasis, provider: str, bar_start: datetime | None, session_date: date | None, batch_id: int | None,
    ) -> tuple[PriceRejection, str] | None:
        if obs_instrument != instrument_id:
            return PriceRejection.INSTRUMENT_MISMATCH, "observation is for a different instrument"
        if any(t.tzinfo is None for t in (*data_times, observed_at)):
            return PriceRejection.NAIVE_TIMESTAMP, "observation timestamps must be timezone-aware"
        o, h, low, c = ohlc
        prices = [p for p in (o, h, low, c) if p is not None]
        if any(p <= 0 for p in prices):
            return PriceRejection.NON_POSITIVE_PRICE, "observation price is not positive"
        if None not in (o, h, low) and not (h >= low and h >= o and h >= c and low <= o and low <= c):
            return PriceRejection.INVALID_OHLC, "OHLC relationships violated"
        if volume is not None and volume < 0:
            return PriceRejection.INVALID_VOLUME, "volume is negative"
        if not is_final:
            return PriceRejection.NOT_FINAL, "observation is not finalized (forming candle)"
        now = self._clock()
        if any(t > now for t in data_times) or observed_at > now:
            return PriceRejection.FUTURE_TIMESTAMP, "observation is later than the decision time"
        basis_problem = self._basis_problem(basis)
        if basis_problem:
            return basis_problem
        if provider not in self.policy.approved_providers:
            return PriceRejection.UNAPPROVED_PROVIDER, f"provider '{provider}' is not approved"
        if self._integrity is not None and bar_start is not None:
            return self._integrity.problem(instrument_id, bar_start, session_date, observed_at, batch_id)
        return None

    def _disabled(self) -> tuple[PriceRejection, str]:
        reason = getattr(self.source, "reason", "no execution mode is enabled (decision D4)")
        invalid = getattr(self.source, "config_invalid", False)
        return (PriceRejection.CONFIG_INVALID if invalid else PriceRejection.SOURCE_DISABLED), reason

    def check(self, instrument_id: int) -> PriceCheck:
        """Reference price for decisions, sizing and marks (never a fill price)."""
        if not self.source.enabled:
            code, reason = self._disabled()
            return PriceCheck(instrument_id, None, code, reason)
        q = self.source.latest(instrument_id)
        if q is None:
            return PriceCheck(
                instrument_id, None, PriceRejection.NO_DATA, f"no finalized observation from source '{self.source.name}'"
            )
        problem = self._common(
            instrument_id, obs_instrument=q.instrument_id, data_times=(q.as_of,), observed_at=q.observed_at,
            ohlc=(q.open, q.high, q.low, q.price), volume=q.volume, is_final=q.is_final, basis=q.price_basis,
            provider=q.provider_code, bar_start=q.bar_start, session_date=q.session_date, batch_id=q.ingestion_batch_id,
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
            code, reason = self._disabled()
            return BarCheck([], code, reason)
        eligible: list[ExecutionBar] = []
        for bar in self.source.bars_from(instrument_id, start):
            if bar.bar_start < start:
                return BarCheck(eligible, PriceRejection.FUTURE_TIMESTAMP, "bar started before the order was accepted")
            problem = self._common(
                instrument_id, obs_instrument=bar.instrument_id, data_times=(bar.bar_start, bar.bar_end),
                observed_at=bar.observed_at, ohlc=(bar.open, bar.high, bar.low, bar.close), volume=bar.volume,
                is_final=bar.is_final, basis=bar.price_basis, provider=bar.provider_code, bar_start=bar.bar_start,
                session_date=bar.session_date, batch_id=bar.ingestion_batch_id,
            )
            if problem is None and bar.volume == 0:
                problem = (PriceRejection.ZERO_VOLUME, "bar has zero volume; no simulated fill is possible on it")
            if problem:
                return BarCheck(eligible, problem[0], f"{bar.source_ref}: {problem[1]}")
            eligible.append(bar)
        return BarCheck(eligible, None, "eligible")


def build_price_gate(settings: Settings, db: Session, clock: Clock) -> ExecutionPriceGate:
    policy = ExecutionPricePolicy.from_settings(settings)
    source: ExecutionPriceSource
    if settings.execution_mode == "disabled":
        source = DisabledExecutionPriceSource()
    elif problems := settings.execution_config_problems(session_date_of(clock())):
        source = DisabledExecutionPriceSource("; ".join(problems), config_invalid=True)
    else:
        source = CandleBarSource(db, list(settings.execution_approved_providers), settings.execution_candle_timeframe, clock)
    return ExecutionPriceGate(source, policy, clock, BarIntegrity(db))
