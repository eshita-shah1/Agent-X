"""Archive fixtures for the backtest integration tests: candles are written directly (provider 'yahoo', canonical,
finalized, UNVERIFIED_PROVIDER_NATIVE) so no provider code or network is involved."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import BatchStatus, InstrumentType, JobStatus, JobType, Timeframe, TradingStatus
from app.db.models import Candle, CorporateAction, DataIngestionBatch, DataIngestionJob, DataProvider, Instrument
from app.marketdata.calendar import TradingCalendar

CAL = TradingCalendar()
DAYS = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10)]
OBSERVED = datetime(2026, 11, 11, 11, 0, tzinfo=UTC)


def provider(db: Session) -> DataProvider:
    row = db.scalar(select(DataProvider).where(DataProvider.code == "yahoo"))
    if row is None:
        row = DataProvider(code="yahoo", name="Yahoo Finance")
        db.add(row)
        db.commit()
    return row


def instrument(db: Session, symbol: str = "TESTCO", **kw) -> Instrument:
    inst = Instrument(symbol=symbol, exchange="NSE", instrument_type=kw.pop("instrument_type", InstrumentType.EQUITY), timezone="Asia/Kolkata", **kw)
    db.add(inst)
    db.commit()
    return inst


def disabled_future(db: Session) -> Instrument:
    return instrument(db, "NIFTYFUT", instrument_type=InstrumentType.FUTURE, trading_status=TradingStatus.DISABLED, disabled_reason="no contract data")


def archive(db: Session, inst: Instrument, days=DAYS, phase: float = 0.0) -> int:
    """Full 75-bar sessions of a deterministic oscillating series. Returns the number of candles written."""
    prov = provider(db)
    job = DataIngestionJob(job_type=JobType.BACKFILL, status=JobStatus.SUCCEEDED, provider_id=prov.id, timeframe=Timeframe.M5)
    db.add(job)
    db.commit()
    batch = DataIngestionBatch(
        job_id=job.id, instrument_id=inst.id, provider_id=prov.id, timeframe=Timeframe.M5, provider_symbol=f"{inst.symbol}.NS",
        request_start=OBSERVED - timedelta(days=30), request_end=OBSERVED, status=BatchStatus.SUCCEEDED,
    )
    db.add(batch)
    db.commit()
    rows, i = [], 0
    for day in days:
        for start in CAL.expected_bar_starts(CAL.session(day), "5m"):
            i += 1
            close = 100 + 6 * math.sin(i / 25 + phase) + 0.8 * math.sin(i / 3) + 0.3 * math.sin(i / 1.7)
            o, c = Decimal(f"{close + 0.15 * math.sin(i * 1.3):.2f}"), Decimal(f"{close:.2f}")
            wick = Decimal(f"{0.2 + 0.25 * abs(math.sin(i * 0.7)):.2f}")
            rows.append(
                Candle(
                    instrument_id=inst.id, provider_id=prov.id, timeframe=Timeframe.M5, bar_start_ts=start, bar_end_ts=start + timedelta(minutes=5),
                    session_date=day, open=o, high=max(o, c) + wick, low=min(o, c) - wick, close=c, volume=1000 + i % 300, is_final=True,
                    observed_at=OBSERVED, provider_symbol=f"{inst.symbol}.NS", ingestion_batch_id=batch.id,
                )
            )
    db.add_all(rows)
    db.commit()
    return len(rows)


def split(db: Session, inst: Instrument, ex_date: date) -> None:
    db.add(
        CorporateAction(
            instrument_id=inst.id, action_type="SPLIT", ex_date=ex_date, ratio_numerator=Decimal(2), ratio_denominator=Decimal(1),
            source="test", provider_id=provider(db).id, observed_at=OBSERVED,
        )
    )
    db.commit()


def body(instrument_ids, /, **over) -> dict:
    """A valid request: regime-priority over all five strategies with their approved defaults."""
    out = {
        "instrument_ids": list(instrument_ids),
        "start": "2026-11-04",
        "end": "2026-11-10",
        "mode": "REGIME_PRIORITY",
        "candidates": [
            {"config_id": "orb-1", "strategy_id": "ORB", "priority": 1},
            {"config_id": "ema-1", "strategy_id": "EMA_CROSSOVER", "priority": 2},
            {"config_id": "st-1", "strategy_id": "SUPERTREND", "priority": 3},
            {"config_id": "rsi-1", "strategy_id": "RSI_BOLLINGER", "priority": 4},
            {"config_id": "vwap-1", "strategy_id": "VWAP", "priority": 5},
        ],
    }
    out.update(over)
    return out
