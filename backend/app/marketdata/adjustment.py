"""Split-adjusted series, derived from raw candles + recorded corporate actions and stored SEPARATELY.

Why the care: the probe showed Yahoo's unadjusted history is *already* split-adjusted for splits that happened
before the data was fetched. Applying our own split factor to such a bar would adjust it twice. We therefore
adjust a bar for a split only when we hold evidence the bar was captured BEFORE that split took effect:

    candle.observed_at (local date)  <  split ex_date

For a bar captured on/after the ex-date the provider may or may not already have adjusted it; that is unknown,
so no adjusted row is written for that bar and the skip is reported. Nothing is guessed.

Dividends are NOT applied: total-return adjustment needs verified dividend data (not claimed in Phase 2).
Adjusted rows never replace or modify raw candles (separate table, separate write path).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import AdjustmentMethod, CorporateActionType, Timeframe
from app.db.models import AdjustedCandle, Candle, CorporateAction
from app.marketdata.calendar import EXCHANGE_TZ

QUANTUM = Decimal("0.000001")


@dataclass
class AdjustmentReport:
    action_set_version: str
    splits_considered: int
    candles_examined: int = 0
    adjusted_rows_written: int = 0
    unchanged_rows_written: int = 0
    skipped_ambiguous: int = 0
    skipped_candle_ids_sample: list[int] = field(default_factory=list)


def action_set_version(actions: list[CorporateAction]) -> str:
    """A stable fingerprint of the exact set of split actions used."""
    rows = sorted((a.ex_date.isoformat(), str(a.ratio_numerator), str(a.ratio_denominator)) for a in actions)
    digest = hashlib.sha256(repr(rows).encode()).hexdigest()[:12]
    return f"splits-{len(rows)}-{digest}"


def compute_split_adjusted(
    session: Session, *, instrument_id: int, provider_id: int, timeframe: Timeframe, source: str = "agentx-derived"
) -> AdjustmentReport:
    splits = list(
        session.scalars(
            select(CorporateAction)
            .where(
                CorporateAction.instrument_id == instrument_id,
                CorporateAction.action_type.in_([CorporateActionType.SPLIT, CorporateActionType.BONUS]),
            )
            .order_by(CorporateAction.ex_date)
        )
    )
    version = action_set_version(splits)
    report = AdjustmentReport(version, len(splits))

    already = set(
        session.scalars(
            select(AdjustedCandle.candle_id)
            .join(Candle, Candle.id == AdjustedCandle.candle_id)
            .where(
                Candle.instrument_id == instrument_id,
                AdjustedCandle.adjustment_method == AdjustmentMethod.SPLIT,
                AdjustedCandle.corporate_action_set_version == version,
            )
        )
    )

    candles = session.scalars(
        select(Candle).where(
            Candle.instrument_id == instrument_id,
            Candle.provider_id == provider_id,
            Candle.timeframe == timeframe,
            Candle.is_canonical,
        )
    )
    for candle in candles:
        report.candles_examined += 1
        if candle.id in already:
            continue
        observed_day: date = candle.observed_at.astimezone(EXCHANGE_TZ).date()
        factor = Decimal(1)
        ambiguous = False
        for s in splits:
            if s.ex_date <= candle.session_date:
                continue  # split happened before this bar: the bar is already on the post-split basis
            if observed_day < s.ex_date:
                factor /= s.ratio_numerator / s.ratio_denominator  # captured pre-split: scale down by new:old
            else:
                ambiguous = True  # captured on/after ex-date: provider may already have adjusted it
        if ambiguous:
            report.skipped_ambiguous += 1
            if len(report.skipped_candle_ids_sample) < 10:
                report.skipped_candle_ids_sample.append(candle.id)
            continue
        inv = Decimal(1) / factor
        q = lambda v: (v * factor).quantize(QUANTUM, rounding=ROUND_HALF_EVEN)  # noqa: E731
        session.add(
            AdjustedCandle(
                candle_id=candle.id,
                adjustment_method=AdjustmentMethod.SPLIT,
                adjustment_source=source,
                corporate_action_set_version=version,
                adjustment_factor=factor.quantize(Decimal("0.000000000001")),
                adj_open=q(candle.open), adj_high=q(candle.high), adj_low=q(candle.low), adj_close=q(candle.close),
                adj_volume=int((Decimal(candle.volume) * inv).to_integral_value(rounding=ROUND_HALF_EVEN)),
                notes="split-adjusted from raw; dividends not applied" if factor != 1 else "no split applies; equals raw",
            )
        )
        report.adjusted_rows_written += 1 if factor != 1 else 0
        report.unchanged_rows_written += 1 if factor == 1 else 0
    session.flush()
    return report
