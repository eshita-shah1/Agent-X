"""Execution-price contract and gate (open decision D4).

Background: Phase 2 research found that Yahoo Finance `Close` values are split-adjusted even
with `auto_adjust=False`, that forming candles are returned, that there is no guaranteed fixed
delay, and that values carry float32 noise. Provider data is therefore stored as
`UNVERIFIED_PROVIDER_NATIVE` and is NOT approved as an execution price.

This module defines:
  * `ExecutionQuote`, the provider-independent observation the exchange may execute against;
  * `ExecutionPriceSource`, the contract a market-data implementation provides (Phase 2 or a
    future provider adapter implements it; the exchange never builds prices itself);
  * `ExecutionPriceGate`, which accepts a quote only if every requirement below holds.

Acceptance requirements (all must hold):
  1. a source is explicitly enabled in configuration;
  2. the quote is for the requested instrument;
  3. timestamps are timezone-aware (UTC instants);
  4. the price is positive;
  5. the observation is final (no forming candles);
  6. it is not from the future relative to the decision time (no lookahead);
  7. it is fresh (age <= EXECUTION_MAX_PRICE_AGE_SECONDS, provisional);
  8. its price basis is verified (never UNVERIFIED_PROVIDER_NATIVE) and explicitly approved;
  9. its provider is explicitly approved (no silent provider switching).

Default configuration enables no source and approves nothing, so every check fails closed.
"""

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.config import Settings
from app.db.enums import PriceBasis, Timeframe
from app.db.models import Candle, DataProvider


class QuoteKind(str, enum.Enum):
    # Close of the most recent *finalized* bar, used as a proxy for the current price.
    # Whether this proxy is acceptable at all is part of decision D4.
    CANDLE_CLOSE = "CANDLE_CLOSE"


@dataclass(frozen=True)
class ExecutionQuote:
    instrument_id: int
    price: Decimal
    as_of: datetime  # data time (bar end for a candle) — UTC instant
    observed_at: datetime  # when Agent-X received the observation
    provider_code: str
    price_basis: PriceBasis
    is_final: bool
    kind: QuoteKind
    source_ref: str  # stable identifier of the observation, e.g. "candle:123"
    volume: int | None = None

    def provenance(self) -> dict:
        return {
            "instrument_id": self.instrument_id,
            "price": str(self.price),
            "as_of": self.as_of.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "provider_code": self.provider_code,
            "price_basis": self.price_basis.value,
            "is_final": self.is_final,
            "kind": self.kind.value,
            "source_ref": self.source_ref,
            "volume": self.volume,
        }


class ExecutionPriceSource(Protocol):
    """Provides the latest observation for an instrument, or None. Must not fabricate data."""

    name: str
    enabled: bool

    def latest(self, instrument_id: int) -> ExecutionQuote | None: ...


class DisabledExecutionPriceSource:
    """The default: no execution-price source has been approved (decision D4 open)."""

    name = "disabled"
    enabled = False

    def latest(self, instrument_id: int) -> ExecutionQuote | None:
        return None


class CandleCloseExecutionPriceSource:
    """Reads the latest *finalized* bar from `candles` for one configured provider.

    It never mixes providers, never reads `adjusted_candles`, never reads a bar that ended or
    was observed after `now`, and never fills gaps. It returns whatever basis label the row
    carries; the gate decides whether that basis is acceptable.
    """

    name = "candle_close"
    enabled = True

    def __init__(self, db: Session, provider_codes: list[str], timeframe: Timeframe, clock: Clock):
        self._db = db
        self._provider_codes = provider_codes
        self._timeframe = timeframe
        self._clock = clock

    def latest(self, instrument_id: int) -> ExecutionQuote | None:
        if len(self._provider_codes) != 1:
            # Choosing among several providers would be silent provider switching.
            return None
        now = self._clock()
        row = self._db.execute(
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
            .order_by(Candle.bar_start_ts.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        candle, provider_code = row
        return ExecutionQuote(
            instrument_id=candle.instrument_id,
            price=candle.close,
            as_of=candle.bar_end_ts,
            observed_at=candle.observed_at,
            provider_code=provider_code,
            price_basis=candle.price_basis,
            is_final=candle.is_final,
            kind=QuoteKind.CANDLE_CLOSE,
            source_ref=f"candle:{candle.id}",
            volume=candle.volume,
        )


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

    def check(self, instrument_id: int) -> PriceCheck:
        def reject(code: PriceRejection, detail: str, quote: ExecutionQuote | None = None) -> PriceCheck:
            return PriceCheck(instrument_id, quote, code, detail)

        if not self.source.enabled:
            return reject(
                PriceRejection.SOURCE_DISABLED,
                "no execution-price source is approved (decision D4); simulated fills are blocked",
            )
        quote = self.source.latest(instrument_id)
        if quote is None:
            return reject(PriceRejection.NO_DATA, f"no finalized observation from source '{self.source.name}'")
        if quote.instrument_id != instrument_id:
            return reject(PriceRejection.INSTRUMENT_MISMATCH, "observation is for a different instrument", quote)
        if quote.as_of.tzinfo is None or quote.observed_at.tzinfo is None:
            return reject(PriceRejection.NAIVE_TIMESTAMP, "observation timestamps must be timezone-aware", quote)
        if quote.price <= 0:
            return reject(PriceRejection.NON_POSITIVE_PRICE, "observation price is not positive", quote)
        if not quote.is_final:
            return reject(PriceRejection.NOT_FINAL, "observation is not finalized (forming candle)", quote)
        now = self._clock()
        if quote.as_of > now or quote.observed_at > now:
            return reject(PriceRejection.FUTURE_TIMESTAMP, "observation is later than the decision time", quote)
        age = now - quote.as_of
        if age > self.policy.max_age:
            return reject(
                PriceRejection.STALE,
                f"observation age {int(age.total_seconds())}s exceeds {int(self.policy.max_age.total_seconds())}s",
                quote,
            )
        if quote.price_basis is PriceBasis.UNVERIFIED_PROVIDER_NATIVE:
            return reject(PriceRejection.UNVERIFIED_BASIS, "price basis is UNVERIFIED_PROVIDER_NATIVE", quote)
        if quote.price_basis not in self.policy.approved_bases:
            return reject(
                PriceRejection.UNAPPROVED_BASIS, f"price basis {quote.price_basis.value} is not approved", quote
            )
        if quote.provider_code not in self.policy.approved_providers:
            return reject(
                PriceRejection.UNAPPROVED_PROVIDER, f"provider '{quote.provider_code}' is not approved", quote
            )
        return PriceCheck(instrument_id, quote, None, "eligible")


def build_price_gate(settings: Settings, db: Session, clock: Clock) -> ExecutionPriceGate:
    source: ExecutionPriceSource
    if settings.execution_price_source == "candle_close":
        source = CandleCloseExecutionPriceSource(
            db, list(settings.execution_approved_providers), settings.execution_candle_timeframe, clock
        )
    else:
        source = DisabledExecutionPriceSource()
    return ExecutionPriceGate(source, ExecutionPricePolicy.from_settings(settings), clock)
