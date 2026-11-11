"""Deterministic fixtures for Phase 4 integration tests.

IMPORTANT: candles created here are SYNTHETIC test fixtures. Where a test needs an executable
price it labels a fixture candle AS_TRADED_VERIFIED with `basis_verified_by` naming the test
fixture. This exercises the execution path; it is NOT evidence that any real provider's data
is verified, and no real provider data is relabelled anywhere.
"""

import itertools
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.config import Settings
from tests.cost_fixtures import FIXTURE_SCHEDULE_PATH
from sqlalchemy import select

from app.db.enums import (
    BatchStatus,
    InstrumentType,
    JobStatus,
    JobType,
    OrderSide,
    OrderSource,
    OrderType,
    PriceBasis,
    Timeframe,
)
from app.db.models import (
    Candle,
    DataIngestionBatch,
    DataIngestionJob,
    DataProvider,
    Instrument,
    RiskSettings,
    User,
    VirtualAccount,
)
from app.exchange.types import OrderIntent
from app.orders.service import OrderService, TradingContext

D = Decimal
NOW = datetime(2026, 1, 5, 5, 0, tzinfo=UTC)  # 10:30 IST, a Monday
IST = ZoneInfo("Asia/Kolkata")
FIXTURE_PROVIDER = "fixture"
FIXTURE_EVIDENCE = "test-fixture: synthetic data, not a provider verification"

_ids = itertools.count(1)


# SYNTHETIC cost schedule chosen for easy hand calculation (tests/fixtures/cost_schedule_synthetic.json). It is not
# real NSE/SEBI/tax/broker data.
FIXTURE_COSTS = dict(exchange_cost_schedule_path=str(FIXTURE_SCHEDULE_PATH))


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def enabled_settings(base: Settings, **over) -> Settings:
    """Verified mode against the fixture provider (fixture bars labelled AS_TRADED_VERIFIED by `make_candle`)."""
    values = dict(
        execution_mode="verified_next_bar_open",
        execution_approval_reference="TEST-FIXTURE-APPROVAL",
        execution_approved_price_bases=[PriceBasis.AS_TRADED_VERIFIED],
        execution_approved_providers=[FIXTURE_PROVIDER],
        **FIXTURE_COSTS,
    )
    values.update(over)
    return base.model_copy(update=values)


def simulation_settings(base: Settings, **over) -> Settings:
    """Simulation-only mode: accepts only UNVERIFIED_PROVIDER_NATIVE bars (as Yahoo data is archived)."""
    values = dict(
        execution_mode="simulation_provider_native",
        execution_approval_reference="TEST-FIXTURE-SIMULATION-APPROVAL",
        execution_approved_price_bases=[],
        execution_approved_providers=[FIXTURE_PROVIDER],
        **FIXTURE_COSTS,
    )
    values.update(over)
    return base.model_copy(update=values)


def make_provider(db: Session, code: str = FIXTURE_PROVIDER) -> DataProvider:
    p = DataProvider(code=code, name=f"{code} (test fixture)")
    db.add(p)
    db.commit()
    return p


def make_instrument(db: Session, symbol: str = "TESTCO", **over) -> Instrument:
    fields = dict(symbol=symbol, exchange="NSE", instrument_type=InstrumentType.EQUITY, timezone="Asia/Kolkata")
    fields.update(over)
    inst = Instrument(**fields)
    db.add(inst)
    db.commit()
    return inst


def _batch(db: Session, provider: DataProvider, instrument: Instrument) -> DataIngestionBatch:
    """Phase 2 requires every candle to reference the ingestion batch that produced it."""
    batch = db.scalar(
        select(DataIngestionBatch).where(
            DataIngestionBatch.provider_id == provider.id, DataIngestionBatch.instrument_id == instrument.id
        )
    )
    if batch is None:
        job = DataIngestionJob(
            job_type=JobType.BACKFILL, status=JobStatus.SUCCEEDED, provider_id=provider.id, timeframe=Timeframe.M5
        )
        db.add(job)
        db.flush()
        batch = DataIngestionBatch(
            job_id=job.id,
            instrument_id=instrument.id,
            provider_id=provider.id,
            timeframe=Timeframe.M5,
            provider_symbol=f"{instrument.symbol}.FIXTURE",
            request_start=NOW - timedelta(days=1),
            request_end=NOW,
            status=BatchStatus.SUCCEEDED,
        )
        db.add(batch)
        db.flush()
    return batch


def make_candle(
    db: Session,
    provider: DataProvider,
    instrument: Instrument,
    close: str,
    end: datetime = NOW - timedelta(minutes=5),
    *,
    verified: bool = True,
    volume: int = 100_000,
    observed_delay: timedelta = timedelta(seconds=30),
    version: int = 1,
    open: str | None = None,
    high: str | None = None,
    low: str | None = None,
) -> Candle:
    """A synthetic, final (Phase 2 archives only final bars) candle. version > 1 is a non-canonical restatement."""
    c = D(close)
    o = D(open) if open is not None else c
    h = D(high) if high is not None else max(o, c)
    lo = D(low) if low is not None else min(o, c)
    batch = _batch(db, provider, instrument)
    candle = Candle(
        instrument_id=instrument.id,
        provider_id=provider.id,
        timeframe=Timeframe.M5,
        bar_start_ts=end - timedelta(minutes=5),
        bar_end_ts=end,
        session_date=(end - timedelta(minutes=5)).astimezone(IST).date(),
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=volume,
        is_final=True,
        observed_at=end + observed_delay,
        price_basis=PriceBasis.AS_TRADED_VERIFIED if verified else PriceBasis.UNVERIFIED_PROVIDER_NATIVE,
        basis_verified_by=FIXTURE_EVIDENCE if verified else None,
        provider_symbol=batch.provider_symbol,
        ingestion_batch_id=batch.id,
        observation_version=version,
        is_canonical=version == 1,
    )
    db.add(candle)
    db.commit()
    return candle


def make_account(db: Session, capital: str = "100000.00") -> VirtualAccount:
    user = User(email=f"trader{next(_ids)}-{uuid.uuid4().hex[:6]}@example.com", password_hash="x")
    db.add(user)
    db.flush()
    account = VirtualAccount(user_id=user.id, currency="INR", initial_capital=D(capital))
    db.add(account)
    db.commit()
    return account


def configure_risk(db: Session, account: VirtualAccount, **over) -> RiskSettings:
    fields = dict(
        max_risk_per_trade_pct=D("1"),
        max_position_allocation_pct=D("20"),
        max_portfolio_exposure_pct=D("100"),
        max_daily_loss_pct=D("5"),
        max_drawdown_pct=D("10"),
        max_position_quantity=None,
        resize_allowed=True,
    )
    fields.update(over)
    s = RiskSettings(user_id=account.user_id, **fields)
    db.add(s)
    db.commit()
    return s


def intent(instrument: Instrument, side=OrderSide.BUY, qty=10, otype=OrderType.MARKET, **over) -> OrderIntent:
    fields = dict(
        client_order_id=f"t-{uuid.uuid4().hex[:12]}",
        instrument_id=instrument.id,
        side=side,
        order_type=otype,
        quantity=qty,
        source=OrderSource.MANUAL,
        stop_loss_price=D("95") if side is OrderSide.BUY else None,
    )
    fields.update(over)
    return OrderIntent(**fields)


def submit(db: Session, settings: Settings, clock, account: VirtualAccount, order_intent: OrderIntent):
    result = OrderService(TradingContext.build(db, settings, clock)).submit(account.user_id, order_intent)
    db.commit()
    return result


def next_bar(
    db: Session, provider: DataProvider, instrument: Instrument, clock: Clock, open: str, close: str | None = None,
    *, high: str | None = None, low: str | None = None, volume: int = 100_000, version: int = 1, verified: bool = True,
) -> Candle:
    """Archive the next 5-minute bar starting at or after `clock.now` (aligned to 5 minutes), observed at its end,
    and move the clock to its end, so consecutive calls produce a contiguous archive (as a real one is)."""
    minute = (clock.now.minute // 5) * 5
    start = clock.now.replace(minute=minute, second=0, microsecond=0)
    if start < clock.now:
        start += timedelta(minutes=5)
    end = start + timedelta(minutes=5)
    candle = make_candle(
        db, provider, instrument, close or open, end=end, open=open, high=high, low=low, volume=volume,
        version=version, verified=verified, observed_delay=timedelta(0),
    )
    clock.now = end
    return candle


def process(db: Session, settings: Settings, clock, account: VirtualAccount):
    ctx = TradingContext.build(db, settings, clock)
    changed = ctx.exchange.process_open_orders(account)
    db.commit()
    return changed
