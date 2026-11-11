"""Read-only loading of archived candles and corporate actions into the engine's input types.

Only canonical, finalized 5-minute candles of the configured active provider are read, unadjusted and labelled
UNVERIFIED_PROVIDER_NATIVE. Nothing is written and no provider is contacted. Each bar carries
(candle id, observation version) as its source reference, so the data fingerprint pins the exact rows used.
"""

from __future__ import annotations

from datetime import UTC, date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtest import PRICE_BASIS, BacktestBar, BacktestRefused, CorporateActionRecord, InstrumentData, RefusalCode
from app.core.config import Settings
from app.db.enums import InstrumentType, Timeframe, TradingStatus
from app.strategies.configs import lookback_days  # shared with the agent (decision D13); re-exported
from app.db.models import Candle, CorporateAction, DataProvider, Instrument


def check_instrument(session: Session, instrument_id: int) -> Instrument:
    inst = session.get(Instrument, instrument_id)
    if inst is None:
        raise BacktestRefused(RefusalCode.INVALID_CONFIGURATION, "unknown instrument", instrument_id)
    if inst.instrument_type is not InstrumentType.EQUITY:
        raise BacktestRefused(RefusalCode.INVALID_CONFIGURATION, f"{inst.symbol} is not an equity (futures are not supported)", instrument_id)
    if inst.trading_status is not TradingStatus.ENABLED:
        raise BacktestRefused(RefusalCode.INVALID_CONFIGURATION, f"{inst.symbol} is disabled: {inst.disabled_reason}", instrument_id)
    return inst


def _detail(a: CorporateAction) -> str:
    parts = [a.source]
    if a.ratio_numerator is not None:
        parts.append(f"ratio {a.ratio_numerator}:{a.ratio_denominator}")
    if a.cash_amount is not None:
        parts.append(f"cash {a.cash_amount}")
    return "; ".join(parts)


def load_instrument_data(session: Session, settings: Settings, instrument_id: int, start: date, end: date, warmup_sessions: int) -> InstrumentData:
    check_instrument(session, instrument_id)
    provider = session.scalar(select(DataProvider).where(DataProvider.code == settings.marketdata_provider))
    if provider is None:
        raise BacktestRefused(RefusalCode.NO_DATA, f"active provider {settings.marketdata_provider!r} is not registered", instrument_id)
    rows = session.scalars(
        select(Candle)
        .where(
            Candle.instrument_id == instrument_id, Candle.provider_id == provider.id, Candle.timeframe == Timeframe.M5,
            Candle.is_canonical, Candle.session_date >= start - timedelta(days=lookback_days(warmup_sessions)), Candle.session_date <= end,
        )
        .order_by(Candle.bar_start_ts)
    ).all()
    bars = []
    for r in rows:
        if r.price_basis.value != PRICE_BASIS:
            raise BacktestRefused(RefusalCode.INVALID_DATA, f"candle {r.id} has price basis {r.price_basis.value}; the engine labels results {PRICE_BASIS}", instrument_id)
        try:
            # Normalise to UTC: the driver returns timestamptz in the connection's session time zone, and the
            # selection explanations record timestamps with their offset, so equal instants must look identical.
            start = r.bar_start_ts.astimezone(UTC)
            bars.append(BacktestBar(start, r.session_date, r.open, r.high, r.low, r.close, r.volume, (r.id, r.observation_version)))
        except ValueError as exc:
            raise BacktestRefused(RefusalCode.INVALID_DATA, f"candle {r.id}: {exc}", instrument_id) from None
    actions = session.scalars(
        select(CorporateAction).where(CorporateAction.instrument_id == instrument_id).order_by(CorporateAction.ex_date, CorporateAction.id)
    ).all()
    records = tuple(CorporateActionRecord(a.ex_date, a.action_type.value, _detail(a)) for a in actions)
    return InstrumentData(instrument_id, tuple(bars), records)
